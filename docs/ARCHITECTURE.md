# Architecture

## Shape

```
GitHub webhook  -->  signature verified (HMAC-SHA256, constant time, raw body)
                         |
    +--------------------+--------------------+
    |  DETERMINISTIC -- exact, no network, no randomness
    |
    |  diff.py        unified diff --> changed files --> nodes
    |  lineage.py     manifest.json --> node graph --> BFS blast radius
    |  report.py      structural change x reach --> severity
    |  retrieval.py   applies_to prefilter --> TF-IDF over 14 rules
    +--------------------+--------------------+
                         |  assessments + retrieved rules
    +--------------------+--------------------+
    |  JUDGMENT -- Claude, tool-calling
    |  agent.py       tools: get_lineage, get_policies, get_columns
    |                 returns validated Finding objects, never prose
    +--------------------+--------------------+
                         |  list[Finding]
    report.py            deterministic template --> Markdown
    github.py            comment (upserted) + commit status
```

The split down the middle is the whole design. Everything above the line is reproducible
from inputs alone; everything below is a judgment call that structure cannot answer. The
boundary is enforced by types: the agent receives `Assessment` objects and returns
`Finding` objects, and has no path to the renderer.

## Modules

| Module | Responsibility | Depends on |
|---|---|---|
| `models.py` | Domain types: `Node`, `ChangedFile`, `ChangedNode`, `BlastRadius` | stdlib |
| `lineage.py` | Manifest parsing (v7-v14), node graph, BFS, freshness | `models` |
| `diff.py` | Unified diff parsing, file-to-node resolution, column extraction | `lineage`, `models` |
| `report.py` | Severity scoring, Markdown + Mermaid, finding rendering | `lineage`, `models` |
| `retrieval.py` | Policy pack loading, prefilter, TF-IDF, disk cache | `models`, `pyyaml` |
| `agent.py` | Claude tool-calling loop, schema validation, degradation | all above, `anthropic`, `pydantic` |
| `pipeline.py` | PR ref to posted review; shared by webhook and CLI | all above |
| `github.py` | App JWT, installation token, diff/file reads, comment/status writes | `retry` |
| `retry.py` | Bounded backoff for transient upstream failures | stdlib |
| `pricing.py` | Token prices; one source for comment footer and eval report | stdlib |
| `webhook.py` | Signature verification, event routing | `pipeline`, `github` |
| `cli.py` | Local entrypoint: flags, exit codes, `--explain` / `--agent` | all above |

Dependency direction is one-way: `models` knows nothing, `agent` knows everything. No
module imports the eval harness — that was a real defect on day 8, fixed on day 9.

## Data flow, concretely

A PR renaming `stg_orders.customer_id` to `cust_id`:

1. **Diff parse.** `models/staging/stg_orders.sql` modified; removed line contains
   `user_id as customer_id`, added line `user_id as cust_id`.
2. **Resolve.** Path matched against `original_file_path` *and* `patch_path`, separators
   normalised, giving `model.jaffle_shop.stg_orders`.
3. **Columns.** Regex extracts `customer_id` removed, `cust_id` added.
4. **Traverse.** BFS over `child_map`, test nodes excluded: 7 downstream, 3 exposures,
   1 contracted.
5. **Score.** A removed column *is* structural, so HIGH, amplified by reach.
6. **Retrieve.** Prefilter keeps rules whose `applies_to` matches a view model in
   `models/staging/`; TF-IDF ranks `contract-breaking-change` first at 0.495.
7. **Judge** (optional). The agent gets the assessment and the rule, calls `get_lineage`
   to confirm consumers, returns one `Finding`.
8. **Render.** Template emits Markdown. Comment upserted, status set to `failure`.

Steps 1-6 are identical on every run. Only step 7 varies.

---

# ADR-001: Graph traversal over vector retrieval for lineage

**Status:** accepted, day 2. Never revisited.

**Context.** The tool must know what is downstream of a changed model. The manifest is a
~700KB JSON document containing the full dependency graph.

**Decision.** Parse `child_map` into an adjacency dict and BFS it. No embeddings, no
`networkx`.

**Rejected: embed the manifest and let the model reason over the graph.** This was the
obvious 2024-era move and it is wrong on every axis. *Accuracy* — reachability is an exact
question with an exact answer; nearest-neighbour retrieval over graph text returns
*plausible* neighbours, and a blast radius that is approximately right is a confident lie.
*Cost* — the BFS is microseconds and free; embedding 700KB per review costs money per PR
to get a worse answer. *Debuggability* — when a BFS is wrong you read the `child_map`;
when a retrieval is wrong you have a similarity score and no recourse.

**Rejected: `networkx`.** A dependency to do what 15 lines of `deque` does. The manifest
already ships the adjacency list.

**Consequence.** Lineage is exact and free, and this became design rule 1: *if a question
can be answered by graph traversal or string parsing, answer it that way.* Two later
temptations were refused by pointing at it.

**Cost of being wrong.** If a graph were genuinely too large to traverse per review, the
fix would be caching, not retrieval. No plausible dbt project is that large.

---

# ADR-002: Structural change triggers severity; reach only amplifies

**Status:** accepted, day 2, after a false positive. The most important decision here.

**Context.** The first scorer treated proximity to an exposure as risk. A comment-only edit
to a staging model upstream of a dashboard scored HIGH.

**Decision.** Severity is triggered *only* by a structural change — removed or renamed
column, deletion, contract edit, grain change. Reach decides how loud, never whether.

**Rejected: reach as an independent risk factor.** Nearly every staging model in a real
project sits upstream of a dashboard, so this makes almost every PR HIGH. The failure is
not statistical but behavioural: a reviewer who sees HIGH on a whitespace diff learns the
badge is noise, and the next *real* HIGH is ignored too. A tool that cries wolf has
negative value — it consumes attention and trains people to discount its only useful
signal.

**Rejected: a weighted risk score combining reach, test coverage and change size.** More
expressive and untunable — with no ground truth on day 2 the weights would have been
invented, and any number it produced would have been unfalsifiable.

**Consequence.** Became design rule 2. Encoded as a permanent regression test
(`test_comment_only_change_is_not_high`), and the eval suite deliberately pairs `b01`
(rename, HIGH) with `p10` (additive column, LOW) on the *same* model with the *same* reach,
so a regression to reach-based scoring fails visibly.

**Where it is still wrong.** `p10` currently scores HIGH. `is_structural` counts *added*
columns as structural, so an additive change trips the trigger. The principle is right and
the predicate is too broad — measured at FPR 0.200 and deliberately unfixed until day 7.

---

# ADR-003: Structured output with deterministic rendering

**Status:** accepted, day 5.

**Context.** The reviewer agent must produce a PR comment. The simple path is to ask for
Markdown and post it.

**Decision.** The LLM calls a `submit_findings` tool and returns objects validated against
a Pydantic schema. Rendering is template code. Model-supplied strings are flattened to one
line, and `rule_id` / `model` reject backticks, pipes and newlines.

**Rejected: let the model write the comment.** Three problems. *Injection* — a model that
writes Markdown can write a link, a fake heading, or an authoritative-looking severity
badge into a comment carrying the project name; a compromised or confused model becomes a
posting channel. *Consistency* — the comment is a product surface, and a model that
formats differently each run makes it unscannable. *Measurability* — you cannot compute
precision over prose, so the entire eval suite depends on findings being structured.

**Rejected: JSON in a text response, parsed with a regex.** Works until it does not, and
the failure mode is a half-parsed finding rather than a clean error. Tool-calling gives a
schema the API itself enforces.

**Consequence.** The eval suite is possible at all. Escaping is tested by attempting the
injection (`test_model_cannot_inject_markdown_through_rule_id`). The cost is that the model
cannot express anything the schema lacks, which is the intended trade.

---

# ADR-004: Degrade to deterministic-only on LLM failure

**Status:** accepted, day 5.

**Context.** The agent can fail in many ways: no key, timeout, rate limit, 5xx, invalid
schema, prose instead of a tool call, an endless tool loop.

**Decision.** Every failure path returns a degraded result carrying its reason, and the
comment says so. `review()` never raises. Transient failures retry with bounded backoff
first; permanent ones fail immediately. The deterministic analysis is posted regardless.

**Rejected: fail the whole review.** The structural analysis — blast radius, severity,
retrieved rules — was already computed correctly and costs nothing. Throwing it away
because an optional enrichment failed is strictly worse for the reviewer.

**Rejected: degrade silently.** The tempting option, and the dangerous one. A reader who
cannot tell the agent ran will assume it did, and read "no findings" as "nothing to worry
about" when it means "the API timed out". Silence about degradation converts a partial
outage into a false reassurance.

**Rejected: retry indefinitely.** GitHub redelivers webhooks that do not answer, so
unbounded retries earn a second delivery and post the review twice. Every wait is capped.

**Consequence.** Became design rule 5. Nine separate degradation tests, and the `--agent`
path is verified end-to-end with no key present. The CI gate is driven by deterministic
severity, so agent failure cannot weaken it.

---

## Known architectural limits

| Limit | Consequence |
|---|---|
| Model-level lineage only | Reports 12 downstream models, not which select the dropped column |
| Regex column extraction | Misses `select *`, CTE aliases, macro-generated columns |
| YAML attribution keyed on `columns:` | A `data_type` or `owner:` edit resolves to nothing — 4 fixtures silently dropped |
| `is_structural` counts additions | Additive columns score as breaking — FPR 0.200 |
| Lexical retrieval | Cannot match a paraphrase |
| Manifest assumed current | Stale manifest shrinks blast radius; warned, not fixed |
| Single-project | No cross-project `ref()` resolution |

The middle two are the top failure modes. Both are measured, both are parked in
[ROADMAP.md](../ROADMAP.md), and both were left unfixed on purpose so the day-6
before/after comparison would mean something.
