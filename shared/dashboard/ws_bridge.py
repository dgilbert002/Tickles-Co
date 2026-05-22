"""
Module: ws_bridge
Purpose: Dashboard WebSocket bridge — accepts browser WS connections at
         ``/api/ws/prices?symbols=...``, connects to the price feed daemon's
         internal WS server, relays subscriptions and ticker updates.
Location: /opt/tickles/shared/dashboard/ws_bridge.py
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional, Set
from urllib.parse import parse_qs

import aiohttp
from aiohttp import web

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DAEMON_WS_URL: str = "ws://127.0.0.1:18790"
MAX_BRIDGE_CLIENTS: int = 50


# ---------------------------------------------------------------------------
# Bridge handler
# ---------------------------------------------------------------------------
async def handle_price_ws(request: web.Request) -> web.WebSocketResponse:
    """WebSocket handler for browser price subscriptions.

    Accepts ``ws://host/api/ws/prices?symbols=BTC/USDT,ETH/USDT,...``
    from the browser. Connects to the price feed daemon's internal WS,
    forwards subscriptions, and relays ticker updates to the browser.

    If the daemon is not running, the WS connection is accepted but
    no data is streamed — the browser falls back to polling.
    """
    # Parse symbols from query string
    raw = request.query.get("symbols", "")
    symbols: list[str] = [s.strip() for s in raw.split(",") if s.strip()]

    ws = web.WebSocketResponse(heartbeat=30.0)
    await ws.prepare(request)

    if not symbols:
        await ws.send_json({"type": "error", "message": "No symbols provided"})
        await ws.close()
        return ws

    logger.info("Bridge client connected, symbols: %s", symbols)

    daemon_ws: Optional[aiohttp.ClientWebSocketResponse] = None
    daemon_session: Optional[aiohttp.ClientSession] = None
    daemon_task: Optional[asyncio.Task] = None

    async def connect_daemon() -> Optional[aiohttp.ClientWebSocketResponse]:
        """Connect to the price feed daemon and subscribe to symbols."""
        nonlocal daemon_session
        try:
            daemon_session = aiohttp.ClientSession()
            dws = await daemon_session.ws_connect(DAEMON_WS_URL, heartbeat=15.0)
            await dws.send_json({"subscribe": symbols})
            logger.info("Bridge connected to daemon, subscribed to %d symbols", len(symbols))
            return dws
        except Exception as exc:
            logger.warning("Bridge cannot reach daemon at %s: %s", DAEMON_WS_URL, exc)
            return None

    async def relay_from_daemon() -> None:
        """Read tickers from daemon WS and forward to browser."""
        nonlocal daemon_ws
        while not ws.closed and daemon_ws is not None and not daemon_ws.closed:
            try:
                msg = await asyncio.wait_for(daemon_ws.receive_json(), timeout=30.0)
                if not ws.closed:
                    await ws.send_json(msg)
            except asyncio.TimeoutError:
                continue
            except Exception:
                break

    try:
        daemon_ws = await connect_daemon()
        if daemon_ws is not None:
            daemon_task = asyncio.create_task(relay_from_daemon())

        # Listen for browser messages (close, unsubscribe)
        async for raw in ws:
            if raw.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(raw.data)
                except json.JSONDecodeError:
                    continue
                # Browser sent updated subscription list
                if "subscribe" in data and daemon_ws is not None and not daemon_ws.closed:
                    try:
                        await daemon_ws.send_json(data)
                    except Exception:
                        break
            elif raw.type == web.WSMsgType.ERROR:
                break
    except ConnectionResetError:
        logger.debug("Bridge browser client reset")
    finally:
        if daemon_task:
            daemon_task.cancel()
        if daemon_ws is not None and not daemon_ws.closed:
            try:
                await daemon_ws.send_json({"unsubscribe": symbols})
                await daemon_ws.close()
            except Exception:
                pass
        if daemon_session is not None:
            try:
                await daemon_session.close()
            except Exception:
                pass
        logger.info("Bridge client disconnected")

    return ws
