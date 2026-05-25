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
import re as _re
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
from shared.intelligence.text_signal_extractor import (
    classify_message_type,
    extract_signal_from_text,
    strip_reply_prefix,
)
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
# Round 10 (2026-05-24): the three vision-model slots are now resolved at
# runtime via shared.intelligence.model_config (DB-row > env-var > code-default).
# These module-level constants stay for backward-compat — they hold the
# bootstrap (env-or-default) value at process start. The canonical per-cycle
# lookup is `await get_model(slot)`, which picks up dashboard dropdown changes
# inside the 60-second cache window. See:
#   shared/intelligence/model_config.py
#   shared/intelligence/vision_model_catalogue.json
from shared.intelligence.model_config import (
    get_model as _runtime_get_model,
    get_model_sync as _bootstrap_get_model,
    SLOT_PRIMARY,
    SLOT_FALLBACK,
    SLOT_PREFILTER,
)

PRIMARY_MODEL = _bootstrap_get_model(SLOT_PRIMARY)
FALLBACK_MODEL = _bootstrap_get_model(SLOT_FALLBACK)
PREFILTER_MODEL = _bootstrap_get_model(SLOT_PREFILTER)
PREFILTER_ENABLED = os.environ.get("CHART_HACKER_PREFILTER_ENABLED", "true").lower() in ("1", "true", "yes")
FRESHNESS_THRESHOLD_S = float(os.environ.get("INTERPRETATION_FRESHNESS_S", "180"))
MEDIA_RETENTION_DAYS = float(os.environ.get("MEDIA_RETENTION_DAYS", "7.0"))


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
    media_retention_days: float = MEDIA_RETENTION_DAYS


@dataclass
class LlmResult:
    """Result from the LLM vision track.

    Phase J (2026-05-03) — extended for dual extraction. The LLM now returns
    both ``trader_trades`` (what the human marked on the chart) and
    ``chart_hacker_trades`` (chart_hacker's own independent analysis). The old
    ``direction`` / ``confidence`` / ``levels`` fields are retained as a
    "primary signal" view derived from the dominant trade in either array
    (trader takes precedence) so existing callers keep working.
    """

    direction: str = "unclear"
    confidence: float = 0.0
    reasoning: str = ""
    levels: Dict[str, Any] = field(default_factory=dict)
    instrument: str = ""  # LLM-identified trading pair from the chart
    timeframe: str = ""   # LLM-identified chart timeframe
    # Phase J — full Lens-style dual extraction
    trader_trades:       List[Dict[str, Any]] = field(default_factory=list)
    chart_hacker_trades: List[Dict[str, Any]] = field(default_factory=list)
    chart_analysis:      Dict[str, Any]       = field(default_factory=dict)
    ai_agreement:        float                = 0.0
    ai_comment:          str                  = ""
    trader_market_view:  str                  = ""
    chart_hacker_market_view: str             = ""
    # Existing model/audit fields
    model_used: str = ""
    model_resolved: str = ""
    cost_usd: float = 0.0
    raw_response: str = ""
    request_path: str = ""
    response_path: str = ""
    prompt_version: str = ""
    prompt_hash: str = ""
    prompt_source: str = ""  # 'db', 'config', or 'file'


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
# ---------------------------------------------------------------------------
# Phase J — chart_hacker actor identity (cached)
# ---------------------------------------------------------------------------
_CHART_HACKER_PROFILE_ID: Optional[int] = None


async def _get_chart_hacker_profile_id(pool: DatabasePool) -> Optional[int]:
    """Resolve chart_hacker's trader_profiles.id (memoised).

    The migration 2026_05_03_phase_j seeds a row with platform='api' and
    handle_normalized='chart_hacker'. We look it up by those keys so we
    don't depend on a hardcoded id.
    """
    global _CHART_HACKER_PROFILE_ID
    if _CHART_HACKER_PROFILE_ID is not None:
        return _CHART_HACKER_PROFILE_ID
    try:
        row = await pool.fetch_one(
            "SELECT id FROM public.trader_profiles "
            "WHERE platform = $1 AND handle_normalized = $2",
            ("api", "chart_hacker"),
        )
        if row:
            _CHART_HACKER_PROFILE_ID = int(row["id"])
            return _CHART_HACKER_PROFILE_ID
    except Exception as exc:
        logger.warning("get chart_hacker profile id failed: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Phase J — Current price lookup (fallback when entry is missing)
# ---------------------------------------------------------------------------
async def _lookup_current_price(
    pool: DatabasePool,
    symbol: str,
    exchange: str = "bybit",
) -> Optional[float]:
    """Fetch the latest 1m candle close for ``symbol`` on ``exchange``.

    Used as a fallback when the trader/chart_hacker provides direction +
    symbol but no explicit entry price (e.g. text-only "long TIA"). We use
    the latest known price as the entry so the position can still be
    tracked. If no candle is available, return None and let the caller
    decide whether to skip or proceed.

    Args:
        pool: Shared Postgres pool.
        symbol: Slash-form symbol (e.g. 'BTC/USDT').
        exchange: Exchange/venue string.

    Returns:
        Latest close price as float, or None if no candle found.
    """
    if not symbol:
        return None
    try:
        row = await pool.fetch_one(
            """
            SELECT c.close
              FROM public.candles c
              JOIN public.instruments i ON i.id = c.instrument_id
             WHERE i.symbol = $1
               AND i.exchange = $2
               AND i.is_active = TRUE
               AND c.timeframe = '1m'::timeframe_t
             ORDER BY c.timestamp DESC
             LIMIT 1
            """,
            (symbol, exchange),
        )
    except Exception as exc:
        logger.debug("price lookup failed for %s@%s: %s", symbol, exchange, exc)
        return None
    if not row:
        return None
    try:
        return float(row["close"])
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Slice 2 — Always-on quant: CCXT live-price probe (degraded fallback)
# ---------------------------------------------------------------------------
async def _ccxt_live_price(
    symbol: str,
    exchange: str = "bybit",
    *,
    timeout_s: float = 5.0,
) -> Optional[Tuple[float, str, int]]:
    """Probe CCXT for the live price of ``symbol`` on ``exchange``.

    Thin wrapper around :func:`shared.market_data.live_price.fetch_live_price`
    that swallows all errors and returns ``None`` so the quant track can
    fall back to a degraded ``QuantResult`` rather than abort. Always
    closes the underlying CCXT instance via the helper's ``finally``.

    Args:
        symbol: Trading pair (any common form).
        exchange: Lower-cased exchange id.
        timeout_s: Hard deadline for the probe.

    Returns:
        ``(price, resolved_symbol, ts_ms)`` on success, else ``None``.
    """
    if not symbol:
        return None
    try:
        from shared.market_data.live_price import fetch_live_price
    except Exception as exc:  # noqa: BLE001 - import-time failure
        logger.debug("ccxt live-price helper unavailable: %s", exc)
        return None
    try:
        result = await fetch_live_price(
            symbol, exchange or "bybit", timeout_s=timeout_s,
        )
    except Exception as exc:  # noqa: BLE001 - probe is best-effort
        logger.info(
            "ccxt live-price probe failed for %s@%s: %s",
            symbol, exchange, exc,
        )
        return None
    return result.price, result.symbol, result.ts_ms


def _quant_symbol_candidates(symbol: str) -> List[str]:
    """Return alternate ``instruments.symbol`` forms to look up in DB.

    The collector / LLM may yield slash form (``BTC/USDT``), no-slash
    (``BTCUSDT``), or perp-suffixed (``TAOUSDT.P``). The instruments
    table can store any of those depending on origin, so we try all
    plausible forms before short-circuiting the quant track.

    Args:
        symbol: Caller's input.

    Returns:
        Ordered, deduped list of forms.
    """
    if not symbol:
        return []
    s = symbol.strip().upper()
    out: List[str] = [s]
    base = s[:-2] if s.endswith(".P") else s
    if base != s and base not in out:
        out.append(base)
    if "/" not in base and len(base) > 3:
        for quote in ("USDT", "USDC", "BUSD", "USD"):
            if base.endswith(quote) and len(base) > len(quote):
                slash = base[: -len(quote)] + "/" + quote
                if slash not in out:
                    out.append(slash)
                break
    elif "/" in base:
        no_slash = base.replace("/", "")
        if no_slash not in out:
            out.append(no_slash)
        # Perp variant ``BTC/USDT.P`` -> ``BTCUSDT.P``
        if not s.endswith(".P"):
            perp = no_slash + ".P"
            if perp not in out:
                out.append(perp)
    return out


# ---------------------------------------------------------------------------
# Phase J — Memory recall before LLM call
# ---------------------------------------------------------------------------
async def _build_data_edge_block(symbol: Optional[str]) -> str:
    """Build a concise data-driven edge snapshot for ChartHacker's recall context.

    Queries recent competition_trades to surface:
      - Coin-specific win rates (only for the current symbol)
      - Best/worst time windows across all coins
      - Direction bias (long vs short)

    Returns a short string or empty string if not enough data.
    """
    try:
        from shared.utils.db import get_shared_pool
    except Exception:
        return ""

    pool = await get_shared_pool()
    lines: List[str] = []

    # Coin-specific edge for current symbol
    if symbol and symbol.strip():
        sym = symbol.strip().upper()
        # Try multiple forms since competition_trades may store perp-suffixed form
        _perp_re = __import__('re').compile(r":(USDT|USDC|BUSD|USD)$", __import__('re').IGNORECASE)
        stripped = _perp_re.sub("", sym)
        candidates = [stripped] if stripped != sym else [sym]
        if sym not in candidates:
            candidates.append(sym)
        try:
            async with pool.acquire() as conn:
                for cand in candidates:
                    row = await conn.fetchrow("""
                        SELECT COUNT(*) as t, COUNT(*) FILTER (WHERE pnl > 0) as w,
                               ROUND(SUM(pnl)::numeric, 2) as pnl
                        FROM competition_trades
                        WHERE exit_price IS NOT NULL AND UPPER(symbol) = $1
                    """, cand)
                    if row and row["t"] >= 3:
                        wr = round(100.0 * row["w"] / row["t"], 0)
                        lines.append(
                            f"COIN EDGE — {cand}: {row['t']} trades, {wr:.0f}% win, "
                            f"${row['pnl']:.0f} net. "
                            + (f"PREFER THIS COIN." if wr >= 70 and float(row['pnl']) > 0 else
                               f"AVOID THIS COIN." if wr <= 30 or float(row['pnl']) < -100 else
                               "NEUTRAL.")
                        )
                        break
        except Exception:
            pass

    # Time edge — best and worst hours
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT EXTRACT(HOUR FROM entered_at)::int as h,
                       COUNT(*) as t,
                       ROUND(100.0 * COUNT(*) FILTER (WHERE pnl > 0) / COUNT(*), 0) as wr,
                       ROUND(SUM(pnl)::numeric, 0) as pnl
                FROM competition_trades
                WHERE exit_price IS NOT NULL
                GROUP BY 1 HAVING COUNT(*) >= 4
                ORDER BY wr DESC
            """)
            if len(rows) >= 3:
                best = rows[0]
                worst = rows[-1]
                lines.append(
                    f"TIME EDGE — BEST: {int(best['h'])}:00 UTC ({int(best['wr'])}% win, ${int(best['pnl'])}). "
                    f"WORST: {int(worst['h'])}:00 UTC ({int(worst['wr'])}% win, ${int(worst['pnl'])}). "
                    f"Bias confidence toward BEST hours, skip during WORST hours."
                )
    except Exception:
        pass

    # Direction bias
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT
                  COUNT(*) FILTER (WHERE direction='long') as l,
                  COUNT(*) FILTER (WHERE direction='long' AND pnl>0) as lw,
                  COUNT(*) FILTER (WHERE direction='short') as s,
                  COUNT(*) FILTER (WHERE direction='short' AND pnl>0) as sw
                FROM competition_trades WHERE exit_price IS NOT NULL
            """)
            if row and row["l"] >= 10 and row["s"] >= 5:
                lwr = round(100.0 * row["lw"] / row["l"], 0)
                swr = round(100.0 * row["sw"] / row["s"], 0)
                better = "SHORTS" if swr > lwr else "LONGS"
                lines.append(
                    f"DIRECTION EDGE — Longs: {lwr:.0f}% ({row['l']} trades), "
                    f"Shorts: {swr:.0f}% ({row['s']} trades). "
                    f"PREFER {better}."
                )
    except Exception:
        pass

    if not lines:
        return ""

    return "DATA-DRIVEN MARKET EDGE (from real closed trades, update every cycle):\n" + "\n".join(lines) + "\n"


async def _load_source_color_rules(shared_pool, source: str) -> str:
    """Load source-specific color scheme rules for the vision LLM prompt.

    Looks up chart_prompts in system_config. First tries source-specific
    key (e.g. 'telegram/rose'), then source-wide ('telegram'), then 'default'.
    """
    try:
        # Build key candidates: source/subsource, source, default
        keys = []
        if source:
            keys.append(source)
            # Also try source/* — any subsource variant for this platform
            # (e.g. 'telegram' may have 'telegram/rose' or 'telegram/general')
        keys.append("default")

        async with shared_pool.acquire() as conn:
            # First try the most specific key (the source as passed)
            row = await conn.fetchrow(
                "SELECT config_value FROM system_config "
                "WHERE namespace = 'chart_prompts' AND config_key = $1",
                keys[0],
            )
            # If not found, try any key starting with source/
            if not row and len(keys) > 1:
                row = await conn.fetchrow(
                    "SELECT config_value FROM system_config "
                    "WHERE namespace = 'chart_prompts' "
                    "AND config_key LIKE $1 || '/%' "
                    "ORDER BY config_key LIMIT 1",
                    source,
                )
            # Fall back to default
            if not row:
                row = await conn.fetchrow(
                    "SELECT config_value FROM system_config "
                    "WHERE namespace = 'chart_prompts' AND config_key = 'default'",
                )
            if row:
                cfg = row["config_value"]
                if isinstance(cfg, str):
                    import json
                    cfg = json.loads(cfg)
                colors = cfg.get("colors", {})
                bull = colors.get("bull", "green")
                bear = colors.get("bear", "red")
                note = cfg.get("note", "")
                return (
                    f"⚠️ SOURCE COLOR OVERRIDE — Apply these colors for THE ONE RULE: "
                    f"Bullish/profit zone = {bull.upper()} (NOT green). "
                    f"Bearish/loss zone = {bear.upper()} (NOT red). "
                    f"Drawn rectangles/zones in these colors ARE valid position boxes. "
                    f"Treat {bull} touching entry above + {bear} touching entry below = LONG. "
                    f"{note}"
                )
    except Exception:
        pass
    return ""


async def _recall_relevant_memories(
    company: str,
    symbol: Optional[str],
    direction: Optional[str],
    trader_handle: Optional[str],
    limit_self: int = 4,
    limit_trader: int = 3,
) -> str:
    """Pull chart_hacker's relevant past lessons from mem0.

    Returns a formatted string ready to inject into the LLM prompt's
    ``{recall_context}`` slot. On any failure, returns an empty string —
    memory recall is best-effort and must never block analysis.

    Two streams are queried (same agent_id, different metadata.about tags):
      * self  — chart_hacker's own past calls and outcomes
      * trader — chart_hacker's accumulated observations about THIS trader

    Args:
        company: Company slug (e.g. 'rubicon').
        symbol: Trading pair if known.
        direction: 'long' / 'short' if known.
        trader_handle: The trader's handle for trader-specific recall.
        limit_self: Max self-memories to recall.
        limit_trader: Max trader-observation memories to recall.

    Returns:
        Formatted recall context string, or "" on failure / no results.
    """
    try:
        from shared.utils.mem0_config import get_memory
    except Exception:
        return ""

    query_parts = [p for p in (symbol, direction) if p]
    self_query = " ".join(query_parts) if query_parts else "chart pattern setup"
    trader_query = (
        f"{trader_handle} {symbol or ''} {direction or ''}".strip()
        if trader_handle
        else None
    )

    blocks: List[str] = []
    try:
        mem, agent_id = get_memory(company, "chart_hacker")
    except Exception as exc:
        logger.debug("mem0 recall: get_memory failed for %s/chart_hacker: %s", company, exc)
        return ""

    def _to_text(items: Any) -> List[str]:
        """Normalise mem0 search return into a list of memory strings."""
        if not items:
            return []
        # mem0 returns either {"results": [...]} or [...]
        if isinstance(items, dict):
            items = items.get("results") or items.get("memories") or []
        out: List[str] = []
        for it in items:
            if isinstance(it, dict):
                m = it.get("memory") or it.get("content") or it.get("text") or ""
                if m:
                    out.append(str(m).strip())
            elif isinstance(it, str):
                out.append(it.strip())
        return out[:8]  # hard cap

    # Self-recall — chart_hacker's own past lessons
    try:
        self_items = await asyncio.to_thread(
            mem.search,
            self_query,
            user_id=company,
            agent_id=agent_id,
            limit=limit_self,
        )
        self_lines = _to_text(self_items)
        if self_lines:
            blocks.append("Your own past lessons:\n- " + "\n- ".join(self_lines))
    except Exception as exc:
        logger.debug("mem0 self-recall failed: %s", exc)

    # Trader-observation recall — what you've learned about this trader
    if trader_query:
        try:
            trader_items = await asyncio.to_thread(
                mem.search,
                trader_query,
                user_id=company,
                agent_id=agent_id,
                limit=limit_trader,
            )
            trader_lines = _to_text(trader_items)
            if trader_lines:
                blocks.append(
                    f"Your past observations about {trader_handle}:\n- "
                    + "\n- ".join(trader_lines)
                )
        except Exception as exc:
            logger.debug("mem0 trader-recall failed: %s", exc)

    if not blocks:
        return ""

    # DATA-DRIVEN MARKET EDGE — coin/time biases from real closed trades.
    # Updated each call so ChartHacker always sees the latest patterns.
    try:
        edge_block = await _build_data_edge_block(symbol)
        if edge_block:
            blocks.insert(0, edge_block)
    except Exception:
        pass  # best-effort — never block analysis on edge data

    return (
        "PAST MEMORIES (use these to inform your analysis — your own past lessons "
        "and observations about this trader). Apply these as context, but base your "
        "actual extraction on what you see in the chart:\n\n"
        + "\n\n".join(blocks)
        + "\n"
    )


def _param_hash(
    model: str,
    primary_model: str,
    fallback_model: str,
    freshness_threshold: float,
    max_age_hours: float,
    extra: str = "",
) -> str:
    """Deterministic hash of the interpretation parameters for reproducibility."""
    payload = json.dumps(
        {
            "model": model,
            "primary_model": primary_model,
            "fallback_model": fallback_model,
            "freshness_threshold": freshness_threshold,
            "max_age_hours": max_age_hours,
            "extra": extra,
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
def _load_prompts(source: str = "", channel: str = "") -> Dict[str, Any]:
    """Load chart analysis prompts — source-aware with system_config override.

    Priority:
      1. system_config.chart_prompts.{source}/{channel}/prompt
      2. system_config.chart_prompts.{source}/prompt
      3. prompts/chart_analysis.json file
      4. Hardcoded fallback

    Args:
        source: Platform source (e.g. 'telegram', 'discord')
        channel: Channel name or ID (e.g. 'rose', '-1001755624949')
    """
    # Default — load from JSON file (sync, called at startup)
    prompt_path = Path(__file__).with_suffix("").parent / "prompts" / "chart_analysis.json"
    file_prompts = {}
    if prompt_path.exists():
        try:
            with open(prompt_path, "r", encoding="utf-8") as f:
                file_prompts = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load prompts from %s: %s", prompt_path, exc)

    default_ca = file_prompts.get("chart_analysis", {}) if file_prompts else {}
    if not default_ca:
        default_ca = {
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

    return {
        "chart_analysis": default_ca,
        "_source": source,
        "_channel": channel,
    }


def _build_prompt_result(row, source: str, channel: str) -> Dict[str, Any]:
    """Build the prompt result dict from a prompt_versions row."""
    return {
        "chart_analysis": {
            "system_prompt": row["system"],
            "user_prompt_template": row["body"],
        },
        "_source": source,
        "_channel": channel,
        "_prompt_key": f"db:{row['version']}",
        "_prompt_source": "db",
        "_prompt_hash": row["prompt_hash"],
    }


async def _load_prompts_async(
    shared_pool, source: str = "", channel: str = ""
) -> Dict[str, Any]:
    """Load chart_analysis prompt from DB (prompt_versions), falling back to file.

    Priority:
      1. prompt_versions.name='chart_analysis', version matching source/channel
      2. system_config.chart_prompts (legacy — being phased out)
      3. prompts/chart_analysis.json file (hardcoded fallback)

    Returns dict with keys: chart_analysis (system_prompt, user_prompt_template),
    _source, _channel, _prompt_key, _prompt_source ('db'|'file'|'config').
    """
    if not shared_pool:
        result = _load_prompts(source, channel)
        result["_prompt_source"] = "file"
        return result

    try:
        async with shared_pool.acquire() as conn:
# 1. Try prompt_versions — source-specific then newest default
            version_keys = []
            if source and channel:
                version_keys.append(f"{source}-{channel}-v1")
            if source:
                version_keys.append(f"{source}-v1")
            # Wildcard: find any source-specific prompt for this platform
            source_wildcard = source  # 'telegram', 'discord', etc.

            # Try exact matches first
            for vk in version_keys:
                try:
                    row = await conn.fetchrow(
                        "SELECT system, body, version, prompt_hash FROM prompt_versions "
                        "WHERE name = 'chart_analysis' AND version = $1 AND source = 'db'",
                        vk,
                    )
                except Exception as fetch_exc:
                    logger.warning("prompt_versions fetch failed for %s: %s", vk, fetch_exc)
                    continue
                if row:
                    logger.info("Loaded prompt from DB: chart_analysis/%s hash=%s",
                                row["version"], row["prompt_hash"])
                    return _build_prompt_result(row, source, channel)

            # No exact match — try ILIKE for platform name in version
            if source_wildcard:
                try:
                    row = await conn.fetchrow(
                        "SELECT system, body, version, prompt_hash FROM prompt_versions "
                        "WHERE name = 'chart_analysis' AND source = 'db' "
                        "AND version ILIKE $1 "
                        "ORDER BY created_at DESC LIMIT 1",
                        f"%{source_wildcard}%",
                    )
                except Exception as fetch_exc:
                    logger.warning("prompt_versions ILIKE failed: %s", fetch_exc)
                    row = None
                if row:
                    logger.info("Loaded source prompt via ILIKE: chart_analysis/%s", row["version"])
                    return _build_prompt_result(row, source, channel)

            # Fallback: newest chart_analysis entry
            try:
                row = await conn.fetchrow(
                    "SELECT system, body, version, prompt_hash FROM prompt_versions "
                    "WHERE name = 'chart_analysis' AND source = 'db' "
                    "ORDER BY created_at DESC LIMIT 1",
                )
            except Exception as fetch_exc:
                logger.warning("prompt_versions newest fetch failed: %s", fetch_exc)
                row = None
            if row:
                logger.info("Loaded newest prompt from DB: chart_analysis/%s", row["version"])
                return _build_prompt_result(row, source, channel)

            # 2. Legacy system_config fallback (will be removed in Phase 4)
            for key in (
                f"{source}/{channel}/prompt" if channel else None,
                f"{source}/prompt",
            ):
                if key is None:
                    continue
                row = await conn.fetchrow(
                    "SELECT config_value FROM system_config "
                    "WHERE namespace = 'chart_prompts' AND config_key = $1",
                    key,
                )
                if row:
                    cfg = row["config_value"]
                    if isinstance(cfg, str):
                        cfg = json.loads(cfg)
                    sp = cfg.get("system_prompt", "")
                    ut = cfg.get("user_prompt_template", "")
                    if sp and ut:
                        logger.info("Loaded prompt from system_config: %s", key)
                        return {
                            "chart_analysis": {
                                "system_prompt": sp,
                                "user_prompt_template": ut,
                            },
                            "_source": source,
                            "_channel": channel,
                            "_prompt_key": key,
                            "_prompt_source": "config",
                        }
    except Exception as exc:
        logger.warning("Failed to load prompt from DB: %s", exc)

    # 3. JSON file fallback
    result = _load_prompts(source, channel)
    result["_prompt_source"] = "file"
    logger.info("Using file prompt fallback (no DB prompt found for source=%s)", source)
    return result




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
    # Round 10: per-call resolution, so dropdown changes in the dashboard
    # take effect at the next prefilter call (within the 60s cache window).
    model = await _runtime_get_model(SLOT_PREFILTER)

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
    recall_context: str = "",
    news_source: str = "",
    channel_name: str = "",
    shared_pool=None,
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

    prompts = (await _load_prompts_async(shared_pool, news_source, channel_name)).get("chart_analysis", {})
    system_prompt = prompts.get("system_prompt", "")
    if not system_prompt:
        system_prompt = (
            "You are ChartHacker, a crypto and financial chart analyst. Analyze the provided chart image. "
            "Identify the trading instrument from the chart title or axis labels. "
            "Respond ONLY with a JSON object containing:\n"
            "  instrument: the trading pair shown (e.g. 'BTC/USDT', 'ETH/USDT') or 'UNKNOWN'\n"
            "  direction: 'long' | 'short' | 'neutral' | 'unclear'\n"
            "  confidence: 0.0-1.0\n"
            "  reasoning: brief text (max 200 chars)\n"
            "  levels: { entry, stop_loss, take_profit } as strings or null\n"
            "No markdown, no prose outside the JSON."
        )
        logger.warning("chart_analysis.json system_prompt missing — using hardcoded fallback")
    user_template = prompts.get("user_prompt_template", "Analyze this chart. Identify the trading instrument from the chart itself.\nNews context: {context}\n\n{recall_context}")
    # Render the prompt with whichever placeholders the template uses. We
    # support {symbol}, {context}, {recall_context} — all optional.
    try:
        user_text = user_template.format(
            symbol=instrument_symbol or "UNKNOWN",
            context=news_context[:500],
            recall_context=recall_context or "",
        )
    except KeyError:
        # Older templates may only know {context}; fall back gracefully
        user_text = (
            user_template.replace("{context}", news_context[:500])
            .replace("{symbol}", instrument_symbol or "UNKNOWN")
        )
        if recall_context:
            user_text += "\n\n" + recall_context

    # Phase 3: compute prompt metadata for audit trail
    prompt_version = compute_prompt_version(_load_prompts())
    prompt_hash = compute_prompt_hash(system_prompt, user_text)

    # Round 10: resolve primary/fallback fresh per call so dashboard
    # dropdown changes propagate within the model_config cache window (60s).
    runtime_primary = await _runtime_get_model(SLOT_PRIMARY)
    runtime_fallback = await _runtime_get_model(SLOT_FALLBACK)
    models = [runtime_primary, runtime_fallback]
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
                    operation="vision_primary" if model == runtime_primary else "vision_fallback",
                )
                parsed = _parse_llm_json(resp["content"])
                # Cost is now logged automatically by the gateway via api_cost_log.
                # We keep a local zero placeholder; the true cost is in the DB.
                cost = 0.0

                # Phase J — Lens prompt returns trader_trades[] and
                # chart_hacker_trades[] arrays. Derive the legacy primary
                # direction/confidence/levels from the first available trade
                # so old downstream callers (audit columns, MemU broadcast
                # gating) keep working unchanged.
                trader_trades       = parsed.get("trader_trades") or []
                chart_hacker_trades = parsed.get("chart_hacker_trades") or []
                chart_analysis      = parsed.get("chart_analysis") or {}

                # Coerce to lists in case the LLM returned a dict by mistake
                if not isinstance(trader_trades, list):
                    trader_trades = []
                if not isinstance(chart_hacker_trades, list):
                    chart_hacker_trades = []

                # Pick the dominant trade for legacy fields. Trader's trade
                # wins if present and has a clear direction; otherwise
                # fall back to chart_hacker's first trade. If neither, the
                # legacy fields stay at their defaults ("unclear", 0.0, {}).
                primary = None
                for t in trader_trades:
                    if isinstance(t, dict) and t.get("direction") in ("long", "short"):
                        primary = t
                        break
                if primary is None:
                    for t in chart_hacker_trades:
                        if isinstance(t, dict) and t.get("direction") in ("long", "short"):
                            primary = t
                            break

                if primary is not None:
                    direction = str(primary.get("direction", "unclear")).lower()
                    confidence_raw = primary.get("confidence",
                                                 primary.get("trader_confidence", 0.0))
                    try:
                        legacy_confidence = float(confidence_raw)
                    except (TypeError, ValueError):
                        legacy_confidence = 0.0
                    legacy_levels = {
                        "entry":       primary.get("entry"),
                        "stop_loss":   primary.get("stop_loss"),
                        # Bug Hunter 2 §10.2 — accept either the numbered key
                        # (``tp1``) or the singular fallback (``take_profit``).
                        # Without this, LLM payloads that emit only
                        # ``take_profit`` silently lose the level here AND in
                        # llm_levels, even though the rest of the pipeline
                        # tolerates singulars.
                        "take_profit": primary.get("tp1") or primary.get("take_profit"),
                    }
                    legacy_reasoning = str(
                        primary.get("rationale")
                        or primary.get("trader_rationale")
                        or ""
                    )[:500]
                else:
                    # No trades extracted from either side — chart was
                    # commentary, meme, or unreadable.
                    direction = str(parsed.get("direction", "unclear")).lower()
                    legacy_confidence = float(parsed.get("confidence", 0.0) or 0.0)
                    legacy_levels = parsed.get("levels", {}) or {}
                    legacy_reasoning = str(parsed.get("reasoning", ""))

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
                    extra={"operation": "vision_primary" if model == runtime_primary else "vision_fallback", "correlation_id": correlation_id},
                )
                response_payload = build_response_payload(
                    model_resolved=resp.get("model", model),
                    content=resp.get("content", ""),
                    usage=resp.get("usage"),
                    extra={"operation": "vision_primary" if model == runtime_primary else "vision_fallback", "correlation_id": correlation_id},
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

                # ai_agreement may come back as float or string
                try:
                    ai_agreement_val = float(parsed.get("ai_agreement_with_trader", 0.0) or 0.0)
                except (TypeError, ValueError):
                    ai_agreement_val = 0.0

                return LlmResult(
                    direction=direction,
                    confidence=legacy_confidence,
                    reasoning=legacy_reasoning,
                    levels=legacy_levels,
                    instrument=str(parsed.get("instrument", "")).strip(),
                    timeframe=str(parsed.get("timeframe", "")).strip(),
                    trader_trades=trader_trades,
                    chart_hacker_trades=chart_hacker_trades,
                    chart_analysis=chart_analysis,
                    ai_agreement=ai_agreement_val,
                    ai_comment=str(parsed.get("ai_comment_on_trader", "")).strip()[:1000],
                    trader_market_view=str(parsed.get("trader_market_view", "")).strip()[:500],
                    chart_hacker_market_view=str(parsed.get("chart_hacker_market_view", "")).strip()[:500],
                    model_used=model,
                    model_resolved=resp.get("model", model),
                    cost_usd=cost,
                    raw_response=resp["content"],
                    request_path=req_path,
                    response_path=resp_path,
                    prompt_version=prompt_version,
                    prompt_hash=prompt_hash,
                    prompt_source=prompts.get("_prompt_source", ""),
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
    """Run the quant indicator track: read recent candles, compute lightweight signals.

    Reads the last 100 1m candles for the instrument, computes RSI(14), EMA(20/50),
    ATR(14), and Bollinger(20,2). Derives a directional score from the ensemble.

    If the instrument is not registered in ``public.instruments`` under the given
    symbol form, alternate symbol forms are tried (slash/no-slash, ``.P`` perp
    suffix). If no candles can be located at all, the function falls back to a
    CCXT live-price probe and returns a degraded :class:`QuantResult` whose
    ``indicators`` carry just ``current_price`` and ``price_source`` so the
    downstream interpretation can still stamp a price.

    Args:
        shared_pool: DatabasePool for the shared database (instruments, candles).
        company_pool: DatabasePool for the company database (not used for reads here).
        instrument_symbol: Trading pair symbol (e.g., 'BTCUSDT').
        exchange: Exchange name (e.g., 'bybit').
        freshness_threshold: Max age in seconds for candle data.

    Returns:
        QuantResult with direction, confidence, and indicator snapshot.
    """
    if not instrument_symbol:
        logger.warning("Quant track: instrument_symbol is None or empty")
        return QuantResult(direction="unclear", confidence=0.0, indicators={})

    exch = (exchange or "").strip().lower() or "bybit"
    candidates = _quant_symbol_candidates(instrument_symbol)

    # Resolve instrument_id from symbol + exchange (shared DB) — try alternates.
    instrument_id: Optional[int] = None
    resolved_symbol: Optional[str] = None
    for cand in candidates:
        try:
            row = await shared_pool.fetch_one(
                "SELECT id FROM public.instruments WHERE symbol = $1 AND exchange = $2",
                (cand, exch),
            )
        except Exception as exc:
            logger.warning(
                "Quant track: instruments lookup failed for %s@%s: %s",
                cand, exch, exc,
            )
            row = None
        if row:
            instrument_id = int(row["id"])
            resolved_symbol = cand
            break

    if instrument_id is None:
        logger.info(
            "Quant track: no instrument row for %s@%s (tried %s); "
            "attempting CCXT live-price fallback",
            instrument_symbol, exch, candidates,
        )
        return await _quant_degraded_from_ccxt(instrument_symbol, exch)

    # Fetch last 100 1m candles (shared DB)
    try:
        candles = await shared_pool.fetch_all(
            "SELECT timestamp, open, high, low, close, volume "
            "FROM public.candles "
            "WHERE instrument_id = $1 AND source = $2 AND timeframe = '1m' "
            "ORDER BY timestamp DESC LIMIT 100",
            (instrument_id, exch),
        )
    except Exception as exc:
        logger.warning(
            "Quant track: candles query failed for %s@%s: %s",
            resolved_symbol, exch, exc,
        )
        candles = []

    if len(candles) < 50:
        logger.info(
            "Quant track: only %d candles for %s@%s, need >=50; "
            "attempting CCXT live-price fallback",
            len(candles), resolved_symbol, exch,
        )
        return await _quant_degraded_from_ccxt(instrument_symbol, exch)

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
        "current_price": round(last_close, 4),
        "price_source": "candles_1m",
        "score": round(score, 4),
        "reasons": reasons,
    }

    return QuantResult(direction=direction, confidence=confidence, indicators=indicators)


async def _quant_degraded_from_ccxt(
    instrument_symbol: str,
    exchange: str,
) -> QuantResult:
    """Return a degraded :class:`QuantResult` based on a CCXT live-price probe.

    Used by :func:`run_quant_track` when ``public.instruments`` has no row for
    the requested symbol (or no recent candles). Tries each candidate symbol
    form against CCXT in order, and on the first successful probe returns a
    QuantResult with ``direction='unclear'``, ``confidence=0.0`` and a small
    ``indicators`` payload containing the live price so downstream code can
    still stamp ``current_price`` on the interpretation row.

    Args:
        instrument_symbol: Original symbol requested by the caller.
        exchange: Exchange name to probe.

    Returns:
        A :class:`QuantResult`. ``indicators`` is empty if every probe failed.
    """
    if not instrument_symbol:
        return QuantResult(direction="unclear", confidence=0.0, indicators={})
    candidates = _quant_symbol_candidates(instrument_symbol)
    exch = (exchange or "bybit").strip().lower() or "bybit"
    for cand in candidates:
        try:
            probe = await _ccxt_live_price(cand, exch)
        except Exception as exc:
            logger.debug(
                "Quant degraded probe error %s@%s: %s", cand, exch, exc,
            )
            probe = None
        if probe is None:
            continue
        price, resolved_sym, ts_ms = probe
        logger.info(
            "Quant degraded fallback: live price %s for %s@%s (ts=%s)",
            price, resolved_sym, exch, ts_ms,
        )
        return QuantResult(
            direction="unclear",
            confidence=0.0,
            indicators={
                "current_price": float(price),
                "price_source": "ccxt_live",
                "live_symbol": resolved_sym,
                "live_exchange": exch,
                "live_ts_ms": int(ts_ms) if ts_ms else 0,
                "degraded": True,
            },
        )
    logger.info(
        "Quant degraded fallback: no CCXT live price for %s on %s (tried %s)",
        instrument_symbol, exch, candidates,
    )
    return QuantResult(direction="unclear", confidence=0.0, indicators={})


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
    # Slice 4 — only feed `image` media into the vision pipeline. Videos (and
    # any other non-image media_type) crashed the vision LLM with
    # "INVALID_ARGUMENT: Provided image is not valid". Non-image rows are
    # excluded here and handled separately by `cleanup_unsupported_media()`,
    # which transitions them to processing_status='skipped_unsupported_media'.
    #
    # Bug C3 fix (atomic claim — Code Analyzer 2 §1.3):
    #   Previously this function ran a plain SELECT and never marked the chosen
    #   rows as ``analyzing``. Two concurrent InterpretationService workers
    #   (or two ticks running on top of each other under back-pressure) could
    #   both pick the same media_id and both run a paid LLM call against it —
    #   doubling cost and creating duplicate `tracked_positions`. We now use
    #   `FOR UPDATE OF m SKIP LOCKED` inside a CTE plus an UPDATE ... RETURNING
    #   so each candidate row is locked, transitioned to
    #   ``processing_status='analyzing'``, and returned to exactly one worker.
    #   A separate sweep (``recover_stale_analyzing``) requeues rows that get
    #   stuck in ``analyzing`` if a worker dies mid-process.
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    sql = (
        "WITH claimed AS ("
        "  SELECT m.id "
        "  FROM public.media_items m "
        "  JOIN public.news_items n ON n.id = m.news_item_id "
        "  WHERE (m.processing_status = 'downloaded' "
        "         OR (m.processing_status = 'pending' AND m.source_url IS NOT NULL)) "
        "    AND m.media_type = 'image' "
        "    AND n.collected_at > $1 "
        "  ORDER BY n.collected_at ASC "
        "  LIMIT $2 "
        "  FOR UPDATE OF m SKIP LOCKED"
        "), updated AS ("
        "  UPDATE public.media_items "
        "  SET processing_status = 'analyzing', "
        "      processed_at      = NOW() "
        "  WHERE id IN (SELECT id FROM claimed) "
        "  RETURNING id, processed_at AS claim_ts"
        ") "
        "SELECT "
        "  m.id AS media_id, "
        "  m.news_item_id, "
        "  m.local_path, "
        "  m.source_url, "
        "  m.mime_type, "
        "  m.media_type, "
        "  m.processing_status, "
        "  (SELECT claim_ts FROM updated u WHERE u.id = m.id) AS claim_ts, "
        "  n.headline, "
        "  n.content, "
        "  n.source_id, "
        "  n.instruments, "
        "  n.author, "
        "  n.channel_name, "
        "  n.collected_at, "
        "  n.source, "
        "  n.context_window "
        "FROM public.media_items m "
        "JOIN public.news_items n ON n.id = m.news_item_id "
        "WHERE m.id IN (SELECT id FROM updated) "
        "ORDER BY n.collected_at ASC"
    )
    async with shared_pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(sql, cutoff, batch_size)
    return [dict(r) for r in rows]


async def recover_stale_analyzing(
    shared_pool: DatabasePool,
    stale_minutes: int = 60,
) -> int:
    """Reset media rows stuck in ``processing_status='analyzing'`` back to a
    re-claimable state if no worker has touched them within ``stale_minutes``.

    Bug C3 companion: a worker that crashes mid-process (OOM, sigkill) leaves
    its claimed rows pinned at ``analyzing`` forever. This sweep guards
    against silent queue starvation by transitioning rows older than the
    threshold back to their original state — ``downloaded`` if a local file
    is present, otherwise ``pending`` (CDN-hosted source_url path).

    Bug H fix (2026-05-24 second-round audit): the previous default of 15
    minutes was below the p99 latency of vision-LLM calls under load. A
    healthy worker that took 16 minutes to finish would lose its claim
    mid-flight, a second worker would re-claim, both would pay the LLM
    bill (the duplicate-trade write is blocked by DB unique constraints,
    but cost is doubled). Raised the default to 60 minutes — well above
    any observed worker latency — so this sweep only fires for genuinely
    crashed workers.

    Returns the number of rows recovered (0 in the steady-state).
    """
    sql = (
        "UPDATE public.media_items "
        "SET processing_status = CASE "
        "      WHEN local_path IS NOT NULL AND local_path <> '' THEN 'downloaded' "
        "      WHEN source_url IS NOT NULL AND source_url <> ''  THEN 'pending'    "
        "      ELSE 'failed' "
        "    END, "
        "    processing_error = COALESCE(processing_error, '') || "
        "                       ' [recover_stale_analyzing]', "
        "    processed_at = NOW() "
        "WHERE processing_status = 'analyzing' "
        "  AND processed_at < NOW() - make_interval(mins => $1)"
    )
    try:
        async with shared_pool.acquire() as conn:
            result = await conn.execute(sql, stale_minutes)
        try:
            count = int(str(result).rsplit(" ", 1)[-1])
        except (ValueError, IndexError):
            count = 0
        if count:
            logger.info(
                "recover_stale_analyzing: requeued %d media rows stuck in 'analyzing' >%dmin",
                count,
                stale_minutes,
            )
        return count
    except Exception as exc:
        logger.warning("recover_stale_analyzing: query failed: %s", exc)
        return 0


async def cleanup_unsupported_media(
    shared_pool: DatabasePool,
    max_age_hours: float,
) -> int:
    """Mark non-image pending/downloaded media as ``skipped_unsupported_media``.

    Videos and other non-image media types cannot be processed by the vision
    LLM. Leaving them at status ``pending``/``downloaded`` causes them to
    accumulate forever and pollute the queue view. This routine sweeps them
    into a terminal status so the queue stays small.

    Args:
        shared_pool: Shared Postgres pool.
        max_age_hours: Only consider rows whose parent news_item is younger
            than this. Older rows are not touched here — the daemon's
            existing age cutoff already excludes them from work selection.

    Returns:
        Number of media rows transitioned to ``skipped_unsupported_media``.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    sql = (
        "UPDATE public.media_items AS m "
        "SET processing_status = 'skipped_unsupported_media', "
        "    processing_error  = COALESCE(m.processing_error, '') || "
        "                        'slice4: media_type != image', "
        "    processed_at      = NOW() "
        "FROM public.news_items AS n "
        "WHERE n.id = m.news_item_id "
        "  AND m.media_type IS DISTINCT FROM 'image' "
        "  AND m.processing_status IN ('pending','downloaded') "
        "  AND n.collected_at > $1"
    )
    try:
        async with shared_pool.acquire() as conn:
            result = await conn.execute(sql, cutoff)
        # asyncpg returns a string like "UPDATE 7"; parse the trailing int.
        try:
            count = int(str(result).rsplit(" ", 1)[-1])
        except (ValueError, IndexError):
            count = 0
        if count:
            logger.info(
                "Slice 4 hygiene: marked %d non-image media rows as "
                "skipped_unsupported_media",
                count,
            )
        return count
    except Exception as exc:
        logger.warning("cleanup_unsupported_media failed: %s", exc)
        return 0


async def cleanup_expired_local_media(
    shared_pool: DatabasePool,
    retention_days: float = 7.0,
) -> int:
    """Scan and delete local media files older than retention_days, updating
    local_path to NULL so the frontend falls back to the original source_url.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    sql_fetch = (
        "SELECT id, local_path FROM public.media_items "
        "WHERE local_path IS NOT NULL "
        "  AND processing_status NOT IN ('analyzing', 'downloading', 'pending', 'downloaded') "
        "  AND created_at < $1 "
        "LIMIT 100"
    )
    sql_update = (
        "UPDATE public.media_items "
        "SET local_path = NULL "
        "WHERE id = $1"
    )
    deleted_count = 0
    try:
        async with shared_pool.acquire() as conn:
            rows = await conn.fetch(sql_fetch, cutoff)
            if not rows:
                return 0
                
            for r in rows:
                mid = r["id"]
                path_str = r["local_path"]
                if path_str:
                    p = Path(path_str)
                    if p.exists() and p.is_file():
                        try:
                            p.unlink()
                            deleted_count += 1
                        except Exception as exc:
                            logger.warning("Failed to delete local file %s for media_id=%s: %s", path_str, mid, exc)
                
                # Update DB row to set local_path to NULL
                await conn.execute(sql_update, mid)
                
        if deleted_count:
            logger.info("Local media cleanup: unlinked %d files older than %.1f days", deleted_count, retention_days)
        return deleted_count
    except Exception as exc:
        logger.warning("cleanup_expired_local_media failed: %s", exc)
        return 0


async def cleanup_stale_pending_news(
    shared_pool: DatabasePool,
    stale_after_hours: float = 6.0,
) -> int:
    """Drain stale text-only news with no media and no signal from the queue.

    News items that arrive with neither media nor a signal interpretation
    after ``stale_after_hours`` are not going to produce a signal — most are
    chat noise. We mark them ``skipped_no_content`` on
    ``news_items.enrichment_status`` so the dashboard stops counting them
    as "pending".

    Args:
        shared_pool: Shared Postgres pool.
        stale_after_hours: Age threshold (hours) past which a pending,
            mediumless, signalless news_item is dropped.

    Returns:
        Number of news_items transitioned to ``skipped_no_content``.
    """
    if stale_after_hours <= 0:
        return 0
    sql = (
        "UPDATE public.news_items AS n "
        "SET enrichment_status = 'skipped_no_content', "
        "    enriched_at       = NOW() "
        "WHERE n.collected_at < NOW() - ($1 || ' hours')::interval "
        "  AND n.has_media = FALSE "
        "  AND n.enrichment_status = 'pending' "
        "  AND NOT EXISTS ( "
        "    SELECT 1 FROM public.signal_interpretations si "
        "     WHERE si.news_item_id = n.id "
        "  )"
    )
    try:
        async with shared_pool.acquire() as conn:
            result = await conn.execute(sql, str(stale_after_hours))
        try:
            count = int(str(result).rsplit(" ", 1)[-1])
        except (ValueError, IndexError):
            count = 0
        if count:
            logger.info(
                "Slice 4 hygiene: drained %d stale text-only news_items as "
                "skipped_no_content (older than %.1fh, no media, no signal)",
                count, stale_after_hours,
            )
        return count
    except Exception as exc:
        logger.warning("cleanup_stale_pending_news failed: %s", exc)
        return 0


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


async def is_valid_db_instrument(shared_pool: DatabasePool, symbol: str) -> bool:
    """Check if a symbol (or any of its candidate forms) exists in public.instruments."""
    if not symbol or symbol == "UNKNOWN":
        return False
    candidates = _quant_symbol_candidates(symbol)
    for cand in candidates:
        try:
            row = await shared_pool.fetch_one(
                "SELECT id FROM public.instruments WHERE symbol = $1",
                (cand,),
            )
            if row:
                return True
        except Exception as exc:
            logger.warning("is_valid_db_instrument check failed for %s: %s", cand, exc)
    return False


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


def _coerce_level(value: Any) -> Optional[float]:
    """Coerce a heterogeneous LLM-supplied level value to a float.

    The LLM emits levels as either JSON numbers (``42500.5``) or JSON
    strings (``"42,500.50"``, ``"$42500"``, ``"~42.5k"``). We strip
    everything but digits/sign/decimal point and parse. Anything that
    cannot be expressed as a finite positive number returns ``None``
    so the column stays NULL rather than poisoning downstream consumers.

    Args:
        value: Raw value from the LLM levels dict — number, string, or None.

    Returns:
        ``float`` if a finite positive number was extracted, else ``None``.
    """
    try:
        if value is None or value == "":
            return None
        if isinstance(value, (int, float)):
            num = float(value)
        elif isinstance(value, str):
            cleaned = _re.sub(r"[^0-9.\-]", "", value)
            if not cleaned or cleaned in ("-", ".", "-."):
                return None
            num = float(cleaned)
        else:
            return None
        if num != num or num == float("inf") or num == float("-inf"):
            return None
        if num <= 0:
            return None
        return num
    except (TypeError, ValueError):
        return None


def _flatten_llm_levels(levels: Optional[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    """Pull entry/SL/TP1..6 out of an LLM levels dict into dedicated floats.

    The Phase Z migration added dedicated NUMERIC columns mirrored from
    the canonical ``llm_levels`` JSONB. This helper extracts each known
    key, coerces it via :func:`_coerce_level`, and returns a stable
    six-TP shape suitable for direct binding to asyncpg parameters.

    Args:
        levels: The ``llm_levels`` dict from the LLM (may be ``None``).

    Returns:
        Dict with keys ``entry_price``, ``stop_loss``,
        ``take_profit_1`` .. ``take_profit_6`` — each ``Optional[float]``.
    """
    out: Dict[str, Optional[float]] = {
        "entry_price": None,
        "stop_loss": None,
        "take_profit_1": None,
        "take_profit_2": None,
        "take_profit_3": None,
        "take_profit_4": None,
        "take_profit_5": None,
        "take_profit_6": None,
    }
    if not isinstance(levels, dict):
        return out
    out["entry_price"] = _coerce_level(levels.get("entry"))
    out["stop_loss"] = _coerce_level(levels.get("stop_loss"))
    # The chart-analysis prompt asks the LLM for a SINGULAR `take_profit`
    # (see prompts/chart_analysis.json + system_prompt strings). However we
    # store TP1..TP6 separately in the DB. Map the singular key into TP1
    # first (so vision-only signals don't silently drop their take-profit),
    # then allow numbered TP1..TP6 to override / supplement when present.
    singular_tp = _coerce_level(
        levels.get("take_profit") if "take_profit" in levels else None
    )
    if singular_tp is not None:
        out["take_profit_1"] = singular_tp
    for n in range(1, 7):
        numbered = _coerce_level(levels.get(f"take_profit_{n}"))
        if numbered is not None:
            out[f"take_profit_{n}"] = numbered
    return out


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
    prompt_source: str = "",
    prompt_hash: str = "",
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

    # Phase Z — denormalised level mirrors. Read from the canonical
    # ``llm.levels`` JSONB and bind to dedicated NUMERIC columns so the
    # Signals dashboard / live-vs-signal trackers don't have to parse
    # JSON on every read. JSONB stays the source of truth for new keys.
    levels_flat = _flatten_llm_levels(llm.levels if llm else None)

    # Bug H8 — derive pattern_tags / setup_tags from chart_analysis JSON.
    # The vision LLM emits `chart_patterns` (array of strings) and `key_levels`
    # (array of {type, price, method}) inside `chart_analysis`. Phase J never
    # surfaced these into dedicated columns, so dashboard chips/queries that
    # read pattern_tags / setup_tags were always empty even when the LLM had
    # extracted them. We mirror them here so the data is queryable without a
    # JSONB scan.
    chart_analysis_obj = llm.chart_analysis if llm and llm.chart_analysis else {}
    raw_patterns = chart_analysis_obj.get("chart_patterns") if isinstance(chart_analysis_obj, dict) else None
    pattern_tags_list: List[str] = []
    if isinstance(raw_patterns, list):
        seen_p: set = set()
        for p in raw_patterns:
            if not isinstance(p, str):
                continue
            tag = p.strip().lower()
            if tag and tag not in seen_p:
                seen_p.add(tag)
                pattern_tags_list.append(tag)
    raw_levels = chart_analysis_obj.get("key_levels") if isinstance(chart_analysis_obj, dict) else None
    setup_tags_list: List[str] = []
    if isinstance(raw_levels, list):
        seen_s: set = set()
        for lvl in raw_levels:
            if not isinstance(lvl, dict):
                continue
            tag_type = lvl.get("type")
            if not isinstance(tag_type, str):
                continue
            tag = tag_type.strip().lower()
            if tag and tag not in seen_s:
                seen_s.add(tag)
                setup_tags_list.append(tag)

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
        # prompt provenance
        "  prompt_source, "
        # Phase J — dual-extraction columns
        "  timeframe, chart_analysis, trader_trades, chart_hacker_trades, "
        "  ai_agreement_score, ai_comment, "
        # Phase Z — denormalised level columns
        "  entry_price, stop_loss, "
        "  take_profit_1, take_profit_2, take_profit_3, "
        "  take_profit_4, take_profit_5, take_profit_6, "
        # Bug H8 — denormalised pattern/setup tags from chart_analysis
        "  pattern_tags, setup_tags, "
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
        # Phase J params $32..$37
        "  $32, $33::jsonb, $34::jsonb, $35::jsonb, "
        "  $36, $37, "
        # Phase Z params $38..$45
        "  $38, $39, "
        "  $40, $41, $42, "
        "  $43, $44, $45, "
        # Bug H8 params $46, $47
        "  $46::jsonb, $47::jsonb, "
        # prompt provenance param $48
        "  $48, "
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
        # Phase J — dual-extraction payload
        (llm.timeframe if llm and llm.timeframe else None),
        json.dumps(chart_analysis_obj),
        json.dumps(llm.trader_trades if llm and llm.trader_trades else []),
        json.dumps(llm.chart_hacker_trades if llm and llm.chart_hacker_trades else []),
        round(llm.ai_agreement, 2) if llm and llm.ai_agreement else None,
        (llm.ai_comment if llm and llm.ai_comment else None),
        # Phase Z — denormalised level mirrors ($38..$45)
        levels_flat["entry_price"],
        levels_flat["stop_loss"],
        levels_flat["take_profit_1"],
        levels_flat["take_profit_2"],
        levels_flat["take_profit_3"],
        levels_flat["take_profit_4"],
        levels_flat["take_profit_5"],
        levels_flat["take_profit_6"],
        # Bug H8 — pattern_tags ($46), setup_tags ($47)
        json.dumps(pattern_tags_list),
        json.dumps(setup_tags_list),
        # prompt provenance ($48)
        prompt_source or "",
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


# ---------------------------------------------------------------------------
# Symbol extraction from text — finds tickers like $BTC, BTCUSDT, ETH/USDT
# ---------------------------------------------------------------------------
_FOREX_3LETTER = "EUR|GBP|JPY|CHF|CAD|AUD|NZD|USD"
_INDEX_PATTERN = _re.compile(r"\b(NAS|SPX|US30|US100|NQ|DJI|DAX|FTSE)\d*\b")
_TICKER_PATTERNS = [
    _re.compile(r"\b([A-Z]{2,6})/([A-Z]{3,5})\b"),                # BTC/USDT, ETH/USD
    # Forex pair (no slash) — narrow to the eight majors so USDJPY,
    # EURUSD, GBPCHF etc. resolve without colliding with crypto-quote form.
    _re.compile(rf"\b({_FOREX_3LETTER})({_FOREX_3LETTER})\b"),
    _re.compile(r"\b([A-Z]{2,6})(USDT|USD|BUSD)\b"),               # BTCUSDT, ETHBUSD
    _re.compile(r"\$([A-Z]{2,6})\b"),                                # $BTC, $SOL
    _re.compile(r"\b(XAU|XAG)(USD)\b"),                              # XAUUSD, XAGUSD
]

# Common crypto/forex symbols we recognise. Forex bases (EUR/GBP/JPY/etc.)
# are included so EURUSD, GBPUSD, USDJPY etc. resolve via the dedicated
# forex pattern — without them they previously fell through to the
# LLM-vision fallback and got tagged BTC by default.
_KNOWN_BASES = {
    # Crypto
    "BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "DOT", "AVAX", "LINK",
    "MATIC", "BNB", "LTC", "UNI", "AAVE", "NEAR", "APT", "SUI", "FTM",
    "OP", "ARB", "INJ", "TIA", "SEI", "JUP", "WIF", "PEPE", "BONK",
    # Wrapped / staked / stablecoins (treated as crypto bases too)
    "WBTC", "WETH", "STETH", "USDC", "DAI", "TUSD",
    # Forex majors and minors
    "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "USD",
    # Metals + selected equities. Indices live in _INDEX_PATTERN — they
    # use their own quote convention (XAU/USD, NAS100 etc.) and shouldn't
    # be USDT-defaulted by the bare-base path.
    "XAU", "XAG", "AAPL", "TSLA", "MSFT", "NVDA", "AMD",
}

# Bare-word alias map for headlines that mention an instrument by name
# without a ticker decoration ("Gold breaks out", "ETH dropping",
# "SOL pumping"). Only consulted when the regex patterns find nothing.
# Keep narrow — false positives mis-tag interpretations.
_BARE_WORD_ALIASES = {
    "GOLD": "XAU/USD",
    "SILVER": "XAG/USD",
    "OIL": "WTI/USD",
    "BRENT": "BRENT/USD",
    "BITCOIN": "BTC/USDT",
    "ETHEREUM": "ETH/USDT",
    "SOLANA": "SOL/USDT",
}

# Currencies whose pair should NOT default to USDT (forex doesn't use it).
_FOREX_BASES = {"EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "USD"}

# Standalone crypto bases — mentioned bare ("ETH dropped", "SOL pumping")
# get a USDT default. Restrict to liquid majors so we don't misfire on
# ambiguous 3-letter words.
_BARE_CRYPTO_BASES = {
    "BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "DOT", "AVAX", "LINK",
    "BNB", "LTC", "MATIC", "UNI", "AAVE", "NEAR", "APT", "SUI",
}


def _extract_symbol_from_text(text: str) -> Optional[str]:
    """Try to extract a trading symbol from free text.

    Returns canonical slash form (e.g. 'BTC/USDT', 'EUR/USD', 'XAU/USD')
    or ``None`` when no recognised symbol is present. Indices and
    out-of-band markets return ``None`` here — they're routed by
    callers that already know the venue.
    """
    if not text:
        return None
    upper = text.upper()

    # Index symbols (NAS100, SPX500, etc.) shouldn't be coerced into a
    # USDT pair — return None so callers route them to their own venue.
    if _INDEX_PATTERN.search(upper):
        return None

    for pat in _TICKER_PATTERNS:
        m = pat.search(upper)
        if m:
            groups = m.groups()
            if len(groups) == 2:
                base, quote = groups[0], groups[1]
                if base in _KNOWN_BASES:
                    return f"{base}/{quote}"
            elif len(groups) == 1:
                base = groups[0]
                if base in _KNOWN_BASES and base not in _FOREX_BASES:
                    # Forex bases alone (`$EUR`) are ambiguous — skip.
                    return f"{base}/USDT"

    # Bare-word fallback (only when nothing above matched).
    # Walk word-by-word so multi-word headlines hit the alias map first
    # ("Gold breaks 2400") before falling to bare crypto bases ("ETH down 4%").
    for raw in _re.findall(r"[A-Z]{3,8}", upper):
        if raw in _BARE_WORD_ALIASES:
            return _BARE_WORD_ALIASES[raw]
        if raw in _BARE_CRYPTO_BASES:
            return f"{raw}/USDT"
        if raw in _FOREX_BASES:
            # Bare forex base alone is too ambiguous (could be USD context
            # in any sentence). Skip — only resolve forex via paired regex.
            continue
    return None


# ---------------------------------------------------------------------------
# Price-level parsing — LLM returns strings like "$8.77-$8.82", "Below 94k",
# "1.240-1.260", "4605.767", etc.  We need floats for tracked_positions.
# ---------------------------------------------------------------------------
_PRICE_CLEAN_RE = _re.compile(r"[^\d.\-]")  # keep digits, dots, hyphens
_RANGE_RE = _re.compile(r"^([\d.]+)\s*[-–—to]+\s*([\d.]+)$")
_K_SUFFIX_RE = _re.compile(r"^([\d.]+)\s*[kK]$")


# ---------------------------------------------------------------------------
# Round 9 (2026-05-24) — Trader-setup gate.
#
# The chart_analysis prompt was previously instructing the LLM to "construct
# a trade" from drawn zones whenever no explicit setup was present. That
# pushed every level-commentary post (e.g. "btc must get above here") into
# `trader_trades`, which then booked a tracked_position falsely attributed
# to the trader. The new prompt (version >= 2026.05.24-trader-explicit-only-v1)
# forbids this and instead requires each trader_trade to carry an
# `explicit_evidence` tag of A1 / A2 / A3 / A4 (see prompt for definitions).
#
# This gate is a server-side defence in depth: even if the LLM disobeys, we
# refuse to book a tracked_position with signal_source='trader' unless we
# have evidence the trader actually called the setup.
#
# Backward-compatible: for prompt versions issued before this gate (anything
# without "trader-explicit-only" in the version string), we skip the gate
# and trust the old behaviour, so historical interpretations and any
# in-flight cached prompts don't suddenly stop producing trades.
# ---------------------------------------------------------------------------
_TRADER_GATE_PROMPT_TAG = "trader-explicit-only"
_DIRECTION_WORDS = (
    "long", "short", "buy", "sell",
    "buying", "selling",
    "entered", "entering", "enter ",
    "shorting", "longing",
    "bought", "sold",
)
_LEVEL_WORDS = (
    "entry", "entries",
    "sl", "stop", "stoploss", "stop-loss", "stop loss",
    "tp", "tp1", "tp2", "tp3",
    "target", "targets",
    "take profit", "takeprofit", "take-profit",
)
_VALID_EVIDENCE_CODES = {"A1", "A2", "A3", "A4"}


def _is_explicit_trader_setup(
    trade: Dict[str, Any],
    news_text: str,
    prompt_version: str,
) -> Tuple[bool, str]:
    """Return (is_explicit, evidence_code).

    Decision rules (only applied when prompt enforces the gate):

      1. If the LLM provided an `explicit_evidence` field set to A1/A2/A3/A4,
         trust it (the prompt told the LLM what each code means).
      2. Else scan the news text for a direction-word + level-word pair
         (e.g. "long BTC entry 78000 SL 74850 TP 79500").
      3. Else accept a direction-word followed by a price-like number
         within a short window (e.g. "Jumped in a BTC long to 79100",
         "shorting 4200, target 4000"). This catches casual entry calls
         that omit the formal "entry"/"sl" keywords.
      4. Anything else → not explicit. The trade should NOT be written as
         a trader_trade. Caller should drop it (the chart_hacker pass
         independently produces an inferred opinion if the chart warrants).

    Backward-compatible: returns (True, "legacy") when the prompt is the old
    one, so historical interpretations / cached prompt versions don't break.
    """
    if not isinstance(trade, dict):
        return False, "invalid_trade_object"

    # Backward-compatible bypass for old prompts.
    if not prompt_version or _TRADER_GATE_PROMPT_TAG not in str(prompt_version):
        return True, "legacy_prompt"

    # 1. LLM-declared evidence code.
    ev_raw = str(trade.get("explicit_evidence") or "").upper().strip()
    if ev_raw in _VALID_EVIDENCE_CODES:
        return True, ev_raw

    text = (news_text or "").lower()
    if not text:
        return False, "no_explicit_evidence"

    has_dir = any(w in text for w in _DIRECTION_WORDS)

    # 2. Strict keyword fallback — direction-word + level-word.
    if has_dir and any(w in text for w in _LEVEL_WORDS):
        return True, "A3_text_fallback"

    # 3. Loose fallback — direction-word followed by a price-like number
    # within ~40 characters. Catches "long 79100", "shorting at 4200",
    # "buying $0.85", "entered btc 70k".
    if has_dir:
        for dw in _DIRECTION_WORDS:
            i = text.find(dw)
            while i != -1:
                window = text[i + len(dw): i + len(dw) + 40]
                if _re.search(r"[\$]?\s*\d{1,3}(?:[\.,]\d+)?\s*[kK]?", window):
                    # Make sure the matched number isn't trivially "1" / "0"
                    m = _re.search(r"[\$]?\s*(\d{1,3}(?:[\.,]\d+)?)\s*([kK]?)", window)
                    if m:
                        try:
                            val = float(m.group(1).replace(",", "."))
                            if m.group(2):
                                val *= 1000.0
                        except ValueError:
                            val = 0.0
                        if val >= 0.0001:  # any plausible price
                            return True, "A3_price_window"
                i = text.find(dw, i + 1)

    return False, "no_explicit_evidence"


def _parse_price_level(value: Any) -> Optional[float]:
    """Parse a price-level string from the LLM into a float.

    Handles: "$8.77", "$8.77-$8.82" (midpoint), "Below $8.40" -> 8.40,
    "Above 94k" -> 94000, "1.240-1.260" (midpoint), plain "4605.767",
    None -> None.  Returns None on any parse failure.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v if v > 0 else None

    text = str(value).strip()
    if not text or text.lower() == "null" or text.lower() == "none":
        return None

    # Strip surrounding prose like "Below", "Above", "Around", "~"
    text = _re.sub(r"^(?:below|above|around|approx\.?|~)\s*", "", text, flags=_re.IGNORECASE)

    # Strip currency symbols, commas, spaces (but preserve 'k' for suffix check)
    text = text.replace(",", "").replace(" ", "").replace("$", "")

    if not text:
        return None

    # Handle "k" suffix BEFORE general cleanup: "94k" -> 94000
    k_match = _K_SUFFIX_RE.match(text)
    if k_match:
        try:
            return float(k_match.group(1)) * 1000
        except ValueError:
            return None

    # Strip remaining non-numeric chars (letters other than k already handled)
    text = _PRICE_CLEAN_RE.sub("", text)
    if not text:
        return None

    # Handle ranges: "8.77-8.82" -> midpoint
    range_match = _RANGE_RE.match(text)
    if range_match:
        try:
            lo = float(range_match.group(1))
            hi = float(range_match.group(2))
            mid = (lo + hi) / 2.0
            return mid if mid > 0 else None
        except ValueError:
            return None

    # Plain float
    try:
        v = float(text)
        return v if v > 0 else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Slice 3 — Entry-price resolution helper.
# ---------------------------------------------------------------------------
_ENTRY_PRICE_SOURCES = (
    "trader",
    "llm",
    "live_price",
    "last_candle",
    "surgeon",  # exchange fill mirrored from a Surgeon daemon (Slice 3 bridge)
    "none",
)


async def _resolve_entry_price(
    pool: DatabasePool,
    interpretation: Dict[str, Any],
    symbol: str,
    exchange: str,
) -> Tuple[Optional[float], str]:
    """Resolve an entry price for a tracked_position with provenance.

    Tries, in order:
      1. ``interpretation['trader_entry']`` — explicit trader-marked price.
      2. ``interpretation['llm_entry']``    — LLM-extracted level.
      3. live CCXT price probe              — degraded but real-time.
      4. last 1m candle close (DB)          — coldest fallback.

    Args:
        pool: Shared Postgres pool used by the candle fallback.
        interpretation: Mapping that may contain ``trader_entry``/``llm_entry``
            keys (raw values get parsed via :func:`_parse_price_level`).
        symbol: Slash-form symbol (e.g. ``BTC/USDT``) for the live/candle path.
        exchange: Venue string (e.g. ``bybit``).

    Returns:
        Tuple ``(price, source)`` where ``source`` is one of
        ``{'trader','llm','live_price','last_candle','none'}``. ``price`` is
        ``None`` only when source is ``'none'``.
    """
    try:
        trader_raw = interpretation.get("trader_entry") if isinstance(interpretation, dict) else None
        trader_p = _parse_price_level(trader_raw)
        if trader_p is not None and trader_p > 0.0:
            return trader_p, "trader"
    except Exception as exc:
        logger.debug("entry resolve trader stage failed: %s", exc)

    try:
        llm_raw = interpretation.get("llm_entry") if isinstance(interpretation, dict) else None
        llm_p = _parse_price_level(llm_raw)
        if llm_p is not None and llm_p > 0.0:
            return llm_p, "llm"
    except Exception as exc:
        logger.debug("entry resolve llm stage failed: %s", exc)

    if symbol and symbol.upper() != "UNKNOWN":
        try:
            probe = await _ccxt_live_price(symbol, exchange or "bybit")
            if probe is not None:
                px = float(probe[0])
                if px > 0.0:
                    return px, "live_price"
        except Exception as exc:
            logger.debug("entry resolve live_price stage failed: %s", exc)

        try:
            cdl = await _lookup_current_price(pool, symbol, exchange or "bybit")
            if cdl is not None and cdl > 0.0:
                return float(cdl), "last_candle"
        except Exception as exc:
            logger.debug("entry resolve last_candle stage failed: %s", exc)

    return None, "none"


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
    # Phase J — dual-source extensions
    signal_source: str = "trader",
    actor_type: Optional[str] = None,
    actor_id: Optional[str] = None,
    trade_type: Optional[str] = None,
    timeframe: Optional[str] = None,
    take_profit_2: Optional[float] = None,
    take_profit_3: Optional[float] = None,
    take_profit_4: Optional[float] = None,
    take_profit_5: Optional[float] = None,
    take_profit_6: Optional[float] = None,
    # Slice 3 — entry-price provenance
    entry_price_source: Optional[str] = None,
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

    # Slice 3 — explicit gating: require minimum confidence and a known symbol.
    try:
        _conf = float(detection_confidence)
    except (TypeError, ValueError):
        _conf = 0.0
    if _conf < 0.4:
        logger.debug(
            "Skipping tracked_position creation: confidence=%.4f below 0.4 "
            "(symbol=%s direction=%s news_item_id=%s)",
            _conf, instrument_symbol, direction, news_item_id,
        )
        return None
    if not instrument_symbol or str(instrument_symbol).strip().upper() == "UNKNOWN":
        logger.debug(
            "Skipping tracked_position creation: symbol=%r is UNKNOWN "
            "(direction=%s news_item_id=%s)",
            instrument_symbol, direction, news_item_id,
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

    # Phase 8 — entry_price sanity: reject implausibly low entries for major coins
    _MIN_ENTRY_PRICE = {
        "BTC": 500.0,
        "ETH": 50.0,
        "SOL": 5.0,
        "AVAX": 1.0,
        "BNB": 10.0,
        "XRP": 0.01,
    }
    _base = (instrument_symbol or "").split("/")[0].upper()
    _min = _MIN_ENTRY_PRICE.get(_base)
    if _min is not None and entry_price is not None and float(entry_price) < _min:
        logger.warning(
            "F5 reject tracked_position: entry_price=%.8f below sanity floor "
            "for %s (min=%.2f) news_item_id=%s — likely chart-reading error",
            float(entry_price), instrument_symbol, _min, news_item_id,
        )
        return None

    # Phase 8 — consensus quality gate: when LLM and quant disagree
    # on direction (method="conflict"), skip position creation. 54% of
    # interpretations currently have conflicting tracks — creating
    # positions from these produces noise that expires uselessly.
    if detection_method == "conflict":
        logger.info(
            "Skipping tracked_position: LLM/quant conflict on direction "
            "(symbol=%s news_item_id=%s conf=%.4f)",
            instrument_symbol, news_item_id, detection_confidence,
        )
        return None

    now = datetime.now(timezone.utc)

    # Round 12 (2026-05-24): Exchange routing.
    #
    # Pre-Round-12, every position defaulted to ``instrument_exchange='bybit'``
    # regardless of what the symbol actually was. PositionMonitor then spammed
    # ~20k "bybit does not have market symbol" errors per day on four symbols
    # (GOLD, US100, 1000PEPE/USDT, COTI/USDT). The new exchange_router
    # consults unified_instruments + a small alias table + a Bybit-perp
    # synthesis fallback to produce a deterministic (exchange, epic_code,
    # canonical_symbol) tuple. See shared/utils/exchange_router.py.
    #
    # Three outcomes:
    #   1. supported & crypto  → routed.exchange ∈ {bybit,bitget,blofin}
    #      with .canonical_symbol used for the row + .ccxt_perp_symbol kept
    #      in metadata for monitor lookups.
    #   2. supported & cfd     → routed.exchange = 'capital.com',
    #      routed.epic_code populated, used by PositionMonitor's Capital
    #      OHLCV path (Phase 12.1c).
    #   3. unsupported         → INSERT the row anyway with status='cancelled'
    #      and status_reason='unsupported:<reason>:<utc>' so the trader's
    #      call appears in history with a clear "we couldn't route this"
    #      tag. This stops the monitor from polling and stops dashboard
    #      "phantom pending" rows. The legacy ``or "bybit"`` default is
    #      gone — fail loudly instead of silently sending bogus symbols.
    from shared.utils.exchange_router import (
        resolve_market as _resolve_market_r12,
        unsupported_status_reason as _unsupported_reason_r12,
    )
    routed = await _resolve_market_r12(instrument_symbol, instrument_exchange)
    if routed.supported:
        # Use the routed values — the trader's free-text symbol gets
        # canonicalised (e.g. NAS100 -> US100, XAU/USD -> GOLD) so dashboards
        # and dedup queries see one form, not five. raw_signal_text retains
        # the trader's original wording for audit.
        instrument_symbol = routed.canonical_symbol or instrument_symbol
        instrument_exchange = routed.exchange or instrument_exchange
        _routed_epic_code = routed.epic_code  # may be None for crypto
        _routed_perp_symbol = routed.ccxt_perp_symbol  # informational
    else:
        # Symbol can't be routed. Skip the rest of the heavy machinery
        # (CCXT pre-flights, dedup) and persist a cancelled row so the
        # operator sees the rejection once, not 20,000 times.
        cancelled_reason = _unsupported_reason_r12(routed, now.isoformat())
        logger.info(
            "Round 12 routing: symbol=%r unsupported (reason=%s) — inserting "
            "cancelled tracked_position news_item_id=%s",
            instrument_symbol, routed.unsupported_reason, news_item_id,
        )
        try:
            row_unsup = await shared_pool.fetch_one(
                """
                INSERT INTO public.tracked_positions (
                    news_item_id, media_item_id, trader_profile_id,
                    signal_interpretation_id,
                    instrument_symbol, instrument_exchange,
                    instrument_symbol_normalised,
                    direction, entry_price, stop_loss, take_profit_1,
                    detection_method, detection_confidence,
                    raw_signal_text, signal_timestamp,
                    status, status_reason, company_id,
                    correlation_id,
                    signal_source, actor_type, actor_id,
                    created_at, updated_at
                ) VALUES (
                    $1, $2, $3, $4,
                    $5, $6, $7,
                    $8, $9, $10, $11,
                    $12, $13,
                    $14, $15,
                    'cancelled', $16, $17,
                    $18,
                    $19, $20, $21,
                    $22, $22
                )
                ON CONFLICT (news_item_id, trader_profile_id, instrument_symbol, direction)
                DO NOTHING
                RETURNING id
                """,
                (
                    news_item_id, media_item_id, trader_profile_id,
                    signal_interpretation_id,
                    (instrument_symbol or "")[:64],
                    "unsupported",
                    (routed.canonical_symbol or instrument_symbol or "")[:64],
                    direction,
                    entry_price, stop_loss, take_profit_1,
                    "text_parser" if detection_method not in (
                        "manual", "llm_vision", "text_parser",
                        "quant_pattern", "agent_override",
                    ) else detection_method,
                    round(float(detection_confidence or 0.0), 4),
                    (raw_signal_text or "")[:2000],
                    now,
                    cancelled_reason[:512],
                    company_id,
                    correlation_id or None,
                    signal_source, actor_type, actor_id,
                    now,
                ),
            )
            return int(row_unsup["id"]) if row_unsup else None
        except Exception as exc:
            logger.warning(
                "Round 12 routing: failed to write cancelled row "
                "for symbol=%r news_item_id=%s: %s",
                instrument_symbol, news_item_id, exc,
            )
            return None

    # Phase 6: normalise instrument symbol for cross-venue lookups.
    # Round 12 (2026-05-24): instrument_exchange is now post-router, so
    # the legacy ``or "bybit"`` fallback is unnecessary. Defensive: still
    # falls back to bybit if upstream somehow handed us a None (the router
    # should already have replaced it, but normalise_instrument crashes
    # on None).
    sym_norm, venue_norm = normalise_instrument(
        instrument_symbol, instrument_exchange or "bybit"  # post-router; defensive only
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

    # Map consensus method values to detection_method CHECK constraint values.
    # ConsensusResult.method ∈ {agreement, quant_dominant, llm_dominant, conflict};
    # text-track passes 'text_extraction'. The DB CHECK constraint allows only
    # {manual, llm_vision, text_parser, quant_pattern, agent_override}.
    _METHOD_MAP = {
        "agreement": "llm_vision",
        "llm_dominant": "llm_vision",
        "conflict": "llm_vision",
        "quant_dominant": "quant_pattern",
        "text_extraction": "text_parser",
    }
    _ALLOWED_METHODS = {
        "manual",
        "llm_vision",
        "text_parser",
        "quant_pattern",
        "agent_override",
    }
    detection_method = _METHOD_MAP.get(detection_method, detection_method)
    if detection_method not in _ALLOWED_METHODS:
        logger.warning(
            "Unknown detection_method=%r for news_item_id=%s; defaulting to llm_vision",
            detection_method,
            news_item_id,
        )
        detection_method = "llm_vision"

    # asyncpg cannot bind a Python list to pgvector; serialise to the
    # PostgreSQL vector literal form '[a,b,c]' so the $23::vector(384) cast
    # accepts it as text.
    trader_embed_literal: Optional[str] = None
    if trader_embed:
        try:
            trader_embed_literal = (
                "[" + ",".join(repr(float(x)) for x in trader_embed) + "]"
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "Failed to serialise trader_embed for news_item_id=%s: %s",
                news_item_id,
                exc,
            )
            trader_embed_literal = None

    # Slice 3 — sanitise entry_price_source against the known set.
    if entry_price_source is not None and entry_price_source not in _ENTRY_PRICE_SOURCES:
        logger.warning(
            "Unknown entry_price_source=%r for news_item_id=%s; storing NULL",
            entry_price_source, news_item_id,
        )
        entry_price_source = None

    # 2026-05-22 — Already-In-Play / Play-Out Guard.
    # Check if the trade setup has already been completed or played out by the time
    # we first process/detect it. This prevents creating pending positions for
    # retroactive retrospective/autopsy posts where price has already hit TP or SL.
    if entry_price is not None and entry_price > 0:
        try:
            # Round 12 (2026-05-24): instrument_exchange is now the routed
            # value (no more "or 'bybit'" default). For CFDs (capital.com)
            # _ccxt_live_price will return None — that's fine, the in-play
            # guard simply doesn't fire. CFD live-price probing happens via
            # the Capital adapter elsewhere.
            probe = await _ccxt_live_price(instrument_symbol, instrument_exchange)
            if probe is not None:
                live_px, _, _ = probe
                if direction == "long":
                    if stop_loss is not None and float(stop_loss) > 0 and live_px <= float(stop_loss):
                        logger.warning(
                            "Already-In-Play Guard reject tracked_position: LONG already stopped out "
                            "(Symbol=%s, Live=%.6f, Entry=%.6f, SL=%.6f) news_item_id=%s",
                            instrument_symbol, live_px, entry_price, float(stop_loss), news_item_id,
                        )
                        return None
                    if take_profit_1 is not None and float(take_profit_1) > 0 and live_px >= float(take_profit_1):
                        logger.warning(
                            "Already-In-Play Guard reject tracked_position: LONG already hit TP1 "
                            "(Symbol=%s, Live=%.6f, Entry=%.6f, TP1=%.6f) news_item_id=%s",
                            instrument_symbol, live_px, entry_price, float(take_profit_1), news_item_id,
                        )
                        return None
                elif direction == "short":
                    if stop_loss is not None and float(stop_loss) > 0 and live_px >= float(stop_loss):
                        logger.warning(
                            "Already-In-Play Guard reject tracked_position: SHORT already stopped out "
                            "(Symbol=%s, Live=%.6f, Entry=%.6f, SL=%.6f) news_item_id=%s",
                            instrument_symbol, live_px, entry_price, float(stop_loss), news_item_id,
                        )
                        return None
                    if take_profit_1 is not None and float(take_profit_1) > 0 and live_px <= float(take_profit_1):
                        logger.warning(
                            "Already-In-Play Guard reject tracked_position: SHORT already hit TP1 "
                            "(Symbol=%s, Live=%.6f, Entry=%.6f, TP1=%.6f) news_item_id=%s",
                            instrument_symbol, live_px, entry_price, float(take_profit_1), news_item_id,
                        )
                        return None
        except Exception as exc:
            logger.warning("Already-In-Play Guard price lookup failed: %s", exc)

    # Round 13.6 (2026-05-24) — per-trader pending uniqueness.
    #
    # Operator rule (verbatim): "only one long and one short can be opened
    # per coin per trader, whether the AI charthacker or a discord trader.
    # We can update with the newest ones (freshest) if there's a new one
    # that comes in."
    #
    # Translation: per (trader_profile_id, normalised_symbol, direction)
    # there must be at most ONE row in status='pending'. When a new signal
    # arrives for the same key, REFRESH the existing row in place with the
    # freshest entry/SL/TP/reasons/raw text — do NOT create a second row.
    # The function returns the existing id so downstream wiring (postmortem,
    # surgeon-bridge, dashboard anchors) all keep pointing at one stable id.
    #
    # Why per-trader (not cross-trader): cross-trader dedup loses signal
    # provenance (we'd cancel trader_1's BTC long because chart_hacker had
    # one at the same price). Per-trader keeps each trader's own track
    # record intact while still bounding total row count: with N traders
    # the upper bound is now 2N rows per coin (1 long + 1 short per
    # trader), vs. effectively unbounded before.
    #
    # Symbol matching: we accept canonical / compact / case-variant forms
    # to catch legacy rows stored as ``BTCUSDT`` while a new write arrives
    # canonicalised to ``BTC/USDT:USDT``. See ``trade_dedup._compact_symbol``
    # for the full strip rules.
    #
    # Replaces the silently-broken Phase-8 cross-actor dedup (TypeError in
    # fetch_one signature got swallowed by bare except, letting 125
    # duplicates accumulate) AND the Round-13.5 1%/4h tolerance logic
    # (too aggressive — collapsed genuine multi-trader confluence).
    if entry_price is not None and entry_price > 0:
        from shared.intelligence.trade_dedup import _compact_symbol  # noqa: WPS433
        compact_sym = _compact_symbol(instrument_symbol) or instrument_symbol

        existing = None
        try:
            existing = await shared_pool.fetch_one(
                """
                SELECT id, entry_price, created_at
                FROM public.tracked_positions
                WHERE status = 'pending'
                  AND trader_profile_id = $1
                  AND direction = $2
                  AND (instrument_symbol = $3
                       OR instrument_symbol = $4
                       OR instrument_symbol_normalised = $3
                       OR UPPER(instrument_symbol) = UPPER($4)
                       OR UPPER(instrument_symbol_normalised) = UPPER($3))
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (
                    int(trader_profile_id),
                    direction,
                    instrument_symbol,
                    compact_sym,
                ),
            )
        except Exception as exc:
            # Defensive: never block a legitimate signal on a dedup hiccup.
            # Log loudly — the previous bare-except swallow is exactly what
            # hid the silently-broken Phase-8 dedup for 2 days.
            logger.warning(
                "per-trader dedup query failed (news_item_id=%s trader=%s symbol=%s dir=%s): %s "
                "— proceeding without dedup",
                news_item_id, trader_profile_id, instrument_symbol, direction, exc,
            )
            existing = None

        if existing is not None:
            existing_id = int(existing["id"])
            existing_entry = 0.0
            try:
                existing_entry = float(existing.get("entry_price") or 0)
            except (ValueError, TypeError):
                pass  # non-numeric DB value — treat as unknown, fall through to refresh

            # 1% variance rule: >1% diff = different setup (INSERT new).
            # Within 1% = same trade, freshest numbers win (refresh).
            if existing_entry > 0 and entry_price is not None and entry_price > 0:
                pct_diff = abs(existing_entry - float(entry_price)) / existing_entry
                if pct_diff > 0.01:
                    logger.info(
                        "per-trader dedup: entry variance %.2f%% > 1%% for trader=%s symbol=%s — "
                        "keeping both (different setups)",
                        pct_diff * 100, trader_profile_id, instrument_symbol,
                    )
                    existing = None  # fall through to INSERT new row

            if existing is not None:
                try:
                    # In-place refresh: pull freshest entry/SL/TPs/reasons onto
                    # the existing pending row, advance signal_interpretation_id
                    # so the drawer points at the newest interpretation, stamp
                    # deduped_at for the dashboard KPI. raw_signal_text is only
                    # overwritten when the incoming text is non-empty (preserve
                    # the most-detailed prior wording on terse follow-ups).
                    await shared_pool.execute(
                    """
                        UPDATE public.tracked_positions
                        SET entry_price             = $1,
                        stop_loss               = COALESCE($2, stop_loss),
                        take_profit_1           = COALESCE($3, take_profit_1),
                        take_profit_2           = COALESCE($4, take_profit_2),
                        take_profit_3           = COALESCE($5, take_profit_3),
                        take_profit_4           = COALESCE($6, take_profit_4),
                        take_profit_5           = COALESCE($7, take_profit_5),
                        take_profit_6           = COALESCE($8, take_profit_6),
                        entry_reason_trader     = COALESCE($9,  entry_reason_trader),
                        entry_reason_llm        = COALESCE($10, entry_reason_llm),
                        entry_reason_agent      = COALESCE($11, entry_reason_agent),
                        raw_signal_text         = CASE WHEN COALESCE($12,'') != '' THEN $12 ELSE raw_signal_text END,
                        signal_interpretation_id = $13,
                        timeframe               = COALESCE($14, timeframe),
                        signal_timestamp        = $15,
                        deduped_at              = NOW(),
                        updated_at              = NOW()
                        WHERE id = $16
                        AND status = 'pending'
                """,
                (
                        float(entry_price),
                        float(stop_loss) if stop_loss is not None else None,
                        float(take_profit_1) if take_profit_1 is not None else None,
                        float(take_profit_2) if take_profit_2 is not None else None,
                        float(take_profit_3) if take_profit_3 is not None else None,
                        float(take_profit_4) if take_profit_4 is not None else None,
                        float(take_profit_5) if take_profit_5 is not None else None,
                        float(take_profit_6) if take_profit_6 is not None else None,
                        (entry_reason_trader[:2000] if entry_reason_trader else None),
                        (entry_reason_llm[:2000] if entry_reason_llm else None),
                        (entry_reason_agent[:2000] if entry_reason_agent else None),
                        (raw_signal_text or ""),
                        signal_interpretation_id,
                        timeframe,
                        now,
                        existing_id,
                    ),
)
                    logger.info(
                        "per_trader_dedup_refresh news_item_id=%s trader=%s symbol=%s dir=%s "
                        "entry=%.6f -> refreshed pid=%s (was entry=%s)",
                        news_item_id, trader_profile_id, instrument_symbol, direction,
                        float(entry_price), existing_id, existing.get("entry_price"),
                    )
                except Exception as exc:
                    # If the UPDATE fails we still return the existing id so the
                    # caller doesn't accidentally create a duplicate via the
                    # INSERT below. The pending row keeps its previous values
                # — operator can re-trigger by toggling the news item.
                    logger.warning(
                        "per_trader_dedup UPDATE failed for pid=%s (%s) — returning "
                        "existing id without refresh; operator should re-poll",
                        existing_id, exc,
                )
            return existing_id

    row = await shared_pool.fetch_one(
        """
        INSERT INTO public.tracked_positions (
            news_item_id, media_item_id, trader_profile_id,
            signal_interpretation_id,
            instrument_symbol, instrument_exchange,
            instrument_symbol_normalised,
            epic_code,
            direction, entry_price, stop_loss, take_profit_1,
            detection_method, detection_confidence,
            raw_signal_text, signal_timestamp,
            status, status_reason, company_id,
            entry_reason_trader, entry_reason_llm, entry_reason_agent,
            entry_reason_frozen_at, reason_agreement_score,
            entry_reason_trader_embedding,
            correlation_id,
            signal_source, actor_type, actor_id,
            trade_type, timeframe,
            take_profit_2, take_profit_3,
            take_profit_4, take_profit_5, take_profit_6,
            entry_price_source,
            created_at, updated_at
        ) VALUES (
            $1, $2, $3,
            $4,
            $5, $6,
            $7,
            $8,
            $9, $10, $11, $12,
            $13, $14,
            $15, $16,
            $17, $18, $19,
            $20, $21, $22,
            $23, $24,
            $25::vector(384),
            $26,
            $27, $28, $29,
            $30, $31,
            $32, $33,
            $34, $35, $36,
            $37,
            $38, $38
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
            # Round 12 (2026-05-24): instrument_exchange is now ALWAYS the
            # routed exchange (no more silent ``or "bybit"`` default). For
            # CFDs the routed value is 'capital.com' and epic_code is
            # populated below.
            instrument_exchange,
            sym_norm,
            _routed_epic_code,
            direction,
            entry_price,
            stop_loss,
            take_profit_1,
            detection_method,
            round(detection_confidence, 4),
            raw_signal_text[:2000] if raw_signal_text else "",
            now,
            "pending",
            "awaiting_entry",
            company_id,
            entry_reason_trader[:2000] if entry_reason_trader else None,
            entry_reason_llm[:2000] if entry_reason_llm else None,
            entry_reason_agent[:2000] if entry_reason_agent else None,
            now,
            round(reason_agreement, 4) if reason_agreement is not None else None,
            trader_embed_literal,
            correlation_id or None,
            signal_source,
            actor_type,
            actor_id,
            trade_type,
            timeframe,
            take_profit_2,
            take_profit_3,
            take_profit_4,
            take_profit_5,
            take_profit_6,
            entry_price_source,
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
    expected_processed_at: Optional[datetime] = None,
) -> None:
    """Update media_items.processing_status and optional error.

    Bug H fix (2026-05-24 second-round audit): only flip rows that are still
    in a non-terminal state. Without this guard, a stale worker that finishes
    a 60-minute LLM call after ``recover_stale_analyzing`` has reset its claim
    (and a sibling worker has already written ``analyzed``) would blindly
    overwrite the sibling's status. The DB unique constraints already prevent
    duplicate trades, but the status field can still be cosmetically wrong.
    The guard makes the late writer a no-op.

    Bug H round-3 review fix (BH2 #2, CA1 Fix H #2): add an optional
    ``expected_processed_at`` claim-timestamp CAS. When provided (callers that
    fetched the row via ``fetch_pending_media`` know their claim timestamp),
    the UPDATE additionally asserts ``processed_at = $expected_processed_at``.
    If a stale-recovery sweep + sibling reclaim has rotated the row since
    this worker's claim, ``processed_at`` will differ → 0 rows updated →
    no-op + log. This closes the cosmetic ``analyzing → analyzed`` clobber
    that the non_terminal guard alone could not catch.
    """
    non_terminal = ('downloaded', 'pending', 'analyzing')
    async with shared_pool.acquire() as conn:
        if error and expected_processed_at is not None:
            result = await conn.execute(
                "UPDATE public.media_items SET "
                "  processing_status = $1, "
                "  processing_error = $2, "
                "  processed_at = NOW() "
                "WHERE id = $3 "
                "  AND processing_status = ANY($4::text[]) "
                "  AND processed_at = $5",
                status, error, media_id, list(non_terminal), expected_processed_at,
            )
        elif error:
            result = await conn.execute(
                "UPDATE public.media_items SET "
                "  processing_status = $1, "
                "  processing_error = $2, "
                "  processed_at = NOW() "
                "WHERE id = $3 "
                "  AND processing_status = ANY($4::text[])",
                status, error, media_id, list(non_terminal),
            )
        elif expected_processed_at is not None:
            result = await conn.execute(
                "UPDATE public.media_items SET "
                "  processing_status = $1, "
                "  processed_at = NOW() "
                "WHERE id = $2 "
                "  AND processing_status = ANY($3::text[]) "
                "  AND processed_at = $4",
                status, media_id, list(non_terminal), expected_processed_at,
            )
        else:
            result = await conn.execute(
                "UPDATE public.media_items SET "
                "  processing_status = $1, "
                "  processed_at = NOW() "
                "WHERE id = $2 "
                "  AND processing_status = ANY($3::text[])",
                status, media_id, list(non_terminal),
            )
    # Log when the CAS path no-ops so operators can see late-worker drops.
    # Round-6 sweep (CA2 #18): downgraded to DEBUG. The original INFO line
    # was useful when the CAS guard first shipped, but in steady-state it
    # spams the journal whenever multiple workers race on a slow vision LLM
    # cycle. DEBUG keeps the audit trail accessible via journalctl -u
    # tickles-interpretation -p debug while leaving INFO clean.
    if isinstance(result, str) and result.endswith(" 0") and expected_processed_at is not None:
        logger.debug(
            "update_media_status no-op: media_id=%s status=%s claim revoked "
            "(expected_processed_at=%s) — stale worker dropped its result.",
            media_id, status, expected_processed_at,
        )


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
# Surrounding Context Extractor
# ---------------------------------------------------------------------------
def _format_context_window(context_window_raw: Any, target_author: str) -> str:
    """Format the context window messages from the same author chronologically."""
    if not context_window_raw:
        return ""
    
    try:
        if isinstance(context_window_raw, str):
            try:
                context_window = json.loads(context_window_raw)
            except Exception:
                return ""
        else:
            context_window = context_window_raw
                
        if not isinstance(context_window, dict):
            return ""
            
        before_msgs = context_window.get("before")
        after_msgs = context_window.get("after")
        
        if not isinstance(before_msgs, list):
            before_msgs = []
        if not isinstance(after_msgs, list):
            after_msgs = []
        
        all_msgs = []
        
        def is_same_author(msg_author: Any) -> bool:
            if not msg_author or not target_author:
                return False
            ma = str(msg_author).lower().strip()
            ta = str(target_author).lower().strip()
            return ma == ta
        
        # Add before messages
        for msg in before_msgs:
            if isinstance(msg, dict) and is_same_author(msg.get("author")):
                all_msgs.append(msg)
                
        # Add after messages
        for msg in after_msgs:
            if isinstance(msg, dict) and is_same_author(msg.get("author")):
                all_msgs.append(msg)
                
        if not all_msgs:
            return ""
            
        lines = ["--- Surrounding Context Messages ---"]
        for m in all_msgs:
            if not isinstance(m, dict):
                continue
            ts_val = m.get("timestamp")
            ts = str(ts_val) if ts_val is not None else "unknown"
            if isinstance(ts_val, str) and "T" in ts_val:
                try:
                    dt = datetime.fromisoformat(ts_val.replace("Z", "+00:00"))
                    ts = dt.strftime("%H:%M:%S")
                except Exception:
                    pass
            author_display = str(m.get("author", "trader"))
            text_display = str(m.get("text", ""))
            lines.append(f"[{ts}] {author_display}: {text_display}")
        lines.append("----------------------------------")
        return "\n".join(lines)
    except Exception as exc:
        logger.warning("Failed to format context window safely: %s", exc)
        return ""


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
        from typing import List, Optional
        tmp_holder: List[Optional[str]] = [None]
        try:
            return await self._process_one_impl(media_row, tmp_holder)
        finally:
            if tmp_holder[0]:
                try:
                    from pathlib import Path
                    p = Path(tmp_holder[0])
                    if p.exists() and p.is_file():
                        p.unlink()
                        logger.debug("Successfully cleaned up downloaded CDN temp file: %s", tmp_holder[0])
                except Exception as exc:
                    logger.warning("Failed to delete temp file %s: %s", tmp_holder[0], exc)

    async def _process_one_impl(
        self,
        media_row: Dict[str, Any],
        tmp_holder: List[Optional[str]],
    ) -> Dict[str, Any]:
        """Process a single media item implementation: resolve context, run dual-track, write results.

        Returns a status dict for logging/metrics.
        """
        media_id = media_row["media_id"]
        news_item_id = media_row["news_item_id"]
        local_path = media_row["local_path"]
        source_id = media_row.get("source_id")
        headline = media_row.get("headline", "")
        content = media_row.get("content", "")
        instruments_jsonb = media_row.get("instruments")
        # Bug H round-3 review fix (BH2 #2): claim timestamp from
        # `fetch_pending_media`'s atomic UPDATE — used as a CAS sentinel
        # on the terminal `update_media_status` call so a stale-recovered
        # worker can't blindly clobber a sibling's fresh claim.
        claim_ts = media_row.get("claim_ts")

        # Resolve company
        shared_pool = await self._ensure_pool()
        company = "jarvais"
        if source_id:
            resolved = await resolve_company_for_source(shared_pool, source_id)
            if resolved:
                company = resolved

        # Resolve instrument — first try JSONB metadata from the collector,
        # then regex extraction from the news text, then defer to the LLM.
        # F8 removed the old BTCUSDT hardcoded fallback; we now let the LLM
        # identify the instrument from the chart image instead of failing.
        symbol, exchange = await resolve_instrument_symbol(
            shared_pool, instruments_jsonb
        )
        symbol_from_llm = False
        if not symbol:
            # Strip Discord reply quotes ([Reply to @user]: <quoted text>)
            # before symbol extraction. Without this, common words inside the
            # PARENT message's quoted text (e.g. "looking for the OF link"
            # → matches LINK ticker) get falsely attributed to the replying
            # author's signal. We want the symbol to come from the replying
            # author's OWN words (or, if they have none, from the chart via
            # the LLM — which has its own anti-guessing rules).
            clean_headline = strip_reply_prefix(headline or "")
            clean_content = strip_reply_prefix(content or "")
            # Try regex extraction from headline + content (reply-quote-stripped)
            symbol = _extract_symbol_from_text(
                f"{clean_headline} {clean_content}"
            )
            if symbol:
                exchange = "bybit"  # default venue; LLM/quant can refine
                logger.info(
                    "media_id=%s: instrument resolved from text: %s",
                    media_id, symbol,
                )
            else:
                # Let the LLM identify the instrument from the chart image.
                # Use a placeholder; we'll replace it after the LLM responds.
                symbol = "UNKNOWN"
                exchange = "bybit"
                symbol_from_llm = True
                logger.info(
                    "media_id=%s: no instrument in metadata or text; "
                    "deferring to LLM chart identification",
                    media_id,
                )

        # Resolve trader profile (from news item author/channel)
        author = media_row.get("author", "unknown")
        channel = media_row.get("headline", "")  # fallback
        news_source = (media_row.get("source") or "discord").lower()
        platform = "telegram" if news_source == "telegram" else "discord"
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
                                tmp_holder[0] = tmp_path
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
                                    expected_processed_at=claim_ts,
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
                        expected_processed_at=claim_ts,
                    )
                    return {
                        "media_id": media_id,
                        "status": "failed",
                        "reason": f"cdn_download_error: {e}",
                    }
            else:
                logger.warning("Media file missing: %s (media_id=%s)", local_path, media_id)
                await update_media_status(
                    shared_pool, media_id, "failed", error="local_path missing or file not found",
                    expected_processed_at=claim_ts,
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
                await update_media_status(
                    shared_pool, media_id, "analyzed",
                    expected_processed_at=claim_ts,
                )
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
        # Bug 12 sibling: strip the `[Reply to @user]: <quoted>` prefix from
        # both headline and content before handing them to the vision LLM,
        # otherwise the LLM gets the parent message's quoted text as context
        # and can hallucinate the wrong direction or symbol from it.
        clean_headline_for_ctx = strip_reply_prefix(headline or "")
        clean_content_for_ctx = strip_reply_prefix(content or "")
        news_context = f"{clean_headline_for_ctx}\n{clean_content_for_ctx}"[:1000]
        context_window_raw = media_row.get("context_window")
        ctx_txt = _format_context_window(context_window_raw, author)
        if ctx_txt:
            news_context = f"{news_context}\n\n{ctx_txt}"
        # Phase J — pull chart_hacker's relevant past memories (best-effort,
        # never blocks). Provides self-recall + trader-specific observations.
        recall_context = await _recall_relevant_memories(
            company=company,
            symbol=symbol if symbol and symbol != "UNKNOWN" else None,
            direction=None,  # not yet known at this stage
            trader_handle=author if author and author != "unknown" else None,
        )
        # Inject source-specific color scheme rules for the vision LLM
        source_color_rules = await _load_source_color_rules(shared_pool, news_source)
        if source_color_rules:
            recall_context = source_color_rules + "\n\n" + recall_context
        try:
            # Round 10: rate-limit budget is per-model, so resolve fresh
            # so the dropdown change immediately moves us onto the new
            # model's bucket without a service restart.
            _rl_model = await _runtime_get_model(SLOT_PRIMARY)
            await self._rate_limiter.acquire(
                model=_rl_model,
                estimated_cost_usd=0.005,
            )
            llm_result = await run_llm_track(
                cfg=self.cfg,
                image_path=local_path,
                news_context=news_context,
                instrument_symbol=symbol,
                recall_context=recall_context,
                correlation_id=cid,
                news_source=news_source,
                channel_name=media_row.get("channel_name", ""),
                shared_pool=shared_pool,
            )
            self._rate_limiter.report_success()
        except RuntimeError as exc:
            logger.error("LLM track failed for media_id=%s: %s", media_id, exc)
            await update_media_status(
                shared_pool,
                media_id,
                "skipped_vision_unavailable",
                error=str(exc)[:500],
                expected_processed_at=claim_ts,
            )
            return {
                "media_id": media_id,
                "status": "skipped_vision_unavailable",
                "reason": str(exc),
            }

        # --- Slice 4: prefilter rejection short-circuit ---
        # `run_prefilter()` returns an LlmResult with direction='unclear' and
        # reasoning prefixed 'pre-filter:' when the cheap classifier decides
        # the image is commentary / meme / unclear. Previously this still
        # flowed through the full consensus + signal_interpretation write,
        # leaving media_items.processing_status at 'analyzed' and the queue
        # view confusing ("why are these still here?"). We now mark the row
        # 'skipped_not_chart' and return early — no expensive vision call,
        # no signal row, terminal state.
        if (
            llm_result is not None
            and llm_result.direction == "unclear"
            and isinstance(llm_result.reasoning, str)
            and llm_result.reasoning.startswith("pre-filter:")
        ):
            reason_snippet = llm_result.reasoning[:240]
            logger.info(
                "media_id=%s: prefilter rejected — marking skipped_not_chart "
                "(reason=%s)",
                media_id, reason_snippet,
            )
            await update_media_status(
                shared_pool,
                media_id,
                "skipped_not_chart",
                error=f"prefilter: not a chart — {reason_snippet}"[:500],
                expected_processed_at=claim_ts,
            )
            return {
                "media_id": media_id,
                "status": "skipped_not_chart",
                "reason": reason_snippet,
            }

        # --- Post-LLM instrument resolution ---
        # If the collector didn't provide an instrument, use what the LLM
        # identified from the chart image. This replaces the old F8 hard-fail.
        #
        # The vision prompt (shared/intelligence/prompts/chart_analysis.json)
        # forbids guessing: when no ticker text is visible, the LLM must
        # return "UNKNOWN", which is caught a few lines below and marks the
        # media row as failed_unresolved_instrument. signal_interpretations
        # rows produced from this branch are tagged instrument_resolved_from
        # = 'inferred' so downstream consumers can apply lower trust.
        if symbol_from_llm and llm_result and llm_result.instrument:
            llm_instrument = llm_result.instrument.strip().upper()
            if llm_instrument and llm_instrument != "UNKNOWN":
                # Normalise: ensure slash form
                if "/" not in llm_instrument and len(llm_instrument) > 3:
                    # Try to split: BTCUSDT -> BTC/USDT
                    for quote in ("USDT", "USD", "BUSD", "USDC"):
                        if llm_instrument.endswith(quote):
                            llm_instrument = llm_instrument[:-len(quote)] + "/" + quote
                            break
                symbol = llm_instrument
                logger.info(
                    "media_id=%s: LLM identified instrument as %s",
                    media_id, symbol,
                )
            else:
                # LLM couldn't identify it either — mark failed
                logger.warning(
                    "media_id=%s: LLM also returned UNKNOWN instrument; marking failed",
                    media_id,
                )
                await update_media_status(
                    shared_pool, media_id, "failed",
                    error="unresolved_instrument_after_llm",
                    expected_processed_at=claim_ts,
                )
                return {
                    "media_id": media_id,
                    "status": "failed_unresolved_instrument",
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

        # --- Stamp interpretation-time price into quant_indicators (Slice 2) ---
        # `run_quant_track` already injects `current_price` and `price_source`
        # for both the happy-path (last candle close) and the degraded CCXT
        # fallback. We additionally stamp a wall-clock UTC timestamp so the
        # drawer can compute drift between interp-time and live-now price
        # without reaching back to the candles table.
        try:
            interp_indicators = dict(quant_result.indicators or {})
            current_price = interp_indicators.get("current_price")
            if current_price is not None:
                interp_indicators["current_price_at_interp"] = float(current_price)
                interp_indicators["interp_price_source"] = (
                    interp_indicators.get("price_source") or "unknown"
                )
                interp_indicators["interp_price_at"] = (
                    datetime.now(timezone.utc).isoformat()
                )
                quant_result = QuantResult(
                    direction=quant_result.direction,
                    confidence=quant_result.confidence,
                    indicators=interp_indicators,
                    cost_usd=quant_result.cost_usd,
                )
        except Exception as exc:
            logger.debug(
                "Slice 2 price-stamp non-fatal failure for media_id=%s: %s",
                media_id, exc,
            )

        # --- Consensus ---
        consensus = run_consensus(llm_result, quant_result)

        # --- Param hash for reproducibility (Rule 1) ---
        # Round 10: resolve fresh so the audit hash reflects what the
        # InterpretationService actually used at this moment in time, not the
        # value baked into self.cfg at process start.
        _ph_primary = await _runtime_get_model(SLOT_PRIMARY)
        _ph_fallback = await _runtime_get_model(SLOT_FALLBACK)
        param_hash = _param_hash(
            model=llm_result.model_used,
            primary_model=_ph_primary,
            fallback_model=_ph_fallback,
            freshness_threshold=self.cfg.freshness_threshold_s,
            max_age_hours=self.cfg.max_age_hours,
            extra=str(media_id) if media_id is not None else "",  # unique per chart
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
        # Determine instrument_resolved_from honestly based on where the symbol
        # actually came from, so we can later filter / audit "vision-only" rows.
        #   - "inferred"  → symbol came from the LLM reading the chart image
        #                   (collector provided no instrument). These rows are
        #                   inherently lower-trust because the model can guess.
        #   - "context"   → symbol came from a context_window attached to the
        #                   media row (e.g. surrounding messages or thread).
        #   - "message"   → symbol came from the news_item message text /
        #                   collector-extracted instruments JSONB.
        if symbol_from_llm:
            resolved_from = "inferred"
        elif media_row.get("context_window"):
            resolved_from = "context"
        else:
            resolved_from = "message"
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
            prompt_source=llm_result.prompt_source,
            prompt_hash=llm_result.prompt_hash,
        )

        # --- Wire to tracked_positions — Phase J dual write ---
        # Two passes:
        #   1. Each trader_trade → tracked_positions row with signal_source='trader'
        #   2. Each chart_hacker_trade → tracked_positions row with signal_source='chart_hacker'
        # Both pass through the same downstream pipeline (monitor → postmortem
        # → skill score) and land on the same leaderboard.
        positions_created: List[int] = []
        if sig_id is not None and llm_result is not None:
            chart_hacker_pid = await _get_chart_hacker_profile_id(shared_pool)

            async def _write_one(
                trade: Dict[str, Any],
                source: str,
                profile_id: int,
                actor_type_val: str,
                actor_id_val: str,
                reason_trader: Optional[str],
                reason_agent: Optional[str],
            ) -> None:
                if not isinstance(trade, dict):
                    return
                direction = str(trade.get("direction", "")).lower().strip()
                if direction not in ("long", "short"):
                    return  # neutral/unclear/missing → skip
                # Resolve entry price via 4-tier pipeline (trader → LLM → live_price → last_candle)
                entry_levels: Dict[str, Any] = {
                    "trader_entry": trade.get("entry"),
                    "llm_entry": None,
                }
                entry_p, entry_src = await _resolve_entry_price(
                    shared_pool, entry_levels, symbol, exchange or "bybit",
                )
                try:
                    pid = await create_tracked_position_from_interpretation(
                        shared_pool=shared_pool,
                        signal_interpretation_id=sig_id,
                        news_item_id=news_item_id,
                        media_item_id=media_id,
                        trader_profile_id=profile_id,
                        instrument_symbol=symbol,
                        instrument_exchange=exchange or "bybit",
                        direction=direction,
                        entry_price=entry_p,
                        stop_loss=_parse_price_level(trade.get("stop_loss")),
                        # Bug Hunter 2 §10.2 — fall back to singular
                        # ``take_profit`` when the LLM emits a single TP
                        # without numbering it.
                        take_profit_1=_parse_price_level(
                            trade.get("tp1") or trade.get("take_profit")
                        ),
                        detection_method=consensus.method,
                        detection_confidence=float(
                            trade.get("confidence")
                            or trade.get("trader_confidence")
                            or consensus.confidence
                            or 0.0
                        ),
                        # Bug 12 sibling — persist the cleaned reply body so
                        # downstream consumers (postmortem LLM, dashboard,
                        # learning) never see the parent's quoted text.
                        raw_signal_text=f"{clean_headline_for_ctx}\n{clean_content_for_ctx}"[:2000],
                        company_id=company,
                        entry_reason_trader=reason_trader,
                        entry_reason_llm=str(
                            trade.get("rationale") or trade.get("trader_rationale") or ""
                        )[:2000] or None,
                        entry_reason_agent=reason_agent,
                        correlation_id=cid,
                        signal_source=source,
                        actor_type=actor_type_val,
                        actor_id=actor_id_val,
                        trade_type=str(trade.get("trade_type") or "")[:20] or None,
                        timeframe=str(trade.get("timeframe") or llm_result.timeframe or "")[:8] or None,
                        take_profit_2=_parse_price_level(trade.get("tp2")),
                        take_profit_3=_parse_price_level(trade.get("tp3")),
                        take_profit_4=_parse_price_level(trade.get("tp4")),
                        take_profit_5=_parse_price_level(trade.get("tp5")),
                        take_profit_6=_parse_price_level(trade.get("tp6")),
                        entry_price_source=entry_src,
                    )
                    if pid:
                        positions_created.append(pid)
                except Exception as exc:
                    logger.warning(
                        "Phase J %s tracked_position write failed (sig_id=%s): %s",
                        source, sig_id, exc,
                    )

            # Pass 1 — trader's marked trades.
            # Round 9 (2026-05-24) — Trader-setup gate: only book a position
            # with signal_source='trader' when the trade is backed by explicit
            # evidence the human actually called the setup. See
            # `_is_explicit_trader_setup` for the rules. Trades that fail the
            # gate are dropped here (the chart_hacker pass below still gets
            # to produce its own independent opinion if the LLM emitted one).
            trader_actor_id = f"{company}_trader_{trader_profile_id}"
            news_text_for_gate = (
                f"{clean_headline_for_ctx or ''}\n{clean_content_for_ctx or ''}"
            )
            prompt_version_for_gate = getattr(llm_result, "prompt_version", "") or ""
            for trade in (llm_result.trader_trades or []):
                ok, evidence = _is_explicit_trader_setup(
                    trade, news_text_for_gate, prompt_version_for_gate,
                )
                if not ok:
                    logger.info(
                        "Round 9 gate: dropping trader_trade with no explicit "
                        "evidence (sig_id=%s news_item_id=%s symbol=%s "
                        "direction=%s prompt=%s reason=%s) — chart_hacker "
                        "track may still produce an inferred trade.",
                        sig_id, news_item_id, symbol,
                        trade.get("direction"), prompt_version_for_gate,
                        evidence,
                    )
                    continue
                # Stash the evidence code on the trade so downstream auditing
                # can see WHY this trade survived the gate. _write_one ignores
                # unknown keys.
                trade.setdefault("_setup_evidence", evidence)
                await _write_one(
                    trade=trade,
                    source="trader",
                    profile_id=trader_profile_id,
                    actor_type_val="trader_human",
                    actor_id_val=trader_actor_id,
                    # Bug 12 sibling — store the stripped reply body, not the
                    # full `[Reply to @parent]: ...` blob.
                    reason_trader=clean_content_for_ctx[:2000] if clean_content_for_ctx else None,
                    reason_agent=None,
                )

            # Pass 2 — chart_hacker's independent analysis
            if chart_hacker_pid is not None:
                ch_actor_id = f"{company}_rose_ch" if news_source == "telegram" else f"{company}_chart_hacker"
                for trade in (llm_result.chart_hacker_trades or []):
                    await _write_one(
                        trade=trade,
                        source="chart_hacker",
                        profile_id=chart_hacker_pid,
                        actor_type_val="agent",
                        actor_id_val=ch_actor_id,
                        reason_trader=None,
                        reason_agent=str(
                            trade.get("rationale") or llm_result.chart_hacker_market_view or ""
                        )[:2000] or None,
                    )

            # Legacy fallback — if the LLM didn't return either array but the
            # consensus path produced a direction (e.g. fallback to old prompt
            # format), preserve the previous single-write behaviour.
            if not positions_created and consensus.direction in ("long", "short"):
                try:
                    legacy_levels: Dict[str, Any] = {
                        "trader_entry": llm_result.levels.get("entry") if llm_result.levels else None,
                        "llm_entry": None,
                    }
                    legacy_entry_p, legacy_entry_src = await _resolve_entry_price(
                        shared_pool, legacy_levels, symbol, exchange or "bybit",
                    )
                    legacy_pid = await create_tracked_position_from_interpretation(
                        shared_pool=shared_pool,
                        signal_interpretation_id=sig_id,
                        news_item_id=news_item_id,
                        media_item_id=media_id,
                        trader_profile_id=trader_profile_id,
                        instrument_symbol=symbol,
                        instrument_exchange=exchange or "bybit",
                        direction=consensus.direction,
                        entry_price=legacy_entry_p,
                        stop_loss=_parse_price_level(llm_result.levels.get("stop_loss")) if llm_result.levels else None,
                        take_profit_1=_parse_price_level(llm_result.levels.get("take_profit")) if llm_result.levels else None,
                        detection_method=consensus.method,
                        detection_confidence=consensus.confidence,
                        # Bug 12 sibling — also strip in legacy fallback.
                        raw_signal_text=f"{clean_headline_for_ctx}\n{clean_content_for_ctx}"[:2000],
                        company_id=company,
                        entry_reason_trader=clean_content_for_ctx[:2000] if clean_content_for_ctx else None,
                        entry_reason_llm=llm_result.reasoning[:2000] if llm_result.reasoning else None,
                        correlation_id=cid,
                        signal_source="trader",
                        actor_type="trader_human",
                        actor_id=f"{company}_trader_{trader_profile_id}",
                        timeframe=llm_result.timeframe or None,
                        entry_price_source=legacy_entry_src,
                    )
                    if legacy_pid:
                        positions_created.append(legacy_pid)
                except Exception as exc:
                    logger.warning(
                        "Legacy tracked_position fallback failed (sig_id=%s): %s",
                        sig_id, exc,
                    )

        # --- Update media status ---
        # Bug H round-3 review fix: pass the claim timestamp so the UPDATE is
        # CAS-protected — a stale-recovered worker that finishes after a
        # sibling reclaim cannot clobber the sibling's `analyzing` status.
        await update_media_status(
            shared_pool, media_id, "analyzed",
            expected_processed_at=claim_ts,
        )

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

    async def _mark_news_terminal(
        self,
        news_item_id: int,
        terminal_status: str,
    ) -> None:
        """Mark a text-only news_item as terminally processed so the pending-text
        queue (`_fetch_pending_text_news`) stops re-fetching it every cycle.

        Bug C2 fix: previously, text-only messages that hit a skip path
        (`skipped_meme`, `skipped_commentary`, `no_signal_detected`,
        `skipped_unresolved_instrument`) returned without writing any record.
        The queue's `WHERE enrichment_status NOT IN (...) AND NOT EXISTS (signal
        interpretation)` predicate kept matching them forever, paying for an
        LLM call every 5 minutes per stuck message. Setting `enrichment_status`
        to a terminal value drops them from the queue permanently.

        Allowed terminal values (must already be in the queue's NOT-IN list at
        `_fetch_pending_text_news`): 'non_signal', 'skipped', 'duplicate_zone'.
        """
        if terminal_status not in ("non_signal", "skipped", "duplicate_zone"):
            logger.warning(
                "_mark_news_terminal: invalid terminal_status=%r for news_item_id=%s — skipping",
                terminal_status,
                news_item_id,
            )
            return
        try:
            shared_pool = await self._ensure_pool()
            await shared_pool.execute(
                "UPDATE public.news_items "
                "SET enrichment_status = $1, enriched_at = COALESCE(enriched_at, NOW()) "
                "WHERE id = $2 "
                "  AND (enrichment_status IS NULL OR enrichment_status NOT IN ('non_signal','skipped','duplicate_zone'))",
                (terminal_status, news_item_id),
            )
        except Exception as exc:
            # Non-fatal: if this UPDATE fails the message will be re-tried next
            # cycle, but at least we logged it. Don't poison the queue tick.
            logger.warning(
                "_mark_news_terminal: failed to mark news_item_id=%s terminal=%s: %s",
                news_item_id,
                terminal_status,
                exc,
            )

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
            await self._mark_news_terminal(news_item_id, "skipped")
            return {"news_item_id": news_item_id, "status": "skipped_no_content"}

        # Classify message type
        msg_type = classify_message_type(content)
        if msg_type == "meme":
            await self._mark_news_terminal(news_item_id, "skipped")
            return {"news_item_id": news_item_id, "status": "skipped_meme"}
        if msg_type == "commentary" and len(content.strip()) < 100:
            await self._mark_news_terminal(news_item_id, "skipped")
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
            await self._mark_news_terminal(news_item_id, "non_signal")
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

        # Validate symbol exists in our instruments catalog
        if not await is_valid_db_instrument(shared_pool, symbol):
            logger.warning(
                "news_item_id=%s: text-extracted symbol %r is not a valid instrument. skipping.",
                news_item_id,
                symbol,
            )
            # Bug C2: mark terminal so we don't re-LLM this every 5 min forever.
            await self._mark_news_terminal(news_item_id, "skipped")
            return {"news_item_id": news_item_id, "status": "skipped_unresolved_instrument"}

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
            # Write a placeholder signal_interpretation so it doesn't get picked up again
            try:
                text_consensus = ConsensusResult(
                    direction=signal["direction"],
                    confidence=signal.get("confidence", 0.5),
                    method="text_dedup_continuation",
                    llm_result=None,
                    quant_result=None,
                )
                param_hash = _param_hash(
                    model="text_extractor",
                    primary_model="regex+llm",
                    fallback_model="dedup",
                    freshness_threshold=self.cfg.freshness_threshold_s,
                    max_age_hours=self.cfg.max_age_hours,
                )
                await write_signal_interpretation(
                    shared_pool=shared_pool,
                    news_item_id=news_item_id,
                    media_item_id=None,
                    trader_profile_id=trader_profile_id,
                    consensus=text_consensus,
                    param_hash=param_hash,
                    candle_data_hash="n/a",
                    instrument_symbol=symbol,
                    exchange=exchange,
                    market_data_fresh=False,
                    market_data_at=datetime.now(timezone.utc),
                    instrument_resolved_from="message",
                )
            except Exception as e:
                logger.warning("Failed to write duplicate placeholder interpretation for news_item_id=%s: %s", news_item_id, e)
                return {
                    "news_item_id": news_item_id,
                    "status": "failed",
                    "reason": f"Failed to write duplicate placeholder: {e}",
                }

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
        if news_row.get("context_window"):
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
        # Bug A round-3 review fix (BH1 #1, BH2 #1, CA1 §FixA): variables MUST
        # be initialised BEFORE the `if sig_id is not None:` guard, otherwise
        # when `write_signal_interpretation` returns None (ON CONFLICT DO
        # NOTHING — see line ~1990), the references at the rollback / status
        # / return blocks below trigger NameError and crash the entire
        # text-processing loop. The previous round-3 patch placed these
        # inside the guarded block — that was the root NameError.
        text_position_id: Optional[int] = None
        text_position_failed = False
        if sig_id is not None:
            try:
                text_symbol = symbol or signal.get("symbol", "UNKNOWN")
                text_levels: Dict[str, Any] = {
                    "trader_entry": signal.get("entry"),
                    "llm_entry": None,
                }
                text_entry_p, text_entry_src = await _resolve_entry_price(
                    shared_pool, text_levels, text_symbol or "", exchange or "bybit",
                )
                text_position_id = await create_tracked_position_from_interpretation(
                    shared_pool=shared_pool,
                    signal_interpretation_id=sig_id,
                    news_item_id=news_item_id,
                    media_item_id=None,
                    trader_profile_id=trader_profile_id,
                    instrument_symbol=text_symbol,
                    instrument_exchange=exchange or "bybit",
                    direction=signal["direction"],
                    entry_price=text_entry_p,
                    stop_loss=_parse_price_level(signal.get("stop_loss")),
                    take_profit_1=_parse_price_level((signal.get("take_profits") or [None])[0]),
                    detection_method="text_extraction",
                    detection_confidence=signal.get("confidence", 0.5),
                    # Bug 12 sibling — strip the Discord reply prefix from
                    # text-only signal persistence too. Without this, the
                    # parent's quoted text leaks into raw_signal_text and
                    # entry_reason_trader, polluting postmortems and the UI.
                    raw_signal_text=strip_reply_prefix(content)[:2000] if content else "",
                    company_id=company,
                    entry_reason_trader=(
                        strip_reply_prefix(content)[:2000] if content else None
                    ),
                    entry_price_source=text_entry_src,
                )
            except Exception as exc:
                # Bug Hunter 1 §H3 fix:
                #   Previously this swallowed the exception and the function
                #   still returned `status='analyzed'` to the caller. Result:
                #   the dashboard showed the signal as "analyzed", but no
                #   `tracked_position` row existed → no monitoring, no P&L,
                #   no postmortem. We now flag the failure and surface it in
                #   the result so callers (and the daemon's log line) see it.
                text_position_failed = True
                logger.warning(
                    "Failed to create tracked_position for text sig_id=%s: %s",
                    sig_id, exc,
                )

        # Bug A fix (2026-05-24 second-round audit):
        #   When position creation fails AFTER signal_interpretations was
        #   already INSERTed, the row becomes an "orphan signal" — the queue's
        #   `NOT EXISTS (signal_interpretations …)` check then sees the news
        #   item as already processed and skips it forever, even though no
        #   tracked_position exists. A transient DB hiccup turns into permanent
        #   signal loss.
        #
        #   Fix: roll back the orphan signal_interpretations row on text-path
        #   failure so the news_item is eligible for retry on the next cycle.
        #   Text extraction is cheap (regex + light LLM), so retries are not a
        #   cost concern. We deliberately do NOT apply the same rollback to the
        #   media path: vision LLM is expensive and the analysis itself is
        #   still valid even when no tracked_position is created — the orphan
        #   there is data the operator can act on.
        if text_position_failed and sig_id is not None:
            try:
                async with shared_pool.acquire() as conn:
                    deleted = await conn.execute(
                        "DELETE FROM public.signal_interpretations "
                        "WHERE id = $1 "
                        "  AND NOT EXISTS ("
                        "    SELECT 1 FROM public.tracked_positions tp "
                        "    WHERE tp.signal_interpretation_id = $1"
                        "  )",
                        sig_id,
                    )
                logger.warning(
                    "Bug A: rolled back orphan signal_interpretation id=%s for "
                    "news_item_id=%s after position creation failure (delete=%s); "
                    "news_item will be retried next cycle.",
                    sig_id, news_item_id, deleted,
                )
                sig_id = None
            except Exception as cleanup_exc:
                # Best-effort rollback. If the DELETE itself fails, the orphan
                # remains and the news_item is permanently skipped — but we
                # surface this loud and clear so the operator knows.
                logger.error(
                    "Bug A: orphan signal_interpretation id=%s rollback FAILED "
                    "(news_item_id=%s): %s. News item will be permanently "
                    "skipped until manually fixed.",
                    sig_id, news_item_id, cleanup_exc,
                )

        logger.info(
            "Text signal written for news_item_id=%s: %s %s @ %.2f (position_id=%s)",
            news_item_id,
            signal["symbol"],
            signal["direction"],
            signal["entry"],
            text_position_id if text_position_id else "FAILED",
        )

        result_status = "analyzed"
        if text_position_failed:
            result_status = "analyzed_position_create_failed"
        elif text_position_id is None:
            # Position skipped intentionally (e.g. dedup) — distinguish from
            # a hard failure.
            result_status = "analyzed_no_position"

        return {
            "news_item_id": news_item_id,
            "status": result_status,
            "direction": signal["direction"],
            "confidence": signal.get("confidence", 0.5),
            "method": "text_extraction",
            "symbol": signal["symbol"],
            "entry": signal["entry"],
            "position_id": text_position_id,
        }

    async def run_cycle(self) -> Dict[str, Any]:
        """Fetch one batch, process each item, return summary stats.

        Processes both media items (images/charts) and text-only news items
        in parallel tracks.
        """
        shared_pool = await self._ensure_pool()

        # ── Slice 4: Feed hygiene ─────────────────────────────────────
        # Sweep non-image media and stale text-only news BEFORE fetching the
        # next batch so the queue view reflects reality. Failures here must
        # never block a cycle — they're best-effort housekeeping.
        unsupported_media_swept = 0
        stale_news_drained = 0
        local_media_cleaned = 0
        stale_analyzing_recovered = 0
        try:
            unsupported_media_swept = await cleanup_unsupported_media(
                shared_pool,
                max_age_hours=self.cfg.max_age_hours,
            )
        except Exception as exc:
            logger.warning("cleanup_unsupported_media failed: %s", exc)
        # Bug C3 companion — recover rows pinned at 'analyzing' by a crashed
        # worker. Bug H round-3 review fix (BH2 #6, CA1 Fix H #3, CA2 P1.2):
        # threshold raised to 60 minutes so a healthy worker on a slow vision
        # LLM (p99 ~16 min) is never falsely requeued and double-charged.
        try:
            stale_analyzing_recovered = await recover_stale_analyzing(
                shared_pool,
                stale_minutes=60,
            )
        except Exception as exc:
            logger.warning("recover_stale_analyzing failed: %s", exc)
        try:
            stale_news_drained = await cleanup_stale_pending_news(
                shared_pool,
                stale_after_hours=6.0,
            )
        except Exception as exc:
            logger.warning("cleanup_stale_pending_news failed: %s", exc)
        try:
            local_media_cleaned = await cleanup_expired_local_media(
                shared_pool,
                retention_days=self.cfg.media_retention_days,
            )
        except Exception as exc:
            logger.warning("cleanup_expired_local_media failed: %s", exc)
        if unsupported_media_swept or stale_news_drained or local_media_cleaned or stale_analyzing_recovered:
            logger.info(
                "Feed hygiene: unsupported_media_swept=%d stale_news_drained=%d local_media_cleaned=%d stale_analyzing_recovered=%d",
                unsupported_media_swept,
                stale_news_drained,
                local_media_cleaned,
                stale_analyzing_recovered,
            )

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
                            expected_processed_at=row.get("claim_ts"),
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
        skipped_not_chart = sum(1 for r in media_results if r.get("status") == "skipped_not_chart")
        failed_media = sum(1 for r in media_results if r.get("status") == "failed")
        analyzed_text = sum(1 for r in text_results if r.get("status") == "analyzed")
        failed_text = sum(1 for r in text_results if r.get("status") == "failed")
        # Round-6 sweep (BH2 #7): the text path can return a third terminal
        # status — `analyzed_position_create_failed` — when signal_interp was
        # written but the tracked_position INSERT raised. Round-3 Fix A rolls
        # back the orphan sig_interp so the news_item is retriable, but the
        # operator had no metric for "how often is this happening" — count it.
        position_create_failed_text = sum(
            1 for r in text_results
            if r.get("status") == "analyzed_position_create_failed"
        )

        logger.info(
            "Interpretation cycle complete: media(analyzed=%d skipped_vision=%d "
            "skipped_not_chart=%d failed=%d) text(analyzed=%d failed=%d "
            "position_create_failed=%d) hygiene(unsupported=%d stale_news=%d)",
            analyzed_media,
            skipped_media,
            skipped_not_chart,
            failed_media,
            analyzed_text,
            failed_text,
            position_create_failed_text,
            unsupported_media_swept,
            stale_news_drained,
        )
        return {
            "processed": len(media_results) + len(text_results),
            "analyzed_media": analyzed_media,
            "skipped_media": skipped_media,
            "skipped_not_chart": skipped_not_chart,
            "failed_media": failed_media,
            "analyzed_text": analyzed_text,
            "failed_text": failed_text,
            "position_create_failed_text": position_create_failed_text,
            "unsupported_media_swept": unsupported_media_swept,
            "stale_news_drained": stale_news_drained,
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
            "  n.instruments, n.collected_at, n.context_window "
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
