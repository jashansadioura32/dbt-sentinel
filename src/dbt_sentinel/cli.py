"""CLI: `python -m dbt_sentinel --manifest target/manifest.json --diff pr.diff`"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .diff import resolve_changes
from .lineage import Lineage, ManifestError
from .report import build_assessments, render_markdown, render_mermaid


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dbt-sentinel")
    parser.add_argument("--manifest", required=True, help="path to target/manifest.json")
    parser.add_argument("--diff", required=True, help="unified diff file, or - for stdin")
    parser.add_argument("--mermaid", action="store_true", help="emit a diagram per change")
    parser.add_argument(
        "--fail-on",
        choices=["high", "medium", "low", "never"],
        default="never",
        help="exit non-zero at or above this severity",
    )
    args = parser.parse_args(argv)

    try:
        lineage = Lineage.from_path(args.manifest)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    diff_text = sys.stdin.read() if args.diff == "-" else Path(args.diff).read_text(encoding="utf-8")

    changes, unresolved = resolve_changes(diff_text, lineage)
    assessments = build_assessments(changes, lineage)

    print(render_markdown(assessments, unresolved))

    if args.mermaid:
        for a in assessments:
            print(f"\n<!-- {a.changed.node.name} -->\n```mermaid")
            print(render_mermaid(a))
            print("```")

    if args.fail_on != "never":
        threshold = {"high": 2, "medium": 1, "low": 0}[args.fail_on]
        order = {"high": 2, "medium": 1, "low": 0}
        if any(order[a.severity] >= threshold for a in assessments):
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
