"""News collectors — RSS feeds for Tickles V2.

Telegram, Discord, and TradingView collectors live in their own folders
under shared/collectors/ (telegram/, discord/, tradingview/).
"""

from shared.collectors.base import BaseCollector, CollectorConfig, NewsItem, NewsSource
from shared.collectors.news.rss_collector import RSSCollector

__all__ = [
    "BaseCollector",
    "CollectorConfig",
    "NewsItem",
    "NewsSource",
    "RSSCollector",
]
