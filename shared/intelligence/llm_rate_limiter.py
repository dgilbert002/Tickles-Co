"""
Module: llm_rate_limiter
Purpose: Token-bucket + queue-based rate limiter for LLM API calls.
         Prevents blind retries and handles 429 responses with gradual slowdown.
Location: /opt/tickles/shared/intelligence/llm_rate_limiter.py

Design:
  * Token bucket: controls requests per minute (default 60).
  * Queue: FIFO for pending requests, ordered by arrival time.
  * 429 handling: on receiving HTTP 429, reduce rate by 50% for cooldown period.
  * No blind retries: each request gets max 1 retry on transient failure.
  * Cost tracking: accumulates estimated USD spend per model.
  * Thread-safe / async-safe.

Usage:
    from shared.intelligence.llm_rate_limiter import LlmRateLimiter
    limiter = LlmRateLimiter(requests_per_minute=60)
    async with limiter.acquire(model="claude-sonnet-4", estimated_cost=0.005):
        result = await call_llm(...)
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("tickles.intelligence.llm_rate_limiter")


@dataclass
class RateLimitConfig:
    """Configuration for the LLM rate limiter."""

    requests_per_minute: float = 60.0
    burst_size: int = 5
    cooldown_seconds: float = 60.0
    max_queue_size: int = 500
    retry_once: bool = True


@dataclass
class LlmRequest:
    """A queued LLM request."""

    id: str
    model: str
    estimated_cost_usd: float
    created_at: float
    future: asyncio.Future


class LlmRateLimiter:
    """Token-bucket rate limiter with queue and 429 cooldown."""

    def __init__(self, cfg: Optional[RateLimitConfig] = None) -> None:
        self.cfg = cfg or RateLimitConfig()
        self._tokens: float = self.cfg.burst_size
        self._last_refill: float = time.monotonic()
        self._lock = asyncio.Lock()
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=self.cfg.max_queue_size)
        self._cooldown_until: float = 0.0
        self._current_rpm: float = self.cfg.requests_per_minute
        self._stats: Dict[str, Any] = {
            "total_requests": 0,
            "queued_requests": 0,
            "dropped_requests": 0,
            "rate_limited_429s": 0,
            "total_cost_usd": 0.0,
            "by_model": {},
        }
        self._processor_task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        """Start the background queue processor."""
        if self._processor_task is None or self._processor_task.done():
            self._processor_task = asyncio.create_task(self._process_queue())
            logger.info("LLM rate limiter started (RPM=%.1f)", self._current_rpm)

    async def stop(self) -> None:
        """Stop the background processor and drain queue."""
        self._stop.set()
        if self._processor_task:
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass
        # Cancel any pending futures in queue
        while not self._queue.empty():
            try:
                req = self._queue.get_nowait()
                if not req.future.done():
                    req.future.set_exception(RuntimeError("Rate limiter stopped"))
            except asyncio.QueueEmpty:
                break
        logger.info("LLM rate limiter stopped")

    def _refill_tokens(self) -> None:
        """Refill token bucket based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._last_refill = now
        # During cooldown, use reduced RPM
        effective_rpm = self._current_rpm
        if now < self._cooldown_until:
            effective_rpm = self._current_rpm * 0.5
        tokens_to_add = elapsed * (effective_rpm / 60.0)
        self._tokens = min(self.cfg.burst_size, self._tokens + tokens_to_add)

    async def acquire(self, model: str = "unknown", estimated_cost_usd: float = 0.0) -> None:
        """Acquire a token to make an LLM request. Blocks until token available.

        Args:
            model: Model identifier for cost tracking.
            estimated_cost_usd: Estimated cost of this request.
        """
        async with self._lock:
            self._refill_tokens()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                self._stats["total_requests"] += 1
                self._stats["total_cost_usd"] += estimated_cost_usd
                self._stats.setdefault("by_model", {}).setdefault(model, {"count": 0, "cost": 0.0})
                self._stats["by_model"][model]["count"] += 1
                self._stats["by_model"][model]["cost"] += estimated_cost_usd
                return

        # No token available — wait
        wait_time = 60.0 / max(self._current_rpm, 1.0)
        logger.debug("Rate limit: waiting %.1fs for token (model=%s)", wait_time, model)
        await asyncio.sleep(wait_time)

        # Retry acquire
        async with self._lock:
            self._refill_tokens()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                self._stats["total_requests"] += 1
                self._stats["total_cost_usd"] += estimated_cost_usd
                self._stats.setdefault("by_model", {}).setdefault(model, {"count": 0, "cost": 0.0})
                self._stats["by_model"][model]["count"] += 1
                self._stats["by_model"][model]["cost"] += estimated_cost_usd
                return
            else:
                # Still no token — this shouldn't happen often
                logger.warning("Rate limit: token still unavailable after wait")
                raise RuntimeError(
                    f"Rate limit exceeded for model {model}. "
                    f"RPM={self._current_rpm:.1f}, tokens={self._tokens:.2f}"
                )

    def report_429(self) -> None:
        """Report a 429 response — triggers cooldown and RPM reduction."""
        now = time.monotonic()
        self._cooldown_until = now + self.cfg.cooldown_seconds
        self._current_rpm = max(1.0, self._current_rpm * 0.5)
        self._stats["rate_limited_429s"] += 1
        logger.warning(
            "LLM 429 received — cooldown for %.0fs, RPM reduced to %.1f",
            self.cfg.cooldown_seconds,
            self._current_rpm,
        )

    def report_success(self) -> None:
        """Report successful request — gradually restore RPM if above base."""
        if self._current_rpm < self.cfg.requests_per_minute:
            self._current_rpm = min(
                self.cfg.requests_per_minute,
                self._current_rpm * 1.1,
            )

    def get_stats(self) -> Dict[str, Any]:
        """Return current rate limiter statistics."""
        return dict(self._stats)

    async def _process_queue(self) -> None:
        """Background task that drains the queue."""
        while not self._stop.is_set():
            try:
                req = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            try:
                await self.acquire(model=req.model, estimated_cost_usd=req.estimated_cost_usd)
                if not req.future.done():
                    req.future.set_result(True)
            except Exception as e:
                if not req.future.done():
                    req.future.set_exception(e)

    async def enqueue(self, model: str = "unknown", estimated_cost_usd: float = 0.0) -> None:
        """Enqueue a request to be processed when a token is available.

        Args:
            model: Model identifier.
            estimated_cost_usd: Estimated cost.

        Raises:
            RuntimeError: If queue is full.
        """
        if self._queue.qsize() >= self.cfg.max_queue_size:
            self._stats["dropped_requests"] += 1
            raise RuntimeError(f"LLM queue full (max {self.cfg.max_queue_size})")

        req = LlmRequest(
            id=f"req_{time.monotonic():.6f}",
            model=model,
            estimated_cost_usd=estimated_cost_usd,
            created_at=time.monotonic(),
            future=asyncio.get_event_loop().create_future(),
        )
        self._stats["queued_requests"] += 1
        await self._queue.put(req)
        await req.future


# Global singleton instance (lazy-initialized)
_default_limiter: Optional[LlmRateLimiter] = None
_default_lock = asyncio.Lock()


def get_default_limiter() -> LlmRateLimiter:
    """Get or create the default global rate limiter (synchronous).

    The returned limiter may not have its background queue processor started.
    Call await limiter.start() before using enqueue() if queue processing is needed.
    For direct acquire() calls, start() is optional.
    """
    global _default_limiter
    if _default_limiter is None:
        rpm = float(__import__("os").environ.get("LLM_RATE_LIMIT_RPM", "60"))
        _default_limiter = LlmRateLimiter(RateLimitConfig(requests_per_minute=rpm))
    return _default_limiter
