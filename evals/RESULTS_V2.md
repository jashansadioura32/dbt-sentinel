# Results v2 — day 7, one iteration round

Day 7 fixes the top two failure modes the day-3 baseline measured, re-runs the full
suite, and records the before/after. Per the build plan's hard rule this is **one
round**: no further tuning, and the numbers below are shipped as they landed.

Scored by `python -m evals.runner` over the same 30 fixtures and the same pinned
manifest as [BASELINE.md](BASELINE.md). No labels were changed — see
[LABEL_CHANGES.md](LABEL_CHANGES.md).

## Headline: before and after

| Metric | Day 3 (v1) | Day 7 (v2) | Change |
|---|---|---|---|
| Precision | 0.800 | **0.909** | +0.109 |
| Recall | 0.533 | **0.588** | +0.055 |
| F1 | 0.640 | **0.714** | +0.074 |
| **False-positive rate (should-pass)** | 0.200 | **0.000** | **−0.200** |
| Exact severity match | 14/26 (0.538) | **17/30 (0.567)** | +3 |
| **Fixtures silently dropped** | **4** | **0** | **−4** |

Confusion at the `medium+` threshold: TP=10 FP=1 FN=7 TN=12 (was TP=8 FP=2 FN=7 TN=9).

| Category | n | Errored | Exact match | Day-3 rate | Day-7 rate |
|---|---|---|---|---|---|
| breaking | 10 | 0 | 3 | 0.375 | 0.300 |
| should_pass | 10 | 0 | 10 | 0.800 | **1.000** |
| subtle | 10 | 0 | 4 | 0.375 | 0.400 |

**The two rows that matter are the last two of the headline table.** Precision and
recall moved modestly. The false-positive rate going to zero and the silent-drop count
going to zero are the changes that decide whether anyone leaves the bot installed.

Note the denominator change: day 3 scored 26 fixtures because 4 were excluded as
errors. Day 7 scores all 30. The breaking-category rate *falls* (0.375 → 0.300) purely
because two fixtures that were previously excluded are now scored and still wrong —
that is the honest direction, and inflating it by keeping them excluded was never an
option.

## Fix 1 — YAML column attribution dropped the node entirely

**Was:** 4 fixtures (`b03`, `b06`, `s03`, `s04`) resolved to a real file, matched real
nodes, and were then discarded. Output was not a wrong severity but **nothing at all**:
a dropped contract column produced silence. The exact failure mode design rule 4 exists
to prevent.

**Cause, in two parts.** `yaml_columns_by_model` only recognised a `- name:` as a column
when it had already seen a `columns:` line. A hunk that opens *inside* a `columns:` list
— the common case for a mid-file edit — shows no such line, so the first column name
became the model heading and the rest became its columns. Nothing matched a real model.
Then `resolve_changes` gated on `node.name not in per_model` and dropped the node.

**Fix.** Three changes in [diff.py](../src/dbt_sentinel/diff.py):

- A `- name:` is treated as a model heading only when the parser can see it is *outside*
  a `columns:` list. When it cannot tell, it attributes nothing rather than guessing.
- `data_type:` / `quote:` under an entry proves that entry was a column, which lets a
  mid-list hunk retract a wrongly-inferred heading.
- A `tests:` / `data_tests:` block is tracked separately, so a test name is never read
  as a column.

And the gate in `resolve_changes` now falls through to "no columns, still changed" when
attribution found no model block at all, instead of discarding the node.

**What this deliberately does not do.** `models/marts/schema.yml` documents two models.
When a hunk shows no heading, `customer_id` genuinely cannot be pinned to one of them,
and attributing it to both would recreate the day-2 shared-schema false positive that
`test_day2.py` pins. So an unattributable removed column is carried on
`ChangedNode.unattributed_removed_columns` and rendered as an explicit uncertainty at
**medium**:

> Removed column(s) `customer_id` in `models/marts/schema.yml` could not be attributed
> to a specific model — the hunk shows no model heading. Verify whether
> `fct_order_payments` declares them.

When the file documents exactly one model there is only one possible owner, so the
column is attributed normally. The uncertainty is a property of a *shared* schema.yml,
not a blanket refusal.

**Cost, stated plainly:** `b03` and `b06` are labelled HIGH and now score **medium**, so
they still count as misses. The tool went from silence to a medium-severity warning that
names the column and the file. That is a large practical improvement and it does not
show up in the exact-match number at all.

## Fix 2 — `is_structural` counted added columns

**Was:** `p10_additive_column` (adds `loaded_at` to `stg_orders`) scored **HIGH** —
identical to `b01_column_rename_with_consumers`, its deliberate twin with the same model
and the same 7-node reach and the opposite semantics. `p03_test_added` also scored HIGH.
Both sat in the should-pass block and were the whole of the 0.200 FPR.

**Cause:** `ChangedNode.is_structural` counted `added_columns` alongside
`removed_columns`, so the tool was reacting to *a column set changed* rather than *a
column removed*.

**Fix:** one line in [models.py](../src/dbt_sentinel/models.py) — added columns are no
longer structural. Adding a column breaks no consumer: `select *` picks it up, an
explicit select ignores it. Removal, deletion and rename are the changes that take
something away.

**Result:** the should-pass block goes 8/10 → **10/10**, FPR 0.200 → **0.000**, and
`b01` stays HIGH so the twins diverge as they should.

## The one remaining false positive

`s03_contract_widened_safely` is labelled **low** and now scores **medium**. It is the
single FP behind precision 0.909.

It is a direct consequence of fix 1: `s03` edits the shared marts `schema.yml`, so its
column change is unattributable and draws the medium uncertainty warning above. The
warning is *correct about its own uncertainty* — the tool genuinely cannot tell which
model the edit belongs to — but the underlying change is safe, so the warning is noise
on this fixture.

This is a deliberate trade and it is the right side of it: before the fix `s03` produced
no output at all, which also meant `b03` (a real HIGH) produced no output. Trading four
silent drops for one over-cautious medium is the trade design rule 4 asks for. Closing
it properly needs per-model hunk attribution in a shared schema.yml, which is parked in
[ROADMAP.md](../ROADMAP.md).

## What did not change, and why that matters

The retrieval and check-layer harnesses were re-run on the same commit:

| Harness | Day 3/4 | Day 7 | Why it moved |
|---|---|---|---|
| Retrieval precision@3 | 0.708 | 0.636 | Denominator grew — see below |
| Retrieval recall@3 | 0.630 | **0.778** | 3 of 4 dropped fixtures now hit |
| Correct silence | 0.455 | 0.455 | Unchanged |
| Check precision / recall | 1.000 | 1.000 | Unchanged |
| Undeclared check findings | 0 | 0 | Unchanged |

**Retrieval precision@3 fell and that is an improvement.** `b03`, `b06` and `s03`
previously resolved to no node, so the retriever was never invoked and they contributed
nothing. They now each return 3 rules of which 1–2 are correct, where before they
returned none. Recall rose 0.630 → 0.778 on exactly the same change. A metric that
improves by keeping fixtures out of the denominator is the kind this project publishes
against, not for.

`s04_exposure_owner_change` still misses `exposure-ownership` — the node now resolves,
but the diff carries almost no rule vocabulary. Unchanged from the day-4 analysis.

## Regression tests

Per design rule 6, every fix is pinned. [../tests/test_day7.py](../tests/test_day7.py)
adds 16 tests, all of which failed against the pre-fix code before the fixes landed —
that ordering is what makes them evidence rather than decoration.

One existing test was updated rather than added to:
`test_a_check_finding_never_moves_the_blast_radius_severity` pinned `p03` at HIGH and
said in its own docstring that HIGH was wrong and that day 7 would move it. It now pins
LOW, and additionally asserts the invariant it was always really guarding — that running
the checks does not perturb the severity at all.

The CI floors in [check_baselines.py](../.github/scripts/check_baselines.py) were
ratcheted to the new values in the same commit, as that script's docstring requires.
Leaving them at the day-3 values would let the tool silently regress to the old
behaviour and still pass.

## What is still broken

Unchanged from the day-3 analysis and out of scope for a one-round iteration. All seven
remaining false negatives are changes whose risk lives in SQL semantics that graph
traversal and regex cannot see:

| Fixture | Expected | Actual | Why |
|---|---|---|---|
| `b04_dropped_dedup` | high | low | `distinct` removal is invisible to column extraction |
| `b08_model_renamed` | high | low | Rename detected as a file move; no consumer check |
| `b05_changed_join_grain` | high | medium | `left`→`inner` is not a column change |
| `b07_removed_incremental_filter` | high | medium | `is_incremental()` block deletion not parsed |
| `b09_incremental_key_change` | high | medium | `unique_key` config edit not parsed |
| `b03`, `b06` | high | medium | Unattributable in a shared schema.yml (fix 1's cost) |
| `s01`, `s05`, `s07`, `s08`, `s09` | medium | low | Governance judgments the scorer does not make |

**This is the argument for the agent, quantified after the deterministic core has been
fixed rather than before.** Recall 0.588 at precision 0.909 is the ceiling of a
structural-only scorer: when it fires it is almost always right, and it still misses
about 40% of what a reviewer should catch. Whether the agent beats that is measured in
`RESULTS_V1.md`, which is still unwritten because the agent arm has never run — see
[../ROADMAP.md](../ROADMAP.md).
