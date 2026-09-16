# How dbt-sentinel works

A walkthrough of what this tool does and how the pieces fit, for someone reading the
repo for the first time.

> Looking for *why* it is built this way — the decisions, the alternatives that were
> rejected, the trade-offs? That is [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), which
> carries the ADRs. This file is the *what* and the *how*.

---

## What it does, in one paragraph

A dbt project is a graph of SQL models. Renaming a column in one model can break every
model downstream of it, because dbt does not rewrite references — the next run just
fails, or worse, succeeds with wrong numbers. dbt-sentinel reads a pull request diff,
works out which dbt models it touched, walks the dependency graph to see what those
models feed, and posts a review saying how risky the change is and why.

## The workflow

```
   ┌─────────────┐
   │  PR opened  │
   └──────┬──────┘
          │  diff + manifest.json
          ▼
   ┌─────────────────────────────────────────────┐
   │ 1. PARSE      which files changed?          │  diff.py
   │ 2. RESOLVE    which dbt models are those?   │  diff.py + lineage.py
   │ 3. TRAVERSE   what depends on them?         │  lineage.py
   │ 4. SCORE      how risky is it?              │  report.py
   │ 5. RETRIEVE   which policies apply?         │  retrieval.py
   └──────────────────────┬──────────────────────┘
                          │  ← everything above is deterministic
                          ▼
   ┌─────────────────────────────────────────────┐
   │ 6. JUDGE      what would a reviewer say?    │  agent.py  (optional)
   └──────────────────────┬──────────────────────┘
                          ▼
   ┌─────────────────────────────────────────────┐
   │ 7. RENDER     build the comment             │  report.py
   │ 8. POST       comment + pass/fail check     │  github.py
   └─────────────────────────────────────────────┘
```

Steps 1–5 give the same answer every time, for the same inputs. No network, no
randomness. Step 6 is the only part that calls an LLM, and it is optional — with no API
key the tool still produces steps 1–5 and says in the comment that the agent did not run.

## The two inputs

**The diff** — a normal unified diff, from `git diff` or the GitHub API.

**`target/manifest.json`** — produced by `dbt compile`. This is the important one: it is
dbt's own description of the project, containing every model, its file path, its columns,
its config, and a `child_map` listing what depends on what. The whole dependency graph is
already in there, which is why the tool never has to parse SQL to find relationships.

## Walking through a real example

This is an actual run, not a sketch. The fixture renames `customer_id` to `cust_id` in a
staging model:

```diff
     select
         id as order_id,
-        user_id as customer_id,
+        user_id as cust_id,
```

### Step 1 — Parse the diff

`diff.py` reads the diff into changed files, keeping the added lines, removed lines, and
the surrounding context lines.

```
[('stg_orders.sql', 'modified')]
```

Context lines matter later: in a shared `schema.yml` that documents five models, a
`- name: customer_id` line on its own is unattributable. The unchanged lines around it
are what say which model's column list it belongs to.

### Step 2 — Resolve files to dbt models

`lineage.py` matches the file path against the manifest.

```
models/staging/stg_orders.sql  →  model.jaffle_shop.stg_orders
```

Two subtleties here. A path is matched against **both** `original_file_path` (the `.sql`
that defines a model) and `patch_path` (the `schema.yml` that documents it), because
editing either affects the model. And separators are normalised first — a manifest
compiled on Windows stores `models\staging\stg_orders.sql` while diffs always use forward
slashes, so a raw comparison matches nothing at all.

### Step 3 — Work out what changed about it

```
removed columns: ('customer_id',)
added columns:   ('cust_id',)
structural:      True
```

"Structural" is the key flag: it means the change alters something other models depend
on. A removed column, a deletion, a rename. A comment or a whitespace edit is not
structural.

### Step 4 — Traverse the graph

`lineage.py` does a breadth-first search over `child_map`:

```
stg_orders (changed)
├── customers              depth 1
├── fct_order_payments     depth 1   ← has an enforced contract
├── orders                 depth 1
├── rpt_customer_revenue   depth 2   ← public
├── customer_success_churn depth 2   ← exposure (dashboard)
├── finance_month_end      depth 2   ← exposure
└── exec_dashboard         depth 3   ← exposure

7 downstream · 3 exposures · 1 contracted
```

dbt *tests* are excluded from this walk. Tests are children of every model they cover, so
counting them makes a well-tested model look like it has twelve consumers.

### Step 5 — Score the severity

`report.py` combines what changed with what it reaches:

```
removed column + 7 downstream + reaches a contract + reaches 3 exposures  →  HIGH
```

**The rule that matters most:** a structural change *triggers* severity, and reach only
*amplifies* it. Reach alone never triggers. Nearly every staging model sits upstream of a
dashboard, so if proximity to a dashboard were enough to score HIGH, every PR would score
HIGH and people would stop reading the comments within a week.

Concretely, from the eval fixtures — same file, same 7 downstream models:

| Change | Severity |
|---|---|
| Rename `customer_id` → `cust_id` | HIGH — consumers break |
| Add a comment | LOW — nothing changes |

(There is a third case that *should* belong in this table and does not: adding a new column currently also scores HIGH, which is wrong — see **Honest limits** below.)

### Step 6 — Retrieve the policies that apply

`retrieval.py` searches a pack of 14 governance rules in [policies/](policies/) in two
stages:

1. **Filter structurally.** Each rule declares what it applies to — node kinds, path
   globs, config requirements. A rule about enforced contracts is eliminated outright for
   a model that has no contract. This is a glob match, not a guess.
2. **Rank what survives** by keyword and text similarity against a summary of the change.

```
contract-breaking-change (high) score=0.495
  matched keywords: dropped column, removed column
5 rules eliminated by the prefilter
```

Only the rule pack is searchable text. The manifest, the graph and the SQL are never fed
to a search index — those questions have exact answers.

### Step 7 — Judge (optional, needs an API key)

The LLM gets the analysis above, plus three tools it can call for facts:
`get_lineage`, `get_policies`, `get_columns`. It returns **structured findings** —
`rule_id`, `severity`, `model`, `explanation`, `suggested_fix` — validated against a
schema before anything is rendered.

The LLM never writes the comment. If it fails for any reason — no key, timeout, rate
limit, invalid output — the comment is still produced from steps 1–5, with a visible note
saying the agent did not run.

### Step 8 — Render and post

`report.py` builds the Markdown from a template. `github.py` posts it and sets a commit
status: **HIGH fails the check, everything else passes.**

Re-reviews update the existing comment rather than adding a new one, so a ten-push PR
does not end up with ten stale reviews.

## The output

```
## Blast radius

### 🔴 `stg_orders` — HIGH · 7 downstream
- Removed column(s) `customer_id` with 7 downstream node(s)
- Reaches 1 contracted model(s): fct_order_payments
- Reaches 3 exposure(s): customer_success_churn, finance_month_end, exec_dashboard
- Downstream: `customers`, `fct_order_payments`, `orders`, ... +1 more
```

Exit code `1`, so it works as a CI gate.

## Try it yourself

The repo ships a real manifest and 30 diffs, so this runs with no setup:

```bash
pip install -e ".[dev]"

# Windows only: the severity icons are emoji, and a stock terminal's cp1252 encoding
# cannot print them, which crashes the process before it gets to scoring anything.
# This one line fixes it for the session:
export PYTHONIOENCODING=utf-8

# a breaking change — exits 1
python -m dbt_sentinel \
  --manifest evals/manifest/manifest.json \
  --diff evals/fixtures/b01_column_rename_with_consumers.diff \
  --fail-on high

# the same model, a comment-only change — exits 0
python -m dbt_sentinel \
  --manifest evals/manifest/manifest.json \
  --diff evals/fixtures/p01_comment_added.diff \
  --fail-on high

# add --explain to see which policies matched and why
# add --mermaid for a blast-radius diagram
```

On your own project:

```bash
dbt compile
git diff origin/main...HEAD > pr.diff
python -m dbt_sentinel --manifest target/manifest.json --diff pr.diff --fail-on high
```

## The files

| File | Does |
|---|---|
| `models.py` | The core types: `Node`, `ChangedNode`, `BlastRadius` |
| `diff.py` | Reads the diff; maps files to models; extracts columns |
| `lineage.py` | Reads the manifest; walks the graph; checks freshness |
| `report.py` | Scores severity; renders Markdown and Mermaid |
| `retrieval.py` | Loads the policy pack; filters and ranks rules |
| `agent.py` | The LLM loop, its tools, and its schema validation |
| `pipeline.py` | Ties it together: a PR in, a posted review out |
| `github.py` | Auth, fetching the diff, posting the comment and status |
| `cli.py` | The command-line entry point |
| `retry.py` | Backoff for transient API failures |
| `pricing.py` | Token costs, so the comment can report what it spent |

## The one design idea worth taking away

**Everything that can be computed exactly, is.** Which models changed, what depends on
them, which policies could possibly apply — all answered by string matching and graph
traversal, which are fast, free, and identical every run.

The LLM is only asked the questions that have no exact answer: does this `left join`
becoming an `inner join` change the grain? Does this contract edit widen safely or narrow
dangerously?

That line is why the tool still works with no API key, why its numbers are reproducible,
and why swapping the entire LLM layer from one provider to another changed none of the
published metrics.

## Honest limits

The tool reports which *models* are downstream, not which of them actually use the
changed column. Column extraction is a regex, so it misses `select *` and
macro-generated columns. Four known cases produce no output at all.

**Additive columns currently score too high.** The “reach amplifies, does not trigger” rule above is the intended design, but the code that decides what counts as a triggering change is broader than it should be: adding a new column presently scores the same HIGH as removing one. That is a real, measured false positive, tracked in [ROADMAP.md](ROADMAP.md) and left unfixed on purpose — the eval baseline was established first so a fix can be shown to actually improve it, rather than being tuned against itself.

All measured and written down in [evals/BASELINE.md](evals/BASELINE.md) and
[docs/EVAL_REPORT.md](docs/EVAL_REPORT.md), including the numbers that look bad:
**precision 0.800, recall 0.533.** It catches about half of what a careful reviewer would,
and when it does fire it is usually right.
