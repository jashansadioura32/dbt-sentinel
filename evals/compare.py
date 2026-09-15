"""Day 6: run the agent and the deterministic baseline side by side over every fixture.

    python -m evals.compare                    # all 30 fixtures, both arms
    python -m evals.compare --only breaking    # one block
    python -m evals.compare --fixtures b01_column_rename_with_consumers,p01_comment_added
    python -m evals.compare --dry-run          # baseline arm only, no API calls

This file measures. It does not fix anything, and it must not: tuning a prompt in the
same session that establishes the before/after comparison destroys the comparison.

Every failure is categorised into one of four buckets, because the fix differs per
bucket and an undifferentiated accuracy number cannot tell them apart:

  retrieval_miss    the expected rule never reached the model -> fix rule keywords
  reasoning_error   the rule reached it and the verdict was still wrong -> fix the prompt
  schema_violation  output failed validation, or the agent degraded -> fix the contract
  label_ambiguity   the agent's reasoning is defensible and the label may be wrong
                    -> argue it in LABEL_CHANGES.md, do not silently relabel
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbt_sentinel.agent import DEFAULT_MODEL, AgentResult, ReviewerAgent  # noqa: E402
from dbt_sentinel.diff import resolve_changes  # noqa: E402
from dbt_sentinel.lineage import Lineage  # noqa: E402
from dbt_sentinel.report import build_assessments  # noqa: E402
from dbt_sentinel.retrieval import PolicyPack, summarise_change  # noqa: E402

EVALS = Path(__file__).resolve().parent
FIXTURES = EVALS / "fixtures"
MANIFEST = EVALS / "manifest" / "manifest.json"

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}
FLAG_THRESHOLD = "medium"

# USD per million tokens for the pinned model (claude-sonnet-5). Hardcoded rather than
# fetched so a published cost-per-PR figure is reproducible and attributable to one price
# list. Verify against https://claude.com/pricing before quoting these anywhere.
#
# These were initially written as 3.00/15.00 — Sonnet 4.6's rates, carried over by
# assumption. Sonnet 5 is 2.00/10.00, so every cost figure would have been ~50% too high.
# A published cost number is only as good as its price constant, hence the test.
PRICE_PER_MTOK_INPUT = 2.00
PRICE_PER_MTOK_OUTPUT = 10.00


def cost_usd(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens / 1_000_000 * PRICE_PER_MTOK_INPUT
        + output_tokens / 1_000_000 * PRICE_PER_MTOK_OUTPUT
    )


@dataclass
class ArmOutcome:
    """One arm's verdict on one fixture."""

    severity: str | None = None  # None = nothing resolved / nothing produced
    rule_ids: list[str] = field(default_factory=list)
    errored: bool = False
    detail: str = ""


@dataclass
class Comparison:
    fixture_id: str
    category: str
    expected_severity: str
    expected_rule_ids: list[str]
    baseline: ArmOutcome
    agent: ArmOutcome
    retrieved_rule_ids: list[str] = field(default_factory=list)
    degraded: bool = False
    degradation_reason: str | None = None
    latency_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    rounds: int = 0
    explanations: list[str] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        return cost_usd(self.input_tokens, self.output_tokens)

    def _flag(self, severity: str | None) -> bool:
        if severity is None:
            return False
        return SEVERITY_ORDER[severity] >= SEVERITY_ORDER[FLAG_THRESHOLD]

    @property
    def expected_flag(self) -> bool:
        return SEVERITY_ORDER[self.expected_severity] >= SEVERITY_ORDER[FLAG_THRESHOLD]

    @property
    def baseline_correct(self) -> bool:
        return self.baseline.severity == self.expected_severity

    @property
    def agent_correct(self) -> bool:
        return self.agent.severity == self.expected_severity

    @property
    def failure_category(self) -> str | None:
        """Classify the agent's failure. Order matters: a degradation is a schema
        violation regardless of what the severity ended up being, and a retrieval miss
        is diagnosed before reasoning so a missing rule is not blamed on the prompt."""
        if self.agent.detail == "not run":
            # A dry run has no agent verdict to classify. Reporting these as failures
            # would fill the taxonomy with 30 phantom schema violations.
            return None
        if self.agent_correct:
            return None
        if self.degraded or self.agent.errored:
            return "schema_violation"
        if self.expected_rule_ids and not (
            set(self.expected_rule_ids) & set(self.retrieved_rule_ids)
        ):
            return "retrieval_miss"
        # One severity level apart on a judgment fixture is a defensible disagreement,
        # not a clear error — those are the candidates for a label argument.
        if self.category == "subtle" and self.agent.severity is not None:
            distance = abs(
                SEVERITY_ORDER[self.agent.severity] - SEVERITY_ORDER[self.expected_severity]
            )
            if distance == 1:
                return "label_ambiguity"
        return "reasoning_error"


def _baseline_arm(assessments: list) -> ArmOutcome:
    if not assessments:
        return ArmOutcome(severity=None, errored=True, detail="resolved to no nodes")
    worst = max(assessments, key=lambda a: SEVERITY_ORDER[a.severity])
    return ArmOutcome(severity=worst.severity, detail="; ".join(worst.reasons[:2]))


def _agent_arm(result: AgentResult) -> ArmOutcome:
    if result.degraded:
        return ArmOutcome(
            severity=None,
            errored=True,
            detail=result.degradation_reason or "degraded",
        )
    if not result.findings:
        # No findings is a real verdict: the agent reviewed and found nothing worth
        # flagging, which for a routine PR is the correct answer.
        return ArmOutcome(severity="low", detail="no findings")
    worst = max(result.findings, key=lambda f: SEVERITY_ORDER[f.severity])
    return ArmOutcome(
        severity=worst.severity,
        rule_ids=sorted({f.rule_id for f in result.findings}),
        detail=worst.explanation[:160],
    )


def run_fixture(
    label: dict, lineage: Lineage, pack: PolicyPack, live: bool
) -> Comparison:
    diff_text = (FIXTURES / f"{label['id']}.diff").read_text(encoding="utf-8")
    changes, unresolved = resolve_changes(diff_text, lineage)
    assessments = build_assessments(changes, lineage)

    retrieved: list[str] = []
    for changed in changes:
        result = pack.retrieve(changed, summarise_change(changed))
        retrieved.extend(r.rule_id for r in result.retrieved)

    comparison = Comparison(
        fixture_id=label["id"],
        category=label["category"],
        expected_severity=label["expected_severity"],
        expected_rule_ids=list(label.get("expected_rule_ids") or []),
        baseline=_baseline_arm(assessments),
        agent=ArmOutcome(severity=None, errored=True, detail="not run"),
        retrieved_rule_ids=sorted(set(retrieved)),
    )

    if not live:
        return comparison

    started = time.monotonic()
    agent_result = ReviewerAgent(lineage, pack).review(assessments)
    comparison.latency_s = round(time.monotonic() - started, 2)
    comparison.agent = _agent_arm(agent_result)
    comparison.degraded = agent_result.degraded
    comparison.degradation_reason = agent_result.degradation_reason
    comparison.input_tokens = agent_result.input_tokens
    comparison.output_tokens = agent_result.output_tokens
    comparison.rounds = agent_result.rounds
    comparison.explanations = [f.explanation for f in agent_result.findings]
    return comparison


def _rate(num: int, den: int) -> float:
    return num / den if den else 0.0


def summarise(rows: list[Comparison], live: bool) -> dict:
    def arm_metrics(pick_correct, pick_severity, pick_errored) -> dict:
        scored = [r for r in rows if not pick_errored(r)]
        tp = sum(1 for r in scored if r.expected_flag and _is_flag(pick_severity(r)))
        fp = sum(1 for r in scored if not r.expected_flag and _is_flag(pick_severity(r)))
        fn = sum(1 for r in scored if r.expected_flag and not _is_flag(pick_severity(r)))
        tn = sum(1 for r in scored if not r.expected_flag and not _is_flag(pick_severity(r)))
        precision = _rate(tp, tp + fp)
        recall = _rate(tp, tp + fn)
        should_pass = [r for r in rows if r.category == "should_pass" and not pick_errored(r)]
        return {
            "n_scored": len(scored),
            "n_errored": len(rows) - len(scored),
            "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
            "precision": round(precision, 3),
            "recall": round(recall, 3),
            "f1": round(_rate(2 * precision * recall, precision + recall), 3),
            "false_positive_rate_should_pass": round(
                _rate(sum(1 for r in should_pass if _is_flag(pick_severity(r))), len(should_pass)),
                3,
            ),
            "exact_match": sum(1 for r in scored if pick_correct(r)),
            "exact_match_rate": round(_rate(sum(1 for r in scored if pick_correct(r)), len(scored)), 3),
        }

    def _is_flag(severity: str | None) -> bool:
        return severity is not None and SEVERITY_ORDER[severity] >= SEVERITY_ORDER[FLAG_THRESHOLD]

    baseline = arm_metrics(
        lambda r: r.baseline_correct, lambda r: r.baseline.severity, lambda r: r.baseline.errored
    )
    agent = (
        arm_metrics(
            lambda r: r.agent_correct, lambda r: r.agent.severity, lambda r: r.agent.errored
        )
        if live
        else {}
    )

    taxonomy: dict[str, int] = {}
    for row in rows:
        category = row.failure_category
        if category:
            taxonomy[category] = taxonomy.get(category, 0) + 1

    costs = [r.cost_usd for r in rows if r.input_tokens or r.output_tokens]
    latencies = [r.latency_s for r in rows if r.latency_s]

    return {
        "model": DEFAULT_MODEL,
        "live": live,
        "n_fixtures": len(rows),
        "baseline": baseline,
        "agent": agent,
        "failure_taxonomy": dict(sorted(taxonomy.items(), key=lambda kv: -kv[1])),
        "cost": {
            "total_usd": round(sum(costs), 4),
            "mean_per_fixture_usd": round(_rate(sum(costs), len(costs)), 4) if costs else 0.0,
            "total_input_tokens": sum(r.input_tokens for r in rows),
            "total_output_tokens": sum(r.output_tokens for r in rows),
            "price_per_mtok_input": PRICE_PER_MTOK_INPUT,
            "price_per_mtok_output": PRICE_PER_MTOK_OUTPUT,
        },
        "latency": {
            "mean_s": round(_rate(sum(latencies), len(latencies)), 2) if latencies else 0.0,
            "max_s": round(max(latencies), 2) if latencies else 0.0,
        },
    }


def render(rows: list[Comparison], summary: dict) -> str:
    lines = ["## Agent vs. deterministic baseline", ""]
    lines.append("| Fixture | Category | Expected | Baseline | Agent | Failure | Cost | Lat |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in rows:
        b = "ERROR" if r.baseline.errored else (r.baseline.severity or "-")
        bm = "ok" if r.baseline_correct else "**x**"
        if r.agent.detail == "not run":
            # Never render a dry run's empty agent arm as a verdict: "ERROR x" next to a
            # real baseline column reads as a measured agent failure.
            a, am = "_not run_", ""
        else:
            a = "ERROR" if r.agent.errored else (r.agent.severity or "-")
            am = "ok" if r.agent_correct else "**x**"
        lines.append(
            f"| `{r.fixture_id}` | {r.category} | {r.expected_severity} | {b} {bm} | {a} {am} "
            f"| {r.failure_category or '-'} | ${r.cost_usd:.4f} | {r.latency_s}s |"
        )

    b, a = summary["baseline"], summary["agent"]
    lines += ["", "## Metrics", "", "| Metric | Baseline | Agent |", "|---|---|---|"]
    if a:
        for key, label in [
            ("precision", "Precision"),
            ("recall", "Recall"),
            ("f1", "F1"),
            ("false_positive_rate_should_pass", "FPR (should-pass)"),
            ("exact_match_rate", "Exact severity match"),
        ]:
            lines.append(f"| {label} | {b[key]:.3f} | {a[key]:.3f} |")
        lines.append(f"| Errored / degraded | {b['n_errored']} | {a['n_errored']} |")
    else:
        lines.append(f"| Precision | {b['precision']:.3f} | not run |")
        lines.append(f"| Recall | {b['recall']:.3f} | not run |")

    if summary["failure_taxonomy"]:
        lines += ["", "## Failure taxonomy", "", "| Category | Count |", "|---|---|"]
        for name, count in summary["failure_taxonomy"].items():
            lines.append(f"| {name} | {count} |")

    c, lat = summary["cost"], summary["latency"]
    lines += [
        "",
        "## Cost and latency",
        "",
        f"- Model: `{summary['model']}`",
        f"- Total: **${c['total_usd']:.4f}** over {summary['n_fixtures']} fixtures",
        f"- Mean per fixture: **${c['mean_per_fixture_usd']:.4f}**",
        f"- Tokens: {c['total_input_tokens']:,} in / {c['total_output_tokens']:,} out",
        f"- Latency: mean {lat['mean_s']}s, max {lat['max_s']}s",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.compare")
    parser.add_argument("--only", choices=["breaking", "should_pass", "subtle"])
    parser.add_argument("--fixtures", help="comma-separated fixture ids")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="baseline arm only; makes no API calls and writes no results file",
    )
    parser.add_argument("--json", dest="json_path", default=str(EVALS / "compare_results.json"))
    args = parser.parse_args(argv)

    lineage = Lineage.from_path(MANIFEST)
    pack = PolicyPack.load()
    pack.load_cache()

    labels = yaml.safe_load((FIXTURES / "labels.yml").read_text(encoding="utf-8"))["fixtures"]
    if args.only:
        labels = [x for x in labels if x["category"] == args.only]
    if args.fixtures:
        wanted = {f.strip() for f in args.fixtures.split(",")}
        labels = [x for x in labels if x["id"] in wanted]

    live = not args.dry_run
    if live:
        import os

        if not os.environ.get("ANTHROPIC_API_KEY"):
            print(
                "error: ANTHROPIC_API_KEY is not set, so the agent arm cannot run.\n"
                "       Export it, or pass --dry-run to score the baseline arm only.\n"
                "       Refusing to write results with a fabricated agent arm.",
                file=sys.stderr,
            )
            return 2

    rows: list[Comparison] = []
    for index, label in enumerate(labels, 1):
        if live:
            print(f"[{index}/{len(labels)}] {label['id']}...", file=sys.stderr, flush=True)
        rows.append(run_fixture(label, lineage, pack, live))

    summary = summarise(rows, live)
    print(render(rows, summary))

    if live:
        Path(args.json_path).write_text(
            json.dumps(
                {
                    "summary": summary,
                    "rows": [
                        {
                            "id": r.fixture_id,
                            "category": r.category,
                            "expected_severity": r.expected_severity,
                            "expected_rule_ids": r.expected_rule_ids,
                            "baseline": {
                                "severity": r.baseline.severity,
                                "errored": r.baseline.errored,
                                "detail": r.baseline.detail,
                            },
                            "agent": {
                                "severity": r.agent.severity,
                                "rule_ids": r.agent.rule_ids,
                                "errored": r.agent.errored,
                                "detail": r.agent.detail,
                            },
                            "retrieved_rule_ids": r.retrieved_rule_ids,
                            "failure_category": r.failure_category,
                            "degraded": r.degraded,
                            "degradation_reason": r.degradation_reason,
                            "latency_s": r.latency_s,
                            "input_tokens": r.input_tokens,
                            "output_tokens": r.output_tokens,
                            "cost_usd": round(r.cost_usd, 6),
                            "rounds": r.rounds,
                            "explanations": r.explanations,
                        }
                        for r in rows
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nWrote {args.json_path}", file=sys.stderr)
    else:
        print(
            "\n(dry run: agent arm not executed, no results file written)", file=sys.stderr
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
