"""End-to-end review pipeline: a PR reference in, a posted review out.

Shared by the webhook and the CLI so both paths produce byte-identical comments. The
sequence is deterministic-first throughout: resolve, traverse, score, retrieve, and only
then optionally ask the model for judgment. Every stage degrades rather than aborting,
because a partial review that says what it could not do beats no review at all.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .diff import resolve_changes
from .github import GitHubClient, GitHubError, PullRequestRef
from .lineage import Lineage, ManifestError
from .pricing import cost_usd
from .report import build_assessments, render_agent_findings, render_markdown

# Identifies our own comment so re-reviews update in place. Invisible when rendered.
COMMENT_MARKER = "<!-- dbt-sentinel:review -->"

# Where a committed manifest is expected when no CI artifact is available.
DEFAULT_MANIFEST_PATHS = ("target/manifest.json", "manifest.json")


@dataclass
class ReviewOutcome:
    comment: str
    severity: str = "low"
    manifest_source: str = "none"
    warnings: list[str] = field(default_factory=list)
    agent_ran: bool = False
    cost_usd: float = 0.0
    latency_s: float = 0.0
    changed_nodes: int = 0

    @property
    def status_state(self) -> str:
        """HIGH blocks the merge; everything else passes.

        MEDIUM deliberately does not block. A gate that fires on judgment calls gets
        switched off within a week, and then the HIGH signal is gone too.
        """
        return "failure" if self.severity == "high" else "success"

    @property
    def status_description(self) -> str:
        if self.severity == "high":
            return f"Breaking change detected across {self.changed_nodes} changed model(s)"
        if self.changed_nodes == 0:
            return "No dbt models changed"
        return f"{self.changed_nodes} model(s) reviewed, no breaking change found"


def source_manifest(
    client: GitHubClient, pr: PullRequestRef, local_path: str | None = None
) -> tuple[Lineage | None, str, list[str]]:
    """Find a manifest, in order of trustworthiness. Returns (lineage, source, warnings).

    Order matters and is worth stating: a CI artifact from the base branch is built by
    `dbt compile` on the real project and is the only source that is both current and
    complete. A committed manifest is a fallback that is usually stale — it is whatever
    someone last remembered to check in — so it is used, and the comment says so.
    """
    warnings: list[str] = []

    if local_path:
        try:
            return Lineage.from_path(local_path), f"local:{local_path}", warnings
        except ManifestError as exc:
            warnings.append(f"Local manifest unusable: {exc}")

    try:
        artifact = client.find_manifest_artifact(pr)
    except GitHubError as exc:
        artifact = None
        warnings.append(f"Could not list CI artifacts: {exc}")

    if artifact is not None:
        # Downloading and unzipping a workflow artifact is day-9 work; recording that we
        # found one keeps the sourcing decision auditable in the meantime.
        warnings.append(
            f"CI artifact `{artifact['name']}` found on `{pr.base_ref}` but artifact "
            f"download is not yet implemented; falling back to a committed manifest."
        )

    for path in DEFAULT_MANIFEST_PATHS:
        raw = client.fetch_file(pr, path, ref=pr.base_ref)
        if not raw:
            continue
        try:
            import json

            lineage = Lineage.from_manifest(json.loads(raw))
        except (ManifestError, ValueError) as exc:
            warnings.append(f"Committed manifest at `{path}` is unusable: {exc}")
            continue
        warnings.append(
            f"Using the committed manifest at `{path}` on `{pr.base_ref}`. If it predates "
            f"recent model changes, blast radius is under-reported — publish a "
            f"`manifest` artifact from CI for an accurate graph."
        )
        return lineage, f"committed:{path}", warnings

    warnings.append(
        "No manifest found. Commit `target/manifest.json` on the base branch, or publish "
        "a `manifest` artifact from a CI workflow, then re-run."
    )
    return None, "none", warnings


def review_pull_request(
    client: GitHubClient,
    pr: PullRequestRef,
    *,
    local_manifest: str | None = None,
    use_agent: bool = True,
    changed_at: datetime | None = None,
) -> ReviewOutcome:
    started = time.monotonic()
    lineage, manifest_source, warnings = source_manifest(client, pr, local_manifest)

    if lineage is None:
        return ReviewOutcome(
            comment=_render_comment(
                body="Could not review this PR: no dbt manifest was available.",
                warnings=warnings,
                footer=_footer(manifest_source, 0.0, time.monotonic() - started, False),
            ),
            manifest_source=manifest_source,
            warnings=warnings,
        )

    try:
        diff_text = client.fetch_diff(pr)
    except GitHubError as exc:
        warnings.append(f"Could not fetch the PR diff: {exc}")
        return ReviewOutcome(
            comment=_render_comment(
                body="Could not review this PR: the diff could not be fetched.",
                warnings=warnings,
                footer=_footer(manifest_source, 0.0, time.monotonic() - started, False),
            ),
            manifest_source=manifest_source,
            warnings=warnings,
        )

    changes, unresolved = resolve_changes(diff_text, lineage)
    assessments = build_assessments(changes, lineage)

    if staleness := lineage.staleness_warning(changed_at):
        warnings.append(staleness)

    body = render_markdown(assessments, unresolved)
    cost = 0.0
    agent_ran = False

    if use_agent and assessments and os.environ.get("OPENAI_API_KEY"):
        from .agent import ReviewerAgent
        from .retrieval import PolicyPack

        pack = None
        try:
            pack = PolicyPack.load()
            pack.load_cache()
        except (FileNotFoundError, ValueError):
            pass

        result = ReviewerAgent(lineage, pack).review(assessments)
        agent_ran = result.ran
        cost = _agent_cost(result)
        body = body + "\n" + render_agent_findings(result)
    elif use_agent and assessments:
        warnings.append(
            "Reviewer agent skipped: OPENAI_API_KEY is not configured on the server. "
            "Structural analysis above is unaffected."
        )

    severity = assessments[0].severity if assessments else "low"
    latency = time.monotonic() - started

    return ReviewOutcome(
        comment=_render_comment(
            body=body,
            warnings=warnings,
            footer=_footer(manifest_source, cost, latency, agent_ran),
        ),
        severity=severity,
        manifest_source=manifest_source,
        warnings=warnings,
        agent_ran=agent_ran,
        cost_usd=cost,
        latency_s=latency,
        changed_nodes=len(changes),
    )


def _agent_cost(result) -> float:
    """Priced from `pricing.py`, which the eval harness also imports, so a cost quoted
    in a PR comment and a cost quoted in the eval report cannot disagree."""
    return cost_usd(result.input_tokens, result.output_tokens)


def _footer(manifest_source: str, cost: float, latency: float, agent_ran: bool) -> str:
    """Cost and latency in the footer: a reviewer deciding whether to keep this bot
    installed should not have to ask what it costs to run."""
    parts = [
        f"manifest: `{manifest_source}`",
        f"agent: {'yes' if agent_ran else 'no'}",
        f"{latency:.1f}s",
    ]
    if cost:
        parts.append(f"${cost:.4f}")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"_dbt-sentinel · {' · '.join(parts)} · {stamp}_"


def _render_comment(body: str, warnings: list[str], footer: str) -> str:
    sections = [COMMENT_MARKER, "", body.rstrip(), ""]
    if warnings:
        sections.append("<details><summary>⚠️ Warnings and caveats</summary>")
        sections.append("")
        sections.extend(f"- {w}" for w in warnings)
        sections.append("")
        sections.append("</details>")
        sections.append("")
    sections.append("---")
    sections.append(footer)
    return "\n".join(sections)
