"""
Module: test_api_cost_log
Purpose: Smoke tests for shared.utils.api_cost_log.
Location: /opt/tickles/shared/utils/test_api_cost_log.py
"""

from decimal import Decimal
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.utils.api_cost_log import _estimate_cost_usd, _extract_tokens, log_api_call


# ---------------------------------------------------------------------------
# _estimate_cost_usd
# ---------------------------------------------------------------------------

def test_estimate_cost_known_model() -> None:
    """Known model uses its pricing table."""
    cost = _estimate_cost_usd("anthropic/claude-sonnet-4", 1_000_000, 500_000)
    # (1M * 3.0 + 0.5M * 15.0) / 1M = 3.0 + 7.5 = 10.5
    assert cost == Decimal("10.50000000")


def test_estimate_cost_unknown_model_fallback() -> None:
    """Unknown model falls back to default pricing (3.0, 15.0)."""
    cost = _estimate_cost_usd("unknown/model", 1_000_000, 0)
    assert cost == Decimal("3.00000000")


def test_estimate_cost_zero_tokens() -> None:
    """Zero tokens yields zero cost."""
    cost = _estimate_cost_usd("google/gemini-2.0-flash-001", 0, 0)
    assert cost == Decimal("0.00000000")


# ---------------------------------------------------------------------------
# _extract_tokens
# ---------------------------------------------------------------------------

def test_extract_tokens_openrouter_shape() -> None:
    """OpenRouter-style usage dict with prompt_tokens / completion_tokens."""
    usage: Dict[str, Any] = {"prompt_tokens": 100, "completion_tokens": 50}
    inp, out = _extract_tokens(usage)
    assert inp == 100
    assert out == 50


def test_extract_tokens_requesty_shape() -> None:
    """Requesty-style usage dict with input_tokens / output_tokens."""
    usage: Dict[str, Any] = {"input_tokens": 200, "output_tokens": 75}
    inp, out = _extract_tokens(usage)
    assert inp == 200
    assert out == 75


def test_extract_tokens_none() -> None:
    """None usage returns (0, 0)."""
    inp, out = _extract_tokens(None)
    assert inp == 0
    assert out == 0


def test_extract_tokens_empty() -> None:
    """Empty dict returns (0, 0)."""
    inp, out = _extract_tokens({})
    assert inp == 0
    assert out == 0


def test_extract_tokens_total_tokens_fallback() -> None:
    """When only total_tokens is present, treat it as input."""
    usage: Dict[str, Any] = {"total_tokens": 300}
    inp, out = _extract_tokens(usage)
    assert inp == 300
    assert out == 0


# ---------------------------------------------------------------------------
# log_api_call
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_pool():
    """Return a mock asyncpg pool with an async context manager."""
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=async_context_manager(conn))
    return pool


def async_context_manager(mock_conn):
    """Helper to build an async context manager around a mock connection."""
    class _ACM:
        async def __aenter__(self):
            return mock_conn
        async def __aexit__(self, *args):
            pass
    return _ACM()


@pytest.mark.anyio
async def test_log_api_call_disabled() -> None:
    """When API_COST_LOG_ENABLED=false, returns True without DB write."""
    with patch("shared.utils.api_cost_log._COST_LOG_ENABLED", False):
        result = await log_api_call(
            provider="openrouter",
            model="claude-sonnet-4",
            role="test",
        )
        assert result is True


@pytest.mark.anyio
async def test_log_api_call_with_conn(mock_pool) -> None:
    """When conn is provided, writes directly without acquiring pool."""
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    with patch("shared.utils.api_cost_log._COST_LOG_ENABLED", True):
        result = await log_api_call(
            provider="openrouter",
            model="claude-sonnet-4",
            role="test",
            tokens_in=100,
            tokens_out=50,
            latency_ms=250,
            correlation_id="sig-a1b2c3d4e5f6",
            operation="vision_primary",
            conn=conn,
        )
        assert result is True
        conn.execute.assert_awaited_once()


@pytest.mark.anyio
async def test_log_api_call_estimates_cost_when_none(mock_pool) -> None:
    """When cost_usd is None, estimates from tokens + model."""
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    with patch("shared.utils.api_cost_log._COST_LOG_ENABLED", True):
        result = await log_api_call(
            provider="openrouter",
            model="anthropic/claude-sonnet-4",
            role="test",
            tokens_in=1_000_000,
            tokens_out=500_000,
            cost_usd=None,
            conn=conn,
        )
        assert result is True
        # Verify the call was made — conn.execute(sql, provider, model, ...)
        call_args = conn.execute.await_args
        assert call_args is not None
        # args[0] = SQL, args[1] = provider, args[2] = model, args[3] = role,
        # args[4] = context, args[5] = tokens_in, args[6] = tokens_out, args[7] = cost_usd
        assert call_args.args[7] == Decimal("10.50000000")


@pytest.mark.anyio
async def test_log_api_call_extracts_from_usage() -> None:
    """When usage dict is provided, tokens are extracted from it."""
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    with patch("shared.utils.api_cost_log._COST_LOG_ENABLED", True):
        result = await log_api_call(
            provider="requesty",
            model="gpt-4o",
            role="test",
            usage={"prompt_tokens": 200, "completion_tokens": 100},
            conn=conn,
        )
        assert result is True
        call_args = conn.execute.await_args
        # args[5] = tokens_in, args[6] = tokens_out
        assert call_args.args[5] == 200
        assert call_args.args[6] == 100


@pytest.mark.anyio
async def test_log_api_call_failure_returns_false() -> None:
    """When DB write raises, returns False and logs warning."""
    conn = AsyncMock()
    conn.execute = AsyncMock(side_effect=RuntimeError("DB down"))
    with patch("shared.utils.api_cost_log._COST_LOG_ENABLED", True):
        result = await log_api_call(
            provider="openrouter",
            model="claude-sonnet-4",
            role="test",
            conn=conn,
        )
        assert result is False
