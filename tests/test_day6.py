"""Regression tests for the day-6 comparison harness.

The harness produces published numbers, so the things worth testing are the ones that
would corrupt a published number silently: the price constants, the cost arithmetic, the
failure-category classifier, and the refusal to write a results file without a real run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from evals.compare import (  # noqa: E402
    PRICE_PER_MTOK_INPUT,
    PRICE_PER_MTOK_OUTPUT,
    ArmOutcome,
    Comparison,
    cost_usd,
    summarise,
)
from dbt_sentinel.agent import DEFAULT_MODEL  # noqa: E402


def _row(**kwargs) -> Comparison:
    defaults = dict(
        fixture_id="f",
        category="breaking",
        expected_severity="high",
        expected_rule_ids=[],
        baseline=ArmOutcome(severity="high"),
        agent=ArmOutcome(severity="high"),
    )
    defaults.update(kwargs)
    return Comparison(**defaults)


# ---------- price constants and cost math ----------


def test_price_constants_match_the_pinned_model():
    """claude-sonnet-5 is $2/$10 per MTok. Sonnet 4.6's $3/$15 is the easy wrong answer,
    and using it inflates every published cost figure by ~50%."""
    assert DEFAULT_MODEL == "claude-sonnet-5"
    assert PRICE_PER_MTOK_INPUT == 2.00
    assert PRICE_PER_MTOK_OUTPUT == 10.00


def test_cost_math_is_per_million_tokens():
    assert cost_usd(1_000_000, 0) == pytest.approx(2.00)
    assert cost_usd(0, 1_000_000) == pytest.approx(10.00)
    assert cost_usd(1_000_000, 1_000_000) == pytest.approx(12.00)


def test_cost_of_a_realistic_fixture_is_cents_not_dollars():
    """A sanity floor: if one fixture ever costs dollars, something is looping."""
    assert cost_usd(8_000, 800) < 0.05


def test_zero_usage_costs_nothing():
    assert cost_usd(0, 0) == 0.0


# ---------- failure taxonomy ----------


def test_correct_agent_has_no_failure_category():
    assert _row().failure_category is None


def test_degradation_is_a_schema_violation_regardless_of_severity():
    """A degraded run is a contract failure even if the severity happened to land right,
    because no finding was actually produced."""
    row = _row(agent=ArmOutcome(severity=None, errored=True), degraded=True)
    assert row.failure_category == "schema_violation"


def test_missing_expected_rule_is_a_retrieval_miss():
    """Diagnosed before reasoning: a rule that never arrived cannot be a prompt problem."""
    row = _row(
        expected_rule_ids=["grain-integrity"],
        retrieved_rule_ids=["pii-tagging"],
        agent=ArmOutcome(severity="low"),
    )
    assert row.failure_category == "retrieval_miss"


def test_retrieved_rule_but_wrong_verdict_is_a_reasoning_error():
    row = _row(
        expected_rule_ids=["grain-integrity"],
        retrieved_rule_ids=["grain-integrity"],
        agent=ArmOutcome(severity="low"),
    )
    assert row.failure_category == "reasoning_error"


def test_one_level_apart_on_a_subtle_fixture_is_label_ambiguity():
    """medium vs low on a judgment call is a defensible disagreement, and a candidate for
    a documented label argument rather than a silent relabel."""
    row = _row(
        category="subtle",
        expected_severity="medium",
        agent=ArmOutcome(severity="low"),
    )
    assert row.failure_category == "label_ambiguity"


def test_two_levels_apart_on_a_subtle_fixture_is_still_a_reasoning_error():
    row = _row(
        category="subtle",
        expected_severity="high",
        agent=ArmOutcome(severity="low"),
    )
    assert row.failure_category == "reasoning_error"


def test_should_pass_fixture_flagged_high_is_a_reasoning_error_not_ambiguity():
    """A false positive on a routine PR is never a label argument."""
    row = _row(
        category="should_pass",
        expected_severity="low",
        agent=ArmOutcome(severity="high"),
    )
    assert row.failure_category == "reasoning_error"


# ---------- summary integrity ----------


def test_agent_metrics_are_absent_on_a_dry_run():
    """A dry run must not emit an agent column that looks like a measurement."""
    summary = summarise([_row()], live=False)
    assert summary["agent"] == {}
    assert summary["live"] is False


def test_degraded_rows_are_excluded_from_agent_metrics_not_counted_correct():
    rows = [
        _row(fixture_id="a", agent=ArmOutcome(severity=None, errored=True), degraded=True),
        _row(fixture_id="b", agent=ArmOutcome(severity="high")),
    ]
    summary = summarise(rows, live=True)
    assert summary["agent"]["n_errored"] == 1
    assert summary["agent"]["n_scored"] == 1


def test_false_positive_rate_is_computed_over_should_pass_only():
    rows = [
        _row(category="should_pass", expected_severity="low", agent=ArmOutcome(severity="high")),
        _row(category="should_pass", expected_severity="low", agent=ArmOutcome(severity="low")),
        _row(category="breaking", expected_severity="high", agent=ArmOutcome(severity="high")),
    ]
    summary = summarise(rows, live=True)
    assert summary["agent"]["false_positive_rate_should_pass"] == pytest.approx(0.5)


def test_summary_records_the_price_list_used():
    """A published cost figure is meaningless without the prices behind it."""
    summary = summarise([_row(input_tokens=1000, output_tokens=100)], live=True)
    assert summary["cost"]["price_per_mtok_input"] == PRICE_PER_MTOK_INPUT
    assert summary["cost"]["price_per_mtok_output"] == PRICE_PER_MTOK_OUTPUT
