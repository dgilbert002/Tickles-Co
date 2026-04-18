"""RSS collector for Tickles V2.

Collects news from RSS feeds and writes raw content to tickles_shared.news_items.
No detection logic — just fetch, parse, and store.

Uses aiohttp for async HTTP requests and feedparser for RSS parsing.
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional

import aiohttp
import feedparser

from shared.collectors.base import BaseCollector, CollectorConfig, NewsItem, NewsSource
from shared.utils import config

logger = logging.getLogger("tickles.news.rss")


class RSSCollector(BaseCollector):
    """Collects news from RSS feeds.

    Fetches RSS feeds in parallel, parses entries, and returns NewsItem objects.
    No symbol detection or signal analysis — raw content only.
    """

    DEFAULT_FEEDS = [
        {
            "name": "CNBC World Markets",
            "url": "https://www.cnbc.com/id/100727362/device/rss/rss.html",
            "category": "news"
        },
        {
            "name": "CNBC Economy",
            "url": "https://www.cnbc.com/id/20910258/device/rss/rss.html",
            "category": "news"
        },
        {
            "name": "CNBC Finance",
            "url": "https://www.cnbc.com/id/10000664/device/rss/rss.html",
            "category": "news"
        },
        {
            "name": "Yahoo Finance",
            "url": "https://finance.yahoo.com/news/rssindex",
            "category": "news"
        },
        {
            "name": "Google News: Forex & Gold",
            "url": "https://news.google.com/rss/search?q=forex+gold+XAUUSD+trading&hl=en-US&gl=US&ceid=US:en",
            "category": "news"
        },
        {
            "name": "Google News: Fed & Rates",
            "url": "https://news.google.com/rss/search?q=federal+reserve+interest+rate+inflation&hl=en-US&gl=US&ceid=US:en",
            "category": "central_bank"
        },
        {
            "name": "Google News: Gold Price",
            "url": "https://news.google.com/rss/search?q=gold+price+precious+metals&hl=en-US&gl=US&ceid=US:en",
            "category": "market_data"
        },
        {
            "name": "FXStreet News",
            "url": "https://www.fxstreet.com/rss/news",
            "category": "news"
        },
        {
            "name": "Investing.com News",
            "url": "https://www.investing.com/rss/news.rss",
            "category": "news"
        },
    ]

    def __init__(self, config: Optional[CollectorConfig] = None):
        """Initialize RSS collector.

        Args:
            config: Optional CollectorConfig with custom feeds.
        """
        super().__init__("rss", config)
        self._feeds: List[Dict[str, str]] = list(self.DEFAULT_FEEDS)

        # If config has channels, use them as feeds
        if config and config.channels:
            self._feeds = []
            for ch in config.channels:
                self._feeds.append({
                    "name": ch.get("name", "Custom Feed"),
                    "url": ch.get("url", ch.get("id", "")),
                    "category": ch.get("category", "news"),
                })

    def add_feed(self, name: str, url: str, category: str = "news") -> None:
        """Add a custom RSS feed.

        Args:
            name: Human-readable feed name.
            url: RSS feed URL.
            category: Feed category.
        """
        self._feeds.append({
            "name": name,
            "url": url,
            "category": category,
        })
        logger.info("Added RSS feed: %s", name)

    def remove_feed(self, name: str) -> None:
        """Remove a feed by name.

        Args:
            name: Feed name to remove.
        """
        self._feeds = [f for f in self._feeds if f["name"] != name]

    async def collect(self) -> List[NewsItem]:
        """Collect news from all configured feeds in parallel.

        Returns:
            List of NewsItem objects.
        """
        async def _safe_collect(feed: Dict[str, str]) -> List[NewsItem]:
            try:
                return await self._collect_feed(feed)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.error("Error collecting from %s: %s", feed["name"], e)
                self.errors += 1
                return []

        results = await asyncio.gather(
            *[_safe_collect(f) for f in self._feeds],
            return_exceptions=False,
        )

        all_items: List[NewsItem] = []
        for items in results:
            all_items.extend(items)

        logger.info("RSS: collected %d total items from %d feeds", len(all_items), len(self._feeds))

        return all_items

    async def _collect_feed(self, feed: Dict[str, str]) -> List[NewsItem]:
        """Parse an RSS feed and return NewsItems.

        Args:
            feed: Feed dict with 'name', 'url', 'category'.

        Returns:
            List of NewsItem objects.
        """
        items: List[NewsItem] = []
        try:
            headers = {
                "User-Agent": config.RSS_USER_AGENT,
            }
            timeout = aiohttp.ClientTimeout(total=config.RSS_FETCH_TIMEOUT)
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(feed["url"], timeout=timeout) as resp:
                    if resp.status != 200:
                        logger.warning("RSS feed %s returned %d", feed["name"], resp.status)
                        return []

                    content = await resp.text()

            parsed = feedparser.parse(content)
            if parsed.entries:
                for entry in parsed.entries:
                    headline = entry.get("title", "")
                    summary = entry.get("summary", entry.get("description", ""))
                    link = entry.get("link", "")
                    pub_date_raw = entry.get("published", entry.get("updated", ""))
                    author = entry.get("author", "")

                    pub_date = None
                    try:
                        if hasattr(entry, "published_parsed") and entry.published_parsed:
                            pub_date = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                        elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                            pub_date = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
                        elif pub_date_raw:
                            dt = parsedate_to_datetime(pub_date_raw)
                            if dt.tzinfo is None:
                                pub_date = dt.replace(tzinfo=timezone.utc)
                            else:
                                pub_date = dt.astimezone(timezone.utc)
                    except (ValueError, TypeError):
                        logger.warning("Could not parse date: %s", pub_date_raw)
                        pub_date = None

                    item = NewsItem(
                        source=NewsSource.RSS,
                        author=author,
                        headline=headline,
                        content=self._strip_html(summary),
                        url=link,
                        published_at=pub_date,
                    )
                    item.compute_hash()
                    items.append(item)

        except asyncio.TimeoutError:
            logger.warning("RSS feed %s timed out", feed["name"])
        except aiohttp.ClientError as e:
            logger.error("RSS client error for %s: %s", feed["name"], e)

        return items

    @staticmethod
    def _strip_html(text: str) -> str:
        """Remove HTML tags from text.

        Args:
            text: HTML text.

        Returns:
            Plain text with HTML tags removed.
        """
        clean = re.sub(r"<[^>]+>", "", text)
        clean = re.sub(r"\s+", " ", clean).strip()
        return clean
