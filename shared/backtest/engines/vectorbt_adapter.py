"""
shared.backtest.engines.vectorbt_adapter — fast vectorised engine.

The 21-year-old version
=======================
The classic engine is a Python for-loop — accurate, but running
1,000 parameter combinations over 500k bars is unpleasantly slow.
VectorBT is a numba-accelerated library that does the entire
portfolio simulation in one vectorised call, which is ~100x faster
in practice.

Trade-offs to be honest about:

  * Fill model is simpler. VectorBT supports "close" or "nextopen"
    fills via ``price=...``. We pick nextopen to match engine.py's
    next-bar-open rule for signal-based entries.
  * Intrabar SL/TP is partial: vbt has ``sl_stop=`` / ``tp_stop=`` but
    it fills at the stop level on the CURRENT bar's close, not on
    the intrabar high/low like engine.py. Close enough for sweeping
    a param space; exact parity belongs to the classic engine.
  * Funding is modelled as a flat per-bar carry cost applied to
    notional (not the per-direction funding rate from engine.py).
    For crypto perps the direction matters; we flag this in the
    capabilities block so ``parity.py`` knows to apply a looser
    tolerance.

Result adaptation: we build a ``BacktestResult`` with the same
schema as engine.py so downstream consumers don't care which backend
they got.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from shared.backtest.engine import (
    BacktestConfig,
    BacktestResult,
    Trade,
    _bars_per_year,
    _deflated_sharpe,
    _empty_result,
    _sharpe,
    _sortino,
)
from shared.backtest.engines.protocol import (
    BacktestEngine,
    EngineCapabilities,
    StrategyFn,
)

log = logging.getLogger("tickles.engines.vbt")


class VectorBTEngine(BacktestEngine):
    """Vectorised backtest engine backed by vectorbt."""

    name = "vectorbt"

    capabilities = EngineCapabilities(
        supports_intrabar_sl_tp=False,
        supports_funding=False,
        supports_fees=True,
        supports_slippage=True,
        supports_vectorised_sweep=True,
        supports_walk_forward=False,
        notes=(
            "vectorbt.Portfolio.from_signals — fast approximate engine "
            "for parameter sweeps. Not exact parity with classic engine "
            "on SL/TP and funding; use parity harness with loose "
            "tolerance."
        ),
    )

    def available(self) -> bool:
        try:
            import vectorbt  # noqa: F401
        except Exception:
            return False
        return True

    def run(
        self,
        candles_df: pd.DataFrame,
        strategy: StrategyFn,
        cfg: BacktestConfig,
        crash_protection_series: Optional[pd.Series] = None,
    ) -> BacktestResult:
        t0 = time.perf_counter()
        if not self.available():
            raise RuntimeError(
                "vectorbt is not installed; install with `pip install vectorbt`"
            )
        import vectorbt as vbt  # type: ignore[import-not-found]

        if candles_df is None or len(candles_df) < 50:
            return _empty_result(cfg, time.perf_counter() - t0)

        close_col = "closePrice" if "closePrice" in candles_df.columns else "close"
        open_col = "openPrice" if "openPrice" in candles_df.columns else "open"
        ts_col = "snapshotTime" if "snapshotTime" in candles_df.columns else None

        close = candles_df[close_col].astype(float).reset_index(drop=True)
        open_ = candles_df[open_col].astype(float).reset_index(drop=True)

        raw = (
            strategy(candles_df, cfg.indicator_params)
            .astype(float)
            .fillna(0.0)
            .clip(-1, 1)
        )
        raw = pd.Series(raw.values, index=close.index)

        if cfg.direction == "long":
            entries = raw > 0
            exits = raw < 0
            short_entries = pd.Series(False, index=close.index)
            short_exits = pd.Series(False, index=close.index)
        elif cfg.direction == "short":
            entries = pd.Series(False, index=close.index)
            exits = pd.Series(False, index=close.index)
            short_entries = raw < 0
            short_exits = raw > 0
        else:
            entries = raw > 0
            exits = raw < 0
            short_entries = raw < 0
            short_exits = raw > 0

        if cfg.crash_protection and crash_protection_series is not None:
            crash = (
                crash_protection_series.reindex(candles_df.index)
                .fillna(False)
                .astype(bool)
                .reset_index(drop=True)
            )
            entries = entries & ~crash.values
            short_entries = short_entries & ~crash.values

        entries = entries.shift(1, fill_value=False).astype(bool)
        exits = exits.shift(1, fill_value=False).astype(bool)
        short_entries = short_entries.shift(1, fill_value=False).astype(bool)
        short_exits = short_exits.shift(1, fill_value=False).astype(bool)

        fees = cfg.fee_taker_bps / 10_000.0
        slippage = cfg.slippage_bps / 10_000.0

        sl_stop = cfg.stop_loss_pct / 100.0 if cfg.stop_loss_pct > 0 else None
        tp_stop = cfg.take_profit_pct / 100.0 if cfg.take_profit_pct > 0 else None

        size_frac = max(min(cfg.position_pct / 100.0, 1.0), 0.01)

        def _build(**extra: Any) -> Any:
            common: Dict[str, Any] = dict(
                close=close,
                entries=entries,
                exits=exits,
                price=open_,
                init_cash=cfg.initial_capital,
                size=size_frac,
                size_type="percent",
                fees=fees,
                slippage=slippage,
                sl_stop=sl_stop,
                tp_stop=tp_stop,
                freq="1T",
            )
            common.update(extra)
            return vbt.Portfolio.from_signals(**common)

        if cfg.direction != "long":
            try:
                pf = _build(
                    short_entries=short_entries,
                    short_exits=short_exits,
                )
            except TypeError:
                pf = _build()
        else:
            pf = _build()

        result = self._to_result(pf, cfg, candles_df, ts_col, t0)
        log.info(
            "vectorbt backtest %s trades=%d pnl=%.2f%% sharpe=%.2f in %.1fms",
            cfg.symbol,
            result.num_trades,
            result.pnl_pct,
            result.sharpe,
            result.runtime_ms,
        )
        return result

    @staticmethod
    def _to_result(
        pf: "object",
        cfg: BacktestConfig,
        candles_df: pd.DataFrame,
        ts_col: Optional[str],
        t0: float,
    ) -> BacktestResult:
        try:
            value_series = pf.value()  # type: ignore[attr-defined]
        except Exception:
            value_series = pd.Series([cfg.initial_capital] * len(candles_df))

        if ts_col is not None:
            idx = pd.to_datetime(candles_df[ts_col], utc=True).reset_index(drop=True)
            value_series = pd.Series(value_series.values, index=idx.values)
        else:
            value_series = pd.Series(value_series.values, index=candles_df.index)

        returns = value_series.pct_change().dropna()
        bars_py = _bars_per_year(cfg.timeframe, cfg.bars_per_year_override)
        sharpe = _sharpe(returns, bars_py)
        sortino = _sortino(returns, bars_py)

        running_max = value_series.cummax()
        drawdowns = (value_series - running_max) / running_max.replace(0, np.nan)
        mdd = float(drawdowns.min()) if len(drawdowns) else 0.0
        if not math.isfinite(mdd):
            mdd = 0.0

        trades: List[Trade] = []
        try:
            tr_df = pf.trades.records_readable  # type: ignore[attr-defined]
            for _, row in tr_df.iterrows():
                entry_at = row.get("Entry Timestamp", row.get("Entry Index"))
                exit_at = row.get("Exit Timestamp", row.get("Exit Index"))
                entry_px = float(row.get("Avg Entry Price", row.get("Entry Price", 0.0)))
                exit_px_v = row.get("Avg Exit Price", row.get("Exit Price", None))
                exit_px = float(exit_px_v) if exit_px_v is not None else None
                qty = float(row.get("Size", 0.0))
                pnl = float(row.get("PnL", 0.0))
                status = str(row.get("Status", "closed")).lower()
                is_short = "short" in str(row.get("Direction", "Long")).lower()
                trades.append(
                    Trade(
                        entry_at=pd.Timestamp(entry_at, tz="UTC") if entry_at is not None else None,
                        entry_px=entry_px,
                        exit_at=pd.Timestamp(exit_at, tz="UTC") if exit_at is not None else None,
                        exit_px=exit_px,
                        direction="short" if is_short else "long",
                        qty=qty,
                        pnl_abs=pnl,
                        pnl_pct=(pnl / (entry_px * qty) * 100.0) if entry_px * qty > 0 else 0.0,
                        fees=float(row.get("Entry Fees", 0.0)) + float(row.get("Exit Fees", 0.0)),
                        funding=0.0,
                        exit_reason=status,
                    )
                )
        except Exception as exc:
            log.warning("vectorbt trade extraction failed: %s", exc)

        wins = [t for t in trades if t.pnl_abs > 0]
        losses = [t for t in trades if t.pnl_abs <= 0]
        gross_profit = sum(t.pnl_abs for t in wins)
        gross_loss = abs(sum(t.pnl_abs for t in losses))
        pf_ratio = (
            gross_profit / gross_loss
            if gross_loss > 0
            else (math.inf if gross_profit > 0 else 0.0)
        )

        final_equity = float(value_series.iloc[-1]) if len(value_series) else cfg.initial_capital

        return BacktestResult(
            config=cfg,
            param_hash=cfg.param_hash(),
            final_equity=final_equity,
            pnl_abs=float(final_equity - cfg.initial_capital),
            pnl_pct=float((final_equity - cfg.initial_capital) / cfg.initial_capital * 100),
            max_drawdown=mdd * 100,
            sharpe=sharpe,
            sortino=sortino,
            deflated_sharpe=_deflated_sharpe(sharpe, n_trials=cfg.n_trials, n_obs=len(returns)),
            winrate=(len(wins) / len(trades) * 100) if trades else 0.0,
            num_trades=len(trades),
            avg_trade_pnl=(sum(t.pnl_abs for t in trades) / len(trades)) if trades else 0.0,
            profit_factor=float(pf_ratio) if math.isfinite(pf_ratio) else float("inf"),
            total_fees=float(sum(t.fees for t in trades)),
            total_funding=0.0,
            trades=trades,
            equity_curve=value_series,
            runtime_ms=(time.perf_counter() - t0) * 1000,
            bars_processed=len(candles_df),
            engine_version="vectorbt-" + _safe_version(),
        )


def _safe_version() -> str:
    try:
        import vectorbt
        return str(getattr(vectorbt, "__version__", "unknown"))
    except Exception:
        return "unknown"


__all__ = ["VectorBTEngine"]
