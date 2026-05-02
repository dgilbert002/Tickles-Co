"""
Module: interpretation_service
Purpose: Daemon that polls media_items for downloaded charts/images,
         runs dual-track interpretation (LLM vision + quant indicators),
         and writes signal_interpretations to company databases.
Location: /opt/tickles/shared/intelligence/interpretation_service.py

Design:
  * Polls tickles_shared.media_items WHERE processing_status='downloaded'.
  * For each item, resolves the associated news_item to determine company
    context and instrument symbols.
  * LLM Track: encodes the image + news context, sends to vision LLM
    (claude-sonnet-4 primary, gemini-2.0-flash fallback).
  * Quant Track: reads recent candles + indicators for the instrument
    from Postgres, runs a lightweight directional scoring.
  * Consensus Engine: merges LLM + quant into a single direction/confidence.
  * Writes to tickles_<company>.signal_interpretations.
  * Updates media_items.processing_status -> 'analyzed' or 'skipped_vision_unavailable'.
  * Broadcasts high-confidence insights to MemU for cross-company learning.
  * Respects interpretation_max_age_hours from system_config (default 24h).
  * Idempotent: composite UNIQUE on (news_item_id, model_version, param_hash)
    prevents duplicate interpretations.

Hardening:
  * Exponential backoff on LLM failures (max 3 attempts).
  * If vision LLM is permanently down, mark skipped_vision_unavailable and retry next cycle.
  * Freshness Guard on all market data reads.
  * Budget tracking per interpretation (llm_cost_usd, quant_cost_usd).
  * SIGTERM/SIGINT graceful shutdown with in-flight completion.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import asyncpg

# Ensure shared imports resolve
_HERE = Path(__file__).resolve().parent
_SHARED = _HERE.parent
_ROOT = _SHARED.parent
for p in (_ROOT, _SHARED):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from shared.utils.config import load_env
from shared.utils.correlation import new_correlation_id
from shared.utils.db import DatabasePool, get_shared_pool
from shared.memu.broadcast_payload import BroadcastPayload
from shared.utils.freshness import StaleDataError, validate_freshness
from shared.utils.instrument_normaliser import normalise_instrument

# New intelligence pipeline modules
from shared.intelligence.llm_rate_limiter import LlmRateLimiter, get_default_limiter
from shared.intelligence.reason_similarity import compute_reason_agreement
from shared.intelligence.embed import embed
from shared.intelligence.trade_dedup import find_duplicate_position, is_continuation_text
from shared.intelligence.text_signal_extractor import extract_signal_from_text, classify_message_type
from shared.intelligence.payload_store import (
    build_request_payload,
    build_response_payload,
    compute_prompt_hash,
    compute_prompt_version,
    save_payload_pair,
)

logger = logging.getLogger("tickles.intelligence.interpretation")

# ---------------------------------------------------------------------------
# Config (env-driven, no hardcodes)
# ---------------------------------------------------------------------------
POLL_INTERVAL_S = float(os.environ.get("INTERPRETATION_POLL_S", "300"))  # 5 min default
BATCH_SIZE = int(os.environ.get("INTERPRETATION_BATCH", "10"))
MAX_AGE_HOURS = float(os.environ.get("INTERPRETATION_MAX_AGE_H", "24"))
LLM_MAX_RETRIES = int(os.environ.get("INTERPRETATION_LLM_RETRIES", "3"))
LLM_BACKOFF_BASE = float(os.environ.get("INTERPRETATION_LLM_BACKOFF", "2.0"))
PRIMARY_MODEL = os.environ.get("CHART_HACKER_MODEL_PRIMARY", "anthropic/claude-sonnet-4")
FALLBACK_MODEL = os.environ.get("CHART_HACKER_MODEL_FALLBACK", "google/gemini-2.0-flash-001")
PREFILTER_MODEL = os.environ.get("CHART_HACKER_PREFILTER_MODEL", "google/gemini-2.5-flash")
PREFILTER_ENABLED = os.environ.get("CHART_HACKER_PREFILTER_ENABLED", "true").lower() in ("1", "true", "yes")
FRESHNESS_THRESHOLD_S = float(os.environ.get("INTERPRETATION_FRESHNESS_S", "180"))


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass
class InterpretationConfig:
    """Runtime configuration for the interpretation service."""

    poll_interval_s: float = POLL_INTERVAL_S
    batch_size: int = BATCH_SIZE
    max_age_hours: float = MAX_AGE_HOURS
    llm_max_retries: int = LLM_MAX_RETRIES
    llm_backoff_base: float = LLM_BACKOFF_BASE
    primary_model: str = PRIMARY_MODEL
    fallback_model: str = FALLBACK_MODEL
    freshness_threshold_s: float = FRESHNESS_THRESHOLD_S


@dataclass
class LlmResult:
    """Result from the LLM vision track."""

    direction: str = "unclear"
    confidence: float = 0.0
    reasoning: str = ""
    levels: Dict[str, Any] = field(default_factory=dict)
    model_used: str = ""
    model_resolved: str = ""
    cost_usd: float = 0.0
    raw_response: str = ""
    request_path: str = ""
    response_path: str = ""
    prompt_version: str = ""
    prompt_hash: str = ""


@dataclass
class QuantResult:
    """Result from the quant indicator track."""

    direction: str = "unclear"
    confidence: float = 0.0
    indicators: Dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0


@dataclass
class ConsensusResult:
    """Merged result from LLM + quant consensus engine."""

    direction: str = "unclear"
    confidence: float = 0.0
    method: str = ""
    llm_result: Optional[LlmResult] = None
    quant_result: Optional[QuantResult] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _param_hash(
    model: str,
    primary_model: str,
    fallback_model: str,
    freshness_threshold: float,
    max_age_hours: float,
) -> str:
    """Deterministic hash of the interpretation parameters for reproducibility."""
    payload = json.dumps(
        {
            "model": model,
            "primary_model": primary_model,
            "fallback_model": fallback_model,
            "freshness_threshold": freshness_threshold,
            "max_age_hours": max_age_hours,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


MAX_IMAGE_BYTES = int(os.environ.get("TICKLES_MAX_IMAGE_BYTES", "10000000"))


def _encode_image_b64(path: str) -> str:
    """Read an image file and return base64-encoded string.

    Args:
        path: Path to the image file.

    Returns:
        Base64-encoded image string.

    Raises:
        ValueError: If file exceeds MAX_IMAGE_BYTES or is not a valid image.
        FileNotFoundError: If path does not exist.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Image not found: {path}")
    size = os.path.getsize(path)
    if size > MAX_IMAGE_BYTES:
        raise ValueError(
            f"Image too large: {size} bytes (max {MAX_IMAGE_BYTES}). "
            f"Resize or compress before vision analysis."
        )
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _image_mime_type(path: str) -> str:
    """Guess MIME type from file extension."""
    ext = Path(path).suffix.lower()
    mapping = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }
    return mapping.get(ext, "image/png")


# ---------------------------------------------------------------------------
# LLM Vision Track
# ---------------------------------------------------------------------------
def _load_prompts() -> Dict[str, Any]:
    """Load chart analysis prompts from external JSON config.

    Falls back to embedded defaults if the config file is missing or invalid.
    """
    prompt_path = Path(__file__).with_suffix("").parent / "prompts" / "chart_analysis.json"
    if prompt_path.exists():
        try:
            with open(prompt_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load prompts from %s: %s", prompt_path, exc)
    return {
        "chart_analysis": {
            "system_prompt": (
                "You are ChartHacker, a crypto chart analyst. Analyze the provided chart image. "
                "Respond ONLY with a JSON object containing:\n"
                "  direction: 'long' | 'short' | 'neutral' | 'unclear'\n"
                "  confidence: 0.0-1.0\n"
                "  reasoning: brief text (max 200 chars)\n"
                "  levels: { entry, stop_loss, take_profit } as strings or null\n"
                "No markdown, no prose outside the JSON."
            ),
            "user_prompt_template": "Analyze this chart for {symbol}.\nNews context: {context}",
        }
    }




def _parse_llm_json(content: str) -> Dict[str, Any]:
    """Extract JSON decision block from LLM response text.

    Tries ```json fences first, then raw JSON object.
    """
    import re

    # Try fenced JSON
    m = re.search(r"```json\s*(.*?)\s*```", content, re.DOTALL)
    if m:
        raw = m.group(1)
    else:
        # Try raw JSON object
        m = re.search(r"(\{.*\})", content, re.DOTALL)
        raw = m.group(1) if m else content
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("Failed to parse LLM JSON: %s", e)
        return {}


async def run_prefilter(
    cfg: InterpretationConfig,
    image_path: str,
    instrument_symbol: str,
    correlation_id: str = "",
) -> Optional[LlmResult]:
    """Lightweight vision pre-filter using Gemini 2.5 Flash.

    Quickly classifies whether the image is worth sending to the expensive
    primary vision model. Returns an LlmResult with direction='unclear' if
    the image is not a trade_setup (commentary, meme, unclear), or None if
    the pre-filter passes and the primary model should run.

    Args:
        cfg: InterpretationConfig.
        image_path: Path to the chart image.
        instrument_symbol: Trading pair symbol.
        correlation_id: Tracing ID from the top-level operation chain.

    Returns:
        LlmResult with direction='unclear' if filtered out, or None to proceed.
    """
    if not PREFILTER_ENABLED:
        return None

    image_b64 = _encode_image_b64(image_path)
    image_mime = _image_mime_type(image_path)

    prefilter_prompt = (
        "You are a fast image classifier. Look at this chart image and decide: "
        "is it a trade setup (clear entry/stop-loss/take-profit levels), commentary "
        "(market discussion without levels), meme (joke/image macro), or unclear?\n\n"
        "Respond ONLY with JSON:\n"
        "  {\n"
        "    \"is_trade_setup\": true | false,\n"
        "    \"message_type\": \"trade_setup\" | \"commentary\" | \"meme\" | \"unclear\",\n"
        "    \"reasoning\": \"brief one-line reason (max 100 chars)\"\n"
        "  }\n"
        "No markdown, no prose outside the JSON."
    )
    user_text = f"Chart for {instrument_symbol}."

    from shared.intelligence.gateway_config import GatewayConfig, call_vision_llm

    gateway_cfg = GatewayConfig.for_service("interpretation")
    model = PREFILTER_MODEL

    # Phase 3: compute prompt metadata for audit trail
    prompt_version = compute_prompt_version(_load_prompts())
    prompt_hash = compute_prompt_hash(prefilter_prompt, user_text)

    try:
        resp = await call_vision_llm(
            cfg=gateway_cfg,
            model=model,
            system_prompt=prefilter_prompt,
            user_text=user_text,
            image_b64=image_b64,
            image_mime=image_mime,
            correlation_id=correlation_id,
            operation="vision_prefilter",
        )

        # Phase 3: persist raw request/response payloads
        request_payload = build_request_payload(
            provider=gateway_cfg.gateway,
            model_requested=model,
            system_prompt=prefilter_prompt,
            user_text=user_text,
            image_b64=image_b64,
            temperature=gateway_cfg.temperature,
            extra={"operation": "vision_prefilter", "correlation_id": correlation_id},
        )
        response_payload = build_response_payload(
            model_resolved=resp.get("model", model),
            content=resp.get("content", ""),
            usage=resp.get("usage"),
            extra={"operation": "vision_prefilter", "correlation_id": correlation_id},
        )
        req_path, resp_path = "", ""
        try:
            req_path, resp_path = await save_payload_pair(
                correlation_id=correlation_id,
                request_payload=request_payload,
                response_payload=response_payload,
            )
        except Exception as exc:
            logger.warning("Failed to save prefilter payload for cid=%s: %s", correlation_id, exc)

        parsed = _parse_llm_json(resp["content"])
        is_trade_setup = bool(parsed.get("is_trade_setup", False))
        msg_type = str(parsed.get("message_type", "unclear")).lower()

        if not is_trade_setup or msg_type in ("commentary", "meme", "unclear"):
            logger.info(
                "Pre-filter rejected image for %s: type=%s reason=%s",
                instrument_symbol,
                msg_type,
                parsed.get("reasoning", ""),
            )
            return LlmResult(
                direction="unclear",
                confidence=0.0,
                reasoning=f"pre-filter: {msg_type} — {parsed.get('reasoning', '')}",
                levels={},
                model_used=model,
                model_resolved=resp.get("model", model),
                cost_usd=0.0,
                raw_response=resp["content"],
                request_path=req_path,
                response_path=resp_path,
                prompt_version=prompt_version,
                prompt_hash=prompt_hash,
            )
    except Exception as exc:
        logger.warning("Pre-filter failed for %s: %s — proceeding to primary model", instrument_symbol, exc)
        return None

    return None


async def run_llm_track(
    cfg: InterpretationConfig,
    image_path: str,
    news_context: str,
    instrument_symbol: str,
    correlation_id: str = "",
) -> LlmResult:
    """Run the LLM vision track: encode image, call vision model, parse response.

    Falls back to fallback_model on failure. Retries with exponential backoff.
    If all attempts fail, raises RuntimeError (caller marks skipped_vision_unavailable).

    Phase 3 additions:
      * Saves request/response payloads to disk for audit.
      * Populates prompt_version and prompt_hash on returned LlmResult.
      * Captures model_resolved from gateway response.

    Args:
        cfg: InterpretationConfig.
        image_path: Path to the chart image.
        news_context: Headline + content text for context.
        instrument_symbol: Trading pair symbol.
        correlation_id: Tracing ID from the top-level operation chain.

    Returns:
        LlmResult with parsed direction, confidence, levels, and cost.
    """
    # --- Pre-filter: cheap vision model to avoid wasting expensive calls ---
    prefilter_result = await run_prefilter(
        cfg, image_path, instrument_symbol, correlation_id=correlation_id
    )
    if prefilter_result is not None:
        return prefilter_result

    image_b64 = _encode_image_b64(image_path)
    image_mime = _image_mime_type(image_path)

    prompts = _load_prompts().get("chart_analysis", {})
    system_prompt = prompts.get("system_prompt", "")
    if not system_prompt:
        system_prompt = (
            "You are ChartHacker, a crypto chart analyst. Analyze the provided chart image. "
            "Respond ONLY with a JSON object containing:\n"
            "  direction: 'long' | 'short' | 'neutral' | 'unclear'\n"
            "  confidence: 0.0-1.0\n"
            "  reasoning: brief text (max 200 chars)\n"
            "  levels: { entry, stop_loss, take_profit } as strings or null\n"
            "No markdown, no prose outside the JSON."
        )
        logger.warning("chart_analysis.json system_prompt missing — using hardcoded fallback")
    user_template = prompts.get("user_prompt_template", "Analyze this chart for {symbol}.\nNews context: {context}")
    user_text = user_template.format(symbol=instrument_symbol, context=news_context[:500])

    # Phase 3: compute prompt metadata for audit trail
    prompt_version = compute_prompt_version(_load_prompts())
    prompt_hash = compute_prompt_hash(system_prompt, user_text)

    models = [cfg.primary_model, cfg.fallback_model]
    last_exc: Optional[Exception] = None

    # Lazy-import gateway to avoid circular deps at module load time
    from shared.intelligence.gateway_config import GatewayConfig, call_vision_llm

    gateway_cfg = GatewayConfig.for_service("interpretation")

    for model in models:
        for attempt in range(cfg.llm_max_retries):
            try:
                resp = await call_vision_llm(
                    cfg=gateway_cfg,
                    model=model,
                    system_prompt=system_prompt,
                    user_text=user_text,
                    image_b64=image_b64,
                    image_mime=image_mime,
                    correlation_id=correlation_id,
                    operation="vision_primary" if model == cfg.primary_model else "vision_fallback",
                )
                parsed = _parse_llm_json(resp["content"])
                # Cost is now logged automatically by the gateway via api_cost_log.
                # We keep a local zero placeholder; the true cost is in the DB.
                cost = 0.0

                direction = str(parsed.get("direction", "unclear")).lower()
                if direction not in ("long", "short", "neutral", "unclear"):
                    direction = "unclear"

                # Phase 3: persist raw request/response payloads
                request_payload = build_request_payload(
                    provider=gateway_cfg.gateway,
                    model_requested=model,
                    system_prompt=system_prompt,
                    user_text=user_text,
                    image_b64=image_b64,
                    temperature=gateway_cfg.temperature,
                    extra={"operation": "vision_primary" if model == cfg.primary_model else "vision_fallback", "correlation_id": correlation_id},
                )
                response_payload = build_response_payload(
                    model_resolved=resp.get("model", model),
                    content=resp.get("content", ""),
                    usage=resp.get("usage"),
                    extra={"operation": "vision_primary" if model == cfg.primary_model else "vision_fallback", "correlation_id": correlation_id},
                )
                req_path, resp_path = "", ""
                try:
                    req_path, resp_path = await save_payload_pair(
                        correlation_id=correlation_id,
                        request_payload=request_payload,
                        response_payload=response_payload,
                    )
                except Exception as exc:
                    logger.warning("Failed to save primary payload for cid=%s: %s", correlation_id, exc)

                return LlmResult(
                    direction=direction,
                    confidence=float(parsed.get("confidence", 0.0)),
                    reasoning=str(parsed.get("reasoning", "")),
                    levels=parsed.get("levels", {}),
                    model_used=model,
                    model_resolved=resp.get("model", model),
                    cost_usd=cost,
                    raw_response=resp["content"],
                    request_path=req_path,
                    response_path=resp_path,
                    prompt_version=prompt_version,
                    prompt_hash=prompt_hash,
                )
            except Exception as exc:
                last_exc = exc
                wait = cfg.llm_backoff_base * (2 ** attempt)
                logger.warning(
                    "LLM track attempt %d/%d for model %s failed: %s. Retrying in %.1fs",
                    attempt + 1,
                    cfg.llm_max_retries,
                    model,
                    exc,
                    wait,
                )
                await asyncio.sleep(wait)

    raise RuntimeError(
        f"LLM track failed after {cfg.llm_max_retries} retries on both models: {last_exc}"
    )


# ---------------------------------------------------------------------------
# Quant Track
# ---------------------------------------------------------------------------
async def run_quant_track(
    shared_pool: DatabasePool,
    company_pool: DatabasePool,
    instrument_symbol: str,
    exchange: str,
    freshness_threshold: float,
) -> QuantResult:
    if not instrument_symbol:
        logger.warning("Quant track: instrument_symbol is None or empty")
        return QuantResult(direction="unclear", confidence=0.0, indicators={})
    """Run the quant indicator track: read recent candles, compute lightweight signals.

    Reads the last 100 1m candles for the instrument, computes RSI(14), EMA(20/50),
    ATR(14), and Bollinger(20,2). Derives a directional score from the ensemble.

    Args:
        shared_pool: DatabasePool for the shared database (instruments, candles).
        company_pool: DatabasePool for the company database (not used for reads here).
        instrument_symbol: Trading pair symbol (e.g., 'BTCUSDT').
        exchange: Exchange name (e.g., 'bybit').
        freshness_threshold: Max age in seconds for candle data.

    Returns:
        QuantResult with direction, confidence, and indicator snapshot.
    """
    # Resolve instrument_id from symbol + exchange (shared DB)
    row = await shared_pool.fetch_one(
        "SELECT id FROM public.instruments WHERE symbol = $1 AND exchange = $2",
        (instrument_symbol, exchange),
    )
    if not row:
        logger.warning("Quant track: no instrument found for %s@%s", instrument_symbol, exchange)
        return QuantResult(direction="unclear", confidence=0.0, indicators={})

    instrument_id = row["id"]

    # Fetch last 100 1m candles (shared DB)
    candles = await shared_pool.fetch_all(
        "SELECT timestamp, open, high, low, close, volume "
        "FROM public.candles "
        "WHERE instrument_id = $1 AND source = $2 AND timeframe = '1m' "
        "ORDER BY timestamp DESC LIMIT 100",
        (instrument_id, exchange),
    )
    if len(candles) < 50:
        logger.warning(
            "Quant track: only %d candles for %s@%s, need >=50",
            len(candles),
            instrument_symbol,
            exchange,
        )
        return QuantResult(direction="unclear", confidence=0.0, indicators={})

    # Freshness guard on the most recent candle
    latest_ts = candles[0]["timestamp"]
    try:
        validate_freshness(
            latest_ts,
            threshold_seconds=freshness_threshold,
            context=f"quant_candles:{instrument_symbol}@{exchange}",
        )
    except StaleDataError as e:
        logger.warning("Quant track stale data: %s", e)
        return QuantResult(direction="unclear", confidence=0.0, indicators={"error": str(e)})

    # Reverse to chronological order for indicator calc
    candles.reverse()
    closes = [float(c["close"]) for c in candles]
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]

    # RSI(14)
    rsi_val = _rsi(closes, period=14)
    # EMA(20) vs EMA(50)
    ema20 = _ema(closes, period=20)
    ema50 = _ema(closes, period=50)
    # ATR(14)
    atr_val = _atr(highs, lows, closes, period=14)
    # Bollinger(20,2)
    bb = _bollinger(closes, period=20, k=2.0)

    # Directional scoring (simple ensemble)
    score = 0.0  # positive = bullish, negative = bearish
    reasons: List[str] = []

    # EMA crossover
    if ema20 > ema50:
        score += 0.25
        reasons.append("ema20>ema50")
    elif ema20 < ema50:
        score -= 0.25
        reasons.append("ema20<ema50")

    # RSI extremes
    if rsi_val > 70:
        score -= 0.20
        reasons.append("rsi>70")
    elif rsi_val < 30:
        score += 0.20
        reasons.append("rsi<30")
    elif 40 < rsi_val < 60:
        score *= 0.5  # Neutral zone dampens signal
        reasons.append("rsi_neutral")

    # Price vs Bollinger bands
    last_close = closes[-1]
    if last_close > bb["upper"]:
        score -= 0.15
        reasons.append("price>bb_upper")
    elif last_close < bb["lower"]:
        score += 0.15
        reasons.append("price<bb_lower")

    # Normalize score to confidence
    confidence = min(abs(score) * 2.0, 1.0)
    if score > 0.15:
        direction = "long"
    elif score < -0.15:
        direction = "short"
    else:
        direction = "neutral"

    indicators = {
        "rsi14": round(rsi_val, 2),
        "ema20": round(ema20, 4),
        "ema50": round(ema50, 4),
        "atr14": round(atr_val, 4),
        "bollinger": bb,
        "last_close": round(last_close, 4),
        "score": round(score, 4),
        "reasons": reasons,
    }

    return QuantResult(direction=direction, confidence=confidence, indicators=indicators)


def _rsi(closes: List[float], period: int = 14) -> float:
    """Compute RSI for a list of closes."""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[-i] - closes[-i - 1]
        if diff >= 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-diff)
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _ema(values: List[float], period: int) -> float:
    """Compute EMA for a list of values."""
    if not values:
        return 0.0
    k = 2.0 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1.0 - k)
    return e


def _atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
    """Compute ATR for lists of highs, lows, closes."""
    if len(closes) < period + 1:
        return 0.0
    trs: List[float] = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    return sum(trs[-period:]) / period


def _bollinger(closes: List[float], period: int = 20, k: float = 2.0) -> Dict[str, float]:
    """Compute Bollinger Bands."""
    if len(closes) < period:
        m = sum(closes) / max(len(closes), 1)
        return {"mid": round(m, 4), "upper": round(m, 4), "lower": round(m, 4)}
    s = closes[-period:]
    m = sum(s) / period
    var = sum((x - m) ** 2 for x in s) / period
    sd = var ** 0.5
    return {
        "mid": round(m, 4),
        "upper": round(m + k * sd, 4),
        "lower": round(m - k * sd, 4),
    }


# ---------------------------------------------------------------------------
# Consensus Engine
# ---------------------------------------------------------------------------
def run_consensus(llm: LlmResult, quant: QuantResult) -> ConsensusResult:
    """Merge LLM and quant tracks into a single interpretation.

    Rules:
      * If both agree (same direction), confidence is weighted average boosted by 0.1.
      * If they disagree, confidence is reduced and direction goes to 'unclear'.
      * If either is 'unclear' or 'neutral', defer to the clearer one.
      * Method records which track dominated.

    The final direction is always one of: long, short, unclear.
    This matches the DB check constraint on signal_interpretations.
    """
    d_llm = llm.direction
    d_quant = quant.direction
    c_llm = llm.confidence
    c_quant = quant.confidence

    # Normalize unclear/neutral to a common bucket for comparison
    def _norm(d: str) -> str:
        return "neutral" if d in ("unclear", "neutral") else d

    n_llm = _norm(d_llm)
    n_quant = _norm(d_quant)

    if n_llm == n_quant:
        # Agreement
        conf = min((c_llm + c_quant) / 2.0 + 0.1, 1.0)
        method = "agreement"
        direction = n_llm
    elif n_llm == "neutral":
        direction = n_quant
        conf = c_quant * 0.9
        method = "quant_dominant"
    elif n_quant == "neutral":
        direction = n_llm
        conf = c_llm * 0.9
        method = "llm_dominant"
    else:
        # Disagreement — map to 'unclear' for DB constraint compliance
        direction = "unclear"
        conf = max(abs(c_llm - c_quant) * 0.5, 0.1)
        method = "conflict"

    # Final safety: DB only accepts long, short, unclear
    if direction in ("neutral", "conflict"):
        direction = "unclear"

    return ConsensusResult(
        direction=direction,
        confidence=round(conf, 4),
        method=method,
        llm_result=llm,
        quant_result=quant,
    )


# ---------------------------------------------------------------------------
# Database Operations
# ---------------------------------------------------------------------------
async def fetch_pending_media(
    shared_pool: DatabasePool,
    batch_size: int,
    max_age_hours: float,
) -> List[Dict[str, Any]]:
    """Fetch media_items ready for interpretation and associated news context.

    Picks up:
      * processing_status='downloaded' (local files ready for vision LLM)
      * processing_status='pending' WITH a source_url (CDN-hosted images like
        TradingView charts that don't need downloading — the vision LLM can
        fetch them directly or we encode on-the-fly).

    Joins news_items to get headline, content, source_id (for company resolution).
    Filters out items older than max_age_hours.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    sql = (
        "SELECT "
        "  m.id AS media_id, "
        "  m.news_item_id, "
        "  m.local_path, "
        "  m.source_url, "
        "  m.mime_type, "
        "  m.processing_status, "
        "  n.headline, "
        "  n.content, "
        "  n.source_id, "
        "  n.instruments, "
        "  n.collected_at "
        "FROM public.media_items m "
        "JOIN public.news_items n ON n.id = m.news_item_id "
        "WHERE (m.processing_status = 'downloaded' "
        "       OR (m.processing_status = 'pending' AND m.source_url IS NOT NULL)) "
        "  AND n.collected_at > $1 "
        "ORDER BY n.collected_at ASC "
        "LIMIT $2"
    )
    async with shared_pool.acquire() as conn:
        rows = await conn.fetch(sql, cutoff, batch_size)
    return [dict(r) for r in rows]


async def resolve_company_for_source(
    shared_pool: DatabasePool,
    source_id: int,
) -> Optional[str]:
    """Resolve a collector source_id to its company name.

    Looks up collector_catalog hierarchy: if the source has a company_id
    in metadata, use it; otherwise fall back to 'jarvais' as default.
    """
    sql = (
        "SELECT metadata FROM public.collector_catalog WHERE id = $1"
    )
    async with shared_pool.acquire() as conn:
        row = await conn.fetchrow(sql, source_id)
    if not row:
        return None
    metadata = row["metadata"] or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = {}
    return metadata.get("company_id") or metadata.get("company") or "jarvais"


async def resolve_instrument_symbol(
    shared_pool: DatabasePool,
    instruments_jsonb: Any,
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve instruments JSONB to a primary (symbol, exchange) tuple.

    F8 — Bug D fix: returns ``(None, None)`` instead of the historic
    hardcoded ``("BTCUSDT","bybit")`` fallback. The hardcoded fallback
    masked every signal where the upstream pipeline failed to attach
    instrument metadata, silently routing all unrelated assets through
    BTC/USDT-on-bybit and corrupting downstream analytics.

    Callers MUST handle a (None, None) return by either:
      * skipping the row and marking media_status='failed_unresolved', or
      * recovering the symbol from another field (e.g. the text-extracted
        signal's "symbol" key), without inventing a venue.

    Args:
        shared_pool: Shared Postgres pool (reserved for future alias
            lookups).
        instruments_jsonb: Raw `media_items.instruments` payload (list,
            JSON string, or None).

    Returns:
        ``(symbol, exchange)`` if resolved from the JSONB payload, else
        ``(None, None)``. Symbol/exchange are NOT canonicalised here —
        canonicalisation happens at the consumer (e.g. F1 wiring in
        position_monitor.fetch_latest_price).
    """
    if not instruments_jsonb:
        return (None, None)
    try:
        if isinstance(instruments_jsonb, str):
            instruments_jsonb = json.loads(instruments_jsonb)
        if isinstance(instruments_jsonb, list) and instruments_jsonb:
            first = instruments_jsonb[0]
            if isinstance(first, dict):
                symbol = first.get("symbol") or first.get("ticker")
                exchange = first.get("exchange") or first.get("venue")
                if symbol:
                    return (symbol, exchange)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning(
            "resolve_instrument_symbol: malformed instruments JSONB: %s",
            exc,
        )
    return (None, None)


async def get_or_create_trader_profile(
    shared_pool: DatabasePool,
    platform: str,
    handle_raw: str,
    display_name: Optional[str] = None,
) -> int:
    """Get or create a trader_profile row and return its id.

    Uses ON CONFLICT on (platform, handle_normalized) to ensure idempotency.
    Retries up to 3 times with exponential backoff on transient DB failures.
    """
    handle_normalized = handle_raw.lower().strip().lstrip("@")
    sql = (
        "INSERT INTO public.trader_profiles "
        "  (platform, handle_raw, handle_normalized, display_name, first_seen_at, last_seen_at) "
        "VALUES ($1, $2, $3, $4, NOW(), NOW()) "
        "ON CONFLICT (platform, handle_normalized) DO UPDATE SET "
        "  last_seen_at = EXCLUDED.last_seen_at, "
        "  display_name = COALESCE(EXCLUDED.display_name, public.trader_profiles.display_name) "
        "RETURNING id"
    )
    max_attempts = 3
    backoff = 2.0
    last_exc: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            async with shared_pool.acquire() as conn:
                row = await conn.fetchrow(
                    sql, platform, handle_raw, handle_normalized, display_name or handle_raw
                )
            return int(row["id"])
        except Exception as exc:
            last_exc = exc
            wait = backoff ** attempt
            logger.warning(
                "get_or_create_trader_profile attempt %d/%d failed: %s. Retrying in %.1fs",
                attempt + 1,
                max_attempts,
                exc,
                wait,
            )
            await asyncio.sleep(wait)
    raise RuntimeError(
        f"get_or_create_trader_profile failed after {max_attempts} attempts: {last_exc}"
    )


async def write_signal_interpretation(
    shared_pool: DatabasePool,
    news_item_id: int,
    media_item_id: int,
    trader_profile_id: int,
    consensus: ConsensusResult,
    param_hash: str,
    candle_data_hash: str,
    instrument_symbol: str,
    exchange: str,
    market_data_fresh: bool,
    market_data_at: datetime,
    correlation_id: str = "",
    instrument_resolved_from: str = "unknown",
) -> Optional[int]:
    """Write a signal_interpretation row to the shared database.

    Phase 3 additions:
      * Writes prefilter_provider, vision_model_requested, vision_model_resolved,
        prompt_version, prompt_hash, llm_raw_request_path, llm_raw_response_path,
        and correlation_id columns (all Phase 2 schema additions).
      * Uses ON CONFLICT (news_item_id, model_version, param_hash) DO NOTHING
        for idempotency.

    Phase 9 addition:
      * instrument_resolved_from tracks whether the symbol came from the message
        text, context window, or LLM inference ('message','context','inferred','unknown').

    Returns:
        Inserted row ID, or None on conflict (already exists).
    """
    llm = consensus.llm_result
    quant = consensus.quant_result

    sql = (
        "INSERT INTO public.signal_interpretations ("
        "  news_item_id, media_item_id, trader_profile_id, "
        "  model_version, param_hash, candle_data_hash, "
        "  llm_direction, llm_confidence, llm_reasoning, llm_levels, "
        "  quant_direction, quant_confidence, quant_indicators, "
        "  consensus_direction, consensus_confidence, consensus_method, "
        "  instrument_symbol, exchange, market_data_fresh, market_data_at, "
        "  llm_cost_usd, quant_cost_usd, "
        "  prefilter_provider, vision_model_requested, vision_model_resolved, "
        "  prompt_version, prompt_hash, "
        "  llm_raw_request_path, llm_raw_response_path, "
        "  correlation_id, "
        "  instrument_resolved_from, "
        "  created_at"
        ") VALUES ("
        "  $1, $2, $3, $4, $5, $6, "
        "  $7, $8, $9, $10::jsonb, "
        "  $11, $12, $13::jsonb, "
        "  $14, $15, $16, "
        "  $17, $18, $19, $20, "
        "  $21, $22, "
        "  $23, $24, $25, "
        "  $26, $27, "
        "  $28, $29, "
        "  $30, "
        "  $31, "
        "  NOW()"
        ")"
        "ON CONFLICT (news_item_id, model_version, param_hash) DO NOTHING "
        "RETURNING id"
    )
    params = (
        news_item_id,
        media_item_id,
        trader_profile_id,
        llm.model_used if llm else "",
        param_hash,
        candle_data_hash,
        llm.direction if llm else "unclear",
        round(llm.confidence, 4) if llm else 0.0,
        llm.reasoning if llm else "",
        json.dumps(llm.levels if llm else {}),
        quant.direction if quant else "unclear",
        round(quant.confidence, 4) if quant else 0.0,
        json.dumps(quant.indicators if quant else {}),
        consensus.direction,
        round(consensus.confidence, 4),
        consensus.method,
        instrument_symbol,
        exchange,
        market_data_fresh,
        market_data_at,
        round(llm.cost_usd, 6) if llm else 0.0,
        round(quant.cost_usd, 6) if quant else 0.0,
        # Phase 3: prefilter / vision model metadata
        "",  # prefilter_provider — not yet wired; placeholder
        llm.model_used if llm else "",
        llm.model_resolved if llm else "",
        llm.prompt_version if llm else "",
        llm.prompt_hash if llm else "",
        llm.request_path if llm else "",
        llm.response_path if llm else "",
        correlation_id,
        instrument_resolved_from,
    )
    row = await shared_pool.fetch_one(sql, params)
    if row is None:
        logger.debug(
            "signal_interpretation dedup skip: news_item_id=%s model=%s hash=%s",
            news_item_id,
            llm.model_used if llm else "",
            param_hash,
        )
        return None
    return int(row["id"])


async def create_tracked_position_from_interpretation(
    shared_pool: DatabasePool,
    signal_interpretation_id: int,
    news_item_id: int,
    media_item_id: Optional[int],
    trader_profile_id: int,
    instrument_symbol: str,
    instrument_exchange: str,
    direction: str,
    entry_price: Optional[float],
    stop_loss: Optional[float],
    take_profit_1: Optional[float],
    detection_method: str,
    detection_confidence: float,
    raw_signal_text: str,
    company_id: str = "jarvais",
    *,
    entry_reason_trader: Optional[str] = None,
    entry_reason_llm: Optional[str] = None,
    entry_reason_agent: Optional[str] = None,
    correlation_id: str = "",
) -> Optional[int]:
    """Create a tracked_positions row from a signal interpretation.

    Phase 6 additions:
      * Populates instrument_symbol_normalised via normalise_instrument().
      * Computes reason_agreement_score via compute_reason_agreement().
      * Embeds entry_reason_trader via embed() for pgvector similarity.
      * Freezes entry reasons at open time (hindsight-bias guard).

    This is the critical wiring that feeds the PositionMonitor and
    ChartHackerGuru downstream pipelines.

    Args:
        shared_pool: Shared Postgres pool.
        signal_interpretation_id: FK to signal_interpretations.id.
        news_item_id: FK to news_items.id.
        media_item_id: FK to media_items.id (nullable).
        trader_profile_id: FK to trader_profiles.id.
        instrument_symbol: Resolved symbol (e.g. BTCUSDT).
        instrument_exchange: Resolved exchange (e.g. bybit).
        direction: 'long' or 'short'.
        entry_price: Detected entry price (nullable).
        stop_loss: Detected stop loss (nullable).
        take_profit_1: Detected take profit (nullable).
        detection_method: How the signal was detected.
        detection_confidence: Confidence score 0.0-1.0.
        raw_signal_text: The original message text.
        company_id: Company context for multi-tenancy.
        entry_reason_trader: Human trader's stated reason (optional).
        entry_reason_llm: LLM-generated reasoning (optional).
        entry_reason_agent: Agent-generated reasoning (optional).
        correlation_id: Trace ID for cross-service correlation.

    Returns:
        Inserted tracked_positions.id, or None on conflict/duplicate.
    """
    if direction not in ("long", "short"):
        logger.debug(
            "Skipping tracked_position creation: direction=%s is not tradeable",
            direction,
        )
        return None

    # F5 — entry_price sanity guard.
    #
    # Pre-F5, the pipeline would silently insert tracked_positions with
    # entry_price IS NULL whenever the LLM/quant track failed to extract
    # a level. That produced 77 orphan rows (PHASE_X0_POSITION_PIPELINE
    # diagnosis 2026-05-02) which then crashed PositionMonitor on every
    # cycle until F7 added the NULL guard.
    #
    # New default: refuse the insert and emit a structured WARN with the
    # full signal context so F9's backfill script can find these and
    # resolve P&L from raw_signal_text + candle history. The env-var
    # override exists so F9 itself can insert + immediately backfill in a
    # single transaction without tripping its own guard.
    allow_null_entry = os.environ.get("ALLOW_NULL_ENTRY_PRICE", "0") == "1"
    if (entry_price is None or float(entry_price) <= 0.0) and not allow_null_entry:
        logger.warning(
            "F5 reject tracked_position: entry_price missing/invalid "
            "news_item_id=%s media_item_id=%s trader=%s symbol=%s exchange=%s "
            "direction=%s detection_method=%s correlation_id=%s "
            "raw_signal_text=%r",
            news_item_id,
            media_item_id,
            trader_profile_id,
            instrument_symbol,
            instrument_exchange,
            direction,
            detection_method,
            correlation_id or "",
            (raw_signal_text or "")[:240],
        )
        return None

    now = datetime.now(timezone.utc)

    # Phase 6: normalise instrument symbol for cross-venue lookups
    sym_norm, venue_norm = normalise_instrument(
        instrument_symbol, instrument_exchange or "bybit"
    )

    # Phase 6: compute reason agreement + embed trader reason
    reason_agreement: Optional[float] = None
    trader_embed: Optional[list[float]] = None
    if entry_reason_trader:
        try:
            if entry_reason_llm:
                reason_agreement = await compute_reason_agreement(
                    entry_reason_trader, entry_reason_llm
                )
            trader_embed = await embed(entry_reason_trader)
        except Exception as exc:
            logger.warning(
                "Phase 6 reason processing failed for news_item_id=%s: %s",
                news_item_id,
                exc,
            )

    row = await shared_pool.fetch_one(
        """
        INSERT INTO public.tracked_positions (
            news_item_id, media_item_id, trader_profile_id,
            signal_interpretation_id,
            instrument_symbol, instrument_exchange,
            instrument_symbol_normalised,
            direction, entry_price, stop_loss, take_profit_1,
            detection_method, detection_confidence,
            raw_signal_text, signal_timestamp,
            status, company_id,
            entry_reason_trader, entry_reason_llm, entry_reason_agent,
            entry_reason_frozen_at, reason_agreement_score,
            entry_reason_trader_embedding,
            correlation_id,
            created_at, updated_at
        ) VALUES (
            $1, $2, $3,
            $4,
            $5, $6,
            $7,
            $8, $9, $10, $11,
            $12, $13,
            $14, $15,
            $16, $17,
            $18, $19, $20,
            $21, $22,
            $23::vector(384),
            $24,
            $25, $25
        )
        ON CONFLICT (news_item_id, trader_profile_id, instrument_symbol, direction)
        DO NOTHING
        RETURNING id
        """,
        (
            news_item_id,
            media_item_id,
            trader_profile_id,
            signal_interpretation_id,
            instrument_symbol,
            instrument_exchange or "bybit",
            sym_norm,
            direction,
            entry_price,
            stop_loss,
            take_profit_1,
            detection_method,
            round(detection_confidence, 4),
            raw_signal_text[:2000] if raw_signal_text else "",
            now,
            "open",
            company_id,
            entry_reason_trader[:2000] if entry_reason_trader else None,
            entry_reason_llm[:2000] if entry_reason_llm else None,
            entry_reason_agent[:2000] if entry_reason_agent else None,
            now,
            round(reason_agreement, 4) if reason_agreement is not None else None,
            trader_embed,
            correlation_id or None,
            now,
        ),
    )
    if row is None:
        logger.info(
            "tracked_position dedup skip: news_item_id=%s trader=%s symbol=%s dir=%s",
            news_item_id,
            trader_profile_id,
            instrument_symbol,
            direction,
        )
        return None

    position_id = int(row["id"])
    logger.info(
        "Created tracked_position %s from interpretation %s: %s %s @ %.4f",
        position_id,
        signal_interpretation_id,
        instrument_symbol,
        direction,
        entry_price or 0.0,
    )
    return position_id


async def update_media_status(
    shared_pool: DatabasePool,
    media_id: int,
    status: str,
    error: Optional[str] = None,
) -> None:
    """Update media_items.processing_status and optional error."""
    if error:
        sql = (
            "UPDATE public.media_items SET "
            "  processing_status = $1, "
            "  processing_error = $2, "
            "  processed_at = NOW() "
            "WHERE id = $3"
        )
        async with shared_pool.acquire() as conn:
            await conn.execute(sql, status, error, media_id)
    else:
        sql = (
            "UPDATE public.media_items SET "
            "  processing_status = $1, "
            "  processed_at = NOW() "
            "WHERE id = $2"
        )
        async with shared_pool.acquire() as conn:
            await conn.execute(sql, status, media_id)


# ---------------------------------------------------------------------------
# MemU Broadcast
# ---------------------------------------------------------------------------
async def broadcast_insight(
    payload: "BroadcastPayload",
    *,
    shared_pool: DatabasePool,
) -> None:
    """Broadcast a significant insight via durable outbox + pg_notify.

    Inserts into memu_outbox in a transaction, then NOTIFYs the listener.
    If the listener is offline the row stays unprocessed and is back-filled
    on reconnect. If the listener is online the NOTIFY ID lookup is O(1).

    Args:
        payload: TypedDict conforming to BroadcastPayload schema.
        shared_pool: Shared Postgres pool for the outbox insert.
    """
    try:
        body = json.dumps(payload, separators=(",", ":"))
        async with shared_pool.acquire() as conn:
            async with conn.transaction():
                row_id = await conn.fetchval(
                    "INSERT INTO public.memu_outbox (payload) VALUES ($1::jsonb) RETURNING id",
                    body,
                )
                await conn.execute(
                    "SELECT pg_notify('memu_broadcast', $1)",
                    str(row_id),
                )
        logger.debug("broadcast_insight: outbox row %s kind=%s", row_id, payload.get("insight_kind"))
    except Exception as exc:
        logger.error("broadcast_insight failed: %s", exc)
        raise


# ---------------------------------------------------------------------------
# Main Worker
# ---------------------------------------------------------------------------
class InterpretationService:
    """Daemon that polls media_items and runs dual-track interpretation."""

    def __init__(self, cfg: Optional[InterpretationConfig] = None) -> None:
        self.cfg = cfg or InterpretationConfig()
        self._stop = asyncio.Event()
        self._shared_pool: Optional[DatabasePool] = None
        self._rate_limiter = get_default_limiter()

    async def _ensure_pool(self) -> DatabasePool:
        if self._shared_pool is None:
            self._shared_pool = await get_shared_pool()
        return self._shared_pool

    async def _process_one(
        self,
        media_row: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Process a single media item: resolve context, run dual-track, write results.

        Returns a status dict for logging/metrics.
        """
        media_id = media_row["media_id"]
        news_item_id = media_row["news_item_id"]
        local_path = media_row["local_path"]
        source_id = media_row.get("source_id")
        headline = media_row.get("headline", "")
        content = media_row.get("content", "")
        instruments_jsonb = media_row.get("instruments")

        # Resolve company
        shared_pool = await self._ensure_pool()
        company = "jarvais"
        if source_id:
            resolved = await resolve_company_for_source(shared_pool, source_id)
            if resolved:
                company = resolved

        # Resolve instrument — F8: refuses to invent BTCUSDT/bybit when
        # upstream metadata is missing. Skip the row instead of silently
        # mis-tagging it.
        symbol, exchange = await resolve_instrument_symbol(
            shared_pool, instruments_jsonb
        )
        if not symbol:
            logger.warning(
                "media_id=%s: unresolved instrument (instruments=%r); marking failed",
                media_id, instruments_jsonb,
            )
            await update_media_status(
                shared_pool, media_id, "failed",
                error="unresolved_instrument",
            )
            return {
                "media_id": media_id,
                "status": "failed_unresolved_instrument",
            }

        # Resolve trader profile (from news item author/channel)
        author = media_row.get("author", "unknown")
        channel = media_row.get("headline", "")  # fallback
        platform = "discord"  # default; could be resolved from source metadata
        trader_profile_id = await get_or_create_trader_profile(
            shared_pool, platform, author or "unknown", display_name=author
        )

        # Check file exists (or source_url for CDN-hosted images)
        source_url = media_row.get("source_url")
        if not local_path or not Path(local_path).exists():
            if source_url:
                # CDN-hosted image (TradingView chart, etc.) — download on-the-fly
                # for vision LLM processing
                logger.info(
                    "CDN media (media_id=%s): downloading %s for vision analysis",
                    media_id, source_url[:80],
                )
                try:
                    import aiohttp
                    async with aiohttp.ClientSession() as session:
                        async with session.get(source_url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                            if resp.status == 200:
                                import tempfile
                                import os
                                suffix = ".png" if ".png" in source_url else ".jpg"
                                fd, tmp_path = tempfile.mkstemp(suffix=suffix)
                                try:
                                    os.write(fd, await resp.read())
                                    local_path = tmp_path
                                finally:
                                    os.close(fd)
                                logger.debug("Downloaded CDN image to %s", local_path)
                            else:
                                logger.warning(
                                    "CDN download failed HTTP %d for %s",
                                    resp.status, source_url[:80],
                                )
                                await update_media_status(
                                    shared_pool, media_id, "failed",
                                    error=f"CDN download HTTP {resp.status}",
                                )
                                return {
                                    "media_id": media_id,
                                    "status": "failed",
                                    "reason": f"cdn_download_http_{resp.status}",
                                }
                except Exception as e:
                    logger.warning("CDN download error for media_id=%s: %s", media_id, e)
                    await update_media_status(
                        shared_pool, media_id, "failed",
                        error=f"CDN download error: {e}"[:500],
                    )
                    return {
                        "media_id": media_id,
                        "status": "failed",
                        "reason": f"cdn_download_error: {e}",
                    }
            else:
                logger.warning("Media file missing: %s (media_id=%s)", local_path, media_id)
                await update_media_status(
                    shared_pool, media_id, "failed", error="local_path missing or file not found"
                )
                return {"media_id": media_id, "status": "failed", "reason": "file_missing"}

        # --- Text signal extraction (parallel track) ---
        text_signal = None
        if content and len(content.strip()) > 10:
            msg_type = classify_message_type(content)
            if msg_type in ("trade_setup", "unknown"):
                try:
                    text_signal = await extract_signal_from_text(
                        text=content,
                        author=author,
                        context={"channel": headline, "source_id": source_id},
                        use_llm_fallback=False,  # Regex only to save cost; LLM fallback in dedicated cycle
                    )
                    if text_signal:
                        logger.info(
                            "Text signal extracted for media_id=%s: %s %s @ %.2f",
                            media_id,
                            text_signal["symbol"],
                            text_signal["direction"],
                            text_signal["entry"],
                        )
                except Exception as e:
                    logger.debug("Text extraction failed for media_id=%s: %s", media_id, e)

        # --- Dedup check before LLM (if text signal has levels) ---
        if text_signal and text_signal.get("entry"):
            dup_id = await find_duplicate_position(
                shared_pool,
                symbol=text_signal["symbol"],
                direction=text_signal["direction"],
                entry=text_signal["entry"],
                stop_loss=text_signal.get("stop_loss"),
                take_profit=text_signal.get("take_profits", [None])[0],
                hours=8,
                tolerance_pct=0.02,
            )
            if dup_id:
                logger.info(
                    "media_id=%s is continuation of position #%d (text dedup). Skipping LLM.",
                    media_id,
                    dup_id,
                )
                await update_media_status(shared_pool, media_id, "analyzed")
                return {
                    "media_id": media_id,
                    "status": "analyzed",
                    "direction": text_signal["direction"],
                    "confidence": 0.6,
                    "method": "text_dedup_continuation",
                    "duplicate_of": dup_id,
                }

        # --- Correlation ID for this signal interpretation chain ---
        cid = new_correlation_id("sig")

        # --- LLM Track (with rate limiting) ---
        news_context = f"{headline}\n{content}"[:1000]
        try:
            await self._rate_limiter.acquire(
                model=self.cfg.primary_model,
                estimated_cost_usd=0.005,
            )
            llm_result = await run_llm_track(
                cfg=self.cfg,
                image_path=local_path,
                news_context=news_context,
                instrument_symbol=symbol,
                correlation_id=cid,
            )
            self._rate_limiter.report_success()
        except RuntimeError as exc:
            logger.error("LLM track failed for media_id=%s: %s", media_id, exc)
            await update_media_status(
                shared_pool,
                media_id,
                "skipped_vision_unavailable",
                error=str(exc)[:500],
            )
            return {
                "media_id": media_id,
                "status": "skipped_vision_unavailable",
                "reason": str(exc),
            }

        # --- Quant Track ---
        from shared.utils.db import get_company_pool

        company_pool = await get_company_pool(company)
        quant_result = await run_quant_track(
            shared_pool=shared_pool,
            company_pool=company_pool,
            instrument_symbol=symbol,
            exchange=exchange,
            freshness_threshold=self.cfg.freshness_threshold_s,
        )

        # --- Consensus ---
        consensus = run_consensus(llm_result, quant_result)

        # --- Param hash for reproducibility (Rule 1) ---
        param_hash = _param_hash(
            model=llm_result.model_used,
            primary_model=self.cfg.primary_model,
            fallback_model=self.cfg.fallback_model,
            freshness_threshold=self.cfg.freshness_threshold_s,
            max_age_hours=self.cfg.max_age_hours,
        )

        # Candle data hash (simplified: hash of last 10 closes)
        candle_data_hash = "n/a"
        if quant_result.indicators:
            candle_data_hash = hashlib.sha256(
                json.dumps(quant_result.indicators, sort_keys=True).encode()
            ).hexdigest()[:16]

        # Market data freshness
        market_data_fresh = quant_result.direction != "unclear" or quant_result.confidence > 0
        market_data_at = datetime.now(timezone.utc)

        # --- Write signal_interpretation ---
        # Phase 9: determine instrument_resolved_from based on news_item context_window
        resolved_from = "message"
        if news_item.get("context_window"):
            resolved_from = "context"
        sig_id = await write_signal_interpretation(
            shared_pool=shared_pool,
            news_item_id=news_item_id,
            media_item_id=media_id,
            trader_profile_id=trader_profile_id,
            consensus=consensus,
            param_hash=param_hash,
            candle_data_hash=candle_data_hash,
            instrument_symbol=symbol,
            exchange=exchange,
            market_data_fresh=market_data_fresh,
            market_data_at=market_data_at,
            correlation_id=cid,
            instrument_resolved_from=resolved_from,
        )

        # --- Wire to tracked_positions (feeds PositionMonitor + ChartHackerGuru) ---
        if sig_id is not None:
            try:
                await create_tracked_position_from_interpretation(
                    shared_pool=shared_pool,
                    signal_interpretation_id=sig_id,
                    news_item_id=news_item_id,
                    media_item_id=media_id,
                    trader_profile_id=trader_profile_id,
                    instrument_symbol=symbol,
                    instrument_exchange=exchange or "bybit",
                    direction=consensus.direction,
                    entry_price=llm_result.levels.get("entry") if llm_result and llm_result.levels else None,
                    stop_loss=llm_result.levels.get("stop_loss") if llm_result and llm_result.levels else None,
                    take_profit_1=llm_result.levels.get("take_profit") if llm_result and llm_result.levels else None,
                    detection_method=consensus.method,
                    detection_confidence=consensus.confidence,
                    raw_signal_text=f"{headline}\n{content}"[:2000],
                    company_id=company,
                    entry_reason_trader=content[:2000] if content else None,
                    entry_reason_llm=llm_result.reasoning[:2000] if llm_result and llm_result.reasoning else None,
                    correlation_id=cid,
                )
            except Exception as exc:
                logger.warning("Failed to create tracked_position for sig_id=%s: %s", sig_id, exc)

        # --- Update media status ---
        await update_media_status(shared_pool, media_id, "analyzed")

        # --- MemU broadcast for high-confidence signals ---
        if consensus.confidence >= 0.7 and consensus.direction in ("long", "short"):
            from shared.memu.broadcast_payload import BroadcastPayload

            payload = BroadcastPayload(
                schema_version=1,
                company=company,
                actor_type="agent",
                actor_id="chart_hacker",
                insight_kind="lesson",
                summary=f"{consensus.direction.upper()} {symbol}@{exchange} signal",
                body_md=(
                    f"ChartHacker signal: {consensus.direction.upper()} {symbol}@{exchange} "
                    f"(confidence {consensus.confidence:.0%}).\n\n"
                    f"LLM: {llm_result.direction} ({llm_result.confidence:.0%}).\n"
                    f"Quant: {quant_result.direction} ({quant_result.confidence:.0%}).\n"
                    f"Reasoning: {llm_result.reasoning[:200]}"
                ),
                instrument_symbol_normalised=symbol,
                instrument_exchange=exchange or "bybit",
                correlation_id=cid,
                created_at_iso=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            )
            await broadcast_insight(payload, shared_pool=shared_pool)

        return {
            "media_id": media_id,
            "status": "analyzed",
            "direction": consensus.direction,
            "confidence": consensus.confidence,
            "method": consensus.method,
            "llm_model": llm_result.model_used,
            "llm_cost_usd": round(llm_result.cost_usd, 6),
            "quant_cost_usd": round(quant_result.cost_usd, 6),
        }

    async def _process_text_one(
        self,
        news_row: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Process a single text-only news item for trade signal extraction.

        Uses regex + lightweight LLM fallback to detect trade setups in message text.
        """
        news_item_id = news_row["id"]
        content = news_row.get("content", "")
        headline = news_row.get("headline", "")
        author = news_row.get("author", "unknown")
        source_id = news_row.get("source_id")
        instruments_jsonb = news_row.get("instruments")

        if not content or len(content.strip()) < 10:
            return {"news_item_id": news_item_id, "status": "skipped_no_content"}

        # Classify message type
        msg_type = classify_message_type(content)
        if msg_type == "meme":
            return {"news_item_id": news_item_id, "status": "skipped_meme"}
        if msg_type == "commentary" and len(content.strip()) < 100:
            return {"news_item_id": news_item_id, "status": "skipped_commentary"}

        # Extract signal
        try:
            signal = await extract_signal_from_text(
                text=content,
                author=author,
                context={"channel": headline, "source_id": source_id},
                use_llm_fallback=True,
            )
        except Exception as e:
            logger.debug("Text extraction failed for news_item_id=%s: %s", news_item_id, e)
            signal = None

        if not signal:
            return {"news_item_id": news_item_id, "status": "no_signal_detected"}

        # Resolve company and instrument
        shared_pool = await self._ensure_pool()
        company = "jarvais"
        if source_id:
            resolved = await resolve_company_for_source(shared_pool, source_id)
            if resolved:
                company = resolved

        symbol, exchange = await resolve_instrument_symbol(
            shared_pool, instruments_jsonb
        )
        if not symbol:
            symbol = signal.get("symbol", "UNKNOWN")

        # Resolve trader profile
        platform = "discord"
        trader_profile_id = await get_or_create_trader_profile(
            shared_pool, platform, author or "unknown", display_name=author
        )

        # Dedup check
        dup_id = await find_duplicate_position(
            shared_pool,
            symbol=signal["symbol"],
            direction=signal["direction"],
            entry=signal["entry"],
            stop_loss=signal.get("stop_loss"),
            take_profit=(signal.get("take_profits") or [None])[0],
            hours=8,
            tolerance_pct=0.02,
        )
        if dup_id:
            logger.info(
                "news_item_id=%s is continuation of position #%d (text dedup).",
                news_item_id,
                dup_id,
            )
            return {
                "news_item_id": news_item_id,
                "status": "analyzed",
                "direction": signal["direction"],
                "confidence": 0.5,
                "method": "text_dedup_continuation",
                "duplicate_of": dup_id,
            }

        # Build a lightweight consensus for text signals
        shared_pool = await self._ensure_pool()

        # Write signal_interpretation for text-only signal
        text_consensus = ConsensusResult(
            direction=signal["direction"],
            confidence=signal.get("confidence", 0.5),
            method="text_extraction",
            llm_result=None,
            quant_result=None,
        )
        param_hash = _param_hash(
            model="text_extractor",
            primary_model="regex+llm",
            fallback_model="",
            freshness_threshold=self.cfg.freshness_threshold_s,
            max_age_hours=self.cfg.max_age_hours,
        )
        # Phase 9: text-only signals resolve from message text
        text_resolved_from = "message"
        if news_item.get("context_window"):
            text_resolved_from = "context"
        sig_id = await write_signal_interpretation(
            shared_pool=shared_pool,
            news_item_id=news_item_id,
            media_item_id=None,  # No media for text-only
            trader_profile_id=trader_profile_id,
            consensus=text_consensus,
            param_hash=param_hash,
            candle_data_hash="n/a",
            instrument_symbol=symbol,
            exchange=exchange,
            market_data_fresh=False,
            market_data_at=datetime.now(timezone.utc),
            instrument_resolved_from=text_resolved_from,
        )

        # --- Wire to tracked_positions ---
        if sig_id is not None:
            try:
                await create_tracked_position_from_interpretation(
                    shared_pool=shared_pool,
                    signal_interpretation_id=sig_id,
                    news_item_id=news_item_id,
                    media_item_id=None,
                    trader_profile_id=trader_profile_id,
                    instrument_symbol=symbol or signal.get("symbol", "UNKNOWN"),
                    instrument_exchange=exchange or "bybit",
                    direction=signal["direction"],
                    entry_price=signal.get("entry"),
                    stop_loss=signal.get("stop_loss"),
                    take_profit_1=(signal.get("take_profits") or [None])[0],
                    detection_method="text_extraction",
                    detection_confidence=signal.get("confidence", 0.5),
                    raw_signal_text=content[:2000],
                    company_id=company,
                    entry_reason_trader=content[:2000] if content else None,
                )
            except Exception as exc:
                logger.warning("Failed to create tracked_position for text sig_id=%s: %s", sig_id, exc)

        logger.info(
            "Text signal written for news_item_id=%s: %s %s @ %.2f",
            news_item_id,
            signal["symbol"],
            signal["direction"],
            signal["entry"],
        )

        return {
            "news_item_id": news_item_id,
            "status": "analyzed",
            "direction": signal["direction"],
            "confidence": signal.get("confidence", 0.5),
            "method": "text_extraction",
            "symbol": signal["symbol"],
            "entry": signal["entry"],
        }

    async def run_cycle(self) -> Dict[str, Any]:
        """Fetch one batch, process each item, return summary stats.

        Processes both media items (images/charts) and text-only news items
        in parallel tracks.
        """
        shared_pool = await self._ensure_pool()

        # ── Media track ───────────────────────────────────────────────
        media_rows = await fetch_pending_media(
            shared_pool,
            batch_size=self.cfg.batch_size,
            max_age_hours=self.cfg.max_age_hours,
        )
        media_results: List[Dict[str, Any]] = []
        if media_rows:
            logger.info("Interpretation cycle: %d media items to process", len(media_rows))
            for row in media_rows:
                if self._stop.is_set():
                    break
                try:
                    result = await self._process_one(row)
                    media_results.append(result)
                except Exception as exc:
                    logger.exception("Unexpected error processing media_id=%s: %s", row.get("media_id"), exc)
                    try:
                        await update_media_status(
                            shared_pool,
                            row["media_id"],
                            "failed",
                            error=f"unexpected: {exc}"[:500],
                        )
                    except Exception:
                        pass
                    media_results.append({
                        "media_id": row.get("media_id"),
                        "status": "failed",
                        "reason": str(exc),
                    })

        # ── Text-only track ───────────────────────────────────────────
        text_results: List[Dict[str, Any]] = []
        try:
            text_rows = await self._fetch_pending_text_news(shared_pool)
            if text_rows:
                logger.info("Interpretation cycle: %d text-only news items to process", len(text_rows))
                for row in text_rows:
                    if self._stop.is_set():
                        break
                    try:
                        result = await self._process_text_one(row)
                        text_results.append(result)
                    except Exception as exc:
                        logger.exception("Unexpected error processing news_item_id=%s: %s", row.get("id"), exc)
                        text_results.append({
                            "news_item_id": row.get("id"),
                            "status": "failed",
                            "reason": str(exc),
                        })
        except Exception as e:
            logger.warning("Text news fetch/processing failed: %s", e)

        analyzed_media = sum(1 for r in media_results if r.get("status") == "analyzed")
        skipped_media = sum(1 for r in media_results if r.get("status") == "skipped_vision_unavailable")
        failed_media = sum(1 for r in media_results if r.get("status") == "failed")
        analyzed_text = sum(1 for r in text_results if r.get("status") == "analyzed")

        logger.info(
            "Interpretation cycle complete: media(analyzed=%d skipped=%d failed=%d) "
            "text(analyzed=%d failed=%d)",
            analyzed_media,
            skipped_media,
            failed_media,
            analyzed_text,
            sum(1 for r in text_results if r.get("status") == "failed"),
        )
        return {
            "processed": len(media_results) + len(text_results),
            "analyzed_media": analyzed_media,
            "skipped_media": skipped_media,
            "failed_media": failed_media,
            "analyzed_text": analyzed_text,
            "results": media_results + text_results,
        }

    async def _fetch_pending_text_news(self, shared_pool: DatabasePool) -> List[Dict[str, Any]]:
        """Fetch news_items that have content but no associated media_items,
        and haven't been processed for text signals yet.

        Excludes items already processed (checked via signal_interpretations
        with media_item_id=0 or NULL).
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.cfg.max_age_hours)
        sql = (
            "SELECT "
            "  n.id, n.headline, n.content, n.author, n.source_id, "
            "  n.instruments, n.collected_at "
            "FROM public.news_items n "
            "LEFT JOIN public.media_items m ON m.news_item_id = n.id "
            "WHERE m.id IS NULL "
            "  AND n.content IS NOT NULL "
            "  AND LENGTH(n.content) > 20 "
            "  AND n.collected_at > $1 "
            "  AND n.enrichment_status NOT IN ('non_signal','skipped','duplicate_zone') "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM public.signal_interpretations si "
            "    WHERE si.news_item_id = n.id "
            "      AND (si.media_item_id = 0 OR si.media_item_id IS NULL)"
            "  ) "
            "ORDER BY n.collected_at ASC "
            "LIMIT $2"
        )
        async with shared_pool.acquire() as conn:
            rows = await conn.fetch(sql, cutoff, self.cfg.batch_size)
        return [dict(r) for r in rows]

    async def run_forever(self) -> None:
        """Main loop: poll, process, sleep until stop event."""
        logger.info("InterpretationService started (poll=%.0fs, batch=%d, max_age=%.0fh)",
                    self.cfg.poll_interval_s, self.cfg.batch_size, self.cfg.max_age_hours)
        while not self._stop.is_set():
            try:
                await self.run_cycle()
            except Exception as exc:
                logger.exception("Interpretation cycle crashed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.poll_interval_s)
            except asyncio.TimeoutError:
                pass
        logger.info("InterpretationService stopped")

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------
# Signal handlers
# ---------------------------------------------------------------------------
def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, AttributeError, RuntimeError):
            if os.name != "nt" or sig == signal.SIGINT:
                try:
                    signal.signal(sig, lambda *_a: stop_event.set())
                except (OSError, ValueError):
                    pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_env()

    cfg = InterpretationConfig()
    svc = InterpretationService(cfg)
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, svc._stop)

    try:
        await svc.run_forever()
    finally:
        logger.info("InterpretationService shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
