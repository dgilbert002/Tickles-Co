"""
Module: position_monitor
Purpose: Fixed daemon that continuously monitors open tracked_positions,
         computes live P&L, distance-to-SL/TP, time metrics, and writes
         position_updates snapshots. Detects SL/TP hits and updates outcomes.
Location: /opt/tickles/shared/intelligence/position_monitor.py

Design:
  * Polls tickles_shared.tracked_positions for status IN ('open','partial_exit').
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
# Round 12 (2026-05-24): Multi-venue OHLCV fallback.
#
# Pre-Round-12, every CCXT fallback path hardcoded ``exchange or "bybit"``.
# That meant Capital.com CFDs (GOLD, US100, USDJPY, etc.) and obscure alts
# routed elsewhere produced "bybit does not have market symbol" errors on
# every monitor cycle. The router now stamps tracked_positions.instrument_
# exchange with the correct venue at INSERT time; this helper picks the
# right adapter based on that value.
#
# Rules:
#   * exchange == "capital.com" → CapitalAdapter; symbol passed in is the
#     bare epic name (matches what unified_instruments stores).
#   * Anything else → CCXTAdapter with the supplied exchange id; we still
#     try _candidate_symbols() form-variants because the local DB may have
#     slash form even when CCXT wants ``BTC/USDT:USDT``.
#
# This helper is intentionally NOT a class method — it has no state, only
# adapter caching, which is owned by the caller's ``adapters`` dict.
# ---------------------------------------------------------------------------
async def _fetch_ohlcv_for_market(
    symbol: str,
    exchange: Optional[str],
    timeframe: str,
    since_utc: datetime,
    *,
    limit: int = 1000,
    adapters: Optional[Dict[str, Any]] = None,
) -> List[Any]:
    """Fetch OHLCV candles via the right adapter for the routed exchange.

    Returns a list of ``Candle`` objects from the adapter (each has
    ``.timestamp``, ``.high``, ``.low``, ``.close``). Empty list if the
    adapter can't find the market or fails — callers handle the empty
    case as "no candles available, leave pending".

    Args:
        symbol: For CCXT exchanges, the unified slash form (or perp form);
            for capital.com, the bare epic name (``GOLD``, ``US100``).
        exchange: Routed exchange. ``capital.com`` triggers Capital path;
            ``bybit``/``bitget``/``blofin`` use CCXT; ``None`` defaults
            to ``bybit`` for backward compatibility with rows written
            before Round 12 (the row wasn't routed → fall back to old
            behaviour, log loudly).
        timeframe: Candle timeframe (``"1m"``, ``"5m"``, etc.).
        since_utc: Fetch candles strictly after this UTC timestamp.
        limit: Max candles to fetch.
        adapters: Optional reuse cache keyed by exchange id.

    Returns:
        List of Candle objects. Always returns a list, never raises.
        Errors are logged + swallowed because monitor cycles must
        continue regardless of one symbol's failure.
    """
    ex_id = (exchange or "").lower().strip() or "bybit"

    if ex_id == "capital.com":
        from shared.connectors.capital_adapter import CapitalAdapter
        # Round 12 (2026-05-24): Capital.com's /prices/{epic} endpoint
        # caps the requested window per resolution. Empirically (verified
        # against demo on 2026-05-24): 1m allows up to ~12h, longer
        # ranges return ``error.invalid.max.daterange``. The monitor
        # only needs the recent window (activation lookback + SL/TP
        # wicks); anything older is academic. Clamp before the call.
        _CAPITAL_TF_MAX_LOOKBACK = {
            "1m":  timedelta(hours=10),    # Capital cap is ~12h; 10h gives margin
            "5m":  timedelta(days=2),
            "15m": timedelta(days=10),
            "1h":  timedelta(days=40),
            "4h":  timedelta(days=120),
            "1d":  timedelta(days=900),
        }
        max_lookback = _CAPITAL_TF_MAX_LOOKBACK.get(timeframe, timedelta(hours=10))
        now_utc = datetime.now(timezone.utc)
        clamp_floor = now_utc - max_lookback
        if since_utc < clamp_floor:
            logger.debug(
                "_fetch_ohlcv_for_market(capital.com): clamped since=%s -> %s "
                "for tf=%s (Capital max-range guard)",
                since_utc, clamp_floor, timeframe,
            )
            since_utc = clamp_floor
        adapter = None
        is_cached = False
        if adapters is not None and "capital.com" in adapters:
            adapter = adapters["capital.com"]
            is_cached = True
        else:
            env = os.environ.get("CAPITAL_ENV", "demo")
            adapter = CapitalAdapter(environment=env)
            email = os.environ.get("CAPITAL_EMAIL", "")
            password = os.environ.get("CAPITAL_PASSWORD", "")
            api_key = os.environ.get("CAPITAL_API_KEY", "")
            if not (email and password and api_key):
                logger.warning(
                    "_fetch_ohlcv_for_market: Capital.com credentials missing "
                    "(CAPITAL_EMAIL/PASSWORD/API_KEY) — cannot fetch %s",
                    symbol,
                )
                return []
            try:
                await adapter.authenticate(email, password, api_key)
            except Exception as auth_exc:
                logger.warning(
                    "_fetch_ohlcv_for_market: Capital auth failed for %s: %s",
                    symbol, auth_exc,
                )
                return []
            if adapters is not None:
                adapters["capital.com"] = adapter
                is_cached = True
        try:
            candles = await adapter.fetch_ohlcv(
                epic=symbol, timeframe=timeframe, since=since_utc, limit=limit,
            )
            return list(candles or [])
        except Exception as exc:
            # Round 12 (2026-05-24): Capital surfaces several "not really
            # an error" conditions as 404s:
            #   * error.prices.not-found  → market closed (forex/commodity
            #     on weekend), no prices in the requested window
            #   * error.not-found.epic    → epic spelling wrong (rare with
            #     unified_instruments-driven routing)
            # Downgrade the noisy "no prices" path to DEBUG so weekends
            # don't flood the log; treat genuine config issues as WARN.
            msg = str(exc)
            if "prices.not-found" in msg:
                logger.debug(
                    "_fetch_ohlcv_for_market(capital.com): no prices in "
                    "window for epic=%s tf=%s (likely market closed)",
                    symbol, timeframe,
                )
            else:
                logger.warning(
                    "_fetch_ohlcv_for_market: Capital fetch_ohlcv failed for "
                    "epic=%s tf=%s: %s",
                    symbol, timeframe, exc,
                )
            return []
        finally:
            if not is_cached:
                try:
                    await adapter.close()
                except Exception:
                    pass

    # CCXT path (bybit / bitget / blofin / unknown)
    from shared.connectors.ccxt_adapter import CCXTAdapter
    from shared.market_data.live_price import _candidate_symbols
    adapter = None
    is_cached = False
    if adapters is not None and ex_id in adapters:
        adapter = adapters[ex_id]
        is_cached = True
    else:
        adapter = CCXTAdapter(ex_id)
        if adapters is not None:
            adapters[ex_id] = adapter
            is_cached = True
    try:
        candidates = _candidate_symbols(symbol)
        last_err: Optional[Exception] = None
        for cand in candidates:
            try:
                candles = await adapter.fetch_ohlcv(
                    symbol=cand, timeframe=timeframe,
                    since=since_utc, limit=limit,
                )
                if candles:
                    return list(candles)
            except Exception as sym_exc:
                last_err = sym_exc
                continue
        if last_err is not None:
            logger.debug(
                "_fetch_ohlcv_for_market: all CCXT symbol forms failed for "
                "%s on %s tf=%s: %s",
                symbol, ex_id, timeframe, last_err,
            )
        return []
    finally:
        if not is_cached:
            try:
                await adapter.close()
            except Exception:
                pass

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
#
# Round 11 (2026-05-24): widened from 30 min → 24 h. The previous bound was
# too tight: a brief outage or candle-fetch failure on a less-traded symbol
# meant a pending row whose entry was touched even an hour ago would never
# auto-activate, polluting the dashboard with "16% away" rows that should
# have already been retired. 24 h covers monitor restarts and intermittent
# fetch failures without retro-activating ancient stale calls. Rows
# activated outside the original 30-min window are tagged with
# ``status_reason='retro_activated:<utc>'`` for audit.
ACTIVATION_LOOKBACK_S = float(os.environ.get("POSITION_MONITOR_ACTIVATION_LOOKBACK_S", "86400"))
# How far back into history a touch can be before we stamp the row with the
# retro_activated status_reason (in seconds). Anything outside this window
# is "fast path" / normal, anything inside is "retro" and audited.
FAST_ACTIVATION_LOOKBACK_S = 1800.0


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
    # Bug H1 fix (Bug Hunter 1 §C2):
    #   Previously this query returned only the NEWEST `batch_size` open rows
    #   (`ORDER BY created_at DESC LIMIT 50` by default). Once we have 50+
    #   open positions, the oldest ones permanently fall off the bottom of
    #   the queue: they never get wick-scanned for SL/TP, never have
    #   `price_updated_at` advanced, and never close on a hit. Dashboard
    #   P&L silently drifts from reality.
    #
    #   We now order by `price_updated_at NULLS FIRST`: positions that have
    #   never been touched (or have been waiting longest) are processed FIRST.
    #   Combined with the 1000-cap raise from Bug 6, a healthy steady state
    #   re-checks every position regularly even at scale.
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
        ORDER BY price_updated_at ASC NULLS FIRST, created_at ASC
        LIMIT $1
        """,
        (batch_size,),
    )


# Bug Code Analyzer 1 #4 — bound the instrument ID cache.
#   Previously this was a plain dict that grew without bound. Each unique
#   (symbol, exchange) pair added an entry; over multi-week uptimes with
#   many tickers the cache leaked memory. The bound is generous (4096
#   keys ≈ all instruments × major exchanges) and uses a simple
#   first-in-first-out eviction so we don't pay for full LRU bookkeeping
#   on every call. ~32 KB worst-case footprint.
_INSTRUMENT_ID_CACHE_MAX = 4096
_INSTRUMENT_ID_CACHE: "OrderedDict[Tuple[str, Optional[str]], int]" = None  # type: ignore


async def _resolve_instrument_id(
    pool: DatabasePool,
    raw_symbol: str,
    raw_exchange: Optional[str],
) -> Optional[int]:
    global _INSTRUMENT_ID_CACHE
    if _INSTRUMENT_ID_CACHE is None:
        from collections import OrderedDict
        _INSTRUMENT_ID_CACHE = OrderedDict()
    cache_key = (raw_symbol, raw_exchange)
    cached = _INSTRUMENT_ID_CACHE.get(cache_key)
    if cached is not None:
        # Touch for FIFO ordering refresh — cheap.
        try:
            _INSTRUMENT_ID_CACHE.move_to_end(cache_key)  # type: ignore[attr-defined]
        except Exception:
            pass
        return cached
    res = await _resolve_instrument_id_impl(pool, raw_symbol, raw_exchange)
    if res is not None:
        _INSTRUMENT_ID_CACHE[cache_key] = res
        if len(_INSTRUMENT_ID_CACHE) > _INSTRUMENT_ID_CACHE_MAX:
            try:
                _INSTRUMENT_ID_CACHE.popitem(last=False)  # type: ignore[attr-defined]
            except Exception:
                pass
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
    scan_state: Optional[Dict[str, Any]] = None,
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

    # Bug H12 fix (Code Analyzer 2 §2.5):
    #   Previously this fetched up to ``max_candles`` (default 10000 ≈ 7 days
    #   of 1m) starting at ``since_utc`` in one shot. A position open more
    #   than 7 days had any SL/TP wicks beyond row 10000 silently dropped:
    #   the watermark would advance, the loop body would find no hit, and
    #   the position would stay "open" forever despite a real exit.
    #
    #   We now keyset-paginate: fetch in pages, scan each page, and if the
    #   page is full and contains no hit, advance ``since_utc`` past the
    #   last row's timestamp and re-fetch. Bounded by ``max_iterations`` so
    #   pathological inputs can't loop forever.
    rows: List[Dict[str, Any]] = []
    page_size = max(1, min(max_candles, 10000))
    max_iterations = 12  # 12 × 10000 = 120k 1m candles ≈ 83 days, more than
                        # enough for any real-world long-running position.
    fetch_since = since_utc
    iterations = 0
    last_scanned_ts: Optional[datetime] = None
    scan_complete = True  # Optimistic; flipped False on exhaustion / query failure.
    try:
        while iterations < max_iterations:
            page = await pool.fetch_all(
                """
                SELECT "timestamp", high, low, close
                FROM public.candles
                WHERE instrument_id = $1
                  AND timeframe = $2
                  AND "timestamp" >= $3
                ORDER BY "timestamp" ASC
                LIMIT $4
                """,
                (instrument_id, timeframe, fetch_since, page_size),
            )
            iterations += 1
            if not page:
                break
            rows.extend(page)
            last_scanned_ts = ensure_utc(page[-1]["timestamp"])
            # Scan this page in-place for a hit. If we find one, return it
            # without paginating further.
            for r in page:
                hi = float(r["high"])
                lo = float(r["low"])
                close_v = float(r["close"])
                if direction == "long":
                    if stop_loss is not None and lo <= stop_loss:
                        if scan_state is not None:
                            scan_state["complete"] = True
                            scan_state["last_scanned_ts"] = ensure_utc(r["timestamp"])
                            scan_state["hit"] = True
                        return r["timestamp"], close_v, "sl", float(stop_loss)
                    if take_profit is not None and hi >= take_profit:
                        if scan_state is not None:
                            scan_state["complete"] = True
                            scan_state["last_scanned_ts"] = ensure_utc(r["timestamp"])
                            scan_state["hit"] = True
                        return r["timestamp"], close_v, "tp", float(take_profit)
                else:  # short
                    if stop_loss is not None and hi >= stop_loss:
                        if scan_state is not None:
                            scan_state["complete"] = True
                            scan_state["last_scanned_ts"] = ensure_utc(r["timestamp"])
                            scan_state["hit"] = True
                        return r["timestamp"], close_v, "sl", float(stop_loss)
                    if take_profit is not None and lo <= take_profit:
                        if scan_state is not None:
                            scan_state["complete"] = True
                            scan_state["last_scanned_ts"] = ensure_utc(r["timestamp"])
                            scan_state["hit"] = True
                        return r["timestamp"], close_v, "tp", float(take_profit)
            # No hit in this page; if it was a full page, paginate forward.
            if len(page) < page_size:
                break
            last_ts = page[-1]["timestamp"]
            # Advance past the last row to avoid re-scanning it. 1ms increment
            # works because candle timestamps are minute-aligned.
            fetch_since = ensure_utc(last_ts) + timedelta(milliseconds=1)
        if iterations >= max_iterations:
            # Bug F fix (2026-05-24 second-round audit): the scan ran out of
            # iterations before reaching the end of available candles. There
            # MAY be a wick-hit beyond what we scanned. Flag scan_complete=False
            # so the watermark advance code path resumes from `last_scanned_ts`
            # next cycle instead of jumping to `now` (which would skip the
            # unscanned region permanently).
            scan_complete = False
            logger.warning(
                "_find_sl_tp_wick_candle paginated %d iterations (%d candles) for "
                "instrument_id=%s tf=%s — stopping. Position likely older than "
                "scan budget; watermark will resume from last_scanned_ts=%s "
                "next cycle.",
                iterations, len(rows), instrument_id, timeframe, last_scanned_ts,
            )
    except Exception as exc:  # pragma: no cover — defensive
        # Bug F fix: a query failure also means the scan is incomplete — do
        # NOT advance the watermark past the last successfully scanned ts.
        scan_complete = False
        logger.exception(
            "_find_sl_tp_wick_candle query failed instrument_id=%s tf=%s: %s",
            instrument_id, timeframe, exc,
        )
        rows = []

    if not rows:
        # Round 12 (2026-05-24): use multi-venue OHLCV helper (handles
        # bybit/bitget/blofin via CCXT + capital.com via REST). The legacy
        # ``or "bybit"`` default lives inside the helper so legacy rows with
        # NULL instrument_exchange still find the old code path. Adapter
        # caching/cleanup is now owned by the helper — this block no longer
        # constructs adapters directly, so the old try/finally adapter.close()
        # is gone with it.
        try:
            logger.info(
                "_find_sl_tp_wick_candle: Local candles missing for %s since %s. "
                "Falling back via %s adapter.",
                symbol, since_utc, exchange or "bybit",
            )
            ccxt_candles = await _fetch_ohlcv_for_market(
                symbol=symbol,
                exchange=exchange,
                timeframe=timeframe,
                since_utc=since_utc,
                limit=1000,
                adapters=adapters,
            )
            if ccxt_candles:
                logger.debug(
                    "_find_sl_tp_wick_candle: fetched %d candles for %s "
                    "(exchange=%s)",
                    len(ccxt_candles), symbol, exchange or "bybit",
                )
                rows = []
                for c in ccxt_candles:
                    rows.append({
                        "timestamp": c.timestamp,
                        "high": float(c.high),
                        "low": float(c.low),
                        "close": float(c.close)
                    })
                # Bug F round-3 review fix (BH1 #5, CA1 Fix F #2):
                #   When the local-DB pagination failed (exception path
                #   set scan_complete=False, last_scanned_ts=None) and
                #   CCXT successfully picked up some candles, surface
                #   THIS scan's progress so the caller can resume from
                #   the last CCXT row's timestamp. Without this, the
                #   no-hit fallthrough at the bottom of this function
                #   reports last_scanned_ts=None and the caller treats
                #   it as "advance to MAX" — skipping the unscanned
                #   tail forever.
                if rows:
                    last_scanned_ts = ensure_utc(rows[-1]["timestamp"])
                    if len(rows) >= 1000:
                        scan_complete = False
        except Exception as fallback_exc:
            # Bug F round-3 review fix: CCXT fallback failure means we
            # cannot trust our scan progress at all — keep scan_complete
            # at its current False value (set by the local-DB exception)
            # and DON'T advance last_scanned_ts. Caller will hold the
            # watermark at `wick_since` for the next cycle.
            scan_complete = False
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
                if scan_state is not None:
                    scan_state["complete"] = True
                    scan_state["last_scanned_ts"] = ensure_utc(r["timestamp"])
                    scan_state["hit"] = True
                return r["timestamp"], close_v, "sl", float(stop_loss)
            if take_profit is not None and hi >= take_profit:
                if scan_state is not None:
                    scan_state["complete"] = True
                    scan_state["last_scanned_ts"] = ensure_utc(r["timestamp"])
                    scan_state["hit"] = True
                return r["timestamp"], close_v, "tp", float(take_profit)
        else:
            # Short: SL is ABOVE entry → wick up through SL = stop hit.
            # TP is BELOW entry → wick down through TP = profit hit.
            if stop_loss is not None and hi >= stop_loss:
                if scan_state is not None:
                    scan_state["complete"] = True
                    scan_state["last_scanned_ts"] = ensure_utc(r["timestamp"])
                    scan_state["hit"] = True
                return r["timestamp"], close_v, "sl", float(stop_loss)
            if take_profit is not None and lo <= take_profit:
                if scan_state is not None:
                    scan_state["complete"] = True
                    scan_state["last_scanned_ts"] = ensure_utc(r["timestamp"])
                    scan_state["hit"] = True
                return r["timestamp"], close_v, "tp", float(take_profit)

    # No hit found anywhere. Surface the scan state so the caller can decide
    # whether it's safe to advance the watermark past `last_scanned_ts`.
    if scan_state is not None:
        scan_state["complete"] = scan_complete
        scan_state["last_scanned_ts"] = last_scanned_ts
        scan_state["hit"] = False
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
            # 2026-05-23: Safe pending queue check optimization.
            # Check if local candles are up-to-date (fresh). If they are, and there's no local touch,
            # we don't need the slow CCXT API fallback query!
            max_ts = await pool.fetch_val(
                """
                SELECT MAX("timestamp") FROM public.candles
                WHERE instrument_id = $1 AND timeframe = $2
                """,
                (instrument_id, timeframe),
            )
            if max_ts is not None:
                now_utc = datetime.now(timezone.utc)
                if now_utc - ensure_utc(max_ts) < timedelta(minutes=10):
                    logger.debug(
                        "_find_entry_touch_candle: Local candles are fresh for %s (last at %s). Skipping CCXT fallback.",
                        symbol, max_ts
                    )
                    return None
        except Exception as t_exc:
            logger.warning("Failed to check local candle freshness for %s: %s", symbol, t_exc)

        # Round 12 (2026-05-24): use multi-venue OHLCV helper. Adapter
        # construction + auth + cleanup all live inside the helper so we
        # only branch on "got candles? scan them" here.
        try:
            logger.info(
                "_find_entry_touch_candle: Local touch not found for %s. "
                "Checking OHLCV fallback via %s.",
                symbol, exchange or "bybit",
            )
            ccxt_candles = await _fetch_ohlcv_for_market(
                symbol=symbol,
                exchange=exchange,
                timeframe=timeframe,
                since_utc=floor_ts,
                limit=1000,
                adapters=adapters,
            )
            if ccxt_candles:
                logger.debug(
                    "_find_entry_touch_candle: fetched %d candles for %s "
                    "(exchange=%s)",
                    len(ccxt_candles), symbol, exchange or "bybit",
                )
                for c in ccxt_candles:
                    hi = float(c.high)
                    lo = float(c.low)
                    if lo <= entry <= hi:
                        logger.info(
                            "Entry touch found via OHLCV fallback for %s at "
                            "%s (entry=%.6f, low=%.6f, high=%.6f)",
                            symbol, c.timestamp, entry, lo, hi,
                        )
                        return c.timestamp, float(c.close)
        except Exception as fallback_exc:
            logger.warning(
                "_find_entry_touch_candle OHLCV fallback failed for %s: %s",
                symbol, fallback_exc,
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
    # Bug C4 fix (Code Analyzer 2 §1.2) — optimistic status guard.
    # Without `AND status IN ('open','partial_exit')`, two concurrent monitor
    # ticks (or a manual close followed by a monitor close) could both update
    # the same row, with the second write overwriting `exit_price`,
    # `realized_pnl_usd`, and `realized_pnl_usd_final` with potentially
    # different values. The guard ensures the second writer becomes a no-op.
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
              AND status IN ('open', 'partial_exit')
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
          AND status IN ('open', 'partial_exit')
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
    distance_to_entry_pct: Optional[float] = None,
    distance_to_sl_pct: Optional[float] = None,
    distance_to_tp1_pct: Optional[float] = None,
    time_in_trade_minutes: Optional[int] = None,
) -> int:
    """Update live P&L + distance + time-in-trade fields on tracked_positions.

    Round 12 (2026-05-24): the four "live monitor" fields
    ``distance_to_entry_pct``, ``distance_to_sl_pct``, ``distance_to_tp1_pct``,
    and ``time_in_trade_minutes`` are now mirrored back to ``tracked_positions``
    on every cycle. Previously they were only written to ``position_updates``
    (the time-series), which forced the dashboard to either compute them
    client-side or run an extra subquery per row.

    Mirroring keeps ``position_updates`` as the canonical history and
    ``tracked_positions`` as the always-current snapshot — both are written
    in the same monitor tick from the same ``PositionSnapshot`` so they
    cannot disagree.

    Idempotent: only writes when at least one value differs from the stored
    row, avoiding unnecessary write amplification on every cycle. The
    distance/time mirrors are passed via optional kwargs so existing
    callers (tests, partial-exit settle path) keep working without
    modification.

    Args:
        pool: Shared Postgres pool.
        position_id: tracked_positions.id.
        current_price: Current market price.
        pnl_pct: Unrealized P&L percentage.
        unrealized_pnl_usd: Unrealized P&L in USD.
        distance_to_entry_pct: % distance from current_price to entry_price
            (mirrored from PositionSnapshot.distance_to_entry_pct).
        distance_to_sl_pct: % distance from current_price to stop_loss
            (mirrored from PositionSnapshot.distance_to_sl_pct).
        distance_to_tp1_pct: % distance from current_price to take_profit_1
            (mirrored from PositionSnapshot.distance_to_tp_pct — note rename).
        time_in_trade_minutes: Minutes since opened_at (int, mirrored from
            PositionSnapshot.hours_open * 60).

    Returns:
        Affected row count.
    """
    return await pool.execute(
        """
        UPDATE public.tracked_positions
        SET current_price = $1,
            unrealized_pnl_pct = $2,
            unrealized_pnl_usd = $3,
            distance_to_entry_pct = COALESCE($4, distance_to_entry_pct),
            distance_to_sl_pct = COALESCE($5, distance_to_sl_pct),
            distance_to_tp1_pct = COALESCE($6, distance_to_tp1_pct),
            time_in_trade_minutes = COALESCE($7, time_in_trade_minutes),
            price_updated_at = NOW(),
            updated_at = NOW()
        WHERE id = $8
          AND (current_price IS DISTINCT FROM $1
               OR unrealized_pnl_pct IS DISTINCT FROM $2
               OR unrealized_pnl_usd IS DISTINCT FROM $3
               OR distance_to_entry_pct IS DISTINCT FROM COALESCE($4, distance_to_entry_pct)
               OR distance_to_sl_pct IS DISTINCT FROM COALESCE($5, distance_to_sl_pct)
               OR distance_to_tp1_pct IS DISTINCT FROM COALESCE($6, distance_to_tp1_pct)
               OR time_in_trade_minutes IS DISTINCT FROM COALESCE($7, time_in_trade_minutes))
        """,
        (
            current_price,
            pnl_pct,
            unrealized_pnl_usd,
            distance_to_entry_pct,
            distance_to_sl_pct,
            distance_to_tp1_pct,
            time_in_trade_minutes,
            position_id,
        ),
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
            LIMIT 1000
            """,
        )

        if not pending:
            return {"activated": 0, "expired": 0, "expired_lost_race": 0, "no_price": 0, "error": 0}

        activated = 0
        expired = 0
        # Round-6 sweep (BH2 #8): split the lost-race counter from the success
        # counter so dashboards can distinguish "nothing to expire" from
        # "expired UPDATE returned 0 rows because a sibling monitor already
        # flipped this row". Without this, every concurrent expire-vs-activate
        # race was silently classified as "no rows expired".
        expired_lost_race = 0
        no_price = 0
        error = 0
        expiry_cutoff = now - timedelta(days=7)

        for pos in pending:
            if self._stop.is_set():
                break
            try:
                pos_id = int(pos["id"])
                created = pos["created_at"]
                # Expire positions older than 7 days.
                # Bug D fix (2026-05-24 second-round audit): mirror the H13
                # activation guard with `AND status='pending'` so a position
                # that was activated by another monitor instance between this
                # cycle's SELECT and this UPDATE cannot be flipped from 'open'
                # → 'expired'. Without the guard, two overlapping monitor
                # instances could clobber an open position with `expired`,
                # silently killing live trades. The guard makes the lost-race
                # case a no-op, with a clear log line so we know it happened.
                if isinstance(created, datetime) and created < expiry_cutoff:
                    expire_result = await pool.execute(
                        """
                        UPDATE public.tracked_positions
                        SET status = 'expired',
                            status_reason = 'entry_never_reached',
                            updated_at = $1
                        WHERE id = $2
                          AND status = 'pending'
                        """,
                        (now, pos_id),
                    )
                    if isinstance(expire_result, str) and expire_result.endswith(" 0"):
                        # Round-6 sweep: count it so the cycle summary
                        # surfaces the race; demote to DEBUG so a degenerate
                        # all-races sweep doesn't spam the journal.
                        expired_lost_race += 1
                        logger.debug(
                            "Expiry sweep lost race for position %s "
                            "(status changed concurrently to non-pending); skipping.",
                            pos_id,
                        )
                        continue
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
                # Round 11 (2026-05-24): when the touch is outside the fast
                # path (older than 30 min), tag the row so we can audit which
                # activations "caught up" vs which fired live. A normal/live
                # activation clears status_reason; a retro activation stamps
                # `retro_activated:<utc>` so dashboards can show a small
                # badge and operators can grep the log.
                trigger_age_s = (
                    (now - trigger_ts).total_seconds()
                    if isinstance(trigger_ts, datetime) else 0.0
                )
                is_retro = trigger_age_s > FAST_ACTIVATION_LOOKBACK_S
                retro_reason = (
                    f"retro_activated:{now.replace(microsecond=0).isoformat()}"
                    if is_retro else None
                )
                # Bug H13 fix (Code Analyzer 2 §2.6) — guard the activation
                # UPDATE against a concurrent state change. Without
                # `AND status = 'pending'`, the expiry sweep (or a manual
                # cancel) could mark a row 'expired' between our SELECT and
                # this UPDATE, and we'd still flip it to 'open' — reviving
                # a position that should have died. The guard turns that race
                # into a no-op.
                update_result = await pool.execute(
                    """
                    UPDATE public.tracked_positions
                    SET status = 'open',
                        status_reason = $4,
                        current_price = $1,
                        price_updated_at = $2,
                        updated_at = $2
                    WHERE id = $3
                      AND status = 'pending'
                    """,
                    (trigger_close, trigger_ts, pos_id, retro_reason),
                )
                # asyncpg returns "UPDATE n" — n=0 means we lost the race.
                if isinstance(update_result, str) and update_result.endswith(" 0"):
                    logger.info(
                        "Pending activation lost race for position %s "
                        "(status changed concurrently); skipping.",
                        pos_id,
                    )
                    continue
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
            "expired_lost_race": expired_lost_race,
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
            wick_scan_state: Dict[str, Any] = {}
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
                        scan_state=wick_scan_state,
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
            # Bug H2 fix (Bug Hunter 1 §C3 + Code Analyzer 2 §2.4):
            #   Previously we advanced `price_updated_at` to the GLOBAL
            #   `MAX(timestamp)` of all 1m candles. If the candle ingester
            #   then back-filled a candle older than that MAX (e.g. CCXT
            #   gap-fill of an exchange feed outage), it would land below
            #   our watermark and never be wick-scanned — silently missing
            #   real SL/TP hits.
            #
            #   Bug F fix (2026-05-24 second-round audit): when H12 keyset
            #   pagination exhausts at max_iterations (~83 days of 1m), the
            #   scan covers only `[wick_since, last_scanned_ts]`, not all
            #   the way to `now`. Advancing the watermark past
            #   `last_scanned_ts` would skip the unscanned tail forever.
            #   We now check `scan_state["complete"]` returned by the wick
            #   scanner: if False, watermark advances only to `last_scanned_ts`
            #   so the next cycle resumes from there. Same logic applies on
            #   query-failure paths.
            try:
                scan_complete = bool(wick_scan_state.get("complete", True))
                last_scanned_ts = wick_scan_state.get("last_scanned_ts")

                if not scan_complete:
                    # Bug F round-3 review fix (BH1 #4, BH1 #5):
                    #   An incomplete scan means we did NOT scan all the way
                    #   up to MAX(timestamp) or `now`, so advancing past the
                    #   actual progress would skip the unscanned tail. Resume
                    #   from `last_scanned_ts` if we have it; if we don't
                    #   (first-page failure with no CCXT fallback), HOLD the
                    #   watermark at `wick_since` so the next cycle re-tries
                    #   from where we started. Never jump forward on failure.
                    if last_scanned_ts is not None:
                        watermark = ensure_utc(last_scanned_ts)
                    else:
                        watermark = ensure_utc(wick_since)
                    logger.info(
                        "Position %s wick scan incomplete (last_scanned_ts=%s) — "
                        "watermark held at %s; next cycle will re-attempt forward "
                        "from this point.",
                        pos_id, last_scanned_ts, watermark,
                    )
                else:
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
                    # Choose the earlier of `now` and DB MAX so back-fills below
                    # `now` still get scanned next cycle.
                    if max_ts is not None and max_ts < now:
                        watermark = max_ts
                    else:
                        watermark = now
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
        # CCXT fallback — try live exchange ticker if all DB sources failed
        if price is None:
            try:
                from shared.market_data.live_price import fetch_live_price as _ccxt_price
                result = await _ccxt_price(symbol, instrument_exchange or "bybit", timeout_s=4.0)
                price = result.price
                logger.debug("position_monitor CCXT fallback: %s = %.4f", symbol, price)
            except Exception:
                pass

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

        # Round 12 (2026-05-24): mirror live distance + time-in-trade fields
        # back to tracked_positions alongside price/P&L. These four columns
        # used to live ONLY in the position_updates time-series, forcing
        # the dashboard to compute them client-side from row payload. Now
        # the daemon is the single source of truth for both the snapshot
        # (tracked_positions) and the history (position_updates) — they are
        # written in the same tick from the same PositionSnapshot.
        await update_position_price_pnl(
            pool,
            pos_id,
            snapshot.current_price,
            snapshot.pnl_pct,
            snapshot.unrealized_pnl,
            distance_to_entry_pct=snapshot.distance_to_entry_pct,
            distance_to_sl_pct=snapshot.distance_to_sl_pct,
            distance_to_tp1_pct=snapshot.distance_to_tp_pct,
            time_in_trade_minutes=int(snapshot.hours_open * 60),
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
