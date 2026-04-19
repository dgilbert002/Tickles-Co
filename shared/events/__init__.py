"""Phase 30 — Events Calendar package."""
from __future__ import annotations

from pathlib import Path

from shared.events.memory_pool import InMemoryEventsPool
from shared.events.protocol import (
    EventRecord,
    EventWindow,
    IMPORTANCE_HIGH,
    IMPORTANCE_LOW,
    IMPORTANCE_MEDIUM,
    KIND_CUSTOM,
    KIND_EARNINGS,
    KIND_FUNDING_ROLL,
    KIND_HALVING,
    KIND_MACRO,
    KIND_MAINTENANCE,
    KIND_REGULATORY,
    KINDS,
    build_dedupe_key,
)
from shared.events.service import EventsCalendarService
from shared.events.store import EventsStore

MIGRATION_PATH = Path(__file__).parent / "migrations" / "2026_04_19_phase30_events.sql"


def read_migration_sql() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


__all__ = [
    "EventRecord",
    "EventWindow",
    "EventsStore",
    "EventsCalendarService",
    "InMemoryEventsPool",
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
    "MIGRATION_PATH",
    "build_dedupe_key",
    "read_migration_sql",
]
