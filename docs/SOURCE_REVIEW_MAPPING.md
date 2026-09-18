# Source review mapping — what we took, what we left

An external dbt code reviewer (a Data Vault 2.x implementation, 80 deterministic checks
across categories A–Q) was evaluated as a source of ideas for dbt-sentinel. This document
records every check in it, what happened to each one, and why.

It exists because the interesting number is not the four checks that shipped — it is the
seventy-six that did not, and the reasons.

**Summary: 4 of 80 adopted (5%).** That ratio is the finding, not a shortfall. The source
reviewer encodes one organisation's modelling standard; roughly 70% of it is meaningless
outside a Data Vault repo. What genuinely transferred was **mechanisms**, not rules.

> A note on the count: the source file's own summary table says "Total checks: 63". That
> figure is stale — it predates categories O (5), P (6) and Q (2), plus the late C4 and E3.
> The actual row count is **80**, verified by extracting every check id from the file. It
> also marks O1–O5 and P1–P6 as "documented-pending", meaning 11 of the 80 are
> unimplemented even in their own repo.

---

## What we implemented

Four checks, all in [`src/dbt_sentinel/checks.py`](../src/dbt_sentinel/checks.py),
specified in [CHECKS.md](CHECKS.md) before the code was written.

| Our check | Implementation | Severity | Closest source ancestor | What had to change |
|---|---|---|---|---|
| `missing-schema-entry` | [checks.py:97](../src/dbt_sentinel/checks.py#L97) | medium | **H1** `yaml_exists_for_sql` | Generic dbt; dropped the `v_psa_stg` path assumption. Resolves the model name from the filename because a model added in the PR is absent from the base manifest. |
| `deprecated-tests-key` | [checks.py:148](../src/dbt_sentinel/checks.py#L148) | low | **H2** `data_tests_not_deprecated` | Taken nearly as-is. It is a dbt fact (renamed in 1.8), not house style. Anchored regex, because `data_tests:` ends in the same eight characters. |
| `hardcoded-relation` | [checks.py:192](../src/dbt_sentinel/checks.py#L192) | medium | **I1** `uses_source_or_ref` | Dropped the Snowflake `SCHEMA.TABLE` shape; strips Jinja before matching so `{{ source(...) }}` is not flagged. |
| `cross-layer-reference` | [checks.py:263](../src/dbt_sentinel/checks.py#L263) | medium | **I2** `no_cross_layer_ref_down` | Layer prefixes are `staging`/`intermediate`/`marts`, not `raw_vault`/`bus_vault`/`info_mart`. Resolved via the manifest rather than by regex. |

**Supporting code:**

| Concern | Location |
|---|---|
| Registry (`CHECKS` tuple) | [checks.py:315-320](../src/dbt_sentinel/checks.py#L315-L320) |
| `CheckFinding` / `CheckSpec` / `CheckContext` | [checks.py:39-81](../src/dbt_sentinel/checks.py#L39-L81) |
| `run_checks` orchestration | [checks.py:322+](../src/dbt_sentinel/checks.py) |
| Rendering (`render_checks`) | [report.py](../src/dbt_sentinel/report.py) |
| PR-comment wiring | [pipeline.py:182-190](../src/dbt_sentinel/pipeline.py#L182-L190) |
| CLI wiring + `--no-checks` | [cli.py:184-188](../src/dbt_sentinel/cli.py#L184-L188) |
| Eval | [evals/checks_eval.py](../evals/checks_eval.py) |
| Fixtures (8) | [evals/fixtures/checks/](../evals/fixtures/checks/) |
| Tests (16) | [tests/test_checks.py](../tests/test_checks.py) |
| CI floors | [.github/scripts/check_baselines.py](../.github/scripts/check_baselines.py) |

---

## Mechanisms adopted (not checks)

More valuable than any individual check. These came from the source's workflow and CLI
rather than its rule set.

| Mechanism | Where it landed |
|---|---|
| Structured finding with check id + suggestion | `CheckFinding` in [checks.py:39](../src/dbt_sentinel/checks.py#L39) |
| Check inventory written **before** the code | [CHECKS.md](CHECKS.md) |
| Comment upsert by hidden marker | Already existed; fixed a >100-comment pagination bug in [github.py](../src/dbt_sentinel/github.py) |
| Stale comment deletion on a clean pass | `delete_comment` + `has_nothing_to_report` in [pipeline.py:51](../src/dbt_sentinel/pipeline.py#L51) |
| Workflow concurrency, cancel-in-progress | [.github/workflows/ci.yml](../.github/workflows/ci.yml) |
| Exit-code taxonomy (0 / 1 / 2) | [cli.py:1-12](../src/dbt_sentinel/cli.py#L1-L12) |

---

## Mechanisms rejected

| Mechanism | Decision |
|---|---|
| **FAIL/WARN axis** separate from severity | **Rejected.** dbt-sentinel already has severity + `--fail-on` + `status_state`. A fourth axis would duplicate the third. Instead checks are capped at `medium` and can never reach the commit status. |
| **Grandfather list** of pre-existing violations | **Deferred**, with precondition. Unnecessary because our checks read only *added* diff lines, so pre-existing violations never fire. Becomes necessary the moment any check gains whole-file scanning. |
| **`.code_review_ignore`** scoped to a category | **Deferred.** Nothing to ignore at four checks. A check needing an escape hatch should be dropped instead. |
| **`--category` / `--check` filters** | **Deferred.** With four checks a filter selects between four and three. |
| **Graduated enforcement** (new file FAIL, modified WARN) | **Adopted inverted.** Theirs is a legacy ramp. Ours is the reverse: a new model has no consumers and little blast radius; a modified one may have 200. So `ChangeType` varies the message, never the severity. |
| **Line-number tracking / inline comments** | **Deferred.** GitHub 422s the *entire* review on one bad position — that collides with "degrade, don't crash". Also the only change that would edit `parse_diff`, which feeds both published baselines. |

All deferrals are recorded in [ROADMAP.md](../ROADMAP.md) with the condition that would
make each one necessary.

---

## The full inventory: all 80 source checks

Disposition key:

- **ADOPTED** — ported, in some form, to `checks.py`
- **DV** — Data Vault house style; permanently out of scope
- **SNOW** — Snowflake/SQL-dialect or formatting convention
- **REJECTED** — generic and considered, but explicitly declined (see [CHECKS.md](CHECKS.md))
- **POLICY** — covered instead by the LLM policy pack in [policies/](../policies/), as judgment rather than a deterministic check
- **N/A** — not a code check at all

### Category A — Hash Key formula (5)

| # | Check | Sev | What it does | Disposition |
|---|---|---|---|---|
| A1 | `hk_uses_concat_ws` | FAIL | HK must use `MD5_BINARY(UPPER(CONCAT_WS('\|\|', ...)))` | **DV** |
| A2 | `hk_coalesce_nullif_trim` | FAIL | Each HK component wraps `COALESCE(NULLIF(TRIM(CAST(...))))` | **DV** |
| A3 | `hk_uses_raw_column_names` | FAIL | HK references raw source columns, not BK aliases | **DV** |
| A4 | `hk_bkcc_last_component` | FAIL | BKCC must be the last `CONCAT_WS` argument | **DV** |
| A5 | `hk_has_upper_wrapper` | FAIL | HK formula wrapped in `UPPER(...)` | **DV** |

### Category B — HASHDIFF formula (8)

| # | Check | Sev | What it does | Disposition |
|---|---|---|---|---|
| B1 | `hashdiff_uses_nullif_concat` | FAIL | `MD5_BINARY(UPPER(NULLIF(CONCAT(...))))` shape | **DV** |
| B2 | `hashdiff_ifnull_trim_pattern` | FAIL | Components use `IFNULL(TRIM(col::text), '^^')` | **DV** |
| B3 | `hashdiff_separator_pattern` | WARN | `'\|\|'` separator placement | **DV** |
| B4 | `hashdiff_excludes_metadata` | FAIL | No `_HK`/`_BK`/`LOAD_DTS`/`REC_SRC`/`BKCC` in HASHDIFF | **DV** |
| B5 | `hashdiff_includes_psa_delete_ind` | FAIL | `PSA_DELETE_IND` is data, must be included | **DV** |
| B6 | `hashdiff_includes_fivetran_deleted` | FAIL | `_FIVETRAN_DELETED` is data, must be included | **DV** |
| B7 | `hashdiff_ends_with_sentinel` | FAIL | Closes with the `'^^\|\|^^'` sentinel | **DV** |
| B8 | `hashdiff_delete_flag_explicit_cast` | FAIL | Delete flags carry an explicit text cast | **DV** |

### Category C — CTE structure (4)

| # | Check | Sev | What it does | Disposition |
|---|---|---|---|---|
| C1 | `cte_4layer_new_models` | WARN | New models use `SRC → LOGIC → JOIN → FINAL` | **DV** |
| C2 | `cte_no_nonstandard_names` | WARN | CTE names match `SRC_*`/`LOGIC_*`/`RENAME_*`/… | **DV** |
| C3 | `final_select_from_join_result` | WARN | Final SELECT reads `JOIN_RESULT` or `FINAL` | **DV** |
| C4 | `where_in_src_cte_only` | WARN | WHERE belongs in the SRC CTE, not LOGIC | **DV** |

### Category D — BKCC (3)

| # | Check | Sev | What it does | Disposition |
|---|---|---|---|---|
| D1 | `bkcc_join_on_1_equals_1` | FAIL | BKCC joined via `INNER JOIN ... ON '1' = '1'` | **DV** |
| D2 | `bkcc_from_ref_table` | FAIL | BKCC sourced from `ref('ref_business_key_collision')` | **DV** |
| D3 | `bkcc_column_present` | FAIL | Every staging model outputs a `BKCC` column | **DV** |

### Category E — Dedup / QUALIFY (3)

| # | Check | Sev | What it does | Disposition |
|---|---|---|---|---|
| E1 | `no_select_distinct` | FAIL | `SELECT DISTINCT` banned; use `QUALIFY ROW_NUMBER()` | **SNOW** — `QUALIFY` is Snowflake syntax, and banning `DISTINCT` outright is their rule, not dbt's |
| E2 | `qualify_has_row_number` | WARN | QUALIFY should use `ROW_NUMBER()`/`RANK()` | **SNOW** |
| E3 | `qualify_order_by_load_dts` | WARN | Hub/link QUALIFY orders by `LOAD_DTS` | **DV** |

### Category F — Date / timezone (5)

| # | Check | Sev | What it does | Disposition |
|---|---|---|---|---|
| F1 | `convert_timezone_utc` | FAIL | `CONVERT_TIMEZONE` targets `'UTC'` | **SNOW** |
| F2 | `null_date_1900_placeholder` | WARN | NULL dates use `'1900-01-01'::TIMESTAMP` | **SNOW** |
| F3 | `load_dts_derivation_fivetran` | FAIL | Fivetran `LOAD_DTS` derivation | **DV** |
| F4 | `load_dts_derivation_snp_glue` | FAIL | SNP GLUE `GLCHANGETIME` parsing | **DV** |
| F5 | `load_dts_has_convert_timezone` | FAIL | Every `LOAD_DTS` includes `CONVERT_TIMEZONE` | **DV** |

### Category G — Naming (6)

| # | Check | Sev | What it does | Disposition |
|---|---|---|---|---|
| G1 | `vpsa_stg_prefix` | FAIL | Staging files start with `v_psa_stg_` | **DV** |
| G2 | `hub_prefix` | FAIL | `raw_vault/hub/` files start with `hub_` | **DV** |
| G3 | `sat_prefix` | FAIL | Satellite prefix taxonomy (`sat_`, `lsat_`, `msat_`, …) | **DV** |
| G4 | `link_prefix` | FAIL/WARN | `lnk_`/`tlink_`, graduated for legacy `link_` | **DV** (the *graduated enforcement* mechanism was adopted, inverted) |
| G5 | `double_underscore_separator` | WARN | `entity__source` naming | **DV** |
| G6 | `column_names_uppercase` | WARN | Column aliases UPPERCASE | **SNOW** |

### Category H — Test coverage (9)

| # | Check | Sev | What it does | Disposition |
|---|---|---|---|---|
| H1 | `yaml_exists_for_sql` | FAIL | Every `.sql` has a matching `.yml` | **ADOPTED** → `missing-schema-entry` |
| H2 | `data_tests_not_deprecated` | FAIL | `data_tests:` not `tests:` | **ADOPTED** → `deprecated-tests-key` |
| H3 | `vpsa_bk_not_null_test` | FAIL | `not_null` on BK columns | **DV** |
| H4 | `vpsa_unique_combo_bk_load_dts` | FAIL | `unique_combination_of_columns` on BK + LOAD_DTS | **DV** |
| H5 | `rv_primary_key_constraint` | FAIL | `dbt_constraints.primary_key` on raw vault | **DV** |
| H6 | `rv_foreign_key_constraint` | FAIL | `dbt_constraints.foreign_key` sat → hub | **DV** |
| H7 | `rv_row_count_test` | WARN | Row-count expectation, `min_value: 4` | **POLICY** — `test-coverage-keys` |
| H8 | `no_constraints_on_views` | FAIL | No PK/FK constraints on views | **DV** |
| H9 | `sat_grain_suggests_msat` | WARN | Extended PK suggests `msat_` naming | **DV** |

### Category I — Source & layer integrity (4)

| # | Check | Sev | What it does | Disposition |
|---|---|---|---|---|
| I1 | `uses_source_or_ref` | FAIL | No direct `SCHEMA.TABLE` references | **ADOPTED** → `hardcoded-relation` |
| I2 | `no_cross_layer_ref_down` | FAIL | No ref() against the layer direction | **ADOPTED** → `cross-layer-reference` |
| I3 | `dim_fact_no_business_logic` | WARN | No `CASE WHEN`/`WHERE` in dim/fact models | **REJECTED** — assumes their PIT/PB layering |
| I4 | `source_registered_in_yaml` | WARN | Every `source()` declared in a sources YAML | **DV** — keyed to their sources file |

### Category J — Incremental config (5)

| # | Check | Sev | What it does | Disposition |
|---|---|---|---|---|
| J1 | `where_not_exists_uses_hashdiff` | FAIL | Satellite `NOT EXISTS` compares HK **and** HASHDIFF | **DV** |
| J2 | `hub_where_not_exists_hk_only` | WARN | Hub `NOT EXISTS` compares HK only | **DV** |
| J3 | `on_schema_change_config` | WARN | Inherit `on_schema_change: sync_all_columns` | **POLICY** — `incremental-safety` |
| J4 | `full_refresh_guard` | WARN | Large models guard `full_refresh` | **POLICY** — `incremental-safety` |
| J5 | `watermark_scoped_per_rec_src` | FAIL | Watermark scoped per `REC_SRC` | **DV** |

### Category K — Ghost records (5)

| # | Check | Sev | What it does | Disposition |
|---|---|---|---|---|
| K1 | `ghost_record_decode_pattern` | FAIL | `DECODE(GR.VALUE, 0, …, -1, …, -2, …)` for BKCC | **DV** |
| K2 | `ghost_record_three_sentinels` | FAIL | All three sentinels (0, -1, -2) present | **DV** |
| K3 | `ghost_record_load_dts` | WARN | Ghost `LOAD_DTS` is `'1900-01-01'` | **DV** |
| K4 | `ghost_record_rec_src` | WARN | Ghost `REC_SRC` literal | **DV** |
| K5 | `ghost_record_hk_formula` | FAIL | Ghost HK uses `MD5_BINARY(GR.VALUE)` | **DV** |

### Category L — Join patterns (2)

| # | Check | Sev | What it does | Disposition |
|---|---|---|---|---|
| L1 | `inner_join_has_comment` | WARN | Non-BKCC `INNER JOIN` carries an explaining comment | **SNOW** — a commenting convention |
| L2 | `left_join_default_lookups` | WARN | Lookups default to `LEFT JOIN` | **POLICY** — `grain-integrity` covers join-type changes as judgment |

### Category M — Miscellaneous (5)

| # | Check | Sev | What it does | Disposition |
|---|---|---|---|---|
| M1 | `no_hardcoded_env` | FAIL | No `'DEV'`/`'QA'`/`'PRD'` literals | **Partly ADOPTED** — `hardcoded-relation` catches hardcoded database names, which is the same failure in the shape dbt actually suffers from |
| M2 | `no_select_star_outside_src` | WARN | `SELECT *` only in SRC CTEs | **REJECTED** — `p04_new_unused_model` has `select * from {{ ref(...) }}` and is labelled should_pass |
| M3 | `rec_src_format` | WARN | `Location.System.Application.Table` format | **DV** |
| M4 | `header_comment_present` | WARN | Header comment at line 1 | **SNOW** — formatting convention |
| M5 | `fix_proof_atomicity_prompt` | WARN | Reviewer prompt about fix+proof in one commit | **N/A** — not a code check |

### Category N — Staging data integrity (3)

| # | Check | Sev | What it does | Disposition |
|---|---|---|---|---|
| N1 | `no_delete_flag_filter` | FAIL | Never filter on delete flags in staging | **DV** |
| N2 | `coalesce_on_payload_in_staging` | WARN | No COALESCE on payload columns in staging | **DV** |
| N3 | `primary_src_no_business_rules` | WARN | Driver CTE has no unexplained WHERE/QUALIFY | **DV** |

### Category O — Multi-source safety (5) — *unimplemented in source*

| # | Check | Sev | What it does | Disposition |
|---|---|---|---|---|
| O1 | `multi_source_not_exists_includes_rec_src` | FAIL | `NOT EXISTS` includes REC_SRC/BKCC | **DV** |
| O2 | `multi_source_qualify_includes_rec_src` | FAIL | QUALIFY `PARTITION BY` includes REC_SRC | **DV** |
| O3 | `qualify_no_hashdiff_in_partition` | FAIL | HASHDIFF not in `PARTITION BY` | **DV** |
| O4 | `ghost_union_column_order_matches_main` | FAIL | Ghost UNION column order matches | **DV** |
| O5 | `qualify_no_literal_order_by` | FAIL | No `ORDER BY 1` in QUALIFY | **SNOW** — generic idea, Snowflake-specific surface |

### Category P — Business vault quality (6) — *unimplemented in source*

| # | Check | Sev | What it does | Disposition |
|---|---|---|---|---|
| P1 | `pk_not_on_generated_column` | FAIL | PK test must not target a `ROW_NUMBER()` column | **REJECTED** — needs column-level reasoning; brushes the column-level-lineage non-goal |
| P2 | `bv_primary_key_defined` | FAIL | PIT/PB models define a PK or uniqueness test | **DV** |
| P3 | `pit_stg_no_pit_metadata` | WARN | Ephemeral PIT staging omits PIT metadata | **DV** |
| P4 | `pit_has_qualify_dedup` | WARN | UNION'd PIT models dedup on the grain | **POLICY** — `grain-integrity` |
| P5 | `unreferenced_ephemeral_model` | FAIL | Ephemeral models must be `ref()`d somewhere | **REJECTED** — "unreferenced today" is the normal state of a new model (`p04` again); partly covered by `blast.size == 0` |
| P6 | `unused_yaml_schema_file` | WARN | No orphan YAML without a matching `.sql` | **REJECTED** — needs a full-repo listing; our input is a diff |

### Category Q — Conceptual modeling (2)

| # | Check | Sev | What it does | Disposition |
|---|---|---|---|---|
| Q1 | `link_hk_component_collision` | WARN | Two HKs sharing an ordered component list collide | **DV** |
| Q2 | `satellite_pii_not_split` | WARN | PII split into a sibling `*_pii__*` satellite | **POLICY** — `pii-tagging` |

---

## Disposition totals

| Disposition | Count | Share |
|---|---|---|
| **DV** — Data Vault house style | 55 | 69% |
| **SNOW** — dialect / formatting convention | 8 | 10% |
| **POLICY** — covered by the LLM policy pack | 6 | 8% |
| **REJECTED** — generic, considered, declined | 5 | 6% |
| **ADOPTED** | 4 | 5% |
| **Partly adopted** (M1) | 1 | 1% |
| **N/A** — not a code check | 1 | 1% |
| **Total** | **80** | |

---

## Why the policy pack absorbs some of these

dbt-sentinel splits deterministic checks from judgment. Eight source checks map onto
[policy rules](../policies/) rather than `checks.py`, because they need reasoning about
*intent* rather than pattern matching:

| Policy rule | Source equivalents |
|---|---|
| `pii-tagging` | Q2 |
| `incremental-safety` | J3, J4 |
| `grain-integrity` | L2, P4 |
| `test-coverage-keys` | H7 |
| `naming-convention` | the generic core of category G |

(H7, J3, J4, L2, P4 and Q2 are the six rows marked POLICY in the tables above; the
`naming-convention` row is a family resemblance to category G rather than a one-to-one
mapping, since every G check names a Data Vault prefix.)

The policy pack is retrieved and handed to the LLM as context; the LLM returns validated
structured findings. That is a different mechanism from a deterministic check and is
measured separately — see [../evals/BASELINE.md](../evals/BASELINE.md).

---

## What this cost and what it bought

Four checks, 8 fixtures, 16 tests, one new eval, four CI gates. Precision and recall are
1.000 on the purpose-built fixtures — which BASELINE.md explicitly tells the reader to
distrust, since those fixtures were written from the spec by whoever then wrote the checks.

The numbers that carry weight: **28 of 30** severity fixtures (written a week before this
layer existed) draw no check finding at all, and **0** undeclared findings. Both are gated
in CI.

All seven pre-existing published baselines were unchanged by the work — 0.800 / 0.533 /
0.200 on severity and 0.708 / 0.630 / 0.455 on retrieval — and both results files re-ran
byte-identical. That is the evidence for the claim the layer rests on: checks are a peer
of the blast radius, never a component of it.
