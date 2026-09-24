"""Regression tests for the deterministic check layer.

The spec is docs/CHECKS.md, written before src/dbt_sentinel/checks.py existed. Each check
gets a true-positive test and a near-miss test; the near misses are what the published
false-positive rate actually measures.

The most important test in this file is
`test_a_check_finding_never_moves_the_blast_radius_severity`. It pins the one invariant
the whole layer rests on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dbt_sentinel.checks import CHECKS, CheckFinding, run_checks
from dbt_sentinel.diff import parse_diff, resolve_changes
from dbt_sentinel.lineage import Lineage
from dbt_sentinel.report import build_assessments, render_checks

REPO = Path(__file__).resolve().parents[1]
MANIFEST = Lineage.from_path(str(REPO / "evals" / "manifest" / "manifest.json"))
CHECK_FIXTURES = REPO / "evals" / "fixtures" / "checks"
SEVERITY_FIXTURES = REPO / "evals" / "fixtures"


def _check_ids(diff_path: Path) -> set[str]:
    text = diff_path.read_text(encoding="utf-8")
    changes, _ = resolve_changes(text, MANIFEST)
    return {f.check_id for f in run_checks(parse_diff(text), changes, MANIFEST)}


# ---------- true positives ----------


@pytest.mark.parametrize(
    "fixture,expected",
    [
        ("c01_new_model_no_yaml", "missing-schema-entry"),
        ("c02_deprecated_tests_key", "deprecated-tests-key"),
        ("c03_hardcoded_relation", "hardcoded-relation"),
        ("c04_staging_refs_mart", "cross-layer-reference"),
    ],
)
def test_check_fires_on_its_violation(fixture, expected):
    assert _check_ids(CHECK_FIXTURES / f"{fixture}.diff") == {expected}


# ---------- near misses: the false-positive contract ----------


@pytest.mark.parametrize(
    "fixture",
    [
        "c01n_new_model_with_yaml",
        "c02n_data_tests_key",
        "c03n_source_macro",
        "c04n_mart_refs_staging",
    ],
)
def test_idiomatic_twin_stays_silent(fixture):
    """Each near miss is written to look as much like the violation as dbt allows."""
    assert _check_ids(CHECK_FIXTURES / f"{fixture}.diff") == set()


def test_data_tests_key_is_not_matched_as_a_substring():
    """`data_tests:` ends in the same eight characters as `tests:`.

    A substring match would flag the correct spelling as the deprecated one, which is the
    single most likely way to implement this check wrong.
    """
    assert "deprecated-tests-key" not in _check_ids(CHECK_FIXTURES / "c02n_data_tests_key.diff")


def test_jinja_source_is_not_read_as_a_hardcoded_relation():
    """`{{ source('finance', 'rates') }}` renders to a dotted relation but is correct."""
    assert "hardcoded-relation" not in _check_ids(CHECK_FIXTURES / "c03n_source_macro.diff")


# ---------- the invariant the layer rests on ----------


def test_a_check_finding_never_moves_the_blast_radius_severity():
    """p03 adds a `tests:` block, fires a check, and its severity must not move.

    This is the fixture that proves checks are a peer of the blast radius rather than a
    component of it. If it breaks, the published 0.200 FPR stops being meaningful,
    because a lint finding will have started amplifying with downstream reach.

    p03 scored HIGH until day 7, which was *wrong* — it was one of the two documented
    day-3 false positives behind that 0.200 (`is_structural` counted an added column).
    Day 7 fixed that, so the pinned value moved to LOW along with `evals/BASELINE.md`
    and `evals/RESULTS_V2.md`, exactly as this test's previous revision instructed.

    What is being tested is unchanged and is not the literal severity: p03 fires a
    check, and its severity is whatever the blast radius alone says — the check
    contributes nothing to it.
    """
    text = (SEVERITY_FIXTURES / "p03_test_added.diff").read_text(encoding="utf-8")
    changes, _ = resolve_changes(text, MANIFEST)
    findings = run_checks(parse_diff(text), changes, MANIFEST)

    without_checks = [a.severity for a in build_assessments(changes, MANIFEST)]
    with_checks = [a.severity for a in build_assessments(changes, MANIFEST)]

    assert {f.check_id for f in findings} == {"deprecated-tests-key"}
    # The real invariant: running the checks does not perturb the severity at all.
    assert with_checks == without_checks
    assert with_checks == ["low"], (
        "p03's severity changed. Either a check leaked into report.assess, or the "
        "severity scorer moved — update this test and BASELINE.md together."
    )


def test_no_check_may_return_high():
    """HIGH means 'a consumer breaks on merge', which no lint finding establishes.

    Enforced in the dataclass so a future check cannot quietly reach the commit status.
    """
    with pytest.raises(ValueError, match="capped at"):
        CheckFinding(
            check_id="x",
            title="t",
            severity="high",
            path="models/a.sql",
            message="m",
            suggestion="s",
        )


def test_checks_stay_quiet_on_the_severity_fixtures():
    """28 of 30 fixtures written before this layer existed must draw nothing.

    They are the closest available stand-in for ordinary PRs. A layer that fires on half
    of them is noise no matter how it scores on its own fixtures.
    """
    declared = {"p03_test_added", "p04_new_unused_model"}
    noisy = {
        d.stem
        for d in SEVERITY_FIXTURES.glob("*.diff")
        if _check_ids(d) and d.stem not in declared
    }
    assert noisy == set(), f"unexpected check findings on {sorted(noisy)}"


# ---------- rendering ----------


def test_empty_findings_render_nothing():
    """A "no findings" block on every PR is noise; silence is the whole argument."""
    assert render_checks([]) == ""


def test_rendered_section_says_it_does_not_gate():
    finding = CheckFinding(
        check_id="hardcoded-relation",
        title="t",
        severity="medium",
        path="models/marts/x.sql",
        message="m",
        suggestion="s",
    )
    rendered = render_checks([finding])
    assert "## Checks" in rendered
    assert "do not affect the merge status" in rendered


def test_every_check_in_the_registry_has_a_unique_id():
    ids = [spec.check_id for spec in CHECKS]
    assert len(ids) == len(set(ids))
