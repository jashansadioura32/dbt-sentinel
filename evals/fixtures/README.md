# Eval fixtures

30 labelled PR fixtures: 10 breaking, 10 should-pass, 10 subtle. Each fixture is a
unified diff plus a ground-truth label in [labels.yml](labels.yml).

## The one rule that makes this worth publishing

**The labels were written before the runner existed.** Every `rationale` in `labels.yml`
argues from dbt semantics — what the change does to consumers of the model — not from
what dbt-sentinel outputs. No fixture was labelled by running the tool and recording the
answer.

This ordering is the whole point. Writing the runner first and the labels second produces
a suite that confirms whatever the code already does, and a precision number derived from
it means nothing. If you relabel a fixture to improve a metric, you have deleted the value
of the artifact.

The legitimate reason to change a label: **the tool was right and the label was wrong on
dbt semantics.** Change it, and log it in `../LABEL_CHANGES.md` with the reasoning.

## What the fixtures run against

`../manifest/manifest.json` — a real manifest compiled by dbt 1.12.4 (schema v12) from
`dbt-labs/jaffle_shop`, extended with the node kinds upstream jaffle_shop lacks:

| Added | Why |
|---|---|
| 2 sources (`ecom.raw_orders`, `ecom.raw_payments`) | Upstream loads everything via seeds, so the compiled manifest had **zero** source nodes |
| 3 exposures with owners | Upstream has none, so `BlastRadius.exposures` — the loudest severity signal — was never exercised |
| `fct_order_payments` (incremental + enforced contract) | Upstream has no contracts and nothing incremental; both branches of `report.assess` were dead code |
| `rpt_customer_revenue` (`access: public`) | Gives a real public node and a second hop of depth |

The manifest is pinned in the repo rather than recompiled per run, so eval numbers are
reproducible and do not depend on a clone outside the repo.

## Blast-radius facts the labels rely on

```
stg_orders           -> 7 downstream, 3 exposures, 1 contracted, 1 public
stg_payments         -> 7 downstream (same surface)
stg_customers        -> 2 downstream, 1 exposure
fct_order_payments   -> 3 downstream, 2 exposures, 1 public   [incremental, contracted]
rpt_customer_revenue -> 1 downstream, 1 exposure              [public]
orders, stg_raw_events -> 0 downstream (leaves)
```

`stg_orders` having the widest reach in the project is deliberate: most should-pass
fixtures touch it, so any tool that treats reach as a severity trigger fails the
false-positive block loudly.

## Fixture pairs that carry the most signal

Designed so a tool cannot pass both halves by reacting to reach or to file identity:

| Pair | Same | Different | Correct verdicts |
|---|---|---|---|
| `b01` / `p10` | stg_orders, 7 downstream | rename vs. add a column | HIGH / LOW |
| `b06` / `s03` | contracted `order_id`/`total_amount` | narrow vs. widen the type | HIGH / LOW |
| `b04` / `s06` | stg_customers column list | dedup dropped vs. columns reordered | HIGH / LOW |
| `s09` / `s10` | external consumers break, DAG fine | schema move vs. alias | MEDIUM / MEDIUM |

## Severity vocabulary

Matches `report.assess`:

- **high** — breaks a downstream consumer, or silently corrupts data, on merge
- **medium** — risky, warrants review, nothing breaks immediately
- **low** — no structural change, or no consumer can observe it

## Known label judgments worth arguing with

Honest disclosure of the calls most likely to be contested:

- **`s07_test_removed` = MEDIUM.** Removing `not_null` from a primary key breaks nothing
  today; it removes future detection. Defensible as LOW.
- **`s05_public_access_added` = MEDIUM.** The model was already public; only the group
  changes. Defensible as LOW.
- **`s02_incremental_no_full_refresh` = MEDIUM.** The multiplier may be exactly what the
  author intended; the missing full refresh is the actual defect. Defensible as HIGH
  given the data ends up internally inconsistent.
- **`b10_source_column_dropped` = HIGH.** Whether this breaks depends on whether the
  upstream table really lost the column or only its declaration did.
