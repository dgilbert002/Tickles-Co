import sys
import os
import argparse
import asyncio
from datetime import datetime, timezone, timedelta

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from shared.utils.db import get_shared_pool
from shared.intelligence.position_monitor import (
    _find_entry_touch_candle,
    _find_sl_tp_wick_candle,
    PositionMonitor,
    _resolve_instrument_id
)

async def main():
    parser = argparse.ArgumentParser(description="Retroactively activate and settle starved pending positions.")
    parser.add_argument("--apply", action="store_true", help="Apply changes to the database (defaults to dry-run)")
    parser.add_argument("--id", type=int, help="Target a specific position ID")
    args = parser.parse_args()

    pool = await get_shared_pool()
    
    if args.id:
        pending = await pool.fetch_all(
            "SELECT * FROM public.tracked_positions WHERE id = $1 AND status = 'pending'",
            (args.id,)
        )
    else:
        pending = await pool.fetch_all(
            "SELECT * FROM public.tracked_positions WHERE status = 'pending' ORDER BY id ASC"
        )
        
    print(f"Found {len(pending)} pending positions to inspect.")
    
    pm = PositionMonitor()
    activated_count = 0
    closed_count = 0
    expired_count = 0
    
    for pos in pending:
        pos_id = pos["id"]
        symbol = pos["instrument_symbol"]
        exchange = pos["instrument_exchange"]
        entry = float(pos["entry_price"])
        created = pos["created_at"]
        direction = pos["direction"]
        
        # Check if there is an entry touch since creation
        triggered = await _find_entry_touch_candle(
            pool=pool,
            symbol=symbol,
            exchange=exchange,
            timeframe="1m",
            entry=entry,
            since=created,
            not_before=None, # ignore the recency bound for retroactive recovery!
        )
        
        if triggered is None:
            # If older than 7 days, check if it should be expired
            expiry_cutoff = datetime.now(timezone.utc) - timedelta(days=7)
            if created < expiry_cutoff:
                print(f"Position {pos_id} ({symbol}): No entry touch found and older than 7 days. Expiring...")
                if args.apply:
                    # Bug D round-3 review fix (BH1 #10): mirror the
                    # `AND status='pending'` guard from position_monitor's
                    # expiry sweep so concurrent monitor-instance activations
                    # cannot be clobbered by this offline re-activation tool.
                    expire_result = await pool.execute(
                        """
                        UPDATE public.tracked_positions
                        SET status = 'expired',
                            status_reason = 'entry_never_reached',
                            updated_at = NOW()
                        WHERE id = $1
                          AND status = 'pending'
                        """,
                        (pos_id,)
                    )
                    if isinstance(expire_result, str) and expire_result.endswith(" 0"):
                        print(
                            f"Position {pos_id}: expiry skipped — status changed "
                            f"concurrently to non-pending (lost race, safe)."
                        )
                        continue
                expired_count += 1
            continue
            
        trigger_ts, trigger_close = triggered
        print(f"Position {pos_id} ({symbol} {direction}): Triggered entry @ {entry} on candle at {trigger_ts} (close={trigger_close})")
        
        # Now find if there's any SL/TP hit since entry trigger
        stop_loss = float(pos["stop_loss"]) if pos["stop_loss"] is not None else None
        
        # Look up take profits
        take_profit = None
        for i in range(1, 7):
            tp_val = pos.get(f"take_profit_{i}")
            if tp_val is not None:
                take_profit = float(tp_val)
                break # Use TP1 for simple wick exit detection
                
        exit_hit = await _find_sl_tp_wick_candle(
            pool=pool,
            symbol=symbol,
            exchange=exchange,
            timeframe="1m",
            direction=direction,
            stop_loss=stop_loss,
            take_profit=take_profit,
            since=trigger_ts,
        )
        
        if exit_hit is not None:
            hit_ts, hit_close, hit_type, hit_price = exit_hit
            outcome = f"{hit_type}_hit"
            print(f"  -> Exit HIT: {outcome} at price {hit_price} on candle {hit_ts} (close={hit_close})")
            
            if args.apply:
                activate_result = await pool.execute(
                    """
                    UPDATE public.tracked_positions
                    SET status = 'open',
                        current_price = $1,
                        price_updated_at = $2,
                        updated_at = $2
                    WHERE id = $3
                      AND status = 'pending'
                    """,
                    (trigger_close, trigger_ts, pos_id)
                )
                if isinstance(activate_result, str) and activate_result.endswith(" 0"):
                    print(
                        f"  -> Activate SKIPPED: position {pos_id} no longer pending "
                        f"(monitor or another retro run won the race); not closing."
                    )
                    continue
                fresh_pos = await pool.fetch_one("SELECT * FROM public.tracked_positions WHERE id = $1", (pos_id,))
                fresh_pos = await pool.fetch_one("SELECT * FROM public.tracked_positions WHERE id = $1", (pos_id,))
                breakdown = await pm._settle_close(
                    pool=pool,
                    position=dict(fresh_pos),
                    snapshot=None,
                    outcome=outcome,
                    exit_price=hit_price,
                    now=hit_ts,
                )
                if breakdown:
                    print(f"  -> Settle successful: Net P&L USD = {breakdown.net_pnl_usd}")
                    closed_count += 1
                else:
                    print(f"  -> Settle FAILED!")
            else:
                closed_count += 1
        else:
            print(f"  -> Still OPEN (no SL/TP hit yet since activation)")
            if args.apply:
                activate_result = await pool.execute(
                    """
                    UPDATE public.tracked_positions
                    SET status = 'open',
                        current_price = $1,
                        price_updated_at = $2,
                        updated_at = $2
                    WHERE id = $3
                      AND status = 'pending'
                    """,
                    (trigger_close, trigger_ts, pos_id)
                )
                if isinstance(activate_result, str) and activate_result.endswith(" 0"):
                    print(
                        f"  -> Activate SKIPPED: position {pos_id} no longer pending "
                        f"(monitor or another retro run won the race)."
                    )
                    continue
                activated_count += 1
            else:
                activated_count += 1
                
    print(f"\nSummary:")
    print(f"  Dry-run: {'No' if args.apply else 'Yes'}")
    print(f"  Expired (no touch & >7d): {expired_count}")
    print(f"  Activated (remains open): {activated_count}")
    print(f"  Closed (exited): {closed_count}")

if __name__ == "__main__":
    asyncio.run(main())
