"""
Module: run_baseline_backtest
Purpose: Run a baseline backtest (BTC/USDT, rsi_reversal) and store results in Postgres.
Location: /opt/tickles/shared/scripts/run_baseline_backtest.py
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

import pandas as pd

sys.path.insert(0, "/opt/tickles")

from shared.backtest.candle_loader import load_candles_sync
from shared.backtest.engine import BacktestConfig, BacktestResult, run_backtest
from shared.backtest.strategies.single_indicator import get as get_strategy
from shared.utils.db import DatabasePool, get_shared_pool

logger = logging.getLogger(__name__)

DEFAULT_SYMBOL = "BTC/USDT"
DEFAULT_EXCHANGE = "bybit"
DEFAULT_TIMEFRAME = "1h"
DEFAULT_START = "2026-02-20"
DEFAULT_END = "2026-05-01"
DEFAULT_STRATEGY = "rsi_reversal"
DEFAULT_PERIOD = 14
DEFAULT_OVERBOUGHT = 70.0
DEFAULT_OVERSOLD = 30.0


def store_result_sync(
    result: BacktestResult,
    config: BacktestConfig,
    instrument_id: int = 3,
) -> int:
    """Store backtest result in public.backtest_results using psycopg2.

    Args:
        result: The BacktestResult returned by the engine.
        config: The BacktestConfig used to generate the result.
        instrument_id: The instrument ID (default 3 = BTC/USDT on bybit).

    Returns:
        The id of the inserted row.
    """
    import json as _json
    import psycopg2
    from shared.utils import config as cfg

    param_hash = config.param_hash()
    indicator_params_json = _json.dumps(config.indicator_params, sort_keys=True)

    conn = psycopg2.connect(
        host=cfg.DB_HOST, port=cfg.DB_PORT,
        user=cfg.DB_USER, password=cfg.DB_PASSWORD,
        dbname=cfg.DB_NAME_SHARED,
        connect_timeout=10,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.backtest_results (
                    instrument_id, indicator_name, param_hash, params, timeframe,
                    date_from, date_to,
                    initial_balance, final_balance, total_return_pct,
                    total_trades, win_rate_pct, sharpe_ratio, max_drawdown_pct,
                    profit_factor, total_fees,
                    engine_version, run_duration_ms, deflated_sharpe
                ) VALUES (
                    %s, %s, %s, %s::jsonb, %s,
                    %s, %s,
                    %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s,
                    %s, %s, %s
                )
                RETURNING id
                """,
                (
                    instrument_id,
                    f"{config.indicator_name}_{config.strategy_name}",
                    param_hash,
                    indicator_params_json,
                    config.timeframe,
                    date.fromisoformat(config.start_date),
                    date.fromisoformat(config.end_date),
                    Decimal(str(config.initial_capital)),
                    Decimal(str(result.final_equity)),
                    Decimal(str(result.pnl_pct / 100.0)),
                    result.num_trades,
                    Decimal(str(result.winrate)),
                    Decimal(str(result.sharpe)),
                    Decimal(str(result.max_drawdown)),
                    Decimal(str(result.profit_factor)),
                    Decimal(str(result.total_fees)),
                    result.engine_version,
                    int(result.runtime_ms),
                    Decimal(str(result.deflated_sharpe)),
                ),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("INSERT into backtest_results returned no row")
            conn.commit()
            return int(row[0])
    finally:
        conn.close()


def build_config(
    symbol: str,
    exchange: str,
    timeframe: str,
    start_date: str,
    end_date: str,
    strategy_name: str,
    indicator_name: str,
    indicator_params: Dict[str, Any],
    direction: str = "both",
    initial_capital: float = 10_000.0,
    stop_loss_pct: float = 0.0,
    take_profit_pct: float = 0.0,
) -> BacktestConfig:
    """Build a BacktestConfig for the specified parameters.

    Args:
        symbol: Trading symbol, e.g. BTC/USDT.
        exchange: Exchange name, e.g. bybit.
        timeframe: Candle timeframe, e.g. 1h.
        start_date: ISO date range start.
        end_date: ISO date range end.
        strategy_name: Strategy label.
        indicator_name: Indicator name key.
        indicator_params: Dict of indicator parameters.
        direction: One of 'long', 'short', 'both'.
        initial_capital: Starting equity in USD.
        stop_loss_pct: Stop loss percentage (0 = disabled).
        take_profit_pct: Take profit percentage (0 = disabled).

    Returns:
        A fully-populated BacktestConfig.
    """
    return BacktestConfig(
        symbol=symbol,
        source=exchange,
        timeframe=timeframe,
        start_date=start_date,
        end_date=end_date,
        direction=direction,
        initial_capital=initial_capital,
        position_pct=100.0,
        leverage=1.0,
        fee_taker_bps=5.0,
        slippage_bps=2.0,
        funding_bps_per_8h=0.0,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        crash_protection=False,
        strategy_name=strategy_name,
        indicator_name=indicator_name,
        indicator_params=indicator_params,
        n_trials=1,
    )


async def run_baseline(
    symbol: str = DEFAULT_SYMBOL,
    exchange: str = DEFAULT_EXCHANGE,
    timeframe: str = DEFAULT_TIMEFRAME,
    start_date: str = DEFAULT_START,
    end_date: str = DEFAULT_END,
    strategy_name: str = DEFAULT_STRATEGY,
    period: int = DEFAULT_PERIOD,
    overbought: float = DEFAULT_OVERBOUGHT,
    oversold: float = DEFAULT_OVERSOLD,
    direction: str = "both",
    stop_loss_pct: float = 0.0,
    take_profit_pct: float = 0.0,
    dry_run: bool = False,
) -> Optional[BacktestResult]:
    """Load candles, run the backtest engine, and store the result.

    Args:
        symbol: Trading symbol.
        exchange: Exchange name.
        timeframe: Candle timeframe.
        start_date: ISO date range start.
        end_date: ISO date range end.
        strategy_name: Strategy label.
        period: RSI period.
        overbought: RSI overbought threshold.
        oversold: RSI oversold threshold.
        direction: 'long', 'short', or 'both'.
        stop_loss_pct: Stop loss percentage.
        take_profit_pct: Take profit percentage.
        dry_run: When True, skip DB write.

    Returns:
        The BacktestResult on success, or None on failure.
    """
    indicator_params = {
        "period": period,
        "overbought": overbought,
        "oversold": oversold,
    }
    config = build_config(
        symbol=symbol,
        exchange=exchange,
        timeframe=timeframe,
        start_date=start_date,
        end_date=end_date,
        strategy_name=strategy_name,
        indicator_name="rsi",
        indicator_params=indicator_params,
        direction=direction,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
    )

    logger.info(
        "Loading candles: %s/%s %s [%s..%s]",
        exchange, symbol, timeframe, start_date, end_date,
    )
    try:
        df = load_candles_sync(symbol, exchange, timeframe, start_date, end_date)
    except Exception as exc:
        logger.error("Failed to load candles: %s", exc)
        return None

    if df is None or df.empty:
        logger.error("No candle data found for %s/%s %s [%s..%s]",
                     exchange, symbol, timeframe, start_date, end_date)
        return None

    logger.info("Loaded %d candle rows", len(df))

    strategy_fn = get_strategy(strategy_name)
    logger.info("Running strategy: %s with params %s", strategy_name, indicator_params)

    try:
        result = run_backtest(df, strategy_fn, config)
    except Exception as exc:
        logger.exception("Backtest engine failed: %s", exc)
        return None

    logger.info(
        "Backtest result: pnl=%.2f%% sharpe=%.2f winrate=%.1f%% trades=%d mdd=%.2f%%",
        result.pnl_pct, result.sharpe, result.winrate, len(result.trades), result.max_drawdown,
    )

    if dry_run:
        logger.info("Dry run — skipping DB write")
        return result

    row_id = store_result_sync(result, config)
    logger.info("Stored backtest result with id=%s", row_id)
    return result


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run a baseline backtest")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument("--exchange", default=DEFAULT_EXCHANGE)
    parser.add_argument("--timeframe", default=DEFAULT_TIMEFRAME)
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--strategy", default=DEFAULT_STRATEGY)
    parser.add_argument("--period", type=int, default=DEFAULT_PERIOD)
    parser.add_argument("--overbought", type=float, default=DEFAULT_OVERBOUGHT)
    parser.add_argument("--oversold", type=float, default=DEFAULT_OVERSOLD)
    parser.add_argument("--direction", default="both", choices=["long", "short", "both"])
    parser.add_argument("--stop-loss", type=float, default=0.0)
    parser.add_argument("--take-profit", type=float, default=0.0)
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Skip DB write (default). Use --apply to write.")
    parser.add_argument("--apply", dest="dry_run", action="store_false",
                        help="Actually write result to DB.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    result = await run_baseline(
        symbol=args.symbol,
        exchange=args.exchange,
        timeframe=args.timeframe,
        start_date=args.start,
        end_date=args.end,
        strategy_name=args.strategy,
        period=args.period,
        overbought=args.overbought,
        oversold=args.oversold,
        direction=args.direction,
        stop_loss_pct=args.stop_loss,
        take_profit_pct=args.take_profit,
        dry_run=args.dry_run,
    )

    if result is None:
        logger.error("Backtest failed")
        sys.exit(1)

    print(f"\n=== Backtest Summary ===")
    print(f"Symbol:       {args.exchange}:{args.symbol} {args.timeframe}")
    print(f"Strategy:     {args.strategy}(period={args.period})")
    print(f"Date Range:   {args.start} .. {args.end}")
    print(f"PnL:          {result.pnl_abs:+.2f} USD ({result.pnl_pct:+.2f}%)")
    print(f"Sharpe:       {result.sharpe:.3f}")
    print(f"Sortino:      {result.sortino:.3f}")
    print(f"Defl Sharpe:  {result.deflated_sharpe:.3f}")
    print(f"Win Rate:     {result.winrate:.1f}%")
    print(f"Max Drawdown: {result.max_drawdown:.2f}%")
    print(f"Total Trades: {result.num_trades}")
    print(f"Avg Trade PnL:{result.avg_trade_pnl:+.2f}")
    print(f"Profit Factor:{result.profit_factor:.2f}")
    print(f"Final Equity: {result.final_equity:.2f}")
    print(f"Runtime:      {result.runtime_ms:.0f}ms")


if __name__ == "__main__":
    asyncio.run(main())
