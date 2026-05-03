"""
Module: test_gateway_config
Purpose: Smoke tests for shared.intelligence.gateway_config Phase 1 extensions.
Location: /opt/tickles/shared/intelligence/test_gateway_config.py
"""

import os
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.intelligence.gateway_config import (
    GatewayConfig,
    _build_headers,
    _check_loop,
    _log_call,
    call_vision_llm,
    chat_completion,
    get_gateway_for_service,
)


# ---------------------------------------------------------------------------
# GatewayConfig
# ---------------------------------------------------------------------------

def test_gateway_config_defaults() -> None:
    """Default GatewayConfig has sensible defaults."""
    cfg = GatewayConfig(
        gateway="openrouter",
        api_key="sk-test",
        base_url="https://openrouter.ai/api/v1",
        service_name="test",
        default_model="claude-sonnet-4",
        fallback_model="gemini-2.0-flash",
    )
    assert cfg.max_tokens == 2048
    assert cfg.timeout_s == 60
    assert cfg.temperature == 0.7
    assert cfg.cost_log_enabled is True


def test_gateway_config_for_service_openrouter() -> None:
    """for_service resolves to openrouter when env says so."""
    env = {
        "LLM_GATEWAY_DEFAULT": "openrouter",
        "OPENROUTER_API_KEY": "sk-or-test",
        "OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1",
    }
    with patch.dict(os.environ, env, clear=True):
        cfg = GatewayConfig.for_service("interpretation")
        assert cfg.gateway == "openrouter"
        assert cfg.api_key == "sk-or-test"
        assert cfg.base_url == "https://openrouter.ai/api/v1"
        assert cfg.service_name == "interpretation"


def test_gateway_config_for_service_requesty() -> None:
    """for_service resolves to requesty with correct default URL."""
    env = {
        "LLM_GATEWAY_DEFAULT": "requesty",
        "REQUESTY_API": "rqsty-sk-test",
    }
    with patch.dict(os.environ, env, clear=True):
        cfg = GatewayConfig.for_service("mcp")
        assert cfg.gateway == "requesty"
        assert cfg.api_key == "rqsty-sk-test"
        # Phase 1 fix: default must be router.requesty.ai, NOT api.requesty.ai
        assert cfg.base_url == "https://router.requesty.ai/v1"
        assert cfg.service_name == "mcp"


def test_gateway_config_temperature_override() -> None:
    """Per-service temperature override is respected."""
    env = {
        "LLM_GATEWAY_DEFAULT": "openrouter",
        "OPENROUTER_API_KEY": "sk-test",
        "LLM_TEMPERATURE_INTERPRETATION": "0.3",
    }
    with patch.dict(os.environ, env, clear=True):
        cfg = GatewayConfig.for_service("interpretation")
        assert cfg.temperature == 0.3


def test_gateway_config_temperature_invalid_fallback() -> None:
    """Invalid temperature string falls back to 0.7."""
    env = {
        "LLM_GATEWAY_DEFAULT": "openrouter",
        "OPENROUTER_API_KEY": "sk-test",
        "LLM_TEMPERATURE_INTERPRETATION": "hot",
    }
    with patch.dict(os.environ, env, clear=True):
        cfg = GatewayConfig.for_service("interpretation")
        assert cfg.temperature == 0.7


def test_get_gateway_for_service_alias() -> None:
    """get_gateway_for_service is an alias for GatewayConfig.for_service."""
    env = {
        "LLM_GATEWAY_DEFAULT": "openrouter",
        "OPENROUTER_API_KEY": "sk-test",
    }
    with patch.dict(os.environ, env, clear=True):
        cfg1 = GatewayConfig.for_service("guru")
        cfg2 = get_gateway_for_service("guru")
        assert cfg1.gateway == cfg2.gateway
        assert cfg1.service_name == cfg2.service_name


# ---------------------------------------------------------------------------
# _build_headers
# ---------------------------------------------------------------------------

def test_build_headers_openrouter() -> None:
    """OpenRouter headers include referer and title."""
    cfg = GatewayConfig(
        gateway="openrouter",
        api_key="sk-test",
        base_url="https://openrouter.ai/api/v1",
        service_name="test",
        default_model="claude",
        fallback_model="gemini",
    )
    headers = _build_headers(cfg)
    assert headers["Authorization"] == "Bearer sk-test"
    assert headers["Content-Type"] == "application/json"
    assert headers["HTTP-Referer"] == "https://tickles.co"
    assert headers["X-Title"] == "TicklesChartHacker"


def test_build_headers_requesty() -> None:
    """Requesty headers omit referer/title."""
    cfg = GatewayConfig(
        gateway="requesty",
        api_key="rqsty-sk-test",
        base_url="https://router.requesty.ai/v1",
        service_name="test",
        default_model="gpt-4o",
        fallback_model="gpt-4o-mini",
    )
    headers = _build_headers(cfg)
    assert headers["Authorization"] == "Bearer rqsty-sk-test"
    assert "HTTP-Referer" not in headers
    assert "X-Title" not in headers


# ---------------------------------------------------------------------------
# _check_loop
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_check_loop_disabled() -> None:
    """When LLM_LOOP_DETECTOR_ENABLED=false, returns immediately."""
    with patch("shared.intelligence.gateway_config._LOOP_DETECTOR_ENABLED", False):
        cfg = GatewayConfig(
            gateway="openrouter",
            api_key="sk-test",
            base_url="https://openrouter.ai/api/v1",
            service_name="test",
            default_model="claude",
            fallback_model="gemini",
        )
        # Should not raise
        await _check_loop(cfg, "claude", "sys", "user", "cid-123", "op")


@pytest.mark.anyio
async def test_check_loop_enabled_no_anomaly() -> None:
    """When detector is enabled and no anomaly, returns without raising."""
    with patch("shared.intelligence.gateway_config._LOOP_DETECTOR_ENABLED", True):
        cfg = GatewayConfig(
            gateway="openrouter",
            api_key="sk-test",
            base_url="https://openrouter.ai/api/v1",
            service_name="test",
            default_model="claude",
            fallback_model="gemini",
        )
        # First call should never be an anomaly
        await _check_loop(cfg, "claude", "sys", "user", "cid-123", "op")


@pytest.mark.anyio
async def test_check_loop_raises_on_anomaly() -> None:
    """When detector reports anomaly, raises RuntimeError."""
    with patch("shared.intelligence.gateway_config._LOOP_DETECTOR_ENABLED", True):
        cfg = GatewayConfig(
            gateway="openrouter",
            api_key="sk-test",
            base_url="https://openrouter.ai/api/v1",
            service_name="test",
            default_model="claude",
            fallback_model="gemini",
        )
        # Simulate anomaly by mocking LoopDetector at its source module
        with patch("shared.intelligence.loop_detector.LoopDetector") as MockLD:
            instance = MagicMock()
            instance.record.return_value = (True, "identical_loop")
            MockLD.return_value = instance
            with pytest.raises(RuntimeError, match="LLM loop/burst detected"):
                await _check_loop(cfg, "claude", "sys", "user", "cid-123", "op")


# ---------------------------------------------------------------------------
# _log_call
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_log_call_disabled() -> None:
    """When cost_log_enabled=False, returns without writing."""
    cfg = GatewayConfig(
        gateway="openrouter",
        api_key="sk-test",
        base_url="https://openrouter.ai/api/v1",
        service_name="test",
        default_model="claude",
        fallback_model="gemini",
        cost_log_enabled=False,
    )
    with patch("shared.utils.api_cost_log.log_api_call") as mock_log:
        await _log_call(
            cfg=cfg,
            model="claude",
            correlation_id="cid-123",
            operation="test",
            start_ts=0.0,
            data={},
            resp_status=200,
            success=True,
        )
        mock_log.assert_not_awaited()


@pytest.mark.anyio
async def test_log_call_enabled() -> None:
    """When cost_log_enabled=True, calls log_api_call."""
    cfg = GatewayConfig(
        gateway="openrouter",
        api_key="sk-test",
        base_url="https://openrouter.ai/api/v1",
        service_name="test_svc",
        default_model="claude",
        fallback_model="gemini",
        cost_log_enabled=True,
        temperature=0.5,
    )
    with patch("shared.utils.api_cost_log.log_api_call", new_callable=AsyncMock) as mock_log:
        await _log_call(
            cfg=cfg,
            model="claude-sonnet-4",
            correlation_id="sig-a1b2c3d4e5f6",
            operation="vision_primary",
            start_ts=0.0,
            data={"model": "claude-sonnet-4", "usage": {"prompt_tokens": 100}},
            resp_status=200,
            success=True,
            company_id="rubicon",
            agent_id="chart_hacker_01",
        )
        mock_log.assert_awaited_once()
        call_kwargs = mock_log.await_args.kwargs
        assert call_kwargs["provider"] == "openrouter"
        assert call_kwargs["model"] == "claude-sonnet-4"
        assert call_kwargs["role"] == "test_svc"
        assert call_kwargs["correlation_id"] == "sig-a1b2c3d4e5f6"
        assert call_kwargs["operation"] == "vision_primary"
        assert call_kwargs["company_id"] == "rubicon"
        assert call_kwargs["agent_id"] == "chart_hacker_01"
        assert call_kwargs["temperature"] == 0.5


# ---------------------------------------------------------------------------
# call_vision_llm / chat_completion — missing API key
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_call_vision_llm_missing_key() -> None:
    """Missing API key raises RuntimeError before any HTTP call."""
    cfg = GatewayConfig(
        gateway="openrouter",
        api_key="",
        base_url="https://openrouter.ai/api/v1",
        service_name="test",
        default_model="claude",
        fallback_model="gemini",
    )
    with pytest.raises(RuntimeError, match="API key not set"):
        await call_vision_llm(
            cfg=cfg,
            model="claude",
            system_prompt="sys",
            user_text="user",
            image_b64="b64",
            image_mime="image/png",
        )


@pytest.mark.anyio
async def test_chat_completion_missing_key() -> None:
    """Missing API key raises RuntimeError before any HTTP call."""
    cfg = GatewayConfig(
        gateway="requesty",
        api_key="",
        base_url="https://router.requesty.ai/v1",
        service_name="test",
        default_model="gpt-4o",
        fallback_model="gpt-4o-mini",
    )
    with pytest.raises(RuntimeError, match="API key not set"):
        await chat_completion(
            cfg=cfg,
            model="gpt-4o",
            system_prompt="sys",
            user_text="user",
        )
