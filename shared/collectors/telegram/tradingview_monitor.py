"""
Module: tradingview_monitor
Purpose: Monitor TradingView idea stream for trade ideas and analysis
Location: /opt/tickles/shared/collectors/telegram/tradingview_monitor.py

Fetches recent ideas from TradingView's public API for configured symbols.
No detection logic — just fetch, parse, and store.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp

from shared.collectors.base import BaseCollector, CollectorConfig, NewsItem, NewsSource
from shared.utils import config

logger = logging.getLogger("tickles.news.tradingview")

TRADINGVIEW_IDEA_URL = "https://www.tradingview.com/v2/api/get_ideas/"
TRADINGVIEW_SYMBOLS = [
    "EURUSD", "GBPUSD", "XAUUSD", "BTCUSD", "ETHUSD",
    "NAS100", "US30", "USOIL",
]


class TradingViewMonitor(BaseCollector):
    """Monitors TradingView idea stream for trade ideas and analysis.

    Fetches recent ideas from TradingView's public API for configured symbols.
    Does not require authentication — uses public endpoints.
    No detection logic — raw content only.
    """

    def __init__(self, config: Optional[CollectorConfig] = None):
        """Initialize TradingView monitor.

        Args:
            config: Optional CollectorConfig with custom symbols.
        """
        super().__init__("tradingview", config)
        self._symbols: List[str] = list(TRADINGVIEW_SYMBOLS)

        # If config has channels, use them as symbols
        if config and config.channels:
            self._symbols = []
            for ch in config.channels:
                symbol = ch.get("id", ch.get("name", ""))
                if symbol:
                    self._symbols.append(symbol)

    async def collect(self) -> List[NewsItem]:
        """Collect recent TradingView ideas for configured symbols.

        Returns:
            List of NewsItem objects.
        """
        items: List[NewsItem] = []

        try:
            timeout = aiohttp.ClientTimeout(total=15)
            headers = {
                "User-Agent": config.RSS_USER_AGENT,
                "Referer": "https://www.tradingview.com/",
            }

            async with aiohttp.ClientSession(
                headers=headers, timeout=timeout
            ) as session:
                for symbol in self._symbols:
                    try:
                        symbol_items = await self._fetch_symbol_ideas(session, symbol)
                        items.extend(symbol_items)
                    except Exception as e:
                        logger.warning("Failed to fetch ideas for %s: %s", symbol, e)
                        continue

            logger.info(
                "TradingView: collected %d ideas for %d symbols",
                len(items),
                len(self._symbols),
            )

        except Exception as e:
            logger.error("TradingView monitor failed: %s", e, exc_info=True)
            self.errors += 1

        return items

    async def _fetch_symbol_ideas(
        self, session: aiohttp.ClientSession, symbol: str
    ) -> List[NewsItem]:
        """Fetch TradingView ideas for a single symbol.

        Args:
            session: aiohttp ClientSession.
            symbol: Trading symbol (e.g., 'EURUSD').

        Returns:
            List of NewsItem objects.
        """
        items: List[NewsItem] = []

        params: Dict[str, Any] = {
            "lang": "en",
            "symbol": symbol,
            "sort": "latest",
            "limit": 10,
        }

        try:
            async with session.get(
                TRADINGVIEW_IDEA_URL, params=params
            ) as resp:
                if resp.status != 200:
                    logger.debug("TradingView returned %d for %s", resp.status, symbol)
                    return items

                data = await resp.json()
                ideas = data.get("results", [])

                for idea in ideas:
                    item = self._parse_idea(idea, symbol)
                    if item:
                        items.append(item)

        except aiohttp.ClientError as e:
            logger.warning("Network error fetching ideas for %s: %s", symbol, e)
        except Exception as e:
            logger.warning("Error parsing ideas for %s: %s", symbol, e)

        return items

    def _parse_idea(self, idea: Dict[str, Any], symbol: str) -> Optional[NewsItem]:
        """Parse a TradingView idea into a NewsItem.

        Args:
            idea: Idea data from TradingView API.
            symbol: Symbol the idea is for.

        Returns:
            NewsItem or None if parsing fails.
        """
        try:
            title = idea.get("title", "").strip()
            description = idea.get("description", "").strip()
            content = f"{title}\n\n{description}" if description else title

            if not content or len(content) < 10:
                return None

            author = idea.get("username", "Unknown")
            timestamp = idea.get("published_at", idea.get("created_at", 0))

            pub_date = None
            if timestamp:
                try:
                    pub_date = datetime.fromtimestamp(
                        int(timestamp), tz=timezone.utc
                    )
                except (ValueError, TypeError, OSError):
                    pub_date = datetime.now(timezone.utc)

            item = NewsItem(
                source=NewsSource.TRADINGVIEW,
                headline=title[:200] if title else f"TradingView idea on {symbol}",
                content=content,
                author=author,
                url=f"https://www.tradingview.com/chart/{symbol}/",
                published_at=pub_date,
            )
            item.compute_hash()
            return item

        except Exception as e:
            logger.warning("Failed to parse TradingView idea: %s", e)
            return None
