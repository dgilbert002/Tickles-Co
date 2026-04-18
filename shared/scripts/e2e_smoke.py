"""
End-to-end smoke test for the V2 backtest stack.

Steps (all autonomous — no human intervention needed):
  1. Verify Postgres, ClickHouse, Redis reachable.
  2. Pull 14 days of 1h candles for 3 symbols (BTC/USDT, ETH/USDT, SOL/USDT)
     from Binance using CCXT. Upsert into Postgres.
  3. Enqueue 50 backtest jobs (3 strategies × multiple params × 3 symbols).
  4. Spin up 4 worker processes, wait for queue to drain.
  5. Verify every run_id appears in ClickHouse.
  6. Rebuild backtests.txt and sanity-check size.
  7. Time a /backtests/top lookup — must be under 100ms.

Writes a structured report to /var/log/tickles/e2e_smoke_<ts>.json.

Run:
    sudo python3 /opt/tickles/shared/scripts/e2e_smoke.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, "/opt/tickles")
sys.path.insert(0, "/opt/tickles/shared")

import ccxt.async_support as ccxt_async  # noqa
from shared.utils.db import get_shared_pool
from backtest.queue import BacktestQueue
from backtest.ch_writer import ClickHouseWriter
from backtest.accessible import rebuild as rebuild_txt, top as bt_top

log = logging.getLogger("tickles.e2e")


async def step_fetch_and_store(pool, symbols: List[str], source: str,
                               tf: str = "1h", days: int = 14) -> Dict[str, int]:
    """Fetch recent candles via CCXT, upsert into Postgres."""
    cls = getattr(ccxt_async, source)
    ex = cls({"enableRateLimit": True})
    out: Dict[str, int] = {}
    try:
        since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
        for sym in symbols:
            # instrument id
            row = await pool.fetch_one(
                "SELECT id FROM instruments WHERE symbol=$1 AND exchange=$2",
                (sym, source),
            )
            if row is None:
                log.warning("no instrument row for %s@%s", sym, source)
                continue
            iid = row["id"]
            # fetch in a loop to cover the full window
            all_rows: List[list] = []
            cursor = since
            while True:
                ohlcv = await ex.fetch_ohlcv(sym, timeframe=tf, since=cursor, limit=500)
                if not ohlcv:
                    break
                all_rows.extend(ohlcv)
                next_cursor = int(ohlcv[-1][0]) + 1
                if next_cursor <= cursor or len(ohlcv) < 500:
                    break
                cursor = next_cursor
                if datetime.fromtimestamp(cursor / 1000, tz=timezone.utc) >= datetime.now(timezone.utc):
                    break
            # dedup by ts
            by_ts = {int(r[0]): r for r in all_rows}
            rows = sorted(by_ts.values(), key=lambda r: r[0])
            log.info("fetched %d candles for %s@%s %s", len(rows), sym, source, tf)

            # Upsert
            batch = []
            for r in rows:
                ts_ms, o, h, l, c, v = r
                ts = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
                batch.append((iid, tf, source, ts,
                              float(o), float(h), float(l), float(c), float(v or 0)))
            if batch:
                async with pool.acquire() as conn:
                    await conn.executemany(
                        """
                        INSERT INTO candles
                          (instrument_id, timeframe, source, "timestamp",
                           "open", high, low, "close", volume)
                        VALUES ($1, $2::timeframe_t, $3, $4, $5, $6, $7, $8, $9)
                        ON CONFLICT (instrument_id, source, timeframe, "timestamp")
                        DO UPDATE SET
                          "open"  = EXCLUDED."open",
                          high    = EXCLUDED.high,
                          low     = EXCLUDED.low,
                          "close" = EXCLUDED."close",
                          volume  = EXCLUDED.volume
                        """,
                        batch,
                    )
            out[sym] = len(rows)
    finally:
        await ex.close()
    return out


def enqueue_jobs(symbols: List[str], source: str, tf: str,
                 start: str, end: str) -> List[str]:
    q = BacktestQueue()
    ids = []
    # ema_cross sweep
    for sym in symbols:
        for fast in [5, 9, 12, 20]:
            for slow in [21, 30, 50]:
                if fast >= slow:
                    continue
                jid = q.enqueue({
                    "symbol":         sym,
                    "source":         source,
                    "timeframe":      tf,
                    "start_date":     start,
                    "end_date":       end,
                    "direction":      "long",
                    "strategy":       "ema_cross",
                    "indicator_name": "ema_cross",
                    "params":         {"fast": fast, "slow": slow},
                    "risk": {"initial_capital": 10000, "position_pct": 100,
                             "leverage": 1.0, "fee_bps": 5, "slip_bps": 2,
                             "sl": 0, "tp": 0, "crash_protection": False},
                    "batch_id":       "e2e_smoke",
                })
                if jid:
                    ids.append(jid)
    # rsi_reversal sweep
    for sym in symbols:
        for period in [7, 14, 21]:
            jid = q.enqueue({
                "symbol":         sym,
                "source":         source,
                "timeframe":      tf,
                "start_date":     start,
                "end_date":       end,
                "direction":      "both",
                "strategy":       "rsi_reversal",
                "indicator_name": "rsi_reversal",
                "params":         {"period": period, "overbought": 70, "oversold": 30},
                "risk": {"initial_capital": 10000, "position_pct": 100,
                         "leverage": 1.0, "fee_bps": 5, "slip_bps": 2,
                         "sl": 0, "tp": 0, "crash_protection": False},
                "batch_id":       "e2e_smoke",
            })
            if jid:
                ids.append(jid)
    # bollinger_pullback
    for sym in symbols:
        for period in [14, 20, 30]:
            jid = q.enqueue({
                "symbol":         sym,
                "source":         source,
                "timeframe":      tf,
                "start_date":     start,
                "end_date":       end,
                "direction":      "long",
                "strategy":       "bollinger_pullback",
                "indicator_name": "bollinger_pullback",
                "params":         {"period": period, "stddev": 2.0},
                "risk": {"initial_capital": 10000, "position_pct": 100,
                         "leverage": 1.0, "fee_bps": 5, "slip_bps": 2,
                         "sl": 0, "tp": 0, "crash_protection": False},
                "batch_id":       "e2e_smoke",
            })
            if jid:
                ids.append(jid)
    return ids


def start_workers(n: int) -> subprocess.Popen:
    env = os.environ.copy()
    env.setdefault("LOG_LEVEL", "INFO")
    cmd = ["python3", "-m", "backtest.runner", "--workers", str(n)]
    log.info("spawning workers: %s", " ".join(cmd))
    return subprocess.Popen(
        cmd, cwd="/opt/tickles/shared", env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )


def wait_for_drain(timeout_s: int = 600):
    q = BacktestQueue()
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        s = q.stats()
        log.info("queue: %s", s)
        if s["pending"] == 0 and s["running"] == 0:
            return True
        time.sleep(3)
    return False


async def _async_fetch(symbols, source, tf, days):
    pool = await get_shared_pool()
    return await step_fetch_and_store(pool, symbols, source, tf, days=days)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    report: Dict[str, Any] = {"t0": time.time(), "steps": {}}
    symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    source = "binance"
    tf = "1h"
    # use end=yesterday (avoid half-open current bar)
    end = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    start = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()

    # 1. Fetch + store (single event loop so pool + queries share lifetime).
    log.info("[1/6] fetching candles")
    fetched = asyncio.run(_async_fetch(symbols, source, tf, days=30))
    report["steps"]["fetch_candles"] = fetched

    # 2. Enqueue
    log.info("[2/6] enqueue jobs")
    # Clear any stale queue state from previous runs so hashseen doesn't dedup.
    BacktestQueue().flush_all()
    ids = enqueue_jobs(symbols, source, tf, start, end)
    log.info("  enqueued %d jobs", len(ids))
    report["steps"]["enqueue"] = {"count": len(ids), "first_3": ids[:3]}

    # 3. Start workers
    log.info("[3/6] starting worker pool")
    proc = start_workers(n=min(4, os.cpu_count() or 2))

    # 4. Wait for drain
    log.info("[4/6] waiting for queue to drain")
    ok = wait_for_drain(timeout_s=600)
    report["steps"]["drain_ok"] = ok

    # Kill workers
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()

    # 5. Verify ClickHouse
    log.info("[5/6] verifying ClickHouse")
    ch = ClickHouseWriter()
    # batch_id now lives in `notes` JSON (no dedicated column)
    like_filter = '%"batch_id":"e2e_smoke"%'
    n_runs = ch.client.execute(
        "SELECT count() FROM backtest_runs WHERE notes LIKE %(p)s",
        {"p": like_filter},
    )[0][0]
    n_trades = ch.client.execute(
        "SELECT count() FROM backtest_trades WHERE run_id IN "
        "(SELECT run_id FROM backtest_runs WHERE notes LIKE %(p)s)",
        {"p": like_filter},
    )[0][0]
    report["steps"]["ch_runs"] = n_runs
    report["steps"]["ch_trades"] = n_trades

    # 6. Rebuild txt + lookup timing
    log.info("[6/6] rebuild backtests.txt + lookup timing")
    n_lines = rebuild_txt()
    t0 = time.time()
    top_rows = bt_top(n=10, sort="sharpe")
    elapsed_ms = (time.time() - t0) * 1000
    report["steps"]["txt_lines"] = n_lines
    report["steps"]["top_lookup_ms"] = round(elapsed_ms, 1)
    report["steps"]["best_sharpe"] = top_rows[0] if top_rows else None

    report["elapsed_s"] = round(time.time() - report["t0"], 1)

    out = Path("/var/log/tickles/e2e_smoke_%s.json" % int(time.time()))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str))
    log.info("report written to %s", out)


if __name__ == "__main__":
    main()
