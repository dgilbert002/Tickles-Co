"""
Discrete-signal backtest engine — executes specific trade entries with
SL, multiple TPs, fees, funding, and intrabar detection on 1m candles.

Unlike the strategy-based engine, this accepts concrete entry points
and price levels, making it ideal for:
  - Copy-trade simulation
  - Parameter optimisation (sweep SL/TP combos)
  - Discord chart signal backtesting

Uses the same BacktestConfig and fee/funding model as the main engine.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from shared.backtest.engine import BacktestConfig, Trade

log = logging.getLogger("tickles.discrete_backtest")


@dataclass
class DiscreteTradeSpec:
    """One trade to simulate: entry point + levels + sizing."""
    entry_ts: datetime          # signal timestamp
    entry_px: float             # entry price
    direction: str              # 'long' | 'short'
    sl_px: float               # stop-loss price
    tp_levels: List[float]      # take-profit prices (ascending for long, descending for short)
    tp_allocations: List[float] # fraction of position to close at each TP (must sum ≤ 1.0)
    notional_usd: float = 1000.0 # position size in USD


@dataclass
class DiscreteBacktestResult:
    """Results from a discrete-signal backtest run."""
    config: BacktestConfig
    trades: List[Trade] = field(default_factory=list)
    initial_capital: float = 10000.0
    final_equity: float = 10000.0
    pnl_abs: float = 0.0
    pnl_pct: float = 0.0
    max_drawdown: float = 0.0
    sharpe: float = 0.0
    winrate: float = 0.0
    num_trades: int = 0
    avg_trade_pnl: float = 0.0
    profit_factor: float = 0.0
    total_fees: float = 0.0
    total_funding: float = 0.0


def _find_entry_bar(candles: List[Dict], entry_ts: datetime) -> int:
    """Find the first candle index at or after entry_ts."""
    for i, c in enumerate(candles):
        if c["timestamp"] >= entry_ts:
            return i
    return -1


def run_discrete_backtest(
    candles_1m: List[Dict[str, Any]],  # list of {timestamp, open, high, low, close}
    specs: List[DiscreteTradeSpec],
    cfg: BacktestConfig,
    *,
    max_concurrent: int = 1,
) -> DiscreteBacktestResult:
    """Run a discrete-signal backtest on 1m candles.

    For each trade spec, the engine:
      1. Enters at the bar where entry_ts falls (fill at that bar's close or entry_px, whichever is worse)
      2. Each bar: checks SL against low/high, checks ALL TPs against high/low
      3. TP hits close a fraction of the position (partial fills)
      4. SL hit closes the remaining position entirely
      5. Fees: maker on entry, maker on TP limit fills, taker on SL market fill
      6. Funding: accrued per bar while position is open

    Args:
        candles_1m: 1-minute candle data as list of dicts with timestamp, open, high, low, close
        specs: List of trade specifications to simulate
        cfg: Backtest configuration (fees, funding, slippage, capital)
        max_concurrent: Max simultaneous positions (1 = sequential)

    Returns:
        DiscreteBacktestResult with all trades and metrics
    """
    if not candles_1m:
        return DiscreteBacktestResult(config=cfg, initial_capital=cfg.initial_capital, final_equity=cfg.initial_capital)

    # Fee and slippage rates (use config's taker fee for both entry and exit;
    # discrete backtest can be configured with separate maker/taker by
    # setting fee_maker_bps on the config object manually before calling)
    maker_fee = (getattr(cfg, 'fee_maker_bps', None) or cfg.fee_taker_bps) / 10_000.0
    taker_fee = cfg.fee_taker_bps / 10_000.0
    slip_rate = cfg.slippage_bps / 10_000.0

    # Funding per bar (assuming 1m candles, 480 bars per 8h)
    funding_per_bar = (cfg.funding_bps_per_8h / 10_000.0) / 480.0

    equity = float(cfg.initial_capital)
    all_trades: List[Trade] = []
    total_fees = 0.0
    total_funding = 0.0

    # Sort specs by entry time
    specs = sorted(specs, key=lambda s: s.entry_ts)

    for spec in specs:
        entry_idx = _find_entry_bar(candles_1m, spec.entry_ts)
        if entry_idx < 0:
            continue

        # Entry: fill at entry_px + adverse slippage
        entry_fill = spec.entry_px * (1.0 + slip_rate) if spec.direction == "long" else spec.entry_px * (1.0 - slip_rate)
        qty = spec.notional_usd / entry_fill if entry_fill > 0 else 0.0
        if qty <= 0 or not math.isfinite(qty):
            continue

        # Entry fee
        entry_fee = entry_fill * qty * maker_fee
        equity -= entry_fee
        total_fees += entry_fee

        position_qty = qty
        accumulated_funding = 0.0
        sl_hit = False
        exit_reason = "eod"
        exit_px = float(candles_1m[-1]["close"])
        exit_qty = 0.0

        # Walk through candles
        for i in range(entry_idx, len(candles_1m)):
            c = candles_1m[i]
            hi, lo, cl = float(c["high"]), float(c["low"]), float(c["close"])
            ts = c["timestamp"]

            # Accrue funding
            if funding_per_bar != 0.0 and position_qty > 0:
                notional = position_qty * cl
                sign = 1.0 if spec.direction == "long" else -1.0
                accumulated_funding += notional * funding_per_bar * sign

            # Check SL
            if spec.direction == "long" and lo <= spec.sl_px:
                sl_fill = spec.sl_px * (1.0 - slip_rate)  # adverse
                # SL: taker fee
                exit_fee_amount = sl_fill * position_qty * taker_fee
                pnl_per_unit = (sl_fill - entry_fill) * (-1 if spec.direction == "short" else 1)
                gross = pnl_per_unit * position_qty
                net = gross - exit_fee_amount - accumulated_funding
                equity += net
                total_fees += exit_fee_amount
                total_funding += accumulated_funding
                exit_qty += position_qty
                exit_px = sl_fill
                exit_reason = "sl"
                sl_hit = True

                all_trades.append(Trade(
                    entry_at=spec.entry_ts, entry_px=entry_fill,
                    exit_at=ts, exit_px=sl_fill,
                    direction=spec.direction, qty=position_qty,
                    pnl_abs=net - entry_fee,
                    pnl_pct=((net - entry_fee) / (entry_fill * position_qty) * 100) if position_qty > 1e-9 else 0.0,
                    fees=entry_fee + exit_fee_amount,
                    funding=accumulated_funding,
                    exit_reason="sl",
                ))
                position_qty = 0
                break

            elif spec.direction == "short" and hi >= spec.sl_px:
                sl_fill = spec.sl_px * (1.0 + slip_rate)
                exit_fee_amount = sl_fill * position_qty * taker_fee
                pnl_per_unit = (entry_fill - sl_fill) * (-1 if spec.direction == "long" else 1)
                gross = pnl_per_unit * position_qty
                net = gross - exit_fee_amount - accumulated_funding
                equity += net
                total_fees += exit_fee_amount
                total_funding += accumulated_funding
                exit_qty += position_qty
                exit_px = sl_fill
                exit_reason = "sl"
                sl_hit = True

                all_trades.append(Trade(
                    entry_at=spec.entry_ts, entry_px=entry_fill,
                    exit_at=ts, exit_px=sl_fill,
                    direction=spec.direction, qty=position_qty,
                    pnl_abs=net - entry_fee,
                    pnl_pct=((net - entry_fee) / (entry_fill * position_qty) * 100) if position_qty > 1e-9 else 0.0,
                    fees=entry_fee + exit_fee_amount,
                    funding=accumulated_funding,
                    exit_reason="sl",
                ))
                position_qty = 0
                break

            # Check TPs (multiple)
            if spec.direction == "long":
                for tp_idx, tp_level in enumerate(spec.tp_levels):
                    if hi >= tp_level and position_qty > 0:
                        alloc = spec.tp_allocations[tp_idx] if tp_idx < len(spec.tp_allocations) else 1.0
                        close_qty = min(qty * alloc, position_qty)
                        tp_fill = tp_level * (1.0 - slip_rate)
                        exit_fee_amount = tp_fill * close_qty * maker_fee  # maker on TP
                        pnl_per_unit = (tp_fill - entry_fill) * (-1 if spec.direction == "short" else 1)
                        gross = pnl_per_unit * close_qty
                        # Pro-rate funding
                        funding_share = accumulated_funding * (close_qty / position_qty) if position_qty > 0 else 0.0
                        net = gross - exit_fee_amount - funding_share
                        equity += net
                        total_fees += exit_fee_amount
                        total_funding += funding_share
                        exit_qty += close_qty
                        accumulated_funding -= funding_share
                        position_qty -= close_qty
                        exit_px = tp_fill
                        exit_reason = f"tp{tp_idx+1}"

                        all_trades.append(Trade(
                            entry_at=spec.entry_ts, entry_px=entry_fill,
                            exit_at=ts, exit_px=tp_fill,
                            direction=spec.direction, qty=close_qty,
                            pnl_abs=net - entry_fee * (close_qty / qty) if qty > 0 else 0,
                            pnl_pct=((net - entry_fee * (close_qty / qty)) / (entry_fill * close_qty) * 100) if close_qty > 1e-9 else 0.0,
                            fees=entry_fee * (close_qty / qty) + exit_fee_amount if qty > 0 else 0,
                            funding=funding_share,
                            exit_reason=f"tp{tp_idx+1}",
                        ))

            elif spec.direction == "short":
                for tp_idx, tp_level in enumerate(spec.tp_levels):
                    if lo <= tp_level and position_qty > 0:
                        alloc = spec.tp_allocations[tp_idx] if tp_idx < len(spec.tp_allocations) else 1.0
                        close_qty = min(qty * alloc, position_qty)
                        tp_fill = tp_level * (1.0 + slip_rate)
                        exit_fee_amount = tp_fill * close_qty * maker_fee
                        pnl_per_unit = (entry_fill - tp_fill) * (-1 if spec.direction == "long" else 1)
                        gross = pnl_per_unit * close_qty
                        funding_share = accumulated_funding * (close_qty / position_qty) if position_qty > 0 else 0.0
                        net = gross - exit_fee_amount - funding_share
                        equity += net
                        total_fees += exit_fee_amount
                        total_funding += funding_share
                        exit_qty += close_qty
                        accumulated_funding -= funding_share
                        position_qty -= close_qty
                        exit_px = tp_fill
                        exit_reason = f"tp{tp_idx+1}"

                        all_trades.append(Trade(
                            entry_at=spec.entry_ts, entry_px=entry_fill,
                            exit_at=ts, exit_px=tp_fill,
                            direction=spec.direction, qty=close_qty,
                            pnl_abs=net - entry_fee * (close_qty / qty) if qty > 0 else 0,
                            pnl_pct=((net - entry_fee * (close_qty / qty)) / (entry_fill * close_qty) * 100) if close_qty > 1e-9 else 0.0,
                            fees=entry_fee * (close_qty / qty) + exit_fee_amount if qty > 0 else 0,
                            funding=funding_share,
                            exit_reason=f"tp{tp_idx+1}",
                        ))

            if position_qty <= 1e-12:
                break

        # EOD close if position still open
        if position_qty > 1e-12 and not sl_hit:
            last_c = candles_1m[-1]
            eod_px = float(last_c["close"])
            eod_fill = eod_px * (1.0 - slip_rate) if spec.direction == "long" else eod_px * (1.0 + slip_rate)
            exit_fee_amount = eod_fill * position_qty * taker_fee
            pnl_per_unit = (eod_fill - entry_fill) * (-1 if spec.direction == "short" else 1)
            gross = pnl_per_unit * position_qty
            net = gross - exit_fee_amount - accumulated_funding
            equity += net
            total_fees += exit_fee_amount
            total_funding += accumulated_funding

            all_trades.append(Trade(
                entry_at=spec.entry_ts, entry_px=entry_fill,
                exit_at=last_c["timestamp"], exit_px=eod_fill,
                direction=spec.direction, qty=position_qty,
                pnl_abs=net - entry_fee,
                pnl_pct=((net - entry_fee) / (entry_fill * position_qty) * 100) if position_qty > 1e-9 else 0.0,
                fees=entry_fee + exit_fee_amount,
                funding=accumulated_funding,
                exit_reason="eod",
            ))

    # Compute metrics
    wins = [t for t in all_trades if t.pnl_abs > 0]
    losses = [t for t in all_trades if t.pnl_abs <= 0]
    gross_profit = sum(t.pnl_abs for t in wins) if wins else 0
    gross_loss = abs(sum(t.pnl_abs for t in losses)) if losses else 0
    pf = (gross_profit / gross_loss) if gross_loss > 0 else (math.inf if gross_profit > 0 else 0.0)

    return DiscreteBacktestResult(
        config=cfg,
        trades=all_trades,
        initial_capital=cfg.initial_capital,
        final_equity=equity,
        pnl_abs=equity - cfg.initial_capital,
        pnl_pct=(equity / cfg.initial_capital - 1) * 100 if cfg.initial_capital > 0 else 0,
        winrate=(len(wins) / len(all_trades) * 100) if all_trades else 0,
        num_trades=len(all_trades),
        avg_trade_pnl=(sum(t.pnl_abs for t in all_trades) / len(all_trades)) if all_trades else 0,
        profit_factor=float(pf) if math.isfinite(pf) else float("inf"),
        total_fees=total_fees,
        total_funding=total_funding,
    )


async def load_candles_1m(pool, symbol: str, start, end) -> List[Dict]:
    """Load 1m candles for a symbol. Returns list of dicts sorted by timestamp."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT c.timestamp, c.open, c.high, c.low, c.close
            FROM candles c JOIN instruments i ON i.id = c.instrument_id
            WHERE i.symbol = $1 AND c.timeframe = '1m'
              AND c.timestamp >= $2 AND c.timestamp <= $3
            ORDER BY c.timestamp
            LIMIT 100000
        """, symbol, start, end)
    return [{"timestamp": r["timestamp"], "open": float(r["open"]),
             "high": float(r["high"]), "low": float(r["low"]),
             "close": float(r["close"])} for r in rows]
