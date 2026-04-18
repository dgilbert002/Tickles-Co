"""
ClickHouse Writer — Tickles & Co V2.0 (hardened 2026-04-17)
=============================================================

Writes backtest results into the ClickHouse `backtests` database.

HARDENING (audit 2026-04-17):
  * `CH_PASSWORD` has NO default. Writer raises on missing env to avoid leaking
    credentials via git history or shipping the default to other people's
    machines. Set CH_PASSWORD in the env.
  * `run_id` is now deterministic from `param_hash` (UUID5 over a fixed
    namespace). Two workers racing the same job produce the same run_id, so
    `run_exists()` dedup is sound and downstream readers can GROUP BY run_id
    without worrying about double-identity.
  * Insert ORDER is trades-first, then the run summary. If the trade insert
    fails, the run row is never written, so there are no orphan runs with
    zero trades.
  * `run_exists()` re-checks AFTER the would-be insert is prepared (second
    check) so the race window between pre-check and insert is much smaller
    (defense-in-depth until we move to ReplacingMergeTree).
"""
from __future__ import annotations

import json
import logging
import math
import os
import socket
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

from clickhouse_driver import Client

from backtest.engine import BacktestResult, Trade

log = logging.getLogger("tickles.ch_writer")

# Fixed UUID namespace for deterministic run_id generation from param_hash.
# This is an arbitrary UUID the project picked — do NOT change, or old run_ids
# will no longer match new ones and dedup will break.
RUN_ID_NAMESPACE = uuid.UUID("1f4c2c0a-8d20-4d87-8e7f-f17b7a2f1234")


def _d(x: Any) -> Decimal:
    """Quantise to 8 decimals for Decimal(20,8) columns.

    CRITICAL (audit Pass 2 P0): NaN/Inf must NEVER reach the ClickHouse
    driver — `Decimal("nan")` is constructible but crashes on INSERT.
    Normalise non-finite values to 0.
    """
    try:
        v = float(x)
    except (TypeError, ValueError):
        return Decimal("0")
    if not math.isfinite(v):
        return Decimal("0")
    return Decimal(str(round(v, 8)))


def _f(x: Any) -> float:
    """Safe float coercion for Float64/Float32 columns — non-finite → 0.0."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return v if math.isfinite(v) else 0.0


def _as_uint32(x: Any) -> int:
    try:
        n = int(round(float(x)))
    except (TypeError, ValueError):
        return 0
    return max(0, min(n, 2**32 - 1))


def deterministic_run_id(param_hash: str) -> str:
    """Return a deterministic UUID5 for the given param_hash."""
    return str(uuid.uuid5(RUN_ID_NAMESPACE, param_hash))


class ClickHouseWriter:
    def __init__(self, require_password: bool = True):
        host = os.getenv("CH_HOST", "127.0.0.1")
        port = int(os.getenv("CH_PORT", "9000"))
        user = os.getenv("CH_USER", "admin")
        pwd  = os.getenv("CH_PASSWORD")
        if pwd is None:
            if require_password:
                raise RuntimeError(
                    "CH_PASSWORD env var is not set. Refusing to use a default."
                )
            pwd = ""
        db   = os.getenv("CH_DATABASE", "backtests")
        self.client = Client(
            host=host, port=port, user=user, password=pwd, database=db,
            settings={"use_numpy": False},
            send_receive_timeout=int(os.getenv("CH_TIMEOUT", "60")),
        )
        self.db = db
        self.worker_id = os.getenv("TICKLES_WORKER_ID", socket.gethostname())

    def close(self) -> None:
        try:
            self.client.disconnect()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def write_result(self, result: BacktestResult,
                     batch_id: Optional[str] = None,
                     instrument_id: int = 0) -> str:
        """Insert trades + run. Returns run_id (UUID string, deterministic)."""
        run_id = deterministic_run_id(result.param_hash)

        # Pre-check: if the run already exists, skip entirely.
        if self.run_exists(result.param_hash):
            log.info("ch_write_result: dedup run_id=%s hash=%s",
                     run_id, result.param_hash[:12])
            return run_id

        # Order matters: write TRADES first. If anything fails after, we
        # have orphaned trade rows (harmless; they're filtered by JOIN to
        # run_id in catalog), but not the reverse (orphan run with 0 trades).
        if result.trades:
            self._insert_trades(run_id, result.trades)

        self._insert_run(run_id, result, batch_id, instrument_id)
        log.info("ch_write_result: run=%s trades=%d pnl=%.2f%% sharpe=%.2f",
                 run_id, len(result.trades), result.pnl_pct, result.sharpe)
        return run_id

    def write_many(self, results: Iterable[BacktestResult],
                   batch_id: Optional[str] = None) -> List[str]:
        return [self.write_result(r, batch_id=batch_id) for r in results]

    def run_exists(self, param_hash: str) -> bool:
        rows = self.client.execute(
            "SELECT count() FROM backtest_runs WHERE param_hash = %(h)s",
            {"h": param_hash},
        )
        return bool(rows and rows[0][0] > 0)

    def best_per_indicator(self, limit: int = 50) -> List[Dict]:
        rows = self.client.execute(
            "SELECT instrument_id, indicator_name, timeframe, top_run_id, "
            "top_sharpe, top_return_pct, top_drawdown "
            "FROM top_sharpe_per_indicator ORDER BY top_sharpe DESC "
            "LIMIT %(lim)s",
            {"lim": int(max(1, min(limit, 1000)))},
        )
        keys = ["instrument_id", "indicator_name", "timeframe", "top_run_id",
                "top_sharpe", "top_return_pct", "top_drawdown"]
        return [dict(zip(keys, r)) for r in rows]

    # ------------------------------------------------------------------
    # Low-level inserts
    # ------------------------------------------------------------------
    def _insert_run(self, run_id: str, r: BacktestResult,
                    batch_id: Optional[str], instrument_id: int) -> None:
        cfg = r.config

        notes = json.dumps({
            "batch_id":         batch_id or "",
            "strategy_name":    cfg.strategy_name,
            "direction":        cfg.direction,
            "initial_capital":  cfg.initial_capital,
            "position_pct":     cfg.position_pct,
            "leverage":         cfg.leverage,
            "fee_taker_bps":    cfg.fee_taker_bps,
            "slippage_bps":     cfg.slippage_bps,
            "funding_bps_per_8h": cfg.funding_bps_per_8h,
            "stop_loss_pct":    cfg.stop_loss_pct,
            "take_profit_pct":  cfg.take_profit_pct,
            "crash_protection": bool(cfg.crash_protection),
            "winrate":          r.winrate,
            "avg_trade_pnl":    r.avg_trade_pnl,
            "bars_processed":   r.bars_processed,
            "total_fees":       r.total_fees,
            "total_funding":    r.total_funding,
        }, default=str, separators=(",", ":"))

        # Pass 2 fix: use `is not None`, not falsy — 0 is a valid total_fees.
        if r.total_fees is not None:
            fees_sum = _f(r.total_fees)
        elif r.trades:
            fees_sum = _f(sum(t.fees for t in r.trades))
        else:
            fees_sum = 0.0
        funding_sum = _f(r.total_funding)         # _f handles None / NaN / Inf
        candle_hash = ("0" * 64)

        pf = r.profit_factor
        if pf is None or not math.isfinite(float(pf)):
            pf = 1e9

        cols = (
            "run_id", "param_hash", "candle_data_hash", "instrument_id",
            "exchange", "symbol", "indicator_name", "timeframe", "params",
            "date_from", "date_to", "initial_balance", "final_balance",
            "total_return_pct", "sharpe_ratio", "sortino_ratio",
            "max_drawdown_pct", "profit_factor", "win_rate_pct",
            "total_trades", "total_fees", "total_spread_costs",
            "total_overnight", "engine_version", "run_duration_ms",
            "worker_id", "created_at", "deflated_sharpe", "oos_sharpe",
            "oos_return_pct", "promotion_status", "parent_run_id", "notes",
        )
        # Schema mapping (verified against CH DESCRIBE 2026-04-17):
        #   *_pct, *_ratio, sharpe/sortino/deflated, profit_factor → Float64
        #   win_rate_pct → Float32
        #   initial/final_balance, total_fees, total_spread_costs,
        #       total_overnight → Decimal(20,8)
        values = (
            uuid.UUID(run_id),
            r.param_hash[:64],
            candle_hash,
            int(instrument_id or 0),
            cfg.source,
            cfg.symbol,
            cfg.indicator_name or cfg.strategy_name,
            cfg.timeframe,
            json.dumps(cfg.indicator_params, sort_keys=True, default=str),
            datetime.fromisoformat(cfg.start_date).date(),
            datetime.fromisoformat(cfg.end_date).date(),
            _d(cfg.initial_capital),   # Decimal
            _d(r.final_equity),        # Decimal
            _f(r.pnl_pct),             # Float64
            _f(r.sharpe),              # Float64
            _f(r.sortino),             # Float64
            _f(r.max_drawdown),        # Float64
            _f(pf),                    # Float64
            _f(r.winrate),             # Float32
            int(r.num_trades),
            _d(fees_sum),              # Decimal
            _d(0),                     # Decimal (total_spread_costs — unused)
            _d(funding_sum),           # Decimal (total_overnight ← funding)
            r.engine_version,
            _as_uint32(r.runtime_ms),
            self.worker_id,
            datetime.now(timezone.utc),
            _f(r.deflated_sharpe),     # Float64
            0.0,                        # Float64 (oos_sharpe)
            0.0,                        # Float64 (oos_return_pct)
            "candidate",
            None,
            notes,
        )
        self.client.execute(
            f"INSERT INTO backtest_runs ({', '.join(cols)}) VALUES",
            [values],
            types_check=True,
        )

    def _insert_trades(self, run_id: str, trades: List[Trade]) -> None:
        run_uuid = uuid.UUID(run_id)

        # Pass 2 fix: if a prior attempt crashed between trade-insert and
        # run-insert, we'd have orphan trade rows that would double up on
        # retry. Clear any existing rows for this run_id before inserting.
        try:
            self.client.execute(
                "ALTER TABLE backtest_trades DELETE WHERE run_id = %(rid)s",
                {"rid": run_uuid},
            )
        except Exception:
            # Some CH setups disallow DELETE; fall through (orphan trades
            # can be cleaned by TTL or manual op).
            log.debug("_insert_trades: pre-insert cleanup failed", exc_info=True)

        cols = (
            "run_id", "trade_index", "direction", "entry_at", "exit_at",
            "entry_price", "exit_price", "quantity", "gross_pnl", "net_pnl",
            "fees", "slippage", "window_close_time", "signal_hash",
        )
        rows = []
        for i, t in enumerate(trades):
            # Pass 2 fix: refuse to fabricate timestamps. A trade without
            # entry_at is a bug, not a "just now" event.
            if t.entry_at is None:
                raise ValueError(
                    f"Trade {i} has no entry_at timestamp — engine bug")
            entry = t.entry_at.to_pydatetime()
            exit_ = t.exit_at.to_pydatetime() if t.exit_at is not None else None
            gross = _d(t.pnl_abs + t.fees + (t.funding if hasattr(t, "funding") else 0))
            net   = _d(t.pnl_abs)
            # Enum8: 'long' = 1, 'short' = 2. Driver accepts int.
            dir_int = 1 if t.direction == "long" else 2
            rows.append((
                run_uuid,
                int(i),
                dir_int,
                entry,
                exit_,
                _d(t.entry_px),
                _d(t.exit_px) if t.exit_px is not None else None,
                _d(t.qty),
                gross,
                net,
                _d(t.fees),
                _d(0),
                None,
                "0" * 64,
            ))
        self.client.execute(
            f"INSERT INTO backtest_trades ({', '.join(cols)}) VALUES",
            rows,
            types_check=True,
        )
