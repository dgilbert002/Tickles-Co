"""
Backtest Worker — Tickles & Co V2.0 (hardened 2026-04-17)
==========================================================

A single worker process that:
  1. Pulls jobs from the Redis queue (reliable BLMOVE claim).
  2. Loads candles from Postgres.
  3. Runs the backtest engine.
  4. Writes results to ClickHouse (idempotent by param_hash).
  5. Heartbeats via a background thread during process() so long jobs
     aren't false-reaped by the runner.
  6. Classifies failures as transient (retry) vs. permanent (fail-fast).
  7. Cleans up on SIGTERM / SIGINT.

HARDENING (audit 2026-04-17):
  * Background heartbeat thread — ticks HEARTBEAT_TTL_S / 3 regardless of
    what process() is doing. No more duplicate-execution-from-expired-HB.
  * reclaim_orphans() on startup — recovers any envelope we had BLMOVE'd
    but hadn't HSET'd into running when we were last killed.
  * Transient error classification — ConnectionError, TimeoutError, and
    certain psycopg2/ClickHouse exceptions are retried. Deterministic
    errors (KeyError, ValueError, schema mismatches) are hard-failed.
  * Single persistent asyncio loop per worker — no per-call new loops.
  * Explicit close of ClickHouseWriter + queue.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import socket
import sys
import threading
import time
import traceback
from typing import Any, Callable, Dict, Optional

from backtest.candle_loader import load_candles_sync
from backtest.ch_writer import ClickHouseWriter
from backtest.engine import BacktestConfig, run_backtest
from backtest.indicators import get as get_indicator
from backtest.queue import BacktestQueue, HEARTBEAT_TTL_S
from backtest.strategies import get as get_strategy
from shared.utils.db import get_shared_pool

log = logging.getLogger("tickles.worker")

_STOP = threading.Event()


def _on_sigterm(signum, frame):
    log.info("worker: received signal %s, draining…", signum)
    _STOP.set()


# Cache instrument_id lookups per worker process.
_IID_CACHE: Dict[str, int] = {}
_IID_LOCK = threading.Lock()
_LOOP: Optional[asyncio.AbstractEventLoop] = None
_LOOP_LOCK = threading.Lock()


def _get_loop() -> asyncio.AbstractEventLoop:
    """Return a single persistent event loop for this worker process."""
    global _LOOP
    with _LOOP_LOCK:
        if _LOOP is None or _LOOP.is_closed():
            _LOOP = asyncio.new_event_loop()
        return _LOOP


def _resolve_instrument_id(symbol: str, source: str) -> int:
    """Resolve (symbol, source) → instrument_id with a success-only cache.

    Pass 2 fix: previously any failure (transient DB error, missing row)
    was cached as `0` forever — poisoning every subsequent backtest for
    that pair in this worker. Now we only cache *positive* hits; misses
    and errors are transparently re-tried on the next call.
    """
    key = f"{symbol}@{source}"
    with _IID_LOCK:
        cached = _IID_CACHE.get(key)
        if cached:  # only trust non-zero entries
            return cached

    async def _run():
        pool = await get_shared_pool()
        row = await pool.fetch_one(
            "SELECT id FROM instruments WHERE symbol=$1 AND exchange=$2 LIMIT 1",
            (symbol, source),
        )
        return int(row["id"]) if row else 0

    try:
        iid = _get_loop().run_until_complete(_run())
    except Exception:
        log.exception("resolve_instrument_id failed for %s (not cached)", key)
        return 0  # transient: don't cache, let caller retry later

    if iid > 0:
        with _IID_LOCK:
            _IID_CACHE[key] = iid
    else:
        log.warning("resolve_instrument_id: no row for %s (not cached)", key)
    return iid


# --------------------------------------------------------------------------
# Error classification — transient vs. permanent
# --------------------------------------------------------------------------
_TRANSIENT_EXC_NAMES = {
    "ConnectionError", "TimeoutError",
    "OperationalError",       # psycopg2
    "InterfaceError",         # psycopg2
    "NetworkError",           # clickhouse_driver.errors.NetworkError
    "SocketTimeoutError",     # clickhouse_driver
    "ConnectionRefusedError", "BrokenPipeError",
    "RedisError", "RedisConnectionError", "ResponseError",
}


def is_transient_error(exc: BaseException) -> bool:
    """Heuristic: True for network/infra errors we should retry on."""
    if isinstance(exc, (ConnectionError, TimeoutError, BrokenPipeError,
                        ConnectionRefusedError, OSError)):
        # OSError covers most network failure cases across libs.
        return True
    name = type(exc).__name__
    if name in _TRANSIENT_EXC_NAMES:
        return True
    # Walk the exception chain for wrapped transient causes.
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        return is_transient_error(cause)
    return False


# --------------------------------------------------------------------------
# Heartbeat thread
# --------------------------------------------------------------------------
class Heartbeater(threading.Thread):
    """Background thread that ticks q.heartbeat(worker_id) until stopped."""

    def __init__(self, q: BacktestQueue, worker_id: str,
                 interval_s: float = max(5.0, HEARTBEAT_TTL_S / 3.0)):
        super().__init__(name=f"hb-{worker_id}", daemon=True)
        self.q = q
        self.worker_id = worker_id
        self.interval_s = interval_s
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.q.heartbeat(self.worker_id)
            except Exception:
                # Don't log every tick on a broken Redis — just print once.
                log.debug("heartbeat tick failed", exc_info=True)
            self._stop.wait(self.interval_s)


# --------------------------------------------------------------------------
# Job processing
# --------------------------------------------------------------------------
def process(job: Dict[str, Any], ch: ClickHouseWriter) -> Dict[str, Any]:
    p = job["payload"]
    log.info("process: job=%s symbol=%s strategy=%s", job["id"],
             p.get("symbol"), p.get("strategy"))

    risk = p.get("risk", {})
    cfg = BacktestConfig(
        symbol=p["symbol"],
        source=p["source"],
        timeframe=p["timeframe"],
        start_date=p["start_date"],
        end_date=p["end_date"],
        direction=p.get("direction", "long"),
        initial_capital=float(risk.get("initial_capital", 10_000.0)),
        position_pct=float(risk.get("position_pct", 100.0)),
        leverage=float(risk.get("leverage", 1.0)),
        fee_taker_bps=float(risk.get("fee_bps", 5.0)),
        slippage_bps=float(risk.get("slip_bps", 2.0)),
        funding_bps_per_8h=float(risk.get("funding_bps_per_8h", 0.0)),
        stop_loss_pct=float(risk.get("sl", 0.0)),
        take_profit_pct=float(risk.get("tp", 0.0)),
        crash_protection=bool(risk.get("crash_protection", False)),
        strategy_name=p["strategy"],
        indicator_name=p.get("indicator_name", ""),
        indicator_params=p.get("params", {}),
        n_trials=int(p.get("n_trials", 1)),
    )

    if ch.run_exists(cfg.param_hash()):
        log.info("process: dedup hit in CH for %s", cfg.param_hash()[:8])
        return {"status": "dedup", "param_hash": cfg.param_hash()}

    df = load_candles_sync(
        cfg.symbol, cfg.source, cfg.timeframe, cfg.start_date, cfg.end_date,
        include_bid_ask=True,
    )
    if df is None or len(df) < 50:
        raise RuntimeError(
            f"insufficient candles for {cfg.symbol}@{cfg.source} "
            f"{cfg.timeframe} {cfg.start_date}..{cfg.end_date}"
        )

    try:
        strategy = get_strategy(cfg.strategy_name)
    except KeyError as e:
        # Deterministic error — permanent fail, no retry.
        raise RuntimeError(f"strategy not found: {e}") from e

    crash_series = None
    if cfg.crash_protection:
        crash_fn = get_indicator("crash_protection").fn
        crash_series = crash_fn(df, {})

    result = run_backtest(df, strategy, cfg, crash_protection_series=crash_series)

    iid = _resolve_instrument_id(cfg.symbol, cfg.source)
    run_id = ch.write_result(result, batch_id=p.get("batch_id"), instrument_id=iid)

    return {
        "status":          "ok",
        "run_id":          run_id,
        "param_hash":      result.param_hash,
        "pnl_pct":         result.pnl_pct,
        "sharpe":          result.sharpe,
        "deflated_sharpe": result.deflated_sharpe,
        "winrate":         result.winrate,
        "num_trades":      result.num_trades,
        "max_drawdown":    result.max_drawdown,
        "runtime_ms":      result.runtime_ms,
        "total_fees":      result.total_fees,
        "total_funding":   result.total_funding,
    }


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", default=f"w{os.getpid()}", help="worker id")
    parser.add_argument("--block-s", type=int, default=5)
    args = parser.parse_args()

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if os.name != "nt":
        # Windows doesn't support settable SIGTERM. We only install it on POSIX.
        try:
            signal.signal(signal.SIGTERM, _on_sigterm)
        except (OSError, ValueError):
            pass
    signal.signal(signal.SIGINT, _on_sigterm)

    q = BacktestQueue()
    q.register_worker(args.id)
    q.reclaim_orphans(args.id)

    ch = ClickHouseWriter()

    hb = Heartbeater(q, args.id)
    hb.start()

    log.info("worker %s: online (host=%s pid=%d), polling queue",
             args.id, socket.gethostname(), os.getpid())
    try:
        while not _STOP.is_set():
            try:
                job = q.claim(args.id, block_s=args.block_s)
            except Exception as e:
                log.error("worker %s: queue.claim failed: %s", args.id, e)
                _STOP.wait(2.0)
                continue
            if job is None:
                continue
            try:
                summary = process(job, ch)
                q.complete(job["id"], summary, worker_id=args.id)
                log.info("job %s done in %.0fms",
                         job["id"], summary.get("runtime_ms", 0))
            except BaseException as e:  # noqa: BLE001 — we classify below
                transient = is_transient_error(e)
                log.error("job %s failed (%s, transient=%s): %s",
                          job["id"], type(e).__name__, transient, e)
                log.debug("traceback:\n%s", traceback.format_exc())
                try:
                    q.fail(job["id"], str(e), retry=transient, worker_id=args.id)
                except Exception:
                    log.exception("queue.fail failed for %s", job["id"])
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
    finally:
        log.info("worker %s: shutting down", args.id)
        hb.stop()
        hb.join(timeout=5.0)
        try:
            q.close()
        except Exception:
            pass
        try:
            _get_loop().close()
        except Exception:
            pass
        log.info("worker %s: exit", args.id)


if __name__ == "__main__":
    main()
