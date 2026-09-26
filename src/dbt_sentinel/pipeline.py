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

from .checks import run_checks
from .diff import parse_diff, resolve_changes
from .github import GitHubClient, GitHubError, PullRequestRef
from .lineage import Lineage, ManifestError
from .pricing import cost_usd
from .report import (
    build_assessments,
    render_agent_findings,
    render_checks,
    render_markdown,
    render_security,
)
from .security import scan_secrets

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
    has_unresolved: bool = False
    reviewed: bool = True
    secrets_found: int = 0

    @property
    def status_state(self) -> str:
        """HIGH blocks the merge, and so does an exposed secret; everything else passes.

        MEDIUM deliberately does not block. A gate that fires on judgment calls gets
        switched off within a week, and then the HIGH signal is gone too. A secret is not
        a judgment call: it's a pattern match, and it's compromised on push, not on merge.
        """
        return "failure" if self.severity == "high" or self.secrets_found else "success"

    @property
    def has_nothing_to_report(self) -> bool:
        """True when this PR touches no dbt node and nothing was left unresolved.

        Manifest-provenance warnings are excluded deliberately: "the committed manifest
        may be stale" says nothing about a PR that changed no model, and treating it as
        content would keep a review comment alive on every docs-only PR forever.

        Unresolved files are *not* excluded. A changed macro or a model missing from a
        stale manifest is exactly where silence is dangerous (design rule 4), so a
        review carrying one is never deleted.

        A review that never ran is never "nothing to report" — deleting the comment that
        explains why the manifest or diff could not be read would erase the only notice
        the user gets that the tool is broken.

        Derived from the counts rather than by matching the rendered text, so wording
        changes in report.py cannot silently turn deletion off.
        """
        # A secret in profiles.yml changes no dbt node, and deleting the only comment
        # that says "rotate this key" would be the worst silence available.
        return (
            self.reviewed
            and self.changed_nodes == 0
            and not self.has_unresolved
            and not self.secrets_found
        )

    @property
    def status_description(self) -> str:
        if self.secrets_found:
            return f"Exposed credential in this PR ({self.secrets_found}). Rotate it"
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
        # Secret scanning needs no manifest, so a repo without one still gets it. A
        # leaked key must not go unreported because the lineage half couldn't run.
        secrets = []
        try:
            secrets = scan_secrets(parse_diff(client.fetch_diff(pr)))
        except GitHubError:
            pass  # the manifest warning below is already the actionable message
        body = "Could not review this PR: no dbt manifest was available."
        if section := render_security(secrets):
            body = section + "\n" + body
        return ReviewOutcome(
            comment=_render_comment(
                body=body,
                warnings=warnings,
                footer=_footer(manifest_source, 0.0, time.monotonic() - started, False),
            ),
            manifest_source=manifest_source,
            warnings=warnings,
            reviewed=False,
            secrets_found=len(secrets),
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
            reviewed=False,
        )

    changes, unresolved = resolve_changes(diff_text, lineage)
    assessments = build_assessments(changes, lineage)

    if staleness := lineage.staleness_warning(changed_at):
        warnings.append(staleness)

    body = render_markdown(assessments, unresolved)

    # Before anything else in the comment: it's the one finding that needs action
    # whether or not the PR is ever merged.
    files = parse_diff(diff_text)
    secrets = scan_secrets(files)
    if section := render_security(secrets):
        body = section + "\n" + body

    # Deterministic checks render as a peer of the blast radius, never folded into it:
    # a lint finding has no reach, so it must not be amplified by one (design rule 2).
    # Note what is NOT touched below — `severity` still comes only from assessments, so
    # a check can never change the commit status.
    check_findings = run_checks(files, changes, lineage)
    if section := render_checks(check_findings):
        body = body + "\n" + section

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

        # The PR's version of each file, so the agent judges the new SQL rather than
        # the base branch's copy in the manifest.
        result = ReviewerAgent(
            lineage, pack, head_source=lambda path: client.fetch_file(pr, path, ref=pr.head_sha)
        ).review(assessments)
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
        has_unresolved=bool(unresolved),
        secrets_found=len(secrets),
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
