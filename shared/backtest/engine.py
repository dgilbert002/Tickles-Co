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
    log.info("run_backtest start %s %s tf=%s %s..%s dir=%s bars=%d",
             cfg.symbol, cfg.source, cfg.timeframe, cfg.start_date,
             cfg.end_date, cfg.direction, len(candles_df))

    if candles_df is None or len(candles_df) < 50:
        log.warning("run_backtest: insufficient candles (%s)",
                    len(candles_df) if candles_df is not None else 0)
        return _empty_result(cfg, time.perf_counter() - t0)

    # ---------------- compute signals (strategy is deterministic) ----------------
    raw_signals = strategy(candles_df, cfg.indicator_params).astype(float).fillna(0.0).clip(-1, 1)

    # Crash protection: where True, block NEW entries (does NOT cancel exits).
    if cfg.crash_protection and crash_protection_series is not None:
        crash_mask = crash_protection_series.reindex_like(raw_signals).fillna(False).astype(bool).to_numpy()
    else:
        crash_mask = np.zeros(len(raw_signals), dtype=bool)

    # Separate entry-gate signals (respect direction) vs. raw signals (used for exits).
    # This is the FIX for P0-1: a -1 in long-only mode must NOT silently become 0
    # because an existing long needs it to close.
    entry_sig = raw_signals.to_numpy().copy()
    if cfg.direction == "long":
        entry_sig = np.where(entry_sig > 0, 1.0, 0.0)
    elif cfg.direction == "short":
        entry_sig = np.where(entry_sig < 0, -1.0, 0.0)
    # 'both' keeps as-is.

    raw_sig = raw_signals.to_numpy()

    # ---------------- gather price arrays (keep names honest) ----------------
    open_px  = candles_df["openPrice"].astype(float).to_numpy()
    high_px  = candles_df["highPrice"].astype(float).to_numpy()
    low_px   = candles_df["lowPrice"].astype(float).to_numpy()
    close_px = candles_df["closePrice"].astype(float).to_numpy()

    # NB: the DB only provides open_bid / close_ask. There is no true close_bid or
    # open_ask column. The loader exposes them as `openBid` / `closeAsk` (honest
    # names as of 2026-04-17 hardening pass). For short-side fills we approximate
    # the bid at bar open using openBid, and for long-side fills we use closeAsk.
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
    # Pass 2 fix: close is required; for open/high/low fall back to close
    # when NaN or non-positive, so SL/TP detection doesn't silently degrade.
    if not np.all(np.isfinite(close_px)) or (close_px <= 0).any():
        bad = int((close_px <= 0).sum() + np.isnan(close_px).sum())
        log.error("run_backtest: %d invalid close prices in input — refusing to run",
                  bad)
        return _empty_result(cfg, time.perf_counter() - t0)
    for arr, name in ((open_px, "open"), (high_px, "high"), (low_px, "low")):
        mask = ~np.isfinite(arr) | (arr <= 0)
        if mask.any():
            log.warning("run_backtest: %d invalid %s prices — patching from close",
                        int(mask.sum()), name)
            arr[mask] = close_px[mask]

    # ---------------- engine state ----------------
    fee_rate  = cfg.fee_taker_bps  / 10_000.0
    slip_rate = cfg.slippage_bps   / 10_000.0
    bar_s     = _bar_seconds(cfg.timeframe)
    funding_per_bar = (cfg.funding_bps_per_8h / 10_000.0) * (bar_s / 28_800.0)

    equity = float(cfg.initial_capital)
    equity_curve = np.zeros(n, dtype=np.float64)
    position = 0.0  # signed qty (+ long, - short)
    entry_px = 0.0
    entry_at = None
    entry_direction: Optional[str] = None
    entry_fee = 0.0
    funding_accrued = 0.0
    trades: List[Trade] = []
    total_fees = 0.0
    total_funding = 0.0

    def _long_fill_at_open(bar: int) -> float:
        """Buy at ask (or open if ask unavailable) with adverse slippage.

        Pass 2 fix: PREFER ask over open when present — spread is a real
        trading cost that `slippage_bps` was never supposed to subsume.
        """
        base = close_ask[bar] if close_ask[bar] > 0 else open_px[bar]
        return base * (1.0 + slip_rate)

    def _short_fill_at_open(bar: int) -> float:
        """Sell at bid (or open if bid unavailable) with adverse slippage."""
        base = open_bid[bar] if open_bid[bar] > 0 else open_px[bar]
        return base * (1.0 - slip_rate)

    def _open_fill(direction: str, bar: int) -> float:
        return _long_fill_at_open(bar) if direction == "long" else _short_fill_at_open(bar)

    def _close_position(i_exit: int, fill: float, reason: str) -> None:
        nonlocal equity, position, entry_px, entry_at, entry_direction, entry_fee, funding_accrued
        nonlocal total_fees, total_funding
        if position == 0.0:
            return
        direction = entry_direction or ("long" if position > 0 else "short")
        qty = abs(position)
        pnl_per_unit = (fill - entry_px) * (1 if direction == "long" else -1)
        gross = pnl_per_unit * qty
        exit_fee_amount = fill * qty * fee_rate
        net = gross - exit_fee_amount - funding_accrued  # entry_fee was already debited at entry
        equity += net  # entry_fee already off the books
        trades.append(Trade(
            entry_at=entry_at, entry_px=entry_px,
            exit_at=pd.Timestamp(ts[i_exit]).tz_convert("UTC"),
            exit_px=fill, direction=direction, qty=qty,
            pnl_abs=net - entry_fee,  # total net across the trade (fee already off equity)
            pnl_pct=((net - entry_fee) / (entry_px * qty) * 100.0) if (entry_px * qty) > 0 else 0.0,
            fees=entry_fee + exit_fee_amount,
            funding=funding_accrued,
            exit_reason=reason,
        ))
        total_fees += entry_fee + exit_fee_amount
        total_funding += funding_accrued
        position = 0.0
        entry_px = 0.0
        entry_at = None
        entry_direction = None
        entry_fee = 0.0
        funding_accrued = 0.0

    # ---------------- main bar loop ----------------
    for i in range(n):
        sig = raw_sig[i]

        # (a) If a position is open, accrue funding for this bar based on direction.
        if position != 0.0 and funding_per_bar != 0.0:
            notional = abs(position) * close_px[i]
            # Positive funding rate: long pays, short receives.
            sign = 1.0 if position > 0 else -1.0
            funding_accrued += notional * funding_per_bar * sign

        # (b) Check SL/TP INTRABAR (using high/low). This is closer to live.
        sl_hit = False
        tp_hit = False
        sl_fill = 0.0
        tp_fill = 0.0
        if position != 0.0:
            direction = "long" if position > 0 else "short"
            if direction == "long":
                if cfg.stop_loss_pct > 0:
                    sl_level = entry_px * (1.0 - cfg.stop_loss_pct / 100.0)
                    if low_px[i] <= sl_level:
                        sl_hit = True
                        sl_fill = sl_level * (1.0 - slip_rate)  # adverse slippage
                if cfg.take_profit_pct > 0:
                    tp_level = entry_px * (1.0 + cfg.take_profit_pct / 100.0)
                    if high_px[i] >= tp_level:
                        tp_hit = True
                        tp_fill = tp_level * (1.0 - slip_rate)  # slight adverse on TP fill
            else:  # short
                if cfg.stop_loss_pct > 0:
                    sl_level = entry_px * (1.0 + cfg.stop_loss_pct / 100.0)
                    if high_px[i] >= sl_level:
                        sl_hit = True
                        sl_fill = sl_level * (1.0 + slip_rate)
                if cfg.take_profit_pct > 0:
                    tp_level = entry_px * (1.0 - cfg.take_profit_pct / 100.0)
                    if low_px[i] <= tp_level:
                        tp_hit = True
                        tp_fill = tp_level * (1.0 + slip_rate)

        # If BOTH hit on the same bar, assume the adverse one (SL) triggers first.
        # This is conservative and matches typical live behaviour.
        just_exited = False
        if sl_hit:
            _close_position(i, sl_fill, "sl")
            just_exited = True
        elif tp_hit:
            _close_position(i, tp_fill, "tp")
            just_exited = True

        # (e) Record mark-to-market equity using THIS bar's close BEFORE
        #     we schedule any signal exit/entry that fills at bar i+1 open.
        #     (Pass 2 fix: previously the equity at bar i reflected i+1's
        #      exit/entry, contaminating pct_change returns → bad Sharpe.)
        if position != 0.0:
            direction = "long" if position > 0 else "short"
            qty = abs(position)
            mtm_pnl = (close_px[i] - entry_px) * (1 if direction == "long" else -1) * qty
            equity_curve[i] = equity + mtm_pnl - funding_accrued
        else:
            equity_curve[i] = equity

        # (c) Signal-based exit: fill at NEXT bar's open (no look-ahead).
        #     If no next bar, defer to EOD close below.
        if position != 0.0 and not just_exited:
            direction = "long" if position > 0 else "short"
            exit_on_signal = (direction == "long" and sig < 0) or \
                             (direction == "short" and sig > 0)
            if exit_on_signal and i + 1 < n:
                exit_direction = "short" if direction == "long" else "long"
                fill = _open_fill(exit_direction, i + 1)
                _close_position(i + 1, fill, "signal")
                just_exited = True

        # (d) Entry: only if flat, not just-exited on this bar, entry gate is non-zero,
        #     crash-protection isn't blocking, and there is a next bar to fill on.
        #     Fill at bar i+1 open.
        if (position == 0.0 and not just_exited
                and entry_sig[i] != 0.0 and not crash_mask[i] and i + 1 < n):
            direction = "long" if entry_sig[i] > 0 else "short"
            fill = _open_fill(direction, i + 1)
            deploy = equity * (cfg.position_pct / 100.0) * cfg.leverage
            qty = deploy / fill if fill > 0 else 0.0
            if qty > 0 and math.isfinite(qty):
                position = qty if direction == "long" else -qty
                entry_px = fill
                entry_at = pd.Timestamp(ts[i + 1]).tz_convert("UTC")
                entry_direction = direction
                entry_fee = fill * qty * fee_rate
                equity -= entry_fee  # debit entry fee immediately
                funding_accrued = 0.0

    # ---------------- close any open position at end of period ----------------
    if position != 0.0:
        direction = "long" if position > 0 else "short"
        # EOD close at last bar's close price + adverse slippage.
        fill = close_px[-1] * (1.0 - slip_rate if direction == "long" else 1.0 + slip_rate)
        _close_position(n - 1, fill, "eod")
        equity_curve[-1] = equity

    # ---------------- metrics ----------------
    ts_utc = pd.to_datetime(candles_df["snapshotTime"], utc=True)
    equity_series = pd.Series(equity_curve, index=ts_utc.values)
    returns = equity_series.pct_change().dropna()

    bars_py = _bars_per_year(cfg.timeframe, cfg.bars_per_year_override)
    sharpe  = _sharpe(returns, bars_py)
    sortino = _sortino(returns, bars_py)

    running_max = equity_series.cummax()
    drawdowns = (equity_series - running_max) / running_max.replace(0, np.nan)
    mdd = float(drawdowns.min()) if len(drawdowns) else 0.0
    if not math.isfinite(mdd):
        mdd = 0.0

    wins = [t for t in trades if t.pnl_abs > 0]
    losses = [t for t in trades if t.pnl_abs <= 0]
    gross_profit = sum(t.pnl_abs for t in wins)
    gross_loss = abs(sum(t.pnl_abs for t in losses))
    pf = (gross_profit / gross_loss) if gross_loss > 0 else (math.inf if gross_profit > 0 else 0.0)

    result = BacktestResult(
        config=cfg,
        param_hash=cfg.param_hash(),
        final_equity=float(equity),
        pnl_abs=float(equity - cfg.initial_capital),
        pnl_pct=float((equity - cfg.initial_capital) / cfg.initial_capital * 100),
        max_drawdown=mdd * 100,
        sharpe=sharpe,
        sortino=sortino,
        deflated_sharpe=_deflated_sharpe(sharpe, n_trials=cfg.n_trials, n_obs=len(returns)),
        winrate=(len(wins) / len(trades) * 100) if trades else 0.0,
        num_trades=len(trades),
        avg_trade_pnl=(sum(t.pnl_abs for t in trades) / len(trades)) if trades else 0.0,
        profit_factor=float(pf) if math.isfinite(pf) else float("inf"),
        total_fees=float(total_fees),
        total_funding=float(total_funding),
        trades=trades,
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
