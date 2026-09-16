# Evaluation report

Published numbers for dbt-sentinel, day 10 of a 2-week build.

**Read this first.** Two of six target metrics are missed, two are unmeasured, and the
agent has never been benchmarked. Those facts are the top of this document rather than a
footnote, because a report that buries them is not worth reading.

---

## Methodology

**30 labelled fixtures**, each a unified diff plus a ground-truth label: 10 breaking,
10 should-pass, 10 subtle judgment calls.

**The labels were written before the runner existed.** Every `rationale` in
`evals/fixtures/labels.yml` argues from dbt semantics — what the change does to consumers
of the model — not from what the tool outputs. No fixture was labelled by running the tool
and recording the answer.

That ordering is the only thing making these figures worth publishing. Writing the runner
first produces a suite that confirms whatever the code already does, and a precision
number derived from it means nothing. The risk is not hypothetical: the temptation when a
metric looks bad is to decide the label was wrong. Any label that changes is argued in
`evals/LABEL_CHANGES.md` with its reasoning, so every change is auditable. **That file is
currently empty — no label has been changed.**

**The manifest is real.** Compiled by dbt 1.12.4 (schema v12) from `dbt-labs/jaffle_shop`,
extended with node kinds upstream jaffle_shop lacks: 2 sources, 3 exposures with owners, a
contracted incremental mart, and a public model. Upstream has zero sources and zero
exposures, so the exposure and contract branches of the scorer were dead code against a
real manifest until the project was extended.

**Honest caveat on that extension.** The manifest *shapes* are genuine — real dbt compiled
them. The graph *topology* is mine: I wrote the models that create the exposures and the
contract. A reader should discount accordingly. The fixtures that matter most (`b01`/`p10`,
`b06`/`s03`) are pairs designed so the tool cannot pass both halves by reacting to reach
or file identity, which limits how much the authored topology can flatter the result.

**Scoring.** A fixture is "flagged" if the tool surfaces it at MEDIUM or above. Precision,
recall and F1 are computed over that binary decision. The false-positive rate is reported
*separately* over the 10 should-pass fixtures, because that is the number deciding whether
anyone keeps the tool installed — high recall with an unreported FPR usually means the tool
is flagging everything.

**Fixtures that resolve to zero nodes are reported as errors, not scored as passes.**
Counting "the tool found nothing" as "the tool correctly found nothing" is how an eval
suite flatters a broken resolver. This caught a real defect on the first run.

---

## Results: deterministic core

No LLM. Exactly reproducible — `python -m evals.runner` on any machine gives these numbers.

| Metric | Value | Target | Verdict |
|---|---|---|---|
| Precision | **0.800** | > 0.75 | met |
| Recall | **0.533** | > 0.70 | **missed** |
| F1 | 0.640 | — | — |
| False-positive rate (should-pass) | **0.200** | < 0.10 | **missed, 2x target** |
| Exact severity match | 14/26 (0.538) | — | — |
| Fixtures silently dropped | **4** | 0 | **missed** |

Confusion at the MEDIUM+ threshold: TP=8, FP=2, FN=7, TN=9.

| Category | n | Errored | Exact match | Rate |
|---|---|---|---|---|
| breaking | 10 | 2 | 3 | 0.375 |
| should_pass | 10 | 0 | 8 | 0.800 |
| subtle | 10 | 2 | 3 | 0.375 |

**Reading this honestly:** recall 0.533 at precision 0.800 is the expected shape of a
structural-only scorer. When it fires it is usually right; it misses more than half of what
a reviewer should catch. Every miss lives in SQL semantics — join grain, an incremental
predicate, a dropped `distinct` — that no amount of graph traversal reveals. That gap is
the argument for the agent, and it was quantified before the agent existed so the
comparison cannot be retrofitted.

## Results: policy retrieval

Measured separately from severity accuracy, on purpose: when a fixture is wrong, the cause
is either "the right rule never reached the model" or "the rule reached it and the verdict
was still wrong". Those need different fixes — rule keywords versus the prompt — and one
end-to-end number cannot tell them apart.

| Metric | Value | Target | Verdict |
|---|---|---|---|
| Precision@3 | **0.708** | > 0.60 | met |
| Recall@3 | 0.630 | — | — |
| Hit@3 | 0.684 | — | — |
| Correct silence on no-rule fixtures | **0.455** | — | weak |

Both acceptance criteria pass, and the second matters more:

```
s01_pii_column_untagged  -> pii-tagging (0.493)      PII rule surfaces, alone
b05_changed_join_grain   -> grain-integrity (0.566)  no PII rule at all
p01_comment_added        -> no rule matched          silence on a comment
```

A retriever returning the PII rule on every diff would pass the first criterion and be
worthless. `correct_silence_rate` exists so that failure cannot hide — and at 0.455 it
shows 6 of 11 no-rule fixtures still draw a spurious rule.

## Results: the agent

**Not measured.** The 30-fixture agent-vs-baseline comparison has never run: it needs an
`ANTHROPIC_API_KEY` that was not available during the build.

This is a gap in the project, not an omission from this document. What exists:

- `evals/compare.py`, written and tested, which runs both arms and classifies every failure
- A refusal to fabricate: without a key it exits 2 and writes no results file
- 31 tests covering the agent's plumbing — schema validation, the retry, every degradation
  path — **all against an injected fake client**

So the agent's *plumbing* is proven and its *review quality is entirely unknown*. The
passing tests are not evidence of good reviews and were never meant to be.

To close the gap:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
python -m evals.compare          # ~$0.30-1.00 for 30 fixtures on claude-sonnet-5
```

Cost per PR and comment latency are unmeasured for the same reason. The instrumentation
exists (both appear in the comment footer and on the CLI); no run has produced numbers.

---

## Failure taxonomy

Every failure of the deterministic core, categorised. Counts are exact.

| Category | Count | What it means |
|---|---|---|
| Dropped input (resolver) | 4 | Resolved to a file, matched nodes, then discarded |
| False positive (over-broad trigger) | 2 | Additive change scored as breaking |
| Missing capability (SQL semantics) | 5 | Real breakage invisible to column extraction |
| Missing capability (governance) | 4 | Judgment call with no rule to fire before day 4 |

### Top 3 failure modes, with numbers

**1. YAML attribution drops the node entirely — 4 fixtures, 2 of them HIGH.**

`b03`, `b06`, `s03` edit `models/marts/schema.yml`; `s04` edits `models/exposures.yml`. All
four resolve to a file, match real nodes, and are then discarded.

`yaml_columns_by_model` only tracks `- name:` entries under a `columns:` key. The marts
schema puts `data_type:` under each column and the diff hunk begins *inside* a `columns:`
list, so the parser reads the column names themselves as model headings:

```
per_model = {'order_id': ([], []), 'customer_id': ([], []), 'total_amount': ([], [])}
```

Those keys never match a model name, so nothing is kept. The exposures file has no
`columns:` block at all, so the mapping is empty.

**Consequence: a contract column can be dropped and the tool outputs nothing at all.** Not
a wrong severity — no output. This is the worst failure shape in the project, because
silence reads as approval. It also suppresses the retrieval metric, since a node that does
not resolve cannot have rules retrieved for it.

**2. Any column-set delta triggers severity — 2 false positives, FPR 0.200.**

`p10_additive_column` adds `loaded_at` to `stg_orders` and scores HIGH.
`p03_test_added` adds a `not_null` test and scores HIGH.

`p10` is the deliberate twin of `b01` — same file, same 7-node reach, opposite semantics.
The tool scores them identically, which means it is reacting to *a column set changed*
rather than *a column removed*. `ChangedNode.is_structural` counts `added_columns`
alongside `removed_columns`, and `p03`'s new `- name:` under a `tests:` key is misread as a
new column.

At 0.200, one routine PR in five gets a HIGH. That is mute-the-bot territory, and it is the
metric most likely to decide the tool's fate in real use.

**3. SQL semantics are invisible to regex extraction — 5 false negatives.**

| Fixture | Expected | Actual | Why |
|---|---|---|---|
| `b04_dropped_dedup` | high | low | `distinct` removal is not a column change |
| `b08_model_renamed` | high | low | Rename seen as a file move, no consumer check |
| `b05_changed_join_grain` | high | medium | `left`→`inner` is not a column change |
| `b07_removed_incremental_filter` | high | medium | `is_incremental()` block deletion unparsed |
| `b09_incremental_key_change` | high | medium | `unique_key` config edit unparsed |

These are the fixtures that motivate the agent. Each is a genuine breaking change whose
risk lives in SQL meaning, not in structure — and the deterministic core cannot reach any
of them by design. Whether the agent closes this gap is exactly what the unmeasured
comparison would tell us.

---

## Defects found by measuring

Four, all caught by the eval suite or by running a documented command rather than trusting
it. Recorded because the process is part of the result.

**A cost constant that would have inflated every published figure by ~50%.** The harness
was written with $3/$15 per MTok — Sonnet 4.6's rates, carried over by assumption. The
pinned model is `claude-sonnet-5` at $2/$10. A test now pins the constants, and the summary
records the price list behind any cost it reports.

**A retrieval noise defect, caught on the first measured run.** Precision@3 started at
0.400 with correct silence 0.273 — 8 of 11 no-rule fixtures drew spurious rules. The query
was built from the node's *ambient* attributes, so `stg_` matched `staging-layer-purity` on
every staging file and `protected` matched `public-access-review` on every node. Those are
path and config facts, true of every diff touching the node, not evidence of a violation.
After fixing: precision@3 0.708, correct silence 0.455.

**A Windows path bug that made the tool a silent no-op.** A manifest compiled on Windows
stores `models\staging\x.sql`; diffs use forward slashes. Raw comparison resolved *nothing*,
so the CLI reported a clean review on a breaking PR and `--fail-on high` exited 0. Found
by pointing the tool at a real compiled manifest for the first time — the synthetic fixture
used forward slashes and hid it completely.

**Three tests that passed for the wrong reason.** A test asserting `"[:140]" in` the source
text of a module (greps a string, proves nothing). A test patching `sleep` on the wrong
module, sitting through 3.5s of real backoff while appearing mocked. And a meta-test that
ran pytest inside pytest to police wall-clock time, which recursed and spawned 54
processes. All three were mine, and all three are the same error: **a test that appears to
control its environment and does not.**

---

## Limitations

Stated plainly. The ones that look bad are the ones most worth stating.

| Limitation | Honest impact |
|---|---|
| **4 fixtures produce no output at all** | A dropped contract column is invisible. Silence reads as approval. |
| **FPR is 2x target** | One routine PR in five gets a HIGH |
| **Agent unmeasured** | No evidence the LLM improves on the baseline |
| **Never deployed** | No real PR has received a comment |
| Recall 0.533 | Misses over half of what a reviewer should catch |
| Correct silence 0.455 | 6 of 11 no-rule fixtures draw a spurious rule |
| Model-level lineage only | Names 12 downstream models, not which use the column |
| Lexical retrieval | Cannot match a paraphrase |
| 30 fixtures, one project | Small n, and the graph topology is authored |
| Single labeller | No inter-annotator agreement; four labels are self-flagged as contestable |

**The single-labeller point deserves emphasis.** All 30 labels were written by one person
from dbt semantics. `evals/fixtures/README.md` names the four most contestable calls
(`s07`, `s05`, `s02`, `b10`) with the argument against each. With one labeller there is no
agreement statistic, so the figures carry unquantified label noise.

## Reproducing

```bash
pip install -e ".[dev]"
python -m evals.runner              # deterministic severity metrics
python -m evals.retrieval_eval      # retrieval precision@3
python -m evals.compare             # agent vs. baseline (needs a key)
```

The manifest is pinned in the repo, so results do not depend on an external clone. CI runs
the first two on every PR and fails the build if any published metric drops below the value
recorded here — so this document cannot silently go stale.

---

## Conclusion

The deterministic core does what it claims: precision 0.800, and it does not flag
whitespace. Recall 0.533 means it is a useful second pair of eyes, not a safety net, and
the report says so.

**An honest 0.533 with a failure taxonomy is a stronger signal than an unverifiable 0.95.**
The two top failure modes are diagnosed to the specific predicate, and both were left
unfixed on purpose: day 6 measures, day 7 fixes, and doing both in one session destroys the
before/after comparison that makes the fix demonstrable.

What this project does not have is a measured agent or a live deployment. Both are stated
here, in the README, and in the PRD, rather than described as in progress.
