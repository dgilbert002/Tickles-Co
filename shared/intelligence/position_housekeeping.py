"""
Module: position_housekeeping
Purpose: Operator-callable housekeeping helpers for tracked_positions.

         * :func:`cleanup_retro_activations` — finds positions currently
           ``status='open'`` whose entry price was never actually touched
           by a 1m candle between ``created_at`` and ``updated_at``, marks
           them ``expired`` (and deletes any still-open
           ``competition_trades`` linked to them).
         * :func:`recheck_pending_activations` — runs ONE
           :meth:`PositionMonitor._activate_pending_positions` cycle out-
           of-band, so an agent can say "look again right now" instead
           of waiting for the daemon's next 60s poll.
         * :func:`close_wicked_positions` — scans every currently-open
           position and closes any whose SL/TP was wicked through by a
           1m candle since activation. Uses the SAME predicate the live
           monitor uses (``_find_sl_tp_wick_candle``) and routes through
           the same ``_settle_close`` path so backfilled closes are
           indistinguishable from live closes.

All functions are idempotent and safe to call repeatedly. They are
exposed via the MCP tools ``positions.cleanup_retros``,
``positions.recheck``, and ``positions.close_wicked``, and can also be
invoked directly from the CLI:

    python3 -m shared.intelligence.position_housekeeping cleanup-retros [--dry-run]
    python3 -m shared.intelligence.position_housekeeping recheck
    python3 -m shared.intelligence.position_housekeeping close-wicked [--dry-run]

Location: /opt/tickles/shared/intelligence/position_housekeeping.py
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cleanup — retro-activated rows
# ---------------------------------------------------------------------------
async def cleanup_retro_activations(
    *,
    lookback_hours: int = 48,
    dry_run: bool = False,
    include_orphan_competition_trades: bool = True,
) -> Dict[str, Any]:
    """Find and expire ``status='open'`` positions that were activated
    retroactively (no 1m candle between ``created_at`` and ``updated_at``
    had a ``[low, high]`` range containing ``entry_price``).

    Also (when ``include_orphan_competition_trades`` is True — default)
    deletes any still-open ``competition_trades`` rows whose
    ``tracked_position_id IS NULL``. These orphans were created by an
    earlier version of ``copy_trade_monitor`` that didn't populate the
    FK; they cannot be reasoned about by the candle-touch predicate
    (no parent → no created_at to evaluate against), so the only safe
    action is to delete them.

    The predicate matches the one the diagnostic at
    ``/opt/tickles/.cursor/debug_entry_retrofit.py`` uses, so a clean
    diagnostic implies a clean DB.

    Args:
        lookback_hours: Only inspect positions created in this window.
        dry_run: If True, report what would happen but write nothing.
        include_orphan_competition_trades: If True (default), also delete
            any still-open competition_trades whose tracked_position_id
            is NULL.

    Returns:
        Dict with keys:
          - ``candidates`` — n positions inspected by the retro predicate
          - ``expired_ids`` — list of tracked_positions IDs marked expired
          - ``tracked_updated`` — actual UPDATE count
          - ``competition_trades_deleted`` — n shadows deleted for the
            tracked_positions in ``expired_ids``
          - ``orphan_ct_candidates`` — n competition_trades with no parent
          - ``orphan_ct_deleted`` — actual DELETE count for orphans
          - ``dry_run``, ``lookback_hours``, ``orphan_sweep_enabled``
    """
    from shared.utils.db import get_shared_pool

    pool = await get_shared_pool()
    result: Dict[str, Any] = {
        "candidates": 0,
        "expired_ids": [],
        "tracked_updated": 0,
        "competition_trades_deleted": 0,
        "orphan_ct_candidates": 0,
        "orphan_ct_deleted": 0,
        "dry_run": dry_run,
        "lookback_hours": lookback_hours,
        "orphan_sweep_enabled": include_orphan_competition_trades,
    }

    async with pool.acquire() as conn:
        # ---- Part 1: retro-activated open positions ---------------------
        candidates = await conn.fetch(
            f"""
            SELECT id, instrument_symbol, entry_price,
                   created_at, updated_at
            FROM public.tracked_positions
            WHERE status = 'open'
              AND created_at > now() - interval '{lookback_hours} hours'
            """,
        )
        result["candidates"] = len(candidates)

        retro_ids: List[int] = []
        for c in candidates:
            sym = c["instrument_symbol"]
            entry = c["entry_price"]
            if sym is None or entry is None:
                continue
            entry_f = float(entry)
            hit = await conn.fetchrow(
                """
                SELECT c.timestamp
                FROM public.candles c
                JOIN public.instruments i ON i.id = c.instrument_id
                WHERE i.symbol = $1
                  AND c.timeframe = '1m'
                  AND c.timestamp > $2
                  AND c.timestamp <= $3
                  AND c.low <= $4
                  AND c.high >= $4
                LIMIT 1
                """,
                sym, c["created_at"], c["updated_at"], entry_f,
            )
            if hit is None:
                retro_ids.append(int(c["id"]))
        result["expired_ids"] = retro_ids

        # ---- Part 2: orphan competition_trades --------------------------
        # Open competition_trades with no parent tracked_position. These
        # cannot be reasoned about by the candle predicate (no parent →
        # no created_at) and are always garbage to delete. Toggleable so
        # this remains opt-in for callers that only want the retro sweep.
        orphan_ids: List[int] = []
        if include_orphan_competition_trades:
            orphans = await conn.fetch(
                """
                SELECT id FROM public.competition_trades
                WHERE (exited_at IS NULL OR exit_price IS NULL)
                  AND tracked_position_id IS NULL
                """
            )
            orphan_ids = [int(r["id"]) for r in orphans]
            result["orphan_ct_candidates"] = len(orphan_ids)

        # ---- Part 3: write phase (skip on dry-run) ----------------------
        if dry_run or (not retro_ids and not orphan_ids):
            return result

        async with conn.transaction():
            if retro_ids:
                tracked_res = await conn.execute(
                    """
                    UPDATE public.tracked_positions
                    SET status = 'expired',
                        status_reason = 'retro_entry_cleanup',
                        updated_at = now()
                    WHERE id = ANY($1::int[])
                      AND status = 'open'
                    """,
                    retro_ids,
                )
                result["tracked_updated"] = _extract_count(tracked_res)

                ct_res = await conn.execute(
                    """
                    DELETE FROM public.competition_trades
                    WHERE tracked_position_id = ANY($1::int[])
                      AND (exited_at IS NULL OR exit_price IS NULL)
                    """,
                    retro_ids,
                )
                result["competition_trades_deleted"] = _extract_count(ct_res)

            if orphan_ids:
                orphan_res = await conn.execute(
                    """
                    DELETE FROM public.competition_trades
                    WHERE id = ANY($1::int[])
                      AND (exited_at IS NULL OR exit_price IS NULL)
                      AND tracked_position_id IS NULL
                    """,
                    orphan_ids,
                )
                result["orphan_ct_deleted"] = _extract_count(orphan_res)

    logger.info("cleanup_retro_activations result=%s", result)
    return result


def _extract_count(asyncpg_status: str) -> int:
    """Pull the row-count out of an asyncpg COMMAND COMPLETE tag.

    e.g. ``'UPDATE 7'`` -> 7, ``'DELETE 11'`` -> 11, ``'INSERT 0 3'`` -> 3.
    """
    try:
        parts = (asyncpg_status or "").strip().split()
        return int(parts[-1])
    except (ValueError, IndexError):
        return 0


# ---------------------------------------------------------------------------
# Close-wicked — catch positions whose SL/TP was already breached
# ---------------------------------------------------------------------------
async def close_wicked_positions(
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Close any currently-open positions whose SL or TP was wicked
    through by a 1m candle between activation and now.

    Used to backfill the gap created by the old close-only check
    (``check_sl_tp_hit`` compared current candle close to SL/TP, which
    missed intra-candle wicks that the trader's broker would have filled
    at). After the 2026-05-22 fix the live monitor catches new wicks
    itself; this function exists to clear any historical leftovers AND
    as a safety-net callable from MCP for the agent to run on demand.

    Both the wick-detection predicate (``_find_sl_tp_wick_candle``) and
    the settlement path (``_settle_close``) are the same ones the live
    monitor uses, so backfilled closes are indistinguishable from
    real-time closes.

    Args:
        dry_run: If True, report what would be closed without writing.

    Returns:
        Dict with keys:
          - ``candidates`` — open positions inspected
          - ``closed`` — list of {position_id, outcome, hit_price,
                                  candle_ts, realized_pnl?}
          - ``deferred`` — close attempts that fell into _settle_close's
                           settle_deferred path (no profile)
          - ``no_wick`` — positions still legitimately open
          - ``error`` — count of positions that raised during processing
          - ``dry_run``
    """
    from datetime import datetime, timezone
    from shared.intelligence.position_monitor import (
        PositionMonitor, MonitorConfig,
        _find_sl_tp_wick_candle, _build_snapshot,
        write_position_update,
    )
    from shared.utils.db import get_shared_pool

    pool = await get_shared_pool()
    cfg = MonitorConfig()
    monitor = PositionMonitor(cfg=cfg)
    now = datetime.now(timezone.utc)

    result: Dict[str, Any] = {
        "candidates": 0,
        "closed": [],
        "deferred": [],
        "no_wick": 0,
        "error": 0,
        "dry_run": dry_run,
    }

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, instrument_symbol, instrument_exchange, direction,
                   entry_price, stop_loss, take_profit_1,
                   position_size, notional_usd, leverage,
                   created_at, updated_at, price_updated_at,
                   signal_timestamp, expiry_at
            FROM public.tracked_positions
            WHERE status = 'open'
              AND (stop_loss IS NOT NULL OR take_profit_1 IS NOT NULL)
            """,
        )
    result["candidates"] = len(rows)

    for r in rows:
        pos = dict(r)
        pos_id = int(pos["id"])
        try:
            wick_since = (
                pos.get("price_updated_at")
                or pos.get("updated_at")
                or pos.get("created_at")
            )
            if wick_since is None:
                result["no_wick"] += 1
                continue
            sl = pos.get("stop_loss")
            tp = pos.get("take_profit_1")
            wick = await _find_sl_tp_wick_candle(
                pool,
                symbol=pos["instrument_symbol"],
                exchange=pos.get("instrument_exchange"),
                timeframe=cfg.default_timeframe,
                direction=pos.get("direction", ""),
                stop_loss=float(sl) if sl is not None else None,
                take_profit=float(tp) if tp is not None else None,
                since=wick_since,
            )
            if wick is None:
                result["no_wick"] += 1
                continue
            wick_ts, _wick_close, hit_type, hit_price = wick
            outcome = "sl_hit" if hit_type == "sl" else "tp1_hit"
            if dry_run:
                result["closed"].append({
                    "position_id": pos_id,
                    "symbol": pos["instrument_symbol"],
                    "direction": pos["direction"],
                    "outcome": outcome,
                    "hit_price": hit_price,
                    "candle_ts": str(wick_ts),
                    "dry_run": True,
                })
                continue
            snapshot = _build_snapshot(pos, hit_price, wick_ts)
            if snapshot is None:
                result["deferred"].append({
                    "position_id": pos_id, "reason": "missing_levels",
                })
                continue
            await write_position_update(pool, snapshot)
            breakdown = await monitor._settle_close(
                pool, pos, snapshot, outcome, hit_price, wick_ts,
            )
            if breakdown is None:
                result["deferred"].append({
                    "position_id": pos_id,
                    "reason": f"{outcome}_no_profile",
                })
                continue
            result["closed"].append({
                "position_id": pos_id,
                "symbol": pos["instrument_symbol"],
                "direction": pos["direction"],
                "outcome": outcome,
                "hit_price": hit_price,
                "candle_ts": str(wick_ts),
                "realized_pnl": float(breakdown.net_pnl_usd),
            })
        except Exception as exc:
            logger.exception("close_wicked_positions: position %s failed: %s", pos_id, exc)
            result["error"] += 1

    logger.info(
        "close_wicked_positions result: candidates=%d closed=%d deferred=%d "
        "no_wick=%d error=%d dry_run=%s",
        result["candidates"], len(result["closed"]), len(result["deferred"]),
        result["no_wick"], result["error"], dry_run,
    )
    return result


# ---------------------------------------------------------------------------
# Recheck — out-of-band activation cycle
# ---------------------------------------------------------------------------
async def recheck_pending_activations() -> Dict[str, Any]:
    """Run one :meth:`PositionMonitor._activate_pending_positions` cycle.

    Useful when an agent has just inserted a chart-derived position and
    doesn't want to wait the full 60s for the daemon's next poll. Honours
    the same recency bound (``activation_lookback_s``) as the daemon, so
    repeated calls are safe and never retro-activate stale touches.

    Returns:
        The PositionMonitor's per-cycle counts (activated / expired /
        no_price / error). On error: ``{'status': 'error', 'message': ...}``.
    """
    from shared.intelligence.position_monitor import MonitorConfig, PositionMonitor
    from shared.utils.db import get_shared_pool

    monitor = PositionMonitor(cfg=MonitorConfig())
    pool = await get_shared_pool()
    try:
        # _activate_pending_positions is the same method the daemon runs
        # each tick. Calling it directly here just runs ONE cycle.
        counts = await monitor._activate_pending_positions(
            pool, datetime.now(timezone.utc),
        )
        return {"status": "ok", **counts}
    except Exception as exc:
        logger.exception("recheck_pending_activations failed")
        return {"status": "error", "message": str(exc)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Operator-callable housekeeping for tracked_positions: detect "
            "retro-activated rows and/or force a pending-activation cycle."
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

    p_cleanup = sub.add_parser(
        "cleanup-retros",
        help="Find and expire open positions with no real candle touch.",
    )
    p_cleanup.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be expired without writing.",
    )
    p_cleanup.add_argument(
        "--lookback-hours", type=int, default=48,
        help="Only consider positions created in the last N hours.",
    )

    sub.add_parser(
        "recheck",
        help="Run one pending-activation cycle out-of-band.",
    )

    p_wicked = sub.add_parser(
        "close-wicked",
        help="Close any open positions whose SL/TP was already wicked through.",
    )
    p_wicked.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be closed without writing.",
    )
    return p


async def _amain(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if args.command == "cleanup-retros":
        result = await cleanup_retro_activations(
            lookback_hours=args.lookback_hours, dry_run=args.dry_run,
        )
    elif args.command == "recheck":
        result = await recheck_pending_activations()
    elif args.command == "close-wicked":
        result = await close_wicked_positions(dry_run=args.dry_run)
    else:  # pragma: no cover — argparse enforces required subcommand
        return 2
    print(result)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    sys.exit(main())
