"""CLI: `python -m dbt_sentinel --manifest target/manifest.json --diff pr.diff`"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
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
    parser.add_argument(
        "--changed-at",
        help=(
            "ISO-8601 timestamp of the change under review (e.g. the PR head commit "
            "date). If the manifest predates it, a staleness warning is emitted."
        ),
    )
    args = parser.parse_args(argv)

    try:
        lineage = Lineage.from_path(args.manifest)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    diff_text = sys.stdin.read() if args.diff == "-" else Path(args.diff).read_text(encoding="utf-8")

    changed_at = None
    if args.changed_at:
        try:
            changed_at = datetime.fromisoformat(args.changed_at.replace("Z", "+00:00"))
        except ValueError:
            print(
                f"error: --changed-at {args.changed_at!r} is not ISO-8601. "
                f"Use e.g. 2026-09-15T10:00:00Z.",
                file=sys.stderr,
            )
            return 2
        if changed_at.tzinfo is None:
            changed_at = changed_at.replace(tzinfo=timezone.utc)

    changes, unresolved = resolve_changes(diff_text, lineage)
    assessments = build_assessments(changes, lineage)

    # Printed before the findings: a reader who sees a clean review needs to know the
    # lineage behind it may be outdated before they trust it.
    if staleness := lineage.staleness_warning(changed_at):
        print(f"> ⚠️ **Stale manifest.** {staleness}\n")

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
