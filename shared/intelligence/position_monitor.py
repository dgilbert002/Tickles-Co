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
               expiry_at, signal_timestamp
        FROM public.tracked_positions
        WHERE status IN ('open', 'partial_exit')
        ORDER BY created_at DESC
        LIMIT $1
        """,
        (batch_size,),
    )


async def _resolve_instrument_id(
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
    if raw_entry is None or raw_qty is None:
        logger.info(
            "Position %s skipped (missing_levels): entry_price=%s position_size=%s",
            position.get("id"),
            raw_entry,
            raw_qty,
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
    tp = position.get("take_profit_1")
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

    return PositionSnapshot(
        position_id=position["id"],
        current_price=current_price,
        unrealized_pnl=unrealized_pnl_usd,
        pnl_pct=pnl_pct,
        distance_to_entry_pct=distance_to_entry_pct,
        distance_to_sl=dist["distance_to_sl"],
        distance_to_sl_pct=dist["distance_to_sl_pct"],
        distance_to_tp=dist["distance_to_tp"],
        distance_to_tp_pct=dist["distance_to_tp_pct"],
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
            breakdown = await compute_realized_pnl_db(
                pool,
                instrument_id=instrument_id,
                direction=position["direction"],
                entry_price=position["entry_price"],
                exit_price=exit_price,
                qty=position["position_size"],
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

        # Fetch price
        price: Optional[float] = None
        if epic:
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
        """Run one monitoring cycle: fetch open positions, snapshot each.

        Returns:
            Summary dict with counts.
        """
        pool = await self._ensure_pool()
        now = datetime.now(timezone.utc)
        positions = await fetch_open_positions(pool, self.cfg.batch_size)

        if not positions:
            logger.debug("No open positions to monitor")
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
                logger.exception("Error processing position %s: %s", pos.get("id"), exc)

        self._cycle_count += 1
        logger.info(
            "Cycle %s: monitored=%s closed=%s no_price=%s settle_deferred=%s",
            self._cycle_count, monitored, closed, no_price, settle_deferred,
        )
        return {
            "monitored": monitored,
            "closed": closed,
            "no_price": no_price,
            "settle_deferred": settle_deferred,
        }

    async def run_forever(self) -> None:
        """Main loop: run cycles until stopped."""
        logger.info(
            "PositionMonitor starting (poll=%.0fs batch=%s opinion_every=%s)",
            self.cfg.poll_interval_s,
            self.cfg.batch_size,
            self.cfg.agent_opinion_every_n,
        )
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
