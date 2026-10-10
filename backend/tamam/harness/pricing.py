"""Model prices, so every run records what it actually cost.

$ per 1M tokens. Cache writes and reads follow the provider's multipliers (Anthropic:
write 1.25x input for the 5-minute cache, read 0.1x). Sources, checked 2026-09-27:
https://platform.claude.com/docs/en/about-claude/pricing
An unknown model costs None rather than a guess.
"""

from __future__ import annotations

PRICES: dict[str, dict[str, float]] = {
    "anthropic:claude-sonnet-5": {"in": 2.00, "out": 10.00, "cache_write": 2.50, "cache_read": 0.20},
    "anthropic:claude-haiku-4-5": {"in": 1.00, "out": 5.00, "cache_write": 1.25, "cache_read": 0.10},
}


def cost_usd(model: str, *, input_tokens: int, output_tokens: int,
             cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> float | None:
    """`input_tokens` is the TOTAL input as pydantic-ai reports it (it includes the cached
    reads and writes); the uncached remainder is billed at the full input price."""
    p = PRICES.get(model)
    if p is None:
        return None
    fresh = max(input_tokens - cache_read_tokens - cache_write_tokens, 0)
    return round((fresh * p["in"] + cache_write_tokens * p["cache_write"]
                  + cache_read_tokens * p["cache_read"] + output_tokens * p["out"]) / 1e6, 5)
