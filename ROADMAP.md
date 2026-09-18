# Roadmap — the parking lot

Ideas that are valuable but out of scope for the 2-week build. Nothing here gets built
during days 1-10. This file exists so that scope arguments end in an append rather than
a commit.

See the non-goals list in `CLAUDE.md` for the things that are permanently out of scope
rather than merely deferred.

## Deferred, would genuinely help

- **Column-level lineage.** v1 reports that a dropped column has 12 downstream models,
  not which of those 12 actually select it. This is the single biggest precision win
  available, and also the largest piece of work — it needs real SQL parsing per model.
- **sqlglot-based column extraction** to replace the regex heuristic. Would fix
  `select *`, CTE aliases, and macro-generated columns. Costs a dependency, and the
  regex is honest about its misses, so it waits.
- **Macro → model resolution.** A changed macro currently warns instead of resolving.
  Building a macro-to-consumer index from the manifest's `depends_on.macros` is
  tractable and would close a real gap.
- **`--since` git integration** so the tool computes its own diff from a base ref
  instead of being handed one.

## Found by the day-3 eval baseline, deferred to day 7

Both are measured and documented in `evals/BASELINE.md`. Day 6 measures, day 7 fixes the
top two failure modes — these are the current candidates, and they are parked here rather
than fixed on sight so the before/after comparison survives.

- **YAML column attribution drops the node entirely.** `yaml_columns_by_model` only
  tracks `- name:` under a `columns:` key, so a `data_type:` edit on a contracted model
  or an `owner:` edit on an exposure resolves to nothing and the change is silently
  dropped — no severity, no warning. 4 fixtures affected, 2 of them HIGH. This is the
  worst failure shape available: a dropped contract column produces no output at all.
- **`is_structural` counts added columns as structural.** An additive column scores the
  same HIGH as a rename (`p10` vs `b01`, same model, same reach, opposite semantics), and
  a new `- name:` under a `tests:` key is misread as a new column. Drives the 0.200
  false-positive rate on the should-pass block.

## Found by the day-4 retrieval baseline, deferred

Measured in `evals/BASELINE.md`. Not tuned further on purpose: day 4 measures, and
tuning a retriever against the same 30 fixtures it is scored on is overfitting.

- **Lexical retrieval cannot match a paraphrase.** `s02_incremental_no_full_refresh`
  expects `incremental-safety`; the diff says `* 1.1` and the rule's vocabulary is
  `full_refresh` / `backfill` / `is_incremental`. Zero lexical overlap, so it misses.
  This is the strongest argument in the suite for real semantic embeddings, and the
  honest cost of the zero-dependency choice.
- **Residual PII noise on context lines.** 6 of 11 no-rule fixtures still draw a rule,
  usually `pii-tagging` matching `first_name` / `last_name` on unchanged *context* lines
  in a whitespace diff. Fix is to weight added/removed lines above context lines in
  `summarise_change`.
- **Renames carry no vocabulary.** `b08_model_renamed` has no changed content lines, so
  the retrieval query is nearly empty and `contract-breaking-change` misses. The rename
  is detectable structurally; the query builder should synthesise vocabulary from the
  change type rather than relying on diff text.

## Day-5 caveat that day 6 must respect

**The agent has never made a real API call.** Every day-5 test injects a fake client, so
what is proven is the plumbing — schema validation, the retry, the degradation paths, the
tool dispatch — and not the model's behaviour. Today's 31 passing tests are not evidence
that review quality is any good, and they were never meant to be.

Day 6 is the first time real findings exist. Two consequences:

- Budget for the first live run disagreeing with the fake-client shape (tool-call
  sequencing, `submit_findings` arriving alongside other tool calls in one response).
- `DEFAULT_MODEL` is pinned to an exact id rather than an alias, so the day-6 numbers
  stay attributable to one model. Changing it invalidates the comparison.

## Day 6 status: harness built, measurement outstanding

`evals/compare.py` is written and tested, but **no live run has happened** — there is no
`OPENAI_API_KEY`, so the agent arm has never executed. Day 6 is formally incomplete:
`RESULTS_V1.md` is deliberately unwritten rather than filled with placeholder numbers,
and the runner exits 2 rather than emitting a results file with a fabricated agent arm.

To finish day 6:

```bash
export OPENAI_API_KEY="sk-proj-..."
python -m evals.compare            # ~$0.30-1.00 for 30 fixtures on gpt-4o
```

Then the numbers, the failure taxonomy and the top-3 failure modes go into
`evals/RESULTS_V1.md`, and any label the agent proves wrong is argued in
`evals/LABEL_CHANGES.md` — never silently relabelled.

## Deferred from the check layer, each with its precondition

Taken from an external dbt code reviewer and deliberately not built. Each entry names the
condition that would make it necessary, so the decision can be revisited on evidence
rather than on taste.

- **Hunk-offset parsing and inline review comments.** `diff.py` matches `@@` and discards
  the offsets, so nothing in the codebase knows a line number. Parsing them is ~60 lines;
  the reason to wait is what they unlock. Inline comments need
  `POST /pulls/{n}/reviews`, and GitHub **422s the entire review** if one position is not
  part of the diff — a position bug drops everything instead of degrading, which collides
  with design rules 4 and 5. Precondition: a fallback path to an issue comment plus a
  position validator, before the first inline comment is ever posted. It is also the only
  change that would edit `parse_diff`, which feeds both published eval baselines.

- **Grandfather baseline for brownfield adoption.** A frozen list of pre-existing
  violations that downgrade to WARN with a `[GRANDFATHERED]` prefix, plus a
  `--validate-grandfather` mode to find entries whose file no longer violates. Not needed
  here: checks read only added diff lines, so pre-existing violations never fire.
  **Precondition: the moment any check gains whole-file scanning.** Building the exemption
  machinery before the problem exists would also put a suppression mechanism next to
  `evals/`, which weakens the claim that the published numbers are unsuppressed.

- **Scoped ignore file.** A `.sentinel_ignore` restricted to named check categories, so it
  cannot be used to switch off the structural analysis. Nothing to ignore at four checks —
  and a check that needs an escape hatch is a check that should be dropped instead.
  Precondition: roughly 15 checks, or the first check that legitimately needs a per-repo
  exemption.

- **`--category` / `--check` CLI filters.** With four checks a filter selects between four
  and three. Precondition: about ten checks.

## Considered and rejected for v1

- Web UI / dashboard — a non-goal, not a deferral.
- Multi-warehouse SQL dialect handling — Snowflake assumptions are fine for v1.
- Auto-fix PRs that rewrite the offending SQL. The review is advisory; a bot that
  rewrites your models is a different and much scarier product.
- Slack notifications. The PR comment is the surface.

## Post-project, if it earns it

- Historical trend: severity per PR over time, to see whether the repo is getting safer.
- Per-team routing using exposure `owner`, so a breaking change pings the dashboard owner.
