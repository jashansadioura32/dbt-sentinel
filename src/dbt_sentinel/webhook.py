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


def handle_event(event: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Day 5 stub: log what day 8 will act on, and say plainly that nothing ran.

    Returning `pipeline_ran: False` rather than a cheerful 200 with no detail keeps the
    stub honest — a deployment that silently does nothing looks identical to one that
    works until someone checks a PR.
    """
    if event == "ping":
        return {"ok": True, "pong": True, "pipeline_ran": False}

    if event not in HANDLED_EVENTS:
        logger.info("ignoring unhandled event: %s", event)
        return {"ok": True, "ignored": True, "reason": f"unhandled event {event}", "pipeline_ran": False}

    summary = summarise_payload(event, payload)
    logger.info("webhook received: %s", json.dumps(summary))

    if not summary["actionable"]:
        return {"ok": True, "ignored": True, "reason": "non-actionable action", "pipeline_ran": False}

    # Day 8: fetch diff -> source manifest -> run pipeline -> post review + status.
    return {
        "ok": True,
        "received": summary,
        "pipeline_ran": False,
        "note": "day-5 skeleton: signature verified and payload logged, pipeline not wired",
    }


# ---------- FastAPI app, imported lazily so the core stays dependency-free ----------


def create_app() -> Any:
    """Built in a factory so importing this module never requires fastapi."""
    from fastapi import FastAPI, Header, HTTPException, Request

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
