"""
1m Candle Daemon — Tickles & Co V2.0 (hardened 2026-04-17)
==========================================================

Collects 1-minute candles from CCXT-supported exchanges, upserts into the
Postgres `candles` table. Runs forever, memory-bounded.

HARDENING (audit 2026-04-17):
  * Producer skips the *current* (still-forming) minute — we only persist
    bars whose close_time is in the past. Prevents storing a partial bar
    that later mutates. [P0-E2]
  * Consumer uses a dedicated transaction via `pool.acquire()` and also
    reads the queue-draining loop that does NOT silently swallow buffer
    when flush fails — failed batches are re-queued with bounded retries
    before being dropped with a loud error. [P0-E3]
  * Producer task supervisor: if `run_instrument` crashes, it is respawned
    (with exponential backoff) rather than silently dying. [P1-E1]
  * asyncio event-loop acquisition updated for Python 3.10+ (use
    `asyncio.get_running_loop()`). [P1-E8]
  * SIGTERM/SIGINT handlers are best-effort on Windows (which doesn't
    support `loop.add_signal_handler` for SIGTERM).
  * Producer clamps `since` correctly: if fetch returns no rows and the
    exchange is up-to-date, don't loop fast — wait POLL_SECS.
  * On flush failure we DO NOT clear the buffer; we retry up to
    MAX_FLUSH_RETRIES with increasing backoff, then drop with loud error.
    This was a silent data-loss bug before. [P0-E1]
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from typing import Dict, List, Optional, Set, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_SHARED = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_SHARED)
for p in (_ROOT, _SHARED):
    if p not in sys.path:
        sys.path.insert(0, p)

import ccxt.async_support as ccxt_async

from shared.utils.db import get_shared_pool

log = logging.getLogger("tickles.candle_daemon")

BATCH_SIZE = int(os.getenv("CANDLE_DAEMON_BATCH", "100"))
FLUSH_SECS = float(os.getenv("CANDLE_DAEMON_FLUSH_S", "5.0"))
POLL_SECS  = float(os.getenv("CANDLE_DAEMON_POLL_S", "5.0"))
QUEUE_MAX  = int(os.getenv("CANDLE_DAEMON_QUEUE", "500"))
MAX_FLUSH_RETRIES = int(os.getenv("CANDLE_DAEMON_FLUSH_RETRIES", "5"))
TASK_BACKOFF_BASE = float(os.getenv("CANDLE_DAEMON_TASK_BACKOFF", "2.0"))
TASK_BACKOFF_MAX  = float(os.getenv("CANDLE_DAEMON_TASK_BACKOFF_MAX", "60.0"))


class ExchangeAdapter:
    """Thin wrapper around a CCXT async exchange."""
    def __init__(self, source: str):
        self.source = source
        ex_class = getattr(ccxt_async, source, None)
        if ex_class is None:
            raise ValueError(f"unknown CCXT exchange: {source}")
        self.client = ex_class({"enableRateLimit": True})

    async def fetch_recent_ohlcv(self, symbol: str, since_ms: Optional[int]) -> List[list]:
        """Fetch 1m OHLCV since `since_ms` (ms epoch).

        Returns [[ts,o,h,l,c,v], ...]. Exceptions are caught and logged at
        warning level; callers receive `[]` and will retry after POLL_SECS.
        """
        try:
            return await self.client.fetch_ohlcv(
                symbol, timeframe="1m", since=since_ms, limit=500)
        except Exception as e:
            log.warning("fetch_recent_ohlcv(%s@%s) error: %s",
                        symbol, self.source, e)
            return []

    async def close(self):
        try:
            await self.client.close()
        except Exception:
            pass


# --------------------------------------------------------------------
# Candle upsert
# --------------------------------------------------------------------
async def _flush(pool, pending: List[tuple]) -> bool:
    """Upsert a batch inside a single transaction.

    Returns True on success. On failure the caller keeps the buffer and
    retries. We never silently drop candles here — silent drops are the
    single most dangerous failure mode for a data collector.
    """
    if not pending:
        return True
    sql = """
        INSERT INTO candles
          (instrument_id, timeframe, source, "timestamp",
           "open", high, low, "close", volume)
        VALUES
          ($1, $2::timeframe_t, $3, $4, $5, $6, $7, $8, $9)
        ON CONFLICT (instrument_id, source, timeframe, "timestamp")
        DO UPDATE SET
          "open"  = EXCLUDED."open",
          high    = EXCLUDED.high,
          low     = EXCLUDED.low,
          "close" = EXCLUDED."close",
          volume  = EXCLUDED.volume
    """
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.executemany(sql, pending)
        return True
    except Exception as e:
        log.exception("flush failed (batch=%d): %s", len(pending), e)
        return False


# --------------------------------------------------------------------
# Per-instrument task
# --------------------------------------------------------------------
async def run_instrument(
    pool, adapter: ExchangeAdapter,
    iid: int, symbol: str, source: str,
    stop: asyncio.Event,
) -> None:
    log.info("start %s@%s iid=%s", symbol, source, iid)

    queue: asyncio.Queue[tuple] = asyncio.Queue(maxsize=QUEUE_MAX)

    async def producer():
        # Where to resume from: MAX(timestamp) + 1 minute, or now-1h if empty.
        last_ms = await pool.fetch_val(
            "SELECT EXTRACT(EPOCH FROM MAX(\"timestamp\"))*1000 "
            "FROM candles WHERE instrument_id=$1 AND source=$2 AND timeframe='1m'",
            (iid, source),
        )
        since = int(last_ms) + 60_000 if last_ms else int(time.time() * 1000) - 3_600_000
        while not stop.is_set():
            ohlcv = await adapter.fetch_recent_ohlcv(symbol, since_ms=since)
            # Skip the CURRENT (still-forming) minute. A 1m bar is only
            # "closed" once ts_ms + 60000 <= now_ms. [P0-E2]
            now_ms = int(time.time() * 1000)
            wall_cutoff_ms = now_ms - 60_000
            if ohlcv:
                progressed = False
                for row in ohlcv:
                    ts_ms, o, h, l, c, v = row
                    if ts_ms < since:
                        continue
                    if ts_ms > wall_cutoff_ms:
                        # Bar still forming — skip but don't advance `since`
                        # past it so next poll can pick it up once closed.
                        continue
                    ts = time_from_ms(ts_ms)
                    await queue.put((iid, "1m", source, ts,
                                     float(o), float(h), float(l), float(c),
                                     float(v or 0)))
                    since = ts_ms + 60_000
                    progressed = True
                if not progressed:
                    # No NEW closed bars — wait before polling again.
                    pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=POLL_SECS)
            except asyncio.TimeoutError:
                pass

    async def consumer():
        buffer: List[tuple] = []
        last_flush = time.time()
        while not (stop.is_set() and queue.empty() and not buffer):
            try:
                item = await asyncio.wait_for(queue.get(), timeout=FLUSH_SECS)
                buffer.append(item)
            except asyncio.TimeoutError:
                pass
            now = time.time()
            if buffer and (len(buffer) >= BATCH_SIZE or now - last_flush >= FLUSH_SECS):
                ok = False
                for attempt in range(MAX_FLUSH_RETRIES):
                    ok = await _flush(pool, buffer)
                    if ok:
                        break
                    await asyncio.sleep(min(2 ** attempt, 30))
                # Pass 2 P2: record `last_flush` AFTER the flush completes
                # (ok or dropped) so `now - last_flush` reflects actual
                # time since the last DB interaction — not the moment we
                # entered the retry loop.
                if ok:
                    log.debug("flushed %d candles for %s@%s",
                              len(buffer), symbol, source)
                else:
                    log.error(
                        "DROPPING %d candles for %s@%s after %d flush retries",
                        len(buffer), symbol, source, MAX_FLUSH_RETRIES,
                    )
                buffer.clear()
                last_flush = time.time()

    # Pass 2 regression fix: plain asyncio.gather leaks the surviving
    # task if its sibling raises. Use TaskGroup so a crash in producer
    # cancels the consumer (and vice versa), and nothing is left running.
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(producer(), name=f"producer:{symbol}@{source}")
            tg.create_task(consumer(), name=f"consumer:{symbol}@{source}")
    except* asyncio.CancelledError:
        raise
    except* Exception as eg:
        for exc in eg.exceptions:
            log.exception(
                "run_instrument(%s@%s) task crashed: %s",
                symbol, source, exc,
            )
        raise
    finally:
        log.info("stop %s@%s", symbol, source)


async def supervise_instrument(
    pool, adapter: ExchangeAdapter,
    iid: int, symbol: str, source: str,
    stop: asyncio.Event,
) -> None:
    """Respawn `run_instrument` on crash with exponential backoff."""
    attempt = 0
    while not stop.is_set():
        try:
            await run_instrument(pool, adapter, iid, symbol, source, stop)
            # Completed normally (stop event): exit cleanly.
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            attempt += 1
            backoff = min(TASK_BACKOFF_BASE * (2 ** (attempt - 1)), TASK_BACKOFF_MAX)
            log.error("supervise: %s@%s crashed; respawn #%d in %.1fs",
                      symbol, source, attempt, backoff)
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass


def time_from_ms(ms: int):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


# --------------------------------------------------------------------
# Main
# --------------------------------------------------------------------
async def list_active_instruments(pool) -> List[Tuple[int, str, str]]:
    rows = await pool.fetch_all(
        "SELECT id, symbol, exchange FROM instruments "
        "WHERE is_active = TRUE AND asset_class = 'crypto' "
        "ORDER BY exchange, symbol"
    )
    return [(r["id"], r["symbol"], r["exchange"]) for r in rows]


def _install_signal_handlers(loop, stop: asyncio.Event) -> None:
    """Best-effort signal handlers. SIGTERM on Windows is not supported by
    `loop.add_signal_handler`; fall back to `signal.signal` when possible.
    """
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, AttributeError, RuntimeError):
            if os.name != "nt" or sig == signal.SIGINT:
                try:
                    signal.signal(sig, lambda *_a: stop.set())
                except (OSError, ValueError):
                    pass


async def main():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info("candle_daemon: boot")

    pool = await get_shared_pool()
    instruments = await list_active_instruments(pool)
    log.info("candle_daemon: %d crypto instruments active", len(instruments))

    sources: Set[str] = {e for _, _, e in instruments}
    adapters: Dict[str, ExchangeAdapter] = {}
    for src in sources:
        try:
            adapters[src] = ExchangeAdapter(src)
        except Exception as e:
            log.error("candle_daemon: cannot init %s: %s", src, e)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, stop)

    tasks = []
    for iid, symbol, source in instruments:
        if source not in adapters:
            continue
        tasks.append(asyncio.create_task(
            supervise_instrument(pool, adapters[source], iid, symbol, source, stop),
            name=f"bt-{symbol}@{source}",
        ))

    log.info("candle_daemon: %d tasks spawned; waiting for stop", len(tasks))

    try:
        await stop.wait()
    finally:
        log.info("candle_daemon: shutdown")
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for a in adapters.values():
            await a.close()
        log.info("candle_daemon: done")


if __name__ == "__main__":
    asyncio.run(main())
