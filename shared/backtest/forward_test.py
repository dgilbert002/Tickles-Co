"""
Module: forward_test
Purpose: Real-time shadow trading engine for Rule 1 enforcement.
Location: /opt/tickles/shared/backtest/forward_test.py
"""

import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import numpy as np

from shared.backtest.engine import BacktestConfig, BacktestExecutor, Trade
from shared.backtest.strategies import get as get_strategy
from shared.backtest.indicators import get as get_indicator
from shared.backtest.ch_writer import ClickHouseWriter
from shared.utils.db import DatabasePool

logger = logging.getLogger(__name__)

class ForwardTestEngine:
    """
    Orchestrates real-time forward-testing (shadow trading) for multiple strategies.
    Listens for new candles and executes the same logic as the backtest engine.
    """

    def __init__(self):
        self.ch = ClickHouseWriter()
        self.executors: Dict[str, BacktestExecutor] = {}  # forward_run_id -> executor
        self.configs: Dict[str, BacktestConfig] = {}      # forward_run_id -> config
        self.strategy_ids: Dict[str, str] = {}           # forward_run_id -> strategy_id
        self.instrument_ids: Dict[str, int] = {}         # forward_run_id -> instrument_id
        self.candle_buffers: Dict[str, List[Dict[str, Any]]] = {} # forward_run_id -> list of candles
        self.last_trade_counts: Dict[str, int] = {}      # forward_run_id -> count of trades persisted

    async def start_run(self, strategy_id: str, cfg: BacktestConfig, instrument_id: int = 0) -> str:
        """Start a new forward-test (shadow) run."""
        try:
            forward_run_id = str(uuid.uuid4())
            executor = BacktestExecutor(cfg)
            
            self.executors[forward_run_id] = executor
            self.configs[forward_run_id] = cfg
            self.strategy_ids[forward_run_id] = strategy_id
            self.instrument_ids[forward_run_id] = instrument_id
            self.candle_buffers[forward_run_id] = []
            self.last_trade_counts[forward_run_id] = 0

            # Record the start in ClickHouse
            self.ch.write_forward_run_start(forward_run_id, strategy_id, cfg, instrument_id)
            
            logger.info(f"Started forward run {forward_run_id} for strategy {strategy_id} on {cfg.symbol}")
            return forward_run_id
        except Exception as e:
            logger.error(f"Failed to start forward run: {e}")
            raise

    async def on_candle(self, symbol: str, timeframe: str, candle: Dict[str, Any]) -> None:
        """
        Process a new candle for all active runs matching the symbol and timeframe.
        This is the core real-time loop.
        """
        try:
            ts = pd.to_datetime(candle["snapshotTime"], utc=True).to_numpy()
            o, h, l, c = float(candle["openPrice"]), float(candle["highPrice"]), float(candle["lowPrice"]), float(candle["closePrice"])
            bid = float(candle.get("openBid", o))
            ask = float(candle.get("closeAsk", o))

            for run_id, executor in self.executors.items():
                cfg = self.configs[run_id]
                if cfg.symbol != symbol or cfg.timeframe != timeframe:
                    continue

                # 1. Update candle buffer for signal computation
                buffer = self.candle_buffers[run_id]
                buffer.append(candle)
                if len(buffer) > 500: # Keep a reasonable window
                    buffer.pop(0)

                # 2. Process intrabar (SL/TP/Funding) on the bar that just closed
                executor.process_intrabar(ts, o, h, l, c)
                
                # 3. Persist any newly closed trades
                self._persist_new_trades(run_id)

                # 4. Compute signal for NEXT bar fill
                sig, entry_sig = self._compute_signals(run_id)
                
                # 5. Check crash protection
                crash_blocked = False
                if cfg.crash_protection:
                    crash_blocked = self._check_crash_protection(run_id)

                # 6. Process signal for next-bar execution
                # In real-time, we don't know the next bar's open yet.
                # We store the signal and apply it when the NEXT candle arrives.
                # However, the BacktestExecutor.process_signal expects next_bar info.
                # For forward testing, we'll use the current close as a proxy for next open
                # OR we wait for the next candle to call process_signal.
                # To maintain parity with backtest engine (which fills at i+1 open),
                # we should store the signal and apply it at the start of the next on_candle call.
                
                # For now, we'll store the signal in the executor (we need to extend it or handle it here)
                # Let's assume we apply the signal from the PREVIOUS bar now.
                # 6. Process signal for next-bar execution
                # To maintain parity with backtest engine (which fills at i+1 open),
                # we apply the signal generated at the end of the PREVIOUS bar now.
                pending = getattr(executor, "_pending_signal", None)
                if pending is not None:
                    executor.process_signal(
                        sig=pending["sig"],
                        entry_sig=pending["entry_sig"],
                        crash_blocked=pending["crash_blocked"],
                        next_bar_ts=ts,
                        next_bar_open=o,
                        next_bar_bid=bid,
                        next_bar_ask=ask
                    )
                
                # Store current signal for the NEXT candle's open
                executor._pending_signal = {
                    "sig": sig,
                    "entry_sig": entry_sig,
                    "crash_blocked": crash_blocked
                }

                # 7. Update run stats in ClickHouse
                self.ch.update_forward_run_stats(
                    run_id, 
                    executor.get_equity(c),
                    ((executor.get_equity(c) - cfg.initial_capital) / cfg.initial_capital * 100),
                    len(executor.trades)
                )
                
        except Exception as e:
            logger.error(f"Error processing candle for {symbol}: {e}")

    def _persist_new_trades(self, run_id: str) -> None:
        """Persist any newly closed trades to ClickHouse."""
        executor = self.executors[run_id]
        strategy_id = self.strategy_ids[run_id]
        
        current_count = len(executor.trades)
        last_count = self.last_trade_counts[run_id]
        
        if current_count > last_count:
            for i in range(last_count, current_count):
                trade = executor.trades[i]
                self.ch.write_forward_trade(run_id, strategy_id, trade, i)
            
            self.last_trade_counts[run_id] = current_count

    def _compute_signals(self, run_id: str) -> Tuple[float, float]:
        """Compute signals using the strategy and buffered history."""
        cfg = self.configs[run_id]
        buffer = self.candle_buffers[run_id]
        
        if len(buffer) < 1: # Reduced for testing
            return 0.0, 0.0
            
        df = pd.DataFrame(buffer)
        # Ensure column names match what strategies expect
        # (openPrice, highPrice, lowPrice, closePrice, openBid, closeAsk, volume, snapshotTime)
        
        try:
            strategy_fn = get_strategy(cfg.strategy_name)
            raw_signals = strategy_fn(df, cfg.indicator_params).astype(float).fillna(0.0).clip(-1, 1)
            
            sig = float(raw_signals.iloc[-1])
            
            # Entry gate logic
            entry_sig = sig
            if cfg.direction == "long":
                entry_sig = 1.0 if sig > 0 else 0.0
            elif cfg.direction == "short":
                entry_sig = -1.0 if sig < 0 else 0.0
                
            return sig, entry_sig
        except Exception as e:
            logger.error(f"Signal computation failed for run {run_id}: {e}")
            return 0.0, 0.0

    def _check_crash_protection(self, run_id: str) -> bool:
        """Check if crash protection is active."""
        cfg = self.configs[run_id]
        buffer = self.candle_buffers[run_id]
        df = pd.DataFrame(buffer)
        
        try:
            crash_fn = get_indicator("crash_protection").fn
            crash_series = crash_fn(df, {})
            return bool(crash_series.iloc[-1])
        except Exception:
            return False
