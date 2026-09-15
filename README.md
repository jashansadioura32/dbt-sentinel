# dbt-sentinel

An AI agent that reviews dbt pull requests: maps the diff to changed models, computes
the downstream blast radius, and flags changes that will break something.

**Status: day 2 of a 2-week public build.** The deterministic core works. No LLM yet.

## Non-goals

- Not a data catalog, not a lineage UI, not a test generator
- Single warehouse (Snowflake-flavoured SQL assumptions), single repo
- No web UI — it comments on PRs, that's the whole surface
- No column-level lineage at v1 — model-level reach only

## Install

```bash
git clone https://github.com/<you>/dbt-sentinel.git
cd dbt-sentinel
pip install -e .
```

The deterministic core has no third-party dependencies. `pip install -e ".[dev]"` adds
pytest; the `agent` and `server` extras arrive on days 4-5.

## What works today

```bash
python -m dbt_sentinel \
  --manifest target/manifest.json \
  --diff pr.diff \
  --mermaid --fail-on high
```

Without an editable install, prefix with `PYTHONPATH=src`.

- Parses `manifest.json` (schema v7–v14) into a normalised node graph
- Resolves changed files to nodes via both `original_file_path` and `patch_path`
- Extracts added/removed columns, scoped to the correct model inside a shared schema.yml
- Walks the DAG for blast radius, excluding test nodes and following exposures
- Scores severity and renders Markdown + a Mermaid diagram
- Exits non-zero at a severity threshold, so it works as a CI gate

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
