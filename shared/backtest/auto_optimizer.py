"""
Auto-optimizer: periodically sweeps closed trader signals, finds best
SL/TP multipliers per symbol, stores results in DB for live signal adjustment.

Run as: python3 -m shared.backtest.auto_optimizer [--once]
"""
import asyncio
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, "/opt/tickles")

from shared.utils.db import get_shared_pool
from shared.utils.config import load_env
from shared.backtest.discrete_backtest import (
    DiscreteTradeSpec, run_discrete_backtest, load_candles_1m,
)
from shared.backtest.engine import BacktestConfig

logger = logging.getLogger("auto_optimizer")

# Sweep ranges
SL_MULTS = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0]
TP_MULTS = [0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0]

# Minimum trades per symbol to bother optimizing
MIN_TRADES = 3


async def fetch_closed_trades(pool):
    """Fetch closed trades with SL+TP from tracked_positions."""
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT tp.instrument_symbol, tp.direction, tp.entry_price,
                   tp.stop_loss, tp.take_profit_1, tp.signal_timestamp, tp.closed_at,
                   tp.actor_id
            FROM tracked_positions tp
            WHERE tp.status = 'closed' AND tp.entry_price > 0 AND tp.stop_loss > 0
              AND tp.take_profit_1 > 0 AND tp.signal_timestamp IS NOT NULL
              AND tp.closed_at IS NOT NULL
              AND (tp.exit_price/tp.entry_price < 5 AND tp.entry_price/tp.exit_price < 5)
              AND ABS((tp.take_profit_1 - tp.entry_price) / tp.entry_price) >= 0.005
              AND tp.actor_id LIKE 'jarvais_trader_%'
            ORDER BY tp.signal_timestamp
        """)


async def optimize_symbol(pool, sym, trades):
    """Run full sweep, return best multipliers + stats."""
    start = min(r["signal_timestamp"] for r in trades)
    end = max(r["closed_at"] for r in trades)
    candles = await load_candles_1m(pool, sym, start, end)
    if not candles or len(candles) < 10:
        return None

    best = None
    default_pnl = None

    for sl_m in SL_MULTS:
        for tp_m in TP_MULTS:
            specs = []
            for r in trades:
                e = float(r["entry_price"])
                orig_sl = float(r["stop_loss"])
                orig_tp = float(r["take_profit_1"])
                d = r["direction"]

                orig_sl_dist = (e - orig_sl) / e if d == "long" else (orig_sl - e) / e
                orig_tp_dist = abs(orig_tp - e) / e
                if orig_sl_dist <= 0 or orig_tp_dist <= 0:
                    continue

                new_sl = e * (1 - orig_sl_dist * sl_m) if d == "long" else e * (1 + orig_sl_dist * sl_m)
                new_tp = e * (1 + orig_tp_dist * tp_m) if d == "long" else e * (1 - orig_tp_dist * tp_m)

                specs.append(DiscreteTradeSpec(
                    entry_ts=r["signal_timestamp"], entry_px=e, direction=d,
                    sl_px=new_sl, tp_levels=[new_tp], tp_allocations=[1.0],
                    notional_usd=1000.0,
                ))

            if not specs:
                continue

            cfg = BacktestConfig(
                symbol=sym, source="signals", timeframe="1m",
                start_date="2026-05-01", end_date="2026-05-20", direction="both",
                initial_capital=1000.0, position_pct=100.0, leverage=1.0,
                fee_taker_bps=1, slippage_bps=2, funding_bps_per_8h=0,
            )

            result = run_discrete_backtest(candles, specs, cfg)
            wins = sum(1 for t in result.trades if t.pnl_abs > 0)
            pnl = sum(t.pnl_abs for t in result.trades)

            if sl_m == 1.0 and tp_m == 1.0:
                default_pnl = pnl

            if best is None or pnl > best["pnl"]:
                best = {"sl_mult": sl_m, "tp_mult": tp_m, "pnl": pnl,
                        "wins": wins, "total": len(result.trades)}

    if best is None:
        return None

    return {
        "symbol": sym,
        "sl_mult": best["sl_mult"],
        "tp_mult": best["tp_mult"],
        "optimized_pnl": best["pnl"],
        "default_pnl": default_pnl or best["pnl"],
        "improvement": best["pnl"] - (default_pnl or 0),
        "win_rate": best["wins"] / max(best["total"], 1),
        "n_trades": len(trades),
        "n_signals": best["total"],
        "optimized_at": datetime.now(timezone.utc).isoformat(),
    }


async def store_optimizations(pool, results):
    """Store optimization results to DB for live signal adjustment."""
    async with pool.acquire() as conn:
        for r in results:
            if r is None:
                continue
            await conn.execute("""
                INSERT INTO system_config (namespace, config_key, config_value, updated_at)
                VALUES ('auto_opt', $1, $2::jsonb, NOW())
                ON CONFLICT (namespace, config_key) DO UPDATE SET config_value = EXCLUDED.config_value, updated_at = NOW()
            """, f"auto_opt_{r['symbol']}", json.dumps(r))


async def run_once():
    """Single optimization run."""
    load_env()
    pool = await get_shared_pool()

    trades = await fetch_closed_trades(pool)
    symbol_groups = defaultdict(list)
    for t in trades:
        symbol_groups[t["instrument_symbol"]].append(t)

    print(f"Auto-optimizer: {len(symbol_groups)} symbols, {len(trades)} trades")
    results = []

    for sym, group in sorted(symbol_groups.items()):
        if len(group) < MIN_TRADES:
            continue
        r = await optimize_symbol(pool, sym, group)
        if r:
            results.append(r)
            print(f"  {sym:<18} SL×{r['sl_mult']:.1f} TP×{r['tp_mult']:.1f} "
                  f"→ ${r['optimized_pnl']:,.0f} (was ${r['default_pnl']:,.0f}, "
                  f"+${r['improvement']:,.0f})")

    if results:
        await store_optimizations(pool, results)
        print(f"\nStored {len(results)} optimizations to system_config")

    # Print readable summary
    print(f"\n=== OPTIMAL MULTIPLIERS ===")
    for r in sorted(results, key=lambda x: -x["improvement"]):
        arrow = "↑" if r["sl_mult"] < 1.0 else ("↓" if r["sl_mult"] > 1.0 else "→")
        tp_arrow = "↑" if r["tp_mult"] > 1.0 else ("↓" if r["tp_mult"] < 1.0 else "→")
        print(f"  {r['symbol']:<18} SL{arrow}{r['sl_mult']:.1f}x  TP{tp_arrow}{r['tp_mult']:.1f}x  "
              f"+${r['improvement']:,.0f}  ({r['win_rate']:.0%} win, {r['n_trades']} trades)")

    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    results = asyncio.run(run_once())
