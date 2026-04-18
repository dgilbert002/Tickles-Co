"""Tickles V2 Data Collectors.

All collectors inherit from BaseCollector and write raw content to tickles_shared.news_items.
No detection logic — collectors are pure data ingestion services.

Collectors:
- RSSCollector: RSS feeds
- TelegramCollector: Telegram channels/groups (with media download)
- DiscordCollector: Discord channels (with media download)
- TradingViewMonitor: TradingView public ideas

Services:
- MediaExtractor: Unified media download for all collectors
"""

from shared.collectors.base import BaseCollector, CollectorConfig, NewsItem, NewsSource
from shared.collectors.media_extractor import MediaExtractor

__all__ = [
    "BaseCollector",
    "CollectorConfig",
    "MediaExtractor",
    "NewsItem",
    "NewsSource",
]
