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


# ---------- a malformed private key must not become a bare 500 ----------


def test_a_mangled_private_key_returns_json_not_a_bare_500(monkeypatch):
    """Found on a live deployment. Railway flattened the .pem's newlines, and the
    ValueError from `cryptography` was not a GitHubError, so it escaped `run_review`
    and FastAPI answered with a text/plain "Internal Server Error".

    Two reasons that is the worst possible response: it names nothing an operator can
    act on, and GitHub answers a 500 by redelivering the same payload repeatedly.
    """
    from dbt_sentinel.webhook import run_review

    monkeypatch.setenv("GITHUB_APP_ID", "123456")
    monkeypatch.setenv(
        "GITHUB_PRIVATE_KEY",
        "-----BEGIN RSA PRIVATE KEY-----\nNOTAREALKEY\n-----END RSA PRIVATE KEY-----",
    )
    summary = {
        "repo": "owner/repo",
        "pr_number": 1,
        "head_sha": "abc123",
        "base_ref": "main",
        "event": "pull_request",
        "action": "synchronize",
        "actionable": True,
    }

    result = run_review(summary, 164833930)

    assert result["ok"] is False
    assert result["pipeline_ran"] is False
    assert "GITHUB_PRIVATE_KEY" in result["error"]


def test_the_key_error_tells_the_operator_what_to_do(monkeypatch):
    """Error messages say what to do next, not only what went wrong."""
    from dbt_sentinel.github import GitHubError, build_app_jwt

    with pytest.raises(GitHubError) as caught:
        build_app_jwt("123456", "not a pem at all")

    message = str(caught.value)
    assert "base64" in message
    assert "GITHUB_PRIVATE_KEY_PATH" in message


def test_run_review_never_raises_on_an_unexpected_auth_failure(monkeypatch):
    """The broad except the docstring promises. A raise here means a retry storm."""
    import dbt_sentinel.webhook as wh
    from dbt_sentinel import github as gh

    def boom(*_args, **_kwargs):
        raise RuntimeError("socket exploded")

    monkeypatch.setattr(gh.GitHubClient, "for_installation", classmethod(boom))
    summary = {
        "repo": "owner/repo",
        "pr_number": 1,
        "head_sha": "abc",
        "base_ref": "main",
        "event": "pull_request",
        "action": "synchronize",
        "actionable": True,
    }

    result = wh.run_review(summary, 1)
    assert result["ok"] is False
    assert "RuntimeError" in result["error"]


# ---------- the JWT's `iss` claim must be a number ----------


def _throwaway_pem() -> str:
    """A real 2048-bit key generated per call; never a committed secret."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def test_the_iss_claim_is_an_integer_not_a_string():
    """Found against the live API. GitHub answered every installation-token request
    with `401 'Issuer' claim ('iss') must be an Integer`.

    The App ID arrives from an environment variable, so it is a str, and
    `{"iss": app_id}` serialised it as a JSON string. The signature is valid either
    way and the token has three well-formed segments, so no local check on the JWT's
    shape catches this — only GitHub's claim validation does. Hence an assertion on
    the decoded claim type rather than on the token.
    """
    import base64
    import json as _json

    from dbt_sentinel.github import build_app_jwt

    token = build_app_jwt("5071196", _throwaway_pem())
    claims = _json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))

    assert isinstance(claims["iss"], int), f"iss is {type(claims['iss']).__name__}"
    assert claims["iss"] == 5071196


def test_a_non_numeric_app_id_is_rejected_with_a_useful_message():
    """Pasting the App *name* or Client ID into GITHUB_APP_ID is the likely mistake,
    and it must not surface as an opaque 401 from GitHub."""
    from dbt_sentinel.github import GitHubError, build_app_jwt

    with pytest.raises(GitHubError, match="numeric ID"):
        build_app_jwt("dbt-sentinal", _throwaway_pem())


def test_an_app_id_with_stray_whitespace_still_works():
    """Copy-paste from a settings page picks up a trailing newline or space."""
    import base64
    import json as _json

    from dbt_sentinel.github import build_app_jwt

    token = build_app_jwt("  5071196\n", _throwaway_pem())
    claims = _json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
    assert claims["iss"] == 5071196


def test_the_jwt_ttl_stays_inside_githubs_ten_minute_cap():
    """GitHub rejects a token whose exp is more than 10 minutes out, clock skew
    included. The margin is what makes the skew allowance safe."""
    import base64
    import json as _json

    from dbt_sentinel.github import build_app_jwt

    token = build_app_jwt("5071196", _throwaway_pem())
    claims = _json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
    assert 0 < claims["exp"] - claims["iat"] <= 600
