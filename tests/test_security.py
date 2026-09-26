"""Exposed-secret scanning, and the `null-comparison` check that shipped with it.

Token-shaped test values are assembled at runtime and never appear as literals in this
file. A committed string shaped like a real GitHub or Slack token trips GitHub's push
protection and secret-scanning alerts on this repo, which is the very failure mode this
module exists to catch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dbt_sentinel.checks import CheckContext, check_null_comparison
from dbt_sentinel.cli import main
from dbt_sentinel.lineage import Lineage
from dbt_sentinel.models import ChangedFile, ChangeType
from dbt_sentinel.pipeline import review_pull_request
from dbt_sentinel.report import render_security
from dbt_sentinel.security import scan_file, scan_secrets
from tests.test_day8 import PR, FakeGitHub

REPO = Path(__file__).resolve().parents[1]
MANIFEST = str(REPO / "evals" / "manifest" / "manifest.json")


def _file(path: str, *added: str, removed: tuple[str, ...] = ()) -> ChangedFile:
    return ChangedFile(path, ChangeType.MODIFIED, tuple(added), removed)


def _kinds(path: str, *added: str) -> list[str]:
    return [f.kind for f in scan_file(_file(path, *added))]


def _diff(path: str, *added: str) -> str:
    body = "\n".join(f"+{line}" for line in added)
    return (
        f"diff --git a/{path} b/{path}\nindex 1..2 100644\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -1,0 +1,{len(added)} @@\n{body}\n"
    )


# ---------- provider formats ----------

@pytest.mark.parametrize(
    ("kind", "token"),
    [
        ("AWS access key id", "AKIA" + "Q" * 16),
        ("GitHub token", "ghp" + "_" + "a1" * 18),
        ("GitHub token", "github" + "_pat_" + "B" * 30),
        ("Slack token", "xox" + "b-" + "1234567890-abcdef"),
        ("Stripe live key", "sk" + "_live_" + "c" * 24),
        ("Anthropic API key", "sk-" + "ant-" + "d" * 30),
        ("OpenAI API key", "sk-" + "proj-" + "e" * 30),
        ("Google API key", "AI" + "za" + "f" * 35),
        ("private key", "-----BEGIN " + "RSA PRIVATE KEY-----"),
    ],
)
def test_each_provider_format_is_found_in_any_file_type(kind, token):
    assert _kinds("macros/export.sql", f"    select '{token}' as k") == [kind]
    assert _kinds("README.md", f"key: {token}") == [kind]


def test_anthropic_key_is_named_as_anthropic_not_openai():
    """Both share the `sk-` prefix. The first, more specific pattern must win."""
    assert _kinds("a.py", "KEY = 'sk-" + "ant-" + "x" * 30 + "'") == ["Anthropic API key"]


# ---------- credential assignments ----------

def test_literal_password_in_profiles_is_found():
    assert _kinds("profiles.yml", "      password: hunter2-prod") == ["literal value for `password`"]


def test_prefixed_credential_keys_are_found():
    assert _kinds(".env", "DB_PASSWORD=s3cret-value") == ["literal value for `DB_PASSWORD`"]
    assert _kinds("dbt_project.yml", "  client_secret: 'abc123def'") != []


@pytest.mark.parametrize(
    "line",
    [
        "      password: \"{{ env_var('DBT_PASSWORD') }}\"",
        "      password: '{{ env_var(\"DBT_PASSWORD\") }}'",
        "      password: ${SNOWFLAKE_PASSWORD}",
        "      password: <your-password>",
        "      password: '********'",
        "      token: xxxxxxxx",
        "      password: changeme",
        "      api_key: your_api_key_here",
        "      password:",
        "      private_key_path: /home/dbt/.ssh/rsa_key.p8",
        "      token_type: bearer",
        "      authenticator: externalbrowser",
        "      - name: password_hash",
        "        description: the user's password, hashed with bcrypt",
    ],
)
def test_references_placeholders_and_column_names_stay_silent(line):
    assert _kinds("profiles.yml", line) == []


def test_in_code_only_a_quoted_literal_counts():
    """`password = os.environ[...]` reads a secret; it doesn't expose one."""
    assert _kinds("scripts/load.py", "password = os.environ['DBT_PASSWORD']") == []
    assert _kinds("scripts/load.py", "password = 'plain-text-pw'") != []


def test_removed_lines_are_not_scanned():
    """Deleting a secret is the fix. Flagging the deletion would punish it."""
    file = _file("profiles.yml", removed=("      password: hunter2-prod",))
    assert scan_file(file) == []


def test_connection_url_with_inline_password_is_found():
    assert _kinds("profiles.yml", "  url: postgres://dbt:pa55word@db.internal:5432/x") == [
        "password in a connection URL"
    ]
    assert _kinds("profiles.yml", "  url: postgres://dbt:${PGPASSWORD}@db.internal/x") == []


def test_one_finding_per_kind_per_file():
    """Ten lines of the same leaked key are one thing to rotate, not ten findings."""
    token = "AKIA" + "Z" * 16
    assert len(scan_file(_file("m.sql", *[f"'{token}'"] * 10))) == 1


# ---------- rendering never re-publishes the secret ----------

def test_rendered_section_never_contains_the_secret():
    password = "hunter2-" + "prod-9x"
    token = "AKIA" + "R" * 16
    findings = scan_secrets([
        _file("profiles.yml", f"      password: {password}"),
        _file("macros/m.sql", f"'{token}'"),
    ])
    rendered = render_security(findings)
    assert password not in rendered and password[:4] not in rendered
    assert token not in rendered
    assert "AKIA…****" in rendered  # the public prefix, enough to find it
    assert "Rotate it now" in rendered


# ---------- the status gate ----------

def test_a_secret_fails_the_status_even_with_no_dbt_change():
    """profiles.yml resolves to no dbt node, so the blast radius is empty and LOW.
    The status must fail anyway, and the comment must not be deleted as empty."""
    client = FakeGitHub(diff=_diff("profiles.yml", "      password: hunter2-prod"))
    outcome = review_pull_request(client, PR, use_agent=False)
    assert outcome.severity == "low"
    assert outcome.status_state == "failure"
    assert "Exposed credential" in outcome.status_description
    assert not outcome.has_nothing_to_report
    # First in the comment, above the (empty) blast radius, because it needs action first.
    assert outcome.comment.index("## Security") < outcome.comment.index("## Blast radius")
    assert "hunter2-prod" not in outcome.comment


def test_a_secret_is_reported_even_when_no_manifest_is_available():
    """Secret scanning needs no manifest. A leaked key must not go unreported because
    the lineage half of the review could not run."""
    client = FakeGitHub(diff=_diff("profiles.yml", "      password: hunter2-prod"), files={})
    outcome = review_pull_request(client, PR, use_agent=False)
    assert not outcome.reviewed
    assert outcome.status_state == "failure"
    assert "## Security" in outcome.comment


def test_cli_exits_one_on_a_secret_at_every_threshold(tmp_path, capsys):
    diff = tmp_path / "leak.diff"
    diff.write_text(_diff("profiles.yml", "      password: hunter2-prod"), encoding="utf-8")
    for threshold in ("high", "medium", "low"):
        assert main(["--manifest", MANIFEST, "--diff", str(diff), "--fail-on", threshold]) == 1
    # `never` is a promise not to exit non-zero, and it holds.
    assert main(["--manifest", MANIFEST, "--diff", str(diff)]) == 0
    assert "## Security" in capsys.readouterr().out


def test_no_checks_does_not_skip_the_secret_scan(tmp_path, capsys):
    diff = tmp_path / "leak.diff"
    diff.write_text(_diff("profiles.yml", "      password: hunter2-prod"), encoding="utf-8")
    main(["--manifest", MANIFEST, "--diff", str(diff), "--no-checks"])
    assert "## Security" in capsys.readouterr().out


# ---------- null-comparison ----------

def _null(*added: str) -> list:
    ctx = CheckContext(Lineage.from_path(MANIFEST), frozenset())
    return check_null_comparison(_file("models/orders.sql", *added), None, ctx)


@pytest.mark.parametrize(
    ("line", "fix"),
    [
        ("where cancelled_at = null", "is null"),
        ("where cancelled_at = NULL", "is null"),
        ("where cancelled_at != null", "is not null"),
        ("where cancelled_at <> null", "is not null"),
        ("case when x=null then 0 end", "is null"),
    ],
)
def test_operator_comparison_with_null_fires(line, fix):
    [finding] = _null(line)
    assert finding.severity == "medium"
    assert f"`{fix}`" in finding.suggestion


@pytest.mark.parametrize(
    "line",
    [
        "where cancelled_at is null",
        "where cancelled_at is not null",
        "-- never write cancelled_at = null",
        "where x > 0  -- not = null",
        "{% if var('x', none) != none %} and y {% endif %}",
        "update orders set cancelled_at = null where id = 1",
        "select nullif(status, '') as status",
        "where nullable_flag = true",
    ],
)
def test_correct_null_handling_stays_silent(line):
    assert _null(line) == []


def test_null_comparison_reports_once_per_file():
    assert len(_null("where a = null", "and b = null")) == 1
