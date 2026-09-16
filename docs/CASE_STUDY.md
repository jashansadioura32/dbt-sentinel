# dbt-sentinel — a case study

**A blast-radius reviewer for dbt pull requests, built in 10 days as a public artifact.**
The goal was a defensible engineering record, not a product — so the measurements are
published whatever they say, and the two worst failures are named before the successes.

| | |
|---|---|
| **Repo** | [github.com/&lt;you&gt;/dbt-sentinel](https://github.com/<you>/dbt-sentinel) |
| **Stack** | Python 3.10+, one runtime dependency (`pyyaml`), Claude for judgment only |
| **Scale** | 12 modules, 130 tests, 30 labelled eval fixtures, 14 governance rules |
| **Docs** | [PRD](PRD.md) · [Architecture + ADRs](ARCHITECTURE.md) · [Eval report](EVAL_REPORT.md) · [Deployment](DEPLOYMENT.md) |

---

## The problem

A dbt project is a graph of SQL models. Renaming a column in a staging model is one line in
a diff and can break twelve dashboards, because dbt does not rewrite references — the next
run fails, or worse, succeeds with wrong numbers.

The reviewer looking at that diff sees one line. Seeing the twelve consumers means walking
the DAG by hand, so nobody does, and breaking changes ship. The information needed to
prevent it already sits in `target/manifest.json`; nothing puts it in front of the reviewer
at the moment they decide.

## What it does

On every PR: maps the diff to changed models, computes the downstream blast radius by graph
traversal, retrieves the governance rules that apply, and posts a structured review with
severity, affected models, and a suggested fix. Sets a commit status that blocks on HIGH.

```
### 🔴 `stg_orders` — HIGH · 7 downstream
- Removed column(s) `customer_id` with 7 downstream node(s)
- Reaches 1 contracted model(s): fct_order_payments
- Reaches 3 exposure(s): customer_success_churn, finance_month_end, exec_dashboard
```

## Results

Full detail and methodology in the [eval report](EVAL_REPORT.md).

| Metric | Value | Target |
|---|---|---|
| Precision | 0.800 | > 0.75 — met |
| Recall | 0.533 | > 0.70 — **missed** |
| False-positive rate on routine PRs | 0.200 | < 0.10 — **missed, 2x** |
| Retrieval precision@3 | 0.708 | > 0.60 — met |
| Agent vs. baseline | **unmeasured** | needs an API key |
| Deployed to a real PR | **not done** | wired, never deployed |

Two of six targets missed, two unmeasured, one deliverable incomplete. Recall 0.533 at
precision 0.800 is the honest shape of a structural-only scorer: when it fires it is
usually right, and it misses more than half of what a reviewer should catch.

## Four decisions worth defending

Each is an [ADR](ARCHITECTURE.md) with the rejected alternative and why.

**Lineage is graph traversal, not retrieval.** The manifest ships `child_map`. Embedding a
dependency graph and asking a model to reason over it would be slower, costlier, and less
correct than a BFS — reachability is an exact question, and an approximately-right blast
radius is a confident lie. The LLM is for judgment, not lookup.

**Reach amplifies risk, it does not create it.** Nearly every staging model sits upstream of
a dashboard. The first scorer treated that proximity as risk and gave a comment-only edit a
HIGH. The failure is behavioural, not statistical: a reviewer who sees HIGH on whitespace
learns the badge is noise, and the next real HIGH is ignored too. Severity is triggered by
structural change; reach only decides how loud.

**The LLM never writes the comment.** It returns schema-validated `Finding` objects and
rendering is template code. A model that writes Markdown can forge a heading or a severity
badge inside a comment carrying the project's name — and you cannot compute precision over
prose, so the eval suite depends on structure.

**Degrade visibly, never silently.** Nine failure paths — no key, timeout, rate limit,
invalid schema twice, prose instead of a tool call — each degrade to deterministic-only
*with the reason in the comment*. A reader who cannot tell the agent ran will assume it did,
and read "no findings" as "nothing to worry about" when it means "the API timed out".

## What the measurements caught

The eval suite existed before the agent, and it earned that ordering immediately.

**A Windows path bug that made the tool a silent no-op.** A manifest compiled on Windows
stores `models\staging\x.sql`; diffs use forward slashes. Raw comparison resolved
*nothing*, so the CLI reported a clean review on a breaking PR and `--fail-on high` exited
0. The synthetic fixture used forward slashes and hid it completely — it surfaced the first
time the tool met a real compiled manifest.

**A retrieval noise defect.** Precision@3 started at 0.400 with correct silence 0.273: the
query was built from the node's *ambient* attributes, so `stg_` matched a staging rule on
every staging file. Those are path facts, not evidence of a violation. Fixed to 0.708.

**A cost constant that would have inflated every published figure by ~50%** — Sonnet 4.6's
rates carried over onto a Sonnet 5 pin.

**Three of my own tests that passed for the wrong reason:** one grepping a module's source
text for a string, one patching `sleep` on the wrong module and sitting through real
backoff while appearing mocked, and a meta-test that ran pytest inside pytest and spawned
54 processes. All three are the same error — a test that appears to control its environment
and does not.

## Process decisions that shaped the outcome

**Labels before the runner.** All 30 fixtures were labelled from dbt semantics before the
scoring code existed. Writing the runner first produces a suite that confirms whatever the
code already does. Any label that changes is argued in `LABEL_CHANGES.md`; it is empty.

**Measure and fix in separate sessions.** The two top failure modes are diagnosed to the
specific predicate and deliberately *not* fixed — fixing them in the measuring session
destroys the before/after comparison that makes the fix demonstrable.

**Zero-resolution fixtures are errors, not passes.** Counting "found nothing" as "correctly
found nothing" is how an eval suite flatters a broken resolver. This caught the defect that
is now failure mode #1.

**A non-goals list written before any code.** The highest-value twenty minutes of the
project: every later scope argument resolved by pointing at it, and the rejected ideas went
to `ROADMAP.md` instead of into the build.

## The two worst failures

Named here rather than left for a reader to find.

**A dropped contract column produces no output at all.** Four fixtures resolve to a file,
match real nodes, then get discarded because the YAML attribution only tracks `- name:`
under a `columns:` key. Two are HIGH. Silence reads as approval, which makes this the worst
failure shape available.

**Additive columns score as breaking.** `is_structural` counts added columns alongside
removed ones, so adding `loaded_at` scores the same HIGH as renaming `customer_id` — same
model, same reach, opposite semantics. This drives the 0.200 FPR, one routine PR in five.

## What is incomplete

- **The agent is unmeasured.** Its plumbing has 31 tests, all against a fake client. Review
  quality is unknown. One command and an API key would close it.
- **Never deployed.** The GitHub App is built and tested against a fake client; no real PR
  has received a comment.
- **Days 6 and 7 not run.** The comparison harness exists; the measurement and the one
  iteration round did not happen.

## If I did it again

**The eval suite earned its place before anything else.** Every real defect came from
measuring or from running a documented command literally, never from reading code.

**I would test against a real artifact on day 1, not day 3.** The synthetic manifest passed
every test while hiding a bug that made the tool a total no-op on Windows. A fixture you
authored to match your own assertions is the most self-confirming thing in a project.

**I under-weighted the cost of an optional dependency.** Deferring the API key to "later"
left two days unfinishable and one of four documents with a hole in it. The dependency was
not the code — it was the credential.
