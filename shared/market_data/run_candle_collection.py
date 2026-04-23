"""
Module: run_candle_collection
Purpose: Standalone candle collection runner for multi-exchange instruments
Location: /opt/tickles/shared/market_data/run_candle_collection.py
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Dict, List

from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from shared.connectors.ccxt_adapter import CCXTAdapter
from shared.connectors.base import BaseExchangeAdapter
from shared.market_data.candle_service import CandleService
from shared.utils.db import DatabasePool
from shared.utils import config

logger = logging.getLogger("tickles.candle_runner")

# Collection targets from config
COLLECTION_SYMBOLS = os.getenv(
    "COLLECTION_SYMBOLS", "BTC/USDT,ETH/USDT,SOL/USDT"
).split(",")
COLLECTION_TIMEFRAMES = os.getenv(
    "COLLECTION_TIMEFRAMES", "1m,5m,15m,1h,4h"
).split(",")
COLLECTION_EXCHANGES = os.getenv(
    "COLLECTION_EXCHANGES", "bybit,blofin,bitget"
).split(",")


async def _build_adapters() -> Dict[str, BaseExchangeAdapter]:
    """Build exchange adapters from configuration.

    Returns:
        Dict mapping exchange name to adapter instance.
    """
    adapters: Dict[str, BaseExchangeAdapter] = {}

    exchange_configs = {
        "bybit": {
            "api_key": config.BYBIT_API_KEY,
            "api_secret": config.BYBIT_API_SECRET,
            "sandbox": config.BYBIT_SANDBOX,
        },
        "blofin": {
            "api_key": config.BLOFIN_API_KEY,
            "api_secret": config.BLOFIN_API_SECRET,
            "sandbox": False,
        },
        "bitget": {
            "api_key": os.environ.get("BITGET_API_KEY", ""),
            "api_secret": os.environ.get("BITGET_API_SECRET", ""),
            "sandbox": False,
        },
        "capitalcom": {
            "api_key": os.environ.get("CAPITAL_API_KEY", ""),
            "api_secret": "", # Not used by Capital but needed for the loop
            "email": os.environ.get("CAPITAL_EMAIL", ""),
            "password": os.environ.get("CAPITAL_PASSWORD", ""),
            "environment": os.environ.get("CAPITAL_ENV", "demo"),
            "sandbox": True,
        },
    }

    for exchange_name in COLLECTION_EXCHANGES:
        exchange_name = exchange_name.strip()
        if not exchange_name:
            continue

        cfg = exchange_configs.get(exchange_name)
        if not cfg:
            logger.warning("Unknown exchange: %s", exchange_name)
            continue

        try:
            adapter = CCXTAdapter(
                exchange_id=exchange_name,
                use_sandbox=cfg["sandbox"],
                config={
                    "apiKey": cfg["api_key"],
                    "secret": cfg["api_secret"],
                },
            )
            if exchange_name == "capitalcom":
                from shared.connectors.capital_adapter import CapitalAdapter
                adapter = CapitalAdapter(environment=cfg["environment"])
                await adapter.authenticate(cfg["email"], cfg["password"], cfg["api_key"])
                adapters[exchange_name] = adapter
            else:
                adapter = CCXTAdapter(
                    exchange_id=exchange_name,
                    use_sandbox=cfg["sandbox"],
                    config={
                        "apiKey": cfg["api_key"],
                        "secret": cfg["api_secret"],
                    },
                )
                adapters[exchange_name] = adapter
            logger.info("Built adapter for %s", exchange_name)

        except Exception as e:
            logger.error(
                "Failed to build adapter for %s: %s", exchange_name, e
            )

    return adapters


async def run_collection(db_pool: DatabasePool) -> None:
    """Run candle collection for configured exchanges, symbols, and timeframes.

    Args:
        db_pool: Database connection pool
    """
    adapters = await _build_adapters()
    if not adapters:
        logger.error("No exchange adapters available, exiting")
        return

    service = CandleService(db_pool, adapters)

    total_inserted = 0
    total_symbols = 0
    total_errors = 0

    for exchange_name, adapter in adapters.items():
        logger.info("Collecting from exchange: %s", exchange_name)

        for symbol in COLLECTION_SYMBOLS:
            symbol = symbol.strip()
            if not symbol:
                continue

            for timeframe in COLLECTION_TIMEFRAMES:
                timeframe = timeframe.strip()
                if not timeframe:
                    continue

                try:
                    count, last_candle = await service.collect_candles(
                        exchange_name, symbol, timeframe, limit=500
                    )
                    total_inserted += count
                    total_symbols += 1

                    if last_candle:
                        logger.info(
                            "[%s] Collected %d candles for %s/%s (last: %s)",
                            exchange_name,
                            count,
                            symbol,
                            timeframe,
                            last_candle.isoformat(),
                        )
                    else:
                        logger.debug(
                            "[%s] No new candles for %s/%s",
                            exchange_name,
                            symbol,
                            timeframe,
                        )

                except Exception as e:
                    total_errors += 1
                    logger.error(
                        "[%s] Failed to collect %s/%s/%s: %s",
                        exchange_name,
                        exchange_name,
                        symbol,
                        timeframe,
                        str(e),
                    )

    # Close all adapters
    for name, adapter in adapters.items():
        try:
            await adapter.close()
        except Exception as e:
            logger.warning("Error closing adapter %s: %s", name, e)

    logger.info(
        "Collection complete: %d symbols/timeframes processed, "
        "%d new candles inserted, %d errors",
        total_symbols,
        total_inserted,
        total_errors,
    )


async def main() -> None:
    """Main entry point for candle collection."""
    load_dotenv()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Initialize database pool
    db_pool = DatabasePool()
    await db_pool.initialize()

    try:
        await run_collection(db_pool)
    finally:
        await db_pool.close()


if __name__ == "__main__":
    asyncio.run(main())
