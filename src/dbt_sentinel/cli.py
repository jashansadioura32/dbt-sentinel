"""CLI: `python -m dbt_sentinel --manifest target/manifest.json --diff pr.diff`

Or, from inside a dbt repo, `dbt-sentinel --since origin/main` to review the current
branch the way its PR will be reviewed. This is what the VS Code hook in
`integrations/vscode/` runs after each commit.

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
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .checks import run_checks
from .diff import parse_diff, resolve_changes
from .lineage import Lineage, ManifestError
from .report import build_assessments, render_checks, render_markdown, render_mermaid


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
    pack_note = ""
    try:
        pack = PolicyPack.load(policy_dir)
        pack.load_cache()
    except (FileNotFoundError, ValueError) as exc:
        # The agent still runs, but with no rule_ids to cite. Said out loud because a
        # non-editable install has no pack beside it, and a review that silently lost
        # its governance rules looks identical to one where no rule applied.
        pack_note = (
            f"\n> ⚠️ Policy pack unavailable ({exc}). The agent ran without governance "
            f"rules. Pass --policies DIR, or install from a clone with `pip install -e`.\n"
        )

    # ReviewerAgent.review never raises; every failure path returns a degraded result
    # that the renderer states plainly.
    result = ReviewerAgent(lineage, pack).review(assessments)
    rendered = render_agent_findings(result) + pack_note

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


class GitError(Exception):
    """git could not produce the diff. Always an exit-2 misconfiguration, never a finding."""


def _git(*args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise GitError("git is not on PATH. Install git, or pass a diff file with --diff.") from exc
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or f"`git {' '.join(args)}` exited {proc.returncode}")
    return proc.stdout


_DBT_SOURCE_SUFFIXES = {".sql", ".yml", ".yaml", ".csv", ".md", ".py"}


def _last_edited(ref: str) -> datetime:
    """When the branch's changed files were last edited on disk.

    This, not the commit time, is what a local manifest must postdate. The local
    workflow is edit, `dbt parse`, commit, so the commit is always seconds newer than a
    perfectly fresh manifest. Comparing against it warned on every commit (found live on
    jeffle-shop: "compiled 0.0h before the change"), and a warning that always fires
    teaches people to ignore the one time it is real. An edit made after the last parse
    is what makes the graph stale, and that is what file mtimes record.

    Only files dbt parses count. A committed `target/manifest.json` is itself in the diff
    and is always written a moment after its own `generated_at`, so counting it would
    make every review look stale; a `.gitignore` edit cannot change the graph at all.

    Falls back to the head commit time when no such file is left on disk (a branch that
    only deletes).
    """
    names = _git("diff", "--name-only", "--no-renames", "--relative", f"{ref}...HEAD")
    mtimes = [
        path.stat().st_mtime
        for path in map(Path, names.splitlines())
        if path.suffix in _DBT_SOURCE_SUFFIXES and "target" not in path.parts and path.is_file()
    ]
    if mtimes:
        return datetime.fromtimestamp(max(mtimes), tz=timezone.utc)
    return datetime.fromisoformat(_git("log", "-1", "--format=%cI", "HEAD").strip())


def _git_diff_since(ref: str) -> tuple[str, datetime]:
    """The branch's diff against `ref`, and when its changed files were last edited.

    Three dots, not two: `ref...HEAD` diffs from the merge base, which is what the PR
    will show. A two-dot diff would also report every commit merged into `ref` since the
    branch forked, as if this branch had reverted them.

    `--relative` scopes paths to the working directory, so running from a dbt project in
    a monorepo subfolder yields the project-relative paths the manifest records.
    """
    try:
        _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    except GitError as exc:
        raise GitError(
            f"unknown ref {ref!r} ({exc}). Run `git fetch`, or pass a ref that exists "
            f"locally, e.g. `--since main`."
        ) from exc
    diff = _git("diff", "--no-color", "--no-ext-diff", "--relative", f"{ref}...HEAD")
    # Feeds the staleness check, which matters more locally than in CI: the manifest is
    # whatever `dbt parse` last wrote, and a stale graph produces a clean-looking review
    # with shrunken reach.
    return diff, _last_edited(ref)


def _force_utf8_stdout() -> None:
    """The rendered comment contains emoji severity badges and arrows.

    A Windows console defaults to cp1252 and cannot encode them, so without this the
    tool dies with a UnicodeEncodeError before printing a single finding — exit 1 on a
    clean no-op PR, which reads as a failed review rather than a broken terminal. Exit
    codes are the contract CI depends on, so this has to happen before any output.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdout()
    parser = argparse.ArgumentParser(prog="dbt-sentinel")
    parser.add_argument(
        "--manifest",
        default="target/manifest.json",
        help="path to the dbt manifest (default: target/manifest.json)",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--diff", help="unified diff file, or - for stdin")
    source.add_argument(
        "--since",
        metavar="REF",
        help="review the current branch against REF (e.g. origin/main), via git",
    )
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
        "--no-checks",
        action="store_true",
        help="skip the deterministic check layer (schema entries, hardcoded relations, ...)",
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

    last_edited_at = None
    if args.since:
        try:
            diff_text, last_edited_at = _git_diff_since(args.since)
        except GitError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    elif args.diff == "-":
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

    changed_at = last_edited_at
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

    if not args.no_checks:
        # Its own section, never folded into severity above: a lint finding has no reach.
        if section := render_checks(run_checks(parse_diff(diff_text), changes, lineage)):
            print()
            print(section)

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
