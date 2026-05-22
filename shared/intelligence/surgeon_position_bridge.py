"""
Module: surgeon_position_bridge
Purpose: Forward-bridge Surgeon (v1 file-based) and Surgeon2 (v2 PG-backed)
         positions into the shared tickles_shared.public.tracked_positions
         ledger so they appear alongside trader-derived positions in the
         dashboard's Position tab.
Location: /opt/tickles/shared/intelligence/surgeon_position_bridge.py

Notes:
- Idempotent on (company_id, instrument_symbol_normalised, direction,
  status='open', source) — if an open mirror already exists, we update its
  fields in place rather than insert a duplicate.
- Auto-creates trader_profiles rows for `agent_surgeon` / `agent_surgeon2`
  on first call (memoised). Mirrors the chart_hacker pattern in
  interpretation_service._get_chart_hacker_profile_id.
- Uses async asyncpg via the shared DatabasePool. Callers from sync code
  (Surgeon v1/v2) should wrap calls in `asyncio.run(...)` or schedule on
  an existing loop — see `bridge_surgeon_position_open_sync` /
  `bridge_surgeon_position_close_sync` shims at the bottom of this module.
- Detection method is always 'quant_pattern' (CHECK constraint allows it).
- news_item_id is NOT NULL on the table; we use 0 as a sentinel for
  surgeon-originated positions (no upstream news row exists).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import psycopg2
import psycopg2.extras

from shared.utils.db import DatabasePool, get_shared_pool
from shared.utils.instrument_normaliser import normalise_instrument

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SURGEON_DETECTION_METHOD = "quant_pattern"
SURGEON_NEWS_ITEM_SENTINEL = 0
DEFAULT_SURGEON_NOTIONAL_USD = 1000.0

# Source tag — distinguishes Surgeon v1 vs v2 mirrors so the reconciler and
# dashboard can tell them apart. Stored under metadata.source.
SOURCE_SURGEON_V1 = "surgeon_v1"
SOURCE_SURGEON_V2 = "surgeon_v2"

# Memoised trader_profiles.id values, keyed by handle_normalized.
_PROFILE_ID_CACHE: Dict[str, int] = {}

# Sync psycopg2 connection cache (module-level, reused across calls).
_sync_conn: Optional[Any] = None


# ---------------------------------------------------------------------------
# Sync DB helpers (psycopg2 — used by surgeon daemons which are pure sync)
# ---------------------------------------------------------------------------

def _get_sync_conn():
    """Return a cached psycopg2 connection to tickles_shared, creating it on first call."""
    global _sync_conn
    if _sync_conn is not None and not _sync_conn.closed:
        try:
            with _sync_conn.cursor() as cur:
                cur.execute("SELECT 1")
            return _sync_conn
        except Exception:
            _sync_conn = None

    db_host = os.environ.get("DB_HOST", "127.0.0.1")
    db_port = int(os.environ.get("DB_PORT", "5432"))
    db_user = os.environ.get("DB_USER", "admin")
    db_pass = os.environ.get("DB_PASSWORD", "")
    db_name = os.environ.get("DB_NAME_SHARED", "tickles_shared")

    _sync_conn = psycopg2.connect(
        host=db_host,
        port=db_port,
        user=db_user,
        password=db_pass,
        dbname=db_name,
        connect_timeout=10,
    )
    _sync_conn.autocommit = True
    logger.debug("sync bridge: created psycopg2 connection to %s", db_name)
    return _sync_conn


def _get_or_create_surgeon_profile_id_sync(handle: str, display_name: str) -> Optional[int]:
    """Sync version of profile resolver using psycopg2."""
    cached = _PROFILE_ID_CACHE.get(handle)
    if cached is not None:
        return cached

    conn = _get_sync_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id FROM public.trader_profiles "
                "WHERE platform = %s AND handle_normalized = %s",
                ("api", handle),
            )
            row = cur.fetchone()
            if row:
                pid = int(row["id"])
                _PROFILE_ID_CACHE[handle] = pid
                return pid
    except Exception as exc:
        logger.warning("sync lookup surgeon profile (%s) failed: %s", handle, exc)
        return None

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO public.trader_profiles "
                "(platform, handle_raw, handle_normalized, display_name, trader_type) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (platform, handle_normalized) DO UPDATE "
                "    SET display_name = EXCLUDED.display_name, "
                "        last_seen_at = CURRENT_TIMESTAMP "
                "RETURNING id",
                ("api", handle, handle, display_name, "bot"),
            )
            row = cur.fetchone()
            if row:
                pid = int(row["id"])
                _PROFILE_ID_CACHE[handle] = pid
                return pid
    except Exception as exc:
        logger.warning("sync create surgeon profile (%s) failed: %s", handle, exc)

    return None


# ---------------------------------------------------------------------------
# Trader-profile resolver (auto-create + memoise)
# ---------------------------------------------------------------------------

async def _get_or_create_surgeon_profile_id(
    pool: DatabasePool, handle: str, display_name: str
) -> Optional[int]:
    """Resolve or create a trader_profiles row for an internal Surgeon agent.

    Args:
        pool: Shared database pool.
        handle: handle_normalized value (e.g. ``agent_surgeon``).
        display_name: Human-readable name for new rows.

    Returns:
        The trader_profiles.id, or None if creation failed.
    """
    cached = _PROFILE_ID_CACHE.get(handle)
    if cached is not None:
        return cached

    try:
        row = await pool.fetch_one(
            "SELECT id FROM public.trader_profiles "
            "WHERE platform = $1 AND handle_normalized = $2",
            ("api", handle),
        )
        if row:
            pid = int(row["id"])
            _PROFILE_ID_CACHE[handle] = pid
            return pid
    except Exception as exc:
        logger.warning("lookup surgeon profile (%s) failed: %s", handle, exc)
        return None

    # Insert; if a concurrent insert beat us, the ON CONFLICT clause re-selects.
    # Note: platform check-constraint allows {discord, telegram, twitter,
    # tradingview, rss, api, unknown}; we use 'api' for internal agents.
    # trader_type check-constraint allows {pro, amateur, bot, news, unknown};
    # we tag Surgeon agents as 'bot'.
    try:
        row = await pool.fetch_one(
            "INSERT INTO public.trader_profiles "
            "(platform, handle_raw, handle_normalized, display_name, trader_type) "
            "VALUES ($1, $2, $3, $4, $5) "
            "ON CONFLICT (platform, handle_normalized) DO UPDATE "
            "    SET display_name = EXCLUDED.display_name, "
            "        last_seen_at = CURRENT_TIMESTAMP "
            "RETURNING id",
            ("api", handle, handle, display_name, "bot"),
        )
        if row:
            pid = int(row["id"])
            _PROFILE_ID_CACHE[handle] = pid
            return pid
    except Exception as exc:
        logger.warning("create surgeon profile (%s) failed: %s", handle, exc)

    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _coerce_direction(side: str) -> Optional[str]:
    """Translate Surgeon's LONG/SHORT (or long/short) into the table's enum."""
    if not side:
        return None
    s = str(side).strip().lower()
    if s in ("long", "buy", "l"):
        return "long"
    if s in ("short", "sell", "s"):
        return "short"
    return None


def _utc_now() -> datetime:
    """Return the current UTC datetime."""
    return datetime.now(timezone.utc)


def _coerce_ts(ts: Any) -> datetime:
    """Coerce assorted timestamp shapes (str/datetime/None) to a tz-aware UTC datetime."""
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)
    if isinstance(ts, str):
        try:
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            logger.debug("could not parse ts=%r; using now()", ts)
    return _utc_now()


def _safe_float(value: Any) -> Optional[float]:
    """Coerce ``value`` to float; return None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Open bridge
# ---------------------------------------------------------------------------

async def bridge_surgeon_position_open(
    *,
    company_id: str,
    source: str,
    actor_id: str,
    symbol: str,
    side: str,
    entry_price: float,
    entry_ts: Any,
    notional_usd: Optional[float] = None,
    leverage: Optional[float] = None,
    sl: Optional[float] = None,
    tp1: Optional[float] = None,
    tp2: Optional[float] = None,
    tp3: Optional[float] = None,
    reason: Optional[str] = None,
    confidence: float = 0.7,
    extra_metadata: Optional[Dict[str, Any]] = None,
    exchange: str = "bybit",
) -> Optional[int]:
    """Idempotently mirror a Surgeon-opened position into tracked_positions.

    Args:
        company_id: Owning company slug (e.g. ``rubicon``).
        source: One of ``surgeon_v1`` / ``surgeon_v2`` (see module constants).
        actor_id: Logical actor instance (e.g. ``surgeon1`` / ``surgeon2``).
        symbol: Raw symbol (e.g. ``BTC`` or ``BTC/USDT:USDT``).
        side: ``LONG`` / ``SHORT`` (case-insensitive) — coerced to enum.
        entry_price: Filled entry price (post-slippage).
        entry_ts: Timestamp of the open (datetime / ISO str / None).
        notional_usd: Notional in USD; defaults to ``DEFAULT_SURGEON_NOTIONAL_USD``.
        leverage: Optional leverage multiplier.
        sl, tp1, tp2, tp3: Optional risk levels.
        reason: Human-readable reason string.
        confidence: Detection confidence in [0,1]; default 0.7 for quant signals.
        extra_metadata: Additional JSON-serialisable fields to merge into metadata.
        exchange: Exchange slug; default ``bybit``.

    Returns:
        tracked_positions.id of the inserted-or-updated row, or None on failure.
    """
    direction = _coerce_direction(side)
    if direction is None:
        logger.warning("bridge_open: invalid side=%r; skipping", side)
        return None

    entry_px = _safe_float(entry_price)
    if entry_px is None or entry_px <= 0:
        logger.warning("bridge_open: invalid entry_price=%r; skipping", entry_price)
        return None

    if not symbol or not str(symbol).strip():
        logger.warning("bridge_open: empty symbol; skipping")
        return None

    sym_raw = str(symbol).strip()
    sym_norm, venue_norm = normalise_instrument(sym_raw, exchange)
    sym_norm = sym_norm or sym_raw.upper()
    venue_norm = venue_norm or exchange or "bybit"

    signal_ts = _coerce_ts(entry_ts)

    handle_map = {
        SOURCE_SURGEON_V1: ("agent_surgeon", "Surgeon (Twilly v1)"),
        SOURCE_SURGEON_V2: ("agent_surgeon2", "Surgeon (Twilly v2)"),
    }
    if source not in handle_map:
        logger.warning("bridge_open: unknown source=%r; skipping", source)
        return None
    handle, display_name = handle_map[source]

    try:
        pool = await get_shared_pool()
    except Exception as exc:
        logger.error("bridge_open: cannot acquire shared pool: %s", exc)
        return None

    profile_id = await _get_or_create_surgeon_profile_id(pool, handle, display_name)
    if profile_id is None:
        logger.error("bridge_open: cannot resolve trader_profile for handle=%s", handle)
        return None

    metadata: Dict[str, Any] = {
        "source": source,
        "actor_id": actor_id,
        "bridge": "surgeon_position_bridge",
    }
    if extra_metadata:
        try:
            json.dumps(extra_metadata)
            metadata.update(extra_metadata)
        except (TypeError, ValueError):
            logger.warning("bridge_open: extra_metadata not JSON-serialisable; dropping")

    notional = _safe_float(notional_usd) or DEFAULT_SURGEON_NOTIONAL_USD

    # Idempotency check — does an open mirror already exist?
    try:
        existing = await pool.fetch_one(
            "SELECT id FROM public.tracked_positions "
            "WHERE company_id = $1 "
            "  AND instrument_symbol_normalised = $2 "
            "  AND direction = $3 "
            "  AND status = 'open' "
            "  AND actor_type = 'surgeon' "
            "  AND actor_id = $4 "
            "  AND metadata->>'source' = $5 "
            "ORDER BY id DESC LIMIT 1",
            (company_id, sym_norm, direction, actor_id, source),
        )
    except Exception as exc:
        logger.error("bridge_open: idempotency lookup failed: %s", exc)
        return None

    if existing:
        existing_id = int(existing["id"])
        try:
            await pool.execute(
                "UPDATE public.tracked_positions SET "
                "    entry_price = COALESCE(entry_price, $2), "
                "    stop_loss = COALESCE($3, stop_loss), "
                "    take_profit_1 = COALESCE($4, take_profit_1), "
                "    take_profit_2 = COALESCE($5, take_profit_2), "
                "    take_profit_3 = COALESCE($6, take_profit_3), "
                "    leverage = COALESCE($7, leverage), "
                "    notional_usd = $8, "
                "    metadata = metadata || $9::jsonb, "
                "    updated_at = NOW() "
                "WHERE id = $1",
                (
                    existing_id,
                    entry_px,
                    _safe_float(sl),
                    _safe_float(tp1),
                    _safe_float(tp2),
                    _safe_float(tp3),
                    _safe_float(leverage),
                    notional,
                    json.dumps(metadata),
                ),
            )
            logger.info(
                "bridge_open: refreshed existing tracked_position id=%s (%s %s %s)",
                existing_id, company_id, sym_norm, direction,
            )
            return existing_id
        except Exception as exc:
            logger.error("bridge_open: update existing failed id=%s: %s", existing_id, exc)
            return None

    # Insert new mirror row.
    try:
        row = await pool.fetch_one(
            "INSERT INTO public.tracked_positions ("
            "    news_item_id, trader_profile_id, "
            "    instrument_symbol, instrument_exchange, instrument_symbol_normalised, "
            "    direction, entry_price, stop_loss, "
            "    take_profit_1, take_profit_2, take_profit_3, "
            "    leverage, notional_usd, "
            "    detection_method, detection_confidence, "
            "    raw_signal_text, signal_timestamp, "
            "    status, company_id, "
            "    actor_type, actor_id, "
            "    entry_reason_agent, "
            "    metadata, entry_price_source"
            ") VALUES ("
            "    $1, $2, "
            "    $3, $4, $5, "
            "    $6, $7, $8, "
            "    $9, $10, $11, "
            "    $12, $13, "
            "    $14, $15, "
            "    $16, $17, "
            "    'open', $18, "
            "    'surgeon', $19, "
            "    $20, "
            "    $21::jsonb, $22"
            ") RETURNING id",
            (
                SURGEON_NEWS_ITEM_SENTINEL,
                profile_id,
                sym_raw.upper(),
                venue_norm,
                sym_norm,
                direction,
                entry_px,
                _safe_float(sl),
                _safe_float(tp1),
                _safe_float(tp2),
                _safe_float(tp3),
                _safe_float(leverage),
                notional,
                SURGEON_DETECTION_METHOD,
                float(confidence),
                (reason or "")[:2000],
                signal_ts,
                company_id,
                actor_id,
                (reason or "")[:2000],
                json.dumps(metadata),
                # entry_price_source — Surgeon-mirrored rows come from an actual
                # exchange fill, not a trader signal. Tagged "surgeon" so the
                # dashboard provenance pill and downstream filters can
                # distinguish fills from trader/LLM/quant-derived prices.
                "surgeon",
            ),
        )
        if row:
            new_id = int(row["id"])
            logger.info(
                "bridge_open: inserted tracked_position id=%s (%s %s %s src=%s)",
                new_id, company_id, sym_norm, direction, source,
            )
            return new_id
    except Exception as exc:
        logger.error("bridge_open: insert failed: %s", exc)

    return None


# ---------------------------------------------------------------------------
# Close bridge
# ---------------------------------------------------------------------------

async def bridge_surgeon_position_close(
    *,
    company_id: str,
    source: str,
    actor_id: str,
    symbol: str,
    side: str,
    exit_price: float,
    exit_ts: Any,
    realized_pnl_usd: Optional[float] = None,
    realized_pnl_pct: Optional[float] = None,
    exit_reason: Optional[str] = None,
    outcome: Optional[str] = None,
    exchange: str = "bybit",
) -> Optional[int]:
    """Idempotently close the matching open mirror in tracked_positions.

    Args:
        company_id: Owning company slug.
        source: ``surgeon_v1`` / ``surgeon_v2``.
        actor_id: Actor instance string.
        symbol: Raw symbol matching the open call.
        side: LONG/SHORT — coerced to enum.
        exit_price: Filled exit price.
        exit_ts: Timestamp of the close.
        realized_pnl_usd: Final realised PnL in USD.
        realized_pnl_pct: Final realised PnL in percent.
        exit_reason: Free-text reason / action tag.
        outcome: One of the table's outcome enum values, or None.
        exchange: Exchange slug.

    Returns:
        tracked_positions.id of the row that was closed, or None if no open
        mirror was found / on failure.
    """
    direction = _coerce_direction(side)
    if direction is None:
        logger.warning("bridge_close: invalid side=%r; skipping", side)
        return None

    if not symbol or not str(symbol).strip():
        logger.warning("bridge_close: empty symbol; skipping")
        return None

    sym_raw = str(symbol).strip()
    sym_norm, _ = normalise_instrument(sym_raw, exchange)
    sym_norm = sym_norm or sym_raw.upper()

    exit_px = _safe_float(exit_price)
    closed_at = _coerce_ts(exit_ts)

    valid_outcomes = {
        "tp1_hit", "tp2_hit", "tp3_hit", "sl_hit",
        "breakeven", "expired", "manual_close", "invalidated",
    }
    final_outcome: Optional[str] = None
    if outcome and outcome in valid_outcomes:
        final_outcome = outcome
    elif exit_reason:
        tag = exit_reason.upper()
        if tag.startswith("TP1"):
            final_outcome = "tp1_hit"
        elif tag.startswith("TP2"):
            final_outcome = "tp2_hit"
        elif tag.startswith("TP3"):
            final_outcome = "tp3_hit"
        elif tag.startswith("SL") or tag == "STOP":
            final_outcome = "sl_hit"
        elif tag.startswith("CONVERG"):
            final_outcome = "manual_close"
        elif tag.startswith("TIME"):
            final_outcome = "expired"
        elif tag.startswith("STALL"):
            final_outcome = "manual_close"

    try:
        pool = await get_shared_pool()
    except Exception as exc:
        logger.error("bridge_close: cannot acquire shared pool: %s", exc)
        return None

    try:
        existing = await pool.fetch_one(
            "SELECT id FROM public.tracked_positions "
            "WHERE company_id = $1 "
            "  AND instrument_symbol_normalised = $2 "
            "  AND direction = $3 "
            "  AND status = 'open' "
            "  AND actor_type = 'surgeon' "
            "  AND actor_id = $4 "
            "  AND metadata->>'source' = $5 "
            "ORDER BY id DESC LIMIT 1",
            (company_id, sym_norm, direction, actor_id, source),
        )
    except Exception as exc:
        logger.error("bridge_close: lookup failed: %s", exc)
        return None

    if not existing:
        logger.info(
            "bridge_close: no open mirror found for %s %s %s actor=%s src=%s",
            company_id, sym_norm, direction, actor_id, source,
        )
        return None

    pid = int(existing["id"])
    try:
        await pool.execute(
            "UPDATE public.tracked_positions SET "
            "    status = 'closed', "
            "    exit_price = COALESCE($2, exit_price), "
            "    exit_timestamp = $3, "
            "    closed_at = $3, "
            "    exit_reason = COALESCE($4, exit_reason), "
            "    exit_reason_system = COALESCE($4, exit_reason_system), "
            "    realized_pnl_usd = COALESCE($5, realized_pnl_usd), "
            "    realized_pnl_usd_final = COALESCE($5, realized_pnl_usd_final), "
            "    realized_pnl_pct = COALESCE($6, realized_pnl_pct), "
            "    outcome = COALESCE($7, outcome), "
            "    updated_at = NOW() "
            "WHERE id = $1",
            (
                pid,
                exit_px,
                closed_at,
                (exit_reason or "")[:2000] or None,
                _safe_float(realized_pnl_usd),
                _safe_float(realized_pnl_pct),
                final_outcome,
            ),
        )
        logger.info(
            "bridge_close: closed tracked_position id=%s (%s %s %s outcome=%s)",
            pid, company_id, sym_norm, direction, final_outcome,
        )
        return pid
    except Exception as exc:
        logger.error("bridge_close: update failed id=%s: %s", pid, exc)
        return None


# ---------------------------------------------------------------------------
# Sync bridge implementations (psycopg2) — Surgeon v1/v2 are pure sync code.
# These replace the asyncpg-based wrappers that failed due to event-loop issues.
# ---------------------------------------------------------------------------

def bridge_surgeon_position_open_sync(
    *,
    company_id: str,
    source: str,
    actor_id: str,
    symbol: str,
    side: str,
    entry_price: float,
    entry_ts: Any,
    notional_usd: Optional[float] = None,
    leverage: Optional[float] = None,
    sl: Optional[float] = None,
    tp1: Optional[float] = None,
    tp2: Optional[float] = None,
    tp3: Optional[float] = None,
    reason: Optional[str] = None,
    confidence: float = 0.7,
    extra_metadata: Optional[Dict[str, Any]] = None,
    exchange: str = "bybit",
) -> Optional[int]:
    """Sync idempotent mirror of a Surgeon-opened position into tracked_positions.

    Uses psycopg2 directly — no async event loop required.
    """
    direction = _coerce_direction(side)
    if direction is None:
        logger.warning("bridge_open_sync: invalid side=%r; skipping", side)
        return None

    entry_px = _safe_float(entry_price)
    if entry_px is None or entry_px <= 0:
        logger.warning("bridge_open_sync: invalid entry_price=%r; skipping", entry_price)
        return None

    if not symbol or not str(symbol).strip():
        logger.warning("bridge_open_sync: empty symbol; skipping")
        return None

    sym_raw = str(symbol).strip()
    sym_norm, venue_norm = normalise_instrument(sym_raw, exchange)
    sym_norm = sym_norm or sym_raw.upper()
    venue_norm = venue_norm or exchange or "bybit"

    signal_ts = _coerce_ts(entry_ts)

    handle_map = {
        SOURCE_SURGEON_V1: ("agent_surgeon", "Surgeon (Twilly v1)"),
        SOURCE_SURGEON_V2: ("agent_surgeon2", "Surgeon (Twilly v2)"),
    }
    if source not in handle_map:
        logger.warning("bridge_open_sync: unknown source=%r; skipping", source)
        return None
    handle, display_name = handle_map[source]

    profile_id = _get_or_create_surgeon_profile_id_sync(handle, display_name)
    if profile_id is None:
        logger.error("bridge_open_sync: cannot resolve trader_profile for handle=%s", handle)
        return None

    metadata: Dict[str, Any] = {
        "source": source,
        "actor_id": actor_id,
        "bridge": "surgeon_position_bridge",
    }
    if extra_metadata:
        try:
            json.dumps(extra_metadata)
            metadata.update(extra_metadata)
        except (TypeError, ValueError):
            logger.warning("bridge_open_sync: extra_metadata not JSON-serialisable; dropping")

    notional = _safe_float(notional_usd) or DEFAULT_SURGEON_NOTIONAL_USD

    conn = _get_sync_conn()

    # Idempotency check — does an open mirror already exist?
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id FROM public.tracked_positions "
                "WHERE company_id = %s "
                "  AND instrument_symbol_normalised = %s "
                "  AND direction = %s "
                "  AND status = 'open' "
                "  AND actor_type = 'surgeon' "
                "  AND actor_id = %s "
                "  AND metadata->>'source' = %s "
                "ORDER BY id DESC LIMIT 1",
                (company_id, sym_norm, direction, actor_id, source),
            )
            existing = cur.fetchone()
    except Exception as exc:
        logger.error("bridge_open_sync: idempotency lookup failed: %s", exc)
        return None

    if existing:
        existing_id = int(existing["id"])
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE public.tracked_positions SET "
                    "    entry_price = COALESCE(entry_price, %s), "
                    "    stop_loss = COALESCE(%s, stop_loss), "
                    "    take_profit_1 = COALESCE(%s, take_profit_1), "
                    "    take_profit_2 = COALESCE(%s, take_profit_2), "
                    "    take_profit_3 = COALESCE(%s, take_profit_3), "
                    "    leverage = COALESCE(%s, leverage), "
                    "    notional_usd = %s, "
                    "    metadata = metadata || %s::jsonb, "
                    "    updated_at = NOW() "
                    "WHERE id = %s",
                    (
                        entry_px,
                        _safe_float(sl),
                        _safe_float(tp1),
                        _safe_float(tp2),
                        _safe_float(tp3),
                        _safe_float(leverage),
                        notional,
                        json.dumps(metadata),
                        existing_id,
                    ),
                )
            logger.info(
                "bridge_open_sync: refreshed existing tracked_position id=%s (%s %s %s)",
                existing_id, company_id, sym_norm, direction,
            )
            return existing_id
        except Exception as exc:
            logger.error("bridge_open_sync: update existing failed id=%s: %s", existing_id, exc)
            return None

    # Insert new mirror row.
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO public.tracked_positions ("
                "    news_item_id, trader_profile_id, "
                "    instrument_symbol, instrument_exchange, instrument_symbol_normalised, "
                "    direction, entry_price, stop_loss, "
                "    take_profit_1, take_profit_2, take_profit_3, "
                "    leverage, notional_usd, "
                "    detection_method, detection_confidence, "
                "    raw_signal_text, signal_timestamp, "
                "    status, company_id, "
                "    actor_type, actor_id, "
                "    entry_reason_agent, "
                "    metadata, entry_price_source"
                ") VALUES ("
                "    %s, %s, "
                "    %s, %s, %s, "
                "    %s, %s, %s, "
                "    %s, %s, %s, "
                "    %s, %s, "
                "    %s, %s, "
                "    %s, %s, "
                "    'open', %s, "
                "    'surgeon', %s, "
                "    %s, "
                "    %s::jsonb, %s"
                ") RETURNING id",
                (
                    SURGEON_NEWS_ITEM_SENTINEL,
                    profile_id,
                    sym_raw.upper(),
                    venue_norm,
                    sym_norm,
                    direction,
                    entry_px,
                    _safe_float(sl),
                    _safe_float(tp1),
                    _safe_float(tp2),
                    _safe_float(tp3),
                    _safe_float(leverage),
                    notional,
                    SURGEON_DETECTION_METHOD,
                    float(confidence),
                    (reason or "")[:2000],
                    signal_ts,
                    company_id,
                    actor_id,
                    (reason or "")[:2000],
                    json.dumps(metadata),
                    "surgeon",
                ),
            )
            row = cur.fetchone()
            if row:
                new_id = int(row["id"])
                logger.info(
                    "bridge_open_sync: inserted tracked_position id=%s (%s %s %s src=%s)",
                    new_id, company_id, sym_norm, direction, source,
                )
                return new_id
    except Exception as exc:
        logger.error("bridge_open_sync: insert failed: %s", exc)

    return None


def bridge_surgeon_position_close_sync(
    *,
    company_id: str,
    source: str,
    actor_id: str,
    symbol: str,
    side: str,
    exit_price: float,
    exit_ts: Any,
    realized_pnl_usd: Optional[float] = None,
    realized_pnl_pct: Optional[float] = None,
    exit_reason: Optional[str] = None,
    outcome: Optional[str] = None,
    exchange: str = "bybit",
) -> Optional[int]:
    """Sync idempotent close of the matching open mirror in tracked_positions.

    Uses psycopg2 directly — no async event loop required.
    """
    direction = _coerce_direction(side)
    if direction is None:
        logger.warning("bridge_close_sync: invalid side=%r; skipping", side)
        return None

    if not symbol or not str(symbol).strip():
        logger.warning("bridge_close_sync: empty symbol; skipping")
        return None

    sym_raw = str(symbol).strip()
    sym_norm, _ = normalise_instrument(sym_raw, exchange)
    sym_norm = sym_norm or sym_raw.upper()

    exit_px = _safe_float(exit_price)
    closed_at = _coerce_ts(exit_ts)

    valid_outcomes = {
        "tp1_hit", "tp2_hit", "tp3_hit", "sl_hit",
        "breakeven", "expired", "manual_close", "invalidated",
    }
    final_outcome: Optional[str] = None
    if outcome and outcome in valid_outcomes:
        final_outcome = outcome
    elif exit_reason:
        tag = exit_reason.upper()
        if tag.startswith("TP1"):
            final_outcome = "tp1_hit"
        elif tag.startswith("TP2"):
            final_outcome = "tp2_hit"
        elif tag.startswith("TP3"):
            final_outcome = "tp3_hit"
        elif tag.startswith("SL") or tag == "STOP":
            final_outcome = "sl_hit"
        elif tag.startswith("CONVERG"):
            final_outcome = "manual_close"
        elif tag.startswith("TIME"):
            final_outcome = "expired"
        elif tag.startswith("STALL"):
            final_outcome = "manual_close"

    conn = _get_sync_conn()

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id FROM public.tracked_positions "
                "WHERE company_id = %s "
                "  AND instrument_symbol_normalised = %s "
                "  AND direction = %s "
                "  AND status = 'open' "
                "  AND actor_type = 'surgeon' "
                "  AND actor_id = %s "
                "  AND metadata->>'source' = %s "
                "ORDER BY id DESC LIMIT 1",
                (company_id, sym_norm, direction, actor_id, source),
            )
            existing = cur.fetchone()
    except Exception as exc:
        logger.error("bridge_close_sync: lookup failed: %s", exc)
        return None

    if not existing:
        logger.info(
            "bridge_close_sync: no open mirror found for %s %s %s actor=%s src=%s",
            company_id, sym_norm, direction, actor_id, source,
        )
        return None

    pid = int(existing["id"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE public.tracked_positions SET "
                "    status = 'closed', "
                "    exit_price = COALESCE(%s, exit_price), "
                "    exit_timestamp = %s, "
                "    closed_at = %s, "
                "    exit_reason = COALESCE(%s, exit_reason), "
                "    exit_reason_system = COALESCE(%s, exit_reason_system), "
                "    realized_pnl_usd = COALESCE(%s, realized_pnl_usd), "
                "    realized_pnl_usd_final = COALESCE(%s, realized_pnl_usd_final), "
                "    realized_pnl_pct = COALESCE(%s, realized_pnl_pct), "
                "    outcome = COALESCE(%s, outcome), "
                "    updated_at = NOW() "
                "WHERE id = %s",
                (
                    exit_px,
                    closed_at,
                    closed_at,
                    (exit_reason or "")[:2000] or None,
                    (exit_reason or "")[:2000] or None,
                    _safe_float(realized_pnl_usd),
                    _safe_float(realized_pnl_usd),
                    _safe_float(realized_pnl_pct),
                    final_outcome,
                    pid,
                ),
            )
        logger.info(
            "bridge_close_sync: closed tracked_position id=%s (%s %s %s outcome=%s)",
            pid, company_id, sym_norm, direction, final_outcome,
        )
        return pid
    except Exception as exc:
        logger.error("bridge_close_sync: update failed id=%s: %s", pid, exc)
        return None
