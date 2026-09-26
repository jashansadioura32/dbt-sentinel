"""Score the agent on the SQL review checklist (policies/sql_quality.yml).

    python -m evals.sql_policy_eval --dry-run   # validate fixtures, no API calls
    python -m evals.sql_policy_eval             # run the agent; needs OPENAI_API_KEY

Separate from `evals.compare` for the reason retrieval and checks are separate: the 30
severity fixtures are scored on `expected_severity`, and a checklist finding is advisory.
It never moves the blast-radius severity or the status, so folding it in would change
the published severity numbers for a reason that has nothing to do with severity.

Two things are deliberately the same as compare.py:
  - It measures and does not tune. Changing the prompt in the session that establishes
    the numbers destroys them.
  - It refuses to publish a number the agent didn't earn. With no key it exits 2. If the
    agent spent zero tokens across every fixture, it never ran, so it exits 2 and writes
    no results file, keyed off token spend rather than the degraded flag (the day-7
    regression in tests/test_day7.py).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbt_sentinel.agent import ReviewerAgent  # noqa: E402
from dbt_sentinel.diff import resolve_changes  # noqa: E402
from dbt_sentinel.lineage import Lineage  # noqa: E402
from dbt_sentinel.pricing import cost_usd  # noqa: E402
from dbt_sentinel.report import build_assessments  # noqa: E402
from dbt_sentinel.retrieval import PolicyPack  # noqa: E402

EVALS = Path(__file__).resolve().parent
FIXTURES = EVALS / "fixtures" / "sql"
MANIFEST = EVALS / "manifest" / "manifest.json"
RESULTS = EVALS / "sql_policy_results.json"
SEVERITY = {"low": 0, "medium": 1, "high": 2}


def load_labels() -> list[dict]:
    return yaml.safe_load((FIXTURES / "labels.yml").read_text(encoding="utf-8"))["fixtures"]


def validate(lineage: Lineage, pack: PolicyPack) -> list[str]:
    """Problems with the fixtures themselves. An eval over a fixture that resolves to no
    model, or cites a rule the pack doesn't have, would score a broken setup as a
    reasoning failure."""
    checklist = {r.rule_id for r in pack.checklist_rules}
    problems = []
    for label in load_labels():
        fid = label["id"]
        diff, head = FIXTURES / f"{fid}.diff", FIXTURES / f"{fid}.sql"
        if not diff.exists() or not head.exists():
            problems.append(f"{fid}: needs both {fid}.diff and {fid}.sql")
            continue
        changes, _ = resolve_changes(diff.read_text(encoding="utf-8"), lineage)
        if len(changes) != 1:
            problems.append(f"{fid}: resolves to {len(changes)} models, expected 1")
        elif not pack.checklist(changes[0]):
            problems.append(f"{fid}: no checklist rule applies to {changes[0].file.path}")
        for rule_id in label["expected_rule_ids"]:
            if rule_id not in checklist:
                problems.append(f"{fid}: expected rule {rule_id!r} is not a checklist rule")
    return problems


def run_fixture(label: dict, lineage: Lineage, pack: PolicyPack) -> dict:
    fid = label["id"]
    diff = (FIXTURES / f"{fid}.diff").read_text(encoding="utf-8")
    head = (FIXTURES / f"{fid}.sql").read_text(encoding="utf-8")
    changes, _ = resolve_changes(diff, lineage)
    changed_path = changes[0].file.path

    # Mirrors the App: the PR's version of the changed file, the base version of
    # everything else (which the tool then reads from the manifest).
    result = ReviewerAgent(
        lineage, pack, head_source=lambda path: head if path == changed_path else None
    ).review(build_assessments(changes, lineage))

    # A citation counts from its rule's own severity up. A LOW rule (column-name-spelling)
    # scored against a MEDIUM floor would count every correct finding as a miss; the
    # medium and high rules are unaffected, so their published numbers don't move.
    floor = {r.rule_id: min(SEVERITY[r.severity], SEVERITY["medium"]) for r in pack.checklist_rules}
    cited = sorted({
        f.rule_id
        for f in result.findings
        if f.rule_id in floor and SEVERITY[f.severity] >= floor[f.rule_id]
    })
    expected = sorted(label["expected_rule_ids"])
    return {
        "id": fid,
        "expected": expected,
        "cited": cited,
        "pass": cited == expected,
        "degraded": result.degraded,
        "degradation_reason": result.degradation_reason,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "uncited_rule_ids": result.uncited_rule_ids,
        # Every finding, not just the scored ones: a miss that was the right rule at LOW,
        # or a neighbouring rule cited instead, needs a different fix than silence.
        "findings": [
            {"rule_id": f.rule_id, "severity": f.severity, "explanation": f.explanation}
            for f in result.findings
        ],
    }


def summarise(rows: list[dict]) -> dict:
    tp = sum(len(set(r["expected"]) & set(r["cited"])) for r in rows)
    fp = sum(len(set(r["cited"]) - set(r["expected"])) for r in rows)
    fn = sum(len(set(r["expected"]) - set(r["cited"])) for r in rows)
    near_misses = [r for r in rows if not r["expected"]]
    tokens_in = sum(r["input_tokens"] for r in rows)
    tokens_out = sum(r["output_tokens"] for r in rows)
    return {
        "fixtures": len(rows),
        "precision": round(tp / (tp + fp), 3) if tp + fp else 0.0,
        "recall": round(tp / (tp + fn), 3) if tp + fn else 0.0,
        "false_positive_rate_on_near_misses": round(
            sum(1 for r in near_misses if r["cited"]) / len(near_misses), 3
        ) if near_misses else 0.0,
        "exact_match": sum(1 for r in rows if r["pass"]),
        "degraded": sum(1 for r in rows if r["degraded"]),
        "invented_rule_ids": sorted({i for r in rows for i in r["uncited_rule_ids"]}),
        "total_input_tokens": tokens_in,
        "total_output_tokens": tokens_out,
        "cost_usd": round(cost_usd(tokens_in, tokens_out), 4),
    }


def render(rows: list[dict], summary: dict) -> str:
    lines = ["# SQL review checklist eval", "", "| Fixture | Expected | Cited | |", "|---|---|---|---|"]
    for r in rows:
        exp = ", ".join(f"`{x}`" for x in r["expected"]) or "—"
        got = ", ".join(f"`{x}`" for x in r["cited"]) or ("degraded" if r["degraded"] else "—")
        lines.append(f"| `{r['id']}` | {exp} | {got} | {'PASS' if r['pass'] else 'FAIL'} |")
    s = summary
    lines += [
        "",
        f"Precision {s['precision']:.3f} · recall {s['recall']:.3f} · "
        f"near-miss FPR {s['false_positive_rate_on_near_misses']:.3f} · "
        f"exact {s['exact_match']}/{s['fixtures']} · degraded {s['degraded']} · "
        f"${s['cost_usd']:.4f}",
    ]
    if s["invented_rule_ids"]:
        lines.append(f"Invented rule ids: {', '.join(s['invented_rule_ids'])}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="evals.sql_policy_eval")
    parser.add_argument("--dry-run", action="store_true", help="validate fixtures only")
    args = parser.parse_args(argv)

    lineage = Lineage.from_path(MANIFEST)
    pack = PolicyPack.load()
    pack.load_cache()

    if problems := validate(lineage, pack):
        print("Fixture problems (fix these before any run means anything):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2
    if args.dry_run:
        print(f"{len(load_labels())} SQL checklist fixtures valid. No API calls made.")
        return 0

    if not os.environ.get("OPENAI_API_KEY"):
        print(
            "error: OPENAI_API_KEY is not set, so the agent can't run and there is "
            "nothing to measure. Set it, or use --dry-run to validate the fixtures.",
            file=sys.stderr,
        )
        return 2

    rows = [run_fixture(label, lineage, pack) for label in load_labels()]
    summary = summarise(rows)
    print(render(rows, summary))

    if not summary["total_input_tokens"] and not summary["total_output_tokens"]:
        reason = next((r["degradation_reason"] for r in rows if r["degradation_reason"]), "unknown")
        print(
            f"\nerror: the agent spent no tokens on any fixture, so it never ran "
            f"({reason}). No results file written: a table of zeros would be published "
            f"as a measurement.",
            file=sys.stderr,
        )
        return 2

    RESULTS.write_text(json.dumps({"summary": summary, "fixtures": rows}, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {RESULTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
