"""GitHub App webhook receiver — skeleton.

Day 5 scope is deliberately narrow: receive, verify, log. No PR fetching, no manifest
sourcing, no comment posting. Day 8 wires those. Building the signature verification now
moves the setup risk off the riskiest day, which is the whole reason this file exists
before it does anything useful.

Run it:
    pip install -e ".[server]"
    export GITHUB_WEBHOOK_SECRET=...        # the secret you set on the App
    uvicorn dbt_sentinel.webhook:app --port 8000

Verify locally without GitHub:
    python -m dbt_sentinel.webhook --sign '{"action":"opened"}'
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Any

logger = logging.getLogger("dbt_sentinel.webhook")

SIGNATURE_HEADER = "X-Hub-Signature-256"
EVENT_HEADER = "X-GitHub-Event"
DELIVERY_HEADER = "X-GitHub-Delivery"

# Events worth waking up for. Anything else is acknowledged and dropped, so the App can
# be subscribed broadly without this process growing branches it does not handle.
HANDLED_EVENTS = frozenset({"pull_request", "pull_request_review", "ping"})
HANDLED_ACTIONS = frozenset({"opened", "synchronize", "reopened", "ready_for_review"})


class SignatureError(Exception):
    """Raised when a payload's signature is absent, malformed, or wrong."""


def compute_signature(secret: str, body: bytes) -> str:
    """GitHub sends `sha256=<hexdigest>` of the raw body, keyed with the App secret."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(secret: str, body: bytes, header_value: str | None) -> None:
    """Constant-time comparison against the raw request body.

    Two things that look like details and are not:
    - The comparison must be `compare_digest`, not `==`. A byte-by-byte comparison leaks
      the correct prefix through timing and makes the signature forgeable.
    - It must run against the RAW body, before any JSON parsing. Re-serialising changes
      whitespace and key order, so the digest stops matching for legitimate payloads.
    """
    if not secret:
        raise SignatureError(
            "GITHUB_WEBHOOK_SECRET is not set, so payloads cannot be verified. Set it to "
            "the secret configured on the GitHub App."
        )
    if not header_value:
        raise SignatureError(f"missing {SIGNATURE_HEADER} header")
    if not header_value.startswith("sha256="):
        raise SignatureError(f"malformed {SIGNATURE_HEADER}: expected 'sha256=' prefix")

    expected = compute_signature(secret, body)
    if not hmac.compare_digest(expected, header_value):
        raise SignatureError("signature mismatch — payload is not from this GitHub App")


def summarise_payload(event: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Pull the few fields day 8 will need, so the log shows whether routing would work."""
    pr = payload.get("pull_request") or {}
    repo = payload.get("repository") or {}
    return {
        "event": event,
        "action": payload.get("action"),
        "repo": repo.get("full_name"),
        "pr_number": pr.get("number"),
        "head_sha": (pr.get("head") or {}).get("sha"),
        "base_ref": (pr.get("base") or {}).get("ref"),
        "diff_url": pr.get("diff_url"),
        "actionable": (
            event == "pull_request" and payload.get("action") in HANDLED_ACTIONS
        ),
    }


def run_review(summary: dict[str, Any], installation_id: int, client: Any = None) -> dict[str, Any]:
    """Fetch, review, post, and set a commit status for one pull request.

    Wrapped in a broad except on purpose: a webhook handler that raises returns 500, and
    GitHub then retries the same delivery repeatedly. Failing once, visibly, with a
    status of `error` is better than a retry storm that posts nothing either way.
    """
    from .github import GitHubClient, GitHubError, PullRequestRef
    from .pipeline import COMMENT_MARKER, review_pull_request

    owner, _, repo = (summary["repo"] or "").partition("/")
    pr = PullRequestRef(
        owner=owner,
        repo=repo,
        number=summary["pr_number"],
        head_sha=summary["head_sha"] or "",
        base_ref=summary["base_ref"] or "main",
    )

    try:
        github = client or GitHubClient.for_installation(installation_id)
    except GitHubError as exc:
        logger.error("app auth failed: %s", exc)
        return {"ok": False, "pipeline_ran": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - see the docstring; 500 triggers retries
        # The docstring promised a broad except and this clause is what delivers it.
        # Anything a dependency raises that is not a GitHubError — a malformed key, a
        # socket error — previously escaped as an unhandled 500, which GitHub answers
        # by redelivering the same payload repeatedly.
        logger.exception("app auth crashed")
        return {
            "ok": False,
            "pipeline_ran": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    try:
        github.set_commit_status(pr, "pending", "Reviewing dbt changes...")
        outcome = review_pull_request(github, pr)
        if outcome.has_nothing_to_report:
            # A PR that stopped touching dbt models should carry no review at all.
            # Editing the old one down to "nothing to report" leaves what reads as a
            # stale result on a PR that is now clean.
            stale = github.find_comment(pr, COMMENT_MARKER)
            if stale is not None:
                github.delete_comment(pr, stale["id"])
        else:
            github.upsert_comment(pr, outcome.comment, COMMENT_MARKER)
        github.set_commit_status(pr, outcome.status_state, outcome.status_description)
    except GitHubError as exc:
        logger.error("review failed for %s#%s: %s", pr.slug, pr.number, exc)
        try:
            github.set_commit_status(pr, "error", f"Review failed: {exc}")
        except GitHubError:
            pass  # the status is a courtesy; the log is the record
        return {"ok": False, "pipeline_ran": False, "error": str(exc)}

    logger.info(
        "reviewed %s#%s: severity=%s manifest=%s cost=$%.4f %.1fs",
        pr.slug,
        pr.number,
        outcome.severity,
        outcome.manifest_source,
        outcome.cost_usd,
        outcome.latency_s,
    )
    return {
        "ok": True,
        "pipeline_ran": True,
        "severity": outcome.severity,
        "status": outcome.status_state,
        "manifest_source": outcome.manifest_source,
        "agent_ran": outcome.agent_ran,
        "cost_usd": round(outcome.cost_usd, 4),
        "latency_s": round(outcome.latency_s, 2),
        "warnings": len(outcome.warnings),
    }


def handle_event(
    event: str, payload: dict[str, Any], *, client: Any = None, review: bool = True
) -> dict[str, Any]:
    """Route one delivery. `review=False` keeps the day-5 log-only behaviour for smoke
    tests against a live endpoint without posting to anyone's PR."""
    if event == "ping":
        return {"ok": True, "pong": True, "pipeline_ran": False}

    if event not in HANDLED_EVENTS:
        logger.info("ignoring unhandled event: %s", event)
        return {"ok": True, "ignored": True, "reason": f"unhandled event {event}", "pipeline_ran": False}

    summary = summarise_payload(event, payload)
    logger.info("webhook received: %s", json.dumps(summary))

    if not summary["actionable"]:
        return {"ok": True, "ignored": True, "reason": "non-actionable action", "pipeline_ran": False}

    installation_id = (payload.get("installation") or {}).get("id")
    if not review:
        return {"ok": True, "received": summary, "pipeline_ran": False, "note": "review disabled"}
    if not installation_id:
        return {
            "ok": False,
            "pipeline_ran": False,
            "error": "payload carries no installation id, so the App cannot authenticate",
        }

    return run_review(summary, installation_id, client=client)


# ---------- FastAPI app, imported lazily so the core stays dependency-free ----------


def create_app() -> Any:
    """Built in a factory so importing this module never requires fastapi.

    `from __future__ import annotations` turns every annotation in this module into a
    string, and FastAPI resolves those against *module* globals. `Request` is imported
    inside this function, so it is not there to find: FastAPI fell back to treating
    `request` as a query parameter and every delivery 422'd before the signature was
    ever checked. Binding the names into the module namespace below is what makes the
    lazy import and the string annotations coexist.
    """
    from fastapi import FastAPI, Header, HTTPException, Request

    # Deliberate, not incidental: the handler's annotations are strings that FastAPI
    # looks up here. No test caught the 422 because none of them booted the app.
    globals().setdefault("Request", Request)

    app = FastAPI(title="dbt-sentinel webhook", version="0.5.0")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "secret_configured": bool(os.environ.get("GITHUB_WEBHOOK_SECRET")),
        }

    @app.post("/webhook")
    async def webhook(
        request: Request,
        x_github_event: str = Header(default=""),
        x_hub_signature_256: str | None = Header(default=None),
    ) -> dict[str, Any]:
        body = await request.body()  # raw bytes, before parsing — see verify_signature
        secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
        try:
            verify_signature(secret, body, x_hub_signature_256)
        except SignatureError as exc:
            # 401 for a bad signature, 500 for a misconfigured server: the sender should
            # be able to tell "you are not authorised" from "we are broken".
            status = 500 if "not set" in str(exc) else 401
            raise HTTPException(status_code=status, detail=str(exc)) from exc

        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc

        return handle_event(x_github_event, payload)

    return app


def __getattr__(name: str) -> Any:
    """`uvicorn dbt_sentinel.webhook:app` without importing fastapi at module import."""
    if name == "app":
        return create_app()
    raise AttributeError(name)


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="dbt_sentinel.webhook")
    parser.add_argument("--sign", help="print the signature header for a JSON body")
    parser.add_argument("--secret", default=os.environ.get("GITHUB_WEBHOOK_SECRET", ""))
    args = parser.parse_args(argv)

    if args.sign:
        if not args.secret:
            print("error: pass --secret or set GITHUB_WEBHOOK_SECRET")
            return 2
        print(compute_signature(args.secret, args.sign.encode("utf-8")))
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
