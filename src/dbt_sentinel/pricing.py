"""Token pricing for the pinned model (gpt-4o).

Lives in the package rather than the eval harness because both a PR comment and the eval
report quote costs, and they must not be able to disagree. The earlier arrangement had
`pipeline.py` inject the repo root onto `sys.path` to import `evals.compare` — a library
reaching into its own test harness, which works in a checkout and breaks the moment the
package is installed somewhere without one.

Verify against https://openai.com/api/pricing before quoting these anywhere.
"""

from __future__ import annotations

# USD per million tokens for gpt-4o.
#
# History worth keeping: this was first written as 3.00/15.00 (Sonnet 4.6's rates, carried
# over by assumption) while the pin was claude-sonnet-5 at 2.00/10.00 — every published
# cost figure would have been ~50% too high. The agent was later ported to OpenAI, moving
# the pin to gpt-4o at 2.50/10.00. A published cost number is only as good as its price
# constant, which is why a test pins these to the model in DEFAULT_MODEL.
#
# Verify against https://openai.com/api/pricing before quoting these anywhere.
PRICE_PER_MTOK_INPUT = 2.50
PRICE_PER_MTOK_OUTPUT = 10.00


def cost_usd(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens / 1_000_000 * PRICE_PER_MTOK_INPUT
        + output_tokens / 1_000_000 * PRICE_PER_MTOK_OUTPUT
    )
