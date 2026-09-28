# Architecture

How dbt-sentinel works, then why it's built that way. The walkthrough is for someone
reading the repo for the first time; the ADRs record each decision with the alternatives
that were rejected.

## What it does, in one paragraph

A dbt project is a graph of SQL models. Renaming a column in one model can break every
model downstream of it, because dbt doesn't rewrite references. The next run just fails,
or worse, succeeds with wrong numbers. dbt-sentinel reads a pull request diff, works out
which dbt models it touched, walks the dependency graph to see what those models feed,
and posts a review saying how risky the change is and why.

## Shape

```
GitHub webhook (or `dbt-sentinel --diff / --since` locally)
  |  signature verified: HMAC-SHA256, constant time, raw body
  v
+-- DETERMINISTIC: exact, no network, no randomness -----------------------+
|  diff.py        unified diff -> changed files -> dbt nodes               |
|  lineage.py     manifest.json -> node graph -> BFS blast radius          |
|  report.py      structural change x reach -> severity                    |
|  security.py    exposed credentials, any file                            |
|  checks.py      five lint checks on added lines                          |
|  retrieval.py   prefilter -> TF-IDF over 14 governance rules,            |
|                 plus a 7-rule SQL checklist that is never ranked         |
+---------------------------------------------------------------------------+
  |  assessments + rules
  v
+-- JUDGMENT: LLM (gpt-4o), tool-calling, optional -------------------------+
|  agent.py       tools: get_lineage, get_policies, get_columns,           |
|                 get_model_sql; returns validated Findings, never prose   |
+---------------------------------------------------------------------------+
  |  list[Finding]
  v
report.py   deterministic template -> Markdown
github.py   comment (upserted) + commit status: HIGH or a secret fails it
```

The split down the middle is the whole design. Everything above the line is reproducible
from its inputs; everything below is a judgment call that structure can't answer. The
boundary is enforced by types: the agent receives `Assessment` objects and returns
`Finding` objects, and has no path to the renderer.

## The two inputs

**The diff:** a normal unified diff, from the GitHub API, a file, or `git diff` via
`--since`.

**`target/manifest.json`:** produced by `dbt parse` or `dbt compile`. It's dbt's own
description of the project: every model, its file path, columns, declared types, tests,
config, and a `child_map` of what depends on what. The whole dependency graph is already
there, which is why the tool never parses SQL to find relationships.

## Walking through a real example

An actual run. The fixture renames `customer_id` to `cust_id` in a staging model:

```diff
     select
         id as order_id,
-        user_id as customer_id,
+        user_id as cust_id,
```

**1. Parse.** `diff.py` reads the diff into changed files, keeping added, removed and
context lines. Context matters: in a shared `schema.yml`, a `- name: customer_id` line is
only attributable through the unchanged lines around it.

**2. Resolve.** The path is matched against both `original_file_path` (the `.sql`) and
`patch_path` (the `schema.yml`), since editing either affects the model. Separators are
normalised first: a manifest compiled on Windows stores `models\staging\stg_orders.sql`,
and a raw comparison with a diff's forward slashes matches nothing.

**3. What changed.** `customer_id` removed, `cust_id` added, so it's **structural**. A
removed column, a deletion or a rename is structural. A comment, whitespace, or a purely
*added* column is not: `select *` picks up a new column and an explicit select ignores it,
so no consumer breaks.

**4. Traverse.** A breadth-first search over `child_map`:

```
stg_orders (changed)
├── customers              depth 1
├── fct_order_payments     depth 1   ← enforced contract
├── orders                 depth 1
├── rpt_customer_revenue   depth 2   ← public
├── customer_success_churn depth 2   ← exposure (dashboard)
├── finance_month_end      depth 2   ← exposure
└── exec_dashboard         depth 3   ← exposure

7 downstream · 3 exposures · 1 contracted
```

dbt *tests* are excluded from the walk. Tests are children of every model they cover, so
counting them would make a well-tested model look like it has a dozen consumers.

**5. Score.** A structural change *triggers* severity, and reach only *amplifies* it
(ADR-002). Same file, same 7 downstream nodes:

| Change | Severity |
|---|---|
| Rename `customer_id` → `cust_id` | HIGH: consumers break |
| Add a comment | LOW: nothing changes |
| Add a new column | LOW: additive, nothing breaks |

**6. Security and checks.** `security.py` scans the added lines of *every* file for
credentials; any hit fails the status (ADR-005). `checks.py` runs five advisory lint
checks, such as a hardcoded `schema.table` or a `= null` comparison. Neither is folded
into severity: a lint finding has no reach. See [CHECKS.md](CHECKS.md).

**7. Retrieve.** Each governance rule declares what it applies to (node kinds, path
globs, config such as `materialization: incremental`), so ineligible rules are eliminated
by the prefilter. The survivors are ranked by keyword and TF-IDF similarity against a
summary of the change, and the top three are cited. The seven SQL checklist rules are
given to the agent for every SQL change without being ranked (ADR-006).

**8. Judge (optional).** The agent gets the analysis plus four tools for facts. It
returns findings (`rule_id`, `severity`, `model`, `explanation`, `suggested_fix`), which
are validated against a schema before anything is rendered. With no key, or on any
failure, the review is still posted, with a note saying the agent didn't run.

**9. Render and post.** `report.py` builds the Markdown from a template; `github.py`
upserts one comment per PR (a ten-push PR doesn't collect ten reviews) and sets the
status.

Steps 1 to 7 are identical on every run. Only step 8 varies.

## Modules

| Module | Responsibility | Depends on |
|---|---|---|
| `models.py` | Domain types: `Node`, `ChangedFile`, `ChangedNode`, `BlastRadius` | stdlib |
| `lineage.py` | Manifest parsing (v7-v14), graph, BFS, tests per column, freshness | `models` |
| `diff.py` | Diff parsing, file-to-node resolution, column extraction | `lineage`, `models` |
| `report.py` | Severity scoring; Markdown, Mermaid and finding rendering | `lineage`, `models` |
| `checks.py` | Five deterministic lint checks on added lines | `lineage`, `models` |
| `security.py` | Exposed-secret scan across every file; redacted findings | `models` |
| `retrieval.py` | Policy pack, prefilter, TF-IDF, checklist rules, disk cache | `models`, `pyyaml` |
| `agent.py` | LLM tool-calling loop, schema validation, degradation | all above, `openai`, `pydantic` |
| `pipeline.py` | PR ref to posted review | all above |
| `github.py` | App JWT, installation token, reads, comment and status writes | `retry` |
| `webhook.py` | Signature verification, event routing | `pipeline`, `github` |
| `cli.py` | Local entrypoint: `--diff` / `--since`, exit codes | all above |
| `retry.py` | Bounded backoff for transient upstream failures | stdlib |
| `pricing.py` | Token prices; one source for the comment footer and the evals | stdlib |

Dependencies point one way: `models` knows nothing, `agent` knows everything, and no
module imports the eval harness.

---

# ADR-001: Graph traversal over vector retrieval for lineage

**Context.** The tool must know what's downstream of a changed model. The manifest is a
~700KB JSON document that contains the full dependency graph.

**Decision.** Parse `child_map` into an adjacency dict and BFS it. No embeddings, no
`networkx`.

**Rejected: embed the manifest and let a model reason over the graph.** Wrong on every
axis. *Accuracy:* reachability is an exact question; nearest-neighbour retrieval returns
*plausible* neighbours, and an approximately right blast radius is a confident lie.
*Cost:* the BFS is microseconds and free. *Debuggability:* when a BFS is wrong you read
`child_map`; when retrieval is wrong you have a similarity score and no recourse.

**Rejected: `networkx`.** A dependency to do what 15 lines of `deque` do.

**Consequence.** Design rule 1: if graph traversal or string parsing can answer it, they
do. The LLM is for judgment, not lookup.

---

# ADR-002: A structural change triggers severity; reach only amplifies

**Context.** The first scorer treated proximity to an exposure as risk, and a
comment-only edit to a staging model upstream of a dashboard scored HIGH.

**Decision.** Severity is triggered only by a structural change: a removed or renamed
column, a deletion, a contract edit. Reach decides how loud, never whether.

**Rejected: reach as an independent risk factor.** Nearly every staging model sits
upstream of a dashboard, so almost every PR would score HIGH. A reviewer who sees HIGH on
a whitespace diff learns the badge is noise, and ignores the next real one.

**Rejected: a weighted score of reach, coverage and change size.** Untunable without
ground truth; any number it produced would be unfalsifiable.

**Consequence.** Pinned by regression tests, and the eval suite pairs `b01` (rename,
HIGH) with `p10` (additive column, LOW) on the same model with the same reach. The
predicate was first too broad (added columns counted as structural, a 0.200
false-positive rate); the day-7 fix brought it to **0.000**.

---

# ADR-003: Structured output with deterministic rendering

**Decision.** The LLM calls a `submit_findings` tool and returns objects validated
against a Pydantic schema. Rendering is template code. Model strings are flattened to one
line, and `rule_id` / `model` reject backticks, pipes and newlines. A cited `rule_id` that
isn't in the policy pack is flagged in the comment rather than rendered as policy.

**Rejected: let the model write the comment.** *Injection:* a model that writes Markdown
can forge a heading or a severity badge in someone's PR. *Consistency:* the comment is a
product surface. *Measurability:* you can't compute precision over prose.

**Rejected: JSON in a text response, parsed with a regex.** It fails as a half-parsed
finding rather than a clean error.

---

# ADR-004: Degrade to deterministic-only on LLM failure

**Decision.** Every failure (no key, timeout, rate limit, 5xx, invalid schema twice,
prose instead of a tool call, an endless tool loop) returns a degraded result carrying
its reason, and the comment says so. `review()` never raises. Transient failures retry
with bounded backoff; permanent ones fail at once.

**Rejected: fail the whole review.** The structural analysis was already correct and
free.

**Rejected: degrade silently.** A reader who can't tell the agent ran reads "no findings"
as "nothing to worry about" when it means "the API timed out".

**Rejected: retry indefinitely.** GitHub redelivers webhooks that don't answer, so
unbounded retries post the review twice.

---

# ADR-005: An exposed secret fails the status, whatever the reach

**Context.** Checks are capped at MEDIUM and never affect the status, because a lint
finding costs something only if merged.

**Decision.** Secret scanning is its own module and type, not a check. Any exposed
credential fails the status and exits 1 at every `--fail-on` threshold. Values are
redacted in the comment, and the scan runs even when no manifest is available.

**Why.** A credential is compromised when it's *pushed*: it's in git history, forks and
CI logs before anyone merges. Reach is irrelevant, and so is the merge. Keeping it out of
`CHECKS` keeps the checks' MEDIUM cap an invariant rather than a rule with one exception.

**Rejected: an LLM-based secret check.** Credential formats are exact patterns, and a
missed secret is the costliest miss the tool can make.

---

# ADR-006: The SQL checklist is never ranked by retrieval

**Context.** Retrieval ranks governance rules against a summary of *what kind* of change
happened. The SQL checklist (join-key uniqueness, nulls, collation, types, query shape,
column-name spelling, SQL security) applies to *every* SQL change.

**Decision.** Checklist rules carry `applies_as: checklist` and live outside the TF-IDF
index entirely. The agent gets every checklist rule the prefilter admits.

**Why outside the index, not just unranked.** IDF weights are computed across the indexed
rules, so a checklist rule merely *present* would shift every governance rule's score and
move the published retrieval metrics. A test pins the scores as identical with and
without the checklist loaded.

---

## Known limits

| Limit | Consequence |
|---|---|
| Model-level lineage only | Reports 7 downstream models, not which of them select the dropped column |
| Regex column extraction | Misses `select *`, CTE aliases, macro-generated columns |
| A removed column in a shared `schema.yml` | No provable owner; reported as an explicit medium uncertainty (the sole remaining false positive, `s03`) |
| Lexical retrieval | Can't match a paraphrase |
| Macro changes | Warned, not resolved to the models that use them |
| Manifest assumed current | Warned when it predates the change; can't be fixed from the diff |
| Single project | No cross-project `ref()` resolution |
| The SQL checklist | Plumbed and tested, but measured at precision 0.000 on its first live run ([evals/SQL_POLICY_RESULTS.md](../evals/SQL_POLICY_RESULTS.md)) |
