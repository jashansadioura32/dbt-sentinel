#!/usr/bin/env python3
"""Fail CI when a published metric regresses below its recorded baseline.

The point is not to enforce good numbers — the numbers are mediocre and honestly
published. The point is that `evals/BASELINE.md` states figures a reader will trust, and
a change that silently invalidates them must not merge. If a number moves legitimately,
update the floor here *and* the document in the same commit, so the two cannot drift.

Floors are set at the measured values: day-3/day-4 for retrieval and the check layer,
day-7 for severity after the two deferred failure modes were fixed. Metrics where lower
is better are ceilings instead.

The severity floors ratcheted on day 7 (precision 0.800 -> 0.909, recall 0.533 -> 0.588,
FPR 0.200 -> 0.000, dropped fixtures 4 -> 0). Ratcheting is the point: the old floors
would now pass while the tool silently regressed back to the day-3 behaviour.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# (file, dotted path, comparison, threshold, what it is)
CHECKS: list[tuple[str, str, str, float, str]] = [
    ("evals/results.json", "summary.precision", ">=", 0.909, "severity precision"),
    ("evals/results.json", "summary.recall", ">=", 0.588, "severity recall"),
    (
        "evals/results.json",
        "summary.false_positive_rate_should_pass",
        "<=",
        0.000,
        "false-positive rate on should-pass fixtures",
    ),
    (
        "evals/results.json",
        "summary.n_errored",
        "<=",
        0,
        "fixtures silently dropped",
    ),
    (
        "evals/retrieval_results.json",
        # Dropped from 0.708 on day 7, and the fall is an improvement. The 4 fixtures
        # that used to resolve to no node now reach the retriever, so each returns 3
        # rules with 1-2 correct where it previously returned nothing. Recall rose
        # 0.630 -> 0.778 on the same change. Precision@3 fell because the denominator
        # grew, not because retrieval got worse.
        "summary.precision_at_k",
        ">=",
        0.636,
        "retrieval precision@3",
    ),
    (
        "evals/retrieval_results.json",
        "summary.recall_at_k",
        ">=",
        0.778,
        "retrieval recall@3",
    ),
    (
        "evals/retrieval_results.json",
        "summary.correct_silence_rate",
        ">=",
        0.455,
        "correct silence on no-rule fixtures",
    ),
    # Check layer. Precision and recall are 1.000 on 8 self-authored fixtures, which is
    # weak evidence and is labelled as such in BASELINE.md — the floors are set there
    # anyway, because a drop would mean a check stopped working on the case it was
    # written for. The two that carry real weight are below.
    ("evals/checks_results.json", "summary.precision", ">=", 1.000, "check precision"),
    ("evals/checks_results.json", "summary.recall", ">=", 1.000, "check recall"),
    (
        "evals/checks_results.json",
        "summary.false_positive_rate_on_near_misses",
        "<=",
        0.000,
        "check FPR on idiomatic near misses",
    ),
    (
        # The real noise guard: fixtures written before the check layer existed, so
        # nothing was tuned against them. Anything above 0 means a check started firing
        # on an ordinary PR and nobody declared it.
        "evals/checks_results.json",
        "summary.undeclared_findings_on_severity_fixtures",
        "<=",
        0,
        "undeclared check findings on the severity fixtures",
    ),
]


def _dig(payload: dict, dotted: str):
    node = payload
    for part in dotted.split("."):
        node = node[part]
    return node


def main() -> int:
    failures: list[str] = []
    cache: dict[str, dict] = {}

    for rel_path, dotted, comparison, threshold, label in CHECKS:
        path = REPO / rel_path
        if not path.exists():
            failures.append(f"{rel_path} is missing — did the eval step run?")
            continue
        if rel_path not in cache:
            cache[rel_path] = json.loads(path.read_text(encoding="utf-8"))

        actual = _dig(cache[rel_path], dotted)
        ok = actual >= threshold if comparison == ">=" else actual <= threshold
        arrow = "worse than" if not ok else "ok vs"
        print(f"{'PASS' if ok else 'FAIL'}  {label}: {actual} {arrow} {comparison} {threshold}")
        if not ok:
            failures.append(
                f"{label} is {actual}, which is worse than the published baseline "
                f"({comparison} {threshold}). Either fix the regression, or update both "
                f"this floor and evals/BASELINE.md in the same commit."
            )

    if failures:
        print("\nRegressions against published baselines:\n", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print("\nAll published baselines hold.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
