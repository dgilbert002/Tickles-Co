"""
Module: capital_price_poller
Purpose: Capital.com REST price poller — fetches prices and inserts 1m candles
Location: /opt/tickles/shared/market_data/capital_price_poller.py

Uses CapitalAdapter (shared/connectors/capital_adapter.py) for auth and REST calls.
Polls 10 instruments every 60s, writes 1m candles to public.candles.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

sys.path.insert(0, "/opt/tickles")

from shared.connectors.capital_adapter import CapitalAdapter
from shared.utils.db import get_shared_pool
from shared.utils.config import load_env

logger = logging.getLogger("capital.poller")

POLL_INTERVAL_S = int(os.environ.get("CAPITAL_POLL_INTERVAL_S", "60"))

# Capital.com epic codes for the 10 seeded instruments
EPIC_MAP = {
    "GOLD":    "CS.D.CFDGOLD.CFD.IP",
    "SILVER":  "CS.D.CFDSILVER.CFD.IP",
    "OIL":     "CS.D.BRENT.CFD.IP",
    "DE40":    "CS.D.DE40.CFD.IP",
    "US500":   "CS.D.US500.CFD.IP",
    "US100":   "CS.D.US100.CFD.IP",
    "EURUSD":  "CS.D.EURUSD.CFD.IP",
    "GBPUSD":  "CS.D.GBPUSD.CFD.IP",
    "USDJPY":  "CS.D.USDJPY.CFD.IP",
    "SOXL":    "CS.D.SOXL.CFD.IP",
}


async def _resolve_instrument_id(conn, symbol: str) -> Optional[int]:
    row = await conn.fetchrow(
        "SELECT id FROM public.instruments WHERE symbol = $1 AND exchange = 'capital' AND is_active = TRUE LIMIT 1",
        symbol,
    )
    return int(row["id"]) if row else None


async def poll_once(adapter: CapitalAdapter) -> dict:
    """Fetch prices for all instruments and insert 1m candles."""
    pool = await get_shared_pool()
    now = datetime.now(timezone.utc)
    results = {"inserted": 0, "errors": 0, "skipped": 0}

    async with pool.acquire() as conn:
        for symbol, epic in EPIC_MAP.items():
            try:
                inst_id = await _resolve_instrument_id(conn, symbol)
                if inst_id is None:
                    results["skipped"] += 1
                    continue

                prices = await adapter.fetch_ohlcv(epic, "1m", limit=1)
                if not prices:
                    results["skipped"] += 1
                    continue

                candle = prices[-1]  # latest candle
                ts = datetime.fromtimestamp(candle.timestamp / 1000, tz=timezone.utc)

                await conn.execute(
                    """INSERT INTO public.candles
                       (instrument_id, timeframe, timestamp, open, high, low, close, volume)
                       VALUES ($1, '1m', $2, $3, $4, $5, $6, $7)
                       ON CONFLICT (instrument_id, timeframe, timestamp) DO UPDATE
                       SET open = EXCLUDED.open, high = EXCLUDED.high,
                           low = EXCLUDED.low, close = EXCLUDED.close,
                           volume = EXCLUDED.volume""",
                    inst_id, ts,
                    Decimal(str(candle.open)), Decimal(str(candle.high)),
                    Decimal(str(candle.low)), Decimal(str(candle.close)),
                    Decimal(str(candle.volume)),
                )
                results["inserted"] += 1

            except Exception as exc:
                logger.warning("Capital poll failed for %s: %s", symbol, exc)
                results["errors"] += 1

    return results


async def run_forever():
    load_env()
    env = os.environ.get("CAPITAL_ENV", "demo")
    email = os.environ.get("CAPITAL_EMAIL", "")
    password = os.environ.get("CAPITAL_PASSWORD", "")
    api_key = os.environ.get("CAPITAL_API_KEY", "")

    adapter = CapitalAdapter(environment=env)
    await adapter.authenticate(email, password, api_key)
    logger.info("Capital.com poller authenticated (env=%s)", env)

    stop = asyncio.Event()

    def _handle_signal():
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    logger.info("Capital.com poller running (interval=%ds, %d instruments)",
                POLL_INTERVAL_S, len(EPIC_MAP))

    while not stop.is_set():
        try:
            results = await poll_once(adapter)
            logger.info("Capital poll: %s", results)
        except Exception as exc:
            logger.exception("Capital poll cycle failed: %s", exc)

        try:
            await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL_S)
        except asyncio.TimeoutError:
            pass

    logger.info("Capital.com poller stopped")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(run_forever())
