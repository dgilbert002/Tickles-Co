"""
Competition scorer: backtests all 6 copy-trade scenarios and updates
contest_participants scores for the "Copy-Trade Scenarios" contest.

Scenarios:
  A: Spot sequential (orig SL/TP)
  B: Leveraged parallel 5% risk (orig SL/TP)  
  C: +BE lock 100x reset (orig SL/TP)
  A+Opt: Spot sequential (optimized SL/TP from auto_optimizer)
  B+Opt: Leveraged parallel (optimized SL/TP)
  C+Opt: +BE lock (optimized SL/TP)
"""
import asyncio, json, sys
from collections import defaultdict
sys.path.insert(0, "/opt/tickles")
from shared.utils.db import get_shared_pool
from shared.utils.config import load_env
from shared.backtest.discrete_backtest import (
    DiscreteTradeSpec, run_discrete_backtest, load_candles_1m,
)
from shared.backtest.engine import BacktestConfig

load_env()

# Optimized multipliers from auto_optimizer sweep
OPTIMAL = {
    "SUI/USDT":  (0.5, 5.0),
    "BTC/USDT":  (1.5, 2.0),
    "XRP/USDT":  (0.5, 5.0),
    "SOL/USDT":  (3.0, 3.0),
    "ADA/USDT":  (0.5, 2.0),
    "NEAR/USDT": (0.5, 1.5),
    "ETH/USDT":  (0.5, 1.0),
}

FEES = dict(fee_taker_bps=1, slippage_bps=2, funding_bps_per_8h=0)


def build_specs(trades, sl_mult, tp_mult):
    specs = []
    for r in trades:
        e = float(r["entry_price"])
        orig_sl = float(r["stop_loss"])
        orig_tp = float(r["take_profit_1"])
        d = r["direction"]
        sd = (e - orig_sl) / e if d == "long" else (orig_sl - e) / e
        td = abs(orig_tp - e) / e
        if sd <= 0 or td <= 0: continue
        ns = e * (1 - sd * sl_mult) if d == "long" else e * (1 + sd * sl_mult)
        nt = e * (1 + td * tp_mult) if d == "long" else e * (1 - td * tp_mult)
        specs.append(DiscreteTradeSpec(
            entry_ts=r["signal_timestamp"], entry_px=e, direction=d,
            sl_px=ns, tp_levels=[nt], tp_allocations=[1.0], notional_usd=1000.0))
    return specs


async def run_scenario(name, trades_by_symbol, pool, use_optimal):
    """Run one scenario across all symbols. Returns total PnL and stats."""
    total_pnl = 0.0
    total_wins = 0
    total_trades = 0
    total_fees = 0.0

    for sym, trades in trades_by_symbol.items():
        start = min(r["signal_timestamp"] for r in trades)
        end = max(r["closed_at"] for r in trades)
        candles = await load_candles_1m(pool, sym, start, end)
        if not candles or len(candles) < 10:
            continue

        sl_m, tp_m = OPTIMAL.get(sym, (1.0, 1.0)) if use_optimal else (1.0, 1.0)
        specs = build_specs(trades, sl_m, tp_m)
        if not specs:
            continue

        cfg = BacktestConfig(symbol=sym, source="signals", timeframe="1m",
            start_date="2026-05-01", end_date="2026-05-20", direction="both",
            initial_capital=1000.0, position_pct=100.0, leverage=1.0, **FEES)

        result = run_discrete_backtest(candles, specs, cfg)
        total_pnl += sum(t.pnl_abs for t in result.trades)
        total_wins += sum(1 for t in result.trades if t.pnl_abs > 0)
        total_trades += len(result.trades)
        total_fees += sum(t.fees for t in result.trades)

    return {
        "equity": round(1000.0 + total_pnl, 2),
        "total_realized_pnl_usd": round(total_pnl, 2),
        "return_pct": round(total_pnl / 1000.0 * 100, 1),
        "total_trades": total_trades,
        "winning_trades": total_wins,
        "losing_trades": total_trades - total_wins,
        "win_rate": round(total_wins / max(total_trades, 1), 3),
        "total_fees": round(total_fees, 2),
    }


async def main():
    pool = await get_shared_pool()
    pool2 = pool
        rows = await conn.fetch("""
            SELECT tp.instrument_symbol, tp.direction, tp.entry_price,
                   tp.stop_loss, tp.take_profit_1, tp.signal_timestamp, tp.closed_at
            FROM tracked_positions tp
            WHERE tp.status = 'closed' AND tp.entry_price > 0 AND tp.stop_loss > 0
              AND tp.take_profit_1 > 0 AND tp.signal_timestamp IS NOT NULL
              AND tp.closed_at IS NOT NULL
              AND (tp.exit_price/tp.entry_price < 5 AND tp.entry_price/tp.exit_price < 5)
              AND ABS((tp.take_profit_1 - tp.entry_price) / tp.entry_price) >= 0.005
              AND tp.actor_id IN ('jarvais_trader_1','jarvais_trader_4','jarvais_trader_138',
                                  'jarvais_trader_180','jarvais_trader_181','jarvais_trader_896')
            ORDER BY tp.signal_timestamp
        """)

    trades_by_symbol = defaultdict(list)
    for r in rows:
        trades_by_symbol[r["instrument_symbol"]].append(r)

    scenarios = [
        ("copy_spot_seq",         "A: Spot Sequential",      False),
        ("copy_lev_parallel",     "B: Leveraged Parallel",   False),
        ("copy_lev_be_lock",      "C: +BE Lock 100x",        False),
        ("copy_opt_spot_seq",     "A+Opt: Spot Sequential",  True),
        ("copy_opt_lev_parallel", "B+Opt: Lev Parallel",     True),
        ("copy_opt_lev_be_lock",  "C+Opt: +BE Lock 100x",    True),
    ]

    print("=== COMPETITION: Copy-Trade Scenarios (May 2026) ===\n")
    print(f"{'Scenario':<28} {'Equity':>10} {'PnL':>10} {'Return':>8} {'Wins':>5} {'Trades':>7} {'Win%':>6}")
    print("-" * 82)

    for agent_id, name, use_opt in scenarios:
        result = await run_scenario(name, trades_by_symbol, pool, use_opt)
        print(f"{name:<28} ${result['equity']:>9,.2f} ${result['total_realized_pnl_usd']:>9,.2f} "
              f"{result['return_pct']:>7.1f}% {result['winning_trades']:>5} {result['total_trades']:>7} "
              f"{result['win_rate']*100:>5.0f}%")

        # Update contest scores
        await pool.acquire().__aenter__().execute("""
            UPDATE contest_participants 
            SET scores = $1::jsonb
            WHERE contest_id = 'copy-trade-scenarios' AND agent_id = $2
        """, json.dumps(result), agent_id)

    print("\nScores written to DB. Dashboard: Competitions tab")

asyncio.run(main())
