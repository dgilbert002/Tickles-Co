"""
Module: position_monitor
Purpose: Fixed daemon that continuously monitors open tracked_positions,
         computes live P&L, distance-to-SL/TP, time metrics, and writes
         position_updates snapshots. Detects SL/TP hits and updates outcomes.
Location: /opt/tickles/shared/intelligence/position_monitor.py

Design:
  * Polls tickles_shared.tracked_positions for status IN ('open','partial_close').
  * For each position, reads the latest candle from public.candles for the
    resolved instrument + timeframe (default '1m').
  * Computes: unrealized_pnl_usd, pnl_pct, distance_to_sl, distance_to_tp,
    hours_open, mae_pct, mfe_pct.
  * Checks if current price has crossed SL or TP — if so, updates status
    to 'closed' and outcome to 'stop_loss' or 'take_profit'.
  * Writes a position_updates row for every snapshot.
  * Every N cycles, generates an agent_opinion row for positions with
    sufficient history (ChartHacker's parallel analysis).
  * Respects position_monitor_poll_seconds from system_config (default 60).
  * Idempotent: position_updates are append-only; no UNIQUE constraints.

Hardening:
  * Graceful handling of missing candle data (skip snapshot for that position).
  * SIGTERM/SIGINT graceful shutdown.
  * Batch processing to avoid long-running transactions.
  * Exponential backoff on DB errors.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ensure shared imports resolve
_HERE = Path(__file__).resolve().parent
_SHARED = _HERE.parent
_ROOT = _SHARED.parent
for p in (_ROOT, _SHARED):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from shared.utils.config import load_env
from shared.utils.db import DatabasePool, get_shared_pool
from shared.utils.instrument_normaliser import normalise_venue, to_canonical_symbol

from shared.intelligence.fee_calc import (
    RealizedPnlBreakdown,
    compute_realized_pnl_db,
)
from shared.intelligence.position_quant import (
    check_sl_tp_hit,
    compute_distance_to_sl_tp,
    compute_max_adverse_excursion,
    compute_max_favorable_excursion,
    compute_pnl,
    compute_pnl_pct,
    compute_risk_reward_ratio,
    compute_time_metrics,
)

logger = logging.getLogger("tickles.intelligence.position_monitor")

# ---------------------------------------------------------------------------
# Config (env-driven, no hardcodes)
# ---------------------------------------------------------------------------
POLL_INTERVAL_S = float(os.environ.get("POSITION_MONITOR_POLL_S", "60"))
BATCH_SIZE = int(os.environ.get("POSITION_MONITOR_BATCH", "50"))
AGENT_OPINION_EVERY_N = int(os.environ.get("POSITION_MONITOR_OPINION_EVERY", "10"))
DEFAULT_TIMEFRAME = os.environ.get("POSITION_MONITOR_TF", "1m")
# 2026-05-22 — Activation recency bound. The candle that triggers a pending
# -> open transition must be NEWER than ``now - activation_lookback_s``. This
# stops the monitor from "catching up" on touches that happened during
# previous downtime (the agent wasn't watching → it doesn't get the fill).
# Default 30 minutes — generous enough to survive a single restart, tight
# enough that day-old touches don't activate retroactively.
ACTIVATION_LOOKBACK_S = float(os.environ.get("POSITION_MONITOR_ACTIVATION_LOOKBACK_S", "1800"))


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass
class MonitorConfig:
    """Runtime configuration for the position monitor."""

    poll_interval_s: float = POLL_INTERVAL_S
    batch_size: int = BATCH_SIZE
    agent_opinion_every_n: int = AGENT_OPINION_EVERY_N
    default_timeframe: str = DEFAULT_TIMEFRAME
    activation_lookback_s: float = ACTIVATION_LOOKBACK_S


@dataclass
class PositionSnapshot:
    """Computed metrics for a single position at a point in time."""

    position_id: int
    current_price: float
    unrealized_pnl: float
    pnl_pct: float
    distance_to_entry_pct: float
    distance_to_sl: Optional[float]
    distance_to_sl_pct: Optional[float]
    distance_to_tp: Optional[float]
    distance_to_tp_pct: Optional[float]
    hours_open: float
    mae_pct: float
    mfe_pct: float
    rr_ratio: Optional[float]
    sl_hit: bool
    tp_hit: bool
    snapshot_time: datetime


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------
async def fetch_open_positions(
    pool: DatabasePool, batch_size: int
) -> List[Dict[str, Any]]:
    """Fetch open or partially-exited positions from tracked_positions.

    F2: SELECT extended with ``expiry_at`` and ``signal_timestamp`` so
    ``_process_one`` can detect time-based expiry and feed an accurate
    open timestamp into the D7 funding-day calculation.

    Status filter aligned with the schema check constraint, which permits
    only ``'open' | 'partial_exit' | 'closed' | 'expired' | 'invalidated'
    | 'cancelled'`` — the previous ``'partial_close'`` filter was a dead
    string that never matched any row.

    Args:
        pool: Shared Postgres pool.
        batch_size: Max rows to fetch.

    Returns:
        List of position row dicts.
    """
    return await pool.fetch_all(
        """
        SELECT id, trader_profile_id, signal_interpretation_id,
               instrument_symbol, epic_code, instrument_exchange,
               direction, entry_price, position_size, leverage,
               stop_loss, take_profit_1,
               status, created_at, updated_at,
               lowest_price, highest_price,
               expiry_at, signal_timestamp,
               notional_usd
        FROM public.tracked_positions
        WHERE status IN ('open', 'partial_exit')
        ORDER BY created_at DESC
        LIMIT $1
        """,
        (batch_size,),
    )


_INSTRUMENT_ID_CACHE = {}

async def _resolve_instrument_id(
    pool: DatabasePool,
    raw_symbol: str,
    raw_exchange: Optional[str],
) -> Optional[int]:
    cache_key = (raw_symbol, raw_exchange)
    if cache_key in _INSTRUMENT_ID_CACHE:
        return _INSTRUMENT_ID_CACHE[cache_key]
    res = await _resolve_instrument_id_impl(pool, raw_symbol, raw_exchange)
    if res is not None:
        _INSTRUMENT_ID_CACHE[cache_key] = res
    return res


async def _resolve_instrument_id_impl(
    pool: DatabasePool,
    raw_symbol: str,
    raw_exchange: Optional[str],
) -> Optional[int]:
    """Resolve a tracked_positions symbol/exchange pair to instruments.id.

    Resolution order (D6 — slash-form canonical, no ambiguity):
      1. Canonicalise inputs via to_canonical_symbol/normalise_venue.
      2. Exact match on instruments(symbol, exchange) using canonical values.
      3. Fallback to instrument_aliases.alias_value scoped by exchange.
      4. Fallback to instrument_aliases.alias_value any-exchange (lowest id).
      5. Fallback to instruments.symbol any-exchange match (legacy data).

    Args:
        pool: Shared Postgres pool.
        raw_symbol: Symbol as stored on the position (may be legacy form).
        raw_exchange: Exchange as stored on the position (may be alias form).

    Returns:
        instruments.id or None when no resolution is possible.
    """
    canonical_symbol = to_canonical_symbol(raw_symbol)
    canonical_exchange = normalise_venue(raw_exchange) if raw_exchange else ""

    if not canonical_symbol:
        return None

    try:
        if canonical_exchange:
            row = await pool.fetch_one(
                """
                SELECT id FROM public.instruments
                WHERE symbol = $1 AND exchange = $2 AND is_active = TRUE
                ORDER BY id
                LIMIT 1
                """,
                (canonical_symbol, canonical_exchange),
            )
            if row:
                return int(row["id"])

            row = await pool.fetch_one(
                """
                SELECT a.instrument_id AS id
                FROM public.instrument_aliases a
                JOIN public.instruments i ON i.id = a.instrument_id
                WHERE a.alias_value = $1
                  AND i.exchange = $2
                  AND i.is_active = TRUE
                ORDER BY a.instrument_id
                LIMIT 1
                """,
                (canonical_symbol, canonical_exchange),
            )
            if row:
                return int(row["id"])

            # Legacy raw symbol (e.g. 'BTCUSDT') stored on the position
            # may not have been canonicalised yet — try it verbatim too.
            if raw_symbol and raw_symbol != canonical_symbol:
                row = await pool.fetch_one(
                    """
                    SELECT a.instrument_id AS id
                    FROM public.instrument_aliases a
                    JOIN public.instruments i ON i.id = a.instrument_id
                    WHERE a.alias_value = $1
                      AND i.exchange = $2
                      AND i.is_active = TRUE
                    ORDER BY a.instrument_id
                    LIMIT 1
                    """,
                    (raw_symbol, canonical_exchange),
                )
                if row:
                    return int(row["id"])

        # Any-exchange alias fallback
        row = await pool.fetch_one(
            """
            SELECT a.instrument_id AS id
            FROM public.instrument_aliases a
            JOIN public.instruments i ON i.id = a.instrument_id
            WHERE a.alias_value = $1 AND i.is_active = TRUE
            ORDER BY a.instrument_id
            LIMIT 1
            """,
            (canonical_symbol,),
        )
        if row:
            return int(row["id"])

        # Last-resort: any-exchange canonical-symbol match on instruments
        row = await pool.fetch_one(
            """
            SELECT id FROM public.instruments
            WHERE symbol = $1 AND is_active = TRUE
            ORDER BY id
            LIMIT 1
            """,
            (canonical_symbol,),
        )
        if row:
            return int(row["id"])
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception(
            "Instrument resolution failed for symbol=%s exchange=%s canonical=%s: %s",
            raw_symbol,
            raw_exchange,
            canonical_symbol,
            exc,
        )
        return None

    return None


async def fetch_latest_price(
    pool: DatabasePool,
    symbol: str,
    instrument_exchange: Optional[str],
    timeframe: str = DEFAULT_TIMEFRAME,
) -> Optional[float]:
    """Fetch the most recent close price for an instrument.

    Resolves the instrument via canonical slash-form symbol + alias table
    (see _resolve_instrument_id) before reading the latest candle. This
    repairs Bug C — the symbol-format mismatch that defeated the JOIN
    when positions were stored as 'BTCUSDT' but candles indexed
    'BTC/USDT'.

    Args:
        pool: Shared Postgres pool.
        symbol: Instrument symbol as stored on the position.
        instrument_exchange: Exchange / venue as stored on the position.
        timeframe: Candle timeframe (default from env).

    Returns:
        Latest close price, or None if the instrument cannot be resolved
        or no candles exist for the timeframe.
    """
    instrument_id = await _resolve_instrument_id(pool, symbol, instrument_exchange)
    if instrument_id is None:
        logger.debug(
            "fetch_latest_price: unresolved instrument symbol=%s exchange=%s tf=%s",
            symbol,
            instrument_exchange,
            timeframe,
        )
        return None

    try:
        row = await pool.fetch_one(
            """
            SELECT close
            FROM public.candles
            WHERE instrument_id = $1 AND timeframe = $2
            ORDER BY "timestamp" DESC
            LIMIT 1
            """,
            (instrument_id, timeframe),
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception(
            "fetch_latest_price: candle query failed instrument_id=%s tf=%s: %s",
            instrument_id,
            timeframe,
            exc,
        )
        return None

    if row:
        return float(row["close"])
    return None


def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def _find_sl_tp_wick_candle(
    pool: DatabasePool,
    *,
    symbol: str,
    exchange: Optional[str],
    timeframe: str,
    direction: str,
    stop_loss: Optional[float],
    take_profit: Optional[float],
    since: datetime,
    max_candles: int = 10000,
    adapters: Optional[Dict[str, Any]] = None,
) -> Optional[Tuple[datetime, float, str, float]]:
    """Find the FIRST 1m candle since ``since`` whose ``[low, high]`` range
    wicked through SL or TP.

    Mirrors the per-candle scan ``copy_trade_monitor`` uses for the bot
    SL/TP checks. SL is evaluated BEFORE TP within the same candle
    (conservative trader convention — if a 1m candle prints both SL and
    TP, we assume the SL fired first, since OHLC order is ambiguous
    intra-candle).

    Args:
        pool: Shared Postgres pool.
        symbol: Position's instrument_symbol.
        exchange: Position's instrument_exchange (may be None).
        timeframe: Candle timeframe (typically ``'1m'``).
        direction: ``'long'`` or ``'short'``.
        stop_loss: SL price (may be None — that path is skipped).
        take_profit: TP price (may be None — that path is skipped).
        since: Only candles strictly newer than this are considered.
        max_candles: Hard cap on rows fetched (default 10000 = ~7 days
            of 1m). Backfill jobs may pass a higher value.
        adapters: Optional dictionary to cache and reuse CCXTAdapter connections.

    Returns:
        ``(timestamp, close, hit_type, hit_price)`` where ``hit_type``
        is ``'sl'`` or ``'tp'`` and ``hit_price`` is the SL/TP level
        that was wicked (this becomes the fill price for ``_settle_close``,
        which is what the trader's broker would have given them — NOT
        the candle close, which can lie about intra-candle highs/lows).
        Returns ``None`` if no candle since ``since`` wicked either level.
    """
    if direction not in ("long", "short"):
        return None
    if stop_loss is None and take_profit is None:
        return None
    instrument_id = await _resolve_instrument_id(pool, symbol, exchange)
    if instrument_id is None:
        return None
    since_utc = ensure_utc(since)
    try:
        rows = await pool.fetch_all(
            """
            SELECT "timestamp", high, low, close
            FROM public.candles
            WHERE instrument_id = $1
              AND timeframe = $2
              AND "timestamp" >= $3
            ORDER BY "timestamp" ASC
            LIMIT $4
            """,
            (instrument_id, timeframe, since_utc, max_candles),
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception(
            "_find_sl_tp_wick_candle query failed instrument_id=%s tf=%s: %s",
            instrument_id, timeframe, exc,
        )
        rows = []

    if not rows:
        # Fallback: Query historical candle data (OHLCV) on-the-fly from the exchange via CCXT
        try:
            from shared.connectors.ccxt_adapter import CCXTAdapter
            from shared.market_data.live_price import _candidate_symbols
            exchange_id = exchange or "bybit"
            logger.info(
                "_find_sl_tp_wick_candle: Local candles missing for %s since %s. "
                "Falling back to CCXT OHLCV fetch via %s adapter.",
                symbol, since_utc, exchange_id
            )
            
            is_cached = False
            if adapters is not None and exchange_id in adapters:
                adapter = adapters[exchange_id]
                is_cached = True
            else:
                adapter = CCXTAdapter(exchange_id)
                if adapters is not None:
                    adapters[exchange_id] = adapter
                    is_cached = True
                    
            try:
                candidates = _candidate_symbols(symbol)
                ccxt_candles = []
                last_err = None
                for cand_sym in candidates:
                    try:
                        ccxt_candles = await adapter.fetch_ohlcv(
                            symbol=cand_sym,
                            timeframe=timeframe,
                            since=since_utc,
                            limit=1000
                        )
                        if ccxt_candles:
                            logger.debug("Successfully fetched candles for %s using symbol form %s", symbol, cand_sym)
                            break
                    except Exception as sym_exc:
                        last_err = sym_exc
                        continue
                if not ccxt_candles and last_err is not None:
                    raise last_err

                if ccxt_candles:
                    logger.debug("Successfully fetched %d candles from CCXT for %s", len(ccxt_candles), symbol)
                    rows = []
                    for c in ccxt_candles:
                        rows.append({
                            "timestamp": c.timestamp,
                            "high": float(c.high),
                            "low": float(c.low),
                            "close": float(c.close)
                        })
            finally:
                if not is_cached:
                    await adapter.close()
        except Exception as fallback_exc:
            logger.warning(
                "_find_sl_tp_wick_candle CCXT fallback failed for %s: %s",
                symbol, fallback_exc
            )

    for r in rows:
        hi = float(r["high"])
        lo = float(r["low"])
        close_v = float(r["close"])
        if direction == "long":
            # Long: SL is BELOW entry → wick down through SL = stop hit.
            # TP is ABOVE entry → wick up through TP = profit hit.
            if stop_loss is not None and lo <= stop_loss:
                return r["timestamp"], close_v, "sl", float(stop_loss)
            if take_profit is not None and hi >= take_profit:
                return r["timestamp"], close_v, "tp", float(take_profit)
        else:
            # Short: SL is ABOVE entry → wick up through SL = stop hit.
            # TP is BELOW entry → wick down through TP = profit hit.
            if stop_loss is not None and hi >= stop_loss:
                return r["timestamp"], close_v, "sl", float(stop_loss)
            if take_profit is not None and lo <= take_profit:
                return r["timestamp"], close_v, "tp", float(take_profit)
    return None


async def _find_entry_touch_candle(
    pool: DatabasePool,
    *,
    symbol: str,
    exchange: Optional[str],
    timeframe: str,
    entry: float,
    since: datetime,
    not_before: Optional[datetime] = None,
    adapters: Optional[Dict[str, Any]] = None,
) -> Optional[Tuple[datetime, float]]:
    """Find the FIRST 1m candle (strictly newer than ``since`` AND newer
    than ``not_before`` if supplied) whose ``[low, high]`` range contains
    the entry price.

    Used by :meth:`PositionMonitor._activate_pending_positions` to detect
    a genuine entry touch. Same predicate ``copy_trade_monitor`` uses for
    SL/TP — works for ANY direction (long/short) and ANY entry style
    (limit-below, breakout-above, limit-above, breakdown-below).

    Args:
        pool: Shared Postgres pool.
        symbol: Position's instrument_symbol (slash form, e.g. ``BTC/USDT``).
        exchange: Position's instrument_exchange (may be None).
        timeframe: Candle timeframe to scan (typically ``'1m'``).
        entry: Trader's anticipated entry price.
        since: Position's ``created_at`` — only candles AFTER this count.
            Prevents retro-fitting from historical candles created before
            the trader posted the chart.
        not_before: Recency cutoff (e.g. ``now - activation_lookback_s``).
            If supplied, only candles whose ``timestamp >= not_before``
            are eligible. Use this to enforce a "live watcher" semantic
            so the bot does NOT retroactively claim entry on a candle
            that happened while it was offline.
        adapters: Optional dictionary to cache and reuse CCXTAdapter connections.

    Returns:
        ``(candle_timestamp, candle_close)`` of the first eligible
        touching candle, or ``None`` if no such candle exists yet
        (position should stay pending). The trader's anticipated ``entry``
        on the row is left untouched so downstream P&L math still
        references it; the candle close becomes the position's
        ``current_price`` at activation time.
    """
    instrument_id = await _resolve_instrument_id(pool, symbol, exchange)
    if instrument_id is None:
        return None
    since_utc = ensure_utc(since)
    not_before_utc = ensure_utc(not_before)
    # The effective floor for the timestamp search is the LATER of
    # ``since`` (anti-historical-retrofit) and ``not_before`` (recency).
    floor_ts = since_utc
    if not_before_utc is not None and not_before_utc > since_utc:
        floor_ts = not_before_utc
    try:
        row = await pool.fetch_one(
            """
            SELECT "timestamp", high, low, close
            FROM public.candles
            WHERE instrument_id = $1
              AND timeframe = $2
              AND "timestamp" > $3
              AND low <= $4
              AND high >= $4
            ORDER BY "timestamp" ASC
            LIMIT 1
            """,
            (instrument_id, timeframe, floor_ts, entry),
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception(
            "_find_entry_touch_candle query failed instrument_id=%s tf=%s: %s",
            instrument_id, timeframe, exc,
        )
        row = None

    if row is None:
        try:
            from shared.connectors.ccxt_adapter import CCXTAdapter
            from shared.market_data.live_price import _candidate_symbols
            exchange_id = exchange or "bybit"
            logger.info(
                "_find_entry_touch_candle: Local touch not found for %s. "
                "Checking CCXT OHLCV fallback via %s.",
                symbol, exchange_id
            )
            
            is_cached = False
            if adapters is not None and exchange_id in adapters:
                adapter = adapters[exchange_id]
                is_cached = True
            else:
                adapter = CCXTAdapter(exchange_id)
                if adapters is not None:
                    adapters[exchange_id] = adapter
                    is_cached = True
            
            try:
                candidates = _candidate_symbols(symbol)
                ccxt_candles = []
                last_err = None
                for cand_sym in candidates:
                    try:
                        ccxt_candles = await adapter.fetch_ohlcv(
                            symbol=cand_sym,
                            timeframe=timeframe,
                            since=floor_ts,
                            limit=1000
                        )
                        if ccxt_candles:
                            logger.debug("Successfully fetched candles for %s using symbol form %s", symbol, cand_sym)
                            break
                    except Exception as sym_exc:
                        last_err = sym_exc
                        continue
                if not ccxt_candles and last_err is not None:
                    raise last_err

                if ccxt_candles:
                    logger.debug("Fetched %d candles from CCXT for %s entry check", len(ccxt_candles), symbol)
                    for c in ccxt_candles:
                        hi = float(c.high)
                        lo = float(c.low)
                        if lo <= entry <= hi:
                            logger.info(
                                "Entry touch found via CCXT OHLCV fallback for %s at %s (entry=%.6f, low=%.6f, high=%.6f)",
                                symbol, c.timestamp, entry, lo, hi
                            )
                            return c.timestamp, float(c.close)
            finally:
                if not is_cached:
                    await adapter.close()
        except Exception as fallback_exc:
            logger.warning(
                "_find_entry_touch_candle CCXT fallback failed for %s: %s",
                symbol, fallback_exc
            )
        return None

    return row["timestamp"], float(row["close"])


async def fetch_latest_price_by_epic(
    pool: DatabasePool,
    epic: str,
    timeframe: str = DEFAULT_TIMEFRAME,
) -> Optional[float]:
    """Fetch latest price using Capital.com epic code.

    Args:
        pool: Shared Postgres pool.
        epic: Capital.com epic (e.g., 'CS.D.GBPUSD.CFD.IP').
        timeframe: Candle timeframe.

    Returns:
        Latest close price, or None.
    """
    row = await pool.fetch_one(
        """
        SELECT close
        FROM public.candles
        WHERE instrument_id = (
            SELECT id FROM public.instruments
            WHERE metadata->>'epic' = $1 AND is_active = TRUE
            LIMIT 1
        )
        AND timeframe = $2
        ORDER BY "timestamp" DESC
        LIMIT 1
        """,
        (epic, timeframe),
    )
    if row:
        return float(row["close"])
    return None


async def fetch_live_price_from_feed(symbol: str) -> Optional[float]:
    """Fetch the latest price for *symbol* from the price feed daemon.

    Connects to the daemon's internal WebSocket, requests the latest
    cached price, and disconnects. Falls back to None if the daemon
    is unreachable or the symbol is not subscribed.

    Args:
        symbol: Canonical symbol (e.g. ``"BTC/USDT"``).

    Returns:
        Latest price as float, or None.
    """
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect("ws://127.0.0.1:18790") as ws:
                await ws.send_json({"subscribe": [symbol]})
                try:
                    msg = await asyncio.wait_for(ws.receive_json(), timeout=3.0)
                    price = msg.get("price")
                    if price is not None:
                        return float(price)
                except asyncio.TimeoutError:
                    pass
    except Exception:
        pass
    return None


def _clamp_pct(value: Optional[float], max_abs: float = 1_000_000.0) -> float:
    """Clamp a percentage value to a sane range.

    Shitcoins with tiny entry prices (e.g. PEPE at 0.00000038, or AVAX
    with a chart-reading error at 0.00000394 instead of $9.40) can produce
    absurd distance/pnl percentages that overflow the numeric(14,4) columns.
    This clamp caps values to ±1,000,000% (10,000x) — far beyond any real
    trade outcome — preventing DB overflow while preserving the signal that
    "something is wrong with this entry price."
    """
    if value is None:
        return 0.0
    if value > max_abs:
        return max_abs
    if value < -max_abs:
        return -max_abs
    return value


def _build_snapshot(
    position: Dict[str, Any],
    current_price: float,
    now: datetime,
) -> Optional[PositionSnapshot]:
    """Compute all metrics for a position at the current price.

    F7 — NULL guard: returns ``None`` when entry_price or position_size
    is missing on the source row (D8 orphan signals). The caller MUST
    treat ``None`` as "missing_levels" — skip writing a snapshot, do
    not crash, do not silently fabricate values. Backfill of these
    orphan rows is the responsibility of F9 (backfill script).

    Args:
        position: Row dict from tracked_positions.
        current_price: Current market price.
        now: Snapshot timestamp.

    Returns:
        PositionSnapshot with all computed fields, or ``None`` when the
        position is missing required levels (entry_price or position_size).
    """
    direction = position["direction"]
    raw_entry = position.get("entry_price")
    raw_qty = position.get("position_size")
    raw_notional = position.get("notional_usd")

    # If position_size is missing but we have notional_usd and entry_price,
    # derive the quantity: qty = notional / entry_price.
    if raw_qty is None and raw_entry is not None and raw_notional is not None:
        try:
            raw_qty = float(raw_notional) / float(raw_entry)
        except (TypeError, ValueError, ZeroDivisionError):
            raw_qty = None

    if raw_entry is None or raw_qty is None:
        logger.info(
            "Position %s skipped (missing_levels): entry_price=%s position_size=%s notional=%s",
            position.get("id"),
            raw_entry,
            raw_qty,
            raw_notional,
        )
        return None

    try:
        entry = float(raw_entry)
        qty = float(raw_qty)
    except (TypeError, ValueError) as exc:
        logger.warning(
            "Position %s skipped (unparseable levels): entry_price=%r position_size=%r err=%s",
            position.get("id"),
            raw_entry,
            raw_qty,
            exc,
        )
        return None

    lev = float(position.get("leverage") or 1.0)
    sl = position.get("stop_loss")
    tp = position.get("take_profit_1") or position.get("take_profit")
    sl_val = float(sl) if sl is not None else None
    tp_val = float(tp) if tp is not None else None

    # P&L
    unrealized_pnl_usd = compute_pnl(direction, entry, current_price, qty, lev)
    pnl_pct = compute_pnl_pct(direction, entry, current_price)

    # Distance to SL/TP
    dist = compute_distance_to_sl_tp(direction, current_price, sl_val, tp_val)

    # Time metrics
    created_at = position["created_at"]
    if isinstance(created_at, str):
        created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    time_metrics = compute_time_metrics(created_at, now)

    # MAE / MFE
    worst = position.get("lowest_price")
    best = position.get("highest_price")
    mae_pct = (
        compute_max_adverse_excursion(direction, entry, float(worst))
        if worst is not None
        else 0.0
    )
    mfe_pct = (
        compute_max_favorable_excursion(direction, entry, float(best))
        if best is not None
        else 0.0
    )

    # R:R
    rr = compute_risk_reward_ratio(entry, sl_val, tp_val)

    # SL/TP hit check
    sl_hit, tp_hit = check_sl_tp_hit(direction, current_price, sl_val, tp_val)

    # Distance from current price to entry, signed % (positive = above entry).
    # Direction-agnostic — sign tells the analyst which side of entry we're on.
    if entry > 0:
        distance_to_entry_pct = ((current_price / entry) - 1.0) * 100.0
    else:
        distance_to_entry_pct = 0.0

    # Phase 8 — clamp all pct values to a sane range.
    # Shitcoins with tiny prices (e.g. PEPE at 0.00000038) can produce
    # absurd percentages when entry_price data is wrong. numeric(14,4)
    # on the DB side handles up to 9,999,999,999.9999; this clamp
    # prevents obviously-wrong values from polluting reports.
    pnl_pct = _clamp_pct(pnl_pct)
    distance_to_entry_pct = _clamp_pct(distance_to_entry_pct)
    dist_sl = _clamp_pct(dist["distance_to_sl_pct"])
    dist_tp = _clamp_pct(dist["distance_to_tp_pct"])
    mae_pct = _clamp_pct(mae_pct)
    mfe_pct = _clamp_pct(mfe_pct)

    return PositionSnapshot(
        position_id=position["id"],
        current_price=current_price,
        unrealized_pnl=unrealized_pnl_usd,
        pnl_pct=pnl_pct,
        distance_to_entry_pct=distance_to_entry_pct,
        distance_to_sl=dist["distance_to_sl"],
        distance_to_sl_pct=dist_sl,
        distance_to_tp=dist["distance_to_tp"],
        distance_to_tp_pct=dist_tp,
        hours_open=time_metrics["hours_open"],
        mae_pct=mae_pct,
        mfe_pct=mfe_pct,
        rr_ratio=rr,
        sl_hit=sl_hit,
        tp_hit=tp_hit,
        snapshot_time=now,
    )


async def write_position_update(
    pool: DatabasePool,
    snapshot: PositionSnapshot,
) -> int:
    """Write a position_updates row.

    Args:
        pool: Shared Postgres pool.
        snapshot: Computed snapshot.

    Returns:
        Inserted row ID (position_update_id).
    """
    row = await pool.fetch_one(
        """
        INSERT INTO public.position_updates (
            position_id, price, unrealized_pnl_usd, unrealized_pnl_pct,
            distance_to_entry_pct, distance_to_sl_pct, distance_to_tp1_pct,
            time_in_trade_minutes, timestamp
        ) VALUES (
            $1, $2, $3, $4,
            $5, $6, $7,
            $8, $9
        )
        RETURNING id
        """,
        (
            snapshot.position_id,
            snapshot.current_price,
            snapshot.unrealized_pnl,
            snapshot.pnl_pct,
            snapshot.distance_to_entry_pct,
            snapshot.distance_to_sl_pct,
            snapshot.distance_to_tp_pct,
            int(snapshot.hours_open * 60),
            snapshot.snapshot_time,
        ),
    )
    return int(row["id"]) if row else 0


async def update_position_outcome(
    pool: DatabasePool,
    position_id: int,
    status: str,
    outcome: str,
    exit_price: float,
    realized_pnl: float,
    now: datetime,
    realized_pnl_final: Optional[float] = None,
) -> int:
    """Mark a position as closed with outcome and write D7-compliant P&L.

    The schema has TWO P&L columns:
      * ``realized_pnl_usd`` — NOT NULL, default 0; running tally column.
      * ``realized_pnl_usd_final`` — NULLable; canonical settlement column
        used by post-mortems and the orphan-backfill acceptance check.

    F2 writes BOTH on every close so downstream code can rely on
    ``realized_pnl_usd_final IS NOT NULL`` as the "this position has been
    fully accounted for" sentinel without losing the legacy column.

    The valid ``outcome`` values per the check constraint are::

        tp1_hit, tp2_hit, tp3_hit, sl_hit, breakeven,
        expired, manual_close, invalidated

    Callers that pass an outcome outside that set will fail the constraint;
    that is intentional — silent fallback would mask wiring bugs.

    Args:
        pool: Shared Postgres pool.
        position_id: ``tracked_positions.id``.
        status: New status (typically ``'closed'``).
        outcome: One of the constraint-allowed outcome strings above.
        exit_price: Price at which the position exited.
        realized_pnl: Running-tally P&L (written to ``realized_pnl_usd``).
        now: Close timestamp (also written to ``closed_at``).
        realized_pnl_final: D7-settled net P&L. When provided, written to
            ``realized_pnl_usd_final``; when None, that column is left
            untouched (preserves any prior partial-exit settlement).

    Returns:
        Affected row count.
    """
    if realized_pnl_final is None:
        return await pool.execute(
            """
            UPDATE public.tracked_positions
            SET status = $1,
                outcome = $2,
                exit_price = $3,
                realized_pnl_usd = $4,
                closed_at = $5,
                updated_at = $5
            WHERE id = $6
            """,
            (status, outcome, exit_price, realized_pnl, now, position_id),
        )
    return await pool.execute(
        """
        UPDATE public.tracked_positions
        SET status = $1,
            outcome = $2,
            exit_price = $3,
            realized_pnl_usd = $4,
            realized_pnl_usd_final = $5,
            closed_at = $6,
            updated_at = $6
        WHERE id = $7
        """,
        (
            status,
            outcome,
            exit_price,
            realized_pnl,
            realized_pnl_final,
            now,
            position_id,
        ),
    )


async def update_position_extremes(
    pool: DatabasePool,
    position_id: int,
    worst_price: Optional[float],
    best_price: Optional[float],
) -> int:
    """Update lowest_price and highest_price for a position.

    Args:
        pool: Shared Postgres pool.
        position_id: tracked_positions.id.
        worst_price: New worst price (None to skip).
        best_price: New best price (None to skip).

    Returns:
        Affected row count.
    """
    fields: List[str] = []
    params: List[Any] = []
    if worst_price is not None:
        fields.append("lowest_price = LEAST(COALESCE(lowest_price, $1), $1)")
        params.append(worst_price)
    if best_price is not None:
        fields.append("highest_price = GREATEST(COALESCE(highest_price, $1), $1)")
        params.append(best_price)
    if not fields:
        return 0
    params.append(position_id)
    sql = f"""
        UPDATE public.tracked_positions
        SET {', '.join(fields)}, updated_at = NOW()
        WHERE id = ${len(params)}
    """
    return await pool.execute(sql, tuple(params))


async def update_position_price_pnl(
    pool: DatabasePool,
    position_id: int,
    current_price: float,
    pnl_pct: float,
    unrealized_pnl_usd: float,
) -> int:
    """Update current_price, pnl_pct, unrealized_pnl_usd on tracked_positions.

    Idempotent: only writes when at least one value differs from the stored
    row, avoiding unnecessary write amplification on every cycle.

    Args:
        pool: Shared Postgres pool.
        position_id: tracked_positions.id.
        current_price: Current market price.
        pnl_pct: Unrealized P&L percentage.
        unrealized_pnl_usd: Unrealized P&L in USD.

    Returns:
        Affected row count.
    """
    return await pool.execute(
        """
        UPDATE public.tracked_positions
        SET current_price = $1,
            unrealized_pnl_pct = $2,
            unrealized_pnl_usd = $3,
            price_updated_at = NOW(),
            updated_at = NOW()
        WHERE id = $4
          AND (current_price IS DISTINCT FROM $1
               OR unrealized_pnl_pct IS DISTINCT FROM $2
               OR unrealized_pnl_usd IS DISTINCT FROM $3)
        """,
        (current_price, pnl_pct, unrealized_pnl_usd, position_id),
    )


# Phase 8 §G: ChartHackerOpinionService is the sole writer to agent_opinions.
# The dual-role generate_agent_opinion() has been removed from PositionMonitor.
# All agent_opinion writes now flow through shared.intelligence.chart_hacker_opinion_service.


# ---------------------------------------------------------------------------
# Daemon class
# ---------------------------------------------------------------------------
class PositionMonitor:
    """Daemon that continuously monitors open positions and updates metrics."""

    def __init__(self, cfg: Optional[MonitorConfig] = None) -> None:
        self.cfg = cfg or MonitorConfig()
        self._stop = asyncio.Event()
        self._cycle_count = 0
        self._adapters: Dict[str, Any] = {}

    async def _ensure_pool(self) -> DatabasePool:
        return await get_shared_pool()

    async def _settle_close(
        self,
        pool: DatabasePool,
        position: Dict[str, Any],
        snapshot: PositionSnapshot,
        outcome: str,
        exit_price: float,
        now: datetime,
    ) -> Optional[RealizedPnlBreakdown]:
        """Compute D7-compliant net P&L and persist the close.

        Resolves ``instruments.id`` from the position's symbol+exchange,
        looks up the fee profile, and runs ``compute_realized_pnl_db``.
        On success, writes both ``realized_pnl_usd`` (running tally =
        net_pnl) and ``realized_pnl_usd_final`` (D7-settled net) to
        ``tracked_positions``. On failure (no instrument or no profile),
        logs an error and returns None so the caller can defer the close.

        Args:
            pool: Shared Postgres pool.
            position: Row from ``tracked_positions``.
            snapshot: Snapshot used as the unrealized-P&L fallback.
            outcome: Constraint-allowed outcome string.
            exit_price: Settlement price.
            now: Close timestamp (UTC, tz-aware).

        Returns:
            ``RealizedPnlBreakdown`` on success, None when the fee profile
            cannot be loaded (position is left open for human review).
        """
        try:
            instrument_id = await _resolve_instrument_id(
                pool,
                position["instrument_symbol"],
                position.get("instrument_exchange"),
            )
            if instrument_id is None:
                logger.error(
                    "F2 cannot settle position %s: instrument unresolved "
                    "(symbol=%s exchange=%s)",
                    position["id"],
                    position["instrument_symbol"],
                    position.get("instrument_exchange"),
                )
                return None

            opened_at = position.get("signal_timestamp") or position["created_at"]

            # F2 — derive qty from notional_usd when position_size is NULL
            raw_qty = position.get("position_size")
            raw_entry = position.get("entry_price")
            raw_notional = position.get("notional_usd")
            if raw_qty is None and raw_entry is not None and raw_notional is not None:
                try:
                    raw_qty = float(raw_notional) / float(raw_entry)
                except (TypeError, ValueError, ZeroDivisionError):
                    raw_qty = None

            breakdown = await compute_realized_pnl_db(
                pool,
                instrument_id=instrument_id,
                direction=position["direction"],
                entry_price=raw_entry,
                exit_price=exit_price,
                qty=raw_qty,
                leverage=position.get("leverage") or 1,
                opened_at=opened_at,
                closed_at=now,
            )
            if breakdown is None:
                logger.error(
                    "F2 cannot settle position %s: no fee profile for "
                    "instrument_id=%s",
                    position["id"],
                    instrument_id,
                )
                return None

            net_pnl_float = float(breakdown.net_pnl_usd)
            await update_position_outcome(
                pool,
                position["id"],
                "closed",
                outcome,
                exit_price,
                net_pnl_float,
                now,
                realized_pnl_final=net_pnl_float,
            )
            return breakdown
        except Exception as exc:
            logger.exception(
                "F2 settle failed for position %s: %s",
                position["id"],
                exc,
            )
            return None

    async def _activate_pending_positions(
        self,
        pool: DatabasePool,
        now: datetime,
    ) -> Dict[str, int]:
        """Activate pending tracked_positions whose entry price has been reached.

        Correct activation semantics (2026-05-22 fix):
            A position transitions ``pending -> open`` ONLY when a 1m candle
            whose ``timestamp > position.created_at`` had a ``[low, high]``
            range that included the entry price. The candle's close at that
            moment becomes ``current_price``.

        This unified ``low <= entry <= high`` predicate works for every
        direction and every entry style (limit-below, breakout-above,
        limit-above-short, breakdown-below-short) — same predicate
        ``copy_trade_monitor`` already uses for SL/TP detection.

        The previous implementation compared the latest 1m close to entry
        with ``current_price >= entry`` (long) / ``current_price <= entry``
        (short). For ``limit_below`` longs (buy-the-dip) and
        ``limit_above`` shorts (sell-the-rip), the condition was trivially
        satisfied at signal time, causing instant retro-activation even
        when the market never printed at entry. Forensic evidence in
        ``/opt/tickles/.cursor/debug-c8d268.log`` (run "retrofit-*")
        confirmed 15/15 currently-open positions activated this way with
        NO real candle touch between created_at and updated_at.

        Positions older than 7 days are expired with ``entry_never_reached``.

        Args:
            pool: Shared Postgres pool.
            now: Current UTC timestamp.

        Returns:
            Dict with counts for ``activated``, ``expired``, ``no_price``,
            and ``error``. The ``no_price`` bucket is reused for "no real
            candle touched entry yet" (i.e. the position remains pending —
            normal, not an error).
        """
        pending = await pool.fetch_all(
            """
            SELECT id, instrument_symbol, instrument_exchange,
                   direction, entry_price, created_at
            FROM public.tracked_positions
            WHERE status = 'pending'
            ORDER BY created_at ASC
            LIMIT 200
            """,
        )

        if not pending:
            return {"activated": 0, "expired": 0, "no_price": 0, "error": 0}

        activated = 0
        expired = 0
        no_price = 0
        error = 0
        expiry_cutoff = now - timedelta(days=7)

        for pos in pending:
            if self._stop.is_set():
                break
            try:
                pos_id = int(pos["id"])
                created = pos["created_at"]
                # Expire positions older than 7 days
                if isinstance(created, datetime) and created < expiry_cutoff:
                    await pool.execute(
                        """
                        UPDATE public.tracked_positions
                        SET status = 'expired',
                            status_reason = 'entry_never_reached',
                            updated_at = $1
                        WHERE id = $2
                        """,
                        (now, pos_id),
                    )
                    expired += 1
                    logger.info(
                        "Expired pending position %s (symbol=%s, age > 7 days)",
                        pos_id, pos["instrument_symbol"],
                    )
                    continue

                entry_price = pos["entry_price"]
                if entry_price is None or created is None:
                    no_price += 1
                    continue

                entry = float(entry_price)
                # Recency bound: only candles within the last
                # ``activation_lookback_s`` (default 30 min) can trigger
                # activation. This enforces "the agent must be watching"
                # — touches that happened while the monitor was down or
                # the position was undetected don't retro-activate.
                not_before = now - timedelta(seconds=self.cfg.activation_lookback_s)
                triggered = await _find_entry_touch_candle(
                    pool=pool,
                    symbol=pos["instrument_symbol"],
                    exchange=pos.get("instrument_exchange"),
                    timeframe=self.cfg.default_timeframe,
                    entry=entry,
                    since=created,
                    not_before=not_before,
                    adapters=self._adapters,
                )
                if triggered is None:
                    # No candle whose [low, high] contains entry has printed
                    # since the position was created. Still pending —
                    # nothing to do, just leave it. Counted under no_price
                    # so the existing log line keeps working.
                    no_price += 1
                    continue

                trigger_ts, trigger_close = triggered
                await pool.execute(
                    """
                    UPDATE public.tracked_positions
                    SET status = 'open',
                        status_reason = NULL,
                        current_price = $1,
                        price_updated_at = $2,
                        updated_at = $2
                    WHERE id = $3
                    """,
                    (trigger_close, trigger_ts, pos_id),
                )
                activated += 1
                logger.info(
                    "Activated pending position %s: %s %s entry=%.6f "
                    "triggered_by_candle_at=%s close=%.6f",
                    pos_id, pos["instrument_symbol"], pos["direction"],
                    entry, trigger_ts, trigger_close,
                )

            except Exception as exc:
                logger.exception(
                    "Error activating pending position %s: %s",
                    pos.get("id"), exc,
                )
                error += 1

        if activated or expired:
            logger.info(
                "Pending activation cycle: activated=%s expired=%s no_price=%s error=%s",
                activated, expired, no_price, error,
            )

        return {
            "activated": activated,
            "expired": expired,
            "no_price": no_price,
            "error": error,
        }

    async def _process_one(
        self,
        pool: DatabasePool,
        position: Dict[str, Any],
        now: datetime,
    ) -> Dict[str, Any]:
        """Process a single open position: fetch price, compute, write update.

        F2 adds two settlement paths, both routed through ``_settle_close``
        so every close emits D7-compliant fee/spread/funding-aware P&L:

          * **expiry**: when ``position.expiry_at`` is non-null and ``now``
            is past it, settle at the current price with outcome ``expired``.
            Checked BEFORE SL/TP so an already-expired contract is closed
            even if the latest candle happens to also brush an SL/TP level.
          * **SL/TP hit**: outcomes mapped to constraint-allowed values
            (``tp1_hit`` / ``sl_hit``) — the previous ``take_profit`` /
            ``stop_loss`` strings would have failed the check constraint.

        Args:
            pool: Shared Postgres pool.
            position: Row from tracked_positions.
            now: Current timestamp.

        Returns:
            Result dict with status and metrics.
        """
        pos_id = position["id"]
        symbol = position["instrument_symbol"]
        epic = position.get("epic_code")
        instrument_exchange = position.get("instrument_exchange")

        # 2026-05-22 — Wick-aware SL/TP close detection.
        # Scan 1m candles since the previous monitor tick. If any candle's
        # [low, high] range wicked through SL or TP, the trader's broker
        # would have filled there — close at that level, not at the
        # snapshot close (which can lie when price wicked through and
        # closed back inside). Mirrors copy_trade_monitor's per-candle
        # convention (SL beats TP on same-candle ambiguity).
        sl_val_for_wick = position.get("stop_loss")
        tp_val_for_wick = position.get("take_profit_1") or position.get("take_profit")
        wick: Optional[Tuple[datetime, float, str, float]] = None
        wick_since: Optional[datetime] = None
        if sl_val_for_wick is not None or tp_val_for_wick is not None:
            wick_since = (
                position.get("price_updated_at")
                or position.get("updated_at")
                or position.get("created_at")
            )
            if wick_since is not None:
                try:
                    wick = await _find_sl_tp_wick_candle(
                        pool,
                        symbol=symbol,
                        exchange=instrument_exchange,
                        timeframe=self.cfg.default_timeframe,
                        direction=position.get("direction", ""),
                        stop_loss=float(sl_val_for_wick) if sl_val_for_wick is not None else None,
                        take_profit=float(tp_val_for_wick) if tp_val_for_wick is not None else None,
                        since=wick_since,
                        adapters=self._adapters,
                    )
                except Exception as exc:  # noqa: BLE001 — defensive
                    logger.warning(
                        "wick check raised for position %s: %s", pos_id, exc,
                    )

        if wick is not None:
            wick_ts, _wick_close, hit_type, hit_price = wick
            outcome = "sl_hit" if hit_type == "sl" else "tp1_hit"
            wick_snapshot = _build_snapshot(position, hit_price, wick_ts)
            if wick_snapshot is None:
                return {
                    "position_id": pos_id,
                    "status": "missing_levels",
                    "instrument_symbol": symbol,
                }
            update_id = await write_position_update(pool, wick_snapshot)
            breakdown = await self._settle_close(
                pool, position, wick_snapshot, outcome, hit_price, wick_ts,
            )
            if breakdown is not None:
                logger.info(
                    "Position %s closed via wick %s at level=%.6f candle_ts=%s "
                    "net_pnl=%s",
                    pos_id, outcome, hit_price, wick_ts, breakdown.net_pnl_usd,
                )
                return {
                    "position_id": pos_id,
                    "status": "closed",
                    "outcome": outcome,
                    "update_id": update_id,
                    "realized_pnl": float(breakdown.net_pnl_usd),
                    "wick_close": True,
                }
            return {
                "position_id": pos_id,
                "status": "settle_deferred",
                "reason": f"{outcome}_no_profile",
                "update_id": update_id,
                "wick_close": True,
            }

        if wick is None and wick_since is not None:
            # Optimize future cycles: since we found no wick hit, advance the check watermark
            # to the newest candle's timestamp currently in the database to prevent missing late-ingested candles.
            try:
                instrument_id = await _resolve_instrument_id(pool, symbol, instrument_exchange)
                max_ts = None
                if instrument_id is not None:
                    max_ts = await pool.fetch_val(
                        """
                        SELECT MAX("timestamp") FROM public.candles
                        WHERE instrument_id = $1 AND timeframe = $2
                        """,
                        (instrument_id, self.cfg.default_timeframe)
                    )
                
                watermark = max_ts if max_ts is not None else datetime.now(timezone.utc)
                await pool.execute(
                    """
                    UPDATE public.tracked_positions
                    SET price_updated_at = $1,
                        updated_at = NOW()
                    WHERE id = $2
                    """,
                    (watermark, pos_id,)
                )
            except Exception as exc:
                logger.warning("Failed to advance price_updated_at watermark for position %s: %s", pos_id, exc)

        # Fetch price — try live feed first, fall back to candles.
        price: Optional[float] = None
        if not epic:
            price = await fetch_live_price_from_feed(symbol)
        if price is None and epic:
            price = await fetch_latest_price_by_epic(pool, epic, self.cfg.default_timeframe)
        if price is None:
            price = await fetch_latest_price(pool, symbol, instrument_exchange, self.cfg.default_timeframe)

        if price is None:
            logger.warning("No price data for position %s (symbol=%s, epic=%s)", pos_id, symbol, epic)
            return {"position_id": pos_id, "status": "no_price_data"}

        # Build snapshot — F7 NULL guard returns None for orphan signals
        # (entry_price/position_size NULL). These are real trader signals
        # awaiting F9 backfill; we must not crash, must not fabricate.
        snapshot = _build_snapshot(position, price, now)
        if snapshot is None:
            return {
                "position_id": pos_id,
                "status": "missing_levels",
                "instrument_symbol": symbol,
            }

        # Write position_update
        update_id = await write_position_update(pool, snapshot)

        # Update extremes
        await update_position_extremes(
            pool,
            pos_id,
            worst_price=price if snapshot.mae_pct > 0 else None,
            best_price=price if snapshot.mfe_pct > 0 else None,
        )

        # Write current_price, pnl_pct, unrealized_pnl_usd back to tracked_positions
        # so the dashboard snapshot can show real P&L numbers without extra queries.
        await update_position_price_pnl(
            pool,
            pos_id,
            snapshot.current_price,
            snapshot.pnl_pct,
            snapshot.unrealized_pnl,
        )

        # Expiry check (F2): runs before SL/TP so an expired position
        # always settles via the time-based path, not the level-based one.
        expiry_at = position.get("expiry_at")
        if expiry_at is not None and now >= expiry_at:
            breakdown = await self._settle_close(
                pool, position, snapshot, "expired", price, now
            )
            if breakdown is not None:
                logger.info(
                    "Position %s closed via expired at price=%.4f net_pnl=%s",
                    pos_id, price, breakdown.net_pnl_usd,
                )
                return {
                    "position_id": pos_id,
                    "status": "closed",
                    "outcome": "expired",
                    "update_id": update_id,
                    "realized_pnl": float(breakdown.net_pnl_usd),
                }
            return {
                "position_id": pos_id,
                "status": "settle_deferred",
                "reason": "expired_no_profile",
                "update_id": update_id,
            }

        # SL/TP hit check (F2: outcomes constraint-aligned, P&L D7-compliant).
        if snapshot.sl_hit or snapshot.tp_hit:
            outcome = "tp1_hit" if snapshot.tp_hit else "sl_hit"
            breakdown = await self._settle_close(
                pool, position, snapshot, outcome, price, now
            )
            if breakdown is not None:
                logger.info(
                    "Position %s closed via %s at price=%.4f net_pnl=%s",
                    pos_id, outcome, price, breakdown.net_pnl_usd,
                )
                return {
                    "position_id": pos_id,
                    "status": "closed",
                    "outcome": outcome,
                    "update_id": update_id,
                    "realized_pnl": float(breakdown.net_pnl_usd),
                }
            return {
                "position_id": pos_id,
                "status": "settle_deferred",
                "reason": f"{outcome}_no_profile",
                "update_id": update_id,
            }

        # Phase 8 §G: agent_opinion generation removed from PositionMonitor.
        # ChartHackerOpinionService is now the sole writer to agent_opinions.

        return {
            "position_id": pos_id,
            "status": "monitored",
            "update_id": update_id,
            "pnl_pct": snapshot.pnl_pct,
            "hours_open": snapshot.hours_open,
        }

    async def run_cycle(self) -> Dict[str, Any]:
        """Run one monitoring cycle: activate pending, then snapshot open positions.

        2026-05-22 — Wraps the cycle in a try/except + heartbeat. Heartbeat
        status is:
          * ``ok``     — cycle completed and zero per-position errors.
          * ``partial`` — cycle completed but some positions raised.
          * ``error``  — cycle itself raised (pool, query, etc.).
        The cron-canary watchdog reads ``cron_heartbeats`` and alerts when
        ``now - last_run_at > expected_interval_seconds × N``, so a hung
        or crashed monitor surfaces without us hand-tailing the log.

        Returns:
            Summary dict with counts.
        """
        # The interval the canary uses to decide "stale". We pad the poll
        # interval by 2× so a single slow cycle doesn't flap as stale.
        expected_interval = int(max(60, self.cfg.poll_interval_s * 2))
        per_position_errors = 0
        try:
            pool = await self._ensure_pool()
            now = datetime.now(timezone.utc)

            # Activate pending positions whose entry price has been reached
            pending_result = await self._activate_pending_positions(pool, now)

            positions = await fetch_open_positions(pool, self.cfg.batch_size)

            if not positions:
                logger.debug("No open positions to monitor")
                await self._heartbeat(
                    status="ok",
                    expected_interval_seconds=expected_interval,
                    message=(
                        f"no_positions activated={pending_result.get('activated', 0)} "
                        f"pending_expired={pending_result.get('expired', 0)}"
                    ),
                )
                return {"monitored": 0, "closed": 0, "no_price": 0}

            monitored = 0
            closed = 0
            no_price = 0
            settle_deferred = 0

            for pos in positions:
                if self._stop.is_set():
                    break
                try:
                    result = await self._process_one(pool, pos, now)
                    status = result["status"]
                    if status == "closed":
                        closed += 1
                    elif status == "no_price_data":
                        no_price += 1
                    elif status == "settle_deferred":
                        settle_deferred += 1
                    else:
                        monitored += 1
                except Exception as exc:
                    per_position_errors += 1
                    logger.exception("Error processing position %s: %s", pos.get("id"), exc)

            self._cycle_count += 1
            logger.info(
                "Cycle %s: pending_activated=%s pending_expired=%s monitored=%s closed=%s no_price=%s settle_deferred=%s",
                self._cycle_count,
                pending_result.get("activated", 0),
                pending_result.get("expired", 0),
                monitored, closed, no_price, settle_deferred,
            )

            hb_status: str = "partial" if per_position_errors else "ok"
            await self._heartbeat(
                status=hb_status,
                expected_interval_seconds=expected_interval,
                message=(
                    f"cycle={self._cycle_count} monitored={monitored} "
                    f"closed={closed} no_price={no_price} "
                    f"deferred={settle_deferred} per_pos_errors={per_position_errors}"
                ),
            )
            return {
                "monitored": monitored,
                "closed": closed,
                "no_price": no_price,
                "settle_deferred": settle_deferred,
                "pending_activated": pending_result.get("activated", 0),
                "pending_expired": pending_result.get("expired", 0),
            }
        except Exception as exc:
            logger.exception("run_cycle aborted: %s", exc)
            # Best-effort heartbeat — never crash on heartbeat failure.
            try:
                await self._heartbeat(
                    status="error",
                    expected_interval_seconds=expected_interval,
                    message=f"run_cycle exception: {exc}",
                )
            except Exception:  # pragma: no cover — defensive
                pass
            raise

    async def _heartbeat(
        self, *, status: str, expected_interval_seconds: int,
        message: Optional[str] = None,
    ) -> None:
        """Tiny wrapper around shared.intelligence.heartbeat.record_heartbeat.

        Kept here so the monitor never imports the heartbeat module at
        module level (keeps test fixtures simple) and so a heartbeat
        failure can never abort a cycle.
        """
        try:
            from shared.intelligence.heartbeat import record_heartbeat
            await record_heartbeat(
                agent_id="position-monitor",
                status=status,  # type: ignore[arg-type]
                expected_interval_seconds=expected_interval_seconds,
                message=message,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug("heartbeat write failed (non-fatal): %s", exc)

    async def run_forever(self) -> None:
        """Main loop: run cycles until stopped."""
        logger.info(
            "PositionMonitor starting (poll=%.0fs batch=%s opinion_every=%s)",
            self.cfg.poll_interval_s,
            self.cfg.batch_size,
            self.cfg.agent_opinion_every_n,
        )
        try:
            while not self._stop.is_set():
                try:
                    await self.run_cycle()
                except Exception as exc:
                    logger.exception("Cycle failed: %s", exc)
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=self.cfg.poll_interval_s,
                    )
                except asyncio.TimeoutError:
                    pass
        finally:
            if self._adapters:
                logger.info("Closing %d cached CCXT exchange adapters...", len(self._adapters))
                for exchange_id, adapter in list(self._adapters.items()):
                    try:
                        await adapter.close()
                    except Exception as exc:
                        logger.warning("Error closing cached adapter %s: %s", exchange_id, exc)
                self._adapters.clear()
            logger.info("PositionMonitor stopped")

    def stop(self) -> None:
        """Signal the daemon to stop gracefully."""
        self._stop.set()


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------
def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    """Install SIGINT/SIGTERM handlers for graceful shutdown."""
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass


async def main() -> None:
    """Run the PositionMonitor daemon."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_env()
    monitor = PositionMonitor()
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, monitor._stop)
    await monitor.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
