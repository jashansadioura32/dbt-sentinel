# Deploying the GitHub App

Everything requiring credentials is yours to do — App creation, secrets, and install.
Placeholders are marked `<...>`. Nothing in this repo contains a key.

## 1. Create the GitHub App (you)

github.com → Settings → Developer settings → GitHub Apps → **New GitHub App**

| Field | Value |
|---|---|
| Name | `dbt-sentinel` (must be globally unique) |
| Homepage URL | your repo URL |
| Webhook URL | `https://<your-deployment>/webhook` |
| Webhook secret | generate one: `python -c "import secrets; print(secrets.token_hex(32))"` |

**Permissions** — repository level, nothing more:

| Permission | Access | Why |
|---|---|---|
| Pull requests | Read & write | Read the diff, post the review comment |
| Contents | Read-only | Fetch `target/manifest.json` from the base branch |
| Commit statuses | Read & write | Set the blocking check |
| Actions | Read-only | Find the manifest CI artifact |

**Subscribe to events:** Pull request. Nothing else.

Then: **Generate a private key** (downloads a `.pem`), and note the **App ID**.

## 2. Install it on a repo (you)

App settings → Install App → pick the repo. Note the **installation ID** from the URL
(`.../installations/<id>`) — useful for debugging, though deliveries carry it.

## 3. Set the secrets (you)

| Variable | Value |
|---|---|
| `GITHUB_APP_ID` | the App ID |
| `GITHUB_WEBHOOK_SECRET` | the secret from step 1 |
| `GITHUB_PRIVATE_KEY` | contents of the `.pem` |
| `OPENAI_API_KEY` | optional; without it reviews are deterministic-only and say so |

Most hosts mangle multi-line secrets. Two supported workarounds:

```bash
# base64 the PEM (github.py decodes it automatically)
base64 -w0 your-app.private-key.pem

# or mount the file and point at it
GITHUB_PRIVATE_KEY_PATH=/secrets/app.pem
```

## 4. Deploy

```bash
pip install -e ".[server,agent]"
uvicorn dbt_sentinel.webhook:app --host 0.0.0.0 --port ${PORT:-8000}
```

A `Procfile` and `railway.json` are included. On Railway: new project → deploy from repo
→ add the variables above. Fly and Render work the same way.

Verify: `curl https://<your-deployment>/health` → `{"ok": true, "secret_configured": true}`.

Then verify the endpoint accepts a signed delivery, which `/health` does not prove:

```bash
BODY='{"action":"opened"}'
SIG=$(python -m dbt_sentinel.webhook --sign "$BODY" --secret "$GITHUB_WEBHOOK_SECRET")
curl -s -o /dev/null -w '%{http_code}
' -X POST https://<your-deployment>/webhook   -H "X-GitHub-Event: pull_request" -H "X-Hub-Signature-256: $SIG" -d "$BODY"
```

**200** is correct (the stub payload has no installation id, so the body reports that
and no review runs). **401** means the secret differs, **500** means none reached the
process, and **422** means the handler is not binding the request body at all.
If `secret_configured` is false, the secret did not reach the process and **every
delivery will 500** — fix that before redelivering.

## 5. Test the wiring

App settings → Advanced → **Recent Deliveries** shows every attempt with its response.
Open a PR touching a dbt model; expect a `pending` status, then a comment and a
`success`/`failure` status.

Locally, without deploying:

```bash
# tunnel
ngrok http 8000

# or replay a payload by hand
python -m dbt_sentinel.webhook --sign '{"action":"opened"}' --secret "$GITHUB_WEBHOOK_SECRET"
curl -X POST localhost:8000/webhook \
  -H "X-GitHub-Event: pull_request" \
  -H "X-Hub-Signature-256: <output above>" \
  -d '{"action":"opened"}'
```

## 6. Manifest sourcing

The review needs a manifest describing the graph **before** the PR. In preference order:

1. **CI artifact from the base branch** — correct and current. *Artifact download is not
   yet implemented; the comment says so when one is found.*
2. **Committed `target/manifest.json` on the base branch** — what runs today. Usually
   stale, so the comment discloses it and warns that blast radius may be under-reported.

To publish the artifact (recommended once download lands):

```yaml
name: dbt manifest
on:
  push:
    branches: [main]
jobs:
  compile:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install dbt-core dbt-duckdb
      - run: dbt deps && dbt compile
      - uses: actions/upload-artifact@v4
        with:
          name: manifest
          path: target/manifest.json
```

A manifest built from the **PR head** is wrong: it already contains the change, so a
deleted model's blast radius is computed against a graph the deletion already left.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Delivery 500 with a plain-text body | `GITHUB_PRIVATE_KEY` is not a readable PEM — usually a multi-line secret the host flattened. Set it as `base64 -w0 your-app.private-key.pem`. The response now names this instead of returning a bare 500 |
| Delivery 401 `missing X-Hub-Signature-256` | The **App's** Webhook secret field is empty, so GitHub signs nothing. The field is labelled optional and is easy to skip; it must match `GITHUB_WEBHOOK_SECRET` exactly |
| Every delivery 422 | Was a real bug, fixed: `Request` resolved to a query parameter under `from __future__ import annotations`, so nothing reached the signature check. Pinned by `tests/test_webhook_http.py` |
| Every delivery 401 | `GITHUB_WEBHOOK_SECRET` differs from the App's |
| Every delivery 500 | Secret not set at all — `/health` shows `secret_configured: false` |
| 404 fetching the diff | App not installed on that repo, or missing Pull requests permission |
| Comment posted, no status | Commit statuses permission missing |
| `cryptography` ImportError | `pip install -e ".[server]"` |
| Review says "no manifest found" | No `target/manifest.json` on the base branch |
| Comment says agent skipped | `OPENAI_API_KEY` not set on the server (reviews still work) |
