"""Regression tests for the GitHub App wiring.

No test touches the network: `GitHubClient` is replaced by a fake that records calls.
What is worth pinning here is the behaviour that only shows up in production — manifest
sourcing order, comment upsert instead of append, which severity blocks a merge, and
that a failed delivery does not raise into a GitHub retry storm.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dbt_sentinel.github import GitHubError, PullRequestRef, build_app_jwt
from dbt_sentinel.pipeline import COMMENT_MARKER, ReviewOutcome, review_pull_request, source_manifest
from dbt_sentinel.webhook import handle_event, run_review, summarise_payload

REPO = Path(__file__).resolve().parents[1]
MANIFEST_JSON = (REPO / "evals" / "manifest" / "manifest.json").read_text(encoding="utf-8")
BREAKING_DIFF = (
    REPO / "evals" / "fixtures" / "b01_column_rename_with_consumers.diff"
).read_text(encoding="utf-8")
NOOP_DIFF = (REPO / "evals" / "fixtures" / "p01_comment_added.diff").read_text(encoding="utf-8")


PR = PullRequestRef(owner="acme", repo="analytics", number=42, head_sha="deadbeef", base_ref="main")


class FakeGitHub:
    def __init__(
        self,
        diff: str = BREAKING_DIFF,
        files: dict[str, str] | None = None,
        artifact: dict | None = None,
        existing_comments: list[dict] | None = None,
        fail_on: str | None = None,
    ):
        self._diff = diff
        self._files = files if files is not None else {"target/manifest.json": MANIFEST_JSON}
        self._artifact = artifact
        self.comments = existing_comments or []
        self.statuses: list[tuple[str, str]] = []
        self.posted: list[str] = []
        self.patched: list[str] = []
        self.deleted: list[int] = []
        self._fail_on = fail_on

    def _maybe_fail(self, name: str) -> None:
        if self._fail_on == name:
            raise GitHubError(f"simulated failure in {name}")

    def fetch_diff(self, pr):
        self._maybe_fail("fetch_diff")
        return self._diff

    def fetch_file(self, pr, path, ref=None):
        self._maybe_fail("fetch_file")
        return self._files.get(path)

    def find_manifest_artifact(self, pr):
        self._maybe_fail("find_manifest_artifact")
        return self._artifact

    def post_comment(self, pr, body):
        self._maybe_fail("post_comment")
        self.posted.append(body)
        self.comments.append({"id": len(self.comments) + 1, "body": body})
        return self.comments[-1]

    def find_comment(self, pr, marker):
        self._maybe_fail("find_comment")
        for comment in self.comments:
            if marker in comment["body"]:
                return comment
        return None

    def delete_comment(self, pr, comment_id):
        self._maybe_fail("delete_comment")
        self.deleted.append(comment_id)
        self.comments = [c for c in self.comments if c["id"] != comment_id]

    def upsert_comment(self, pr, body, marker):
        self._maybe_fail("upsert_comment")
        existing = self.find_comment(pr, marker)
        if existing is not None:
            existing["body"] = body
            self.patched.append(body)
            return existing
        return self.post_comment(pr, body)

    def set_commit_status(self, pr, state, description, context="dbt-sentinel"):
        self._maybe_fail("set_commit_status")
        self.statuses.append((state, description))
        return {"state": state}


# ---------- manifest sourcing ----------


def test_committed_manifest_is_used_and_disclosed():
    """A committed manifest is usually stale, so using it is fine but silence is not."""
    client = FakeGitHub()
    lineage, source, warnings = source_manifest(client, PR)
    assert lineage is not None
    assert source == "committed:target/manifest.json"
    assert any("committed manifest" in w for w in warnings)


def test_missing_manifest_says_what_to_do():
    client = FakeGitHub(files={})
    lineage, source, warnings = source_manifest(client, PR)
    assert lineage is None and source == "none"
    assert any("dbt compile" in w or "manifest` artifact" in w for w in warnings)


def test_ci_artifact_is_preferred_and_its_absence_of_support_is_stated():
    """The artifact path is found but not yet downloadable. That gap must be visible in
    the comment rather than silently falling through to a stale manifest."""
    client = FakeGitHub(artifact={"name": "manifest", "expired": False, "id": 1})
    _, source, warnings = source_manifest(client, PR)
    assert any("artifact download is not yet implemented" in w for w in warnings)
    assert source == "committed:target/manifest.json"


def test_unusable_committed_manifest_is_reported_not_swallowed():
    client = FakeGitHub(files={"target/manifest.json": "{not json"})
    lineage, source, warnings = source_manifest(client, PR)
    assert lineage is None
    assert any("unusable" in w for w in warnings)


def test_local_manifest_overrides_remote_sourcing(tmp_path: Path):
    path = tmp_path / "manifest.json"
    path.write_text(MANIFEST_JSON, encoding="utf-8")
    _, source, _ = source_manifest(FakeGitHub(), PR, local_path=str(path))
    assert source.startswith("local:")


# ---------- the review ----------


def test_breaking_pr_is_high_and_blocks_the_merge():
    outcome = review_pull_request(FakeGitHub(), PR, use_agent=False)
    assert outcome.severity == "high"
    assert outcome.status_state == "failure"
    assert "stg_orders" in outcome.comment


def test_routine_pr_passes_the_gate():
    outcome = review_pull_request(FakeGitHub(diff=NOOP_DIFF), PR, use_agent=False)
    assert outcome.severity == "low"
    assert outcome.status_state == "success"


def test_medium_does_not_block():
    """A gate that fires on judgment calls gets switched off, taking HIGH with it."""
    assert ReviewOutcome(comment="", severity="medium").status_state == "success"


def test_comment_carries_the_marker_and_a_cost_footer():
    outcome = review_pull_request(FakeGitHub(), PR, use_agent=False)
    assert COMMENT_MARKER in outcome.comment
    assert "dbt-sentinel ·" in outcome.comment
    assert "manifest: `committed:target/manifest.json`" in outcome.comment


def test_warnings_are_rendered_in_the_comment():
    outcome = review_pull_request(FakeGitHub(), PR, use_agent=False)
    assert "Warnings and caveats" in outcome.comment


def test_diff_fetch_failure_still_produces_a_comment():
    """Degrade, don't crash: the reader must learn the review could not run."""
    outcome = review_pull_request(FakeGitHub(fail_on="fetch_diff"), PR, use_agent=False)
    assert "could not be fetched" in outcome.comment
    assert outcome.status_state == "success"  # nothing was assessed, so nothing blocks


def test_agent_skipped_without_a_key_is_disclosed(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    outcome = review_pull_request(FakeGitHub(), PR, use_agent=True)
    assert not outcome.agent_ran
    assert any("OPENAI_API_KEY" in w for w in outcome.warnings)


# ---------- comment upsert ----------


def test_rereview_updates_in_place_rather_than_appending():
    """A bot that comments per push turns a ten-push PR into a wall of stale reviews."""
    client = FakeGitHub(existing_comments=[{"id": 7, "body": f"{COMMENT_MARKER}\nold review"}])
    client.upsert_comment(PR, f"{COMMENT_MARKER}\nnew review", COMMENT_MARKER)
    assert len(client.comments) == 1
    assert "new review" in client.comments[0]["body"]
    assert client.patched and not client.posted


def test_first_review_posts_a_new_comment():
    client = FakeGitHub()
    client.upsert_comment(PR, f"{COMMENT_MARKER}\nreview", COMMENT_MARKER)
    assert client.posted and len(client.comments) == 1


def test_unrelated_comments_are_not_overwritten():
    client = FakeGitHub(existing_comments=[{"id": 1, "body": "a human review"}])
    client.upsert_comment(PR, f"{COMMENT_MARKER}\nreview", COMMENT_MARKER)
    assert client.comments[0]["body"] == "a human review"
    assert len(client.comments) == 2


def test_marker_is_found_beyond_the_first_page_of_comments(monkeypatch):
    """A busy PR must still update in place rather than posting a second review.

    The original single `per_page=100` request found nothing past comment 100, so the
    bot appended a duplicate on exactly the PRs where a duplicate is least welcome.
    """
    import dbt_sentinel.github as gh

    ours = {"id": 999, "body": f"{COMMENT_MARKER}\nour earlier review"}
    pages = {1: [{"id": i, "body": "chatter"} for i in range(100)], 2: [ours]}
    seen: list[str] = []

    def fake_request(method, url, token, *, body=None, accept="application/vnd.github+json"):
        seen.append(url)
        page = int(url.rsplit("page=", 1)[1])
        return pages.get(page, [])

    monkeypatch.setattr(gh, "_request", fake_request)
    found = gh.GitHubClient("t").find_comment(PR, COMMENT_MARKER)

    assert found == ours
    assert len(seen) == 2, "should have paginated rather than giving up after one page"


def test_pagination_stops_on_a_short_page(monkeypatch):
    """A short page is the last page — keep requesting and we burn the rate limit."""
    import dbt_sentinel.github as gh

    calls: list[str] = []

    def fake_request(method, url, token, *, body=None, accept="application/vnd.github+json"):
        calls.append(url)
        return [{"id": 1, "body": "unrelated"}]

    monkeypatch.setattr(gh, "_request", fake_request)
    assert gh.GitHubClient("t").find_comment(PR, COMMENT_MARKER) is None
    assert len(calls) == 1


def test_review_is_deleted_when_a_pr_stops_touching_dbt_files():
    """Editing a stale review down to "nothing to report" still reads as a result."""
    unrelated = (
        "diff --git a/README.md b/README.md\n"
        "--- a/README.md\n+++ b/README.md\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    client = FakeGitHub(
        diff=unrelated,
        existing_comments=[{"id": 7, "body": f"{COMMENT_MARKER}\nold HIGH review"}],
    )
    run_review(summarise_payload("pull_request", _pr_payload()), 99, client=client)

    assert client.deleted == [7]
    assert client.comments == []
    assert not client.patched and not client.posted


def test_nothing_to_delete_is_not_an_error():
    """No prior comment is the common case; it must not raise or post an empty review."""
    unrelated = (
        "diff --git a/README.md b/README.md\n"
        "--- a/README.md\n+++ b/README.md\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    client = FakeGitHub(diff=unrelated)
    result = run_review(summarise_payload("pull_request", _pr_payload()), 99, client=client)

    assert result["ok"] is True
    assert client.deleted == [] and client.posted == []


@pytest.mark.parametrize("broken", ["fetch_diff", "fetch_file"])
def test_a_failed_review_never_deletes_the_comment_explaining_it(broken):
    """A review that could not run resolves zero nodes — but must not read as clean.

    Both failure paths return changed_nodes=0, so a count-only test would delete the
    only notice the user gets that the tool is broken.
    """
    client = FakeGitHub(
        fail_on=broken,
        existing_comments=[{"id": 7, "body": f"{COMMENT_MARKER}\nprior review"}],
    )
    run_review(summarise_payload("pull_request", _pr_payload()), 99, client=client)
    assert client.deleted == []


def test_a_real_review_is_never_deleted():
    """Only an empty review deletes. A HIGH finding must survive as a comment."""
    client = FakeGitHub(existing_comments=[{"id": 7, "body": f"{COMMENT_MARKER}\nold"}])
    run_review(summarise_payload("pull_request", _pr_payload()), 99, client=client)

    assert client.deleted == []
    assert client.patched, "the existing review should have been updated in place"


# ---------- webhook routing ----------


def _pr_payload(action: str = "opened") -> dict:
    return {
        "action": action,
        "installation": {"id": 99},
        "repository": {"full_name": "acme/analytics"},
        "pull_request": {
            "number": 42,
            "head": {"sha": "deadbeef"},
            "base": {"ref": "main"},
        },
    }


def test_delivery_runs_the_review_and_sets_statuses():
    client = FakeGitHub()
    result = handle_event("pull_request", _pr_payload(), client=client)
    assert result["pipeline_ran"] is True
    assert [s[0] for s in client.statuses] == ["pending", "failure"]
    assert client.posted


def test_missing_installation_id_is_reported_not_crashed():
    payload = _pr_payload()
    del payload["installation"]
    result = handle_event("pull_request", payload)
    assert result["ok"] is False
    assert "installation id" in result["error"]


def test_closed_pr_does_not_run_the_pipeline():
    client = FakeGitHub()
    result = handle_event("pull_request", _pr_payload("closed"), client=client)
    assert result["pipeline_ran"] is False
    assert not client.statuses


def test_review_disabled_flag_keeps_log_only_behaviour():
    client = FakeGitHub()
    result = handle_event("pull_request", _pr_payload(), client=client, review=False)
    assert result["pipeline_ran"] is False
    assert not client.statuses


def test_github_failure_sets_error_status_and_does_not_raise():
    """A raising handler returns 500 and GitHub retries the same delivery forever."""
    client = FakeGitHub(fail_on="upsert_comment")
    result = run_review(summarise_payload("pull_request", _pr_payload()), 99, client=client)
    assert result["ok"] is False
    assert client.statuses[-1][0] == "error"


def test_status_description_is_truncated_for_github(monkeypatch):
    """GitHub returns 422 past 140 characters, so the real client must truncate.

    Asserts the request body the client actually builds, by intercepting its HTTP layer.
    An earlier version of this test grepped github.py for `[:140]`, which proved only
    that the source contained a string — it would have passed with the truncation dead
    and failed on a harmless rewrite.
    """
    from dbt_sentinel import github as gh

    captured: dict = {}

    def fake_request(method, url, token, *, body=None, accept="application/vnd.github+json"):
        captured["body"] = body
        return {"state": "success"}

    monkeypatch.setattr(gh, "_request", fake_request)
    gh.GitHubClient("tok").set_commit_status(PR, "success", "x" * 500)

    assert len(captured["body"]["description"]) == 140


def test_invalid_commit_status_state_is_rejected_before_the_call():
    from dbt_sentinel.github import GitHubClient

    with pytest.raises(ValueError, match="invalid commit status state"):
        GitHubClient("tok").set_commit_status(PR, "borked", "desc")


# ---------- app auth ----------


def test_jwt_has_three_segments_and_backdated_iat():
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    import base64 as b64

    token = build_app_jwt("12345", pem, now=1_000_000)
    segments = token.split(".")
    assert len(segments) == 3

    def _decode(segment: str) -> dict:
        return json.loads(b64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))

    payload = _decode(segments[1])
    # GitHub rejects a JWT whose iat is even a second in the future.
    assert payload["iat"] < 1_000_000
    assert payload["iss"] == "12345"
    assert payload["exp"] > payload["iat"]


def test_missing_app_credentials_says_which_env_vars(monkeypatch):
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("GITHUB_PRIVATE_KEY_PATH", raising=False)
    from dbt_sentinel.github import GitHubClient

    with pytest.raises(GitHubError, match="GITHUB_APP_ID"):
        GitHubClient.for_installation(1)
