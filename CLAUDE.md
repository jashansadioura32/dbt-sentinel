# dbt-sentinel — project context

Read this before any work. It is the contract for the whole project.

## What this is

An AI agent that reviews dbt pull requests. On every PR it maps the diff to changed
models, computes the downstream blast radius, retrieves the governance rules that apply,
and posts a structured review with severity, affected models, and a suggested fix.

This is a 2-week public portfolio build. The goal is a defensible artifact, not a
product. Optimise for correctness that can be proven, not for feature count.

## Non-goals — do not build these

- Web UI of any kind. PR comments are the entire surface.
- Multi-warehouse support. Snowflake-flavoured SQL assumptions are fine.
- Data catalog, lineage browser, or test generator.
- Column-level lineage. Model-level reach only.
- Auth, multi-tenancy, billing, or anything resembling SaaS scaffolding.
- Agent frameworks that hide control flow. Plain orchestration only.

If you think of something valuable that is out of scope, append it to `ROADMAP.md`
and move on. Do not build it.

## Architecture

```
GitHub webhook
  -> diff parser          (deterministic)
  -> manifest loader      (deterministic)
  -> lineage traversal    (deterministic)
  -> policy retrieval     (hybrid: keyword + embedding)
  -> reviewer agent       (Claude, tool-calling, structured output)
  -> deterministic render -> GitHub review comment + status check
```

## Design rules — these are not negotiable

1. **Deterministic first.** If a question can be answered by graph traversal or string
   parsing, answer it that way. The manifest ships `child_map`; never embed a dependency
   graph and ask a model to traverse it. The LLM is for judgment, not lookup.

2. **Reach amplifies risk, it does not create it.** Nearly every staging model sits
   upstream of a dashboard. Severity must be triggered by a structural change (removed
   column, deletion, rename, contract edit). Reach only decides how loud to be. An agent
   that flags every PR gets muted within a week.

3. **The LLM never writes the final comment.** It returns validated structured findings;
   rendering is deterministic template code.

4. **Unresolvable input is surfaced, never dropped.** A changed macro or a model missing
   from a stale manifest is exactly where silence is dangerous. Emit an explicit warning.

5. **Degrade, don't crash.** If the LLM call fails, times out, or returns invalid schema,
   fall back to deterministic-only output and say so in the comment.

6. **Every bug found becomes a regression test.** No exceptions.

## Tech constraints

- Python 3.10+
- `src/` layout, package is `dbt_sentinel`
- Minimal dependencies. Justify every addition in the PR description.
  Currently allowed: `anthropic`, `pydantic`, `pyyaml`, `fastapi`, `uvicorn`, `pytest`.
- No `networkx`. No LangChain, LlamaIndex, or CrewAI.
- Type hints on all public functions. `from __future__ import annotations` at the top.
- Dataclasses for domain types, Pydantic only at the LLM boundary.

## Code style

- Readability over cleverness. No abstraction with fewer than three call sites.
- Comments explain *why*, never *what*. If a line needs a comment to say what it does,
  rewrite the line.
- Where a design decision is non-obvious, leave a short comment naming the failure mode
  it prevents. These comments are part of the portfolio value.
- Error messages tell the user what to do next, not just what went wrong.

## Current state

Days 1-2 complete:
- `models.py` — domain types (Node, ChangedFile, ChangedNode, BlastRadius)
- `lineage.py` — manifest parsing, node graph, BFS blast radius
- `diff.py` — unified diff parsing, file->node resolution, column extraction
- `report.py` — severity scoring, Markdown + Mermaid rendering
- `cli.py` — entrypoint with `--fail-on` exit codes
- `tests/test_day2.py` — 11 regression tests

Three false positives were found and fixed on day 2. The tests encoding them must
keep passing:
- Comment-only change must not score above LOW
- A shared schema.yml must not flag models that merely share a column name
- A model changed in both .sql and .yml must be reported once

## Known limitations — documented, not hidden

| Limitation | Planned |
|---|---|
| Regex column extraction (misses `select *`, CTE aliases, macro-generated columns) | Out of scope for v1 |
| Macro changes don't resolve to models | Warned, not resolved |
| Manifest assumed current | Freshness warning on day 3 |

## Definition of done for the project

- Deployed GitHub App that comments on a real PR in a public repo
- Eval suite of 30 labelled fixtures with published precision / recall / FPR
- An honest error analysis naming the top 3 failure modes
- PRD, architecture doc with ADRs, and an eval report

Published metrics that are mediocre and honest beat metrics that are impressive and
unverifiable. If recall is 70%, publish 70% and explain why.
