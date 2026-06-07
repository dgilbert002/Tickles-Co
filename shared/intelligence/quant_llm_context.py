"""
Module: quant_llm_context
Purpose: Format live quant snapshots for LLM prompts (chart_hacker-only injection).
Location: /opt/tickles/shared/intelligence/quant_llm_context.py
"""

from __future__ import annotations

import json
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from shared.intelligence.interpretation_service import QuantResult


def is_informative_quant(quant: "QuantResult") -> bool:
    """True when quant has RSI/EMA/score — not just a degraded price stub."""
    indicators = getattr(quant, "indicators", None) or {}
    if indicators.get("rsi14") is not None:
        return True
    if indicators.get("ema20") is not None and indicators.get("ema50") is not None:
        return True
    if indicators.get("score") is not None:
        return True
    if indicators.get("reasons"):
        return True
    return False


def format_chart_hacker_quant_block(quant: "QuantResult") -> str:
    """Build a prompt appendix for chart_hacker fields only.

    The vision Lens prompt fills both ``trader_trades`` and
    ``chart_hacker_trades`` in one call. This block tells the model to
    apply quant to the chart_hacker side only.
    """
    indicators = getattr(quant, "indicators", None) or {}
    degraded = bool(indicators.get("degraded"))
    status = "full" if is_informative_quant(quant) else ("degraded_price_only" if degraded else "unavailable")
    payload = {
        "status": status,
        "direction": getattr(quant, "direction", "unclear"),
        "confidence": round(float(getattr(quant, "confidence", 0.0) or 0.0), 4),
        "rsi14": indicators.get("rsi14"),
        "ema20": indicators.get("ema20"),
        "ema50": indicators.get("ema50"),
        "atr14": indicators.get("atr14"),
        "bollinger": indicators.get("bollinger"),
        "last_close": indicators.get("last_close") or indicators.get("current_price"),
        "score": indicators.get("score"),
        "reasons": indicators.get("reasons"),
        "price_source": indicators.get("price_source"),
    }
    if degraded:
        payload["note"] = (
            "Candle indicators unavailable for this symbol; only live price present. "
            "Do not treat as a full quant snapshot — rely on chart vision for chart_hacker."
        )
    return (
        "--- QUANT SNAPSHOT (chart_hacker ONLY — apply to chart_hacker_trades and "
        "chart_hacker_market_view; do NOT use for trader_trades, trader_market_view, "
        "or trader sentiment) ---\n"
        + json.dumps(payload, separators=(",", ":"))
    )
