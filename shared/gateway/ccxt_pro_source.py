"""
shared.gateway.ccxt_pro_source — per-exchange ccxt.pro WebSocket source.

The 21-year-old version
-----------------------
One ``ExchangeSource`` per exchange (e.g. one for ``binance``, one for
``bybit``).  It owns:

- A single ``ccxt.pro.<exchange>`` client.
- A dict of running ``asyncio.Task``s, one per ``(symbol, channel)``.
- An exponential-backoff reconnect loop on errors.

Each running task calls the appropriate ``await client.watch_*`` method
in a tight loop and forwards each message to a callback supplied by the
gateway.  The callback is responsible for serialising and publishing.

We deliberately keep this file *thin*: it does not know about Redis or
the registry — only about ccxt and asyncio — so it stays unit-testable
with a fake ccxt client.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from shared.gateway.schema import (
    L1Book,
    MarkPrice,
    SubscriptionKey,
    Tick,
    TickChannel,
    Trade,
)

logger = logging.getLogger(__name__)

MessageCallback = Callable[[SubscriptionKey, Any], Awaitable[None]]


def _utc_from_ms(ms: Optional[int]) -> datetime:
    if ms is None:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _maybe_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return None


def _ticker_to_tick(exchange: str, symbol: str, raw: Dict[str, Any]) -> Tick:
    return Tick(
        exchange=exchange,
        symbol=symbol,
        timestamp=_utc_from_ms(raw.get("timestamp")),
        last=_maybe_decimal(raw.get("last") or raw.get("close")),
        bid=_maybe_decimal(raw.get("bid")),
        ask=_maybe_decimal(raw.get("ask")),
        high=_maybe_decimal(raw.get("high")),
        low=_maybe_decimal(raw.get("low")),
        base_volume=_maybe_decimal(raw.get("baseVolume")),
        quote_volume=_maybe_decimal(raw.get("quoteVolume")),
    )


def _trades_to_models(exchange: str, symbol: str, raws: Any) -> list[Trade]:
    if not isinstance(raws, list):
        return []
    out: list[Trade] = []
    for raw in raws:
        try:
            price = _maybe_decimal(raw.get("price"))
            amount = _maybe_decimal(raw.get("amount"))
            if price is None or amount is None:
                continue
            out.append(
                Trade(
                    exchange=exchange,
                    symbol=symbol,
                    timestamp=_utc_from_ms(raw.get("timestamp")),
                    price=price,
                    amount=amount,
                    side=raw.get("side"),
                    trade_id=str(raw["id"]) if raw.get("id") is not None else None,
                )
            )
        except (TypeError, KeyError, ValueError):
            continue
    return out


def _orderbook_to_l1(exchange: str, symbol: str, raw: Dict[str, Any]) -> L1Book:
    bids = raw.get("bids") or []
    asks = raw.get("asks") or []
    bid_price = _maybe_decimal(bids[0][0]) if bids else None
    bid_size = _maybe_decimal(bids[0][1]) if bids else None
    ask_price = _maybe_decimal(asks[0][0]) if asks else None
    ask_size = _maybe_decimal(asks[0][1]) if asks else None
    return L1Book(
        exchange=exchange,
        symbol=symbol,
        timestamp=_utc_from_ms(raw.get("timestamp")),
        bid_price=bid_price,
        bid_size=bid_size,
        ask_price=ask_price,
        ask_size=ask_size,
    )


def _markprice_to_model(exchange: str, symbol: str, raw: Dict[str, Any]) -> Optional[MarkPrice]:
    mark = _maybe_decimal(raw.get("markPrice") or raw.get("mark_price"))
    if mark is None:
        return None
    return MarkPrice(
        exchange=exchange,
        symbol=symbol,
        timestamp=_utc_from_ms(raw.get("timestamp")),
        mark_price=mark,
        index_price=_maybe_decimal(raw.get("indexPrice")),
    )


def parse_message(key: SubscriptionKey, raw: Any) -> list[Any]:
    """Pure helper exposed for tests."""
    if key.channel == TickChannel.TICK and isinstance(raw, dict):
        return [_ticker_to_tick(key.exchange, key.symbol, raw)]
    if key.channel == TickChannel.TRADE:
        return list(_trades_to_models(key.exchange, key.symbol, raw))
    if key.channel == TickChannel.L1 and isinstance(raw, dict):
        return [_orderbook_to_l1(key.exchange, key.symbol, raw)]
    if key.channel == TickChannel.MARK and isinstance(raw, dict):
        m = _markprice_to_model(key.exchange, key.symbol, raw)
        return [m] if m is not None else []
    return []


@dataclass
class _StreamState:
    task: asyncio.Task[None]
    started_at: datetime
    reconnects: int = 0


@dataclass
class ExchangeSource:
    """Owns one ccxt.pro client + a task per active subscription."""

    exchange: str
    client_factory: Callable[[], Any]
    on_message: MessageCallback
    on_reconnect: Optional[Callable[[SubscriptionKey], Awaitable[None]]] = None
    max_backoff_seconds: float = 30.0
    initial_backoff_seconds: float = 1.0
    _client: Any = field(default=None, init=False)
    _streams: Dict[SubscriptionKey, _StreamState] = field(default_factory=dict, init=False)
    _stopping: bool = field(default=False, init=False)

    async def _ensure_client(self) -> Any:
        if self._client is None:
            self._client = self.client_factory()
        return self._client

    async def start_stream(self, key: SubscriptionKey) -> None:
        if key.exchange != self.exchange:
            raise ValueError(f"key.exchange={key.exchange} != source.exchange={self.exchange}")
        if key in self._streams:
            return
        task = asyncio.create_task(self._stream_loop(key), name=f"md:{key.redis_channel}")
        self._streams[key] = _StreamState(task=task, started_at=datetime.now(timezone.utc))

    async def stop_stream(self, key: SubscriptionKey) -> None:
        state = self._streams.pop(key, None)
        if state is None:
            return
        state.task.cancel()
        try:
            await state.task
        except (asyncio.CancelledError, Exception):
            pass

    async def stop(self) -> None:
        self._stopping = True
        for key in list(self._streams.keys()):
            await self.stop_stream(key)
        if self._client is not None:
            try:
                close = getattr(self._client, "close", None)
                if callable(close):
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result
            except Exception as exc:  # noqa: BLE001
                logger.warning("Exchange %s client.close() raised: %s", self.exchange, exc)
            self._client = None

    def streams_snapshot(self) -> list[Dict[str, Any]]:
        return [
            {
                "exchange": key.exchange,
                "symbol": key.symbol,
                "channel": key.channel.value,
                "started_at": state.started_at.isoformat(),
                "reconnects": state.reconnects,
            }
            for key, state in self._streams.items()
        ]

    async def _stream_loop(self, key: SubscriptionKey) -> None:
        backoff = self.initial_backoff_seconds
        while not self._stopping:
            try:
                client = await self._ensure_client()
                await self._watch_once(client, key)
                backoff = self.initial_backoff_seconds
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                state = self._streams.get(key)
                if state is not None:
                    state.reconnects += 1
                logger.warning(
                    "Stream %s errored (%s); retry in %.1fs", key.redis_channel, exc, backoff
                )
                if self.on_reconnect is not None:
                    try:
                        await self.on_reconnect(key)
                    except Exception:  # noqa: BLE001
                        logger.exception("on_reconnect callback failed for %s", key.redis_channel)
                jitter = random.uniform(0.0, backoff * 0.25)
                await asyncio.sleep(min(backoff + jitter, self.max_backoff_seconds))
                backoff = min(backoff * 2.0, self.max_backoff_seconds)

    async def _watch_once(self, client: Any, key: SubscriptionKey) -> None:
        method, arg_kind = self._method_for(key.channel)
        watch = getattr(client, method, None)
        if watch is None:
            raise RuntimeError(
                f"Exchange {self.exchange} has no ccxt.pro method '{method}' for {key.channel.value}"
            )
        if arg_kind == "symbol":
            raw = await watch(key.symbol)
        else:
            raw = await watch(key.symbol)
        for parsed in parse_message(key, raw):
            await self.on_message(key, parsed)

    @staticmethod
    def _method_for(channel: TickChannel) -> Tuple[str, str]:
        if channel == TickChannel.TICK:
            return ("watch_ticker", "symbol")
        if channel == TickChannel.TRADE:
            return ("watch_trades", "symbol")
        if channel == TickChannel.L1:
            return ("watch_order_book", "symbol")
        if channel == TickChannel.MARK:
            return ("watch_mark_price", "symbol")
        if channel == TickChannel.FUNDING:
            return ("watch_funding_rate", "symbol")
        raise ValueError(f"Unsupported channel {channel}")


def default_ccxt_pro_factory(exchange_id: str) -> Callable[[], Any]:
    """Return a callable that builds a ccxt.pro client lazily.

    Importing ``ccxt.pro`` here would force every consumer (e.g. unit
    tests) to install ``ccxt``.  We defer the import to call time.
    """

    def _factory() -> Any:
        import ccxt.pro as ccxtpro  # type: ignore[import-not-found]

        if not hasattr(ccxtpro, exchange_id):
            raise RuntimeError(f"ccxt.pro has no exchange '{exchange_id}'")
        cls = getattr(ccxtpro, exchange_id)
        return cls({"enableRateLimit": True})

    return _factory
