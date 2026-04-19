"""
shared.events.protocol — data types for Phase 30 Events Calendar.

An :class:`EventRecord` is a single row in ``public.events_calendar``. An
:class:`EventWindow` is the [start, end] interval derived from an event
+ its ``window_before_minutes`` / ``window_after_minutes``.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

# Canonical event kinds (freeform strings allowed; these are defaults)
KIND_MACRO = "macro"
KIND_EARNINGS = "earnings"
KIND_MAINTENANCE = "maintenance"
KIND_FUNDING_ROLL = "funding_roll"
KIND_HALVING = "halving"
KIND_REGULATORY = "regulatory"
KIND_CUSTOM = "custom"

KINDS = {
    KIND_MACRO, KIND_EARNINGS, KIND_MAINTENANCE, KIND_FUNDING_ROLL,
    KIND_HALVING, KIND_REGULATORY, KIND_CUSTOM,
}

IMPORTANCE_LOW = 1
IMPORTANCE_MEDIUM = 2
IMPORTANCE_HIGH = 3


def build_dedupe_key(
    kind: str,
    name: str,
    event_time: datetime,
    *,
    symbol: Optional[str] = None,
    exchange: Optional[str] = None,
) -> str:
    """Stable dedupe key: sha256 over normalised fields."""
    # Ensure tz-aware UTC
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=timezone.utc)
    else:
        event_time = event_time.astimezone(timezone.utc)
    material = "|".join([
        kind.lower(),
        (exchange or "").lower(),
        (symbol or "").lower(),
        name.strip().lower(),
        event_time.isoformat(),
    ])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


@dataclass
class EventRecord:
    id: Optional[int]
    kind: str
    provider: str
    name: str
    event_time: datetime
    window_before_minutes: int = 0
    window_after_minutes: int = 0
    universe: Optional[str] = None
    exchange: Optional[str] = None
    symbol: Optional[str] = None
    country: Optional[str] = None
    importance: int = IMPORTANCE_LOW
    payload: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    dedupe_key: Optional[str] = None

    def ensure_dedupe_key(self) -> str:
        if not self.dedupe_key:
            self.dedupe_key = build_dedupe_key(
                self.kind, self.name, self.event_time,
                symbol=self.symbol, exchange=self.exchange,
            )
        return self.dedupe_key

    def window(self) -> "EventWindow":
        start = self.event_time - timedelta(minutes=self.window_before_minutes)
        end = self.event_time + timedelta(minutes=self.window_after_minutes)
        return EventWindow(
            event_id=self.id, kind=self.kind, name=self.name,
            start=start, end=end, event_time=self.event_time,
            importance=self.importance,
            universe=self.universe, exchange=self.exchange, symbol=self.symbol,
        )

    def is_active_at(self, when: datetime) -> bool:
        w = self.window()
        return w.start <= when <= w.end

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["event_time"] = self.event_time.isoformat()
        return d


@dataclass
class EventWindow:
    event_id: Optional[int]
    kind: str
    name: str
    start: datetime
    end: datetime
    event_time: datetime
    importance: int = IMPORTANCE_LOW
    universe: Optional[str] = None
    exchange: Optional[str] = None
    symbol: Optional[str] = None

    def contains(self, when: datetime) -> bool:
        return self.start <= when <= self.end

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "kind": self.kind,
            "name": self.name,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "event_time": self.event_time.isoformat(),
            "importance": self.importance,
            "universe": self.universe,
            "exchange": self.exchange,
            "symbol": self.symbol,
        }


__all__ = [
    "EventRecord",
    "EventWindow",
    "KIND_MACRO",
    "KIND_EARNINGS",
    "KIND_MAINTENANCE",
    "KIND_FUNDING_ROLL",
    "KIND_HALVING",
    "KIND_REGULATORY",
    "KIND_CUSTOM",
    "KINDS",
    "IMPORTANCE_LOW",
    "IMPORTANCE_MEDIUM",
    "IMPORTANCE_HIGH",
    "build_dedupe_key",
]
