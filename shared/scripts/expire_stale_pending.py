"""Auto-expire pending tracked_positions that can never activate.

Run as: python3 -m shared.scripts.expire_stale_pending [--dry-run]

Expires pending signals where:
  - current_price is NULL (no price data available)
  - signal_timestamp older than 12 hours (stale)
  - Never hit their entry (status still 'pending')

These are typically :USDT perp-suffixed symbols that the position_monitor
can't price because the instruments table uses spot forms.
"""
import asyncio, os, sys
sys.path.insert(0, "/opt/tickles")
from datetime import datetime, timedelta, timezone
from shared.utils.db import get_shared_pool
from shared.utils.config import load_env

load_env()

async def main():
    dry_run = "--dry-run" in sys.argv
    pool = await get_shared_pool()
    
    async with pool.acquire() as conn:
        # Count stale pending
        count = await conn.fetchval("""
            SELECT COUNT(*) FROM tracked_positions
            WHERE status = 'pending'
              AND current_price IS NULL
              AND signal_timestamp < now() - interval '12 hours'
        """)
        
        if count == 0:
            print("No stale pending signals to expire.")
            return
        
        if dry_run:
            print(f"Would expire {count} stale pending signals (dry run)")
            rows = await conn.fetch("""
                SELECT instrument_symbol, direction, entry_price, signal_timestamp::date
                FROM tracked_positions
                WHERE status = 'pending'
                  AND current_price IS NULL
                  AND signal_timestamp < now() - interval '12 hours'
                ORDER BY signal_timestamp
            """)
            for r in rows:
                print(f"  {r['instrument_symbol']} {r['direction']} entry={r['entry_price']} since={r['signal_timestamp']}")
        else:
            result = await conn.execute("""
                UPDATE tracked_positions
                SET status = 'expired', status_reason = 'auto-expired (>12h, no price data)'
                WHERE status = 'pending'
                  AND current_price IS NULL
                  AND signal_timestamp < now() - interval '12 hours'
            """)
            print(f"Expired {count} stale pending signals")

asyncio.run(main())
