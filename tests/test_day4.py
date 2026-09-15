"""Regression tests for the policy pack and hybrid retrieval.

The two acceptance criteria from the build plan are the first two tests: a PII fixture
must surface the PII rule in the top 3, and a pure logic change must not surface it at
all. The second is the one that matters — a retriever that returns the PII rule on every
diff passes the first test and is worthless.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dbt_sentinel.diff import resolve_changes
from dbt_sentinel.lineage import Lineage
from dbt_sentinel.retrieval import PolicyPack, summarise_change

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "evals" / "fixtures"
MANIFEST = REPO / "evals" / "manifest" / "manifest.json"


@pytest.fixture(scope="module")
def pack() -> PolicyPack:
    return PolicyPack.load()


@pytest.fixture(scope="module")
def lineage() -> Lineage:
    return Lineage.from_path(MANIFEST)


def _retrieve(pack: PolicyPack, lineage: Lineage, fixture: str, top_k: int = 3):
    diff_text = (FIXTURES / f"{fixture}.diff").read_text(encoding="utf-8")
    changes, _ = resolve_changes(diff_text, lineage)
    assert changes, f"{fixture} resolved to no nodes; retrieval cannot be scored"
    ids: list[str] = []
    for changed in changes:
        result = pack.retrieve(changed, summarise_change(changed), top_k=top_k)
        ids.extend(r.rule_id for r in result.retrieved)
    return ids


# ---------- acceptance criteria ----------


def test_pii_fixture_returns_pii_rule_in_top_3(pack: PolicyPack, lineage: Lineage):
    assert "pii-tagging" in _retrieve(pack, lineage, "s01_pii_column_untagged")


def test_pure_logic_change_does_not_return_pii_rule(pack: PolicyPack, lineage: Lineage):
    """The precision half. A retriever that fires PII at a join change is noise."""
    assert "pii-tagging" not in _retrieve(pack, lineage, "b05_changed_join_grain")


def test_comment_only_change_retrieves_nothing(pack: PolicyPack, lineage: Lineage):
    """No governance rule applies to a comment. Silence is the correct output, and a
    rule surviving the prefilter is not on its own evidence of anything."""
    assert _retrieve(pack, lineage, "p01_comment_added") == []


# ---------- structural prefilter ----------


def test_contract_rule_eliminated_on_uncontracted_model(pack: PolicyPack, lineage: Lineage):
    """`contract-enforcement` requires is_contracted=true. stg_orders is not contracted,
    so the rule must be eliminated structurally rather than ranked low."""
    diff_text = (FIXTURES / "b01_column_rename_with_consumers.diff").read_text(encoding="utf-8")
    changes, _ = resolve_changes(diff_text, lineage)
    result = pack.retrieve(changes[0], summarise_change(changes[0]))
    assert "contract-enforcement" not in [r.rule_id for r in result.retrieved]
    assert "contract-enforcement" in [rid for rid, _ in result.eliminated]


def test_incremental_rule_applies_only_to_incremental_models(pack: PolicyPack, lineage: Lineage):
    on_incremental = _retrieve(pack, lineage, "b07_removed_incremental_filter")
    on_view = _retrieve(pack, lineage, "b01_column_rename_with_consumers")
    assert "incremental-safety" in on_incremental
    assert "incremental-safety" not in on_view


def test_exposure_rule_does_not_apply_to_models(pack: PolicyPack, lineage: Lineage):
    assert "exposure-ownership" not in _retrieve(pack, lineage, "b04_dropped_dedup")


# ---------- pack integrity ----------


def test_pack_loads_all_rules(pack: PolicyPack):
    assert len(pack) == 14


def test_every_rule_has_guidance_and_keywords(pack: PolicyPack):
    """A rule without guidance produces a finding the reader cannot act on, and error
    messages must say what to do next."""
    for rule in pack.rules:
        assert rule.guidance, f"{rule.rule_id} has no guidance"
        assert rule.keywords, f"{rule.rule_id} has no keywords"
        assert rule.severity in {"low", "medium", "high"}, rule.rule_id


def test_duplicate_rule_ids_are_rejected(tmp_path: Path):
    """Rule ids are referenced by the eval labels; a silent duplicate would make one of
    them unreachable and the retrieval metric quietly wrong."""
    (tmp_path / "a.yml").write_text(
        "rules:\n"
        "  - rule_id: dupe\n    title: A\n    description: x\n    keywords: [x]\n"
        "  - rule_id: dupe\n    title: B\n    description: y\n    keywords: [y]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate rule_id"):
        PolicyPack.load(tmp_path)


def test_missing_policy_dir_says_what_to_do(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="Expected .* rule files"):
        PolicyPack.load(tmp_path / "nope")


# ---------- embedding cache ----------


def test_cache_round_trips(pack: PolicyPack, tmp_path: Path):
    pack.save_cache(tmp_path)
    assert PolicyPack.load().load_cache(tmp_path) is True


def test_cache_invalidates_when_a_rule_changes(tmp_path: Path):
    """The cache is keyed on pack content, so editing a rule must not silently serve
    stale vectors."""
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    rule_file = policy_dir / "r.yml"
    rule_file.write_text(
        "rules:\n  - rule_id: r1\n    title: Original\n    description: about pii email\n"
        "    keywords: [pii, email]\n",
        encoding="utf-8",
    )
    original = PolicyPack.load(policy_dir)
    original.save_cache(tmp_path / "cache")

    rule_file.write_text(
        "rules:\n  - rule_id: r1\n    title: Rewritten\n    description: about joins grain\n"
        "    keywords: [join, grain]\n",
        encoding="utf-8",
    )
    edited = PolicyPack.load(policy_dir)
    assert edited.load_cache(tmp_path / "cache") is False


def test_corrupt_cache_does_not_break_retrieval(pack: PolicyPack, tmp_path: Path):
    """Degrade, don't crash: a truncated cache file must fall back to recomputing."""
    (tmp_path / "policy_vectors.json").write_text("{not json", encoding="utf-8")
    assert pack.load_cache(tmp_path) is False


def test_cache_file_is_written_where_expected(pack: PolicyPack, tmp_path: Path):
    path = pack.save_cache(tmp_path)
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["signature"] == pack.cache_signature()
    assert payload["vectors"]
