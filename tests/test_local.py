"""`--since`: the CLI computes its own diff from git, for the VS Code post-commit hook.

The property that matters is parity. A developer who sees a clean local review and then
a HIGH on the PR stops trusting both, so `--since main` must reach the same verdict as
the PR's diff of the same change.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dbt_sentinel.cli import main

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

REPO = Path(__file__).resolve().parents[1]
MANIFEST = str(REPO / "evals" / "manifest" / "manifest.json")
B01 = str(REPO / "evals" / "fixtures" / "b01_column_rename_with_consumers.diff")
HOOKS = REPO / "integrations" / "vscode"

# Lines 13-17 of stg_orders.sql as b01's hunk sees them, padded so the hunk offset holds.
STG_ORDERS = "\n" * 12 + (
    "    select\n"
    "        id as order_id,\n"
    "        user_id as customer_id,\n"
    "        order_date,\n"
    "        status\n"
)


def _git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git repo whose `feature` branch carries b01's rename on top of `main`."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "core.autocrlf", "false")
    model = tmp_path / "models" / "staging" / "stg_orders.sql"
    model.parent.mkdir(parents=True)
    model.write_text(STG_ORDERS, encoding="utf-8", newline="\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _git(tmp_path, "checkout", "-q", "-b", "feature")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _rename_column(repo: Path) -> None:
    model = repo / "models" / "staging" / "stg_orders.sql"
    model.write_text(
        STG_ORDERS.replace("user_id as customer_id", "user_id as cust_id"),
        encoding="utf-8",
        newline="\n",
    )
    _git(repo, "commit", "-q", "-am", "rename")


def test_since_matches_the_pr_diff_verdict(repo, capsys):
    _rename_column(repo)
    since_exit = main(["--manifest", MANIFEST, "--since", "main", "--fail-on", "high"])
    since_out = capsys.readouterr().out

    diff_exit = main(["--manifest", MANIFEST, "--diff", B01, "--fail-on", "high"])
    diff_out = capsys.readouterr().out

    assert since_exit == diff_exit == 1
    assert "`stg_orders` — HIGH" in since_out
    # Same blast-radius section, byte for byte, once the local-only staleness line is cut.
    assert since_out[since_out.index("## Blast radius"):] == diff_out[diff_out.index("## Blast radius"):]


def test_since_diffs_from_the_merge_base_not_the_tip(repo, capsys):
    """Three dots, not two. A commit landing on main after the branch forked must not
    appear in the branch's review as if the branch had reverted it."""
    _rename_column(repo)
    _git(repo, "checkout", "-q", "main")
    (repo / "models" / "staging" / "stg_customers.sql").write_text("select 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "unrelated work on main")
    _git(repo, "checkout", "-q", "feature")

    main(["--manifest", MANIFEST, "--since", "main"])
    assert "stg_customers" not in capsys.readouterr().out


def test_a_utc_commit_date_does_not_crash(repo, capsys, monkeypatch):
    """Regression, found by CI's 3.10 run: git writes a UTC committer date as `...Z`,
    which `fromisoformat` rejects before 3.11, so the CLI crashed with a traceback
    instead of reviewing. A branch with no changed files takes the commit-time path."""
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-09-26T10:00:00+0000")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "utc commit")
    assert main(["--manifest", MANIFEST, "--since", "main"]) == 0


def test_branch_with_no_changes_says_so(repo, capsys):
    assert main(["--manifest", MANIFEST, "--since", "main"]) == 0
    assert "No dbt nodes changed" in capsys.readouterr().out


def test_since_warns_when_a_file_was_edited_after_the_manifest(repo, capsys):
    """The eval manifest is dated 2026-09-15 and the rename is written now, so the graph
    predates the edit. Locally the manifest is whatever `dbt parse` last wrote, so this
    warning must fire without --changed-at."""
    _rename_column(repo)
    main(["--manifest", MANIFEST, "--since", "main"])
    assert "Stale manifest" in capsys.readouterr().out


def test_manifest_parsed_after_the_edit_is_not_stale(repo, capsys):
    """Regression, found live on jeffle-shop: edit, `dbt parse`, commit warned "compiled
    0.0h before the change" because it compared against the commit, which always lands
    seconds after the parse. The edit is what must predate the manifest."""
    _rename_column(repo)
    model = repo / "models" / "staging" / "stg_orders.sql"
    # An hour before the eval manifest's generated_at, i.e. edited, then parsed.
    edited = datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc).timestamp()
    os.utime(model, (edited, edited))

    main(["--manifest", MANIFEST, "--since", "main"])
    out = capsys.readouterr().out
    assert "Stale manifest" not in out
    assert "`stg_orders` — HIGH" in out


def test_a_committed_manifest_and_non_dbt_files_do_not_count_as_edits(repo, capsys):
    """Regression, found live on jeffle-shop: `commit -am` after `dbt parse` puts
    target/manifest.json itself in the diff, and its mtime always trails its own
    generated_at. It, and files dbt never reads, must not make the graph look stale."""
    _rename_column(repo)
    model = repo / "models" / "staging" / "stg_orders.sql"
    edited = datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc).timestamp()
    os.utime(model, (edited, edited))
    (repo / "target").mkdir()
    shutil.copy(MANIFEST, repo / "target" / "manifest.json")
    (repo / ".gitignore").write_text(".sentinel/\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "commit the manifest")

    main(["--manifest", "target/manifest.json", "--since", "main"])
    assert "Stale manifest" not in capsys.readouterr().out


def test_unknown_ref_exits_two_with_guidance(repo, capsys):
    assert main(["--manifest", MANIFEST, "--since", "origin/nope"]) == 2
    err = capsys.readouterr().err
    assert "unknown ref 'origin/nope'" in err
    assert "git fetch" in err


def test_since_outside_a_git_repo_exits_two(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    assert main(["--manifest", MANIFEST, "--since", "main"]) == 2


def test_since_and_diff_are_mutually_exclusive(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--manifest", MANIFEST, "--since", "main", "--diff", B01])
    assert exc.value.code == 2


def test_one_diff_source_is_required(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--manifest", MANIFEST])
    assert exc.value.code == 2


def test_missing_default_manifest_names_the_command_that_makes_one(repo, capsys):
    assert main(["--since", "main"]) == 2
    assert "dbt parse" in capsys.readouterr().err


@pytest.mark.skipif(shutil.which("sh") is None, reason="sh not installed")
def test_review_script_is_silent_in_a_repo_without_dbt(repo):
    """The hook gets installed in repos that are not dbt projects. Those must see no
    output and no .sentinel/ directory."""
    completed = subprocess.run(
        ["sh", str(HOOKS / "sentinel-review.sh")], cwd=repo, capture_output=True, text=True
    )
    assert completed.returncode == 0
    assert completed.stdout == completed.stderr == ""
    assert not (repo / ".sentinel").exists()


@pytest.mark.skipif(shutil.which("sh") is None, reason="sh not installed")
def test_review_script_reports_a_missing_cli_in_the_review_file(repo):
    """An IDE's git often lacks the virtualenv on PATH. That must land in the file the
    developer is looking at, not vanish inside a backgrounded hook."""
    (repo / "dbt_project.yml").write_text("name: t\n", encoding="utf-8")
    _git(repo, "config", "sentinel.command", "definitely-not-installed-sentinel")
    _git(repo, "config", "sentinel.open", "false")
    subprocess.run(["sh", str(HOOKS / "sentinel-review.sh")], cwd=repo, check=True)
    review = (repo / ".sentinel" / "review.md").read_text(encoding="utf-8")
    assert "could not run" in review
    assert "git config sentinel.command" in review
