"""Regression tests for shapes only a real compiled manifest produces.

Every test here encodes a failure found by pointing the day-2 core at a manifest
compiled from dbt-labs/jaffle_shop on Windows. The synthetic day-2 fixture used
forward-slash paths and a `package://` patch_path prefix, which hid all of them.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from dbt_sentinel.lineage import Lineage

FIXTURES = Path(__file__).parent


@pytest.fixture(scope="module")
def win_manifest() -> dict:
    """A manifest carrying the separators and patch_path shape dbt emits on Windows."""
    return json.loads((FIXTURES / "manifest_windows.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def win_lineage(win_manifest: dict) -> Lineage:
    return Lineage.from_manifest(win_manifest)


def test_backslash_model_path_resolves(win_lineage: Lineage):
    """BUG: a Windows-compiled manifest stores `models\\staging\\stg_orders.sql`; the diff
    says `models/staging/stg_orders.sql`. Raw comparison resolved nothing, so the CLI
    reported a clean review on a breaking PR and --fail-on high exited 0."""
    names = [n.name for n in win_lineage.nodes_by_file_path("models/staging/stg_orders.sql")]
    assert "stg_orders" in names


def test_backslash_patch_path_resolves_every_documented_model(win_lineage: Lineage):
    """`jaffle_shop://models\\staging\\schema.yml` must strip the package prefix AND
    normalise the separators. Getting only one of the two still resolves nothing."""
    names = {n.name for n in win_lineage.nodes_by_file_path("models/staging/schema.yml")}
    assert names == {"stg_orders", "stg_customers", "stg_payments"}


def test_seed_with_null_patch_path_resolves(win_lineage: Lineage):
    """Real seeds carry `patch_path: None`. The synthetic fixture had no seed node, so
    the None branch was never exercised."""
    names = [n.name for n in win_lineage.nodes_by_file_path("seeds/raw_orders.csv")]
    assert names == ["raw_orders"]


def test_blast_radius_on_backslash_manifest_is_not_empty(win_lineage: Lineage):
    """End-to-end consequence of the path bug: reach collapsed to zero."""
    blast = win_lineage.blast_radius("model.jaffle_shop.stg_orders")
    assert {n.name for n in blast.downstream} == {"customers", "orders"}


def test_forward_slash_manifest_still_resolves(win_lineage: Lineage):
    """The fix must not regress manifests compiled on Linux/macOS, which is what CI
    will produce for the same project."""
    manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    lineage = Lineage.from_manifest(manifest)
    assert {n.name for n in lineage.nodes_by_file_path("models/staging/schema.yml")} == {
        "stg_orders",
        "stg_customers",
    }


# ---------- manifest freshness ----------


def test_generated_at_is_parsed(win_lineage: Lineage):
    assert win_lineage.generated_at is not None
    assert win_lineage.generated_at.tzinfo is not None  # comparisons must never raise


def test_stale_manifest_warns(win_lineage: Lineage):
    """A manifest compiled before the change under review under-reports blast radius."""
    later = win_lineage.generated_at + timedelta(hours=6)
    warning = win_lineage.staleness_warning(later)
    assert warning is not None
    assert "6.0h" in warning
    assert "dbt compile" in warning  # the message must say what to do next


def test_current_manifest_does_not_warn(win_lineage: Lineage):
    earlier = win_lineage.generated_at - timedelta(minutes=1)
    assert win_lineage.staleness_warning(earlier) is None


def test_no_comparison_timestamp_is_not_a_warning(win_lineage: Lineage):
    """Absent a change timestamp there is nothing to compare, so stay quiet rather
    than crying wolf on every run."""
    assert win_lineage.staleness_warning(None) is None


def test_manifest_without_generated_at_warns():
    """Silence here would imply freshness was checked and passed."""
    lineage = Lineage.from_manifest({"metadata": {}, "nodes": {}, "child_map": {}})
    warning = lineage.staleness_warning(None)
    assert warning is not None
    assert "freshness cannot be checked" in warning
