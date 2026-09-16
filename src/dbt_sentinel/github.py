"""GitHub App client: authenticate, fetch a PR diff, source a manifest, post a review.

Dependency-free by design — `urllib` rather than `requests`, and a hand-rolled JWT rather
than PyJWT. The JWT is 3 base64 segments and one RS256 signature; pulling in a crypto
stack for that is not justifiable against the allowed-dependency list. The one thing we
cannot hand-roll is RSA signing, so `cryptography` is imported lazily and only when a
private key is actually used — the CLI and the eval suite never touch it.

App auth is two steps and the ordering matters:
  1. A JWT signed with the App's private key proves "I am this App". It cannot read a repo.
  2. Exchanging that JWT for an installation access token proves "I am this App, acting
     for this installation". That token is what every repo call uses, and it expires in
     an hour, so it is fetched per delivery rather than cached across them.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .retry import DEFAULT_ATTEMPTS, RetryableError, parse_retry_after, with_retries

logger = logging.getLogger("dbt_sentinel.github")

API_ROOT = "https://api.github.com"
USER_AGENT = "dbt-sentinel"

# GitHub rejects a JWT whose `iat` is in the future by even a second, so back-date it.
_JWT_CLOCK_SKEW_S = 60
_JWT_TTL_S = 540  # 9 minutes; GitHub's ceiling is 10


class GitHubError(RuntimeError):
    """A GitHub call failed in a way the caller must surface, not swallow."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def build_app_jwt(app_id: str, private_key_pem: str, now: int | None = None) -> str:
    """Sign a short-lived RS256 JWT proving App identity."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as exc:  # pragma: no cover - depends on install state
        raise GitHubError(
            "the `cryptography` package is required to sign the GitHub App JWT. "
            "Run: pip install -e '.[server]'"
        ) from exc

    issued = int(now if now is not None else time.time()) - _JWT_CLOCK_SKEW_S
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {"iat": issued, "exp": issued + _JWT_TTL_S, "iss": app_id}

    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    ).encode("ascii")

    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return signing_input.decode("ascii") + "." + _b64url(signature)


def _rate_limit_wait(headers: Any) -> float | None:
    """Seconds to wait, from `Retry-After` or from `X-RateLimit-Reset`.

    GitHub uses both: secondary limits send `Retry-After`, primary limits send a reset
    epoch. Reading only one means waiting a guessed interval while the API is stating
    the real one.
    """
    if headers is None:
        return None
    explicit = parse_retry_after(headers.get("Retry-After"))
    if explicit is not None:
        return explicit
    reset = headers.get("X-RateLimit-Reset")
    if not reset:
        return None
    try:
        return max(0.0, float(reset) - time.time())
    except (TypeError, ValueError):
        return None


def _attempt(
    method: str, url: str, token: str, data: bytes | None, accept: str
) -> Any:
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", accept)
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", USER_AGENT)
    if data is not None:
        request.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        wait = _rate_limit_wait(getattr(exc, "headers", None))

        # Retryable: the limit will lift, and 5xx is upstream being briefly broken.
        if exc.code == 429 or (exc.code == 403 and "rate limit" in detail.lower()):
            raise RetryableError(
                f"GitHub rate limit on {method} {url}: {detail}", retry_after=wait
            ) from exc
        if exc.code >= 500:
            raise RetryableError(f"{exc.code} on {method} {url}: {detail}") from exc

        # Not retryable: sending a 404 or 422 again does not make it a 200.
        if exc.code == 404:
            raise GitHubError(
                f"404 on {method} {url}. Usually the App is not installed on this repo, "
                f"or lacks the required permission. Response: {detail}"
            ) from exc
        raise GitHubError(f"{exc.code} on {method} {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RetryableError(f"network error on {method} {url}: {exc.reason}") from exc

    if accept != "application/vnd.github+json":
        return payload.decode("utf-8", "replace")
    return json.loads(payload) if payload else None


def _request(
    method: str,
    url: str,
    token: str,
    *,
    body: dict | None = None,
    accept: str = "application/vnd.github+json",
) -> Any:
    data = json.dumps(body).encode() if body is not None else None

    def _log_retry(attempt: int, delay: float, exc: Exception) -> None:
        logger.warning(
            "retrying %s %s in %.1fs (attempt %d): %s", method, url, delay, attempt, exc
        )

    try:
        return with_retries(
            lambda: _attempt(method, url, token, data, accept), on_retry=_log_retry
        )
    except RetryableError as exc:
        # Retries exhausted. Surface as GitHubError so callers keep one exception type.
        raise GitHubError(f"{exc} (after {DEFAULT_ATTEMPTS} attempts)") from exc


@dataclass
class PullRequestRef:
    owner: str
    repo: str
    number: int
    head_sha: str
    base_ref: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


class GitHubClient:
    def __init__(self, token: str):
        self._token = token

    @classmethod
    def for_installation(
        cls, installation_id: int, app_id: str | None = None, private_key: str | None = None
    ) -> GitHubClient:
        app_id = app_id or os.environ.get("GITHUB_APP_ID", "")
        private_key = private_key or _load_private_key()
        if not app_id or not private_key:
            raise GitHubError(
                "GITHUB_APP_ID and GITHUB_PRIVATE_KEY (or GITHUB_PRIVATE_KEY_PATH) must "
                "be set to authenticate as the GitHub App."
            )
        jwt = build_app_jwt(app_id, private_key)
        payload = _request(
            "POST", f"{API_ROOT}/app/installations/{installation_id}/access_tokens", jwt
        )
        return cls(payload["token"])

    # ---------- reads ----------

    def fetch_diff(self, pr: PullRequestRef) -> str:
        """The unified diff, via the `.diff` media type — no local clone required."""
        return _request(
            "GET",
            f"{API_ROOT}/repos/{pr.slug}/pulls/{pr.number}",
            self._token,
            accept="application/vnd.github.v3.diff",
        )

    def fetch_file(self, pr: PullRequestRef, path: str, ref: str | None = None) -> str | None:
        """Raw file contents at a ref, or None when absent."""
        ref = ref or pr.base_ref
        try:
            return _request(
                "GET",
                f"{API_ROOT}/repos/{pr.slug}/contents/{path}?ref={ref}",
                self._token,
                accept="application/vnd.github.raw",
            )
        except GitHubError:
            return None

    def find_manifest_artifact(self, pr: PullRequestRef) -> dict | None:
        """Most recent `manifest`-named artifact from a completed run on the base branch.

        The base branch, deliberately: the manifest must describe the graph *before* this
        PR. A manifest built from the PR head already contains the change, so the blast
        radius of a deletion would be computed against a graph the deletion already left.
        """
        runs = _request(
            "GET",
            f"{API_ROOT}/repos/{pr.slug}/actions/runs"
            f"?branch={pr.base_ref}&status=success&per_page=10",
            self._token,
        )
        for run in (runs or {}).get("workflow_runs", []):
            artifacts = _request(
                "GET",
                f"{API_ROOT}/repos/{pr.slug}/actions/runs/{run['id']}/artifacts",
                self._token,
            )
            for artifact in (artifacts or {}).get("artifacts", []):
                if "manifest" in artifact["name"].lower() and not artifact.get("expired"):
                    return artifact
        return None

    # ---------- writes ----------

    def post_comment(self, pr: PullRequestRef, body: str) -> dict:
        return _request(
            "POST",
            f"{API_ROOT}/repos/{pr.slug}/issues/{pr.number}/comments",
            self._token,
            body={"body": body},
        )

    def upsert_comment(self, pr: PullRequestRef, body: str, marker: str) -> dict:
        """Update our previous comment in place rather than appending a new one.

        A bot that adds a comment per push turns a ten-push PR into a wall of stale
        reviews, and reviewers stop reading all of them. The marker is an HTML comment,
        invisible in rendered Markdown.
        """
        existing = _request(
            "GET",
            f"{API_ROOT}/repos/{pr.slug}/issues/{pr.number}/comments?per_page=100",
            self._token,
        )
        for comment in existing or []:
            if marker in (comment.get("body") or ""):
                return _request(
                    "PATCH",
                    f"{API_ROOT}/repos/{pr.slug}/issues/comments/{comment['id']}",
                    self._token,
                    body={"body": body},
                )
        return self.post_comment(pr, body)

    def set_commit_status(
        self, pr: PullRequestRef, state: str, description: str, context: str = "dbt-sentinel"
    ) -> dict:
        if state not in {"success", "failure", "pending", "error"}:
            raise ValueError(f"invalid commit status state: {state!r}")
        return _request(
            "POST",
            f"{API_ROOT}/repos/{pr.slug}/statuses/{pr.head_sha}",
            self._token,
            # GitHub truncates at 140 characters and returns 422 past it.
            body={"state": state, "description": description[:140], "context": context},
        )


def _load_private_key() -> str:
    """From GITHUB_PRIVATE_KEY, or the path in GITHUB_PRIVATE_KEY_PATH.

    Both supported because platforms differ: Railway and Fly take multi-line secrets
    badly, so a base64 or file-mounted key is often the only workable option.
    """
    inline = os.environ.get("GITHUB_PRIVATE_KEY", "")
    if inline:
        # Tolerate a base64-wrapped key, which is how most hosts want a PEM stored.
        if "BEGIN" not in inline:
            try:
                return base64.b64decode(inline).decode()
            except Exception:  # noqa: BLE001 - fall through to the literal value
                return inline
        return inline.replace("\\n", "\n")

    key_path = os.environ.get("GITHUB_PRIVATE_KEY_PATH", "")
    if key_path and Path(key_path).exists():
        return Path(key_path).read_text(encoding="utf-8")
    return ""
