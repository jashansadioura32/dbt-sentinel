"""Score the deterministic check layer on its own.

    python -m evals.checks_eval

Separate from `evals.runner` for the same reason retrieval is: the 30 severity fixtures
are labelled `expected_severity`, and checks do not produce a severity. Folding them into
one number would also break the thing the separation exists to protect — a check finding
must never move the published precision/recall/FPR of the blast radius.

Two populations are scored, and the second is the one that matters:

  1. evals/fixtures/checks/ — 8 purpose-built fixtures, one true positive and one
     idiomatic near-miss per check. The near misses decide precision.

  2. evals/fixtures/ — all 30 severity fixtures, scored for SILENCE. None of them was
     written with checks in mind, so they are the closest thing available to "ordinary
     PRs this tool was not tuned on". A check layer that fires on half of them is noise
     regardless of how it scores on its own fixtures. Two known exceptions are declared
     in EXPECTED_ON_SEVERITY_FIXTURES below rather than silently tolerated.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbt_sentinel.checks import CHECKS, run_checks  # noqa: E402
from dbt_sentinel.diff import parse_diff, resolve_changes  # noqa: E402
from dbt_sentinel.lineage import Lineage  # noqa: E402

EVALS = Path(__file__).resolve().parent
FIXTURES = EVALS / "fixtures" / "checks"
SEVERITY_FIXTURES = EVALS / "fixtures"
MANIFEST = EVALS / "manifest" / "manifest.json"

# Severity fixtures that legitimately carry a check finding. Declared, not tolerated:
# each is a real violation the check layer is right to report, and naming them here keeps
# an accidental regression from hiding among them.
EXPECTED_ON_SEVERITY_FIXTURES = {
    # Adds a `tests:` block. Labelled should_pass on severity, and that must not change —
    # this is the fixture proving a check fires without touching Assessment.severity.
    "p03_test_added": {"deprecated-tests-key"},
    # Adds a new model with no schema.yml entry. Genuinely undocumented.
    "p04_new_unused_model": {"missing-schema-entry"},
}


def _findings_for(diff_path: Path, lineage: Lineage) -> list:
    text = diff_path.read_text(encoding="utf-8")
    changes, _ = resolve_changes(text, lineage)
    return run_checks(parse_diff(text), changes, lineage)


def run() -> dict:
    lineage = Lineage.from_path(str(MANIFEST))
    labels = yaml.safe_load((FIXTURES / "labels.yml").read_text(encoding="utf-8"))["fixtures"]

    tp = fp = fn = 0
    rows = []
    for label in labels:
        fixture_id = label["id"]
        expected = set(label["expected_check_ids"])
        got = {f.check_id for f in _findings_for(FIXTURES / f"{fixture_id}.diff", lineage)}

        tp += len(expected & got)
        fp += len(got - expected)
        fn += len(expected - got)
        rows.append({
            "id": fixture_id,
            "expected": sorted(expected),
            "got": sorted(got),
            "pass": expected == got,
        })

    # Silence on the fixtures this layer was never tuned against.
    noise_rows = []
    unexpected = 0
    for diff_path in sorted(SEVERITY_FIXTURES.glob("*.diff")):
        got = {f.check_id for f in _findings_for(diff_path, lineage)}
        allowed = EXPECTED_ON_SEVERITY_FIXTURES.get(diff_path.stem, set())
        surprise = got - allowed
        if got:
            noise_rows.append({
                "id": diff_path.stem,
                "got": sorted(got),
                "declared": sorted(allowed),
                "unexpected": sorted(surprise),
            })
        unexpected += len(surprise)

    n_severity = len(list(SEVERITY_FIXTURES.glob("*.diff")))
    quiet = sum(
        1
        for d in SEVERITY_FIXTURES.glob("*.diff")
        if not {f.check_id for f in _findings_for(d, lineage)}
    )

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    near_misses = [r for r in rows if not r["expected"]]
    fp_rate = sum(1 for r in near_misses if r["got"]) / len(near_misses) if near_misses else 0.0

    return {
        "summary": {
            "checks": len(CHECKS),
            "fixtures": len(rows),
            "precision": round(precision, 3),
            "recall": round(recall, 3),
            "false_positive_rate_on_near_misses": round(fp_rate, 3),
            "exact_match": sum(1 for r in rows if r["pass"]),
            "severity_fixtures_silent": quiet,
            "severity_fixtures_total": n_severity,
            "undeclared_findings_on_severity_fixtures": unexpected,
        },
        "fixtures": rows,
        "severity_fixture_noise": noise_rows,
    }


def render(result: dict) -> str:
    s = result["summary"]
    lines = [
        "# Check layer eval",
        "",
        "| Fixture | Expected | Got | |",
        "|---|---|---|---|",
    ]
    for row in result["fixtures"]:
        exp = ", ".join(f"`{c}`" for c in row["expected"]) or "—"
        got = ", ".join(f"`{c}`" for c in row["got"]) or "—"
        lines.append(f"| `{row['id']}` | {exp} | {got} | {'PASS' if row['pass'] else 'FAIL'} |")

    lines += [
        "",
        "## Metrics",
        "",
        f"{s['checks']} checks | {s['fixtures']} fixtures",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Precision | {s['precision']:.3f} |",
        f"| Recall | {s['recall']:.3f} |",
        f"| **False-positive rate on near misses** | **{s['false_positive_rate_on_near_misses']:.3f}** |",
        f"| Exact match | {s['exact_match']}/{s['fixtures']} |",
        "",
        "## Silence on the severity fixtures",
        "",
        f"{s['severity_fixtures_silent']}/{s['severity_fixtures_total']} of the severity "
        f"fixtures draw no check finding at all. Those fixtures were written before this "
        f"layer existed, so they are the closest available stand-in for ordinary PRs.",
        "",
        f"Undeclared findings: **{s['undeclared_findings_on_severity_fixtures']}** "
        f"(anything above 0 is a regression).",
    ]
    for row in result["severity_fixture_noise"]:
        mark = " ⚠️ UNDECLARED" if row["unexpected"] else " (declared)"
        lines.append(f"- `{row['id']}` → {', '.join(row['got'])}{mark}")

    return "\n".join(lines)



def _force_utf8_stdout() -> None:
    """Windows consoles default to cp1252, which cannot encode the arrows in the tables.

    Without this, `python -m evals.checks_eval` dies with a UnicodeEncodeError on a
    stock Windows clone and the published numbers cannot be reproduced there at all.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass

def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdout()
    parser = argparse.ArgumentParser(prog="evals.checks_eval")
    parser.add_argument("--json", dest="json_path", default=str(EVALS / "checks_results.json"))
    args = parser.parse_args(argv)

    result = run()
    print(render(result))
    Path(args.json_path).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"\nWrote {args.json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
