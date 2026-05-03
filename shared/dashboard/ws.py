"""
Module: shared.dashboard.ws
Purpose: WebSocket handlers for real-time dashboard updates.
Location: /opt/tickles/shared/dashboard/ws.py
"""

import asyncio
import logging
import json
from datetime import datetime, timezone
from aiohttp import web

from shared.dashboard.snapshot import get_queue_data

logger = logging.getLogger(__name__)

# Constants
MAX_WS_CLIENTS = 50
TICK_INTERVAL = 5.0

# Connection tracking
active_clients = set()


async def handle_queue_ws(request: web.Request) -> web.WebSocketResponse:
    """
    WebSocket handler for the Live Queue tab.
    Pushes updates every 5 seconds.
    """
    logger.debug("WS request received. Current clients: %d/%d", len(active_clients), MAX_WS_CLIENTS)
    if len(active_clients) >= MAX_WS_CLIENTS:
        logger.warning("WS connection rejected: max clients reached (%d)", MAX_WS_CLIENTS)
        # 503 Service Unavailable is a reasonable fallback for "too many connections"
        # although 1013 is the WS-specific code, aiohttp's WebSocketResponse.prepare
        # happens after the middleware/routing.
        return web.Response(status=503, text="Server busy: too many WebSocket connections")

    ws = web.WebSocketResponse(heartbeat=30.0)
    await ws.prepare(request)

    active_clients.add(ws)
    logger.info("New WS client connected. Total: %d", len(active_clients))

    try:
        while not ws.closed:
            try:
                data = await get_queue_data()

                # Serialise datetimes for JSON
                def serialise(obj):
                    if isinstance(obj, datetime):
                        return obj.isoformat()
                    return obj

                await ws.send_json({
                    "type": "queue_update",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "data": data
                }, dumps=lambda x: json.dumps(x, default=serialise))
            except Exception as e:
                logger.error("Error fetching queue data for WS: %s", e)
                if not ws.closed:
                    await ws.send_json({"type": "error", "message": "Internal error fetching queue data"})

            # Wait for next tick or until closed
            await asyncio.sleep(TICK_INTERVAL)
    except ConnectionResetError:
        logger.debug("WS client reset connection")
    except Exception as e:
        logger.error("WS loop error: %s", e)
    finally:
        active_clients.remove(ws)
        logger.info("WS client disconnected. Total: %d", len(active_clients))

    return ws
