"""
Phase-17 unit tests for shared.gateway.

We avoid touching real Redis or real ccxt.pro websockets.  Instead we:

1. Test the schema and channel naming directly.
2. Drive ``SubscriptionRegistry`` through ``asyncio.run``.
3. Run a fake ccxt.pro client through ``ExchangeSource`` and check that
   parsed messages flow into our callback.
4. Drive the full ``Gateway`` against a hand-rolled ``RedisBus`` stand-in
   and a fake source factory; verify subscribe/unsubscribe ref-counting,
   stats, and message publication.

We follow the same ``asyncio.run`` style as the rest of ``shared/tests`` —
no pytest-asyncio dependency.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import pytest

from shared.gateway.ccxt_pro_source import (
    ExchangeSource,
    parse_message,
)
from shared.gateway.gateway import Gateway
from shared.gateway.schema import (
    GatewayStats,
    L1Book,
    SubscriptionKey,
    SubscriptionRequest,
    Tick,
    TickChannel,
    Trade,
    channel_for,
    safe_symbol,
)
from shared.gateway.subscriptions import SubscriptionRegistry


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_safe_symbol_replaces_slash() -> None:
    assert safe_symbol("BTC/USDT") == "BTC-USDT"
    assert safe_symbol("BTC-USDT") == "BTC-USDT"


def test_channel_for_format() -> None:
    assert channel_for("BINANCE", "BTC/USDT", TickChannel.TICK) == "md.binance.BTC-USDT.tick"
    assert channel_for("bybit", "ETH/USDT", TickChannel.L1) == "md.bybit.ETH-USDT.l1"


def test_subscription_key_redis_channel() -> None:
    k = SubscriptionKey(exchange="binance", symbol="BTC/USDT", channel=TickChannel.TRADE)
    assert k.redis_channel == "md.binance.BTC-USDT.trade"


def test_subscription_request_requires_channels() -> None:
    with pytest.raises(Exception):
        SubscriptionRequest(exchange="binance", symbol="BTC/USDT", channels=[])


def test_l1_spread_is_optional() -> None:
    book = L1Book(
        exchange="binance",
        symbol="BTC/USDT",
        timestamp=datetime.now(timezone.utc),
    )
    assert book.spread is None

    book2 = L1Book(
        exchange="binance",
        symbol="BTC/USDT",
        timestamp=datetime.now(timezone.utc),
        bid_price=Decimal("100"),
        ask_price=Decimal("100.5"),
    )
    assert book2.spread == Decimal("0.5")


def test_gateway_stats_serialises() -> None:
    s = GatewayStats(
        started_at=datetime.now(timezone.utc),
        uptime_seconds=10.0,
        subscriptions_total=2,
        sources_total=1,
        messages_in_total=100,
        messages_published_total=99,
        publish_errors_total=1,
        reconnects_total=0,
        last_message_at=None,
    )
    payload = s.model_dump_json()
    assert "subscriptions_total" in payload
    assert "uptime_seconds" in payload


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_ref_counts() -> None:
    async def go() -> None:
        reg = SubscriptionRegistry()
        key = SubscriptionKey(exchange="binance", symbol="BTC/USDT", channel=TickChannel.TICK)

        a = await reg.add(key, "agent-a")
        assert a.activated is True
        assert a.new_count == 1

        b = await reg.add(key, "agent-b")
        assert b.activated is False
        assert b.new_count == 2
        assert reg.total() == 2

        rb = await reg.remove(key, "agent-b")
        assert rb.deactivated is False
        assert rb.new_count == 1

        ra = await reg.remove(key, "agent-a")
        assert ra.deactivated is True
        assert ra.new_count == 0
        assert reg.has(key) is False

    asyncio.run(go())


def test_registry_remove_under_zero_is_noop() -> None:
    async def go() -> None:
        reg = SubscriptionRegistry()
        key = SubscriptionKey(exchange="binance", symbol="BTC/USDT", channel=TickChannel.TICK)
        r = await reg.remove(key, "ghost")
        assert r.deactivated is False
        assert r.new_count == 0

    asyncio.run(go())


# ---------------------------------------------------------------------------
# parse_message
# ---------------------------------------------------------------------------


def test_parse_ticker_message() -> None:
    key = SubscriptionKey(exchange="binance", symbol="BTC/USDT", channel=TickChannel.TICK)
    raw = {
        "timestamp": 1712960000000,
        "last": "75000.5",
        "bid": "74999.0",
        "ask": "75001.0",
        "high": "75100.0",
        "low": "74900.0",
        "baseVolume": "12.34",
        "quoteVolume": "925000",
    }
    parsed = parse_message(key, raw)
    assert len(parsed) == 1
    tick = parsed[0]
    assert isinstance(tick, Tick)
    assert tick.last == Decimal("75000.5")
    assert tick.bid == Decimal("74999.0")
    assert tick.ask == Decimal("75001.0")


def test_parse_trades_message() -> None:
    key = SubscriptionKey(exchange="binance", symbol="BTC/USDT", channel=TickChannel.TRADE)
    raw = [
        {"timestamp": 1712960000000, "price": "75000", "amount": "0.1", "side": "buy", "id": 1},
        {"timestamp": 1712960001000, "price": "75001", "amount": "0.2", "side": "sell", "id": 2},
        {"timestamp": 1712960002000, "price": None, "amount": "0.3"},  # invalid -> dropped
    ]
    parsed = parse_message(key, raw)
    assert len(parsed) == 2
    assert all(isinstance(t, Trade) for t in parsed)
    assert parsed[0].side == "buy"
    assert parsed[1].trade_id == "2"


def test_parse_l1_message() -> None:
    key = SubscriptionKey(exchange="binance", symbol="BTC/USDT", channel=TickChannel.L1)
    raw = {
        "timestamp": 1712960000000,
        "bids": [["74999.0", "1.0"], ["74998.0", "2.0"]],
        "asks": [["75001.0", "0.5"], ["75002.0", "1.5"]],
    }
    parsed = parse_message(key, raw)
    assert len(parsed) == 1
    book = parsed[0]
    assert isinstance(book, L1Book)
    assert book.bid_price == Decimal("74999.0")
    assert book.ask_price == Decimal("75001.0")
    assert book.bid_size == Decimal("1.0")


def test_parse_unknown_returns_empty() -> None:
    key = SubscriptionKey(exchange="binance", symbol="BTC/USDT", channel=TickChannel.MARK)
    parsed = parse_message(key, {"timestamp": 1, "some": "junk"})
    assert parsed == []


# ---------------------------------------------------------------------------
# ExchangeSource against a fake ccxt.pro client
# ---------------------------------------------------------------------------


class _FakeCcxtClient:
    """Returns 3 ticker payloads then sleeps forever."""

    def __init__(self) -> None:
        self.calls = 0
        self.closed = False

    async def watch_ticker(self, symbol: str) -> Dict[str, Any]:
        self.calls += 1
        if self.calls > 3:
            await asyncio.sleep(60)
        return {
            "timestamp": 1712960000000 + self.calls * 1000,
            "last": str(75000 + self.calls),
            "bid": str(74999 + self.calls),
            "ask": str(75001 + self.calls),
        }

    async def close(self) -> None:
        self.closed = True


def test_exchange_source_emits_messages() -> None:
    received: List[Tuple[SubscriptionKey, Any]] = []
    client = _FakeCcxtClient()

    async def cb(key: SubscriptionKey, parsed: Any) -> None:
        received.append((key, parsed))

    async def go() -> None:
        source = ExchangeSource(exchange="binance", client_factory=lambda: client, on_message=cb)
        key = SubscriptionKey(exchange="binance", symbol="BTC/USDT", channel=TickChannel.TICK)
        await source.start_stream(key)
        for _ in range(50):
            if len(received) >= 3:
                break
            await asyncio.sleep(0.02)
        await source.stop()

    asyncio.run(go())

    assert len(received) >= 3
    for k, parsed in received[:3]:
        assert isinstance(parsed, Tick)
    assert client.closed is True


def test_exchange_source_reconnects_on_error() -> None:
    """Client raises on first call -> at least one reconnect callback fires."""
    received: List[Tuple[SubscriptionKey, Any]] = []
    reconnect_keys: List[SubscriptionKey] = []

    class _Flaky:
        def __init__(self) -> None:
            self.calls = 0

        async def watch_ticker(self, symbol: str) -> Dict[str, Any]:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("boom")
            if self.calls > 2:
                await asyncio.sleep(60)
            return {"timestamp": 1712960000000, "last": "75000"}

        async def close(self) -> None:
            return None

    client = _Flaky()

    async def cb(key: SubscriptionKey, parsed: Any) -> None:
        received.append((key, parsed))

    async def on_reconnect(key: SubscriptionKey) -> None:
        reconnect_keys.append(key)

    async def go() -> None:
        source = ExchangeSource(
            exchange="binance",
            client_factory=lambda: client,
            on_message=cb,
            on_reconnect=on_reconnect,
            initial_backoff_seconds=0.05,
            max_backoff_seconds=0.1,
        )
        key = SubscriptionKey(exchange="binance", symbol="BTC/USDT", channel=TickChannel.TICK)
        await source.start_stream(key)
        for _ in range(100):
            if received and reconnect_keys:
                break
            await asyncio.sleep(0.02)
        await source.stop()

    asyncio.run(go())

    assert reconnect_keys, "expected at least one reconnect callback"
    assert len(received) >= 1


# ---------------------------------------------------------------------------
# Gateway end-to-end with fake bus + fake source
# ---------------------------------------------------------------------------


class _FakeBus:
    """Stand-in that mimics RedisBus but stores everything in memory."""

    def __init__(self) -> None:
        self.published: List[Tuple[str, str]] = []
        self.lag: Dict[str, int] = {}
        self.stats_blob: Optional[str] = None
        self.desired: Dict[str, str] = {}
        self.connected = False
        self.closed = False

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True

    async def publish(self, exchange: str, symbol: str, channel: TickChannel, payload: str) -> int:
        ch = channel_for(exchange, symbol, channel)
        self.published.append((ch, payload))
        return 0

    async def record_lag(self, exchange: str, symbol: str, ts: Optional[datetime] = None) -> None:
        ts = ts or datetime.now(timezone.utc)
        self.lag[f"{exchange}:{safe_symbol(symbol)}"] = int(ts.timestamp() * 1000)

    async def write_stats(self, stats: GatewayStats) -> None:
        self.stats_blob = stats.model_dump_json()

    async def add_desired_sub(self, key: SubscriptionKey, requested_by: str) -> bool:
        field = f"{key.exchange}|{key.symbol}|{key.channel.value}"
        if field in self.desired:
            return False
        self.desired[field] = requested_by
        return True

    async def remove_desired_sub(self, key: SubscriptionKey) -> bool:
        field = f"{key.exchange}|{key.symbol}|{key.channel.value}"
        return self.desired.pop(field, None) is not None


class _FakeSource:
    """Drop-in for ExchangeSource that just records start/stop."""

    def __init__(self, exchange: str, on_message: Any) -> None:
        self.exchange = exchange
        self.on_message = on_message
        self.started: List[SubscriptionKey] = []
        self.stopped: List[SubscriptionKey] = []
        self.live: set[SubscriptionKey] = set()

    async def start_stream(self, key: SubscriptionKey) -> None:
        self.started.append(key)
        self.live.add(key)

    async def stop_stream(self, key: SubscriptionKey) -> None:
        self.stopped.append(key)
        self.live.discard(key)

    async def stop(self) -> None:
        for key in list(self.live):
            await self.stop_stream(key)

    def streams_snapshot(self) -> List[Dict[str, Any]]:
        return [
            {"exchange": self.exchange, "symbol": k.symbol, "channel": k.channel.value}
            for k in self.live
        ]


def test_gateway_ref_counts_and_starts_one_stream() -> None:
    bus = _FakeBus()
    sources: Dict[str, _FakeSource] = {}

    def factory(name: str) -> Any:
        s = _FakeSource(name, on_message=None)
        sources[name] = s
        return s

    async def go() -> None:
        gw = Gateway(bus, source_factory=factory)  # type: ignore[arg-type]
        await gw.start()
        try:
            leases1 = await gw.subscribe(SubscriptionRequest(
                exchange="binance", symbol="BTC/USDT",
                channels=[TickChannel.TICK, TickChannel.TRADE],
                requested_by="agent-a",
            ))
            assert all(lease.activated for lease in leases1)
            assert sources["binance"].started == [
                SubscriptionKey(exchange="binance", symbol="BTC/USDT", channel=TickChannel.TICK),
                SubscriptionKey(exchange="binance", symbol="BTC/USDT", channel=TickChannel.TRADE),
            ]

            leases2 = await gw.subscribe(SubscriptionRequest(
                exchange="binance", symbol="BTC/USDT",
                channels=[TickChannel.TICK],
                requested_by="agent-b",
            ))
            assert leases2[0].activated is False
            assert leases2[0].new_count == 2
            assert len(sources["binance"].started) == 2

            rels = await gw.unsubscribe(SubscriptionRequest(
                exchange="binance", symbol="BTC/USDT",
                channels=[TickChannel.TICK],
                requested_by="agent-b",
            ))
            assert rels[0].deactivated is False

            rels2 = await gw.unsubscribe(SubscriptionRequest(
                exchange="binance", symbol="BTC/USDT",
                channels=[TickChannel.TICK, TickChannel.TRADE],
                requested_by="agent-a",
            ))
            assert all(r.deactivated for r in rels2)
            stopped_set = set(sources["binance"].stopped)
            assert SubscriptionKey(exchange="binance", symbol="BTC/USDT", channel=TickChannel.TICK) in stopped_set
            assert SubscriptionKey(exchange="binance", symbol="BTC/USDT", channel=TickChannel.TRADE) in stopped_set
        finally:
            await gw.stop()

    asyncio.run(go())

    assert bus.connected and bus.closed


def test_gateway_publish_path() -> None:
    bus = _FakeBus()

    def factory(name: str) -> Any:
        return _FakeSource(name, on_message=None)

    async def go() -> None:
        gw = Gateway(bus, source_factory=factory)  # type: ignore[arg-type]
        await gw.start()
        try:
            key = SubscriptionKey(exchange="binance", symbol="BTC/USDT", channel=TickChannel.TICK)
            tick = Tick(
                exchange="binance",
                symbol="BTC/USDT",
                timestamp=datetime.now(timezone.utc),
                last=Decimal("75000"),
            )
            await gw._on_message(key, tick)

            stats = await gw.stats()
            assert stats.messages_in_total == 1
            assert stats.messages_published_total == 1
            assert stats.publish_errors_total == 0

            assert len(bus.published) == 1
            ch, payload = bus.published[0]
            assert ch == "md.binance.BTC-USDT.tick"
            assert json.loads(payload)["symbol"] == "BTC/USDT"
        finally:
            await gw.stop()

    asyncio.run(go())
