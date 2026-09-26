# SQL review checklist: first live measurement

The first time the reviewer agent ran against a real model. Every earlier agent test
used an injected fake client, because the OpenAI account had no credits.

- **Harness:** `python -m evals.sql_policy_eval` over `evals/fixtures/sql/`: 12 fixtures,
  one true positive and one near miss for each of the six checklist rules in
  `policies/sql_quality.yml`.
- **Model:** `gpt-4o`, the pinned `DEFAULT_MODEL`.
- **Date:** 2026-09-26.
- **Cost:** about $0.15 per full run.
- **Scoring:** counts only checklist `rule_id`s cited at medium or above. Labels were
  written before the agent ran.

## Result: the checklist doesn't work yet

| Run | Precision | Recall | Near-miss FPR | Exact | Degraded | Cost |
|---|---|---|---|---|---|---|
| 1 | 0.500 | 0.167 | 0.167 | 6/12 | 0 | $0.1419 |
| 2 (published, `sql_policy_results.json`) | **0.000** | **0.000** | **0.167** | 5/12 | 0 | $0.1494 |

Two runs of an unchanged prompt disagree on precision by 0.5. At 12 fixtures, one
changed verdict moves precision by a large step, so a single run is closer to anecdote
than measurement. Run 2 is published because it is the run whose raw findings were
recorded: the harness was changed between runs to keep every finding, not only the
scored ones. That change is measurement, not tuning. The prompt and policies are
identical across both runs.

The plumbing is proven by `tests/test_sql_policies.py`: the checklist reaches the
prompt, the tools return the evidence, invented citations are flagged, and retrieval
scores are byte-identical with and without the checklist loaded. **The reviewing
isn't proven.** On this evidence, the checklist rules should not be advertised as
catching these defects.

## Error analysis: the three failure modes

Ranked by how many fixtures each one decided.

### 1. It cites a governance rule with a similar name instead of the checklist rule (6 of 12)

The prompt lists `retrieved policy rules` before the `SQL review checklist`, and when a
retrieved rule sounds close, the agent cites that one.

| Fixture | Should cite | Cited instead |
|---|---|---|
| `q04_money_as_float` | `data-type-correctness` | `type-safety` (high) |
| `q04n_money_as_decimal` | nothing | `type-safety` (high): the *fix* read as a narrowing |
| `q06_grant_to_public` | `sql-security` | `public-access-review`, which is about dbt `access: public`, a different thing |
| `q01`, `q01n`, `q03`, `q03n` | varies | `pii-tagging` on every added column, PII or not |

This is a retrieval-and-prompt interaction, not a reasoning failure about SQL. In
`q04` the agent's explanation is correct; only the citation is wrong.

**Next iteration:** make each checklist rule's scope disjoint from its governance
neighbour, and say so in both descriptions: `type-safety` covers a contract column's
type changing under consumers, `data-type-correctness` covers a type wrong for its
meaning. Either give the checklist first position in the prompt, or merge the
overlapping pairs.

### 2. It stays silent on real query bugs (2 of 12)

`q02_not_in_nullable` and `q05_where_nullifies_left_join` produced **no findings at
all**. Both are only visible in the full query, and `q05` spent 77 output tokens, which
suggests `get_model_sql` wasn't called. The prompt's "report nothing rather than
something speculative" rule, which is correct for the severity suite, likely suppresses
exactly the findings that need reading the whole query.

**Next iteration:** log tool calls per fixture, which the harness doesn't yet do, to
confirm whether `get_model_sql` ran. If it didn't, require it for any SQL change before
`submit_findings` is accepted.

### 3. It invents changes and reports non-issues (3 of 12)

- `materialization-review` is cited on both grant fixtures, saying the model "changed
  from a view to a table". Nothing in either diff touches materialization. The prompt
  line `materialization: table` is metadata, not a change, and the agent reads it as one.
- In `q01n` it files `join-key-uniqueness` at **medium** while explaining that the join
  is correct and the key is tested. The findings schema has no way to say "checked,
  fine", so a pass gets reported as a finding.

**Next iteration:** make the prompt label metadata lines as current state rather than
changes, and add to the prompt: "a finding is a problem; don't submit a finding to say
something is correct."

## What this means for the published claims

- Nothing in the published severity metrics changes. Checklist findings are advisory:
  they never affect severity or the commit status, by the same rule that keeps agent
  findings out of the status today.
- The deterministic layers shipped alongside the checklist (`exposed-secret`,
  `null-comparison`) are measured separately in `checks_results.json`: 12/12 fixtures,
  precision and recall 1.000. Those claims stand on their own.
- The next step is the iteration above, in its own session, re-measured over several
  runs so the number is an average and not a draw.
