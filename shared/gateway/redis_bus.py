"""
shared.gateway.redis_bus — async Redis pub/sub + stats helper for Phase 17.

The 21-year-old version
-----------------------
This module is the *only* place in the gateway package that talks to
Redis directly.  Everything else asks the bus to publish, peek, or read
stats.  Centralising the I/O makes mocking trivial in tests and keeps
connection management consistent.

Keys we own
-----------

- ``md.<exchange>.<symbol_safe>.<channel>``     pub/sub channels.
- ``md:gateway:stats``                           latest GatewayStats JSON.
- ``md:gateway:lag:<exchange>:<symbol_safe>``   last-message epoch ms.
- ``md:subscriptions``                           hash of desired subs:
      field = ``<exchange>|<symbol>|<channel>``
      value = JSON ``{"requested_by": "...", "since": "..."}``
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Dict, List, Optional, Tuple, cast

import redis.asyncio as aioredis

from shared.gateway.schema import (
    GatewayStats,
    SubscriptionKey,
    TickChannel,
    channel_for,
    safe_symbol,
)

logger = logging.getLogger(__name__)

STATS_KEY = "md:gateway:stats"
SUBS_KEY = "md:subscriptions"
LAG_PREFIX = "md:gateway:lag"


def lag_key(exchange: str, symbol: str) -> str:
    return f"{LAG_PREFIX}:{exchange.lower()}:{safe_symbol(symbol)}"


def sub_field(key: SubscriptionKey) -> str:
    return f"{key.exchange.lower()}|{key.symbol}|{key.channel.value}"


def parse_sub_field(field: str) -> Optional[SubscriptionKey]:
    parts = field.split("|")
    if len(parts) != 3:
        return None
    exchange, symbol, channel_raw = parts
    try:
        channel = TickChannel(channel_raw)
    except ValueError:
        return None
    return SubscriptionKey(exchange=exchange, symbol=symbol, channel=channel)


class RedisBus:
    """Thin async wrapper. Owns a single ``redis.asyncio.Redis`` client."""

    def __init__(self, url: str = "redis://127.0.0.1:6379/0") -> None:
        self._url = url
        self._client: Optional[aioredis.Redis] = None

    async def connect(self) -> None:
        if self._client is None:
            self._client = aioredis.from_url(
                self._url,
                encoding="utf-8",
                decode_responses=True,
                health_check_interval=15,
            )
            await cast(Awaitable[Any], self._client.ping())
            logger.info("RedisBus connected to %s", self._url)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.info("RedisBus closed")

    @property
    def client(self) -> aioredis.Redis:
        if self._client is None:
            raise RuntimeError("RedisBus not connected — call connect() first")
        return self._client

    async def publish(self, exchange: str, symbol: str, channel: TickChannel, payload_json: str) -> int:
        """Publish a JSON payload and return the number of subscribers reached."""
        ch = channel_for(exchange, symbol, channel)
        fut = cast(Awaitable[int], self.client.publish(ch, payload_json))
        return int(await fut)

    async def record_lag(self, exchange: str, symbol: str, ts: Optional[datetime] = None) -> None:
        ts = ts or datetime.now(timezone.utc)
        await self.client.set(lag_key(exchange, symbol), int(ts.timestamp() * 1000))

    async def write_stats(self, stats: GatewayStats) -> None:
        await self.client.set(STATS_KEY, stats.model_dump_json())

    async def read_stats(self) -> Optional[Dict[str, Any]]:
        raw = await self.client.get(STATS_KEY)
        if raw is None:
            return None
        return json.loads(raw)

    async def list_lag(self) -> List[Tuple[str, int]]:
        out: List[Tuple[str, int]] = []
        async for key in self.client.scan_iter(match=f"{LAG_PREFIX}:*"):
            value = await self.client.get(key)
            if value is None:
                continue
            try:
                out.append((key, int(value)))
            except ValueError:
                continue
        return out

    async def list_desired_subs(self) -> List[Tuple[SubscriptionKey, Dict[str, Any]]]:
        result: List[Tuple[SubscriptionKey, Dict[str, Any]]] = []
        raw = await cast(Awaitable[Dict[str, str]], self.client.hgetall(SUBS_KEY))
        for field, value in (raw or {}).items():
            key = parse_sub_field(field)
            if key is None:
                continue
            try:
                meta = json.loads(value) if value else {}
            except json.JSONDecodeError:
                meta = {}
            result.append((key, meta))
        return result

    async def add_desired_sub(self, key: SubscriptionKey, requested_by: str) -> bool:
        meta = {
            "requested_by": requested_by,
            "since": datetime.now(timezone.utc).isoformat(),
        }
        added = await cast(
            Awaitable[int],
            self.client.hsetnx(SUBS_KEY, sub_field(key), json.dumps(meta)),
        )
        return bool(added)

    async def remove_desired_sub(self, key: SubscriptionKey) -> bool:
        removed = await cast(Awaitable[int], self.client.hdel(SUBS_KEY, sub_field(key)))
        return bool(removed)

    async def listen_pattern(self, pattern: str) -> AsyncIterator[Tuple[str, str]]:
        """Yield ``(channel, payload)`` for every message matching pattern.

        We swallow cleanup errors deliberately: when the consumer ``break``s
        out of the ``async for``, Python tears down this generator with
        ``GeneratorExit`` while the redis connection's transport may already
        be closed.  Calling ``punsubscribe`` then raises a spurious
        ``TypeError`` from the asyncio selector — harmless but noisy.
        """
        pubsub = self.client.pubsub()
        try:
            await pubsub.psubscribe(pattern)
            async for message in pubsub.listen():
                if message.get("type") != "pmessage":
                    continue
                yield (message["channel"], message["data"])
        finally:
            try:
                await pubsub.punsubscribe(pattern)
            except Exception:  # noqa: BLE001
                logger.debug("punsubscribe(%s) cleanup failed", pattern, exc_info=True)
            close_fn = getattr(pubsub, "aclose", None) or getattr(pubsub, "close", None)
            if close_fn is not None:
                try:
                    result = close_fn()
                    if result is not None:
                        await result
                except Exception:  # noqa: BLE001
                    logger.debug("pubsub close cleanup failed", exc_info=True)
