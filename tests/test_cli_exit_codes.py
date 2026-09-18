"""The CLI's exit-code contract, which CI depends on to tell findings from breakage.

    0  Reviewed. Nothing at or above --fail-on.
    1  Reviewed. Findings at or above --fail-on.
    2  Could not run.

The 1/2 split is the one that matters. A missing manifest reported as exit 1 looks
exactly like a breaking change, and a tool that cries wolf on its own misconfiguration
gets switched off long before it ever reports a real one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dbt_sentinel.cli import main

REPO = Path(__file__).resolve().parents[1]
MANIFEST = str(REPO / "evals" / "manifest" / "manifest.json")
BREAKING = str(REPO / "evals" / "fixtures" / "b01_column_rename_with_consumers.diff")
NOOP = str(REPO / "evals" / "fixtures" / "p01_comment_added.diff")


def test_clean_review_exits_zero(capsys):
    assert main(["--manifest", MANIFEST, "--diff", NOOP, "--fail-on", "high"]) == 0


def test_finding_at_threshold_exits_one(capsys):
    assert main(["--manifest", MANIFEST, "--diff", BREAKING, "--fail-on", "high"]) == 1


def test_finding_below_threshold_exits_zero(capsys):
    """Same HIGH finding, threshold never — the gate is opt-in, not the default."""
    assert main(["--manifest", MANIFEST, "--diff", BREAKING, "--fail-on", "never"]) == 0


def test_missing_diff_exits_two_not_one(capsys):
    """Regression: the read was unguarded, so a missing file raised and exited 1."""
    code = main(["--manifest", MANIFEST, "--diff", "no/such/file.diff", "--fail-on", "high"])
    assert code == 2
    assert "could not read" in capsys.readouterr().err.lower()


def test_missing_manifest_exits_two(capsys):
    code = main(["--manifest", "no/such/manifest.json", "--diff", NOOP])
    assert code == 2


def test_malformed_changed_at_exits_two(capsys):
    code = main(["--manifest", MANIFEST, "--diff", NOOP, "--changed-at", "last tuesday"])
    assert code == 2
    assert "iso-8601" in capsys.readouterr().err.lower()


@pytest.mark.parametrize("bad", ["", "  "])
def test_unreadable_diff_path_is_a_config_error_not_a_finding(bad, capsys):
    """An empty --diff is a directory read on POSIX and a blank path on Windows.

    Either way it is the caller's mistake, not a risky PR.
    """
    assert main(["--manifest", MANIFEST, "--diff", bad]) == 2
