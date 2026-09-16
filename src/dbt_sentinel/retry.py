"""Retry with backoff for transient upstream failures.

Scoped deliberately narrowly. Only these are retried:

- 429, with the server's own wait honoured when it sends one
- 5xx, which is upstream being briefly broken
- connection and timeout errors

A 4xx that is not 429 is never retried: a 404 from a missing App installation and a 422
from an over-long status description do not become correct by being sent again, and
retrying them turns one clear error into four slow ones.

The ceiling matters as much as the retries. GitHub redelivers a webhook that does not
answer, so a handler that retries for five minutes gets a second delivery on top of the
first and posts the review twice. Total wait is bounded well under any redelivery window.
"""

from __future__ import annotations

import email.utils
import random
import time
from typing import Callable, TypeVar

T = TypeVar("T")

DEFAULT_ATTEMPTS = 3
DEFAULT_BASE_DELAY_S = 1.0
# Any single wait longer than this is not worth holding a webhook handler open for.
MAX_DELAY_S = 20.0


class RetryableError(Exception):
    """A failure worth another attempt.

    `retry_after` carries the server's instruction when it gave one — guessing a backoff
    while the API is telling you exactly how long to wait is how a client earns a longer
    ban than it needed.
    """

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def parse_retry_after(value: str | None) -> float | None:
    """`Retry-After` is either delta-seconds or an HTTP date. Both appear in the wild."""
    if not value:
        return None
    raw = value.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    import datetime as _dt

    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    return max(0.0, (when - _dt.datetime.now(_dt.timezone.utc)).total_seconds())


def backoff_delay(attempt: int, retry_after: float | None = None, jitter: bool = True) -> float:
    """Server instruction wins; otherwise exponential with jitter.

    Jitter is not decoration: without it, several webhook deliveries that hit the same
    rate limit retry in lockstep and stay synchronised through every subsequent wave.
    """
    if retry_after is not None:
        return min(retry_after, MAX_DELAY_S)
    delay = min(DEFAULT_BASE_DELAY_S * (2 ** attempt), MAX_DELAY_S)
    if jitter:
        delay += random.uniform(0, delay * 0.25)
    return min(delay, MAX_DELAY_S)


def with_retries(
    operation: Callable[[], T],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    sleep: Callable[[float], None] | None = None,
    on_retry: Callable[[int, float, Exception], None] | None = None,
) -> T:
    """Call `operation`, retrying only `RetryableError`, and re-raise the last one.

    The caller decides what is retryable by raising `RetryableError`; this function makes
    no guesses about exception types it does not own.
    """
    # Resolved at call time, not bound as a default: `sleep=time.sleep` in the signature
    # captures the function at import, so monkeypatching `retry.time.sleep` in a test
    # silently has no effect and the test sits through real backoff while appearing to
    # have mocked the clock. Three separate tests hit that trap before this was found.
    sleep = sleep if sleep is not None else time.sleep

    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return operation()
        except RetryableError as exc:
            last = exc
            if attempt == attempts - 1:
                break
            delay = backoff_delay(attempt, exc.retry_after)
            if on_retry is not None:
                on_retry(attempt + 1, delay, exc)
            sleep(delay)
    assert last is not None
    raise last
