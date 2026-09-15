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

## Considered and rejected for v1

- Web UI / dashboard — a non-goal, not a deferral.
- Multi-warehouse SQL dialect handling — Snowflake assumptions are fine for v1.
- Auto-fix PRs that rewrite the offending SQL. The review is advisory; a bot that
  rewrites your models is a different and much scarier product.
- Slack notifications. The PR comment is the surface.

## Post-project, if it earns it

- Historical trend: severity per PR over time, to see whether the repo is getting safer.
- Per-team routing using exposure `owner`, so a breaking change pings the dashboard owner.
