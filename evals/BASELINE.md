# Baseline — deterministic core, no LLM

Day 3. Scored by `python -m evals.runner` against 30 labelled fixtures and a real
manifest compiled by dbt 1.12.4 (schema v12) from an extended `dbt-labs/jaffle_shop`.

**No agent exists yet.** These numbers are the floor that day 6 measures the reviewer
agent against. If the agent cannot beat them, it is not worth its cost or its latency.

## Headline

| Metric | Value |
|---|---|
| Precision | **0.800** |
| Recall | **0.533** |
| F1 | 0.640 |
| **False-positive rate (should-pass block)** | **0.200** |
| Exact severity match | 14/26 (0.538) |
| Fixtures silently dropped (excluded from metrics) | **4** |

Confusion at the `medium+` flag threshold: TP=8, FP=2, FN=7, TN=9.

| Category | n | Errored | Exact match | Rate |
|---|---|---|---|---|
| breaking | 10 | 2 | 3 | 0.375 |
| should_pass | 10 | 0 | 8 | 0.800 |
| subtle | 10 | 2 | 3 | 0.375 |

## How to read this

**Recall 0.533 at precision 0.800 is the expected shape of a structural-only scorer.**
When it fires it is usually right; it misses more than half of what a reviewer should
catch. Every miss is a change whose risk lives in SQL semantics — join grain, an
incremental predicate, a dropped `distinct` — that no amount of graph traversal reveals.
That gap is the argument for the day-5 agent, and it is quantified here before the agent
exists so the comparison cannot be retrofitted.

**The 4 errored fixtures are excluded, not scored as passes.** They resolve to zero nodes
and emit no warning, so counting them as correct silence would have reported a materially
better baseline than the tool deserves. Two are HIGH-labelled breaking changes.

## Defects this run found

Listed in priority order. Nothing is fixed here — day 3 measures, and fixing inside the
measuring session is what destroys a before/after comparison.

### 1. YAML column attribution drops the node entirely (4 fixtures, 2 of them HIGH)

`b03`, `b06`, `s03` edit `models/marts/schema.yml`; `s04` edits `models/exposures.yml`.
All four resolve to a file, match real nodes, and are then discarded.

`yaml_columns_by_model` in [diff.py](../src/dbt_sentinel/diff.py) only tracks `- name:`
entries under a `columns:` key. The marts `schema.yml` puts `data_type:` under each
column and the diff hunk begins *inside* a `columns:` list, so the parser reads the
column names themselves as model headings:

```
per_model = {'order_id': ([], []), 'customer_id': ([], []), 'total_amount': ([], [])}
```

Those keys never match a model name, so `resolve_changes` keeps nothing. The exposures
file has no `columns:` block at all, so `per_model` is empty.

Consequence: **a contract column can be dropped and the tool says nothing at all.** Not
a wrong severity — no output. This is the exact failure mode design rule 4 exists to
prevent, and it is worse than the path bug it replaced, because there is no warning.

### 2. Any column-set delta triggers severity, including additive ones (2 FPs)

`p10_additive_column` adds `loaded_at` to `stg_orders` and scores **HIGH**.
`p03_test_added` adds a `not_null` test and scores **HIGH**.

`p10` is the deliberate twin of `b01` — same file, same 7-node reach, opposite semantics.
The tool scores them identically, which means it is reacting to *a column set changed*
rather than *a column removed*. `ChangedNode.is_structural` counts `added_columns`
alongside `removed_columns`, and `p03`'s new `- name: order_id` under a `tests:` key is
misread as a new column.

Both FPs come from the should-pass block, which is why the FPR is reported separately:
0.200 means one in five routine PRs gets a HIGH. That is mute-the-bot territory.

### 3. Real breaking changes score LOW or MEDIUM (7 FNs)

| Fixture | Expected | Actual | Why it is missed |
|---|---|---|---|
| `b04_dropped_dedup` | high | low | `distinct` removal is invisible to column extraction |
| `b08_model_renamed` | high | low | Rename detected as a file move; no consumer check |
| `b05_changed_join_grain` | high | medium | `left`→`inner` is not a column change |
| `b07_removed_incremental_filter` | high | medium | `is_incremental()` block deletion not parsed |
| `b09_incremental_key_change` | high | medium | `unique_key` config edit not parsed |
| `s01_pii_column_untagged` | medium | low | No policy pack yet (day 4) |
| `s07`, `s08`, `s09`, `s05` | medium | low | Governance judgments, no rules yet (day 4) |

The four `s0*` misses are expected: those fixtures label governance concerns and the
policy pack does not exist until day 4. They are counted as misses anyway rather than
excused, because the published recall should reflect what the tool does today.

## Reproducing

```bash
python -m evals.runner                  # full suite, writes evals/results.json
python -m evals.runner --only breaking  # one block
```

The manifest is pinned at `evals/manifest/manifest.json` so results do not depend on a
clone outside the repo. To regenerate it, see [fixtures/README.md](fixtures/README.md).

## Retrieval baseline (day 4)

14 rules across 4 policy files, scored by `python -m evals.retrieval_eval`. Measured
**separately from severity accuracy on purpose**: when the day-6 agent gets a fixture
wrong, the cause is either "the right rule never reached the model" or "the right rule
reached it and it reasoned badly". Those need different fixes, and one end-to-end number
cannot separate them.

| Metric | Value |
|---|---|
| Precision@3 | **0.708** |
| Recall@3 | 0.630 |
| Hit@3 (≥1 expected rule found) | 0.684 |
| **Correct silence on no-rule fixtures** | **0.455** |

Scored over 19 fixtures expecting at least one rule; 11 expecting none.

### Acceptance criteria

Both build-plan criteria pass, and the second is the one that matters:

```
s01_pii_column_untagged  -> pii-tagging (0.493)     [PII rule in top-3]
b05_changed_join_grain   -> grain-integrity (0.566) [no PII rule at all]
p01_comment_added        -> no rule matched          [silence on a comment]
```

A retriever that returned the PII rule on every diff would pass the first criterion and
be worthless. `correct_silence_rate` exists so that failure cannot hide.

### What "embedding similarity" means here

It is **TF-IDF cosine over the rule pack**, stdlib only — not a semantic embedding. Named
plainly because the limitation is specific: it matches a paraphrase only when the
vocabulary overlaps. That is why every rule carries an explicit `keywords` list, and why
a curated keyword hit outranks a marginally higher cosine.

Bought in exchange: no embeddings dependency, no API key, fully offline, and
deterministic — anyone who clones the repo reproduces these numbers exactly. Rejected
alternatives are in ADR form on day 10.

**Only the rule pack is vectorised.** The manifest, lineage graph and SQL are never
embedded — reach is a BFS and file resolution is a string match, both exact. Design
rule 1.

### Noise defect found and fixed during this session

First measured run scored **precision@3 0.400 / correct silence 0.273** — 8 of 11
no-rule fixtures drew spurious rules. `--explain` showed why: `summarise_change` fed the
node's own *ambient* attributes into the query, so `stg_` matched `staging-layer-purity`
on every staging file and `protected` matched `public-access-review` on every node. Those
are path and config facts, true of every diff touching the node, not evidence that
anything was violated.

Two fixes: the query is now built only from what *changed* (a comment-only edit produces
an empty query and therefore silence), and citing a rule requires either a curated
keyword hit or a cosine above a floor, since TF-IDF gives a small positive score to any
incidental shared token.

| | Precision@3 | Correct silence |
|---|---|---|
| Before | 0.400 | 0.273 |
| After | **0.708** | **0.455** |

The day-3 severity baseline is unchanged at 0.800 / 0.533 / 0.200, confirming retrieval
did not perturb the measurement it will later be compared against.

### Retrieval failures remaining (not fixed)

| Fixture | Expected | Why it misses |
|---|---|---|
| `b03`, `b06`, `s03` | contract rules | Node never resolves — the day-3 YAML attribution defect, upstream of retrieval |
| `s04_exposure_owner_change` | `exposure-ownership` | Same: exposures resolve to no node |
| `b08_model_renamed` | `contract-breaking-change` | Rename carries no changed lines, so the query has almost no vocabulary |
| `s02_incremental_no_full_refresh` | `incremental-safety` | Diff says `* 1.1`; the rule's vocabulary is `full_refresh`, `backfill`, `is_incremental` — no lexical overlap. **The clearest case for semantic matching in the suite.** |

Residual noise: 6 of 11 no-rule fixtures still draw a rule, mostly `pii-tagging` firing
on `first_name`/`last_name` context lines in a whitespace diff. Parked in ROADMAP.md
rather than tuned further — day 4 is measurement, and tuning against the eval set is how
a retriever gets overfitted to 30 fixtures.

## What is not measured here

- **Cost and latency** — no LLM calls (day 6).
- **Agent vs. baseline** — the point of this file is to exist before that comparison.
- **Column-level correctness.** The suite scores the severity of a PR, not whether the
  tool named the right column. A fixture can pass with the right severity for a
  partially wrong reason; `b10` resolving to the source node rather than its consumer is
  one such case.
