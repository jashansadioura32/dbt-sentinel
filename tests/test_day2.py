"""Regression tests for the day-2 deterministic core.

Each of these encodes a bug found during the day-2 build. They exist so the day-4
retrieval work and day-5 agent cannot silently reintroduce them.
"""

from pathlib import Path

import pytest

from dbt_sentinel.diff import parse_diff, resolve_changes, yaml_columns_by_model
from dbt_sentinel.lineage import Lineage
from dbt_sentinel.report import build_assessments

FIXTURES = Path(__file__).parent
MANIFEST = FIXTURES / "manifest.json"


@pytest.fixture(scope="module")
def lineage() -> Lineage:
    return Lineage.from_path(MANIFEST)


def _assess(lineage: Lineage, diff_name: str):
    diff = (FIXTURES / diff_name).read_text()
    changes, unresolved = resolve_changes(diff, lineage)
    return build_assessments(changes, lineage), unresolved


# ---------- lineage ----------


def test_tests_are_not_downstream_consumers(lineage: Lineage):
    """A model with 12 tests must not look like it has 12 downstream consumers."""
    blast = lineage.blast_radius("model.jaffle.stg_orders")
    assert all(n.kind.value != "test" for n in blast.downstream)
    assert blast.size == 4


def test_blast_radius_reaches_transitive_exposure(lineage: Lineage):
    blast = lineage.blast_radius("model.jaffle.stg_orders")
    assert [n.name for n in blast.exposures] == ["exec_dashboard"]
    # exec_dashboard is 3 hops away via fct_orders -> rpt_revenue
    assert blast.depth_by_id["exposure.jaffle.exec_dashboard"] == 2


def test_max_depth_truncates(lineage: Lineage):
    assert lineage.blast_radius("model.jaffle.stg_orders", max_depth=1).size == 2


def test_schema_yml_resolves_via_patch_path(lineage: Lineage):
    assert {n.name for n in lineage.nodes_by_file_path("models/staging/schema.yml")} == {
        "stg_orders",
        "stg_customers",
    }


# ---------- diff parsing ----------


def test_diff_header_lines_are_not_content(lineage: Lineage):
    """`+++ b/path` and `--- a/path` must never be read as added/removed lines."""
    files = parse_diff((FIXTURES / "breaking.diff").read_text())
    for f in files:
        assert not any(line.startswith("+ b/") for line in f.added_lines)
        assert not any(line.startswith(" a/") for line in f.removed_lines)


def test_yaml_columns_scoped_to_their_model():
    hunk = (
        " models:",
        "   - name: stg_orders",
        "     columns:",
        "       - name: order_id",
        "-      - name: customer_id",
        "+      - name: cust_id",
        "   - name: stg_customers",
        "     columns:",
        "       - name: customer_id",
    )
    scoped = yaml_columns_by_model(hunk)
    assert scoped["stg_orders"] == ({"cust_id"}, {"customer_id"})
    assert scoped["stg_customers"] == (set(), set())


# ---------- false positives found on day 2 ----------


def test_shared_schema_yml_does_not_flag_unrelated_model(lineage: Lineage):
    """BUG: editing stg_orders' columns flagged stg_customers, which merely declares a
    column of the same name."""
    assessments, _ = _assess(lineage, "breaking.diff")
    assert "stg_customers" not in {a.changed.node.name for a in assessments}


def test_node_reported_once_across_sql_and_yaml(lineage: Lineage):
    """BUG: a model changed in both its .sql and its schema.yml appeared twice."""
    assessments, _ = _assess(lineage, "breaking.diff")
    names = [a.changed.node.name for a in assessments]
    assert len(names) == len(set(names))


def test_comment_only_change_is_not_high(lineage: Lineage):
    """BUG: a comment-only edit scored HIGH because it sat upstream of an exposure.
    Reach amplifies risk; it does not create it."""
    assessments, _ = _assess(lineage, "noop.diff")
    assert [a.severity for a in assessments] == ["low"]


# ---------- true positives must survive the fixes ----------


def test_breaking_rename_still_scores_high(lineage: Lineage):
    assessments, _ = _assess(lineage, "breaking.diff")
    top = assessments[0]
    assert top.changed.node.name == "stg_orders"
    assert top.severity == "high"
    assert "customer_id" in top.changed.removed_columns


def test_unmappable_files_are_surfaced_not_dropped(lineage: Lineage):
    """Silence on an unresolvable file is the dangerous failure mode."""
    _, unresolved = _assess(lineage, "breaking.diff")
    assert [f.path for f in unresolved] == ["macros/cents_to_dollars.sql"]
