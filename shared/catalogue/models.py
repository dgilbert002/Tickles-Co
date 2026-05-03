"""
Module: models
Purpose: Dataclasses for the Collector Catalogue configuration system.
Location: /opt/tickles/shared/catalogue/models.py
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class CollectorSource:
    """A registered data source (Discord server, Telegram, RSS, etc.)."""

    id: Optional[int] = None
    source_type: str = ""
    source_name: str = ""
    source_slug: str = ""
    connection_config: Dict[str, Any] = field(default_factory=dict)
    is_enabled: bool = True
    description: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @property
    def display_name(self) -> str:
        return f"{self.source_name} ({self.source_type})"


@dataclass
class WatchedChannel:
    """A channel within a source with collection configuration."""

    id: Optional[int] = None
    catalog_id: Optional[int] = None
    channel_slug: str = ""
    channel_name: str = ""
    platform_channel_id: Optional[str] = None
    is_enabled: bool = True
    collect_text: bool = True
    collect_images: bool = True
    collect_charts: bool = True
    collect_videos: bool = False
    instrument_patterns: List[str] = field(default_factory=list)
    epic_mappings: Dict[str, str] = field(default_factory=dict)
    filter_keywords: List[str] = field(default_factory=list)
    filter_regex: Optional[str] = None
    max_history_messages: int = 1000
    poll_interval_seconds: int = 60
    description: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @property
    def display_name(self) -> str:
        return f"#{self.channel_name}"


@dataclass
class WatchedUser:
    """A user/trader being monitored within a channel."""

    id: Optional[int] = None
    channel_id: Optional[int] = None
    trader_profile_id: Optional[int] = None
    platform_user_id: Optional[str] = None
    platform_handle: str = ""
    display_name: str = ""
    is_enabled: bool = True
    track_trades: bool = True
    track_charts: bool = True
    track_commentary: bool = True
    track_advice: bool = True
    track_media: bool = True
    auto_detect_entries: bool = True
    auto_detect_sl_tp: bool = True
    min_confidence_threshold: float = 0.6
    notes: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @property
    def display_name_full(self) -> str:
        parts = [self.display_name or self.platform_handle]
        if self.platform_handle and self.platform_handle != self.display_name:
            parts.append(f"(@{self.platform_handle})")
        return " ".join(parts)


@dataclass
class SourceHierarchy:
    """Nested view: Source -> Channels -> Users."""

    source: CollectorSource
    channels: List["ChannelHierarchy"] = field(default_factory=list)


@dataclass
class ChannelHierarchy:
    """Channel with its watched users."""

    channel: WatchedChannel
    users: List[WatchedUser] = field(default_factory=list)
