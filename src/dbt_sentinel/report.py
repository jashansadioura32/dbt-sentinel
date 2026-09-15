"""Deterministic risk scoring and rendering.

No LLM here. If a rule can be decided by graph structure alone, it should be —
the model is reserved for judgment calls that structure cannot answer.
"""

from __future__ import annotations

from dataclasses import dataclass

from .lineage import Lineage
from .models import BlastRadius, ChangedNode, ChangeType, NodeKind

MAX_DIAGRAM_NODES = 25


@dataclass
class Assessment:
    changed: ChangedNode
    blast: BlastRadius
    severity: str
    reasons: list[str]


def assess(changed: ChangedNode, blast: BlastRadius) -> Assessment:
    """Severity = what changed, amplified by what it reaches.

    Reach alone is never a trigger. Almost every staging model in a real project sits
    upstream of a dashboard; if proximity to an exposure were enough to score HIGH, the
    agent would flag every PR and be ignored within a week. The trigger has to be a
    structural change, and reach decides how loud to be about it.
    """
    reasons: list[str] = []
    severity = "low"

    def raise_to(level: str) -> None:
        nonlocal severity
        order = {"low": 0, "medium": 1, "high": 2}
        if order[level] > order[severity]:
            severity = level

    if not changed.has_semantic_change:
        return Assessment(
            changed=changed,
            blast=blast,
            severity="low",
            reasons=["Comment or whitespace only — no semantic change"],
        )

    if changed.removed_columns:
        cols = ", ".join(changed.removed_columns)
        if blast.size:
            raise_to("high")
            reasons.append(f"Removed column(s) `{cols}` with {blast.size} downstream node(s)")
        else:
            raise_to("medium")
            reasons.append(f"Removed column(s) `{cols}` with no known consumers")

    if changed.change_type is ChangeType.DELETED and blast.size:
        raise_to("high")
        reasons.append(f"Model deleted while {blast.size} node(s) still depend on it")

    # Reach is reported either way, but only escalates a structural change.
    if blast.contracted:
        names = ", ".join(n.name for n in blast.contracted[:3])
        if changed.is_structural:
            raise_to("high")
        reasons.append(f"Reaches {len(blast.contracted)} contracted model(s): {names}")

    if blast.exposures:
        owners = {n.owner for n in blast.exposures if n.owner}
        owner_note = f" (owners: {', '.join(sorted(owners))})" if owners else ""
        names = ", ".join(n.name for n in blast.exposures[:3])
        if changed.is_structural:
            raise_to("high")
        reasons.append(f"Reaches {len(blast.exposures)} exposure(s): {names}{owner_note}")

    if blast.public and not blast.contracted:
        if changed.is_structural:
            raise_to("medium")
        reasons.append(f"Reaches {len(blast.public)} model(s) with public access")

    if changed.node.materialization == "incremental":
        raise_to("medium")
        reasons.append("Incremental model — change may require a full refresh to take effect")

    if not reasons:
        reasons.append(f"Logic change with {blast.size} downstream node(s), none contracted")

    return Assessment(changed=changed, blast=blast, severity=severity, reasons=reasons)


def build_assessments(changes: list[ChangedNode], lineage: Lineage) -> list[Assessment]:
    out = [assess(c, lineage.blast_radius(c.node.unique_id)) for c in changes]
    order = {"high": 0, "medium": 1, "low": 2}
    out.sort(key=lambda a: (order[a.severity], -a.blast.size))
    return out


def render_markdown(assessments: list[Assessment], unresolved: list) -> str:
    if not assessments and not unresolved:
        return "No dbt nodes changed in this PR."

    lines = ["## Blast radius", ""]
    icons = {"high": "🔴", "medium": "🟠", "low": "🟢"}

    for a in assessments:
        lines.append(
            f"### {icons[a.severity]} `{a.changed.node.name}` "
            f"— {a.severity.upper()} · {a.blast.size} downstream"
        )
        lines.extend(f"- {reason}" for reason in a.reasons)
        if a.blast.downstream:
            preview = ", ".join(f"`{n.name}`" for n in a.blast.downstream[:6])
            more = f" +{a.blast.size - 6} more" if a.blast.size > 6 else ""
            lines.append(f"- Downstream: {preview}{more}")
        lines.append("")

    if unresolved:
        lines.append("### ⚠️ Not resolved to a manifest node")
        lines.append(
            "These files changed but could not be mapped. The manifest may be stale, "
            "or they are macros/tests outside the model graph."
        )
        lines.extend(f"- `{f.path}`" for f in unresolved)
        lines.append("")

    return "\n".join(lines)


def _flatten(text: str, limit: int = 600) -> str:
    """Collapse model-supplied text to a single line before it enters Markdown.

    The LLM's strings are data, not markup. A newline followed by `##` or `-` would
    create block structure the template did not author, letting the model forge headings
    and bullets inside someone's PR comment.
    """
    collapsed = " ".join(str(text).split())
    return collapsed[:limit].rstrip() + ("…" if len(collapsed) > limit else "")


def render_agent_findings(result) -> str:
    """Render `AgentResult` deterministically. The LLM never writes this comment.

    Takes the result rather than the findings list so degradation can never be silent:
    if the agent failed, that is stated here with its reason, because a reader who
    cannot tell the agent ran will assume it did.
    """
    icons = {"high": "🔴", "medium": "🟠", "low": "🟢"}
    lines = ["## Reviewer findings", ""]

    if getattr(result, "degraded", False):
        reason = _flatten(result.degradation_reason or "unknown error", limit=300)
        lines += [
            f"> ⚠️ **Agent unavailable — deterministic analysis only.** {reason}",
            "",
            "The blast radius above is unaffected: it is computed by graph traversal and "
            "does not depend on the model.",
            "",
        ]
        return "\n".join(lines)

    if not result.findings:
        lines += ["No policy or correctness findings. Structural analysis above stands.", ""]
        return "\n".join(lines)

    for finding in result.findings:
        icon = icons.get(finding.severity, "⚪")
        lines.append(
            f"### {icon} `{finding.model}` — {finding.severity.upper()} "
            f"· `{finding.rule_id}`"
        )
        lines.append(f"- {_flatten(finding.explanation)}")
        lines.append(f"- **Fix:** {_flatten(finding.suggested_fix)}")
        lines.append("")

    return "\n".join(lines)


def render_mermaid(assessment: Assessment) -> str:
    """Mermaid flowchart of one change's blast radius. Truncates wide graphs —
    a 200-node diagram communicates nothing."""
    blast = assessment.blast
    root_id = _safe(blast.root.unique_id)
    lines = ["flowchart LR", f'    {root_id}["{blast.root.name}"]:::changed']

    shown = blast.downstream[:MAX_DIAGRAM_NODES]
    shown_ids = {n.unique_id for n in shown}

    for node in shown:
        node_id = _safe(node.unique_id)
        style = "exposure" if node.kind is NodeKind.EXPOSURE else (
            "contract" if node.is_contracted else "normal"
        )
        shape = f'{node_id}(["{node.name}"])' if node.kind is NodeKind.EXPOSURE else (
            f'{node_id}["{node.name}"]'
        )
        lines.append(f"    {shape}:::{style}")

    for node in shown:
        for parent in node.depends_on:
            if parent == blast.root.unique_id or parent in shown_ids:
                lines.append(f"    {_safe(parent)} --> {_safe(node.unique_id)}")

    hidden = blast.size - len(shown)
    if hidden > 0:
        lines.append(f'    more["+{hidden} more downstream"]:::normal')
        lines.append(f"    {root_id} -.-> more")

    lines.extend(
        [
            "    classDef changed fill:#fee2e2,stroke:#dc2626,stroke-width:2px",
            "    classDef exposure fill:#fef3c7,stroke:#d97706",
            "    classDef contract fill:#e0e7ff,stroke:#4f46e5",
            "    classDef normal fill:#f3f4f6,stroke:#9ca3af",
        ]
    )
    return "\n".join(lines)


def _safe(unique_id: str) -> str:
    return unique_id.replace(".", "_").replace("-", "_")
