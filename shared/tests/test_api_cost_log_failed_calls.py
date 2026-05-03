"""
Module: test_api_cost_log_failed_calls
Purpose: Verify failed API calls are logged with success=false and http_status populated.
Location: /opt/tickles/shared/tests/test_api_cost_log_failed_calls.py
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.utils.api_cost_log import log_api_call


class TestFailedCallLogging:
    """Failed calls must write a row with success=FALSE and http_status set."""

    @pytest.mark.anyio
    async def test_failed_call_logs_success_false(self) -> None:
        """A call with success=False must be written to api_cost_log."""
        mock_conn = MagicMock()
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        await log_api_call(
            provider="openrouter",
            model="gpt-4",
            role="vision",
            context="test",
            tokens_in=100,
            tokens_out=0,
            cost_usd=Decimal("0.0"),
            latency_ms=500,
            company_id="rubicon",
            operation="vision.complete",
            agent_id="test_agent",
            temperature=0.7,
            correlation_id="test-cid-123",
            request_path="/tmp/test.req.json",
            response_path="/tmp/test.resp.json",
            success=False,
            http_status=429,
            extra={"error": "rate_limited"},
            conn=mock_conn,
        )

        mock_conn.execute.assert_awaited_once()
        call_args = mock_conn.execute.await_args
        assert call_args is not None
        # conn.execute(sql, *values) — skip the SQL string at index 0
        values = call_args[0][1:]
        # success is at index 15 (0-based), http_status at index 16
        assert values[15] is False
        assert values[16] == 429

    @pytest.mark.anyio
    async def test_successful_call_logs_success_true(self) -> None:
        """A successful call must have success=True and http_status=200."""
        mock_conn = MagicMock()
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        await log_api_call(
            provider="openrouter",
            model="gpt-4",
            role="vision",
            context="test",
            tokens_in=1000,
            tokens_out=500,
            cost_usd=Decimal("0.01500000"),
            latency_ms=1200,
            company_id="rubicon",
            operation="vision.complete",
            agent_id="test_agent",
            temperature=0.7,
            correlation_id="test-cid-456",
            request_path="/tmp/test.req.json",
            response_path="/tmp/test.resp.json",
            success=True,
            http_status=200,
            extra=None,
            conn=mock_conn,
        )

        mock_conn.execute.assert_awaited_once()
        call_args = mock_conn.execute.await_args
        # conn.execute(sql, *values) — skip the SQL string at index 0
        values = call_args[0][1:]
        assert values[15] is True
        assert values[16] == 200
