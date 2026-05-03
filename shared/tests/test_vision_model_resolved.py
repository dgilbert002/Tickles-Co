"""
Module: test_vision_model_resolved
Purpose: Phase 3 benchmark — vision_model_resolved is set even when waterfall picks primary.
Location: /opt/tickles/shared/tests/test_vision_model_resolved.py
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from shared.intelligence.gateway_config import call_vision_llm, GatewayConfig


def _make_mock_session(response_data: dict) -> MagicMock:
    """Build a mock aiohttp ClientSession that yields the given response data."""
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=response_data)
    mock_response.headers = {"x-request-id": "req-123"}

    mock_post_ctx = MagicMock()
    mock_post_ctx.__aenter__ = AsyncMock(return_value=mock_response)
    mock_post_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.post = MagicMock(return_value=mock_post_ctx)
    return mock_session


class TestVisionModelResolved:
    """Verify gateway returns resolved model in response."""

    @pytest.mark.anyio
    async def test_resolved_model_returned(self):
        """[R] call_vision_llm must return resolved model from response."""
        cfg = GatewayConfig(
            gateway="openrouter",
            api_key="sk-test",
            base_url="https://openrouter.ai/api/v1",
            service_name="test",
            default_model="tickles-vision",
            fallback_model="gpt-4o",
            temperature=0.7,
        )

        mock_session = _make_mock_session({
            "model": "anthropic/claude-sonnet-4",
            "choices": [{"message": {"content": "test"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        })

        with patch("aiohttp.ClientSession", return_value=mock_session):
            with patch("shared.intelligence.gateway_config._log_call", new_callable=AsyncMock):
                with patch("shared.intelligence.gateway_config._check_loop", new_callable=AsyncMock):
                    result = await call_vision_llm(
                        cfg,
                        model="tickles-vision",
                        system_prompt="You are a trading analyst",
                        user_text="Describe this chart",
                        image_b64="iVBORw0KGgo=",
                        image_mime="image/png",
                    )

        assert result["model"] == "anthropic/claude-sonnet-4"

    @pytest.mark.anyio
    async def test_resolved_model_defaults_to_requested_when_missing(self):
        """[R] If response has no model field, default to model_requested."""
        cfg = GatewayConfig(
            gateway="openrouter",
            api_key="sk-test",
            base_url="https://openrouter.ai/api/v1",
            service_name="test",
            default_model="tickles-vision",
            fallback_model="gpt-4o",
            temperature=0.7,
        )

        mock_session = _make_mock_session({
            "choices": [{"message": {"content": "test"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        })

        with patch("aiohttp.ClientSession", return_value=mock_session):
            with patch("shared.intelligence.gateway_config._log_call", new_callable=AsyncMock):
                with patch("shared.intelligence.gateway_config._check_loop", new_callable=AsyncMock):
                    result = await call_vision_llm(
                        cfg,
                        model="tickles-vision",
                        system_prompt="You are a trading analyst",
                        user_text="Describe this chart",
                        image_b64="iVBORw0KGgo=",
                        image_mime="image/png",
                    )

        assert result["model"] == "tickles-vision"
