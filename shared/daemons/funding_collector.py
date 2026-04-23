"""Minimal funding-rate collector.

Polls CCXT funding rate for a short list of perpetual symbols every 60s
and INSERTs into derivatives_snapshots. Idempotent via unique constraint
(instrument_id, source, snapshot_at).
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timezone

import ccxt
import asyncpg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
LOG = logging.getLogger("tickles.funding_collector")

DB_DSN = os.environ.get(
    "DATABASE_URL",
    f"postgres://{os.environ.get('DB_USER','admin')}:{os.environ.get('DB_PASSWORD','')}"
    f"@{os.environ.get('DB_HOST','127.0.0.1')}:{os.environ.get('DB_PORT','5432')}/tickles_shared",
)
POLL_S = int(os.environ.get("FUNDING_POLL_S", "60"))
SYMBOLS = os.environ.get("FUNDING_SYMBOLS", "BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT").split(",")
EXCHANGE = os.environ.get("FUNDING_EXCHANGE", "binance")

_stop = asyncio.Event()


async def ensure_instrument(pool: asyncpg.Pool, exchange: str, symbol: str) -> int:
    """Return instrument_id, creating a minimal row if missing.

    Existing convention: asset_class='crypto', symbol uses slash form (BTC/USDT).
    We strip the ccxt :USDT perp suffix because that is a market-type modifier,
    not part of the canonical symbol stored in `instruments`.
    """
    norm_symbol = symbol.split(":")[0]
    async with pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT id FROM instruments WHERE exchange=$1 AND symbol=$2 LIMIT 1",
            exchange, norm_symbol,
        )
        if row:
            return row["id"]
        row = await con.fetchrow(
            """INSERT INTO instruments (exchange, symbol, asset_class, is_active)
               VALUES ($1, $2, 'crypto', true) RETURNING id""",
            exchange, norm_symbol,
        )
        LOG.info("created instrument %s/%s id=%s", exchange, norm_symbol, row["id"])
        return row["id"]


async def poll_once(pool: asyncpg.Pool) -> int:
    ex = getattr(ccxt, EXCHANGE)({"enableRateLimit": True})
    rows_written = 0
    for sym in SYMBOLS:
        sym = sym.strip()
        if not sym:
            continue
        try:
            r = await asyncio.to_thread(ex.fetch_funding_rate, sym)
        except Exception as exc:
            LOG.warning("fetch_funding_rate %s failed: %s", sym, exc)
            continue
        rate = r.get("fundingRate")
        ts_ms = r.get("timestamp") or r.get("fundingTimestamp")
        if rate is None:
            continue
        if isinstance(ts_ms, (int, float)) and ts_ms > 0:
            as_of = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        else:
            as_of = datetime.now(timezone.utc)
        inst_id = await ensure_instrument(pool, EXCHANGE, sym)
        async with pool.acquire() as con:
            try:
                await con.execute(
                    """INSERT INTO derivatives_snapshots
                       (instrument_id, snapshot_at, funding_rate, source)
                       VALUES ($1, $2, $3, $4)
                       ON CONFLICT (instrument_id, source, snapshot_at) DO NOTHING""",
                    inst_id, as_of, float(rate), f"ccxt:{EXCHANGE}",
                )
                rows_written += 1
            except Exception as exc:
                LOG.warning("insert failed %s: %s", sym, exc)
    return rows_written


async def main() -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _stop.set)

    pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=2)
    LOG.info("connected to db, poll every %ss, symbols=%s, exchange=%s", POLL_S, SYMBOLS, EXCHANGE)
    try:
        while not _stop.is_set():
            n = await poll_once(pool)
            LOG.info("wrote %d funding snapshots", n)
            try:
                await asyncio.wait_for(_stop.wait(), timeout=POLL_S)
            except asyncio.TimeoutError:
                pass
    finally:
        await pool.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
