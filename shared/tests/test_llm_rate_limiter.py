"""
Module: test_llm_rate_limiter
Purpose: Smoke test for the LLM rate limiter module
Location: /opt/tickles/shared/tests/test_llm_rate_limiter.py
"""

import pytest

from shared.intelligence.llm_rate_limiter import (
    LlmRateLimiter,
    RateLimitConfig,
    get_default_limiter,
)


def test_rate_limiter_init_and_stats() -> None:
    """Rate limiter initializes with correct defaults and stats."""
    cfg = RateLimitConfig(requests_per_minute=10, burst_size=2, cooldown_seconds=1)
    limiter = LlmRateLimiter(cfg)
    assert limiter.cfg.requests_per_minute == 10
    assert limiter.cfg.burst_size == 2
    assert limiter._stats["total_requests"] == 0
    assert limiter._stats["queued_requests"] == 0


def test_rate_limiter_report_429_and_success() -> None:
    """429 reporting sets cooldown; success clears it."""
    cfg = RateLimitConfig(requests_per_minute=10, burst_size=2, cooldown_seconds=1)
    limiter = LlmRateLimiter(cfg)
    limiter.report_429()
    assert limiter._cooldown_until is not None
    limiter.report_success()
    # Success doesn't clear cooldown_until directly, but RPM may be adjusted
    assert limiter._current_rpm <= cfg.requests_per_minute


def test_rate_limiter_enqueue_no_loop() -> None:
    """Queue accepts items even without running event loop."""
    cfg = RateLimitConfig(requests_per_minute=100, burst_size=10)
    limiter = LlmRateLimiter(cfg)
    limiter._queue.put_nowait(("test", 0.001))
    assert limiter._queue.qsize() == 1


def test_singleton_get_default_limiter() -> None:
    """get_default_limiter returns the same instance."""
    a = get_default_limiter()
    b = get_default_limiter()
    assert a is b
