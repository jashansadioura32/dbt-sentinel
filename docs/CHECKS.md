# Check inventory — the spec

> **READ THIS BEFORE ADDING A CHECK.**
>
> This table was written **before `checks.py` existed**, the same ordering that makes
> [evals/fixtures/labels.yml](../evals/fixtures/labels.yml) worth publishing: the labels
> were written from dbt semantics before the runner could score them, and the day-4
> policy pack answered to rule ids the labels already named. A check inventory written
> after the implementation is a description of what the code happens to do. Written
> before, it is a contract the code has to meet.
>
> Each row is one check function, one true-positive fixture, one near-miss fixture and
> one unit test. No check ships without all four.

## Why checks are not part of the blast radius

A lint finding **has no reach**. `select *` in a model with 200 consumers is exactly as
bad as `select *` in a leaf. Design rule 2 says reach amplifies risk and never creates it,
so folding check findings into `Assessment.severity` leaves two bad options: amplify them
by reach, which is factually wrong and inflates the false-positive rate, or don't, in
which case they sit on a scale whose entire meaning is *structural trigger × reach* while
having neither.

So checks are a **peer** of the blast radius, not a component of it:

| Section | Source | Reach-amplified? | Gates the merge? |
|---|---|---|---|
| `## Blast radius` | `report.assess` | yes | **yes** — HIGH sets a failing status |
| `## Checks` | `checks.py` | no | no |
| `## Reviewer findings` | `agent.py` | no | no |

Two invariants enforce the separation:

1. **No check may return `high`.** HIGH means "a consumer breaks on merge", which no lint
   finding establishes. This is what keeps checks out of the commit status without
   inventing a second severity axis.
2. **`pipeline.py`'s `severity = assessments[0].severity` is never touched.**

The proof case is [`p03_test_added`](../evals/fixtures/p03_test_added.diff): it adds a
`tests:` block and is labelled `should_pass`, so `deprecated-tests-key` **will** fire on
it. That is correct only because the finding lands in Checks at `low` and never reaches
`Assessment.severity`. If the published FPR of 0.200 ever moves because of a check, the
separation has been broken. `tests/test_checks.py` pins exactly that.

## Scope: added lines only

Checks read **only the added lines of a diff**, never whole-file content.

This is the single most important design decision in the layer, and it is what lets
dbt-sentinel skip the grandfathering machinery that a brownfield reviewer needs. A check
that scans whole files fires on every pre-existing violation in every unrelated PR, which
is unusable on a 2000-model repo — so such reviewers bolt on a frozen list of exempted
violations, a `[GRANDFATHERED]` render path, and a staleness validator. Scoping to added
lines means pre-existing violations never fire, so none of that exists here.

The cost is honest and stated: a check cannot see that a *pre-existing* line is wrong, only
that a *new* one is. That is the right trade for a PR reviewer.

## The checks

| check_id | Category | Severity | Fires when | Does NOT fire when |
|---|---|---|---|---|
| `missing-schema-entry` | structure | medium | A new `.sql` model under `models/` resolves to a node with no `patch_path` | The model is a seed or snapshot; a YAML entry for it is added in the same PR; the file is not new |
| `deprecated-tests-key` | testing | low | An added line in `models/**/*.yml` matches `^\s*tests:` | The line is `data_tests:`; the file is `dbt_project.yml`; the key appears only on a context line |
| `hardcoded-relation` | portability | medium | An added SQL line has `from`/`join` followed by a dotted identifier outside `{{ }}` | The line is a comment; the identifier is inside a Jinja expression; the reference is a bare CTE name |
| `cross-layer-reference` | structure | medium | A changed node's path layer is downstream of a layer it `depends_on` — e.g. a staging model refs a mart | Both nodes are in the same layer; the reference points upstream; either path matches no known layer prefix |

**The "Does NOT fire when" column is the false-positive contract.** It is written before
implementation and it is what the near-miss fixtures test. A check whose negative column is
vague is a check that will need an ignore file later.

### Why these four

`hardcoded-relation` is the most defensible of the set: a hardcoded `schema.table` defeats
lineage itself, which is the entire subject of this tool. `cross-layer-reference` is the
most in-character, because it is decided from the manifest graph rather than by regex.
`missing-schema-entry` is nearly free — `Node.patch_path` already exists. `deprecated-tests-key`
is purely factual: dbt 1.8 renamed the key, so there is no judgment and no house style in it.

## Considered and rejected

More is learned from these than from the inclusions.

| Candidate | Why not |
|---|---|
| `select *` outside staging | [`p04_new_unused_model`](../evals/fixtures/p04_new_unused_model.diff) contains `select * from {{ ref('raw_orders') }}` and is labelled `should_pass`. `select *` wrapping a source in a staging model is idiomatic dbt, so the check needs a "non-staging" predicate that is pure path convention — and it would fire on a CTE that is narrowed two lines later. The fixture rejects it. |
| Orphan YAML with no matching `.sql` | Needs a full-repo file listing. The input is a diff; getting one means another GitHub API call on every review. |
| Dead / unreferenced ephemeral model | "Unreferenced today" is the normal state of a new model — `p04` again. Partly covered already by `blast.size == 0`. |
| PK / uniqueness test on a `row_number()` column | Needs column-level reasoning about generated columns, which brushes the column-level-lineage non-goal in `CLAUDE.md`. |
| Missing `not_null` / `unique` on a declared PK | "Declared primary key" is not a first-class dbt concept outside `constraints:` on contracted models, where the existing `contract-enforcement` policy rule already covers it. |

Everything Data Vault-specific in the source material — hash key formulas, HASHDIFF
composition, BKCC, ghost records, `REC_SRC`, `LOAD_DTS` derivation, the
`v_psa_stg_`/`hub_`/`sat_`/`lnk_` prefix taxonomy, 4-layer vs 6-layer CTEs — is one org's
house style and is out of scope by definition. dbt-sentinel is generic.

## How these get measured

The existing 30 fixtures are labelled `expected_severity` and cannot score checks, so the
check layer gets its own eval — the same separation day 4 used for retrieval, and for the
same reason: when something is wrong, "the check misfired" and "the severity was wrong"
need different fixes and one number cannot tell them apart.

- `evals/fixtures/checks/` — 8 diffs, one true positive and one idiomatic near-miss per
  check. The TP/near-miss pairing is the `b01`/`p10` discipline: two fixtures that look
  alike and mean the opposite.
- `evals/checks_eval.py` → `evals/checks_results.json`, publishing precision / recall / FPR.
- Floors are added to `.github/scripts/check_baselines.py` **in a separate commit from the
  one that implements the checks** — measure first, then gate, so the first numbers are
  not chosen to match an implementation.
