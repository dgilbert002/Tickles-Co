"""
Module: shared.gateway.capital_source
Purpose: Native Capital.com WebSocket source for the Market Data Gateway.
Location: /opt/tickles/shared/gateway/capital_source.py
"""

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

import aiohttp

from shared.connectors.capital_adapter import CapitalAdapter
from shared.gateway.schema import Candle, SubscriptionKey, TickChannel

logger = logging.getLogger(__name__)

# WebSocket streaming URLs — must match the adapter's environment.
DEMO_WS_URL = "wss://demo-streaming-capital.backend-capital.com/connect"
LIVE_WS_URL = "wss://api-streaming-capital.backend-capital.com/connect"

# Map timeframe strings to Capital.com WS resolution strings.
_RESOLUTION_MAP = {
    "1m": "MINUTE",
    "5m": "MINUTE_5",
    "15m": "MINUTE_15",
    "1h": "HOUR",
    "4h": "HOUR_4",
    "1d": "DAY",
}

MessageCallback = Callable[[SubscriptionKey, Any], Awaitable[None]]


@dataclass
class _StreamState:
    started_at: datetime
    reconnects: int = 0


class CapitalSource:
    """
    Native Capital.com WebSocket source.
    
    Unlike CCXT Pro which uses one task per subscription, this source
    manages a single shared WebSocket connection for all epics to
    stay within Capital.com's strict connection limits.
    """

    def __init__(
        self,
        adapter: CapitalAdapter,
        on_message: MessageCallback,
        on_reconnect: Optional[Callable[[SubscriptionKey], Awaitable[None]]] = None,
        max_backoff_seconds: float = 30.0,
        initial_backoff_seconds: float = 1.0,
    ) -> None:
        self.exchange = "capital"
        self._adapter = adapter
        self.on_message = on_message
        self.on_reconnect = on_reconnect
        self.max_backoff_seconds = max_backoff_seconds
        self.initial_backoff_seconds = initial_backoff_seconds

        self._desired_keys: Set[SubscriptionKey] = set()
        self._streams: Dict[SubscriptionKey, _StreamState] = {}
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._main_task: Optional[asyncio.Task[None]] = None
        self._stopping = False
        self._lock = asyncio.Lock()

    async def start_stream(self, key: SubscriptionKey) -> None:
        """Add a key to the desired set and ensure the main loop is running."""
        if key.exchange != self.exchange:
            raise ValueError(f"key.exchange={key.exchange} != source.exchange={self.exchange}")

        async with self._lock:
            self._desired_keys.add(key)
            if key not in self._streams:
                self._streams[key] = _StreamState(started_at=datetime.now(timezone.utc))

            if self._main_task is None or self._main_task.done():
                self._main_task = asyncio.create_task(self._run_loop(), name="md:capital:main")

            # If WS is already connected, send the subscription message immediately
            if self._ws is not None and not self._ws.closed:
                await self._subscribe_key(key)

    async def stop_stream(self, key: SubscriptionKey) -> None:
        """Remove a key from the desired set and unsubscribe if connected."""
        async with self._lock:
            self._desired_keys.discard(key)
            self._streams.pop(key, None)

            if self._ws is not None and not self._ws.closed:
                await self._unsubscribe_key(key)

    async def stop(self) -> None:
        """Shut down the entire source and reset so it can be restarted."""
        self._stopping = True
        if self._main_task:
            self._main_task.cancel()
            try:
                await self._main_task
            except (asyncio.CancelledError, Exception):
                pass
            self._main_task = None

        if self._ws:
            await self._ws.close()
            self._ws = None

        await self._adapter.close()
        # Reset so the source can be restarted after stop().
        self._stopping = False
        self._desired_keys.clear()
        self._streams.clear()
        logger.info("CapitalSource stopped")

    def streams_snapshot(self) -> List[Dict[str, Any]]:
        """Return a list of active streams for the gateway stats."""
        return [
            {
                "exchange": self.exchange,
                "symbol": key.symbol,
                "channel": key.channel.value,
                "started_at": state.started_at.isoformat(),
                "reconnects": state.reconnects,
            }
            for key, state in self._streams.items()
        ]

    async def _run_loop(self) -> None:
        """Main reconnection loop."""
        backoff = self.initial_backoff_seconds
        while not self._stopping:
            try:
                await self._connect_and_listen()
                backoff = self.initial_backoff_seconds
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Capital WS loop errored: %s. Retrying in %.1fs", exc, backoff)
                # Only increment reconnect for streams that were active
                # before the error (not for newly added ones).
                for key in list(self._streams):
                    if key in self._desired_keys:
                        self._streams[key].reconnects += 1
                
                jitter = random.uniform(0.0, backoff * 0.25)
                await asyncio.sleep(min(backoff + jitter, self.max_backoff_seconds))
                backoff = min(backoff * 2.0, self.max_backoff_seconds)

    async def _connect_and_listen(self) -> None:
        """Single connection attempt and message processing."""
        # 1. Authenticate to get fresh tokens
        from shared.utils.credentials import Credentials
        creds = Credentials.get("capital", "main")
        
        await self._adapter.authenticate(
            creds["email"],
            creds["password"],
            creds["apiKey"],
            account_id=creds.get("accountId")
        )
        
        # 2. Connect — use the correct WS URL for the adapter's environment.
        url = LIVE_WS_URL if self._adapter._environment == "live" else DEMO_WS_URL
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(url) as ws:
                self._ws = ws
                logger.info("Capital WS connected")

                # 3. Subscribe to all desired keys
                async with self._lock:
                    for key in self._desired_keys:
                        await self._subscribe_key(key)

                # 4. Listen for messages
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._handle_raw_message(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
                
                self._ws = None
                logger.info("Capital WS disconnected")

    async def _subscribe_key(self, key: SubscriptionKey) -> None:
        """Send a subscription message for a specific key."""
        if self._ws is None or self._ws.closed:
            return

        # Capital.com only supports OHLCV streaming via WS for now in our implementation
        if key.channel != TickChannel.CANDLE:
            logger.warning("CapitalSource only supports CANDLE channel via WS, got %s", key.channel)
            return

        # We need the tokens for the subscription message
        cst = self._adapter.cst
        security_token = self._adapter.security_token

        if not cst or not security_token:
            logger.warning("CapitalSource: No auth tokens available for subscription")
            return

        # Derive resolution from the subscription key's symbol metadata.
        # Default to MINUTE (1m) if no timeframe hint is available.
        resolution = _RESOLUTION_MAP.get(
            getattr(key, "timeframe", None) or "1m", "MINUTE"
        )
        msg = {
            "destination": "OHLCMarketData.subscribe",
            "cst": cst,
            "securityToken": security_token,
            "payload": {
                "epics": [key.symbol],
                "resolutions": [resolution],
                "type": "classic"
            }
        }
        await self._ws.send_str(json.dumps(msg))
        logger.info("Subscribed to Capital epic: %s", key.symbol)

    async def _unsubscribe_key(self, key: SubscriptionKey) -> None:
        """Send an unsubscribe message."""
        if self._ws is None or self._ws.closed:
            return

        if key.channel != TickChannel.CANDLE:
            return

        cst = self._adapter.cst
        security_token = self._adapter.security_token

        if not cst or not security_token:
            return

        resolution = _RESOLUTION_MAP.get(
            getattr(key, "timeframe", None) or "1m", "MINUTE"
        )
        msg = {
            "destination": "OHLCMarketData.unsubscribe",
            "cst": cst,
            "securityToken": security_token,
            "payload": {
                "epics": [key.symbol],
                "resolutions": [resolution]
            }
        }
        await self._ws.send_str(json.dumps(msg))
        logger.info("Unsubscribed from Capital epic: %s", key.symbol)

    async def _handle_raw_message(self, data: str) -> None:
        """Parse raw WS message and dispatch to Gateway."""
        try:
            raw = json.loads(data)
            # Capital.com WS messages for OHLCMarketData look like:
            # {"destination": "ohlc.event", "payload": {...}}
            if raw.get("destination") == "ohlc.event":
                payload = raw.get("payload", {})
                epic = payload.get("epic")
                if not epic:
                    return

                # Validate required OHLC fields before constructing Candle.
                required = ("t", "o", "h", "l", "c")
                if not all(payload.get(k) is not None for k in required):
                    logger.debug(
                        "Capital WS: skipping incomplete OHLC payload for %s",
                        epic,
                    )
                    return

                candle = Candle(
                    exchange=self.exchange,
                    symbol=epic,
                    timestamp=datetime.fromtimestamp(
                        payload["t"] / 1000, tz=timezone.utc
                    ),
                    timeframe="1m",
                    open=Decimal(str(payload["o"])),
                    high=Decimal(str(payload["h"])),
                    low=Decimal(str(payload["l"])),
                    close=Decimal(str(payload["c"])),
                    volume=Decimal(str(payload.get("v", 0))),
                )

                key = SubscriptionKey(exchange=self.exchange, symbol=epic, channel=TickChannel.CANDLE)
                await self.on_message(key, candle)

        except Exception as exc:
            logger.warning("Failed to handle Capital WS message: %s", exc)
