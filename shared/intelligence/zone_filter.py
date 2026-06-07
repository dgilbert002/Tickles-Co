"""
Module: zone_filter
Purpose: Phase 9 pre-ingest zone filter — cheap text classifier for trading signals.
"""

import asyncio
import json
import logging
import os
from typing import Optional

from shared.intelligence.gateway_config import chat_completion, resolve_slot_gateway
from shared.utils.api_cost_log import log_api_call
from shared.utils.db import get_shared_pool

logger = logging.getLogger(__name__)

_THRESHOLD = float(os.getenv("SIGNAL_ZONE_FILTER_THRESHOLD", "0.6"))


async def _load_zone_filter_prompt() -> str:
    """Load zone-filter prompt from prompt_versions (DB only)."""
    pool = await get_shared_pool()
    row = await pool.fetch_one(
        "SELECT body FROM prompt_versions "
        "WHERE name IN ('zone_filter', 'chart_zone_filter') "
        "ORDER BY created_at DESC LIMIT 1"
    )
    if row and row["body"]:
        return str(row["body"])
    row = await pool.fetch_one(
        "SELECT body FROM prompt_versions "
        "WHERE name = 'chart_prefilter' ORDER BY created_at DESC LIMIT 1"
    )
    if row and row["body"]:
        return str(row["body"])
    raise RuntimeError("no zone_filter prompt in prompt_versions DB")


async def classify(text: str, *, source_id: int, correlation_id: str) -> dict:
    """Classify a message as trading signal or noise."""
    # BIBLE-P1: correlation_id columns are VARCHAR(36). Clamp defensively so a long
    # id can never raise StringDataRightTruncationError and silently disable the gate.
    if correlation_id and len(correlation_id) > 36:
        correlation_id = correlation_id[:36]
    try:
        system_prompt = await _load_zone_filter_prompt()
        cfg, model = await resolve_slot_gateway("prefilter")
        response = await asyncio.wait_for(
            chat_completion(
                cfg=cfg,
                model=model,
                system_prompt=system_prompt,
                user_text=text[:2000],
                max_tokens=256,
                correlation_id=correlation_id,
                operation="zone_filter",
                agent_id="signal_zone_filter",
            ),
            timeout=10.0,
        )
        usage = response.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens") or 0)
        tokens_out = int(usage.get("completion_tokens") or 0)
        await log_api_call(
            provider=cfg.gateway,
            model=response.get("model", model),
            role="signal_zone_filter",
            context=f"zone_filter:{model}"[:100],
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=0,
            correlation_id=correlation_id,
            operation="zone_filter",
            agent_id="signal_zone_filter",
            success=True,
        )
        # BIBLE-P1: tolerant JSON parse — LLMs wrap JSON in ``` fences or append prose.
        # Extract the first {...} block; fall back to empty dict (which fails open).
        raw = (response.get("content") or "{}").strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:]
        import re as _re
        m = _re.search(r"\{.*\}", raw, _re.S)
        try:
            parsed = json.loads(m.group(0) if m else raw)
        except Exception:
            parsed = {}
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
            "reason": "zone_filter_unavailable: TimeoutError",
        }
    except Exception as exc:
        logger.warning("Zone filter failed for source_id=%s: %s", source_id, exc)
        return {
            "is_trading_signal": True,
            "confidence": 0.0,
            "reason": f"zone_filter_unavailable: {type(exc).__name__}",
        }


def passes(zone_result: dict, *, per_source_threshold: Optional[float] = None) -> bool:
    threshold = per_source_threshold if per_source_threshold is not None else _THRESHOLD
    if zone_result["confidence"] == 0.0 and "unavailable" in zone_result["reason"]:
        return True
    return zone_result["is_trading_signal"] and zone_result["confidence"] >= threshold
