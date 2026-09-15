"""Score dbt-sentinel against the labelled fixtures.

    python -m evals.runner
    python -m evals.runner --json evals/results.json
    python -m evals.runner --only breaking

Design note: a fixture that resolves to zero nodes is reported as an ERROR, not scored
as a pass. Silently treating "the tool found nothing" as "the tool correctly found
nothing" is how an eval suite flatters a broken resolver — and this suite caught exactly
that bug on its first run (see BASELINE.md, YAML attribution).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbt_sentinel.diff import resolve_changes  # noqa: E402
from dbt_sentinel.lineage import Lineage  # noqa: E402
from dbt_sentinel.report import build_assessments  # noqa: E402

EVALS = Path(__file__).resolve().parent
FIXTURES = EVALS / "fixtures"
MANIFEST = EVALS / "manifest" / "manifest.json"

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}

# A fixture is "flagged" if the tool would surface it above this level. Used for the
# precision/recall/FPR triple, which needs a binary decision per fixture.
FLAG_THRESHOLD = "medium"


@dataclass
class Outcome:
    fixture_id: str
    category: str
    expected: str
    actual: str | None  # None = the tool resolved nothing
    resolved: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def errored(self) -> bool:
        """Zero resolved nodes AND nothing surfaced as a warning: the change vanished."""
        return self.actual is None and not self.unresolved

    @property
    def exact(self) -> bool:
        return self.actual == self.expected

    @property
    def expected_flag(self) -> bool:
        return SEVERITY_ORDER[self.expected] >= SEVERITY_ORDER[FLAG_THRESHOLD]

    @property
    def actual_flag(self) -> bool:
        if self.actual is None:
            return False
        return SEVERITY_ORDER[self.actual] >= SEVERITY_ORDER[FLAG_THRESHOLD]


def load_labels() -> list[dict]:
    data = yaml.safe_load((FIXTURES / "labels.yml").read_text(encoding="utf-8"))
    return data["fixtures"]


def run_fixture(label: dict, lineage: Lineage) -> Outcome:
    diff_text = (FIXTURES / f"{label['id']}.diff").read_text(encoding="utf-8")
    changes, unresolved = resolve_changes(diff_text, lineage)
    assessments = build_assessments(changes, lineage)

    actual = None
    reasons: list[str] = []
    if assessments:
        # The PR's severity is its worst finding — that is what gates the CI check.
        worst = max(assessments, key=lambda a: SEVERITY_ORDER[a.severity])
        actual = worst.severity
        reasons = list(worst.reasons)
    elif unresolved:
        # Nothing resolved, but the tool said so out loud. A new model or a non-node file
        # legitimately lands here, and the warning is the correct output.
        actual = "low"

    return Outcome(
        fixture_id=label["id"],
        category=label["category"],
        expected=label["expected_severity"],
        actual=actual,
        resolved=[c.node.name for c in changes],
        unresolved=[f.path for f in unresolved],
        reasons=reasons,
    )


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def summarise(outcomes: list[Outcome]) -> dict:
    scored = [o for o in outcomes if not o.errored]
    errored = [o for o in outcomes if o.errored]

    tp = sum(1 for o in scored if o.expected_flag and o.actual_flag)
    fp = sum(1 for o in scored if not o.expected_flag and o.actual_flag)
    fn = sum(1 for o in scored if o.expected_flag and not o.actual_flag)
    tn = sum(1 for o in scored if not o.expected_flag and not o.actual_flag)

    precision = _rate(tp, tp + fp)
    recall = _rate(tp, tp + fn)

    should_pass = [o for o in outcomes if o.category == "should_pass"]
    sp_scored = [o for o in should_pass if not o.errored]
    # Reported on its own because it is the number that decides whether anyone keeps the
    # tool installed. High recall with an unreported FPR usually means flagging everything.
    fpr = _rate(sum(1 for o in sp_scored if o.actual_flag), len(sp_scored))

    by_category: dict[str, dict] = {}
    for cat in ("breaking", "should_pass", "subtle"):
        rows = [o for o in outcomes if o.category == cat]
        ok = [o for o in rows if not o.errored]
        by_category[cat] = {
            "n": len(rows),
            "errored": sum(1 for o in rows if o.errored),
            "exact_match": sum(1 for o in ok if o.exact),
            "exact_match_rate": round(_rate(sum(1 for o in ok if o.exact), len(ok)), 3),
        }

    return {
        "n_fixtures": len(outcomes),
        "n_scored": len(scored),
        "n_errored": len(errored),
        "errored_ids": [o.fixture_id for o in errored],
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(_rate(2 * precision * recall, precision + recall), 3),
        "false_positive_rate_should_pass": round(fpr, 3),
        "exact_severity_match": sum(1 for o in scored if o.exact),
        "exact_severity_match_rate": round(_rate(sum(1 for o in scored if o.exact), len(scored)), 3),
        "by_category": by_category,
    }


def render(outcomes: list[Outcome], summary: dict) -> str:
    lines: list[str] = []
    icon = {True: "PASS", False: "FAIL"}

    lines.append("## Per-fixture results")
    lines.append("")
    lines.append("| Fixture | Category | Expected | Actual | Match | Resolved |")
    lines.append("|---|---|---|---|---|---|")
    for o in outcomes:
        actual = "ERROR (nothing resolved)" if o.errored else (o.actual or "-")
        match = "ERROR" if o.errored else icon[o.exact]
        resolved = ", ".join(o.resolved) or ("*warned: " + ", ".join(o.unresolved) if o.unresolved else "-")
        lines.append(
            f"| `{o.fixture_id}` | {o.category} | {o.expected} | {actual} | {match} | {resolved} |"
        )

    c = summary["confusion"]
    lines += [
        "",
        "## Metrics",
        "",
        f"Fixtures: {summary['n_fixtures']} | scored: {summary['n_scored']} | "
        f"**errored: {summary['n_errored']}**",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Precision | {summary['precision']:.3f} |",
        f"| Recall | {summary['recall']:.3f} |",
        f"| F1 | {summary['f1']:.3f} |",
        f"| **False-positive rate (should-pass block)** | **{summary['false_positive_rate_should_pass']:.3f}** |",
        f"| Exact severity match | {summary['exact_severity_match']}/{summary['n_scored']} "
        f"({summary['exact_severity_match_rate']:.3f}) |",
        "",
        f"Confusion (flag threshold = {FLAG_THRESHOLD}+): "
        f"TP={c['tp']} FP={c['fp']} FN={c['fn']} TN={c['tn']}",
        "",
        "## By category",
        "",
        "| Category | n | Errored | Exact match | Rate |",
        "|---|---|---|---|---|",
    ]
    for cat, s in summary["by_category"].items():
        lines.append(
            f"| {cat} | {s['n']} | {s['errored']} | {s['exact_match']} | {s['exact_match_rate']:.3f} |"
        )

    if summary["n_errored"]:
        lines += [
            "",
            "## Errored fixtures",
            "",
            "These resolved to zero nodes and emitted no warning — the change was dropped",
            "silently. They are excluded from the metrics above rather than counted as",
            "passes, because scoring them as passes would hide the defect.",
            "",
        ]
        lines += [f"- `{fid}`" for fid in summary["errored_ids"]]

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.runner")
    parser.add_argument("--json", dest="json_path", default=str(EVALS / "results.json"))
    parser.add_argument("--only", choices=["breaking", "should_pass", "subtle"])
    parser.add_argument("--manifest", default=str(MANIFEST))
    args = parser.parse_args(argv)

    lineage = Lineage.from_path(args.manifest)
    labels = load_labels()
    if args.only:
        labels = [x for x in labels if x["category"] == args.only]

    outcomes = [run_fixture(x, lineage) for x in labels]
    summary = summarise(outcomes)

    print(render(outcomes, summary))

    Path(args.json_path).write_text(
        json.dumps(
            {
                "summary": summary,
                "outcomes": [
                    {
                        "id": o.fixture_id,
                        "category": o.category,
                        "expected": o.expected,
                        "actual": o.actual,
                        "errored": o.errored,
                        "resolved": o.resolved,
                        "unresolved": o.unresolved,
                        "reasons": o.reasons,
                    }
                    for o in outcomes
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nWrote {args.json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
