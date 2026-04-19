"""Phase 30 — Events Calendar tests."""
from __future__ import annotations

import asyncio
import io
import json
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from typing import Any, List

from shared.cli import events_cli
from shared.events import (
    EventRecord,
    EventsCalendarService,
    EventsStore,
    IMPORTANCE_HIGH,
    IMPORTANCE_LOW,
    IMPORTANCE_MEDIUM,
    InMemoryEventsPool,
    KIND_MACRO,
    KIND_MAINTENANCE,
    MIGRATION_PATH,
    build_dedupe_key,
    read_migration_sql,
)
from shared.services.registry import SERVICE_REGISTRY, register_builtin_services


def _run(coro):
    return asyncio.run(coro)


def _ev(**over) -> EventRecord:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    kw: dict[str, Any] = dict(
        id=None, kind=KIND_MACRO, provider="fred", name="US CPI YoY",
        event_time=now, window_before_minutes=30, window_after_minutes=30,
        country="US", importance=IMPORTANCE_HIGH,
    )
    kw.update(over)
    return EventRecord(**kw)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_migration_has_expected_objects() -> None:
    assert MIGRATION_PATH.exists()
    sql = read_migration_sql()
    for obj in (
        "public.events_calendar",
        "public.events_active",
        "public.events_upcoming",
        "UNIQUE (provider, dedupe_key)",
    ):
        assert obj in sql


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


def test_build_dedupe_key_is_deterministic() -> None:
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    a = build_dedupe_key("macro", "US CPI YoY", t)
    b = build_dedupe_key("macro", "us cpi yoy", t)
    c = build_dedupe_key("macro", "US CPI YoY", t, symbol="BTC")
    assert a == b
    assert a != c


def test_event_window_contains_event_time() -> None:
    ev = _ev()
    w = ev.window()
    assert w.contains(ev.event_time)
    assert w.contains(ev.event_time - timedelta(minutes=30))
    assert w.contains(ev.event_time + timedelta(minutes=30))
    assert not w.contains(ev.event_time + timedelta(minutes=31))


def test_event_is_active_at_range() -> None:
    ev = _ev()
    t = ev.event_time
    assert ev.is_active_at(t)
    assert ev.is_active_at(t - timedelta(minutes=15))
    assert not ev.is_active_at(t + timedelta(hours=2))


def test_event_record_to_dict_isoformat_event_time() -> None:
    ev = _ev()
    d = ev.to_dict()
    assert d["event_time"] == ev.event_time.isoformat()
    assert d["kind"] == KIND_MACRO


# ---------------------------------------------------------------------------
# Store + InMemory pool
# ---------------------------------------------------------------------------


def test_store_upsert_event_sets_id_and_dedupes_on_provider_key() -> None:
    pool = InMemoryEventsPool()
    store = EventsStore(pool)

    async def go() -> None:
        rid1 = await store.upsert_event(_ev())
        rid2 = await store.upsert_event(_ev())  # same dedupe_key
        assert rid1 > 0
        assert rid1 == rid2
        rows = await store.list_events(kind=KIND_MACRO)
        assert len(rows) == 1

    _run(go())


def test_store_upsert_updates_mutable_fields() -> None:
    pool = InMemoryEventsPool()
    store = EventsStore(pool)

    async def go() -> None:
        await store.upsert_event(_ev(window_before_minutes=10))
        await store.upsert_event(_ev(window_before_minutes=60))
        rows = await store.list_events(kind=KIND_MACRO)
        assert rows[0].window_before_minutes == 60

    _run(go())


def test_store_delete_event() -> None:
    pool = InMemoryEventsPool()
    store = EventsStore(pool)

    async def go() -> None:
        rid = await store.upsert_event(_ev())
        n = await store.delete_event(rid)
        assert n == 1
        rows = await store.list_events(kind=KIND_MACRO)
        assert rows == []

    _run(go())


def test_store_list_filters_kind_symbol_importance_and_time() -> None:
    pool = InMemoryEventsPool()
    store = EventsStore(pool)

    async def go() -> None:
        t = datetime(2026, 2, 1, tzinfo=timezone.utc)
        await store.upsert_event(_ev(
            kind=KIND_MACRO, event_time=t, importance=IMPORTANCE_HIGH,
        ))
        await store.upsert_event(_ev(
            kind=KIND_MAINTENANCE, provider="binance",
            name="binance BTCUSDT maint", exchange="binance", symbol="BTC/USDT",
            event_time=t + timedelta(days=1), importance=IMPORTANCE_LOW,
        ))

        macros = await store.list_events(kind=KIND_MACRO)
        assert len(macros) == 1

        btc = await store.list_events(symbol="BTC/USDT")
        assert len(btc) == 1
        assert btc[0].kind == KIND_MAINTENANCE

        high = await store.list_events(min_importance=IMPORTANCE_HIGH)
        assert len(high) == 1

        future = await store.list_events(
            since=t + timedelta(hours=6), until=t + timedelta(days=2),
        )
        assert len(future) == 1
        assert future[0].kind == KIND_MAINTENANCE

    _run(go())


def test_store_list_active_and_upcoming() -> None:
    pool = InMemoryEventsPool()
    store = EventsStore(pool)
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    async def go() -> None:
        # Active — window overlaps now.
        await store.upsert_event(_ev(
            event_time=now, window_before_minutes=10, window_after_minutes=10,
        ))
        # Far-future — upcoming but not active.
        await store.upsert_event(_ev(
            name="US NFP", event_time=now + timedelta(days=3),
        ))
        active = await store.list_active(now=now)
        assert len(active) == 1
        upc = await store.list_upcoming(now=now, horizon_days=7)
        assert len(upc) == 2

    _run(go())


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def test_service_upsert_many_returns_count() -> None:
    pool = InMemoryEventsPool()
    svc = EventsCalendarService(EventsStore(pool))

    async def go() -> None:
        events = [
            _ev(name=f"evt{i}", event_time=datetime(2026, 2, 1, tzinfo=timezone.utc)
                + timedelta(days=i))
            for i in range(3)
        ]
        n = await svc.upsert_many(events)
        assert n == 3

    _run(go())


def test_service_active_windows_returns_only_covering_intervals() -> None:
    pool = InMemoryEventsPool()
    svc = EventsCalendarService(EventsStore(pool))
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    async def go() -> None:
        await svc.upsert(_ev(event_time=now))
        await svc.upsert(_ev(
            name="stale", event_time=now - timedelta(hours=2),
        ))
        windows = await svc.active_windows(when=now)
        assert len(windows) == 1
        assert windows[0].contains(now)

    _run(go())


def test_service_any_active_respects_scope_and_importance() -> None:
    pool = InMemoryEventsPool()
    svc = EventsCalendarService(EventsStore(pool))
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    async def go() -> None:
        await svc.upsert(_ev(
            kind=KIND_MAINTENANCE, provider="binance",
            name="binance BTC maint",
            exchange="binance", symbol="BTC/USDT",
            event_time=now, importance=IMPORTANCE_MEDIUM,
        ))
        await svc.upsert(_ev(
            kind=KIND_MACRO, provider="fred", name="US CPI",
            event_time=now, importance=IMPORTANCE_HIGH,
        ))

        btc = await svc.any_active(
            when=now, exchange="binance", symbol="BTC/USDT",
        )
        assert len(btc) == 2  # macro is global-scope; included

        other_symbol = await svc.any_active(
            when=now, exchange="binance", symbol="ETH/USDT",
        )
        # Global macro still applies; binance maintenance is BTC-scoped only.
        assert len(other_symbol) == 1
        assert other_symbol[0].kind == KIND_MACRO

        high_only = await svc.any_active(when=now, min_importance=IMPORTANCE_HIGH)
        assert len(high_only) == 1

    _run(go())


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_events_calendar_service_is_registered() -> None:
    register_builtin_services()
    assert "events-calendar" in SERVICE_REGISTRY
    desc = SERVICE_REGISTRY.get("events-calendar")
    assert desc.tags.get("phase") == "30"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _run_cli(argv: List[str]) -> Any:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = events_cli.main(argv)
    out = buf.getvalue().strip()
    assert code == 0, f"cli exit={code}; stdout={out!r}"
    return json.loads(out)


def test_cli_apply_migration_path_only() -> None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = events_cli.main(["apply-migration", "--path-only"])
    assert code == 0
    assert buf.getvalue().strip() == str(MIGRATION_PATH)


def test_cli_kinds_lists_canonical_set() -> None:
    data = _run_cli(["kinds"])
    kinds = set(data["kinds"])
    assert {"macro", "earnings", "maintenance", "funding_roll",
            "halving", "regulatory", "custom"} <= kinds


def test_cli_add_event_in_memory() -> None:
    t = datetime(2026, 3, 1, tzinfo=timezone.utc).isoformat()
    data = _run_cli([
        "add", "--in-memory",
        "--kind", "macro", "--provider", "fred",
        "--name", "US CPI", "--event-time", t,
        "--window-before", "30", "--window-after", "60",
        "--country", "US", "--importance", "3",
    ])
    assert data["ok"] is True
    assert data["id"] >= 1
    assert data["event"]["window_before_minutes"] == 30


def test_cli_list_empty_in_memory() -> None:
    data = _run_cli(["list", "--in-memory"])
    assert data["ok"] is True
    assert data["count"] == 0


def test_cli_active_empty_in_memory() -> None:
    data = _run_cli(["active", "--in-memory"])
    assert data["ok"] is True
    assert data["count"] == 0


def test_cli_upcoming_empty_in_memory() -> None:
    data = _run_cli(["upcoming", "--in-memory", "--horizon-days", "3"])
    assert data["ok"] is True
    assert data["count"] == 0
