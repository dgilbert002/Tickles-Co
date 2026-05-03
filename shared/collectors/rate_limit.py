"""
Module: rate_limit
Purpose: Phase 9 per-source token-bucket rate limiter for collectors.
Location: /opt/tickles/shared/collectors/rate_limit.py
"""

import asyncio
import logging
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class TokenBucket:
    """Per-source rate limiter; non-blocking on tokens, blocks-with-timeout when empty."""

    def __init__(self, rate_per_sec: float, burst: Optional[int] = None) -> None:
        """Initialize token bucket.

        Args:
            rate_per_sec: Tokens added per second.
            burst: Maximum bucket capacity. Defaults to max(rate*2, 10).
        """
        self.rate = float(rate_per_sec)
        self.capacity = float(burst if burst is not None else max(rate_per_sec * 2, 10))
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, n: float = 1.0, *, timeout: float = 30.0) -> bool:
        """Attempt to acquire n tokens from the bucket.

        Args:
            n: Number of tokens to acquire (default 1.0 per message).
            timeout: Maximum seconds to wait for tokens.

        Returns:
            True if tokens were acquired, False if timeout expired.
        """
        deadline = time.monotonic() + timeout
        while True:
            async with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity,
                    self._tokens + (now - self._last) * self.rate,
                )
                self._last = now
                if self._tokens >= n:
                    self._tokens -= n
                    return True
                wait = (n - self._tokens) / self.rate
            if time.monotonic() + wait > deadline:
                return False
            await asyncio.sleep(min(wait, 0.5))


# Per-source registry
_buckets: Dict[int, TokenBucket] = {}


def for_source(source_id: int, rate_per_sec: float) -> TokenBucket:
    """Get or create a TokenBucket for a collector source.

    Rebuilds the bucket if rate_per_sec has changed.

    Args:
        source_id: Collector catalog source ID.
        rate_per_sec: Desired rate limit for this source.

    Returns:
        TokenBucket instance for the source.
    """
    b = _buckets.get(source_id)
    if b is None or b.rate != rate_per_sec:
        b = _buckets[source_id] = TokenBucket(rate_per_sec)
    return b


def reset_source(source_id: int) -> None:
    """Remove a source's bucket (e.g., on config reload)."""
    _buckets.pop(source_id, None)
