"""
shared.gateway.gateway — orchestrator for the Market Data Gateway (Phase 17).

The 21-year-old version
-----------------------
This is the brain.  It glues together:

    SubscriptionRegistry  — who wants what?
    ExchangeSource(s)     — who can give it?
    RedisBus              — where does it go?

Public methods:

    await gateway.start()
    await gateway.subscribe(SubscriptionRequest)  -> List[SubscriptionLease]
    await gateway.unsubscribe(SubscriptionRequest)
    await gateway.stats()                          -> GatewayStats
    await gateway.stop()

The on_message callback wired into each ExchangeSource writes to Redis
and bumps counters.  No business logic in here.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from shared.gateway.ccxt_pro_source import ExchangeSource, default_ccxt_pro_factory
from shared.gateway.redis_bus import RedisBus
from shared.gateway.schema import (
    GatewayStats,
    SubscriptionKey,
    SubscriptionRequest,
    Tick,
    TickChannel,
)
from shared.gateway.subscriptions import (
    SubscriptionLease,
    SubscriptionRegistry,
    SubscriptionReleased,
)

logger = logging.getLogger(__name__)


class Gateway:
    """Coordinates registry, sources, and redis fan-out."""

    def __init__(
        self,
        bus: RedisBus,
        source_factory: Optional[Callable[[str], ExchangeSource]] = None,
    ) -> None:
        self._bus = bus
        self._registry = SubscriptionRegistry()
        self._sources: Dict[str, ExchangeSource] = {}
        self._source_factory = source_factory or self._default_source_factory
        self._started_at: Optional[datetime] = None
        self._messages_in = 0
        self._messages_published = 0
        self._publish_errors = 0
        self._reconnects = 0
        self._last_message_at: Optional[datetime] = None
        self._stats_task: Optional[asyncio.Task[None]] = None

    async def start(self) -> None:
        if self._started_at is not None:
            return
        await self._bus.connect()
        self._started_at = datetime.now(timezone.utc)
        self._stats_task = asyncio.create_task(self._stats_loop(), name="gateway:stats")
        logger.info("Gateway started")

    async def stop(self) -> None:
        if self._stats_task is not None:
            self._stats_task.cancel()
            try:
                await self._stats_task
            except (asyncio.CancelledError, Exception):
                pass
            self._stats_task = None
        for source in list(self._sources.values()):
            await source.stop()
        self._sources.clear()
        await self._bus.close()
        self._started_at = None
        logger.info("Gateway stopped")

    async def subscribe(self, request: SubscriptionRequest) -> List[SubscriptionLease]:
        leases: List[SubscriptionLease] = []
        for channel in request.channels:
            key = SubscriptionKey(
                exchange=request.exchange.lower(),
                symbol=request.symbol,
                channel=channel,
            )
            lease = await self._registry.add(key, request.requested_by)
            await self._bus.add_desired_sub(key, request.requested_by)
            if lease.activated:
                source = self._ensure_source(key.exchange)
                await source.start_stream(key)
            leases.append(lease)
        return leases

    async def unsubscribe(self, request: SubscriptionRequest) -> List[SubscriptionReleased]:
        releases: List[SubscriptionReleased] = []
        for channel in request.channels:
            key = SubscriptionKey(
                exchange=request.exchange.lower(),
                symbol=request.symbol,
                channel=channel,
            )
            release = await self._registry.remove(key, request.requested_by)
            if release.deactivated:
                source = self._sources.get(key.exchange)
                if source is not None:
                    await source.stop_stream(key)
                await self._bus.remove_desired_sub(key)
            releases.append(release)
        return releases

    async def stats(self) -> GatewayStats:
        now = datetime.now(timezone.utc)
        started = self._started_at or now
        return GatewayStats(
            started_at=started,
            uptime_seconds=(now - started).total_seconds(),
            subscriptions_total=self._registry.total(),
            sources_total=len(self._sources),
            messages_in_total=self._messages_in,
            messages_published_total=self._messages_published,
            publish_errors_total=self._publish_errors,
            reconnects_total=self._reconnects,
            last_message_at=self._last_message_at,
        )

    def registry_snapshot(self) -> List[Dict[str, Any]]:
        return self._registry.snapshot()

    def sources_snapshot(self) -> Dict[str, List[Dict[str, Any]]]:
        return {name: source.streams_snapshot() for name, source in self._sources.items()}

    async def _on_message(self, key: SubscriptionKey, parsed: Any) -> None:
        self._messages_in += 1
        self._last_message_at = datetime.now(timezone.utc)
        try:
            payload = parsed.model_dump_json() if hasattr(parsed, "model_dump_json") else str(parsed)
            await self._bus.publish(key.exchange, key.symbol, key.channel, payload)
            await self._bus.record_lag(key.exchange, key.symbol, getattr(parsed, "timestamp", None))
            self._messages_published += 1
        except Exception as exc:  # noqa: BLE001
            self._publish_errors += 1
            logger.warning("Publish failed for %s: %s", key.redis_channel, exc)

    async def _on_reconnect(self, key: SubscriptionKey) -> None:
        self._reconnects += 1

    def _ensure_source(self, exchange: str) -> ExchangeSource:
        source = self._sources.get(exchange)
        if source is not None:
            return source
        source = self._source_factory(exchange)
        self._sources[exchange] = source
        return source

    def _default_source_factory(self, exchange: str) -> ExchangeSource:
        return ExchangeSource(
            exchange=exchange,
            client_factory=default_ccxt_pro_factory(exchange),
            on_message=self._on_message,
            on_reconnect=self._on_reconnect,
        )

    async def _stats_loop(self, interval: float = 5.0) -> None:
        while True:
            try:
                await asyncio.sleep(interval)
                await self._bus.write_stats(await self.stats())
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("stats loop iteration failed")


__all__ = ["Gateway", "Tick", "TickChannel"]
