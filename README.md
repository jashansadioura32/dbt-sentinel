# dbt-sentinel

An AI agent that reviews dbt pull requests: maps the diff to changed models, computes
the downstream blast radius, and flags changes that will break something.

**Status: day 5 of a 2-week public build.** Deterministic core, policy retrieval and
agent v0 work. Measured baselines are published in [evals/BASELINE.md](evals/BASELINE.md);
they are honest rather than flattering.

## Non-goals

- Not a data catalog, not a lineage UI, not a test generator
- Single warehouse (Snowflake-flavoured SQL assumptions), single repo
- No web UI — it comments on PRs, that's the whole surface
- No column-level lineage at v1 — model-level reach only

## Install

```bash
git clone https://github.com/<you>/dbt-sentinel.git
cd dbt-sentinel
pip install -e .            # core + policy retrieval
pip install -e ".[dev]"     # adds pytest
pip install -e ".[agent]"   # adds the reviewer agent (anthropic, pydantic)
pip install -e ".[server]"  # adds the webhook receiver (fastapi, uvicorn)
```

Only `pyyaml` is a runtime dependency: the policy pack is YAML, and retrieval is part of
the deterministic path. Retrieval itself is stdlib-only, so the published eval numbers
reproduce offline with no API key.

## What works today

```bash
python -m dbt_sentinel \
  --manifest target/manifest.json \
  --diff pr.diff \
  --mermaid --explain --fail-on high
```

Without an editable install, prefix with `PYTHONPATH=src`.

**Deterministic core (day 2)**
- Parses `manifest.json` (schema v7–v14) into a normalised node graph
- Resolves changed files to nodes via both `original_file_path` and `patch_path`,
  normalising separators so a Windows-compiled manifest still matches a git diff
- Extracts added/removed columns, scoped to the correct model inside a shared schema.yml
- Walks the DAG for blast radius, excluding test nodes and following exposures
- Scores severity and renders Markdown + a Mermaid diagram
- Exits non-zero at a severity threshold, so it works as a CI gate

**Policy retrieval (day 4)** — `--explain`
- 14 governance rules in [policies/](policies/), hybrid retrieval: a deterministic
  prefilter on `applies_to`, then TF-IDF ranking of what survives
- Only the rule pack is vectorised — never the manifest, graph or SQL
- `--explain` shows what was retrieved, the score, and how many rules the prefilter
  eliminated

**Reviewer agent (day 5)** — `--agent`
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
python -m dbt_sentinel --manifest target/manifest.json --diff pr.diff --agent
```
- Claude with tool-calling: `get_lineage`, `get_policies`, `get_columns`. Lookups are
  tools, so the manifest is never pasted into a prompt
- Returns schema-validated `Finding` objects; **the LLM never writes the comment**
- Any failure — no key, timeout, rate limit, invalid schema twice, prose instead of a
  tool call — degrades to deterministic-only with a visible note

**Webhook skeleton (day 5, not yet wired)** — `dbt_sentinel/webhook.py`
```bash
export GITHUB_WEBHOOK_SECRET=...
uvicorn dbt_sentinel.webhook:app --port 8000
```
Verifies `X-Hub-Signature-256` against the raw body in constant time, then logs the
payload. It does not fetch diffs or post comments yet — day 8 wires that, and says so in
its own response rather than returning a silent 200.

## Layout

```
src/dbt_sentinel/
  models.py     domain types (Node, ChangedFile, ChangedNode, BlastRadius)
  lineage.py    manifest parsing, node graph, BFS blast radius
  diff.py       unified diff parsing, file->node resolution, column extraction
  report.py     severity scoring, Markdown + Mermaid rendering
  cli.py        entrypoint with --fail-on exit codes
tests/
  test_day2.py  11 regression tests, one per bug found
  manifest.json synthetic fixture
```

## Design decisions worth defending

**Lineage is graph traversal, not retrieval.** The manifest ships `child_map`. Embedding
a dependency graph and asking a model to reason over it would be slower, costlier, and
less correct than a BFS. The LLM's job (day 5) is judgment, not lookup.

**Reach amplifies risk, it does not create it.** Nearly every staging model sits upstream
of a dashboard. An agent that scores HIGH on that basis flags every PR and gets muted
within a week. Severity is triggered by structural change — a removed column, a deletion,
a rename — and reach decides how loud to be.

**Unresolvable files are surfaced, never dropped.** A changed macro or a model missing
from a stale manifest is exactly where under-reporting is dangerous. They appear in the
output as an explicit warning.

**Test nodes are excluded from traversal.** dbt tests are children of every model they
cover, so counting them makes a well-tested model look like it has twelve consumers.

## Known limitations

| Limitation | Impact | Planned fix |
|---|---|---|
| SQL column extraction is regex, not a parser | Misses `select *`, macro-generated columns, CTE aliases | Out of scope for v1 (see ROADMAP.md) |
| Macro changes don't resolve to models | Under-reports blast radius for macro edits | Warned, not resolved |
| Column-level lineage not tracked | Reports model reach, not which downstream model uses the dropped column | Post-2-week |
| Manifest assumed current | A stale manifest silently shrinks blast radius | Freshness check + warn (day 3) |

## Tests

```bash
python -m pytest tests/ -q
```

Every test in `test_day2.py` encodes a bug found during the build. Three were false
positives that would have made the agent untrustworthy.
