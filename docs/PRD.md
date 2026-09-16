# dbt-sentinel — product requirements

Written day 1, revised day 10 with what the build actually taught. Where the original
intent and the shipped thing disagree, both are recorded rather than the plan being
quietly rewritten to match the outcome.

## Problem

A dbt project is a directed graph of SQL models. Renaming a column in a staging model is
one line in a diff and can break twelve dashboards, because dbt does not rewrite
references — the next run fails, or worse, succeeds with wrong numbers.

The reviewer looking at that diff sees one line. Seeing the twelve consumers means walking
the DAG by hand, so in practice nobody does it, and breaking changes ship. The information
needed to prevent this already exists in `target/manifest.json`; nothing puts it in front
of the reviewer at the moment they decide.

**Why existing tools don't cover it.** `dbt build` catches a break *after* merge, in CI, when
the cost of the mistake is already paid. `sqlfluff` lints syntax, not consequences. dbt
Cloud's lineage view is a browser you have to remember to open. None of them appear in the
PR unprompted, and the PR is where the decision happens.

## Users

**Primary: the analytics engineer reviewing a teammate's PR.** They know dbt, have limited
time, and cannot hold a 200-model DAG in their head. They need to know whether *this* diff
reaches anything that matters.

**Secondary: the analytics engineer opening the PR.** A warning before merge is cheaper
than a revert after, and cheaper still than a dashboard that is quietly wrong for a week.

**Explicit non-user: the data consumer.** No dashboard, no UI, no digest. The tool speaks
only to people who read pull requests.

## Success metrics

Chosen so failure is visible. Each is published in
[EVAL_REPORT.md](EVAL_REPORT.md) whatever it says.

| Metric | Target | Actual | Why this one |
|---|---|---|---|
| False-positive rate on routine PRs | < 0.10 | **0.200** | The number that decides whether anyone keeps it installed |
| Recall on breaking changes | > 0.70 | **0.533** | A reviewer who trusts it and gets missed breakage is worse off than one who never had it |
| Precision | > 0.75 | **0.800** | Met |
| Retrieval precision@3 | > 0.60 | **0.708** | Measured separately so a retrieval miss is distinguishable from a reasoning error |
| Cost per PR | < $0.05 | **unmeasured** | A review nobody can afford to run is not a review |
| Comment latency | < 60s | **unmeasured** | Past a minute the reviewer has moved on |

**Two of six targets are missed and two are unmeasured.** The FPR is double its target and
recall is well under. The gap is diagnosed in EVAL_REPORT.md rather than smoothed over;
both misses trace to two specific defects, both of which are known and both of which were
deliberately left unfixed so the day-6 measurement could establish a clean baseline.

## Scope

**In:** one dbt project, one repo, one warehouse dialect's assumptions. Model-level
lineage. A PR comment and a commit status. Governance rules as a YAML pack.

**Out, decided before any code and never revisited:**

- Web UI of any kind. PR comments are the entire surface.
- Multi-warehouse support.
- Data catalog, lineage browser, test generator.
- Column-level lineage.
- Auth, multi-tenancy, billing.
- Agent frameworks that hide control flow.

That list was the highest-value twenty minutes of the project. Every later scope argument
resolved by pointing at it, and the ideas it rejected went to
[ROADMAP.md](../ROADMAP.md) instead of into the build.

## What was cut, and why

**Column-level lineage.** The single biggest precision win available: the tool reports that
a dropped column has twelve downstream models, not which of the twelve actually select it.
Cut because it needs real per-model SQL parsing, which is most of a second project.

**sqlglot for column extraction.** Would fix `select *`, CTE aliases, and macro-generated
columns. Cut because the regex is honest about its misses and a dependency is not free.

**Real semantic embeddings.** Retrieval is TF-IDF over the rule pack, so it cannot match a
paraphrase — a diff saying `* 1.1` never reaches a rule whose vocabulary is `full_refresh`.
Cut to avoid a heavyweight local model or a second vendor; the cost is one measured miss
and it is named in the eval report.

**Macro → model resolution.** A changed macro warns instead of resolving. Tractable from
`depends_on.macros`; simply did not fit.

## What was added that was not planned

- **Manifest freshness checking** (`--changed-at`). A stale manifest silently shrinks every
  blast radius, which is a confident wrong answer — the worst kind.
- **A retry policy.** Degrading a whole review because an API was briefly busy throws away
  analysis that was already correct.
- **An eval regression gate in CI.** Published numbers that nothing enforces drift.

## Definition of done

| Criterion | Status |
|---|---|
| Deterministic core with regression tests | Done — 129 tests |
| 30 labelled eval fixtures, labels written before the runner | Done |
| Published precision / recall / FPR | Done, and the numbers are mediocre |
| Policy pack with hybrid retrieval, scored separately | Done |
| Agent returning validated structured findings | Done, **never measured** |
| GitHub App posting to a real PR | **Not done** — wired and tested against a fake client, never deployed |
| Honest error analysis naming the top failure modes | Done |
| PRD, architecture with ADRs, eval report, case study | Done |

Two criteria are unmet. Both are stated here and in the README rather than being described
as "in progress".
