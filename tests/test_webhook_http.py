"""HTTP-layer tests for the webhook. These boot the real FastAPI app.

Why this file exists: every other webhook test calls `handle_event` directly, so the
whole HTTP surface — routing, parameter binding, status codes — was never exercised.
A bug that made the app reject **every** delivery with 422 survived the full suite
because of it.

The bug: `from __future__ import annotations` turns the handler's annotations into
strings, and FastAPI resolves those against module globals. `Request` was imported
inside `create_app`, so the lookup failed and FastAPI bound `request` as a query
parameter. Every delivery 422'd before the signature was checked, which also means
the 401/500 split that DEPLOYMENT.md documents was unreachable.

Anything asserting a status code here should be treated as load-bearing: a GitHub App
whose endpoint 422s is completely non-functional, and nothing else in the suite notices.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

fastapi_testclient = pytest.importorskip(
    "fastapi.testclient", reason="server extra not installed"
)

from dbt_sentinel.webhook import create_app  # noqa: E402

SECRET = "test-secret-not-a-real-one"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    return fastapi_testclient.TestClient(create_app(), raise_server_exceptions=False)


# ---------- the binding bug itself ----------


def test_the_request_object_is_bound_not_treated_as_a_query_param():
    """The direct assertion on the defect, independent of any status code.

    If `request` appears as a query parameter, FastAPI never hands the handler the raw
    body and every delivery fails validation.
    """
    app = create_app()
    route = next(r for r in app.routes if getattr(r, "path", "") == "/webhook")

    assert route.dependant.request_param_name == "request"
    assert [p.name for p in route.dependant.query_params] == []


def test_a_correctly_signed_delivery_is_not_rejected_by_validation(client):
    """The regression test proper: this returned 422 for every possible input."""
    body = json.dumps({"action": "opened"}).encode()
    response = client.post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": _sign(body),
        },
    )
    assert response.status_code != 422, (
        "the handler rejected a valid delivery at the validation layer — "
        "`request` is probably being bound as a query parameter again"
    )
    assert response.status_code == 200


# ---------- the status-code contract DEPLOYMENT.md publishes ----------


def test_a_bad_signature_is_401(client):
    body = b'{"action":"opened"}'
    response = client.post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": "sha256=deadbeef",
        },
    )
    assert response.status_code == 401


def test_a_missing_signature_is_401(client):
    response = client.post(
        "/webhook",
        content=b'{"action":"opened"}',
        headers={"X-GitHub-Event": "pull_request"},
    )
    assert response.status_code == 401


def test_an_unset_secret_is_500_not_401(monkeypatch):
    """The sender must be able to tell "you are not authorised" from "we are broken".

    DEPLOYMENT.md's troubleshooting table keys on exactly this split: every delivery
    401 means the secrets differ, every delivery 500 means no secret reached the
    process.
    """
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    client = fastapi_testclient.TestClient(create_app(), raise_server_exceptions=False)

    response = client.post(
        "/webhook",
        content=b'{"action":"opened"}',
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": "sha256=abc",
        },
    )
    assert response.status_code == 500


def test_malformed_json_under_a_valid_signature_is_400(client):
    """Signature first, then parsing: a valid signature with bad JSON is the sender's
    bug, not an auth failure."""
    body = b"{not json"
    response = client.post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": _sign(body),
        },
    )
    assert response.status_code == 400


# ---------- /health, which is the documented way to check a deployment ----------


def test_health_reports_the_secret_as_configured(client):
    payload = client.get("/health").json()
    assert payload == {"ok": True, "secret_configured": True}


def test_health_reports_a_missing_secret(monkeypatch):
    """DEPLOYMENT.md tells the operator to check this before redelivering."""
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    client = fastapi_testclient.TestClient(create_app())
    assert client.get("/health").json()["secret_configured"] is False
