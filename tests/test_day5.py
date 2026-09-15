"""Regression tests for agent v0 and the webhook skeleton.

These test the two things the session prioritised above review quality: the LLM never
writes the comment, and every failure path degrades visibly to deterministic-only.

No test touches the network. The Anthropic client is injected as a fake, which is also
why `ReviewerAgent` takes a `client` argument at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dbt_sentinel.agent import (
    Finding,
    FindingsResponse,
    ReviewerAgent,
    ToolBox,
    build_user_prompt,
)
from dbt_sentinel.diff import resolve_changes
from dbt_sentinel.lineage import Lineage
from dbt_sentinel.report import build_assessments, render_agent_findings
from dbt_sentinel.retrieval import PolicyPack
from dbt_sentinel.webhook import (
    SignatureError,
    compute_signature,
    handle_event,
    summarise_payload,
    verify_signature,
)

REPO = Path(__file__).resolve().parents[1]
EVAL_FIXTURES = REPO / "evals" / "fixtures"
MANIFEST = REPO / "evals" / "manifest" / "manifest.json"


@pytest.fixture(scope="module")
def lineage() -> Lineage:
    return Lineage.from_path(MANIFEST)


@pytest.fixture(scope="module")
def pack() -> PolicyPack:
    return PolicyPack.load()


@pytest.fixture(scope="module")
def assessments(lineage: Lineage):
    diff = (EVAL_FIXTURES / "b01_column_rename_with_consumers.diff").read_text(encoding="utf-8")
    changes, _ = resolve_changes(diff, lineage)
    return build_assessments(changes, lineage)


# ---------- fake client ----------


class _Block:
    def __init__(self, type_: str, name: str = "", input_: dict | None = None, id_: str = "t1"):
        self.type = type_
        self.name = name
        self.input = input_ or {}
        self.id = id_


class _Usage:
    def __init__(self, input_tokens: int = 10, output_tokens: int = 20):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _Response:
    def __init__(self, content: list[_Block]):
        self.content = content
        self.usage = _Usage()


class FakeClient:
    """Replays a scripted list of responses; records the requests it received."""

    def __init__(self, responses: list[_Response] | Exception):
        self._responses = responses
        self.requests: list[dict] = []
        self.messages = self  # client.messages.create(...)

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if isinstance(self._responses, Exception):
            raise self._responses
        if not self._responses:
            raise AssertionError("FakeClient ran out of scripted responses")
        return self._responses.pop(0)


def _submit(findings: list[dict]) -> _Response:
    return _Response([_Block("tool_use", "submit_findings", {"findings": findings})])


VALID_FINDING = {
    "rule_id": "contract-breaking-change",
    "severity": "high",
    "model": "stg_orders",
    "explanation": "customer_id was renamed, so three downstream models fail to compile.",
    "suggested_fix": "Add cust_id additively, migrate consumers, then drop customer_id.",
}


# ---------- invariant 1: structured output, never prose ----------


def test_valid_findings_are_returned_structured(lineage, pack, assessments):
    agent = ReviewerAgent(lineage, pack, client=FakeClient([_submit([VALID_FINDING])]))
    result = agent.review(assessments)
    assert result.ran
    assert not result.degraded
    assert [f.rule_id for f in result.findings] == ["contract-breaking-change"]
    assert isinstance(result.findings[0], Finding)


def test_prose_without_tool_call_degrades(lineage, pack, assessments):
    """The model answering in Markdown is a degradation, not a result. Its text is
    discarded by design, so there is nothing to render."""
    prose = _Response([_Block("text")])
    agent = ReviewerAgent(lineage, pack, client=FakeClient([prose]))
    result = agent.review(assessments)
    assert result.degraded
    assert "prose" in result.degradation_reason


def test_model_cannot_inject_markdown_through_rule_id():
    """rule_id and model land inside backticks in the comment. A backtick would break
    out of the code span and let the model control layout."""
    with pytest.raises(ValueError, match="backticks"):
        Finding(
            rule_id="x`--> **INJECTED**",
            severity="high",
            model="stg_orders",
            explanation="e",
            suggested_fix="f",
        )


def test_severity_outside_the_vocabulary_is_rejected():
    with pytest.raises(Exception):
        Finding(
            rule_id="r",
            severity="catastrophic",
            model="m",
            explanation="e",
            suggested_fix="f",
        )


def test_findings_response_caps_list_length():
    too_many = {"findings": [VALID_FINDING] * 26}
    with pytest.raises(Exception):
        FindingsResponse.model_validate(too_many)


# ---------- invariant 2: degrade visibly ----------


def test_invalid_schema_retries_once_then_succeeds(lineage, pack, assessments):
    bad = _submit([{"rule_id": "r", "severity": "high"}])  # missing required fields
    good = _submit([VALID_FINDING])
    client = FakeClient([bad, good])
    result = ReviewerAgent(lineage, pack, client=client).review(assessments)
    assert result.ran, result.degradation_reason
    assert len(result.validation_errors) == 1  # the retry was fed the error
    assert len(client.requests) == 2


def test_invalid_schema_twice_degrades(lineage, pack, assessments):
    bad = _submit([{"rule_id": "r", "severity": "high"}])
    client = FakeClient([bad, bad])
    result = ReviewerAgent(lineage, pack, client=client).review(assessments)
    assert result.degraded
    assert "schema validation" in result.degradation_reason
    assert result.findings == []


def test_api_exception_degrades_without_raising(lineage, pack, assessments):
    """Rule 5: a timeout or rate limit must not take the review down."""
    client = FakeClient(TimeoutError("request timed out"))
    result = ReviewerAgent(lineage, pack, client=client).review(assessments)
    assert result.degraded
    assert "TimeoutError" in result.degradation_reason


def test_missing_api_key_degrades_with_actionable_message(lineage, pack, assessments, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = ReviewerAgent(lineage, pack, client=None).review(assessments)
    assert result.degraded
    assert "ANTHROPIC_API_KEY" in result.degradation_reason
    assert "--agent" in result.degradation_reason  # says what to do instead


def test_tool_loop_that_never_submits_degrades(lineage, pack, assessments):
    """A model that keeps calling tools forever must hit a ceiling, not spin."""
    tool_call = _Response([_Block("tool_use", "get_lineage", {"model": "stg_orders"})])
    client = FakeClient([tool_call] * 10)
    result = ReviewerAgent(lineage, pack, client=client).review(assessments)
    assert result.degraded
    assert "tool rounds" in result.degradation_reason


def test_no_assessments_is_not_a_degradation(lineage, pack):
    """An empty PR is a clean result, not a failure."""
    result = ReviewerAgent(lineage, pack, client=FakeClient([])).review([])
    assert not result.degraded
    assert result.findings == []


def test_empty_findings_list_is_valid(lineage, pack, assessments):
    result = ReviewerAgent(lineage, pack, client=FakeClient([_submit([])])).review(assessments)
    assert result.ran
    assert result.findings == []


# ---------- invariant 3: lookups are tools, the manifest is not context ----------


def test_prompt_does_not_contain_the_manifest(assessments, pack):
    prompt = build_user_prompt(assessments, pack)
    assert "child_map" not in prompt
    assert "unique_id" not in prompt
    assert "dbt_schema_version" not in prompt


def test_prompt_carries_deterministic_analysis(assessments, pack):
    prompt = build_user_prompt(assessments, pack)
    assert "stg_orders" in prompt
    assert "downstream nodes: 7" in prompt
    assert "deterministic severity" in prompt


def test_toolbox_lineage_matches_graph_traversal(lineage):
    box = ToolBox(lineage)
    out = box.get_lineage("stg_orders")
    assert out["downstream_count"] == 7
    assert {e["name"] for e in out["exposures"]} == {
        "exec_dashboard",
        "finance_month_end",
        "customer_success_churn",
    }


def test_toolbox_unknown_model_returns_error_not_exception(lineage):
    assert "error" in ToolBox(lineage).get_lineage("does_not_exist")


def test_toolbox_dispatch_survives_a_bad_payload(lineage):
    out = ToolBox(lineage).dispatch("get_lineage", {}, None)
    assert "error" in out


def test_toolbox_records_calls(lineage, pack):
    box = ToolBox(lineage, pack)
    box.dispatch("get_columns", {"model": "stg_orders"}, None)
    assert box.calls == [("get_columns", {"model": "stg_orders"})]


# ---------- rendering is deterministic template code ----------


def test_renderer_marks_degradation_visibly(assessments):
    from dbt_sentinel.agent import AgentResult

    degraded = AgentResult(degraded=True, degradation_reason="TimeoutError: boom")
    out = render_agent_findings(degraded)
    assert "deterministic" in out.lower()
    assert "TimeoutError" in out


def test_renderer_escapes_model_supplied_text(assessments):
    from dbt_sentinel.agent import AgentResult

    nasty = Finding(
        rule_id="structural",
        severity="high",
        model="stg_orders",
        explanation="Broke\n## Injected heading\n- fake bullet",
        suggested_fix="Fix it",
    )
    out = render_agent_findings(AgentResult(findings=[nasty]))
    # Newlines in model text must not create new Markdown block structure.
    assert "\n## Injected heading" not in out


# ---------- webhook: signature verification ----------


def test_valid_signature_passes():
    body = b'{"action":"opened"}'
    verify_signature("s3cret", body, compute_signature("s3cret", body))


def test_wrong_secret_fails():
    body = b'{"action":"opened"}'
    with pytest.raises(SignatureError, match="mismatch"):
        verify_signature("s3cret", body, compute_signature("other", body))


def test_missing_signature_header_fails():
    with pytest.raises(SignatureError, match="missing"):
        verify_signature("s3cret", b"{}", None)


def test_malformed_signature_prefix_fails():
    with pytest.raises(SignatureError, match="malformed"):
        verify_signature("s3cret", b"{}", "md5=abc")


def test_unset_secret_is_a_server_error_not_an_auth_failure():
    """Told apart deliberately: the sender should know whether they are unauthorised or
    whether we are misconfigured."""
    with pytest.raises(SignatureError, match="GITHUB_WEBHOOK_SECRET is not set"):
        verify_signature("", b"{}", "sha256=abc")


def test_signature_is_computed_over_raw_bytes():
    """Verification must run on the raw body, before parsing.

    Re-serialising is not byte-preserving: GitHub sends compact separators and its own
    key order, so any round-trip through json.loads/dumps can change the bytes and break
    the digest for a legitimate payload. Both differences below are ones a naive
    "parse, then re-encode, then verify" implementation would introduce.
    """
    raw = b'{"zebra":1,"apple":2}'
    assert json.dumps(json.loads(raw)).encode() != raw  # spacing differs
    assert json.dumps(json.loads(raw), sort_keys=True).encode() != raw  # order too

    for reserialised in (
        json.dumps(json.loads(raw)).encode(),
        json.dumps(json.loads(raw), sort_keys=True).encode(),
    ):
        assert compute_signature("k", raw) != compute_signature("k", reserialised)


# ---------- webhook: routing ----------


def test_actionable_pull_request_is_recognised():
    payload = {
        "action": "opened",
        "repository": {"full_name": "o/r"},
        "pull_request": {"number": 7, "head": {"sha": "abc"}, "base": {"ref": "main"}},
    }
    summary = summarise_payload("pull_request", payload)
    assert summary["actionable"] and summary["pr_number"] == 7


def test_closed_pull_request_is_not_actionable():
    summary = summarise_payload("pull_request", {"action": "closed", "pull_request": {}})
    assert not summary["actionable"]


def test_ping_is_answered():
    assert handle_event("ping", {})["pong"] is True


def test_unhandled_event_is_acknowledged_not_errored():
    out = handle_event("issues", {"action": "opened"})
    assert out["ok"] and out["ignored"]


def test_stub_states_plainly_that_the_pipeline_did_not_run():
    """A deployment that silently does nothing looks identical to one that works."""
    payload = {
        "action": "opened",
        "repository": {"full_name": "o/r"},
        "pull_request": {"number": 1, "head": {"sha": "s"}, "base": {"ref": "main"}},
    }
    assert handle_event("pull_request", payload)["pipeline_ran"] is False
