# dbt-sentinel — project context

Read this before any work. It is the contract for the whole project.

## What this is

An AI agent that reviews dbt pull requests. On every PR it maps the diff to changed
models, computes the downstream blast radius, retrieves the governance rules that apply,
and posts a structured review with severity, affected models, and a suggested fix.

This is a 2-week public portfolio build. The goal is a defensible artifact, not a
product. Optimise for correctness that can be proven, not for feature count.

## Non-goals — do not build these

- Web UI of any kind. PR comments are the entire surface, plus the local post-commit
  hook (`integrations/vscode/`), which is the same CLI writing Markdown to a file, not a UI.
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

Days 1-10 complete, plus a check layer (3 phases) and the day-7 iteration round.
217 tests pass.

- `models.py` — domain types (Node, ChangedFile, ChangedNode, BlastRadius)
- `lineage.py` — manifest parsing, node graph, BFS blast radius
- `diff.py` — unified diff parsing, file->node resolution, column extraction
- `report.py` — severity scoring, Markdown + Mermaid rendering
- `retrieval.py` — hybrid keyword + TF-IDF retrieval over the policy pack
- `agent.py` — reviewer agent, tool-calling, structured output, degradation paths
- `checks.py` — four deterministic lint checks, a peer of the blast radius
- `github.py` / `webhook.py` / `pipeline.py` — the GitHub App
- `cli.py` — entrypoint with `--fail-on` exit codes; `--since REF` diffs via git
- `integrations/vscode/` — post-commit hook + task for local review (`docs/LOCAL.md`)
- `evals/` — 30 labelled severity fixtures + 8 check fixtures, four harnesses
- `docs/` — PRD, architecture + ADRs, eval report, checks spec, deployment

**Published metrics** (day 7, `evals/RESULTS_V2.md`): precision 0.909, recall 0.588,
FPR 0.000, 0 fixtures silently dropped. Retrieval precision@3 0.636 / recall@3 0.778.
CI ratchets these floors — `.github/scripts/check_baselines.py` fails the build on a
regression, and moving a floor requires updating the published doc in the same commit.

### The two things that remain

1. **The agent has never run.** Every agent test injects a fake client. `evals/compare.py`
   is written and gated, but the OpenAI account has no credits, so `RESULTS_V1.md` does
   not exist. Its plumbing is proven; its review quality is entirely unknown.
2. **Never deployed.** The GitHub App is wired and tested against a fake client. No real
   PR has received a comment.

### Regression tests that must keep passing

Day 2 (three false positives):
- Comment-only change must not score above LOW
- A shared schema.yml must not flag models that merely share a column name
- A model changed in both .sql and .yml must be reported once

Day 7 (`tests/test_day7.py`, the two deferred failure modes):
- A YAML edit matching a real node must never resolve to nothing — an unattributable
  removed column is an explicit medium warning, never silence and never a guessed owner
- An added column is not structural; `p10` and `b01` must not converge
- `evals/compare.py` must exit 2 and write no file when the agent arm never ran, keyed
  off token spend rather than the errored flag

## Known limitations — documented, not hidden

| Limitation | Status |
|---|---|
| Regex column extraction (misses `select *`, CTE aliases, macro-generated columns) | Out of scope for v1 |
| Macro changes don't resolve to models | Warned, not resolved |
| A removed column in a shared schema.yml has no provable owner | Explicit medium uncertainty; sole remaining FP (`s03`) |
| Manifest assumed current | Freshness warning implemented |

## Definition of done for the project

- Deployed GitHub App that comments on a real PR in a public repo
- Eval suite of 30 labelled fixtures with published precision / recall / FPR
- An honest error analysis naming the top 3 failure modes
- PRD, architecture doc with ADRs, and an eval report

Published metrics that are mediocre and honest beat metrics that are impressive and
unverifiable. If recall is 70%, publish 70% and explain why.
