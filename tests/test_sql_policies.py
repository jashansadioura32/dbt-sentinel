"""The SQL review checklist: policies the agent applies, and the evidence it gets.

Every agent test here injects a fake client, like the rest of the suite. What these
prove is the plumbing: the checklist reaches the prompt, the tools return the evidence
each rule demands, retrieval is untouched, and an invented citation is flagged. Whether
the model *applies* the checklist well is measured by `evals/compare.py` on the
`q*` fixtures, which needs a funded API key and hasn't run yet.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from dbt_sentinel.agent import ReviewerAgent, ToolBox, build_user_prompt
from dbt_sentinel.diff import resolve_changes
from dbt_sentinel.lineage import Lineage
from dbt_sentinel.report import build_assessments, render_agent_findings
from dbt_sentinel.retrieval import POLICY_DIR, PolicyPack, summarise_change
from tests.test_day5 import FakeClient, _submit, _tool_call

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "evals" / "fixtures"
MANIFEST = REPO / "evals" / "manifest" / "manifest.json"
CHECKLIST = {
    "join-key-uniqueness",
    "null-handling",
    "collation-consistency",
    "data-type-correctness",
    "sql-correctness",
    "sql-security",
}


@pytest.fixture(scope="module")
def lineage() -> Lineage:
    return Lineage.from_path(MANIFEST)


@pytest.fixture(scope="module")
def pack() -> PolicyPack:
    return PolicyPack.load()


def _changes(lineage: Lineage, fixture: str):
    diff = (FIXTURES / f"{fixture}.diff").read_text(encoding="utf-8")
    changes, _ = resolve_changes(diff, lineage)
    return changes


# ---------- the pack ----------

def test_checklist_rules_are_loaded_and_citable(pack):
    assert {r.rule_id for r in pack.checklist(_changes(Lineage.from_path(MANIFEST), "b01_column_rename_with_consumers")[0])} == CHECKLIST
    assert all(pack.get(rule_id) is not None for rule_id in CHECKLIST)


def test_checklist_rules_are_never_retrieved(pack, lineage):
    """Ranking a checklist against a change summary is a category error: it applies to
    every SQL change. If one leaks into top-k it displaces a governance rule."""
    for diff in FIXTURES.glob("*.diff"):
        changes, _ = resolve_changes(diff.read_text(encoding="utf-8"), lineage)
        for changed in changes:
            retrieved = {r.rule_id for r in pack.retrieve(changed, summarise_change(changed)).retrieved}
            assert not retrieved & CHECKLIST, diff.stem


def test_adding_checklist_rules_does_not_move_retrieval_scores(tmp_path, lineage):
    """Regression guard for the IDF trap: TF-IDF weights are computed across the pack,
    so a checklist rule merely *present* in the index would shift every retrieved
    rule's score and move the published retrieval metrics."""
    without = tmp_path / "policies"
    shutil.copytree(POLICY_DIR, without)
    (without / "sql_quality.yml").unlink()
    before, after = PolicyPack.load(without), PolicyPack.load()

    for diff in FIXTURES.glob("*.diff"):
        changes, _ = resolve_changes(diff.read_text(encoding="utf-8"), lineage)
        for changed in changes:
            summary = summarise_change(changed)
            a = [(r.rule_id, round(r.score, 9)) for r in before.retrieve(changed, summary).retrieved]
            b = [(r.rule_id, round(r.score, 9)) for r in after.retrieve(changed, summary).retrieved]
            assert a == b, diff.stem


def test_checklist_is_scoped_by_the_prefilter(pack, lineage):
    """A schema.yml edit gets the two rules that read YAML (types, grants), not the
    four that judge a query."""
    yml_changes = [c for c in _changes(lineage, "b03_contract_column_dropped") if c.file.path.endswith(".yml")]
    assert yml_changes
    for changed in yml_changes:
        assert {r.rule_id for r in pack.checklist(changed)} == {"data-type-correctness", "sql-security"}


def test_an_unknown_applies_as_is_a_load_error(tmp_path):
    (tmp_path / "bad.yml").write_text(
        "rules:\n  - rule_id: x\n    applies_as: sometimes\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="applies_as 'sometimes'"):
        PolicyPack.load(tmp_path)


# ---------- the evidence ----------

def test_column_tests_are_indexed_from_test_nodes(lineage):
    tests = lineage.tests_for("model.jaffle_shop.stg_payments")
    assert tests["payment_id"] == ("not_null", "unique")
    # stg_payments is one row per payment: order_id carries no uniqueness claim, which
    # is exactly the evidence join-key-uniqueness needs to flag a join on it.
    assert "order_id" not in tests


def test_composite_key_tests_attach_to_the_model_not_a_column():
    manifest = {
        "metadata": {"dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json"},
        "nodes": {
            "model.p.m": {"name": "m", "resource_type": "model", "columns": {}},
            "test.p.combo": {
                "resource_type": "test",
                "attached_node": "model.p.m",
                "column_name": None,
                "test_metadata": {
                    "name": "unique_combination_of_columns",
                    "namespace": "dbt_utils",
                    "kwargs": {"combination_of_columns": ["order_id", "payment_method"]},
                },
            },
        },
    }
    tests = Lineage.from_manifest(manifest).tests_for("model.p.m")
    assert tests == {"": ("dbt_utils.unique_combination_of_columns(order_id, payment_method)",)}


def test_get_columns_returns_tests_and_declared_types(lineage):
    out = ToolBox(lineage).get_columns("fct_order_payments")
    by_name = {c["name"]: c for c in out["columns"]}
    assert by_name["order_id"]["tests"] == ["not_null", "unique"]
    assert by_name["total_amount"]["data_type"] == "double"


def test_undeclared_types_are_null_not_guessed(lineage):
    out = ToolBox(lineage).get_columns("stg_orders")
    assert all(c["data_type"] is None for c in out["columns"])


def test_get_model_sql_prefers_the_prs_version(lineage):
    """Also a regression: the eval manifest was compiled on Windows, so node paths use
    backslashes. Passed through as-is, the GitHub contents API 404s and the agent
    silently got the base-branch SQL instead of the PR's."""
    seen = []
    box = ToolBox(lineage, head_source=lambda path: seen.append(path) or "select 1 as pr_version")
    out = box.get_model_sql("stg_orders")
    assert out["source"] == "this PR" and "pr_version" in out["sql"]
    assert seen == ["models/staging/stg_orders.sql"]


def test_get_model_sql_labels_the_base_version_when_the_pr_file_is_unavailable(lineage):
    """The manifest's SQL predates the PR. Handing it over unlabelled would have the
    agent review the old query as if it were the new one."""
    out = ToolBox(lineage, head_source=lambda path: None).get_model_sql("stg_orders")
    assert out["source"].startswith("base branch")
    assert "select" in out["sql"].lower()


def test_get_model_sql_unknown_model_is_an_error_not_a_raise(lineage):
    assert "error" in ToolBox(lineage).dispatch("get_model_sql", {"model": "nope"}, None)


# ---------- the prompt ----------

def test_the_prompt_carries_the_checklist_for_a_sql_change(lineage, pack):
    prompt = build_user_prompt(build_assessments(_changes(lineage, "b01_column_rename_with_consumers"), lineage), pack)
    assert "SQL review checklist" in prompt
    for rule_id in CHECKLIST:
        assert rule_id in prompt


def test_the_system_prompt_demands_evidence_before_a_join_finding():
    from dbt_sentinel.agent import SYSTEM_PROMPT

    assert "no `unique` test" in SYSTEM_PROMPT
    assert "Don't report them" in SYSTEM_PROMPT  # credentials and = null are deterministic


def test_get_model_sql_is_offered_to_the_model(lineage, pack):
    client = FakeClient([_submit([])])
    ReviewerAgent(lineage, pack, client=client).review(
        build_assessments(_changes(lineage, "b01_column_rename_with_consumers"), lineage)
    )
    names = {t["function"]["name"] for t in client.requests[0]["tools"]}
    assert {"get_model_sql", "get_columns", "get_lineage", "get_policies"} <= names


# ---------- the loop, end to end with a fake model ----------

def test_the_agent_can_read_sql_then_cite_a_checklist_rule(lineage, pack):
    finding = {
        "rule_id": "join-key-uniqueness",
        "severity": "high",
        "model": "stg_orders",
        "explanation": "Joins stg_payments on order_id, which has no unique test.",
        "suggested_fix": "Aggregate payments to one row per order before joining.",
    }
    client = FakeClient([
        _tool_call("get_model_sql", {"model": "stg_orders"}),
        _tool_call("get_columns", {"model": "stg_payments"}),
        _submit([finding]),
    ])
    result = ReviewerAgent(
        lineage, pack, client=client, head_source=lambda p: "select * from pr_head"
    ).review(build_assessments(_changes(lineage, "b01_column_rename_with_consumers"), lineage))

    assert result.ran and result.uncited_rule_ids == []
    sql_reply = json.loads(client.requests[1]["messages"][-1]["content"])
    assert sql_reply["source"] == "this PR"
    assert "`join-key-uniqueness`" in render_agent_findings(result)


def test_an_invented_rule_id_is_flagged_not_rendered_as_policy(lineage, pack):
    """Nothing previously checked citations: a made-up rule_id rendered exactly like a
    real one. The finding is kept, since it may be right, but the citation is marked."""
    invented = {
        "rule_id": "no-cartesian-joins",
        "severity": "medium",
        "model": "stg_orders",
        "explanation": "A cross join was added.",
        "suggested_fix": "Add a join condition.",
    }
    result = ReviewerAgent(lineage, pack, client=FakeClient([_submit([invented])])).review(
        build_assessments(_changes(lineage, "b01_column_rename_with_consumers"), lineage)
    )
    assert result.uncited_rule_ids == ["no-cartesian-joins"]
    assert "rule not in the policy pack" in render_agent_findings(result)


def test_structural_is_a_valid_citation(lineage, pack):
    structural = {
        "rule_id": "structural",
        "severity": "high",
        "model": "stg_orders",
        "explanation": "Breaking change no rule covers.",
        "suggested_fix": "Migrate consumers.",
    }
    result = ReviewerAgent(lineage, pack, client=FakeClient([_submit([structural])])).review(
        build_assessments(_changes(lineage, "b01_column_rename_with_consumers"), lineage)
    )
    assert result.uncited_rule_ids == []


# ---------- the eval harness refuses to publish numbers it didn't earn ----------

def test_harness_dry_run_validates_every_fixture(capsys):
    from evals import sql_policy_eval

    assert sql_policy_eval.main(["--dry-run"]) == 0
    assert "12 SQL checklist fixtures valid" in capsys.readouterr().out


def test_harness_without_a_key_exits_two_and_writes_nothing(monkeypatch, tmp_path):
    from evals import sql_policy_eval

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(sql_policy_eval, "RESULTS", tmp_path / "out.json")
    assert sql_policy_eval.main([]) == 2
    assert not (tmp_path / "out.json").exists()


def test_harness_with_zero_token_spend_exits_two_and_writes_nothing(monkeypatch, tmp_path):
    """Same guard as compare.py (tests/test_day7.py): an agent that never ran degrades
    on every fixture, and a table of zeros must not be written as a measurement."""
    from dbt_sentinel.agent import AgentResult
    from evals import sql_policy_eval

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(sql_policy_eval, "RESULTS", tmp_path / "out.json")
    monkeypatch.setattr(
        sql_policy_eval.ReviewerAgent,
        "review",
        lambda self, assessments: AgentResult(degraded=True, degradation_reason="429 no credits"),
    )
    assert sql_policy_eval.main([]) == 2
    assert not (tmp_path / "out.json").exists()
