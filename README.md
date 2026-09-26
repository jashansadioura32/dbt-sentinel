# dbt-sentinel

[![CI](https://github.com/jashansadioura32/dbt-sentinel/actions/workflows/ci.yml/badge.svg)](https://github.com/jashansadioura32/dbt-sentinel/actions/workflows/ci.yml)

An agent that reviews dbt pull requests. It maps the diff to changed models, computes the
downstream blast radius by graph traversal, retrieves the governance rules that apply, and
posts a structured review with severity, affected models, and a suggested fix.

**Status: day 9 of a 2-week public build.** Measured baselines are published in
[evals/BASELINE.md](evals/BASELINE.md) and they are mediocre and honest rather than
impressive and unverifiable.

---

## The problem

A dbt project is a graph of SQL models. Renaming a column in a staging model is one line
in a diff and can break twelve dashboards, because dbt does not rewrite references — the
next run simply fails, or worse, succeeds with wrong numbers.

A reviewer looking at that diff sees one line. They cannot see the twelve consumers
without walking the DAG by hand, so in practice nobody does, and breaking changes ship.

dbt-sentinel walks the DAG on every PR and says what the diff will reach.

**Why this is not just a lint rule:** the hard part is not detecting a removed column, it
is deciding whether removing it *matters*. That depends on who consumes it, whether a
contract is enforced, and whether a `left join` becoming `inner join` changed the grain.
The first two are graph questions. The third needs judgment.

---

## Install

```bash
git clone https://github.com/jashansadioura32/dbt-sentinel.git
cd dbt-sentinel

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -e ".[dev]"            # core, policy retrieval, and the test suite
```

Optional extras, added per feature:

```bash
pip install -e ".[agent]"          # the reviewer agent (openai, pydantic)
pip install -e ".[server]"         # the GitHub App (fastapi, uvicorn, cryptography)
```

Install `.[dev]` rather than bare `.` — the test command below needs pytest and the
Pydantic schema the agent tests exercise.

**Windows:** clone to a short path such as `C:\src\dbt-sentinel`. Installing `openai`
under a deeply nested directory can fail with `OSError: [Errno 2]` on one of its longer
filenames — that is Windows' 260-character path limit, not a packaging fault. Shorten the
path, or enable long paths:
`reg add HKLM\SYSTEM\CurrentControlSet\Control\FileSystem /v LongPathsEnabled /t REG_DWORD /d 1 /f`

---

## Run it

You need a dbt `manifest.json` and a diff. The repo ships both, so this works immediately:

```bash
python -m dbt_sentinel \
  --manifest evals/manifest/manifest.json \
  --diff evals/fixtures/b01_column_rename_with_consumers.diff \
  --fail-on high
```

```
## Blast radius

### 🔴 `stg_orders` — HIGH · 7 downstream
- Removed column(s) `customer_id` with 7 downstream node(s)
- Reaches 1 contracted model(s): fct_order_payments
- Reaches 3 exposure(s): customer_success_churn, finance_month_end, exec_dashboard
```

Exit code is 1, so it works as a CI gate. On your own project, run `dbt parse`, then
`dbt-sentinel --since origin/main` from the project directory to review the current branch
the way its PR will be reviewed.

### In VS Code, on every commit

A post-commit hook reviews the branch in the background, with the agent included, and
opens `.sentinel/review.md` in the editor. The GitHub App is unaffected; this is a second
entry point to the same CLI. Setup: [docs/LOCAL.md](docs/LOCAL.md).

```bash
sh /path/to/dbt-sentinel/integrations/vscode/install.sh   # run inside your dbt repo
```

| Flag | Does |
|---|---|
| `--manifest PATH` | dbt manifest (default `target/manifest.json`) |
| `--diff PATH` | unified diff, or `-` for stdin |
| `--since REF` | diff the current branch against `REF` via git, instead of `--diff` |
| `--fail-on {high,medium,low,never}` | exit 1 at or above this severity |
| `--mermaid` | emit a blast-radius diagram per change |
| `--explain` | show which policy rules were retrieved, with scores |
| `--agent` | add LLM judgment findings (needs `OPENAI_API_KEY`) |
| `--changed-at ISO8601` | warn if the manifest predates the change |
| `--no-checks` | skip the deterministic check layer ([docs/CHECKS.md](docs/CHECKS.md)) |

### Exit codes

| Code | Means |
|---|---|
| `0` | Reviewed. Nothing at or above `--fail-on`. |
| `1` | Reviewed. Findings at or above `--fail-on`. |
| `2` | Could not run: unreadable manifest, unreadable diff, malformed argument. |

The `1` / `2` split is deliberate. A pipeline that reports a missing manifest as a failed
review teaches its users that the tool cries wolf, and it gets switched off long before it
ever reports a real breaking change.

---

## Architecture

```
GitHub webhook
  ↓  signature verified against the raw body (HMAC-SHA256, constant time)
diff parser          deterministic  — unified diff → changed files
manifest loader      deterministic  — manifest.json → normalised node graph
lineage traversal    deterministic  — BFS over child_map → blast radius
policy retrieval     hybrid         — glob/config prefilter, then TF-IDF over 14 rules
reviewer agent       gpt-4o         — tool-calling, returns validated Findings
deterministic render                — template code → PR comment + commit status
```

Four decisions that shape everything else:

**Deterministic first.** If graph traversal or string parsing can answer it, they do. The
manifest ships `child_map`; embedding a dependency graph and asking a model to traverse it
would be slower, costlier and less correct than a BFS. The LLM is for judgment, not lookup.

**Reach amplifies risk, it does not create it.** Nearly every staging model sits upstream
of a dashboard. Severity is triggered by a *structural* change — a removed column, a
deletion, a rename, a contract edit — and reach only decides how loud to be. An agent that
flags every PR gets muted in a week.

**The LLM never writes the comment.** It returns schema-validated `Finding` objects;
rendering is template code. Model-supplied text is flattened to one line and rejected if it
contains backticks or pipes, so it cannot forge headings inside someone's PR.

**Degrade, don't crash.** No API key, a timeout, a rate limit, invalid schema twice, prose
instead of a tool call — each degrades to deterministic-only output *with a visible note*.
Transient failures (429, 5xx, connection errors) retry with bounded backoff first; a 404 or
a 400 fails immediately, because sending it again will not make it a 200.

---

## Published results

Day-3 baseline in [evals/BASELINE.md](evals/BASELINE.md), day-7 before/after in
[evals/RESULTS_V2.md](evals/RESULTS_V2.md). 30 labelled fixtures, 10 breaking /
10 should-pass / 10 subtle, run against a real manifest compiled by dbt 1.12.4.

**Deterministic core, no LLM** (day 7, after one iteration round):

| Metric | Day 3 | **Current** |
|---|---|---|
| Precision | 0.800 | **0.909** |
| Recall | 0.533 | **0.588** |
| False-positive rate (should-pass block) | 0.200 | **0.000** |
| Fixtures silently dropped | 4 | **0** |

**Policy retrieval, measured separately** so a retrieval miss is distinguishable from a
reasoning error:

| Metric | Day 4 | **Current** |
|---|---|---|
| Precision@3 | 0.708 | 0.636 |
| Recall@3 | 0.630 | **0.778** |
| Correct silence on no-rule fixtures | 0.455 | 0.455 |

Retrieval precision@3 fell because three fixtures that previously resolved to no node now
reach the retriever at all — recall rose on the same change. A metric that improves by
keeping fixtures out of the denominator is one this project publishes against, not for.

Recall of 0.588 at precision 0.909 is the honest shape of a structural-only scorer: when it
fires it is almost always right, and it still misses about 40% of what a reviewer should
catch.
Every miss lives in SQL semantics — join grain, an incremental predicate, a dropped
`distinct` — that no amount of graph traversal reveals. That gap is the argument for the
agent, and it was quantified *before* the agent existed so the comparison cannot be
retrofitted.

**The agent has not been measured yet.** It needs an API key, and the 30-fixture
comparison has not run. There is no agent number in this README because there is no agent
number.

The labels were written from dbt semantics before the eval runner existed
([evals/fixtures/README.md](evals/fixtures/README.md) explains why that ordering is the
only thing making these figures worth publishing). Any label that changes is argued in
[evals/LABEL_CHANGES.md](evals/LABEL_CHANGES.md), never silently edited.

---

## Tests and evals

```bash
python -m pytest tests/ -q          # 130 regression tests (1 skipped without cryptography)
python -m evals.runner              # severity metrics → evals/results.json
python -m evals.retrieval_eval      # retrieval precision@3 → evals/retrieval_results.json
python -m evals.compare             # agent vs. baseline (needs OPENAI_API_KEY)
```

Every test encodes a bug found during the build. The eval suite runs in CI as a regression
gate: if a published metric drops below its recorded value, the build fails — so a change
that quietly makes review quality worse cannot merge while this README still claims
otherwise.

---

## Deploying as a GitHub App

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). It covers App creation, the four repository
permissions needed, private-key handling on hosts that mangle multi-line secrets, and a
troubleshooting table keyed by symptom. `Procfile` and `railway.json` are included.

**Deployed.** On 2026-09-25 the App posted a correct HIGH-severity review on a real PR in
[jashansadioura32/jeffle-shop](https://github.com/jashansadioura32/jeffle-shop) and set a
failing commit status, in 6.5s. The OpenAI account had no credits, so the reviewer agent
degraded and the comment says so explicitly — design rule 5 on real infrastructure rather
than asserted in a doc.

Shipping it found four bugs that all 196 local tests had passed, including a webhook that
rejected 100% of deliveries and a JWT claim GitHub would never accept. Both are recorded
in [docs/DEPLOYMENT_LOG.md](docs/DEPLOYMENT_LOG.md), along with the existing test that
asserted one of them.

---

## Known limitations

Documented rather than hidden. The ones that look bad are the ones most worth stating.

| Limitation | Impact |
|---|---|
| **A removed column in a shared schema.yml cannot be pinned to one model** | When the hunk shows no model heading, the tool reports an explicit medium-severity uncertainty naming the column and file, rather than guessing an owner or staying silent. Sole remaining false positive (`s03`). |
| Column extraction is regex, not a parser | Misses `select *`, CTE aliases, macro-generated columns |
| Lexical retrieval cannot match a paraphrase | A diff saying `* 1.1` never matches a rule whose vocabulary is `full_refresh` |
| Macro changes don't resolve to models | Surfaced as a warning, not resolved |
| Model-level lineage only | Reports that 12 models are downstream, not which of them select the dropped column |
| Manifest assumed current | A stale manifest shrinks blast radius; `--changed-at` warns but cannot fix it |
| CI artifact download unimplemented | Falls back to a committed manifest and says so in the comment |

The first two are the top failure modes and are deliberately *not* fixed yet — day 6
measures, day 7 fixes, and fixing them in the measuring session would destroy the
before/after comparison. They are parked in [ROADMAP.md](ROADMAP.md).

---

## Non-goals

- No web UI. PR comments are the entire surface.
- Not a data catalog, a lineage browser, or a test generator.
- No column-level lineage at v1.
- Single warehouse (Snowflake-flavoured SQL assumptions), single repo.
- No agent framework that hides control flow.

---

## Documentation

| Document | What it covers |
|---|---|
| [docs/PRD.md](docs/PRD.md) | Problem, users, success metrics against actuals, what was cut and why |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Module map, data flow, and 4 ADRs with the rejected alternatives |
| [docs/EVAL_REPORT.md](docs/EVAL_REPORT.md) | Methodology, metrics, failure taxonomy, limitations |
| [docs/CASE_STUDY.md](docs/CASE_STUDY.md) | The build as a narrative: decisions, defects found, what I would redo |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | GitHub App setup, permissions, secrets, troubleshooting |
| [docs/DEPLOYMENT_LOG.md](docs/DEPLOYMENT_LOG.md) | The first live review, and the four bugs only real infrastructure found |
| [evals/RESULTS_V2.md](evals/RESULTS_V2.md) | Day-7 iteration round: before/after on the two deferred failure modes |
| [ROADMAP.md](ROADMAP.md) | Out-of-scope parking lot and deferred fixes |

---

## License

MIT — see [LICENSE](LICENSE).
