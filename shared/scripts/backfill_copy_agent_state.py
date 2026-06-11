"""
Module: backfill_copy_agent_state
Purpose: One-shot recovery: rebuild public.copy_agent_state from the
         historical record in public.competition_trades.

Background
----------
The LiveCopyTradeMonitor (`shared/intelligence/copy_trade_monitor.py`)
kept its 7 paper-trading agents' running balance, win/loss counters,
total P&L, and open-paper-position list in an in-memory `self._agents`
dict. Every service restart wiped that dict, so the dashboard's
`contest_participants` row froze at the round-defaults (balance=$1000,
trades=0, open_positions=0) the moment the service restarted.

The Round-7 persistence layer fixed this going forward (`copy_agent_state`
table + `_load_state` / `_save_state` methods). This script handles the
historical recovery: it derives each agent's correct end-state from
`competition_trades`, which has been writing through the entire outage
(per-trade rows, including pnl/fees/exit_reason).

What this script does
---------------------
For each of the 7 known agent_ids:
  1. Aggregate closed competition_trades  →  total_pnl, total_fees,
     wins, losses, trades. Computes balance = starting_balance + total_pnl.
  2. Collect open competition_trades (`exit_price IS NULL`)  →  rebuild the
     in-memory `open_positions` JSONB list (with sl/tp/leverage/entered_at
     so the live monitor can resume monitoring them on next tick).
  3. UPSERT into public.copy_agent_state.

Defaults
--------
  * `starting_balance` = $1000 (matches the existing seed and the
    in-memory __init__ default in copy_trade_monitor.py).
  * `entered_position_ids` = sorted distinct trader_ids from open positions.

Safety
------
  * `--dry-run` (default): prints the per-agent computed numbers and
    does NOT write anything.
  * `--apply`: actually writes via UPSERT. Re-running with `--apply` is
    idempotent (UPSERT on agent_id PK).
  * Will refuse to run if `public.copy_agent_state` doesn't exist
    (migration `2026_05_24_copy_agent_state.sql` must be applied first).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, "/opt/tickles")

from shared.utils.db import get_shared_pool
from shared.utils.config import load_env

logger = logging.getLogger("backfill_copy_agent_state")

CONTEST_ID = "copy-trade-scenarios"
STARTING_BALANCE = 1000.0
KNOWN_AGENTS = [
    "copy_spot_seq",
    "copy_lev_parallel",
    "copy_lev_be_lock",
    "copy_opt_spot_seq",
    "copy_opt_lev_parallel",
    "copy_opt_lev_be_lock",
    "copy_charthacker",
]


async def _ensure_table(pool) -> None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema='public' AND table_name='copy_agent_state'
            """
        )
        if row is None:
            raise SystemExit(
                "copy_agent_state table missing. Apply migration first:\n"
                "  PGPASSWORD=... psql -h 127.0.0.1 -U admin -d tickles_shared "
                "-f shared/intelligence/migrations/2026_05_24_copy_agent_state.sql"
            )


async def _aggregate_agent(pool, agent_id: str) -> Dict[str, Any]:
    """Build the persisted state for one agent from competition_trades."""
    async with pool.acquire() as conn:
        # Closed trades aggregate
        agg = await conn.fetchrow(
            """
            SELECT
              COALESCE(SUM(pnl),  0)::FLOAT8 AS total_pnl,
              COALESCE(SUM(fees), 0)::FLOAT8 AS total_fees,
              COUNT(*) FILTER (WHERE pnl >  0)::INT  AS wins,
              COUNT(*) FILTER (WHERE pnl <= 0)::INT  AS losses,
              COUNT(*)::INT                          AS trades
            FROM competition_trades
            WHERE contest_id=$1 AND agent_id=$2 AND exit_price IS NOT NULL
            """,
            CONTEST_ID, agent_id,
        )
        # Open paper-positions
        open_rows = await conn.fetch(
            """
            SELECT id, symbol, direction, entry_price, sl_price, tp_price,
                   allocated, leverage, entered_at, tracked_position_id
            FROM competition_trades
            WHERE contest_id=$1 AND agent_id=$2 AND exit_price IS NULL
            ORDER BY entered_at ASC
            """,
            CONTEST_ID, agent_id,
        )

    open_positions: List[Dict[str, Any]] = []
    entered_ids: List[int] = []
    for r in open_rows:
        ts = r["entered_at"]
        # JSONB column wants strings, not datetime; live monitor's
        # _load_state will fromisoformat() back when it loads.
        ts_iso = ts.isoformat() if ts is not None else None
        pos = {
            "trader_id": int(r["tracked_position_id"]) if r["tracked_position_id"] is not None else None,
            "symbol": r["symbol"],
            "direction": r["direction"],
            "entry": float(r["entry_price"]) if r["entry_price"] is not None else 0.0,
            "sl": float(r["sl_price"]) if r["sl_price"] is not None else 0.0,
            "tp": float(r["tp_price"]) if r["tp_price"] is not None else 0.0,
            "allocated": float(r["allocated"]) if r["allocated"] is not None else 0.0,
            "leverage": float(r["leverage"]) if r["leverage"] is not None else 1.0,
            # Conservative defaults — be_locked unknown from competition_trades,
            # but the live monitor will recompute be_price on first tick if
            # the agent's mode requires it.
            "be_locked": False,
            "be_price": 0.0,
            "entered_at": ts_iso,
        }
        open_positions.append(pos)
        if pos["trader_id"] is not None:
            entered_ids.append(pos["trader_id"])

    total_pnl = float(agg["total_pnl"] or 0.0)
    return {
        "agent_id": agent_id,
        "balance": STARTING_BALANCE + total_pnl,
        "starting_balance": STARTING_BALANCE,
        "total_pnl": total_pnl,
        "total_fees": float(agg["total_fees"] or 0.0),
        "wins": int(agg["wins"] or 0),
        "losses": int(agg["losses"] or 0),
        "trades": int(agg["trades"] or 0),
        "open_positions": open_positions,
        "entered_position_ids": sorted(set(entered_ids)),
    }


async def _write_state(pool, state: Dict[str, Any]) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO public.copy_agent_state
              (agent_id, balance, starting_balance, total_pnl, total_fees,
               wins, losses, trades, open_positions, entered_position_ids,
               updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb, now())
            ON CONFLICT (agent_id) DO UPDATE SET
              balance              = EXCLUDED.balance,
              starting_balance     = EXCLUDED.starting_balance,
              total_pnl            = EXCLUDED.total_pnl,
              total_fees           = EXCLUDED.total_fees,
              wins                 = EXCLUDED.wins,
              losses               = EXCLUDED.losses,
              trades               = EXCLUDED.trades,
              open_positions       = EXCLUDED.open_positions,
              entered_position_ids = EXCLUDED.entered_position_ids,
              updated_at           = now()
            """,
            state["agent_id"],
            state["balance"],
            state["starting_balance"],
            state["total_pnl"],
            state["total_fees"],
            state["wins"],
            state["losses"],
            state["trades"],
            json.dumps(state["open_positions"]),
            json.dumps(state["entered_position_ids"]),
        )


def _format_row(s: Dict[str, Any]) -> str:
    return (
        f"{s['agent_id']:<24}"
        f" balance=${s['balance']:>9.2f}"
        f" pnl=${s['total_pnl']:>+9.2f}"
        f" fees=${s['total_fees']:>6.2f}"
        f" trades={s['trades']:>3}"
        f" wins={s['wins']:>3}"
        f" losses={s['losses']:>3}"
        f" open={len(s['open_positions']):>3}"
    )


async def main(apply: bool) -> int:
    load_env()
    pool = await get_shared_pool()
    await _ensure_table(pool)

    logger.info("Backfilling %d agents from competition_trades (contest=%s)",
                len(KNOWN_AGENTS), CONTEST_ID)
    states = []
    for agent_id in KNOWN_AGENTS:
        state = await _aggregate_agent(pool, agent_id)
        states.append(state)
        logger.info("%s", _format_row(state))

    if not apply:
        logger.info("--- dry-run; no writes performed ---")
        logger.info("re-run with --apply to commit.")
        return 0

    logger.info("applying via UPSERT into public.copy_agent_state ...")
    for state in states:
        await _write_state(pool, state)
        logger.info("wrote %s", state["agent_id"])
    logger.info("done.")
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true",
                    help="Actually write to public.copy_agent_state. "
                         "Without this flag the script is a dry-run.")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(apply=args.apply)))
