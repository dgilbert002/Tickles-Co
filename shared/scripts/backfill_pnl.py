"""
One-shot backfill: recompute realized_pnl_usd_final for closed positions
where it's currently $0 but notional_usd and entry/exit prices exist.

The qty derivation fix in position_monitor._settle_close now correctly
derives qty from notional_usd/entry_price, but 197 positions were closed
before the fix and have $0.00 P&L.
"""
import asyncio
import os
import sys
from decimal import Decimal

sys.path.insert(0, "/opt/tickles")
from shared.utils.config import load_env
from shared.utils.db import get_shared_pool

load_env()


async def backfill():
    pool = await get_shared_pool()
    async with pool.acquire() as conn:
        # Fetch closed positions with $0 P&L but valid prices and notional
        rows = await conn.fetch(
            """
            SELECT id, instrument_symbol, direction, entry_price, exit_price,
                   notional_usd, position_size, realized_pnl_usd_final, outcome
            FROM tracked_positions
            WHERE status = 'closed'
              AND realized_pnl_usd_final = 0.00000000
              AND notional_usd IS NOT NULL
              AND notional_usd > 0
              AND entry_price IS NOT NULL
              AND entry_price > 0
              AND exit_price IS NOT NULL
              AND exit_price > 0
            ORDER BY id
            """
        )

        updated = 0
        for r in rows:
            pos_id = r["id"]
            entry = float(r["entry_price"])
            exit_px = float(r["exit_price"])
            notional = float(r["notional_usd"])
            direction = r["direction"]

            # Simple P&L: qty = notional / entry, then P&L = qty * (exit - entry)
            qty = notional / entry
            if direction == "long":
                pnl = qty * (exit_px - entry)
            else:
                pnl = qty * (entry - exit_px)

            await conn.execute(
                "UPDATE tracked_positions SET realized_pnl_usd_final = $1, "
                "realized_pnl_usd = $1, realized_pnl_pct = $2, updated_at = NOW() "
                "WHERE id = $3",
                Decimal(str(round(pnl, 8))),
                Decimal(str(round((pnl / notional) * 100, 4))),
                pos_id,
            )
            updated += 1

        print(f"Backfilled P&L for {updated} positions")
        return updated


if __name__ == "__main__":
    asyncio.run(backfill())
