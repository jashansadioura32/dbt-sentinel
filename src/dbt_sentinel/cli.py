"""CLI: `python -m dbt_sentinel --manifest target/manifest.json --diff pr.diff`

Exit codes, because CI needs to tell a finding from a misconfiguration:

    0  Reviewed. Nothing at or above --fail-on.
    1  Reviewed. Findings at or above --fail-on.
    2  Could not run: unreadable manifest, unreadable diff, malformed argument.

The 1/2 split is the load-bearing one. A pipeline that reports a missing manifest as a
failed review teaches its users that the tool cries wolf, and the tool gets switched off
long before it ever reports a real breaking change.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from .diff import resolve_changes
from .lineage import Lineage, ManifestError
from .report import build_assessments, render_markdown, render_mermaid


def _run_agent(assessments: list, lineage: Lineage, policy_dir: str | None) -> str:
    """Run the reviewer agent and render its findings.

    The lineage is passed in rather than reached for globally: the agent's tools answer
    lookups against the same graph the deterministic report was built from, and a second
    load could silently disagree with it.
    """
    from .agent import ReviewerAgent
    from .pricing import cost_usd
    from .report import render_agent_findings
    from .retrieval import PolicyPack

    pack = None
    try:
        pack = PolicyPack.load(policy_dir)
        pack.load_cache()
    except (FileNotFoundError, ValueError):
        pass  # the agent still works without policy retrieval, just with less to cite

    # ReviewerAgent.review never raises; every failure path returns a degraded result
    # that the renderer states plainly.
    result = ReviewerAgent(lineage, pack).review(assessments)
    rendered = render_agent_findings(result)

    # Same cost and latency the PR comment footer carries. A local run that cannot tell
    # you what it spent makes the published per-PR figure unverifiable.
    if result.input_tokens or result.output_tokens:
        rendered += (
            f"\n_agent: {result.input_tokens:,} in / {result.output_tokens:,} out · "
            f"${cost_usd(result.input_tokens, result.output_tokens):.4f} · "
            f"{result.latency_s:.1f}s · {result.rounds} round(s)_\n"
        )
    return rendered


def _explain_retrieval(changes: list, policy_dir: str | None) -> str:
    """Render what retrieval did and why, including what it eliminated.

    Showing only the winners makes a retrieval bug look like a reasoning bug. The
    eliminated list is what distinguishes "the rule was never a candidate" from "the
    rule was a candidate and scored too low".
    """
    from .retrieval import PolicyPack, summarise_change

    try:
        pack = PolicyPack.load(policy_dir)
    except (FileNotFoundError, ValueError) as exc:
        return f"\n## Policy retrieval\n\n> ⚠️ Policy pack unavailable: {exc}"

    pack.load_cache()

    lines = ["", "## Policy retrieval", "", f"Pack: {len(pack)} rules"]
    for changed in changes:
        summary = summarise_change(changed)
        result = pack.retrieve(changed, summary)
        lines.append("")
        lines.append(f"### `{changed.node.name}`")
        if not result.retrieved:
            lines.append("- No rule matched. Structural scoring only.")
        for item in result.retrieved:
            matched = f" · keywords: {', '.join(item.matched_keywords)}" if item.matched_keywords else ""
            lines.append(
                f"- `{item.rule_id}` ({item.rule.severity}) score={item.score:.3f}{matched}"
            )
        lines.append(f"- _{len(result.eliminated)} rule(s) eliminated by prefilter_")

    pack.save_cache()
    return "\n".join(lines)


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
    parser.add_argument(
        "--explain",
        action="store_true",
        help="show which policy rules were retrieved per change, with scores",
    )
    parser.add_argument(
        "--policies",
        help="path to the policy pack directory (default: ./policies)",
    )
    parser.add_argument(
        "--agent",
        action="store_true",
        help=(
            "run the reviewer agent for judgment findings. Requires OPENAI_API_KEY. "
            "Falls back to deterministic-only output, with a note, on any failure."
        ),
    )
    args = parser.parse_args(argv)

    try:
        lineage = Lineage.from_path(args.manifest)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.diff == "-":
        diff_text = sys.stdin.read()
    else:
        try:
            diff_text = Path(args.diff).read_text(encoding="utf-8")
        except OSError as exc:
            # Unguarded, this raised and exited 1 — reporting a missing file as though
            # the PR were risky.
            print(
                f"error: could not read --diff {args.diff!r}: {exc.strerror or exc}. "
                f"Pass a path to a unified diff, or `-` to read one from stdin.",
                file=sys.stderr,
            )
            return 2

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

    if args.agent:
        print()
        print(_run_agent(assessments, lineage, args.policies))

    if args.explain:
        print(_explain_retrieval(changes, args.policies))

    if args.fail_on != "never":
        threshold = {"high": 2, "medium": 1, "low": 0}[args.fail_on]
        order = {"high": 2, "medium": 1, "low": 0}
        if any(order[a.severity] >= threshold for a in assessments):
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
