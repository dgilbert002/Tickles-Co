"""
Module: run_news_collection
Purpose: Standalone news collection runner for RSS, Telegram, and TradingView
Location: /opt/tickles/shared/collectors/news/run_news_collection.py

Note: Discord runs as a separate long-lived service (DiscordCollectorService)
that writes directly to the database. Start it independently with:
    python -m shared.collectors.discord.discord_collector
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from typing import List

from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from shared.collectors.news.rss_collector import RSSCollector
from shared.collectors.telegram.telegram_collector import TelegramCollector
from shared.collectors.telegram.tradingview_monitor import TradingViewMonitor
from shared.collectors.base import BaseCollector, NewsItem
from shared.utils.db import DatabasePool

logger = logging.getLogger("tickles.news_runner")


async def run_collection(db_pool: DatabasePool) -> None:
    """Run news collection from all configured collectors.

    Args:
        db_pool: Database connection pool
    """
    collectors: List[BaseCollector] = [
        RSSCollector(),
        TelegramCollector(),
        TradingViewMonitor(),
    ]

    total_inserted = 0
    total_errors = 0

    for collector in collectors:
        try:
            logger.info("Running collector: %s", collector.name)
            items = await collector.collect()

            if items:
                inserted = await collector.write_to_db(items, db_pool)
                total_inserted += inserted
                logger.info(
                    "Collector %s: %d items collected, %d new inserted",
                    collector.name, len(items), inserted
                )
            else:
                logger.info("Collector %s: no new items", collector.name)

        except Exception as e:
            total_errors += 1
            logger.error(
                "Collector %s failed: %s",
                collector.name, str(e), exc_info=True
            )

    logger.info(
        "News collection complete: %d items inserted, %d collectors failed",
        total_inserted, total_errors
    )


async def main() -> None:
    """Main entry point for news collection."""
    load_dotenv()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
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
