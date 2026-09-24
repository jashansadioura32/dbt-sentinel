"""Score policy retrieval on its own, separately from end-to-end severity accuracy.

    python -m evals.retrieval_eval

Why this is a separate file and a separate metric: when the day-6 agent gets a fixture
wrong, the cause is either "the right rule never reached the model" or "the right rule
reached it and it reasoned badly". Those need different fixes — rule keywords versus
prompt or model — and a single end-to-end number cannot tell them apart. Measuring
retrieval in isolation now means day 6 can attribute each failure.

Metrics reported:
  precision@3  of the retrieved rules, how many were expected (over fixtures that expect
               at least one rule)
  recall@3     of the expected rules, how many were retrieved
  hit@3        fraction of fixtures where at least one expected rule appeared
  silence      on the 11 fixtures expecting NO rule: how often we correctly returned none.
               This is the precision guard. A retriever that always returns three rules
               scores well on recall and is useless.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbt_sentinel.diff import resolve_changes  # noqa: E402
from dbt_sentinel.lineage import Lineage  # noqa: E402
from dbt_sentinel.retrieval import PolicyPack, summarise_change  # noqa: E402

EVALS = Path(__file__).resolve().parent
FIXTURES = EVALS / "fixtures"
MANIFEST = EVALS / "manifest" / "manifest.json"
TOP_K = 3


def _rate(num: int, den: int) -> float:
    return num / den if den else 0.0


def run(top_k: int = TOP_K) -> dict:
    lineage = Lineage.from_path(MANIFEST)
    pack = PolicyPack.load()
    labels = yaml.safe_load((FIXTURES / "labels.yml").read_text(encoding="utf-8"))["fixtures"]

    rows: list[dict] = []
    for label in labels:
        diff_text = (FIXTURES / f"{label['id']}.diff").read_text(encoding="utf-8")
        changes, _ = resolve_changes(diff_text, lineage)

        retrieved: list[str] = []
        scores: dict[str, float] = {}
        # A fixture can touch several nodes; the union of their retrievals is what the
        # reviewer would see, so that is what gets scored.
        for changed in changes:
            result = pack.retrieve(changed, summarise_change(changed), top_k=top_k)
            for item in result.retrieved:
                if item.rule_id not in scores or item.score > scores[item.rule_id]:
                    scores[item.rule_id] = item.score
                if item.rule_id not in retrieved:
                    retrieved.append(item.rule_id)

        retrieved = sorted(retrieved, key=lambda r: -scores[r])[:top_k]
        expected = list(label.get("expected_rule_ids") or [])
        hit = sorted(set(retrieved) & set(expected))

        rows.append(
            {
                "id": label["id"],
                "category": label["category"],
                "expected": expected,
                "retrieved": retrieved,
                "hits": hit,
                "resolved_nodes": [c.node.name for c in changes],
                "unresolvable": not changes,
            }
        )

    with_rules = [r for r in rows if r["expected"]]
    without_rules = [r for r in rows if not r["expected"]]

    total_retrieved = sum(len(r["retrieved"]) for r in with_rules)
    total_hits = sum(len(r["hits"]) for r in with_rules)
    total_expected = sum(len(r["expected"]) for r in with_rules)

    silent = sum(1 for r in without_rules if not r["retrieved"])

    summary = {
        "top_k": top_k,
        "n_fixtures": len(rows),
        "n_expecting_rules": len(with_rules),
        "n_expecting_no_rules": len(without_rules),
        "precision_at_k": round(_rate(total_hits, total_retrieved), 3),
        "recall_at_k": round(_rate(total_hits, total_expected), 3),
        "hit_at_k": round(_rate(sum(1 for r in with_rules if r["hits"]), len(with_rules)), 3),
        "correct_silence_rate": round(_rate(silent, len(without_rules)), 3),
        "pack_size": len(pack),
    }
    return {"summary": summary, "rows": rows}


def render(result: dict) -> str:
    s = result["summary"]
    lines = ["## Retrieval per fixture", "", "| Fixture | Expected | Retrieved@3 | Hit |", "|---|---|---|---|"]
    for r in result["rows"]:
        exp = ", ".join(f"`{x}`" for x in r["expected"]) or "—"
        got = ", ".join(f"`{x}`" for x in r["retrieved"]) or "—"
        if r["expected"]:
            mark = "yes" if r["hits"] else "MISS"
        else:
            mark = "ok (silent)" if not r["retrieved"] else "NOISE"
        lines.append(f"| `{r['id']}` | {exp} | {got} | {mark} |")

    lines += [
        "",
        "## Retrieval metrics",
        "",
        f"Policy pack: {s['pack_size']} rules | top_k = {s['top_k']}",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Precision@{s['top_k']} | {s['precision_at_k']:.3f} |",
        f"| Recall@{s['top_k']} | {s['recall_at_k']:.3f} |",
        f"| Hit@{s['top_k']} (>=1 expected rule found) | {s['hit_at_k']:.3f} |",
        f"| **Correct silence on no-rule fixtures** | **{s['correct_silence_rate']:.3f}** |",
        "",
        f"Scored over {s['n_expecting_rules']} fixtures expecting rules; "
        f"{s['n_expecting_no_rules']} expecting none.",
    ]
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
    parser = argparse.ArgumentParser(prog="evals.retrieval_eval")
    parser.add_argument("--top-k", type=int, default=TOP_K)
    parser.add_argument("--json", dest="json_path", default=str(EVALS / "retrieval_results.json"))
    args = parser.parse_args(argv)

    result = run(top_k=args.top_k)
    print(render(result))
    Path(args.json_path).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"\nWrote {args.json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
