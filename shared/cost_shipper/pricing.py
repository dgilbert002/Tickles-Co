"""OpenRouter fallback pricing table.

OpenClaw already puts a ``cost`` block in every assistant usage record, so this
table is only used when that block is absent (defensive). Prices are in USD per
1,000 tokens and reflect OpenRouter's public pricing at the time of writing;
keep them slightly conservative so we never under-report spend.

Source of truth for live pricing is OpenRouter's `/api/v1/models` endpoint; the
reconciler job compares our aggregated spend against the canonical auth-key
totals and alerts on drift >5 percent.
"""

from __future__ import annotations

# price per 1K tokens in USD.  [input, cached_input, output]
_PRICE_TABLE: dict[str, tuple[float, float, float]] = {
    # defaults used by the current openclaw.json fleet
    "openrouter/openai/gpt-4.1": (0.002, 0.0005, 0.008),
    "openai/gpt-4.1": (0.002, 0.0005, 0.008),
    "openrouter/openai/gpt-4.1-mini": (0.00015, 0.000038, 0.0006),
    "openai/gpt-4.1-mini": (0.00015, 0.000038, 0.0006),
    "openrouter/google/gemini-2.5-pro": (0.00125, 0.000625, 0.01),
    "google/gemini-2.5-pro": (0.00125, 0.000625, 0.01),
    "openrouter/anthropic/claude-sonnet-4": (0.003, 0.0003, 0.015),
    "anthropic/claude-sonnet-4": (0.003, 0.0003, 0.015),
    "openrouter/anthropic/claude-sonnet-4.6": (0.003, 0.0003, 0.015),
    "anthropic/claude-sonnet-4.6": (0.003, 0.0003, 0.015),
    "openrouter/anthropic/claude-sonnet-4-6": (0.003, 0.0003, 0.015),
    "openrouter/anthropic/claude-opus-4.6": (0.015, 0.0015, 0.075),
    "anthropic/claude-opus-4.6": (0.015, 0.0015, 0.075),
    # seen in live session logs
    "xiaomi/mimo-v2-pro": (0.001, 0.0002, 0.003),
}


def estimate_cost_cents(model: str, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> float:
    """Return estimated cost in cents (float) for a single LLM call.

    When the model is unknown we fall back to the fleet default to avoid
    silently logging $0 rows.
    """
    key = model.lower()
    price = _PRICE_TABLE.get(key)
    if price is None:
        # try without "openrouter/" prefix
        if "/" in key and key.startswith("openrouter/"):
            price = _PRICE_TABLE.get(key.split("openrouter/", 1)[1])
        if price is None:
            price = _PRICE_TABLE["openrouter/openai/gpt-4.1"]  # conservative default
    input_price, cached_price, output_price = price
    usd = (
        (input_tokens / 1000.0) * input_price
        + (cached_input_tokens / 1000.0) * cached_price
        + (output_tokens / 1000.0) * output_price
    )
    return usd * 100.0
