# Session prompts

One prompt per session. Paste verbatim. Do not combine days.

---

## Session 0 — Repo setup (run once)

```
Read CLAUDE.md and BUILD_PLAN.md.

Set up the repo:
- src/ layout with package dbt_sentinel, pyproject.toml, MIT LICENSE, .gitignore
- Place the existing day 1-2 files I've provided into src/dbt_sentinel/ and tests/
- ROADMAP.md as the parking lot for out-of-scope ideas
- Run the test suite and confirm 11 tests pass

Then clone dbt-labs/jaffle_shop into a sibling directory, run dbt compile against
DuckDB, and point the CLI at the real target/manifest.json.

Report exactly what breaks. Do not fix anything yet — I want to see the failures
before we decide which are worth fixing. Real manifests have shapes our synthetic
fixture doesn't, and those differences are day 3's first test cases.
```

---

## Session 1 — Day 3

```
Read CLAUDE.md and BUILD_PLAN.md, then implement Day 3 only.

Build the evaluation harness before any agent code exists.

Order of work:
1. First, write evals/fixtures/ — all 30 labelled fixtures, with the rationale field
   written from dbt semantics, not from what our current code outputs.
2. Only then write evals/runner.py.

This order matters. If you write the runner first you will unconsciously shape the
labels to match the code's behaviour, and the eval suite becomes worthless.

Stop when the acceptance criteria in BUILD_PLAN.md pass. Show me the baseline metrics
table before writing evals/BASELINE.md.
```

---

## Session 2 — Day 4

```
Read CLAUDE.md and BUILD_PLAN.md, then implement Day 4 only.

Policy pack plus hybrid retrieval.

Constraint I want enforced: embed the rule pack and nothing else. Do not embed the
manifest, the lineage graph, or SQL — those are answered deterministically and design
rule 1 in CLAUDE.md applies.

Measure retrieval precision@3 as its own metric, separate from end-to-end accuracy.
I need to be able to tell a bad retrieval from bad reasoning later.

Stop at the acceptance criteria. Show me the retrieval scores for three contrasting
fixtures before you write anything to BASELINE.md.
```

---

## Session 3 — Day 5

```
Read CLAUDE.md and BUILD_PLAN.md, then implement Day 5 only.

Agent v0 plus the GitHub App skeleton.

Two things I care about more than output quality today:
1. The LLM returns validated structured findings and never writes the final comment.
2. Invalid or failed LLM output degrades to deterministic-only with a visible note,
   rather than crashing or silently producing nothing.

Review quality will be poor today. Do not tune prompts to make it look better — that
is day 7, and doing it now destroys the day 6 baseline.

Also scaffold the webhook receiver with signature verification, stubbed. This moves
setup risk off day 8.
```

---

## Session 4 — Day 6

```
Read CLAUDE.md and BUILD_PLAN.md, then implement Day 6 only.

Run the full eval suite, agent vs deterministic baseline. Categorise every failure.

Do not fix anything today. Not one prompt tweak, not one rule adjustment. Measuring
and fixing in the same session destroys the before/after comparison that is the whole
point of the exercise.

Where the agent was right and my label was wrong, change the label and log it in
evals/LABEL_CHANGES.md with the reasoning.

Output: evals/RESULTS_V1.md with the metrics table, failure taxonomy, and counts.
Name the top 3 failure modes with numbers attached.
```

---

## Session 5 — Day 7

```
Read CLAUDE.md, BUILD_PLAN.md, and evals/RESULTS_V1.md, then implement Day 7 only.

Fix the top 2 failure modes. Only those two.

Add a regression test for each fix. Re-run the full suite and write
evals/RESULTS_V2.md with before/after.

One iteration round, then stop. If recall is still around 70%, we ship 70% and explain
it. Further tuning has poor returns and costs days that matter more.
```

---

## Session 6 — Day 8

```
Read CLAUDE.md and BUILD_PLAN.md, then implement Day 8 only.

Wire the day 5 webhook skeleton into a working GitHub App and deploy it.

Handle manifest sourcing explicitly: CI artifact from the base branch, with a
committed target/manifest.json as fallback. Document which one is in use.

I will handle anything that requires entering credentials, creating the GitHub App,
or authorising OAuth — give me the exact steps to do those parts myself and leave
placeholders in the code.

This is the highest-risk day. If you hit a wall on auth or deployment, tell me early
rather than working around it.
```

---

## Session 7 — Day 9

```
Read CLAUDE.md and BUILD_PLAN.md, then implement Day 9 only.

Hardening and instrumentation. Cost and latency per PR surfaced in the comment footer.
Eval suite as a GitHub Actions regression gate. Timeout, rate-limit and API-failure
handling.

Then rewrite the README assuming the reader has never seen the project: install, run,
architecture, and an honest limitations table.

Test the README by following it literally in a clean directory. If a step is missing,
the README is wrong, not the reader.
```

---

## Session 8 — Day 10

```
Read CLAUDE.md, BUILD_PLAN.md, and everything in evals/, then implement Day 10 only.

Build is frozen. Write the four documents in BUILD_PLAN.md: PRD, ARCHITECTURE with
4 ADRs, EVAL_REPORT, and the case study page.

The eval report must state the real numbers including the unflattering ones, and name
the limitations plainly. Do not soften them. An honest 70% with a failure taxonomy is
a stronger signal than an unverifiable 95%.

For the ADRs, write the alternative that was rejected and why — not just the decision.

If you find yourself wanting to fix code today, add it to ROADMAP.md instead.
```

---

## Mid-session corrections worth having ready

When it over-builds:
```
That's outside the non-goals in CLAUDE.md. Remove it and add it to ROADMAP.md.
```

When it reaches for the LLM on a deterministic question:
```
Design rule 1. Can this be answered by graph traversal or string parsing? If yes,
do it that way.
```

When it claims something works:
```
Show me the test that proves it, and the output of running it.
```

When it adds a dependency:
```
Justify that dependency against the allowed list in CLAUDE.md, or remove it.
```

When metrics look suspiciously good:
```
Show me the false-positive rate on the should-pass fixtures specifically. High recall
with an unreported FPR usually means the agent is flagging everything.
```
