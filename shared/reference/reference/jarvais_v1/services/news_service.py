"""
JarvAIs News Collection Background Service
Runs every 60 seconds, collects news from all sources, stores in MySQL.
"""

import asyncio
import json
import logging
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger("jarvais.news_service")


class NewsService:
    """Background service that collects news every 60 seconds and stores in DB."""

    def __init__(self, interval_seconds: int = 60):
        self.interval = interval_seconds
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_run: Optional[datetime] = None
        self._last_count: int = 0
        self._total_collected: int = 0
        self._errors: int = 0

    def start(self):
        """Start the background news collection thread."""
        if self._running:
            logger.warning("News service already running")
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="news-collector")
        self._thread.start()
        logger.info(f"News service started (interval={self.interval}s)")

    def stop(self):
        """Stop the background news collection."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("News service stopped")

    def _run_loop(self):
        """Main collection loop."""
        # Initial collection immediately
        self._collect_once()
        while self._running:
            time.sleep(self.interval)
            if self._running:
                self._collect_once()

    def _collect_once(self):
        """Run one collection cycle."""
        try:
            from data.collectors import get_collector_registry
            from db.database import get_db

            db = get_db()
            registry = get_collector_registry()

            # Run the async collector in a new event loop
            loop = asyncio.new_event_loop()
            try:
                items = loop.run_until_complete(registry.collect_all())
            finally:
                loop.close()

            if not items:
                logger.debug("No news items collected this cycle")
                self._last_run = datetime.now()
                return

            # Store each item in the database
            new_count = 0
            for item in items:
                # StandardizedItem is a dataclass, access via attributes
                title = getattr(item, 'title', '') or ''
                body = getattr(item, 'body', '') or getattr(item, 'summary', '') or ''
                url = getattr(item, 'url', '') or ''
                source = getattr(item, 'source', 'unknown') or 'unknown'
                symbols = getattr(item, 'symbols', []) or []
                sentiment_score = getattr(item, 'sentiment_score', 0.0) or 0.0
                
                # Determine sentiment from score
                if sentiment_score > 0.2:
                    sentiment = 'bullish'
                elif sentiment_score < -0.2:
                    sentiment = 'bearish'
                else:
                    sentiment = 'neutral'
                
                db_item = {
                    "source": source,
                    "source_detail": getattr(item, 'source_detail', '') or '',
                    "title": title,
                    "body": body if body else '',
                    "url": url,
                    "category": self._classify_category_from_item(item),
                    "relevance": getattr(item, 'relevance', 'low') or 'low',
                    "sentiment": sentiment,
                    "sentiment_score": sentiment_score,
                    "symbols": symbols if isinstance(symbols, list) else [],
                    "published_at": getattr(item, 'timestamp', None),
                }
                if db.store_news_item(db_item):
                    new_count += 1

            self._last_run = datetime.now()
            self._last_count = new_count
            self._total_collected += new_count

            if new_count > 0:
                logger.info(f"News collection: {new_count} new items from {len(items)} total")
            else:
                logger.debug(f"News collection: 0 new items ({len(items)} checked, all duplicates)")

        except Exception as e:
            self._errors += 1
            logger.error(f"News collection error: {e}")

    def _classify_category_from_item(self, item) -> str:
        """Classify a StandardizedItem into a category."""
        title = (getattr(item, 'title', '') or '').lower()
        source = (getattr(item, 'source', '') or '').lower()
        category = (getattr(item, 'category', '') or '').lower()
        if category and category != 'unknown':
            return category

        if any(k in title for k in ["fed", "rate", "inflation", "cpi", "nfp", "gdp", "fomc"]):
            return "macro"
        if any(k in title for k in ["gold", "xau", "silver", "commodity"]):
            return "commodity"
        if any(k in title for k in ["forex", "eur", "gbp", "usd", "jpy", "currency"]):
            return "forex"
        if any(k in title for k in ["stock", "nasdaq", "s&p", "dow", "equity"]):
            return "equity"
        if any(k in title for k in ["crypto", "bitcoin", "btc", "eth"]):
            return "crypto"
        if "signal" in source or "ea" in source:
            return "signal"
        return "news"

    def get_status(self) -> Dict:
        """Get the service status."""
        return {
            "running": self._running,
            "interval_seconds": self.interval,
            "last_run": self._last_run.isoformat() if self._last_run else None,
            "last_new_items": self._last_count,
            "total_collected": self._total_collected,
            "errors": self._errors,
        }


# Singleton
_news_service: Optional[NewsService] = None


def get_news_service() -> NewsService:
    global _news_service
    if _news_service is None:
        _news_service = NewsService(interval_seconds=60)
    return _news_service


def start_news_service():
    """Convenience function to start the news service."""
    svc = get_news_service()
    svc.start()
    return svc
