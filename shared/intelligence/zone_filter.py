"""
Module: zone_filter
Purpose: Phase 9 pre-ingest zone filter — cheap text classifier for trading signals.
Location: /opt/tickles/shared/intelligence/zone_filter.py
"""

import asyncio
import json
import logging
import os
from typing import Optional

from shared.intelligence.gateway_config import chat_completion
from shared.utils.api_cost_log import log_api_call

logger = logging.getLogger(__name__)

_THRESHOLD = float(os.getenv("SIGNAL_ZONE_FILTER_THRESHOLD", "0.6"))

ZONE_FILTER_PROMPT = """You are a trading-signal triage filter. Read the message and decide:
is the author trying to communicate a TRADABLE signal (an entry, exit, level, bias)?
Reply with strict JSON: {"is_trading_signal": bool, "confidence": float in [0,1], "reason": str}.
NOT signals: memes, jokes, post-trade celebration, news links without a position, generic chart commentary.
Signals: explicit longs/shorts, level calls, "watching X", "added more here", trade plans."""


async def classify(text: str, *, source_id: int, correlation_id: str) -> dict:
    """Classify a message as trading signal or noise.

    Args:
        text: The message content to classify.
        source_id: Collector catalog source ID for cost attribution.
        correlation_id: Trace ID for cost logging.

    Returns:
        Dict with keys: is_trading_signal (bool), confidence (float), reason (str).
        On failure, returns fail-open dict with is_trading_signal=True.
    """
    try:
        result = await asyncio.wait_for(
            chat_completion(
                role="signal_zone_filter",
                system=ZONE_FILTER_PROMPT,
                user=text,
                response_format="json",
            ),
            timeout=10.0,
        )
        await log_api_call(
            role="signal_zone_filter",
            correlation_id=correlation_id,
            source_id=source_id,
            provider=result.provider,
            model=result.model_resolved,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            cost_usd=result.cost_usd,
            latency_ms=result.latency_ms,
            operation="zone_filter",
            success=True,
        )
        parsed = json.loads(result.content)
        return {
            "is_trading_signal": bool(parsed.get("is_trading_signal", False)),
            "confidence": float(parsed.get("confidence", 0.0)),
            "reason": str(parsed.get("reason", ""))[:500],
        }
    except asyncio.TimeoutError as exc:
        logger.warning("Zone filter timeout for source_id=%s: %s", source_id, exc)
        return {
            "is_trading_signal": True,
            "confidence": 0.0,
            "reason": f"zone_filter_unavailable: TimeoutError",
        }
    except Exception as exc:
        logger.warning("Zone filter failed for source_id=%s: %s", source_id, exc)
        # Fail-open: let the heavier pipeline decide
        return {
            "is_trading_signal": True,
            "confidence": 0.0,
            "reason": f"zone_filter_unavailable: {type(exc).__name__}",
        }


def passes(zone_result: dict, *, per_source_threshold: Optional[float] = None) -> bool:
    """Determine if a zone-filter result passes the threshold.

    Args:
        zone_result: Output from classify().
        per_source_threshold: Optional per-source override; falls back to env default.

    Returns:
        True if the message should proceed to full interpretation.
    """
    threshold = per_source_threshold if per_source_threshold is not None else _THRESHOLD
    if zone_result["confidence"] == 0.0 and "unavailable" in zone_result["reason"]:
        return True  # fail-open
    return zone_result["is_trading_signal"] and zone_result["confidence"] >= threshold
