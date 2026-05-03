"""
Module: rate_limit
Purpose: Token-bucket rate limiter for manage panel routes.
Location: /opt/tickles/shared/dashboard/rate_limit.py
"""

import logging
import os
from collections import defaultdict
from time import monotonic
from typing import Callable, Awaitable

from aiohttp import web

logger = logging.getLogger(__name__)

_READ_CAP = int(os.environ.get("MANAGE_RATE_READ", "600"))
_WRITE_CAP = int(os.environ.get("MANAGE_RATE_WRITE", "30"))

_BUCKETS: dict[tuple[str, str], tuple[float, float]] = defaultdict(lambda: (0.0, 0.0))
# key = (session_token_or_ip, "read"|"write"); value = (tokens, last_refill_ts)

_CAPS = {
    "read":  (_READ_CAP, _READ_CAP / 60.0),
    "write": (_WRITE_CAP, _WRITE_CAP / 60.0),
}


def rate_limit(kind: str, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]) -> Callable:
    """Token-bucket rate limiter decorator.

    Args:
        kind: "read" or "write" — selects cap and refill rate.
        handler: The aiohttp handler to wrap.

    Returns:
        Wrapped handler with rate limiting.
    """
    cap, refill_rate = _CAPS[kind]

    async def wrapped(request: web.Request) -> web.StreamResponse:
        sess = request.get("session_token") or request.get("user", {}).get("chat_id", "anon")
        if sess == "anon":
            # Fallback to client IP for unauthenticated requests (should be rare after auth middleware)
            peer = request.transport.get_extra_info("peername") if request.transport else None
            sess = peer[0] if peer else "unknown"

        tokens, last = _BUCKETS[(sess, kind)]
        now = monotonic()
        tokens = min(cap, tokens + (now - last) * refill_rate)
        if tokens < 1:
            retry_after = str(int(1.0 / refill_rate) + 1)
            logger.warning("Rate limit hit: kind=%s session=%s path=%s", kind, sess, request.path)
            return web.json_response(
                {"error": "rate_limited", "kind": kind},
                status=429,
                headers={"Retry-After": retry_after},
            )
        _BUCKETS[(sess, kind)] = (tokens - 1, now)
        return await handler(request)
    return wrapped


def reset_buckets() -> None:
    """Clear all rate-limit buckets. Used in tests."""
    _BUCKETS.clear()
