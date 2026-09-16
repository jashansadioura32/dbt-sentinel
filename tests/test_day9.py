"""Regression tests for day-9 hardening: retry policy, pricing, instrumentation.

No test sleeps. `with_retries` takes an injectable `sleep`, which is the only reason a
backoff policy can be tested at all — a suite that actually waited out three exponential
backoffs would take ten seconds and get deleted.
"""

from __future__ import annotations

import email.utils
import datetime as dt

import pytest

from dbt_sentinel.pricing import (
    PRICE_PER_MTOK_INPUT,
    PRICE_PER_MTOK_OUTPUT,
    cost_usd,
)
from dbt_sentinel.retry import (
    DEFAULT_ATTEMPTS,
    MAX_DELAY_S,
    RetryableError,
    backoff_delay,
    parse_retry_after,
    with_retries,
)


# ---------- pricing lives in one place ----------


def test_pricing_matches_the_pinned_model():
    from dbt_sentinel.agent import DEFAULT_MODEL

    assert DEFAULT_MODEL == "gpt-4o"
    assert (PRICE_PER_MTOK_INPUT, PRICE_PER_MTOK_OUTPUT) == (2.50, 10.00)


def test_eval_harness_and_package_share_one_price_list():
    """A cost in a PR comment and a cost in the eval report must not be able to disagree."""
    from evals import compare

    assert compare.cost_usd is cost_usd
    assert compare.PRICE_PER_MTOK_INPUT == PRICE_PER_MTOK_INPUT


def test_pipeline_prices_without_importing_the_eval_harness():
    """pipeline.py used to inject the repo root on sys.path to import evals.compare — a
    library reaching into its own test harness, which breaks once installed elsewhere."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "src" / "dbt_sentinel" / "pipeline.py"
    ).read_text(encoding="utf-8")
    assert "evals.compare" not in source
    assert "sys.path" not in source


def test_cost_math_is_per_million_tokens():
    assert cost_usd(1_000_000, 0) == pytest.approx(2.50)
    assert cost_usd(0, 1_000_000) == pytest.approx(10.00)
    assert cost_usd(0, 0) == 0.0


# ---------- Retry-After parsing ----------


def test_delta_seconds_form():
    assert parse_retry_after("30") == 30.0


def test_http_date_form():
    """Secondary rate limits send an HTTP date, not an integer."""
    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=45)
    parsed = parse_retry_after(email.utils.format_datetime(future))
    assert parsed is not None and 40 <= parsed <= 50


def test_past_date_is_clamped_to_zero_not_negative():
    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=60)
    assert parse_retry_after(email.utils.format_datetime(past)) == 0.0


def test_absent_or_junk_header_is_none():
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
    assert parse_retry_after("soon-ish") is None


# ---------- backoff policy ----------


def test_server_instruction_wins_over_our_guess():
    """Guessing a backoff while the API states the real one earns a longer ban."""
    assert backoff_delay(0, retry_after=7.0) == 7.0


def test_backoff_grows_exponentially():
    delays = [backoff_delay(i, jitter=False) for i in range(4)]
    assert delays == sorted(delays)
    assert delays[0] < delays[-1]


def test_every_delay_is_capped():
    """GitHub redelivers a webhook that does not answer; an unbounded wait earns a
    second delivery on top of the first and posts the review twice."""
    assert backoff_delay(50, jitter=False) <= MAX_DELAY_S
    assert backoff_delay(0, retry_after=9999.0) <= MAX_DELAY_S


def test_jitter_desynchronises_concurrent_retries():
    """Without jitter, deliveries that hit one rate limit retry in lockstep forever."""
    samples = {backoff_delay(2) for _ in range(30)}
    assert len(samples) > 1


# ---------- with_retries ----------


def test_succeeds_without_retrying():
    calls = []
    result = with_retries(lambda: calls.append(1) or "ok", sleep=lambda _: None)
    assert result == "ok" and len(calls) == 1


def test_retries_then_succeeds():
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise RetryableError("transient")
        return "recovered"

    assert with_retries(flaky, sleep=lambda _: None) == "recovered"
    assert len(attempts) == 3


def test_exhausts_attempts_and_reraises_the_last_error():
    attempts = []

    def always_fails():
        attempts.append(1)
        raise RetryableError("still down")

    with pytest.raises(RetryableError, match="still down"):
        with_retries(always_fails, sleep=lambda _: None)
    assert len(attempts) == DEFAULT_ATTEMPTS


def test_non_retryable_error_is_not_retried():
    """A 404 from a missing install does not become a 200 by being sent again."""
    attempts = []

    def hard_failure():
        attempts.append(1)
        raise ValueError("permanent")

    with pytest.raises(ValueError):
        with_retries(hard_failure, sleep=lambda _: None)
    assert len(attempts) == 1


def test_on_retry_callback_reports_each_wait():
    seen = []
    def flaky():
        if len(seen) < 1:
            raise RetryableError("transient", retry_after=3.0)
        return "ok"

    with_retries(
        flaky, sleep=lambda _: None, on_retry=lambda n, d, e: seen.append((n, d))
    )
    assert seen == [(1, 3.0)]


def test_sleep_is_called_with_the_computed_delay():
    slept: list[float] = []

    def flaky():
        if len(slept) < 2:
            raise RetryableError("transient", retry_after=2.0)
        return "ok"

    with_retries(flaky, sleep=slept.append)
    assert slept == [2.0, 2.0]


# ---------- GitHub classification ----------


class _Headers(dict):
    def get(self, key, default=None):  # case-insensitive like real HTTP headers
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


def _http_error(code: int, body: str = "", headers: dict | None = None):
    import io
    import urllib.error

    return urllib.error.HTTPError(
        "https://api.github.com/x", code, "err", _Headers(headers or {}), io.BytesIO(body.encode())
    )


def _run_attempt(monkeypatch, exc):
    from dbt_sentinel import github as gh

    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(gh.urllib.request, "urlopen", boom)
    return gh


def test_429_is_retryable_and_carries_the_server_wait(monkeypatch):
    gh = _run_attempt(monkeypatch, _http_error(429, "slow down", {"Retry-After": "12"}))
    with pytest.raises(RetryableError) as info:
        gh._attempt("GET", "https://api.github.com/x", "t", None, "application/vnd.github+json")
    assert info.value.retry_after == 12.0


def test_rate_limit_reset_header_is_used_when_no_retry_after(monkeypatch):
    import time as _t

    reset = str(int(_t.time()) + 30)
    gh = _run_attempt(
        monkeypatch, _http_error(403, "API rate limit exceeded", {"X-RateLimit-Reset": reset})
    )
    with pytest.raises(RetryableError) as info:
        gh._attempt("GET", "https://api.github.com/x", "t", None, "application/vnd.github+json")
    assert info.value.retry_after is not None and 20 <= info.value.retry_after <= 35


def test_5xx_is_retryable(monkeypatch):
    gh = _run_attempt(monkeypatch, _http_error(502, "bad gateway"))
    with pytest.raises(RetryableError):
        gh._attempt("GET", "https://api.github.com/x", "t", None, "application/vnd.github+json")


def test_404_is_not_retryable_and_explains_the_usual_cause(monkeypatch):
    from dbt_sentinel.github import GitHubError

    gh = _run_attempt(monkeypatch, _http_error(404, "Not Found"))
    with pytest.raises(GitHubError, match="not installed"):
        gh._attempt("GET", "https://api.github.com/x", "t", None, "application/vnd.github+json")


def test_422_is_not_retryable(monkeypatch):
    from dbt_sentinel.github import GitHubError

    gh = _run_attempt(monkeypatch, _http_error(422, "Unprocessable"))
    with pytest.raises(GitHubError):
        gh._attempt("GET", "https://api.github.com/x", "t", None, "application/vnd.github+json")


def test_network_error_is_retryable(monkeypatch):
    import urllib.error

    gh = _run_attempt(monkeypatch, urllib.error.URLError("connection reset"))
    with pytest.raises(RetryableError, match="network error"):
        gh._attempt("GET", "https://api.github.com/x", "t", None, "application/vnd.github+json")


def test_exhausted_retries_surface_as_one_exception_type(monkeypatch):
    """Callers catch GitHubError; a leaked RetryableError would bypass every handler.

    Patches `retry.time.sleep`, not `github.time.sleep`: `_request` delegates to
    `with_retries`, which sleeps via its own module. An earlier version patched the
    wrong one, missed entirely, and sat through 3.5s of real backoff while looking
    like it had mocked the clock.
    """
    from dbt_sentinel import github as gh
    from dbt_sentinel import retry as retry_module
    from dbt_sentinel.github import GitHubError

    monkeypatch.setattr(retry_module.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        gh, "_attempt", lambda *a, **k: (_ for _ in ()).throw(RetryableError("down"))
    )
    with pytest.raises(GitHubError, match="attempts"):
        gh._request("GET", "https://api.github.com/x", "t")


# ---------- quota exhaustion is permanent, not transient ----------


def _quota_error(code: str = "insufficient_quota", structured: bool = False):
    """Mimics the OpenAI SDK's RateLimitError for an exhausted credit balance.

    `structured=False` is the shape a real key with no credits actually produces, checked
    against the live SDK: `body` IS a populated dict, but `error.code` and `error.type`
    both come back None, with the code present only in the stringified message. An
    earlier version of this helper set code/type itself and so only ever exercised the
    structured branch -- it passed while the path that fires in production went untested.
    """

    class RateLimitError(Exception):
        def __init__(self):
            super().__init__(
                f"Error code: 429 - {{'error': {{'message': 'You have no credits "
                f"remaining.', 'type': '{code}', 'code': '{code}'}}}}"
            )
            self.status_code = 429
            if structured:
                self.body = {"error": {"message": "no credits", "type": code, "code": code}}
            else:
                self.body = {"error": {"message": "You have no credits remaining.",
                                       "type": None, "code": None}}

    return RateLimitError()


def test_exhausted_credits_is_not_retried():
    """A 429 for an exhausted balance is permanent until someone adds money. Retrying it
    burns three attempts and a backoff per call across every fixture and still fails.
    Found by smoke-testing a real key with no credits."""
    from dbt_sentinel.agent import _is_transient

    # The real-world shape first: body populated, code/type None, code only in the text.
    assert _is_transient(_quota_error("insufficient_quota")) is False
    assert _is_transient(_quota_error("credit_balance_exhausted")) is False
    # And the structured shape, in case a future SDK version populates the fields.
    assert _is_transient(_quota_error("insufficient_quota", structured=True)) is False


def test_a_real_rate_limit_is_still_retried():
    """The fix must not make every 429 permanent -- a genuine rate limit does lift."""
    from dbt_sentinel.agent import _is_transient

    class RateLimitError(Exception):
        def __init__(self):
            super().__init__("Error code: 429 - rate limit exceeded, please slow down")
            self.status_code = 429
            self.body = {"error": {"message": "slow down", "type": "rate_limit_error"}}

    assert _is_transient(RateLimitError()) is True


def test_quota_detection_survives_a_missing_body():
    """SDK versions differ in whether `body` is populated; the message is the fallback."""
    from dbt_sentinel.agent import _is_transient

    class RateLimitError(Exception):
        def __init__(self):
            super().__init__("Error code: 429 - insufficient_quota")
            self.status_code = 429

    assert _is_transient(RateLimitError()) is False


def test_agent_degrades_immediately_on_exhausted_credits(tmp_path):
    """One attempt, not three: the reader gets the reason without waiting out backoff."""
    from dbt_sentinel.agent import ReviewerAgent
    from dbt_sentinel.diff import resolve_changes
    from dbt_sentinel.lineage import Lineage
    from dbt_sentinel.report import build_assessments
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    lineage = Lineage.from_path(repo / "evals" / "manifest" / "manifest.json")
    diff = (repo / "evals" / "fixtures" / "b01_column_rename_with_consumers.diff").read_text(
        encoding="utf-8"
    )
    changes, _ = resolve_changes(diff, lineage)
    assessments = build_assessments(changes, lineage)

    attempts = []

    class Client:
        def __init__(self):
            self.chat = self
            self.completions = self

        def create(self, **kwargs):
            attempts.append(1)
            raise _quota_error()

    result = ReviewerAgent(lineage, None, client=Client(), sleep=lambda _: None).review(
        assessments
    )
    assert result.degraded
    assert len(attempts) == 1, f"retried a permanent failure {len(attempts)} times"
    assert "credits" in result.degradation_reason or "quota" in result.degradation_reason
