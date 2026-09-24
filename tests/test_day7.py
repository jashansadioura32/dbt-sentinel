"""Regression tests for the day-7 iteration round.

Each test here encodes one of the two failure modes the day-3 baseline measured and
deferred, per the build plan's "day 6 measures, day 7 fixes" split. They were written
before the fixes and failed on the pre-fix code; that ordering is what makes them
evidence rather than decoration.

Failure mode 1 — a YAML column edit whose hunk opens inside a `columns:` list resolved
to no node at all. 4 fixtures, 2 of them HIGH, and the output was silence rather than a
wrong severity, which is the shape design rule 4 exists to prevent.

Failure mode 2 — `is_structural` counted an added column as structural, so an additive
column scored the same HIGH as a rename. Drove the whole 0.200 false-positive rate.
"""

import os
from pathlib import Path

import pytest

from dbt_sentinel.diff import resolve_changes, yaml_columns_by_model, yaml_orphan_columns
from dbt_sentinel.lineage import Lineage
from dbt_sentinel.models import ChangedFile, ChangedNode, ChangeType, Node, NodeKind
from dbt_sentinel.report import build_assessments

EVAL_FIXTURES = Path(__file__).parent.parent / "evals" / "fixtures"
EVAL_MANIFEST = Path(__file__).parent.parent / "evals" / "manifest" / "manifest.json"


@pytest.fixture(scope="module")
def lineage() -> Lineage:
    return Lineage.from_path(EVAL_MANIFEST)


def _assess(lineage: Lineage, fixture_id: str):
    diff = (EVAL_FIXTURES / f"{fixture_id}.diff").read_text(encoding="utf-8")
    changes, unresolved = resolve_changes(diff, lineage)
    return build_assessments(changes, lineage), changes, unresolved


def _worst(assessments) -> str | None:
    order = {"low": 0, "medium": 1, "high": 2}
    if not assessments:
        return None
    return max(assessments, key=lambda a: order[a.severity]).severity


# ---------- failure mode 1: the hunk that opens inside a columns: list ----------


def test_hunk_opening_inside_columns_list_does_not_invent_a_model():
    """The b03 shape: no `columns:` line in the hunk, every `- name:` at column depth.

    Pre-fix, the first `- name: order_id` became the model heading and the real column
    names became dict keys, so nothing matched a model and the node was dropped.
    """
    hunk_lines = (
        "       - name: order_id",
        "         data_type: integer",
        "         description: Primary key. Contract-enforced.",
        "-      - name: customer_id",
        "-        data_type: integer",
    )
    per_model = yaml_columns_by_model(hunk_lines)

    # No model heading is discoverable from this hunk, so the parser must not guess one.
    assert per_model == {}, f"invented a model block: {per_model}"


def test_contract_column_dropped_is_not_silently_dropped(lineage: Lineage):
    """b03 must produce output. Wrong severity is recoverable; silence is not.

    `models/marts/schema.yml` documents two models and the hunk shows no model
    heading, so `customer_id` cannot be pinned to one of them. Attributing it to both
    would recreate the day-2 shared-schema false positive, so it surfaces as an
    explicit uncertainty at medium instead of a confident high.
    """
    assessments, changes, unresolved = _assess(lineage, "b03_contract_column_dropped")

    assert changes or unresolved, "b03 produced neither a resolved node nor a warning"
    if not changes:
        pytest.fail("b03 resolved to no node — the contract edit is invisible")

    assert "customer_id" in {
        col for c in changes for col in c.unattributed_removed_columns
    }, "the dropped contract column was neither attributed nor reported as uncertain"

    assert _worst(assessments) == "medium"
    assert any(
        "could not be attributed" in reason
        for a in assessments
        for reason in a.reasons
    ), "the uncertainty is not visible in the rendered reasons"


def test_a_single_model_schema_yml_does_attribute_the_orphan(lineage: Lineage):
    """When the file documents exactly one model there is only one possible owner.

    The uncertainty above is a property of a *shared* schema.yml, not a blanket refusal
    to attribute — otherwise the fix would trade a false negative for a vaguer one.
    """
    hunk_lines = (
        "       - name: order_id",
        "         data_type: integer",
        "-      - name: customer_id",
        "-        data_type: integer",
    )
    added, removed = yaml_orphan_columns(hunk_lines)
    # `order_id` is a context line, so it is unchanged and must not be reported.
    assert removed == {"customer_id"}
    assert added == set()


def test_exposure_owner_change_resolves_without_a_columns_block(lineage: Lineage):
    """s04 edits an exposures.yml, which has no `columns:` key anywhere.

    Pre-fix, `per_model` was empty and the membership gate discarded the exposure.
    """
    assessments, changes, unresolved = _assess(lineage, "s04_exposure_owner_change")

    assert changes or unresolved, "s04 produced neither a resolved node nor a warning"
    assert changes, "the exposure owner edit resolved to no node"
    assert any(c.node.kind is NodeKind.EXPOSURE for c in changes)


def test_a_yaml_node_with_no_attributable_columns_still_reports(lineage: Lineage):
    """The general invariant behind both fixtures above.

    A YAML edit that matches a real node must never vanish because the column parser
    could not attribute it. It reports with no columns rather than not reporting.
    """
    for fixture_id in (
        "b03_contract_column_dropped",
        "b06_type_narrowing_on_contract",
        "s03_contract_widened_safely",
        "s04_exposure_owner_change",
    ):
        _, changes, unresolved = _assess(lineage, fixture_id)
        assert changes or unresolved, f"{fixture_id} was dropped with no warning"


# ---------- failure mode 2: additive columns are not structural ----------


def _changed_node(added=(), removed=(), change_type=ChangeType.MODIFIED) -> ChangedNode:
    node = Node(unique_id="model.j.m", name="m", kind=NodeKind.MODEL)
    file = ChangedFile(path="models/m.sql", change_type=change_type)
    return ChangedNode(
        node=node,
        change_type=change_type,
        file=file,
        added_columns=tuple(added),
        removed_columns=tuple(removed),
    )


def test_an_added_column_alone_is_not_structural():
    """Adding a column breaks nobody. `select *` consumers pick it up; nothing drops."""
    assert _changed_node(added=("loaded_at",)).is_structural is False


def test_a_removed_column_is_structural():
    assert _changed_node(removed=("customer_id",)).is_structural is True


def test_deletion_and_rename_stay_structural():
    assert _changed_node(change_type=ChangeType.DELETED).is_structural is True
    assert _changed_node(change_type=ChangeType.RENAMED).is_structural is True


def test_additive_column_does_not_score_high(lineage: Lineage):
    """p10 is the deliberate twin of b01: same model, same reach, opposite semantics.

    Scoring them identically meant the tool reacted to *a column set changed* rather
    than *a column removed*.
    """
    assessments, _, _ = _assess(lineage, "p10_additive_column")
    assert _worst(assessments) != "high"


def test_the_additive_twin_still_diverges_from_the_rename(lineage: Lineage):
    """The pair must not converge by making b01 quiet too."""
    breaking, _, _ = _assess(lineage, "b01_column_rename_with_consumers")
    additive, _, _ = _assess(lineage, "p10_additive_column")
    assert _worst(breaking) == "high"
    assert _worst(additive) != _worst(breaking)


def test_a_test_name_under_a_tests_key_is_not_read_as_a_column():
    """`- not_null` under tests: must never become a column name.

    p03 documents an existing column and attaches a test to it, so `order_id` really
    is added YAML text and is allowed through as an added column — harmless, because
    added columns are no longer structural. What must not happen is the *test* names
    leaking in as columns.
    """
    hunk_lines = (
        "   - name: stg_payments",
        "     columns:",
        "+      - name: order_id",
        "+        tests:",
        "+          - not_null",
    )
    added, removed = yaml_columns_by_model(hunk_lines)["stg_payments"]
    assert "not_null" not in added
    assert added == {"order_id"}


def test_test_added_does_not_score_high(lineage: Lineage):
    assessments, _, _ = _assess(lineage, "p03_test_added")
    assert _worst(assessments) != "high"


# ---------- the eval harness must refuse to publish a failed agent arm ----------


def _comparison(fixture_id: str, *, errored: bool, detail: str, tokens: int = 0):
    """Build a Comparison row directly; the guard is pure and needs no API."""
    from evals.compare import ArmOutcome, Comparison

    row = Comparison(
        fixture_id=fixture_id,
        category="breaking",
        expected_severity="high",
        expected_rule_ids=[],
        baseline=ArmOutcome(severity="high", detail="baseline ran fine"),
        agent=ArmOutcome(
            severity=None if errored else "low", errored=errored, detail=detail
        ),
    )
    row.input_tokens = tokens
    return row


def test_a_total_agent_failure_is_refused_not_published():
    """The bug this encodes: a key was set, every call 429'd on exhausted credits, and
    `compare.py` still exited 0 and wrote a results file whose agent arm was 30 schema
    violations at $0.00 — indistinguishable from a measured result. The missing-key
    guard did not catch it because the key existed; the credits did not.
    """
    from evals.compare import _agent_arm_wipeout

    rows = [
        _comparison(f"f{i}", errored=True, detail="RateLimitError: 429 insufficient_quota")
        for i in range(5)
    ]
    assert _agent_arm_wipeout(rows) is not None


def test_a_zero_node_fixture_does_not_mask_a_wipeout():
    """The subtler half. A fixture resolving to no nodes short-circuits before any API
    call and records a real-looking `low` / "no findings" at 0 tokens. Keying the guard
    off `errored` let 3 such rows out of 30 defeat it, so it keys off token spend.
    """
    from evals.compare import _agent_arm_wipeout

    rows = [
        _comparison("b01", errored=True, detail="RateLimitError: 429"),
        _comparison("p04", errored=False, detail="no findings"),
    ]
    assert _agent_arm_wipeout(rows) is not None


def test_a_run_that_reached_the_model_is_published():
    """The guard must not block a real measurement that merely contains some errors."""
    from evals.compare import _agent_arm_wipeout

    rows = [
        _comparison("b01", errored=False, detail="real finding", tokens=1200),
        _comparison("b02", errored=True, detail="schema invalid after retry"),
    ]
    assert _agent_arm_wipeout(rows) is None


def test_the_wipeout_detail_names_the_cause():
    """An operator has to learn *why* from the refusal, per the error-message rule."""
    from evals.compare import _agent_arm_wipeout

    detail = _agent_arm_wipeout(
        [_comparison("b01", errored=True, detail="RateLimitError: credit_balance_exhausted")]
    )
    assert "credit_balance_exhausted" in detail


# ---------- the CLI must not die on a non-UTF8 console ----------


def test_cli_exits_zero_on_a_noop_pr_under_a_cp1252_console():
    """The rendered comment carries emoji severity badges and arrows.

    On a stock Windows console (cp1252) the CLI raised UnicodeEncodeError before
    printing anything and exited 1 — a clean no-op PR reported as a failed review. The
    1/2 exit split is the contract CI depends on, so this is run in a subprocess with
    the encoding actually forced rather than by asserting on a helper.
    """
    import subprocess
    import sys as _sys

    root = Path(__file__).parent.parent
    env = {
        **os.environ,
        "PYTHONIOENCODING": "cp1252",
        "PYTHONPATH": str(root / "src"),
    }
    completed = subprocess.run(
        [
            _sys.executable,
            "-m",
            "dbt_sentinel",
            "--manifest",
            str(root / "tests" / "manifest.json"),
            "--diff",
            str(root / "tests" / "noop.diff"),
            "--fail-on",
            "high",
        ],
        capture_output=True,
        env=env,
        cwd=str(root),
    )
    assert completed.returncode == 0, (
        f"a no-op PR exited {completed.returncode} under cp1252.\n"
        f"stderr: {completed.stderr.decode('utf-8', 'replace')[-600:]}"
    )


def test_cli_still_flags_a_breaking_pr_under_a_cp1252_console():
    """The encoding fix must not silence the tool to achieve exit 0."""
    import subprocess
    import sys as _sys

    root = Path(__file__).parent.parent
    env = {
        **os.environ,
        "PYTHONIOENCODING": "cp1252",
        "PYTHONPATH": str(root / "src"),
    }
    completed = subprocess.run(
        [
            _sys.executable,
            "-m",
            "dbt_sentinel",
            "--manifest",
            str(root / "tests" / "manifest.json"),
            "--diff",
            str(root / "tests" / "breaking.diff"),
            "--fail-on",
            "high",
        ],
        capture_output=True,
        env=env,
        cwd=str(root),
    )
    assert completed.returncode == 1
    assert b"HIGH" in completed.stdout
