"""Token pricing for the pinned model.

Lives in the package rather than the eval harness because both a PR comment and the eval
report quote costs, and they must not be able to disagree. The earlier arrangement had
`pipeline.py` inject the repo root onto `sys.path` to import `evals.compare` — a library
reaching into its own test harness, which works in a checkout and breaks the moment the
package is installed somewhere without one.

Verify against https://claude.com/pricing before quoting these anywhere.
"""

from __future__ import annotations

# USD per million tokens for claude-sonnet-5.
#
# Originally written as 3.00/15.00 — Sonnet 4.6's rates, carried over by assumption.
# Sonnet 5 is 2.00/10.00, so every published cost figure would have been ~50% too high.
# A published cost number is only as good as its price constant, hence the test.
PRICE_PER_MTOK_INPUT = 2.00
PRICE_PER_MTOK_OUTPUT = 10.00


def cost_usd(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens / 1_000_000 * PRICE_PER_MTOK_INPUT
        + output_tokens / 1_000_000 * PRICE_PER_MTOK_OUTPUT
    )
