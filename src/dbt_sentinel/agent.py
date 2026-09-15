"""Reviewer agent: Claude with tool-calling, returning validated structured findings.

Three invariants, in priority order. They matter more than review quality today.

1. **The LLM never writes the final comment.** It returns `Finding` objects that are
   schema-validated before anything is rendered. Rendering is template code in
   `report.py`. A model that emits Markdown directly can emit anything at all into a
   comment on someone's PR — a link, an instruction, a fabricated severity badge.

2. **Invalid or failed output degrades to deterministic-only, visibly.** One retry with
   the validation error fed back, then fall back and say so in the comment. Silence
   about degradation is worse than degradation: a reader who cannot tell the agent ran
   assumes it did.

3. **Lookups are tools, not context.** Lineage, policies and columns are answered
   deterministically and handed over on request. The manifest is never pasted into a
   prompt — reach is a BFS, and asking a model to traverse a graph it was shown is
   slower, costlier and less correct. Design rule 1.

The agent's job is the judgment that structure cannot answer: whether a `left join`
becoming `inner join` changes the grain, whether a contract edit widens or narrows.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from .lineage import Lineage
from .models import ChangedNode
from .report import Assessment
from .retrieval import PolicyPack, summarise_change

# Pinned rather than an alias: an alias silently changes the model under the eval
# numbers, and day 6 compares against a fixed baseline.
DEFAULT_MODEL = "claude-sonnet-5"
MAX_TOKENS = 2048
DEFAULT_TIMEOUT_S = 60.0
MAX_TOOL_ROUNDS = 6

Severity = Literal["low", "medium", "high"]


# ---------- the LLM boundary: Pydantic here and nowhere else ----------


class Finding(BaseModel):
    """One reviewable finding. Every field is rendered by template code, never echoed raw
    into Markdown structure."""

    rule_id: str = Field(min_length=1, max_length=64)
    severity: Severity
    model: str = Field(min_length=1, max_length=128)
    explanation: str = Field(min_length=1, max_length=1200)
    suggested_fix: str = Field(min_length=1, max_length=1200)

    @field_validator("rule_id", "model")
    @classmethod
    def _no_markdown_injection(cls, value: str) -> str:
        """These two land inside backticks in the rendered comment. A backtick or a
        newline would break out of the code span and let the model control layout."""
        if any(ch in value for ch in "`\n\r|"):
            raise ValueError("must not contain backticks, pipes or newlines")
        return value.strip()


class FindingsResponse(BaseModel):
    findings: list[Finding] = Field(default_factory=list, max_length=25)


@dataclass
class AgentResult:
    """What the renderer consumes. `degraded` is never silently true."""

    findings: list[Finding] = field(default_factory=list)
    degraded: bool = False
    degradation_reason: str | None = None
    rounds: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0
    validation_errors: list[str] = field(default_factory=list)

    @property
    def ran(self) -> bool:
        return not self.degraded


# ---------- tools: deterministic lookups, exposed on request ----------


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "get_lineage",
        "description": (
            "Downstream nodes of a dbt model, with depth and kind. Authoritative: computed "
            "by graph traversal over the manifest, not inferred. Use this instead of "
            "guessing what depends on a model."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "Model name, e.g. stg_orders"}
            },
            "required": ["model"],
        },
    },
    {
        "name": "get_policies",
        "description": (
            "Governance rules that apply to a change, retrieved from the policy pack. "
            "Returns rule_id, severity, description and guidance. Cite rule_ids from here; "
            "do not invent them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "change_summary": {
                    "type": "string",
                    "description": "What changed, in a few words, e.g. 'removed column customer_id'",
                }
            },
            "required": ["change_summary"],
        },
    },
    {
        "name": "get_columns",
        "description": (
            "Declared columns and tests for a model, from the manifest. Use to check "
            "whether a column exists or is tested before asserting either."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"model": {"type": "string"}},
            "required": ["model"],
        },
    },
]


class ToolBox:
    """Deterministic answers to the agent's lookups.

    Every method returns JSON-serialisable data and never raises: a tool error becomes a
    message the model can recover from, because a crashed tool call would take the whole
    review down for a question that was optional.
    """

    def __init__(self, lineage: Lineage, pack: PolicyPack | None = None):
        self._lineage = lineage
        self._pack = pack
        self.calls: list[tuple[str, dict]] = []

    def _find_node_id(self, model: str) -> str | None:
        for node in self._lineage._nodes.values():  # noqa: SLF001 - same package
            if node.name == model:
                return node.unique_id
        return None

    def get_lineage(self, model: str) -> dict:
        uid = self._find_node_id(model)
        if uid is None:
            return {"error": f"no node named {model!r} in the manifest"}
        blast = self._lineage.blast_radius(uid)
        return {
            "model": model,
            "downstream_count": blast.size,
            "downstream": [
                {
                    "name": n.name,
                    "kind": n.kind.value,
                    "depth": blast.depth_by_id[n.unique_id],
                    "is_contracted": n.is_contracted,
                    "access": n.access,
                }
                for n in blast.downstream
            ],
            "exposures": [{"name": n.name, "owner": n.owner} for n in blast.exposures],
            "contracted": [n.name for n in blast.contracted],
        }

    def get_policies(self, change_summary: str, changed: ChangedNode | None = None) -> dict:
        if self._pack is None:
            return {"error": "no policy pack loaded"}
        if changed is None:
            return {"error": "policy retrieval needs the change context"}
        result = self._pack.retrieve(changed, change_summary or summarise_change(changed))
        return {
            "rules": [
                {
                    "rule_id": r.rule_id,
                    "severity": r.rule.severity,
                    "title": r.rule.title,
                    "description": r.rule.description,
                    "guidance": r.rule.guidance,
                    "score": round(r.score, 3),
                }
                for r in result.retrieved
            ]
        }

    def get_columns(self, model: str) -> dict:
        uid = self._find_node_id(model)
        if uid is None:
            return {"error": f"no node named {model!r} in the manifest"}
        node = self._lineage.get(uid)
        assert node is not None
        return {
            "model": model,
            "columns": list(node.columns),
            "materialization": node.materialization,
            "is_contracted": node.is_contracted,
            "access": node.access,
        }

    def dispatch(self, name: str, payload: dict, changed: ChangedNode | None) -> dict:
        self.calls.append((name, payload))
        try:
            if name == "get_lineage":
                return self.get_lineage(payload.get("model", ""))
            if name == "get_policies":
                return self.get_policies(payload.get("change_summary", ""), changed)
            if name == "get_columns":
                return self.get_columns(payload.get("model", ""))
            return {"error": f"unknown tool {name!r}"}
        except Exception as exc:  # noqa: BLE001 - a tool must not kill the review
            return {"error": f"{type(exc).__name__}: {exc}"}


# ---------- prompt ----------

SYSTEM_PROMPT = """\
You review dbt pull requests for changes that will break downstream consumers or violate \
data governance policy.

You are given the deterministic analysis already computed: which models changed, their \
blast radius, and the policy rules retrieved for each. Your job is the judgment that \
graph traversal cannot answer — whether a SQL change alters grain, whether a contract \
edit widens or narrows, whether an incremental model needs a full refresh.

Rules of engagement:

- Reach amplifies risk, it does not create it. Nearly every staging model sits upstream \
of a dashboard. Only a structural change — a removed or renamed column, a deletion, a \
grain change, a contract edit — justifies medium or high. A comment, a whitespace edit, \
an added test or an additive column is low no matter how many consumers it has.
- An additive change is not a breaking change. SQL ignores columns it does not select.
- Direction matters on types: widening (integer to bigint) is safe, narrowing is not.
- Cite only rule_ids returned by get_policies. Do not invent rule_ids.
- Use the tools for facts. Do not guess what is downstream of a model or which columns \
it declares.
- Report nothing rather than something speculative. An empty findings list is a valid \
and useful answer for a routine PR.

Return your findings by calling the `submit_findings` tool exactly once, with one entry \
per distinct issue. Do not write prose outside the tool call — your text is discarded, \
only the structured findings are used.\
"""

SUBMIT_TOOL: dict[str, Any] = {
    "name": "submit_findings",
    "description": (
        "Submit the final structured findings. Call exactly once. An empty list is valid "
        "when the PR is routine."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "maxItems": 25,
                "items": {
                    "type": "object",
                    "properties": {
                        "rule_id": {
                            "type": "string",
                            "description": "A rule_id from get_policies, or 'structural' "
                            "for a breaking change no rule covers.",
                        },
                        "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                        "model": {"type": "string", "description": "The dbt model name."},
                        "explanation": {
                            "type": "string",
                            "description": "What breaks and for whom. One or two sentences.",
                        },
                        "suggested_fix": {
                            "type": "string",
                            "description": "What the author should do next. Actionable.",
                        },
                    },
                    "required": [
                        "rule_id",
                        "severity",
                        "model",
                        "explanation",
                        "suggested_fix",
                    ],
                },
            }
        },
        "required": ["findings"],
    },
}


def build_user_prompt(assessments: list[Assessment], pack: PolicyPack | None) -> str:
    """The deterministic analysis, as text. Note what is NOT here: the manifest, the full
    DAG, and the compiled SQL. Those are tool calls."""
    blocks: list[str] = []
    for a in assessments:
        changed = a.changed
        node = changed.node
        lines = [
            f"## Changed: {node.name} ({node.kind.value})",
            f"- file: {changed.file.path}",
            f"- change type: {changed.change_type.value}",
            f"- materialization: {node.materialization or 'n/a'}"
            f" | contract enforced: {node.is_contracted} | access: {node.access or 'n/a'}",
            f"- downstream nodes: {a.blast.size}"
            f" (exposures: {len(a.blast.exposures)}, contracted: {len(a.blast.contracted)})",
            f"- deterministic severity: {a.severity}",
            f"- added columns: {list(changed.added_columns) or 'none'}",
            f"- removed columns: {list(changed.removed_columns) or 'none'}",
            f"- semantic change: {changed.has_semantic_change}",
        ]
        if pack is not None:
            result = pack.retrieve(changed, summarise_change(changed))
            if result.retrieved:
                lines.append("- retrieved policy rules:")
                lines += [
                    f"    - {r.rule_id} ({r.rule.severity}): {r.rule.title}"
                    for r in result.retrieved
                ]
            else:
                lines.append("- retrieved policy rules: none matched")

        diff_lines = list(changed.file.added_lines) + list(changed.file.removed_lines)
        if diff_lines:
            lines.append("- changed lines:")
            lines += [f"    {line}" for line in diff_lines[:40]]
        blocks.append("\n".join(lines))

    return (
        "Review this dbt pull request.\n\n"
        + "\n\n".join(blocks)
        + "\n\nCall submit_findings once with your findings."
    )


# ---------- the loop ----------


class ReviewerAgent:
    def __init__(
        self,
        lineage: Lineage,
        pack: PolicyPack | None = None,
        client: Any = None,
        model: str = DEFAULT_MODEL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ):
        self._lineage = lineage
        self._pack = pack
        self._model = model
        self._timeout_s = timeout_s
        self._client = client  # injectable so tests never touch the network

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Export it, or run without --agent for "
                "deterministic-only output."
            )
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on install state
            raise RuntimeError(
                "the anthropic package is not installed. Run: pip install -e '.[agent]'"
            ) from exc
        self._client = anthropic.Anthropic(timeout=self._timeout_s)
        return self._client

    def review(self, assessments: list[Assessment]) -> AgentResult:
        """Never raises. Every failure path returns a degraded result with a reason."""
        started = time.monotonic()
        if not assessments:
            return AgentResult(findings=[], rounds=0)

        try:
            client = self._ensure_client()
        except RuntimeError as exc:
            return AgentResult(
                degraded=True, degradation_reason=str(exc), latency_s=time.monotonic() - started
            )

        toolbox = ToolBox(self._lineage, self._pack)
        by_name = {a.changed.node.name: a.changed for a in assessments}
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": build_user_prompt(assessments, self._pack)}
        ]

        result = AgentResult()
        retried = False

        for round_index in range(MAX_TOOL_ROUNDS):
            result.rounds = round_index + 1
            try:
                response = client.messages.create(
                    model=self._model,
                    max_tokens=MAX_TOKENS,
                    system=SYSTEM_PROMPT,
                    tools=[*TOOL_SCHEMAS, SUBMIT_TOOL],
                    messages=messages,
                )
            except Exception as exc:  # noqa: BLE001 - timeouts, rate limits, 5xx
                result.degraded = True
                result.degradation_reason = f"{type(exc).__name__}: {exc}"
                result.latency_s = time.monotonic() - started
                return result

            usage = getattr(response, "usage", None)
            if usage is not None:
                result.input_tokens += getattr(usage, "input_tokens", 0) or 0
                result.output_tokens += getattr(usage, "output_tokens", 0) or 0

            blocks = list(getattr(response, "content", []) or [])
            tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]

            submit = next((b for b in tool_uses if b.name == "submit_findings"), None)
            if submit is not None:
                try:
                    parsed = FindingsResponse.model_validate(submit.input)
                except ValidationError as exc:
                    result.validation_errors.append(str(exc))
                    if retried:
                        # Rule 2: one retry, then degrade rather than loop on a model
                        # that cannot produce the schema.
                        result.degraded = True
                        result.degradation_reason = (
                            "LLM returned findings that failed schema validation twice"
                        )
                        result.latency_s = time.monotonic() - started
                        return result
                    retried = True
                    messages.append({"role": "assistant", "content": blocks})
                    messages.append(
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": submit.id,
                                    "is_error": True,
                                    "content": (
                                        f"Schema validation failed: {exc}. "
                                        f"Call submit_findings again with valid fields."
                                    ),
                                }
                            ],
                        }
                    )
                    continue

                result.findings = parsed.findings
                result.latency_s = time.monotonic() - started
                return result

            if not tool_uses:
                # No tool call and no submission: the model answered in prose, which we
                # discard by design. Nothing to render.
                result.degraded = True
                result.degradation_reason = (
                    "LLM returned prose instead of calling submit_findings"
                )
                result.latency_s = time.monotonic() - started
                return result

            messages.append({"role": "assistant", "content": blocks})
            tool_results = []
            for block in tool_uses:
                payload = dict(block.input or {})
                changed = by_name.get(payload.get("model") or "") or next(
                    iter(by_name.values()), None
                )
                output = toolbox.dispatch(block.name, payload, changed)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(output),
                    }
                )
            messages.append({"role": "user", "content": tool_results})

        result.degraded = True
        result.degradation_reason = (
            f"LLM did not submit findings within {MAX_TOOL_ROUNDS} tool rounds"
        )
        result.latency_s = time.monotonic() - started
        return result
