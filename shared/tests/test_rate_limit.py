"""
Module: test_rate_limit
Purpose: Unit tests for Phase 9 token-bucket rate limiter.
Location: /opt/tickles/shared/tests/test_rate_limit.py
"""

import asyncio

import pytest

from shared.collectors.rate_limit import TokenBucket, for_source, reset_source


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_token_bucket_acquire_basic() -> None:
    """Basic acquire succeeds when tokens available."""
    bucket = TokenBucket(rate_per_sec=10.0)
    assert await bucket.acquire(n=1.0, timeout=1.0) is True


@pytest.mark.anyio
async def test_token_bucket_acquire_timeout() -> None:
    """Acquire fails when bucket empty and timeout expires."""
    bucket = TokenBucket(rate_per_sec=0.1, burst=1)
    # Drain the single token
    assert await bucket.acquire(n=1.0, timeout=0.1) is True
    # Next acquire should timeout
    assert await bucket.acquire(n=1.0, timeout=0.1) is False


@pytest.mark.anyio
async def test_token_bucket_refills_over_time() -> None:
    """Bucket refills after waiting."""
    bucket = TokenBucket(rate_per_sec=10.0, burst=1)
    # Drain
    assert await bucket.acquire(n=1.0, timeout=0.1) is True
    # Wait for refill
    await asyncio.sleep(0.15)
    assert await bucket.acquire(n=1.0, timeout=0.5) is True


@pytest.mark.anyio
async def test_for_source_creates_bucket() -> None:
    """for_source creates and returns a bucket."""
    b = for_source(source_id=42, rate_per_sec=50.0)
    assert b.rate == 50.0
    assert await b.acquire(n=1.0, timeout=0.1) is True


def test_for_source_rebuilds_on_rate_change() -> None:
    """for_source rebuilds bucket when rate changes."""
    b1 = for_source(source_id=99, rate_per_sec=10.0)
    b2 = for_source(source_id=99, rate_per_sec=20.0)
    assert b2.rate == 20.0
    # Should be a new instance
    assert b1 is not b2


def test_reset_source_removes_bucket() -> None:
    """reset_source removes a bucket from registry."""
    for_source(source_id=77, rate_per_sec=10.0)
    reset_source(77)
    # After reset, next for_source creates a new one
    b = for_source(source_id=77, rate_per_sec=10.0)
    assert b is not None
