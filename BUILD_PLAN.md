# Build plan — days 1 to 10

One day per session. Do not start the next day until the current day's acceptance
criteria pass. If a day overruns, cut scope within that day rather than borrowing
from the next.

Days 1 and 2 are complete. They are documented here because the day 2 design decisions
constrain everything after them, and because the ADRs on day 10 are written from this
record.

---

## Day 1 — Frame the problem, ship the repo — COMPLETE

No feature code. The output of day 1 is a decision record, not a module.

**Tasks**
- Public repo from hour one, MIT licence, `.gitignore`
- `src/` layout, package `dbt_sentinel`, `pyproject.toml`
- `README.md`: problem statement, scope, non-goals
- `ROADMAP.md` as the parking lot for out-of-scope ideas
- `CLAUDE.md` with the six design rules

**Non-goals written down** (the highest-value 20 minutes of the project — every later
scope argument resolves by pointing at this list):
- Single warehouse, single repo
- No web UI; PR comments are the entire surface
- Not a catalog, not a lineage browser, not a test generator
- No column-level lineage at v1

**Acceptance**
- Repo public
- README explains the problem to someone who has never used dbt
- Non-goals list exists before any code is written

---

## Day 2 — Deterministic core — COMPLETE

Everything here must be exactly reproducible. No LLM, no network calls, no randomness.
This layer is the baseline that day 6 measures the agent against; if it is not
deterministic, the comparison is meaningless.

**Tasks**
- `models.py` — domain types: `Node`, `ChangedFile`, `ChangedNode`, `BlastRadius`
- `lineage.py` — manifest parsing (schema v7-v14) into a normalised node graph;
  models, sources and exposures included, test nodes excluded
- File-to-node resolution via both `original_file_path` and `patch_path`, matched on
  suffix (the dbt project may sit in a repo subdirectory)
- `diff.py` — unified diff parser retaining context lines; column extraction scoped to
  the correct model block inside a shared schema.yml
- BFS blast radius, cycle-safe, depth-tracked
- `report.py` — severity scoring, Markdown and Mermaid rendering
- `cli.py` — `--manifest`, `--diff`, `--mermaid`, `--fail-on` with exit codes
- Regression test for every bug found

**Acceptance**
- Correct severity report on a breaking PR
- Silent on a no-op PR — `--fail-on high` exits 0
- Unresolvable files surfaced as an explicit warning, not dropped
- Test suite passes

**Three false positives found and fixed.** The tests encoding them must keep passing
for the rest of the project:

| Bug | Cause | Fix |
|---|---|---|
| Comment-only change scored HIGH | Reach alone triggered severity | Structural change triggers; reach only amplifies |
| Shared schema.yml flagged an unrelated model | Column name collision across model blocks | Attribute column edits using diff context lines to find the enclosing model |
| One model reported twice | Changed in both `.sql` and `.yml`, deduped on the wrong key | Dedupe on `unique_id` |

The first is the important one and became design rule 2 in `CLAUDE.md`. Nearly every
staging model sits upstream of a dashboard; an agent that treats proximity to an
exposure as risk flags every PR and gets muted within a week.

**Test nodes excluded from traversal** for the same reason: dbt tests are children of
every model they cover, so including them makes a well-tested model look like it has
twelve downstream consumers.

**Known gaps accepted, not fixed:** regex column extraction misses `select *`, CTE
aliases and macro-generated columns. Macro changes do not resolve to models. Both are
surfaced as warnings and documented in the README rather than silently under-reported.

---

## Day 3 — Evaluation harness

Build the test set *before* the agent exists. This is the single most valuable artifact
in the project and it must not be written after the fact, when it would be tempted to
match whatever the agent happens to do.

**Tasks**
- `evals/fixtures/` — 30 labelled PR fixtures against a real compiled jaffle_shop manifest
  - 10 breaking: column rename with consumers, model deletion, contract violation,
    dropped dedup, changed join grain, type narrowing, removed filter, renamed model,
    incremental key change, source column dropped
  - 10 should-pass: comment added, whitespace, added test, new unused model, docs edit,
    reformatted SQL, added column (additive), config change, tag added, new seed
  - 10 subtle: PII column added untagged, incremental without full-refresh flag,
    contract edit that widens safely, exposure owner change, public access added,
    column reordered, test removed, materialization changed, cross-schema ref, alias change
- Label schema per fixture: `expected_severity`, `expected_rule_ids`, `rationale`
- `evals/runner.py` — scores precision, recall, false-positive rate, per-rule breakdown
- Writes `evals/results.json` and prints a Markdown table
- Manifest freshness check: warn if the manifest predates the diff

**Acceptance**
- `python -m evals.runner` prints a metrics table against the deterministic core
- Baseline numbers recorded in `evals/BASELINE.md`
- False-positive rate on the 10 should-pass fixtures is reported separately

**Risk:** labels drift toward whatever the code does. Write the `rationale` field for
every fixture from the dbt semantics, not from the output.

---

## Day 4 — Policy pack and retrieval

**Tasks**
- `policies/*.yml` — 12-15 governance rules. Each: `rule_id`, `title`, `description`,
  `severity`, `applies_to` (node kinds / path globs), `example_violation`
  - Cover: PII tagging, naming conventions, contract enforcement, test coverage on keys,
    incremental safety, public access, exposure ownership, staging layer purity
- `retrieval.py` — hybrid retrieval returning top-k rules for a given change
  - Keyword/glob prefilter on `applies_to`, then embedding similarity on the remainder
  - Cache embeddings to disk; do not re-embed the rule pack on every run
- `--explain` flag showing which rules were retrieved and the score for each
- Retrieval eval: precision@3 measured separately from end-to-end accuracy

**Acceptance**
- A PII-column fixture returns the PII rule in top-3
- A pure logic change does not return the PII rule at all
- Retrieval precision@3 recorded in `evals/BASELINE.md`

**Do not** embed the manifest, the lineage graph, or the SQL. Only the rule pack.

---

## Day 5 — Agent v0

**Tasks**
- `agent.py` — Claude API with tool-calling. Tools:
  - `get_lineage(model)` -> downstream nodes with depth and kind
  - `get_policies(change_summary)` -> retrieved rules
  - `get_columns(model)` -> declared columns and tests
- Pydantic `Finding` model: `rule_id`, `severity`, `model`, `explanation`, `suggested_fix`
- Schema validation with one retry on invalid output, then deterministic fallback
- Deterministic renderer consuming `list[Finding]`
- **Also scaffold the GitHub App skeleton today** — webhook receiver, signature
  verification, a stub that logs the payload. Day 8 is then wiring, not setup.

**Acceptance**
- End-to-end run on the breaking fixture produces valid structured findings
- Invalid LLM output falls back to deterministic-only, with a visible note in the comment
- Webhook endpoint receives and verifies a test payload

Quality will be poor today. That is expected and is not a reason to delay.

---

## Day 6 — Eval v1 and error analysis

**Tasks**
- Full 30-fixture run: agent vs. deterministic baseline, side by side
- Categorise every failure into: retrieval miss, reasoning error, schema violation,
  label ambiguity
- Where the agent was right and the label was wrong, fix the label and note it in
  `evals/LABEL_CHANGES.md`
- Record cost and latency per fixture

**Acceptance**
- `evals/RESULTS_V1.md` with the metrics table, failure taxonomy, and counts
- Top 3 failure modes named with counts

Do not fix anything today. Measuring and fixing in the same session corrupts the
before/after comparison.

---

## Day 7 — One iteration round

**Tasks**
- Fix the top 2 failure modes only
- Re-run the full eval suite
- Regression test for each fix
- `evals/RESULTS_V2.md` with before/after

**Hard rule:** one round. If recall lands at 70%, ship 70% and explain it. Further
prompt tuning has poor returns and burns the days that matter more.

---

## Day 8 — GitHub App live

**Tasks**
- Wire the day-5 skeleton: webhook -> fetch PR diff -> fetch manifest artifact ->
  run pipeline -> post review comment -> set commit status
- Manifest sourcing: CI artifact, or a committed `target/manifest.json` on the base branch
- Deploy to Railway / Fly / Render
- Run against 3 real PRs on a fork of a public dbt repo
- Record a demo GIF

**Acceptance**
- A real PR receives a real comment with correct severity
- Status check appears and blocks on HIGH

**This is the riskiest day.** GitHub App auth, webhook delivery, and manifest sourcing
all take longer than estimated. If it slips, cut day 9, not day 10.

---

## Day 9 — Harden and instrument

**Tasks**
- Cost and latency per PR, logged and surfaced in the comment footer
- Eval suite in GitHub Actions as a regression gate on the repo's own PRs
- Timeout, rate-limit, and API-failure handling
- README rewrite: install, run, architecture diagram, limitations table
- Tag `v1.0`

**Acceptance**
- CI green
- A stranger can clone and run it following only the README
- Cost per PR is a published number

---

## Day 10 — Written artifacts

**Build is frozen. Bug fixes only if they break the demo.**

**Tasks**
- `docs/PRD.md` — problem, users, success metrics, non-goals, what was cut and why
- `docs/ARCHITECTURE.md` + 4 ADRs:
  - ADR-001: graph traversal over vector retrieval for lineage
  - ADR-002: structural change triggers severity, reach amplifies
  - ADR-003: structured output with deterministic rendering
  - ADR-004: degradation to deterministic-only on LLM failure
- `docs/EVAL_REPORT.md` — methodology, metrics, failure taxonomy, limitations
- Case study page linking all of the above plus the repo

**Acceptance**
- All four documents exist and are linked from the README
- The eval report states the limitations plainly, including the ones that look bad

If you are writing code on day 10, you will ship a repo with no case study — which is
a repo nobody reads.
