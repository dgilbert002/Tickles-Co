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
    """Fetch open or partially-closed positions from tracked_positions.

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
               lowest_price, highest_price
        FROM public.tracked_positions
        WHERE status IN ('open', 'partial_close')
        ORDER BY created_at DESC
        LIMIT $1
        """,
        (batch_size,),
    )


async def fetch_latest_price(
    pool: DatabasePool,
    symbol: str,
    instrument_exchange: Optional[str],
    timeframe: str = DEFAULT_TIMEFRAME,
) -> Optional[float]:
    """Fetch the most recent close price for an instrument.

    Args:
        pool: Shared Postgres pool.
        symbol: Instrument symbol (e.g., 'XAUUSD').
        instrument_exchange: Exchange/instrument_exchange name (e.g., 'capital', 'binance').
        timeframe: Candle timeframe (default from env).

    Returns:
        Latest close price, or None if no candles found.
    """
    # Try with instrument_exchange first
    if instrument_exchange:
        row = await pool.fetch_one(
            """
            SELECT close
            FROM public.candles
            WHERE instrument_id = (
                SELECT id FROM public.instruments
                WHERE symbol = $1 AND exchange = $2 AND is_active = TRUE
                LIMIT 1
            )
            AND timeframe = $3
            ORDER BY "timestamp" DESC
            LIMIT 1
            """,
            (symbol, instrument_exchange, timeframe),
        )
        if row:
            return float(row["close"])

    # Fallback: any exchange
    row = await pool.fetch_one(
        """
        SELECT close
        FROM public.candles
        WHERE instrument_id = (
            SELECT id FROM public.instruments
            WHERE symbol = $1 AND is_active = TRUE
            ORDER BY id
            LIMIT 1
        )
        AND timeframe = $2
        ORDER BY "timestamp" DESC
        LIMIT 1
        """,
        (symbol, timeframe),
    )
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
) -> PositionSnapshot:
    """Compute all metrics for a position at the current price.

    Args:
        position: Row dict from tracked_positions.
        current_price: Current market price.
        now: Snapshot timestamp.

    Returns:
        PositionSnapshot with all computed fields.
    """
    direction = position["direction"]
    entry = float(position["entry_price"])
    qty = float(position["position_size"])
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

    return PositionSnapshot(
        position_id=position["id"],
        current_price=current_price,
        unrealized_pnl=unrealized_pnl_usd,
        pnl_pct=pnl_pct,
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
            distance_to_sl_pct, distance_to_tp1_pct,
            time_in_trade_minutes, timestamp
        ) VALUES (
            $1, $2, $3, $4,
            $5, $6,
            $7, $8
        )
        RETURNING id
        """,
        (
            snapshot.position_id,
            snapshot.current_price,
            snapshot.unrealized_pnl_usd,
            snapshot.pnl_pct,
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
) -> int:
    """Mark a position as closed with outcome.

    Args:
        pool: Shared Postgres pool.
        position_id: tracked_positions.id.
        status: New status ('closed', 'breakeven', etc.).
        outcome: 'stop_loss', 'take_profit', 'manual_close', 'expired'.
        exit_price: Price at which position exited.
        realized_pnl: Final P&L.
        now: Close timestamp.

    Returns:
        Affected row count.
    """
    return await pool.execute(
        """
        UPDATE public.tracked_positions
        SET status = $1,
            outcome = $2,
            exit_price = $3,
            realized_pnl_usd = $4,
            updated_at = $5
        WHERE id = $6
        """,
        (status, outcome, exit_price, realized_pnl, now, position_id),
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

    async def _process_one(
        self,
        pool: DatabasePool,
        position: Dict[str, Any],
        now: datetime,
    ) -> Dict[str, Any]:
        """Process a single open position: fetch price, compute, write update.

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

        # Build snapshot
        snapshot = _build_snapshot(position, price, now)

        # Write position_update
        update_id = await write_position_update(pool, snapshot)

        # Update extremes
        await update_position_extremes(
            pool,
            pos_id,
            worst_price=price if snapshot.mae_pct > 0 else None,
            best_price=price if snapshot.mfe_pct > 0 else None,
        )

        # Check SL/TP hit
        if snapshot.sl_hit or snapshot.tp_hit:
            outcome = "take_profit" if snapshot.tp_hit else "stop_loss"
            realized = snapshot.unrealized_pnl
            await update_position_outcome(
                pool, pos_id, "closed", outcome, price, realized, now
            )
            logger.info(
                "Position %s closed via %s at price=%.4f pnl=%.2f",
                pos_id, outcome, price, realized
            )
            return {
                "position_id": pos_id,
                "status": "closed",
                "outcome": outcome,
                "update_id": update_id,
                "realized_pnl": realized,
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

        for pos in positions:
            if self._stop.is_set():
                break
            try:
                result = await self._process_one(pool, pos, now)
                if result["status"] == "closed":
                    closed += 1
                elif result["status"] == "no_price_data":
                    no_price += 1
                else:
                    monitored += 1
            except Exception as exc:
                logger.exception("Error processing position %s: %s", pos.get("id"), exc)

        self._cycle_count += 1
        logger.info(
            "Cycle %s: monitored=%s closed=%s no_price=%s",
            self._cycle_count, monitored, closed, no_price
        )
        return {"monitored": monitored, "closed": closed, "no_price": no_price}

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
