# Deployment log — first live review

**On 2026-09-25 the GitHub App posted a correct HIGH-severity review on a real pull
request and set a failing commit status.** Day 8's acceptance criteria are met.

This document is the record of getting there, including the four bugs that only appeared
against real infrastructure. The bugs are the point: every one of them passed the full
local test suite, and three of them were invisible to any test that did not make a real
network call.

| | |
|---|---|
| Deployment | Railway, `uvicorn dbt_sentinel.webhook:app` |
| Target repo | [jashansadioura32/jeffle-shop](https://github.com/jashansadioura32/jeffle-shop) — extended jaffle_shop, 16 nodes, 3 exposures, 1 contracted model |
| First successful delivery | 2026-09-25 15:35:34 UTC |
| Latency | 6.5s end to end |
| Cost | $0.00 — the agent degraded; see below |

---

## The review that landed

PR #1 renames `customer_id` to `cust_id` in `models/staging/stg_orders.sql`. One line.

```markdown
## Blast radius

### 🔴 `stg_orders` — HIGH · 7 downstream
- Removed column(s) `customer_id` with 7 downstream node(s)
- Reaches 1 contracted model(s): fct_order_payments
- Reaches 3 exposure(s): customer_success_churn, finance_month_end, exec_dashboard
  (owners: Analytics Team, Data Science, Finance Data)
- Downstream: `customers`, `fct_order_payments`, `orders`, `customer_success_churn`,
  `finance_month_end`, `rpt_customer_revenue` +1 more

## Reviewer findings

> ⚠️ **Agent unavailable — deterministic analysis only.** RateLimitError: Error code: 429
> — You have no credits remaining. [...] code: 'credit_balance_exhausted'

The blast radius above is unaffected: it is computed by graph traversal and does not
depend on the model.

<details><summary>⚠️ Warnings and caveats</summary>

- Using the committed manifest at `target/manifest.json` on `main`. If it predates recent
  model changes, blast radius is under-reported — publish a `manifest` artifact from CI
  for an accurate graph.

</details>

---
_dbt-sentinel · manifest: `committed:target/manifest.json` · agent: no · 6.5s_
```

Commit status: `pending` → **`failure`**, "Breaking change detected across 1 changed
model(s)".

### The degradation notice is the most useful thing in that comment

The OpenAI account had no credits, so the reviewer agent failed. The review completed
anyway, named the reason, and stated that the structural analysis does not depend on the
model.

That is design rule 5 — *degrade, don't crash* — working on real infrastructure rather
than asserted in a document. It is better evidence than a clean agent run would have
been: a clean run demonstrates the happy path, which was never in doubt. Four rules are
visible in that one comment:

| Rule | Where it shows |
|---|---|
| 2 — reach amplifies risk, does not create it | HIGH came from the removed column; reach set the volume |
| 3 — the LLM never writes the comment | Every heading and bullet is template code; the agent contributed nothing |
| 4 — unresolvable input is surfaced | The stale-manifest caveat is disclosed, not hidden |
| 5 — degrade, don't crash | The agent failed; the review still posted, with the reason |

---

## Four bugs that only real infrastructure found

Each passed the full local suite. Listed in the order they surfaced, because the order is
the lesson: every fix revealed the next fault, and the second fix is what made the third
and fourth diagnosable at all.

### 1. The build failed on a file that was committed

```
OSError: License file does not exist: LICENSE
```

`pyproject.toml` declared `license = { file = "LICENSE" }`. Hatchling hard-fails metadata
generation when that file is absent from the build context, and Nixpacks' upload omits
it. The file was committed, present on `origin/main`, and in every local clone.

Invisible locally because `pip install -e .` never re-derives metadata from a fresh
context. Fixed with the PEP 639 form — `license = "MIT"` plus
`license-files = ["LICENSE"]`, whose glob tolerates a miss.

A second fault was one step behind it: the build log showed Railway running plain
`pip install .`, not `railway.json`'s `buildCommand`, which installs only `pyyaml`.
`fastapi`, `uvicorn` and `cryptography` would all have been missing, and the server would
have failed to start *after a build that reported success*. `nixpacks.toml` pins the
phases so the extras are not optional.

**Test added:** `tests/test_packaging.py` builds a real wheel from a context holding only
the source tree. Nothing else in the suite built one, which is exactly why
`pip install -e .` passing locally proved nothing.

### 2. The webhook rejected every delivery with 422

The endpoint could never have worked. `from __future__ import annotations` — required by
this project's own conventions — turns the handler's annotations into strings, and FastAPI
resolves those against *module* globals. `Request` was imported inside `create_app`, so
the lookup failed and FastAPI bound `request` as a **query parameter**. Every delivery
failed validation before the signature check, which also made the 401/500 split the
deployment doc documents unreachable.

Found by booting the app and posting a signed payload at it. Every existing webhook test
called `handle_event` directly; none started the HTTP layer.

**Test added:** `tests/test_webhook_http.py` boots the real app and covers the whole
documented status contract. 6 of its first 8 tests fail without the one-line fix.

### 3. A mangled private key returned a bare 500

Railway flattened the `.pem`'s newlines. `cryptography` raised `ValueError`, which is not
a `GitHubError`, so it escaped `run_review` and reached FastAPI unhandled:

```
HTTP 500
Content-Type: text/plain
Internal Server Error
```

The least useful response a webhook can give. It names nothing actionable, and **GitHub
answers a 500 by redelivering the same payload repeatedly** — so a misconfigured
credential became a retry storm.

Two fixes. `build_app_jwt` now raises a `GitHubError` naming the likely cause (a host
flattening a multi-line secret) and both remedies. And `run_review` grew the broad
`except` its own docstring already promised, so nothing a dependency raises escapes as a
500.

**This fix is what made the next two bugs findable.** Before it, every misconfiguration
looked identical. After it, each one named itself on the first attempt.

### 4. The JWT's `iss` claim was a string

```
401 'Issuer' claim ('iss') must be an Integer
```

The App ID arrives from an environment variable, so it is a `str`, and
`{"iss": app_id}` serialised it as a JSON string. GitHub validates the claim type and
rejected every installation-token request, so App authentication could never have
succeeded.

No local check could catch it: the JWT was well formed, correctly signed, three segments,
backdated `iat`. Only GitHub's claim validation sees the type.

Worse — **an existing test pinned the bug in place.**
`test_jwt_has_three_segments_and_backdated_iat` asserted `iss == "12345"`. It checked
everything about the token's shape and asserted the one claim wrongly. That assertion now
requires an `int` and carries a comment explaining why, so the next reader does not
"correct" it back.

**Verified against the live API rather than locally:** `GET /app` returns the app, and
`POST /app/installations/{id}/access_tokens` issues a token.

### And one operator error, which is the point of fix 3

`GITHUB_APP_ID` was set to `base64 -w0 5071196.pem` — a shell command pasted into the
variable instead of the number. The response was HTTP 200 with:

```json
{"ok": false, "error": "GITHUB_APP_ID must be the App's numeric ID, but is
'base64 -w0 5071196.pem\n'. Find it on the App's settings page under 'App ID' — it is a
number, not the App name or the Client ID."}
```

Diagnosed and fixed in one round. An hour earlier the same class of mistake returned
`Internal Server Error`. That difference is the entire value of fix 3, and the reason
"error messages tell the user what to do next" is a project convention rather than a
preference.

---

## What this changes about the project's claims

Before this deployment the honest status was "wired and tested against a fake client; no
real PR has received a comment". That is now closed, and two of the bugs above justify a
sharper version of a claim the case study already made.

**Every agent test injects a fake client, and that was disclosed as a limitation.** Bugs 2
and 4 are the same shape one layer down: the *GitHub* integration was also only ever
tested against fakes, and both faults were total — not degraded behaviour but a path that
could never have worked. The project had 196 passing tests and a webhook that rejected
100% of deliveries.

The general lesson is narrower than "test against real systems". It is that **a test which
constructs both sides of an interface cannot discover that the real other side disagrees.**
`test_jwt_has_three_segments_and_backdated_iat` is the clearest case: it was a real test,
it passed for four days, and it asserted the bug.

---

## Reproducing

Deployment steps are in [DEPLOYMENT.md](DEPLOYMENT.md), whose troubleshooting table now
carries every failure above, keyed by the symptom an operator actually sees. The
`/health` endpoint reports whether the webhook secret reached the process; a signed-delivery
curl is included, because `/health` returning `ok` only proves the process booted — it was
green throughout bugs 2, 3 and 4.

## Still open

- **The agent has never made a real API call.** Blocked on OpenAI credits, not on code.
  Its plumbing is proven; its review quality is unknown.
- **`evals/RESULTS_V1.md` does not exist.** The agent-vs-baseline comparison needs the same
  credits. `evals/compare.py` exits 2 and writes nothing rather than publishing a
  fabricated arm.
- **CI-artifact manifest download is unimplemented.** The committed manifest works and the
  comment discloses that it may be stale.
