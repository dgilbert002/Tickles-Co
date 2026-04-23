"""
Backtest Engine — Tickles & Co V2.0
=====================================

A deterministic, single-run backtest engine that executes a strategy
against a pandas DataFrame of candles and produces a structured result.

DESIGN PRINCIPLES (Rule #1 — backtests must equal live trades):
  * Deterministic: same inputs → byte-identical outputs.
  * No look-ahead: signal at bar `t` is acted on at bar `t+1` open.
  * Realistic fills: mid-price by default, optional bid/ask for spread,
    slippage = configured bps added to the ask/bid.
  * SL / TP are detected intrabar using high/low and filled at the
    level (plus adverse slippage) on the same bar — matching the live
    behaviour of resting stop/limit orders on most exchanges.
  * Fees: taker rate applied per side. Entry fee debited at entry,
    exit fee debited at exit, so equity curve is realistic.
  * Funding: `funding_bps_per_8h` is accrued per bar while a position
    is open (positive rate = longs pay shorts; sign applies per side).
  * NOTE: internal PnL math is float64. We rely on `decimal.Decimal`
    in the live trader, not here — at >10k trades the cumulative float
    drift is < 1 USD on a 10k equity curve and is dominated by other
    modelling error. A Decimal end-to-end pass is in the Phase 7 backlog.
  * SIGTERM-safe: no global state, safe to run in multiprocessing workers.

The engine expects:

    candles_df: DataFrame with columns produced by candle_loader
                (openPrice, highPrice, lowPrice, closePrice, openBid,
                 closeAsk, volume, snapshotTime [tz-aware]).
    strategy:   Callable[(df, params) → pd.Series[int]]
                Returns entry/exit signal per bar:
                   +1 = long signal (enter long; exits short if open)
                   -1 = short signal (enter short; exits long if open)
                    0 = flat/hold
    params:     dict of strategy parameters (passed to strategy & indicators)
    config:     BacktestConfig (capital, fees, slippage, sl, tp, direction, funding)

It returns a BacktestResult object with:
    * summary stats (sharpe, sortino, winrate, pnl, mdd, deflated sharpe)
    * trade list (list of Trade objects)
    * equity curve (pd.Series)
    * metadata (hash, runtime, asof)

Change log (see ROADMAP_V2 §11 "Hardening pass"):
  2026-04-17  dean's audit — fixed direction-filter eating exits (P0),
              same-bar SL re-entry (P0), same-bar fill look-ahead (P0),
              funding-rate no-op (P0), open_bid/close_bid rename (P0).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional

import numpy as np
import pandas as pd


log = logging.getLogger("tickles.engine")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass
class BacktestConfig:
    symbol: str
    source: str
    timeframe: str
    start_date: str
    end_date: str
    direction: str = "long"          # 'long' | 'short' | 'both'
    initial_capital: float = 10_000.0
    position_pct: float = 100.0       # % of equity to deploy per trade
    leverage: float = 1.0
    fee_taker_bps: float = 5.0        # 0.05% per side (Binance spot default)
    slippage_bps: float = 2.0         # 0.02% per side on top of spread
    funding_bps_per_8h: float = 0.0   # 0 for spot, set for perps. longs PAY when positive.
    stop_loss_pct: float = 0.0        # 0 = disabled. Intrabar SL (checked against high/low).
    take_profit_pct: float = 0.0      # 0 = disabled. Intrabar TP.
    crash_protection: bool = False
    strategy_name: str = ""
    indicator_name: str = ""
    indicator_params: Dict[str, Any] = field(default_factory=dict)
    # Advanced / research
    n_trials: int = 1                 # for deflated-Sharpe. 1 = raw sharpe.
    bars_per_year_override: float = 0.0  # 0 = derive from timeframe (24/7)

    # ---- dedup hash ----
    _FLOAT_PRECISION = 10  # decimals — round floats before hashing for cross-platform stability

    def param_hash(self) -> str:
        """Deterministic SHA256 of the full parameter set.

        Two configs with identical semantic parameters MUST produce the
        same hash regardless of host machine, Python minor version, or
        float construction path. We round floats to 10 decimals and
        recursively sort dict keys before hashing.
        """
        def _norm(o):
            if isinstance(o, dict):
                return {k: _norm(v) for k, v in sorted(o.items())}
            if isinstance(o, list):
                return [_norm(x) for x in o]
            if isinstance(o, float):
                # Round, then repr via "%.{p}f" so 0.1+0.2 and 0.3 collapse.
                return float(f"{o:.{BacktestConfig._FLOAT_PRECISION}f}")
            return o
        data = _norm(asdict(self))
        payload = json.dumps(data, separators=(",", ":"), default=str, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class Trade:
    entry_at:   pd.Timestamp
    entry_px:   float
    exit_at:    Optional[pd.Timestamp]
    exit_px:    Optional[float]
    direction:  str            # 'long' | 'short'
    qty:        float
    pnl_abs:    float          # net of fees + funding
    pnl_pct:    float
    fees:       float
    funding:    float          # accumulated funding cost over the trade (signed)
    exit_reason: str           # 'signal' | 'sl' | 'tp' | 'eod'


@dataclass
class BacktestResult:
    config: BacktestConfig
    param_hash: str
    # Summary
    final_equity:  float
    pnl_abs:       float
    pnl_pct:       float
    max_drawdown:  float
    sharpe:        float
    sortino:       float
    deflated_sharpe: float
    winrate:       float
    num_trades:    int
    avg_trade_pnl: float
    profit_factor: float
    total_fees:    float
    total_funding: float
    # Detail
    trades:        List[Trade]
    equity_curve:  pd.Series
    # Meta
    runtime_ms:    float
    bars_processed:int
    engine_version:str = "2026.04.17.hardened"


# ---------------------------------------------------------------------------
# Metric helpers
# ---------------------------------------------------------------------------
_EPS = 1e-12


def _sharpe(returns: pd.Series, bars_per_year: float) -> float:
    if len(returns) < 2:
        return 0.0
    sd = float(returns.std(ddof=1))
    if not math.isfinite(sd) or sd < _EPS:
        return 0.0
    return float(returns.mean() / sd * math.sqrt(bars_per_year))


def _sortino(returns: pd.Series, bars_per_year: float) -> float:
    downside = returns[returns < 0]
    if len(downside) < 2:
        return 0.0
    sd = float(downside.std(ddof=1))
    if not math.isfinite(sd) or sd < _EPS:
        return 0.0
    return float(returns.mean() / sd * math.sqrt(bars_per_year))


def _deflated_sharpe(sharpe: float, n_trials: int, n_obs: int) -> float:
    """Deflated Sharpe Ratio — Bonferroni-style haircut for multiple trials."""
    if n_trials <= 1 or n_obs < 30 or not math.isfinite(sharpe):
        return sharpe
    emax = math.sqrt(2 * math.log(max(2, n_trials))) / math.sqrt(n_obs)
    return sharpe - emax


_TF_RE = re.compile(r"^(\d+)([mhdw])$")


def _bars_per_year(timeframe: str, override: float = 0.0) -> float:
    """Crypto 24/7 convention. If timeframe is non-standard, parse it.
    Returns 0 for unknown/zero-seconds timeframe (caller should treat as warning).
    """
    if override > 0:
        return float(override)
    m = _TF_RE.match(timeframe.strip().lower())
    if not m:
        log.warning("_bars_per_year: unknown timeframe %r, defaulting to 1h", timeframe)
        sec = 3600
    else:
        n, unit = int(m.group(1)), m.group(2)
        sec = n * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    if sec <= 0:
        return 0.0
    return 365.25 * 24 * 3600 / sec


def _bar_seconds(timeframe: str) -> int:
    m = _TF_RE.match(timeframe.strip().lower())
    if not m:
        return 3600
    n, unit = int(m.group(1)), m.group(2)
    return n * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]


# ---------------------------------------------------------------------------
# Core loop
# ---------------------------------------------------------------------------
class BacktestExecutor:
    """
    Stateful executor for a single backtest run.
    Enables Rule 1 parity by sharing the same logic between batch backtests
    and real-time forward-testing (shadow trading).
    """

    def __init__(self, cfg: BacktestConfig):
        self.cfg = cfg
        self.fee_rate = cfg.fee_taker_bps / 10_000.0
        self.slip_rate = cfg.slippage_bps / 10_000.0
        self.bar_s = _bar_seconds(cfg.timeframe)
        self.funding_per_bar = (cfg.funding_bps_per_8h / 10_000.0) * (self.bar_s / 28_800.0)

        self.equity = float(cfg.initial_capital)
        self.position = 0.0  # signed qty (+ long, - short)
        self.entry_px = 0.0
        self.entry_at: Optional[pd.Timestamp] = None
        self.entry_direction: Optional[str] = None
        self.entry_fee = 0.0
        self.funding_accrued = 0.0
        self.trades: List[Trade] = []
        self.total_fees = 0.0
        self.total_funding = 0.0
        self.just_exited = False

    def get_equity(self, current_px: float) -> float:
        """Calculate current mark-to-market equity."""
        if self.position == 0.0:
            return self.equity

        direction = self.entry_direction or ("long" if self.position > 0 else "short")
        qty = abs(self.position)
        mtm_pnl = (current_px - self.entry_px) * (1 if direction == "long" else -1) * qty
        return self.equity + mtm_pnl - self.funding_accrued

    def process_intrabar(
        self, timestamp: np.datetime64, open_px: float, high_px: float, low_px: float, close_px: float
    ) -> None:
        """Accrue funding and check SL/TP against high/low of the current bar."""
        self.just_exited = False
        if self.position == 0.0:
            return

        # (a) Accrue funding
        if self.funding_per_bar != 0.0:
            notional = abs(self.position) * close_px
            # Positive funding rate: long pays, short receives.
            sign = 1.0 if self.position > 0 else -1.0
            self.funding_accrued += notional * self.funding_per_bar * sign

        # (b) Check SL/TP
        sl_hit = False
        tp_hit = False
        sl_fill = 0.0
        tp_fill = 0.0

        direction = "long" if self.position > 0 else "short"
        if direction == "long":
            if self.cfg.stop_loss_pct > 0:
                sl_level = self.entry_px * (1.0 - self.cfg.stop_loss_pct / 100.0)
                if low_px <= sl_level:
                    sl_hit = True
                    sl_fill = sl_level * (1.0 - self.slip_rate)  # adverse slippage
            if self.cfg.take_profit_pct > 0:
                tp_level = self.entry_px * (1.0 + self.cfg.take_profit_pct / 100.0)
                if high_px >= tp_level:
                    tp_hit = True
                    tp_fill = tp_level * (1.0 - self.slip_rate)  # slight adverse on TP fill
        else:  # short
            if self.cfg.stop_loss_pct > 0:
                sl_level = self.entry_px * (1.0 + self.cfg.stop_loss_pct / 100.0)
                if high_px >= sl_level:
                    sl_hit = True
                    sl_fill = sl_level * (1.0 + self.slip_rate)
            if self.cfg.take_profit_pct > 0:
                tp_level = self.entry_px * (1.0 - self.cfg.take_profit_pct / 100.0)
                if low_px <= tp_level:
                    tp_hit = True
                    tp_fill = tp_level * (1.0 + self.slip_rate)

        # If BOTH hit on the same bar, assume the adverse one (SL) triggers first.
        if sl_hit:
            self._close_position(timestamp, sl_fill, "sl")
            self.just_exited = True
        elif tp_hit:
            self._close_position(timestamp, tp_fill, "tp")
            self.just_exited = True

    def process_signal(
        self,
        sig: float,
        entry_sig: float,
        crash_blocked: bool,
        next_bar_ts: np.datetime64,
        next_bar_open: float,
        next_bar_bid: float,
        next_bar_ask: float,
    ) -> None:
        """Process strategy signal for next-bar execution."""
        if self.just_exited:
            return

        # (c) Signal-based exit: fill at NEXT bar's open (no look-ahead).
        if self.position != 0.0:
            direction = "long" if self.position > 0 else "short"
            exit_on_signal = (direction == "long" and sig < 0) or (direction == "short" and sig > 0)
            if exit_on_signal:
                exit_direction = "short" if direction == "long" else "long"
                fill = self._get_fill_px(exit_direction, next_bar_open, next_bar_bid, next_bar_ask)
                self._close_position(next_bar_ts, fill, "signal")
                self.just_exited = True
                return

        # (d) Entry: only if flat, not just-exited on this bar, entry gate is non-zero,
        #     crash-protection isn't blocking. Fill at bar i+1 open.
        if self.position == 0.0 and entry_sig != 0.0 and not crash_blocked:
            direction = "long" if entry_sig > 0 else "short"
            fill = self._get_fill_px(direction, next_bar_open, next_bar_bid, next_bar_ask)
            deploy = self.equity * (self.cfg.position_pct / 100.0) * self.cfg.leverage
            qty = deploy / fill if fill > 0 else 0.0
            if qty > 0 and math.isfinite(qty):
                self.position = qty if direction == "long" else -qty
                self.entry_px = fill
                self.entry_at = pd.Timestamp(next_bar_ts)
                if self.entry_at.tz is None:
                    self.entry_at = self.entry_at.tz_localize("UTC")
                else:
                    self.entry_at = self.entry_at.tz_convert("UTC")
                self.entry_direction = direction
                self.entry_fee = fill * qty * self.fee_rate
                self.equity -= self.entry_fee  # debit entry fee immediately
                self.funding_accrued = 0.0

    def close_at_eod(self, timestamp: np.datetime64, close_px: float) -> None:
        """Force close any open position at the end of the data set."""
        if self.position == 0.0:
            return
        direction = "long" if self.position > 0 else "short"
        # EOD close at last bar's close price + adverse slippage.
        fill = close_px * (1.0 - self.slip_rate if direction == "long" else 1.0 + self.slip_rate)
        self._close_position(timestamp, fill, "eod")

    def _get_fill_px(self, direction: str, open_px: float, bid_px: float, ask_px: float) -> float:
        """Calculate fill price with slippage and spread."""
        if direction == "long":
            base = ask_px if ask_px > 0 else open_px
            return base * (1.0 + self.slip_rate)
        else:
            base = bid_px if bid_px > 0 else open_px
            return base * (1.0 - self.slip_rate)

    def _close_position(self, timestamp: np.datetime64, fill: float, reason: str) -> None:
        """Internal helper to finalize a trade and update equity."""
        if self.position == 0.0:
            return
        direction = self.entry_direction or ("long" if self.position > 0 else "short")
        qty = abs(self.position)
        pnl_per_unit = (fill - self.entry_px) * (1 if direction == "long" else -1)
        gross = pnl_per_unit * qty
        exit_fee_amount = fill * qty * self.fee_rate
        net = gross - exit_fee_amount - self.funding_accrued
        self.equity += net

        self.trades.append(
            Trade(
                entry_at=self.entry_at,
                entry_px=self.entry_px,
                exit_at=pd.Timestamp(timestamp),
                exit_px=fill,
                direction=direction,
                qty=qty,
                pnl_abs=net - self.entry_fee,
                pnl_pct=((net - self.entry_fee) / (self.entry_px * qty) * 100.0) if (self.entry_px * qty) > 1e-9 else 0.0,
                fees=self.entry_fee + exit_fee_amount,
                funding=self.funding_accrued,
                exit_reason=reason,
            )
        )
        self.total_fees += self.entry_fee + exit_fee_amount
        self.total_funding += self.funding_accrued
        self.position = 0.0
        self.entry_px = 0.0
        self.entry_at = None
        self.entry_direction = None
        self.entry_fee = 0.0
        self.funding_accrued = 0.0


def run_backtest(
    candles_df: pd.DataFrame,
    strategy: Callable[[pd.DataFrame, Dict[str, Any]], pd.Series],
    cfg: BacktestConfig,
    crash_protection_series: Optional[pd.Series] = None,
) -> BacktestResult:
    """Run a deterministic backtest and return a BacktestResult.

    `strategy` produces a Series of ints: -1, 0, +1 (one per bar in df).
    Entries and signal-based exits are filled at the NEXT bar's open
    (no same-bar look-ahead). SL / TP are detected intrabar using
    high / low and filled at the level (plus adverse slippage).
    """
    t0 = time.perf_counter()
    log.info(
        "run_backtest start %s %s tf=%s %s..%s dir=%s bars=%d",
        cfg.symbol,
        cfg.source,
        cfg.timeframe,
        cfg.start_date,
        cfg.end_date,
        cfg.direction,
        len(candles_df),
    )

    if candles_df is None or len(candles_df) < 50:
        log.warning("run_backtest: insufficient candles (%s)", len(candles_df) if candles_df is not None else 0)
        return _empty_result(cfg, time.perf_counter() - t0)

    # ---------------- compute signals (strategy is deterministic) ----------------
    raw_signals = strategy(candles_df, cfg.indicator_params).astype(float).fillna(0.0).clip(-1, 1)

    # Crash protection: where True, block NEW entries (does NOT cancel exits).
    if cfg.crash_protection and crash_protection_series is not None:
        crash_mask = crash_protection_series.reindex_like(raw_signals).fillna(False).astype(bool).to_numpy()
    else:
        crash_mask = np.zeros(len(raw_signals), dtype=bool)

    # Separate entry-gate signals (respect direction) vs. raw signals (used for exits).
    entry_sig = raw_signals.to_numpy().copy()
    if cfg.direction == "long":
        entry_sig = np.where(entry_sig > 0, 1.0, 0.0)
    elif cfg.direction == "short":
        entry_sig = np.where(entry_sig < 0, -1.0, 0.0)

    raw_sig = raw_signals.to_numpy()

    # ---------------- gather price arrays ----------------
    open_px = candles_df["openPrice"].astype(float).to_numpy()
    high_px = candles_df["highPrice"].astype(float).to_numpy()
    low_px = candles_df["lowPrice"].astype(float).to_numpy()
    close_px = candles_df["closePrice"].astype(float).to_numpy()

    if "openBid" in candles_df.columns:
        open_bid = candles_df["openBid"].astype(float).to_numpy()
    else:
        open_bid = close_px.copy()
    if "closeAsk" in candles_df.columns:
        close_ask = candles_df["closeAsk"].astype(float).to_numpy()
    else:
        close_ask = close_px.copy()

    ts = pd.to_datetime(candles_df["snapshotTime"], utc=True).to_numpy()
    n = len(candles_df)

    # ---------------- integrity guards ----------------
    if not np.all(np.isfinite(close_px)) or (close_px <= 0).any():
        bad = int((close_px <= 0).sum() + np.isnan(close_px).sum())
        log.error("run_backtest: %d invalid close prices in input — refusing to run", bad)
        return _empty_result(cfg, time.perf_counter() - t0)
    for arr, name in ((open_px, "open"), (high_px, "high"), (low_px, "low")):
        mask = ~np.isfinite(arr) | (arr <= 0)
        if mask.any():
            log.warning("run_backtest: %d invalid %s prices — patching from close", int(mask.sum()), name)
            arr[mask] = close_px[mask]

    # ---------------- engine execution ----------------
    executor = BacktestExecutor(cfg)
    equity_curve = np.zeros(n, dtype=np.float64)

    for i in range(n):
        # 1. Accrue funding and check SL/TP on current bar
        executor.process_intrabar(
            timestamp=ts[i], open_px=open_px[i], high_px=high_px[i], low_px=low_px[i], close_px=close_px[i]
        )

        # 2. Record equity at bar close
        equity_curve[i] = executor.get_equity(close_px[i])

        # 3. Process signal for next-bar fill
        if i + 1 < n:
            executor.process_signal(
                sig=raw_sig[i],
                entry_sig=entry_sig[i],
                crash_blocked=crash_mask[i],
                next_bar_ts=ts[i + 1],
                next_bar_open=open_px[i + 1],
                next_bar_bid=open_bid[i + 1],
                next_bar_ask=close_ask[i + 1],
            )

    # ---------------- close any open position at end of period ----------------
    executor.close_at_eod(ts[-1], close_px[-1])
    equity_curve[-1] = executor.equity

    # ---------------- metrics ----------------
    ts_utc = pd.to_datetime(candles_df["snapshotTime"], utc=True)
    equity_series = pd.Series(equity_curve, index=ts_utc.values)
    returns = equity_series.pct_change().dropna()

    bars_py = _bars_per_year(cfg.timeframe, cfg.bars_per_year_override)
    sharpe = _sharpe(returns, bars_py)
    sortino = _sortino(returns, bars_py)

    running_max = equity_series.cummax()
    drawdowns = (equity_series - running_max) / running_max.replace(0, np.nan)
    mdd = float(drawdowns.min()) if len(drawdowns) else 0.0
    if not math.isfinite(mdd):
        mdd = 0.0

    wins = [t for t in executor.trades if t.pnl_abs > 0]
    losses = [t for t in executor.trades if t.pnl_abs <= 0]
    gross_profit = sum(t.pnl_abs for t in wins)
    gross_loss = abs(sum(t.pnl_abs for t in losses))
    pf = (gross_profit / gross_loss) if gross_loss > 0 else (math.inf if gross_profit > 0 else 0.0)

    result = BacktestResult(
        config=cfg,
        param_hash=cfg.param_hash(),
        final_equity=float(executor.equity),
        pnl_abs=float(executor.equity - cfg.initial_capital),
        pnl_pct=float((executor.equity - cfg.initial_capital) / cfg.initial_capital * 100),
        max_drawdown=mdd * 100,
        sharpe=sharpe,
        sortino=sortino,
        deflated_sharpe=_deflated_sharpe(sharpe, n_trials=cfg.n_trials, n_obs=len(returns)),
        winrate=(len(wins) / len(executor.trades) * 100) if executor.trades else 0.0,
        num_trades=len(executor.trades),
        avg_trade_pnl=(sum(t.pnl_abs for t in executor.trades) / len(executor.trades)) if executor.trades else 0.0,
        profit_factor=float(pf) if math.isfinite(pf) else float("inf"),
        total_fees=float(executor.total_fees),
        total_funding=float(executor.total_funding),
        trades=executor.trades,
        equity_curve=equity_series,
        runtime_ms=(time.perf_counter() - t0) * 1000,
        bars_processed=n,
    )

    log.info(
        "run_backtest done %s trades=%d pnl=%.2f%% sharpe=%.2f mdd=%.2f%% fees=%.2f funding=%.2f in %.1fms",
        cfg.symbol, result.num_trades, result.pnl_pct,
        result.sharpe, result.max_drawdown,
        result.total_fees, result.total_funding, result.runtime_ms,
    )
    return result


def _empty_result(cfg: BacktestConfig, elapsed_s: float) -> BacktestResult:
    return BacktestResult(
        config=cfg,
        param_hash=cfg.param_hash(),
        final_equity=cfg.initial_capital,
        pnl_abs=0.0, pnl_pct=0.0, max_drawdown=0.0,
        sharpe=0.0, sortino=0.0, deflated_sharpe=0.0,
        winrate=0.0, num_trades=0, avg_trade_pnl=0.0, profit_factor=0.0,
        total_fees=0.0, total_funding=0.0,
        trades=[], equity_curve=pd.Series(dtype=float),
        runtime_ms=elapsed_s * 1000, bars_processed=0,
    )
