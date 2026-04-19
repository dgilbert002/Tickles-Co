"""
Module: run_all_collectors
Purpose: Unified service runner for all data collectors (market data + news)
Location: /opt/tickles/shared/services/run_all_collectors.py
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Dict, List, Any

from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from shared.connectors.ccxt_adapter import CCXTAdapter
from shared.connectors.capital_adapter import CapitalAdapter
from shared.connectors.base import BaseExchangeAdapter
from shared.market_data.candle_service import CandleService
from shared.market_data.gap_detector import GapDetector
from shared.market_data.retention import RetentionManager
from shared.collectors.news.rss_collector import RSSCollector
from shared.collectors.telegram.telegram_collector import TelegramCollector
from shared.collectors.telegram.tradingview_monitor import TradingViewMonitor
from shared.collectors.base import BaseCollector
from shared.utils.db import DatabasePool
from shared.utils import config

logger = logging.getLogger("tickles.runners")

# Exchange configuration
EXCHANGE_CONFIGS = {
    "bybit": {
        "type": "ccxt",
        "sandbox": config.BYBIT_SANDBOX,
        "api_key": config.BYBIT_API_KEY,
        "api_secret": config.BYBIT_API_SECRET,
    },
    "blofin": {
        "type": "ccxt",
        "sandbox": False,
        "api_key": config.BLOFIN_API_KEY,
        "api_secret": config.BLOFIN_API_SECRET,
    },
    "bitget": {
        "type": "ccxt",
        "sandbox": False,
        "api_key": os.environ.get("BITGET_API_KEY", ""),
        "api_secret": os.environ.get("BITGET_API_SECRET", ""),
    },
    "capitalcom": {
        "type": "capital",
        "environment": "demo",
        "email": os.environ.get("email", ""),
        "password": os.environ.get("password", ""),
        "api_key": os.environ.get("api_key", ""),
    },
}

# Symbols and timeframes to collect
COLLECTION_SYMBOLS = os.getenv(
    "COLLECTION_SYMBOLS", "BTC/USDT,ETH/USDT,SOL/USDT"
).split(",")
COLLECTION_TIMEFRAMES = os.getenv(
    "COLLECTION_TIMEFRAMES", "1m,5m,15m,1h,4h"
).split(",")


async def initialize_adapters() -> Dict[str, BaseExchangeAdapter]:
    """Initialize all configured exchange adapters.

    Returns:
        Dict mapping exchange name to adapter instance.
    """
    adapters: Dict[str, BaseExchangeAdapter] = {}

    for exchange_name, cfg in EXCHANGE_CONFIGS.items():
        try:
            if cfg["type"] == "ccxt":
                adapter = CCXTAdapter(
                    exchange_id=exchange_name,
                    use_sandbox=cfg.get("sandbox", False),
                    config={
                        "apiKey": cfg.get("api_key", ""),
                        "secret": cfg.get("api_secret", ""),
                    },
                )
                adapters[exchange_name] = adapter
                logger.info("Initialized CCXT adapter for %s", exchange_name)

            elif cfg["type"] == "capital":
                adapter = CapitalAdapter(environment=cfg.get("environment", "demo"))
                email = cfg.get("email", "")
                password = cfg.get("password", "")
                api_key = cfg.get("api_key", "")

                if email and password and api_key:
                    auth_success = await adapter.authenticate(email, password, api_key)
                    if auth_success:
                        adapters[exchange_name] = adapter
                        logger.info(
                            "Initialized Capital.com adapter (%s)",
                            cfg.get("environment", "demo"),
                        )
                    else:
                        logger.warning(
                            "Capital.com authentication failed, skipping"
                        )
                else:
                    logger.warning(
                        "Capital.com credentials missing, skipping"
                    )

        except Exception as e:
            logger.error(
                "Failed to initialize adapter for %s: %s",
                exchange_name,
                e,
                exc_info=True,
            )

    return adapters


async def _resolve_instrument_ids(
    db_pool: DatabasePool, adapters: Dict[str, BaseExchangeAdapter]
) -> Dict[str, Dict[str, int]]:
    """Resolve instrument IDs for all exchange/symbol combinations.

    Args:
        db_pool: Database connection pool
        adapters: Dict of exchange name to adapter

    Returns:
        Nested dict: {exchange: {symbol: instrument_id}}
    """
    resolved: Dict[str, Dict[str, int]] = {}

    query = (
        "SELECT id, exchange, symbol FROM tickles_shared.instruments"
    )
    rows = await db_pool.fetch_all(query)

    for row in rows:
        exchange = row["exchange"]
        symbol = row["symbol"]
        instrument_id = row["id"]

        if exchange not in resolved:
            resolved[exchange] = {}
        resolved[exchange][symbol] = instrument_id

    return resolved


async def run_candle_collection(
    db_pool: DatabasePool, adapters: Dict[str, BaseExchangeAdapter]
) -> Dict[str, Any]:
    """Run candle collection for all exchanges, symbols, and timeframes.

    Args:
        db_pool: Database connection pool
        adapters: Dict of exchange name to adapter

    Returns:
        Summary dict with collection stats.
    """
    service = CandleService(db_pool, adapters)
    results: Dict[str, Any] = {
        "symbols_processed": 0,
        "candles_inserted": 0,
        "errors": 0,
    }

    for exchange_name, adapter in adapters.items():
        if exchange_name == "capitalcom":
            logger.info(
                "Skipping Capital.com for candle collection (needs epic mapping)"
            )
            continue

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
                    results["candles_inserted"] += count
                    results["symbols_processed"] += 1

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
                    results["errors"] += 1
                    logger.error(
                        "[%s] Failed to collect %s/%s/%s: %s",
                        exchange_name,
                        exchange_name,
                        symbol,
                        timeframe,
                        str(e),
                    )

    return results


async def run_gap_detection(
    db_pool: DatabasePool, adapters: Dict[str, BaseExchangeAdapter]
) -> Dict[str, Any]:
    """Run gap detection on collected candles.

    Args:
        db_pool: Database connection pool
        adapters: Dict of exchange name to adapter

    Returns:
        Summary dict with gap detection stats.
    """
    if not config.GAP_DETECTION_ENABLED:
        logger.info("Gap detection disabled, skipping")
        return {"gaps_found": 0, "gaps_filled": 0}

    gap_detector = GapDetector(db_pool)
    results = {"gaps_found": 0, "gaps_filled": 0}

    instrument_map = await _resolve_instrument_ids(db_pool, adapters)

    try:
        for exchange_name in adapters:
            if exchange_name not in instrument_map:
                continue

            exchange_symbols = instrument_map[exchange_name]

            for symbol in COLLECTION_SYMBOLS:
                symbol = symbol.strip()
                if symbol not in exchange_symbols:
                    continue

                instrument_id = exchange_symbols[symbol]

                for timeframe in COLLECTION_TIMEFRAMES:
                    timeframe = timeframe.strip()
                    if not timeframe:
                        continue

                    try:
                        date_range = await gap_detector.get_date_range(
                            instrument_id, timeframe
                        )

                        if date_range["count"] == 0:
                            continue

                        earliest = date_range["earliest"]
                        latest = date_range["latest"]

                        if earliest and latest:
                            gaps = await gap_detector.find_gaps(
                                instrument_id, timeframe, earliest, latest
                            )
                            results["gaps_found"] += len(gaps)

                            if gaps:
                                logger.info(
                                    "[%s] Found %d gaps for %s/%s",
                                    exchange_name,
                                    len(gaps),
                                    symbol,
                                    timeframe,
                                )

                    except Exception as e:
                        logger.warning(
                            "Gap detection failed for %s/%s/%s: %s",
                            exchange_name,
                            symbol,
                            timeframe,
                            e,
                        )

    except Exception as e:
        logger.error("Gap detection failed: %s", e, exc_info=True)

    return results


async def run_retention(db_pool: DatabasePool) -> Dict[str, Any]:
    """Apply retention policy to candles.

    Args:
        db_pool: Database connection pool

    Returns:
        Summary dict with retention stats.
    """
    retention = RetentionManager(db_pool)
    results = {"partitions_dropped": 0}

    try:
        await retention.ensure_partitions()
        await retention.drop_expired_partitions()
        results["partitions_dropped"] = 1
        logger.info("Retention policy applied successfully")
    except Exception as e:
        logger.error("Retention failed: %s", e, exc_info=True)

    return results


async def run_news_collection(db_pool: DatabasePool) -> Dict[str, Any]:
    """Run news collection from all configured sources.

    Note: DiscordCollectorService runs as a separate long-lived service
    that writes directly to the database. It is not included here.

    Args:
        db_pool: Database connection pool

    Returns:
        Summary dict with news collection stats.
    """
    collectors: List[BaseCollector] = [
        RSSCollector(),
        TelegramCollector(),
        TradingViewMonitor(),
    ]

    results = {"items_inserted": 0, "collectors_failed": 0}

    for collector in collectors:
        try:
            logger.info("Running news collector: %s", collector.name)
            items = await collector.collect()

            if items:
                inserted = await collector.write_to_db(items, db_pool)
                results["items_inserted"] += inserted
                logger.info(
                    "Collector %s: %d items collected, %d new inserted",
                    collector.name,
                    len(items),
                    inserted,
                )
            else:
                logger.info("Collector %s: no new items", collector.name)

        except Exception as e:
            results["collectors_failed"] += 1
            logger.error(
                "Collector %s failed: %s",
                collector.name,
                str(e),
                exc_info=True,
            )

    return results


async def main() -> None:
    """Main entry point for unified data collection."""
    load_dotenv()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    logger.info("=" * 60)
    logger.info("Tickles V2 — Unified Data Collection")
    logger.info("Started at: %s", datetime.now(timezone.utc).isoformat())
    logger.info("=" * 60)

    # Initialize database
    db_pool = DatabasePool()
    await db_pool.initialize()
    logger.info("Database pool initialized")

    adapters: Dict[str, BaseExchangeAdapter] = {}
    all_results: Dict[str, Any] = {}

    try:
        # Step 1: Initialize exchange adapters
        logger.info("Initializing exchange adapters...")
        adapters = await initialize_adapters()
        logger.info("Initialized %d exchange adapters", len(adapters))

        if not adapters:
            logger.warning("No exchange adapters available, skipping market data")
        else:
            # Step 2: Collect candles
            logger.info("Running candle collection...")
            candle_results = await run_candle_collection(db_pool, adapters)
            all_results["candles"] = candle_results
            logger.info(
                "Candle collection: %d symbols, %d candles, %d errors",
                candle_results["symbols_processed"],
                candle_results["candles_inserted"],
                candle_results["errors"],
            )

            # Step 3: Detect and fill gaps
            logger.info("Running gap detection...")
            gap_results = await run_gap_detection(db_pool, adapters)
            all_results["gaps"] = gap_results
            logger.info(
                "Gap detection: %d gaps found, %d filled",
                gap_results["gaps_found"],
                gap_results["gaps_filled"],
            )

            # Step 4: Apply retention policy
            logger.info("Applying retention policy...")
            retention_results = await run_retention(db_pool)
            all_results["retention"] = retention_results

        # Step 5: Collect news
        logger.info("Running news collection...")
        news_results = await run_news_collection(db_pool)
        all_results["news"] = news_results
        logger.info(
            "News collection: %d items inserted, %d collectors failed",
            news_results["items_inserted"],
            news_results["collectors_failed"],
        )

        # Summary
        logger.info("=" * 60)
        logger.info("Collection Summary:")
        logger.info(
            "  Candles: %d inserted",
            all_results.get("candles", {}).get("candles_inserted", 0),
        )
        logger.info(
            "  Gaps: %d found, %d filled",
            all_results.get("gaps", {}).get("gaps_found", 0),
            all_results.get("gaps", {}).get("gaps_filled", 0),
        )
        logger.info(
            "  Retention: partitions checked",
        )
        logger.info(
            "  News: %d items inserted",
            all_results.get("news", {}).get("items_inserted", 0),
        )
        logger.info("=" * 60)

    finally:
        # Close all adapters
        for name, adapter in adapters.items():
            try:
                await adapter.close()
                logger.info("Closed adapter for %s", name)
            except Exception as e:
                logger.warning("Error closing adapter for %s: %s", name, e)

        # Close database
        await db_pool.close()
        logger.info("Database pool closed")

        logger.info(
            "Collection completed at: %s",
            datetime.now(timezone.utc).isoformat(),
        )


if __name__ == "__main__":
    asyncio.run(main())
