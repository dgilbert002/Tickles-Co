"""
Module: test_intelligence
Purpose: Smoke tests for shared/mcp/tools/intelligence.py (Phase 3B+).
Location: /opt/tickles/shared/mcp/tools/test_intelligence.py

Tests the synchronous helper functions and handler logic without
requiring a running MCP daemon or OpenRouter API key.

Run:  python -m pytest shared/mcp/tools/test_intelligence.py -v
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.mcp.protocol import McpTool
from shared.mcp.registry import ToolRegistry
from shared.mcp.tools.context import ToolContext
from shared.mcp.tools.intelligence import (
    _CHART_HACKER_MODEL_FALLBACK,
    _CHART_HACKER_MODEL_PRIMARY,
    _default_company,
    _estimate_cost_usd,
    _extract_json_block,
    _fmt_ts,
    _handle_reinterpret,
    _image_to_base64,
    _now_iso,
    register,
)
from shared.intelligence.reinterpret_legs import build_legs_from_trades
from shared.intelligence.interpretation_service import (
    LlmResult,
    QuantResult,
    ConsensusResult,
    build_reinterpret_response,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool(name: str = "test.tool", group: str = "test") -> McpTool:
    """Create a minimal McpTool for testing."""
    return McpTool(
        name=name,
        description=f"Tool {name}",
        version="1",
        input_schema={"type": "object", "properties": {}},
        read_only=True,
        enabled=True,
        tags={"phase": "3b", "group": group},
    )


# ---------------------------------------------------------------------------
# Unit tests — pure helpers
# ---------------------------------------------------------------------------


class TestDefaultCompany:
    """Tests for _default_company helper."""

    def test_returns_param(self) -> None:
        result = _default_company({"companyId": "acme"})
        assert result == "acme"

    def test_returns_default(self) -> None:
        result = _default_company({})
        assert result == "jarvais"

    def test_coerces_int(self) -> None:
        result = _default_company({"companyId": 42})
        assert result == "42"


class TestFmtTs:
    """Tests for _fmt_ts timestamp formatter."""

    def test_none_returns_none(self) -> None:
        assert _fmt_ts(None) is None

    def test_datetime_to_iso(self) -> None:
        dt = datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)
        assert _fmt_ts(dt) == "2026-04-21T12:00:00+00:00"

    def test_string_passthrough(self) -> None:
        assert _fmt_ts("2026-04-21") == "2026-04-21"

    def test_int_converts(self) -> None:
        assert _fmt_ts(123) == "123"


class TestNowIso:
    """Tests for _now_iso helper."""

    def test_returns_iso_string(self) -> None:
        result = _now_iso()
        assert result.endswith("+00:00")
        assert "T" in result


class TestImageToBase64:
    """Tests for _image_to_base64 helper."""

    def test_png(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n")
            path = f.name
        try:
            uri = _image_to_base64(path)
            assert uri.startswith("data:image/png;base64,")
            # Verify base64 decodes back
            b64_part = uri.split(",")[1]
            decoded = base64.b64decode(b64_part)
            assert decoded == b"\x89PNG\r\n\x1a\n"
        finally:
            os.unlink(path)

    def test_jpg(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"\xff\xd8\xff")
            path = f.name
        try:
            uri = _image_to_base64(path)
            assert uri.startswith("data:image/jpeg;base64,")
        finally:
            os.unlink(path)

    def test_webp(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".webp", delete=False) as f:
            f.write(b"RIFF")
            path = f.name
        try:
            uri = _image_to_base64(path)
            assert uri.startswith("data:image/webp;base64,")
        finally:
            os.unlink(path)


class TestExtractJsonBlock:
    """Tests for _extract_json_block helper."""

    def test_fenced_json(self) -> None:
        text = '```json\n{"direction": "long", "confidence": 0.8}\n```'
        result = _extract_json_block(text)
        assert result == {"direction": "long", "confidence": 0.8}

    def test_raw_json(self) -> None:
        text = '{"direction": "short", "confidence": 0.6}'
        result = _extract_json_block(text)
        assert result == {"direction": "short", "confidence": 0.6}

    def test_embedded_json(self) -> None:
        text = 'Some text before {"direction": "neutral"} and after'
        result = _extract_json_block(text)
        assert result == {"direction": "neutral"}

    def test_invalid_returns_none(self) -> None:
        text = "not json at all"
        result = _extract_json_block(text)
        assert result is None

    def test_empty_returns_none(self) -> None:
        result = _extract_json_block("")
        assert result is None


class TestEstimateCostUsd:
    """Tests for _estimate_cost_usd helper."""

    def test_claude_sonnet(self) -> None:
        cost = _estimate_cost_usd("anthropic/claude-sonnet-4", 1_000_000, 1_000_000)
        assert cost == 18.0  # 3 + 15

    def test_gemini_flash(self) -> None:
        cost = _estimate_cost_usd("google/gemini-2.0-flash-001", 1_000_000, 1_000_000)
        assert cost == 0.5  # 0.10 + 0.40

    def test_unknown_model_uses_default(self) -> None:
        cost = _estimate_cost_usd("unknown/model", 1_000_000, 1_000_000)
        assert cost == 25.0  # 5 + 20

    def test_zero_tokens(self) -> None:
        cost = _estimate_cost_usd("anthropic/claude-sonnet-4", 0, 0)
        assert cost == 0.0


# ---------------------------------------------------------------------------
# Async handler tests — mocked
# ---------------------------------------------------------------------------


class TestHandleChartAnalyze:
    """Tests for _handle_chart_analyze with mocked vision API."""

    def test_missing_image_path(self) -> None:
        import asyncio
        from shared.mcp.tools.intelligence import _handle_chart_analyze

        result = asyncio.run(_handle_chart_analyze({"imagePath": "/nonexistent.png"}))
        assert result["ok"] is False
        assert "not found" in result["error"]

    def test_successful_analysis(self) -> None:
        import asyncio
        from shared.mcp.tools.intelligence import _handle_chart_analyze

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n")
            path = f.name

        mock_response = {
            "text": '```json\n{"direction": "long", "confidence": 0.85, "reasoning": "Broke resistance", "levels": {"entry": "65000", "stop_loss": "64000", "take_profit": "70000"}, "timeframe": "1h", "pattern_detected": "ascending_triangle"}\n```',
            "model": "anthropic/claude-sonnet-4",
            "tokens_in": 1500,
            "tokens_out": 200,
            "cost_usd": 0.0075,
        }

        try:
            with patch(
                "shared.mcp.tools.intelligence._call_openrouter_vision",
                new_callable=AsyncMock,
                return_value=mock_response,
            ):
                result = asyncio.run(
                    _handle_chart_analyze(
                        {"imagePath": path, "symbol": "BTCUSDT"}
                    )
                )

            assert result["ok"] is True
            assert result["direction"] == "long"
            assert result["confidence"] == 0.85
            assert result["pattern_detected"] == "ascending_triangle"
            assert result["model_used"] == "anthropic/claude-sonnet-4"
            assert result["tokens_in"] == 1500
            assert result["cost_usd"] == 0.0075
        finally:
            os.unlink(path)

    def test_fallback_model_on_failure(self) -> None:
        import asyncio
        from shared.mcp.tools.intelligence import _handle_chart_analyze

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n")
            path = f.name

        mock_response = {
            "text": '{"direction": "short", "confidence": 0.7}',
            "model": "google/gemini-2.0-flash-001",
            "tokens_in": 800,
            "tokens_out": 100,
            "cost_usd": 0.00012,
        }

        try:
            with patch(
                "shared.mcp.tools.intelligence._call_openrouter_vision",
                new_callable=AsyncMock,
                side_effect=[
                    RuntimeError("Primary model down"),
                    mock_response,
                ],
            ):
                result = asyncio.run(
                    _handle_chart_analyze(
                        {"imagePath": path, "symbol": "ETHUSDT"}
                    )
                )

            assert result["ok"] is True
            assert result["direction"] == "short"
            assert result["model_used"] == "google/gemini-2.0-flash-001"
        finally:
            os.unlink(path)

    def test_all_models_fail(self) -> None:
        import asyncio
        from shared.mcp.tools.intelligence import _handle_chart_analyze

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n")
            path = f.name

        try:
            with patch(
                "shared.mcp.tools.intelligence._call_openrouter_vision",
                new_callable=AsyncMock,
                side_effect=RuntimeError("All models down"),
            ):
                result = asyncio.run(_handle_chart_analyze({"imagePath": path}))

            assert result["ok"] is False
            assert "All vision models failed" in result["error"]
        finally:
            os.unlink(path)


class TestHandleSignalsPending:
    """Tests for _handle_signals_pending with mocked DB."""

    def test_pending_count(self) -> None:
        import asyncio
        from shared.mcp.tools.intelligence import _handle_signals_pending

        mock_row = {"pending_count": 42, "oldest_age_seconds": 3600.0}
        mock_pool = MagicMock()
        mock_pool.fetch_one = AsyncMock(return_value=mock_row)

        with patch(
            "shared.mcp.tools.intelligence._get_pool",
            new_callable=AsyncMock,
            return_value=mock_pool,
        ):
            result = asyncio.run(_handle_signals_pending({}))

        assert result["ok"] is True
        assert result["pending_count"] == 42
        assert result["oldest_age_seconds"] == 3600.0

    def test_with_filters(self) -> None:
        import asyncio
        from shared.mcp.tools.intelligence import _handle_signals_pending

        mock_row = {"pending_count": 5, "oldest_age_seconds": 120.0}
        mock_pool = MagicMock()
        mock_pool.fetch_one = AsyncMock(return_value=mock_row)

        with patch(
            "shared.mcp.tools.intelligence._get_pool",
            new_callable=AsyncMock,
            return_value=mock_pool,
        ):
            result = asyncio.run(
                _handle_signals_pending(
                    {"sourceId": 123, "mediaType": "image/png"}
                )
            )

        assert result["ok"] is True
        assert result["pending_count"] == 5
        # Verify the query was called with correct args
        call_args = mock_pool.fetch_one.call_args
        assert "source_id = $1" in call_args[0][0]
        assert "media_type = $2" in call_args[0][0]

    def test_db_error(self) -> None:
        import asyncio
        from shared.mcp.tools.intelligence import _handle_signals_pending

        mock_pool = MagicMock()
        mock_pool.fetch_one = AsyncMock(side_effect=Exception("DB timeout"))

        with patch(
            "shared.mcp.tools.intelligence._get_pool",
            new_callable=AsyncMock,
            return_value=mock_pool,
        ):
            result = asyncio.run(_handle_signals_pending({}))

        assert result["ok"] is False
        assert "DB timeout" in result["error"]


class TestHandleTraderProfile:
    """Tests for _handle_trader_profile with mocked DB."""

    def test_create_profile(self) -> None:
        import asyncio
        from shared.mcp.tools.intelligence import _handle_trader_profile

        mock_row = {"id": 42, "created_at": _now_iso()}
        mock_pool = MagicMock()
        mock_pool.fetch_one = AsyncMock(return_value=mock_row)

        with patch(
            "shared.mcp.tools.intelligence._get_pool",
            new_callable=AsyncMock,
            return_value=mock_pool,
        ):
            result = asyncio.run(
                _handle_trader_profile(
                    {
                        "platform": "discord",
                        "handle": "@CryptoWhale",
                        "displayName": "Crypto Whale",
                        "traderType": "swing",
                    }
                )
            )

        assert result["ok"] is True
        assert result["profile_id"] == 42
        assert result["handle_normalized"] == "cryptowhale"

    def test_missing_platform(self) -> None:
        import asyncio
        from shared.mcp.tools.intelligence import _handle_trader_profile

        result = asyncio.run(_handle_trader_profile({"handle": "test"}))
        assert result["ok"] is False
        assert "platform and handle are required" in result["error"]

    def test_missing_handle(self) -> None:
        import asyncio
        from shared.mcp.tools.intelligence import _handle_trader_profile

        result = asyncio.run(_handle_trader_profile({"platform": "discord"}))
        assert result["ok"] is False
        assert "platform and handle are required" in result["error"]


class TestHandleTraderScore:
    """Tests for _handle_trader_score with mocked DB."""

    def test_found_score(self) -> None:
        import asyncio
        from shared.mcp.tools.intelligence import _handle_trader_score

        mock_row = {
            "accuracy_pct": 0.65,
            "avg_confidence": 0.72,
            "confidence_calibration": 0.93,
            "total_signals": 100,
            "validated_signals": 80,
            "correct_direction": 52,
            "total_pnl_usd": 1250.50,
            "avg_pnl_per_signal": 15.63,
            "max_win_usd": 450.0,
            "max_loss_usd": -120.0,
            "sharpe_ratio": 1.2,
            "max_drawdown_pct": -8.5,
            "scored_at": datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc),
        }
        mock_pool = MagicMock()
        mock_pool.fetch_one = AsyncMock(return_value=mock_row)

        with patch(
            "shared.mcp.tools.intelligence._get_company_pool",
            new_callable=AsyncMock,
            return_value=mock_pool,
        ):
            result = asyncio.run(
                _handle_trader_score(
                    {"profileId": 42, "scorePeriod": "30d"}
                )
            )

        assert result["ok"] is True
        assert result["found"] is True
        assert result["accuracy_pct"] == 0.65
        assert result["sharpe_ratio"] == 1.2
        assert result["max_drawdown_pct"] == -8.5

    def test_not_found(self) -> None:
        import asyncio
        from shared.mcp.tools.intelligence import _handle_trader_score

        mock_pool = MagicMock()
        mock_pool.fetch_one = AsyncMock(return_value=None)

        with patch(
            "shared.mcp.tools.intelligence._get_company_pool",
            new_callable=AsyncMock,
            return_value=mock_pool,
        ):
            result = asyncio.run(_handle_trader_score({"profileId": 99}))

        assert result["ok"] is True
        assert result["found"] is False
        assert result["profile_id"] == 99

    def test_missing_profile_id(self) -> None:
        import asyncio
        from shared.mcp.tools.intelligence import _handle_trader_score

        result = asyncio.run(_handle_trader_score({}))
        assert result["ok"] is False
        assert "profileId is required" in result["error"]


class TestHandleSignalsRecent:
    """Tests for _handle_signals_recent with mocked DB."""

    def test_list_signals(self) -> None:
        import asyncio
        from shared.mcp.tools.intelligence import _handle_signals_recent

        mock_rows = [
            {
                "id": 1,
                "news_item_id": 100,
                "media_item_id": 200,
                "trader_profile_id": 42,
                "model_version": "claude-sonnet-4",
                "param_hash": "abc123",
                "consensus_direction": "long",
                "consensus_confidence": 0.85,
                "consensus_method": "llm_priority",
                "instrument_symbol": "BTCUSDT",
                "exchange": "bybit",
                "market_data_fresh": True,
                "market_data_at": datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc),
                "created_at": datetime(2026, 4, 26, 12, 5, 0, tzinfo=timezone.utc),
            }
        ]
        mock_pool = MagicMock()
        mock_pool.fetch_all = AsyncMock(return_value=mock_rows)

        with patch(
            "shared.mcp.tools.intelligence._get_company_pool",
            new_callable=AsyncMock,
            return_value=mock_pool,
        ):
            result = asyncio.run(_handle_signals_recent({"limit": 10}))

        assert result["ok"] is True
        assert result["count"] == 1
        assert len(result["signals"]) == 1
        sig = result["signals"][0]
        assert sig["consensus_direction"] == "long"
        assert sig["consensus_confidence"] == 0.85
        assert sig["instrument_symbol"] == "BTCUSDT"

    def test_with_filters(self) -> None:
        import asyncio
        from shared.mcp.tools.intelligence import _handle_signals_recent

        mock_pool = MagicMock()
        mock_pool.fetch_all = AsyncMock(return_value=[])

        with patch(
            "shared.mcp.tools.intelligence._get_company_pool",
            new_callable=AsyncMock,
            return_value=mock_pool,
        ):
            result = asyncio.run(
                _handle_signals_recent(
                    {
                        "direction": "long",
                        "minConfidence": 0.7,
                        "profileId": 42,
                        "since": "2026-04-26T00:00:00Z",
                    }
                )
            )

        assert result["ok"] is True
        assert result["count"] == 0
        # Verify query contains all filter conditions
        call_args = mock_pool.fetch_all.call_args
        sql = call_args[0][0]
        assert "consensus_direction = $2" in sql
        assert "consensus_confidence >= $3" in sql
        assert "trader_profile_id = $4" in sql
        assert "created_at >= $1" in sql


# ---------------------------------------------------------------------------
# Registration test
# ---------------------------------------------------------------------------


class TestRegister:
    """Tests for register() function."""

    def test_registers_all_tools(self) -> None:
        reg = ToolRegistry()
        ctx = ToolContext()
        register(reg, ctx)

        tools = reg.list_tools()
        names = [t.name for t in tools]

        # Phase 3B tools
        assert "intelligence.chart_analyze" in names
        assert "intelligence.reinterpret" in names
        assert "intelligence.interpret" in names
        assert "intelligence.trader_profile" in names
        assert "intelligence.trader_score" in names
        assert "intelligence.signals.recent" in names
        assert "intelligence.signals.pending" in names
        # Phase 3C tools
        assert "intelligence.positions.open" in names
        assert "intelligence.positions.history" in names
        assert "intelligence.traders.leaderboard" in names
        assert "intelligence.epic.resolve" in names
        assert len(names) == 12


class TestBuildReinterpretResponse:
    """Full-schema serializer matches Lens prompt contract."""

    def test_includes_dual_tracks_and_parsed_superset(self) -> None:
        llm = LlmResult(
            direction="short",
            confidence=0.82,
            reasoning="legacy slice",
            levels={"entry": 75185.0, "stop_loss": 76801.0, "take_profit": 65471.0},
            instrument="BTC/USDT",
            timeframe="6h",
            trader_trades=[
                {
                    "direction": "short",
                    "entry": 75185.0,
                    "stop_loss": 76801.0,
                    "tp1": 65471.0,
                    "evidence": "position_box",
                    "confidence": 0.85,
                },
                {
                    "direction": "short",
                    "entry": 73500.0,
                    "stop_loss": 74200.0,
                    "tp1": 71000.0,
                    "evidence": "position_box",
                    "confidence": 0.8,
                },
            ],
            chart_hacker_trades=[
                {
                    "direction": "short",
                    "entry": 75185.0,
                    "evidence": "inferred",
                    "confidence": 0.7,
                },
            ],
            chart_analysis={"market_structure": "range"},
            prompt_version="db:2026.05.30-discord-semantic-v8",
            prompt_hash="abc123",
            raw_response='{"reasoning":"full reasoning"}',
        )
        parsed = {
            "instrument": "BTCUSD",
            "timeframe": "6h",
            "setup_state": "actionable",
            "trader_sentiment": 0.2,
            "chart_hacker_sentiment": -0.1,
            "ai_agreement_with_trader": 0.75,
            "ai_comment_on_trader": "agrees on first short",
            "reasoning": "full reasoning",
            "extra_future_field": "preserved",
        }
        quant = QuantResult(direction="short", confidence=0.6, indicators={"rsi": 55})
        consensus = ConsensusResult(
            direction="short", confidence=0.78, method="agreement",
            llm_result=llm, quant_result=quant,
        )
        out = build_reinterpret_response(
            llm, parsed=parsed, consensus=consensus, quant=quant,
            meta={"media_id": 6442},
        )
        assert out["ok"] is True
        assert len(out["trader_trades"]) == 2
        assert len(out["chart_hacker_trades"]) == 1
        assert out["setup_state"] == "actionable"
        assert out["parsed"]["extra_future_field"] == "preserved"
        assert out["consensus"]["method"] == "agreement"
        assert out["quant"]["indicators"]["rsi"] == 55
        assert out["meta"]["media_id"] == 6442


class TestReinterpretPersistFlags:
    """MCP persist/rearm guard rails."""

    @pytest.mark.asyncio
    async def test_persist_requires_interpretation_id(self) -> None:
        out = await _handle_reinterpret({"mediaId": 1, "persist": True})
        assert out["ok"] is False
        assert "interpretationId" in out["error"]

    @pytest.mark.asyncio
    async def test_rearm_requires_interpretation_id(self) -> None:
        out = await _handle_reinterpret({"imagePath": "/tmp/x.png", "rearm": True})
        assert out["ok"] is False
        assert "interpretationId" in out["error"]


class TestReinterpretLegs:
    """Per-leg symbol and timeframe normalization."""

    def test_btcusd_maps_to_usdt_for_candles(self) -> None:
        legs = build_legs_from_trades(
            trader_trades=[{"direction": "short", "entry": 75000, "symbol": "BTCUSD", "timeframe": "6H"}],
            chart_hacker_trades=[],
            default_symbol="BTC/USDT",
            default_exchange="bybit",
            default_timeframe="6h",
        )
        assert len(legs) == 1
        assert legs[0]["symbol"] == "BTC/USDT"
        assert legs[0]["timeframe"] == "6h"
        assert legs[0]["timeframe_source"] == "6H"

    def test_tools_have_correct_tags(self) -> None:
        reg = ToolRegistry()
        ctx = ToolContext()
        register(reg, ctx)

        for tool in reg.list_tools():
            assert tool.tags.get("phase") in ("3b", "3c", "8")
            assert tool.tags.get("group") == "intelligence"


# ---------------------------------------------------------------------------
# Integration guard — skip if no DB
# ---------------------------------------------------------------------------


def _db_available() -> bool:
    """Check if Postgres is reachable for integration tests."""
    try:
        import asyncpg

        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _db_available(), reason="asyncpg not installed")
@pytest.mark.integration
class TestIntegrationGuard:
    """Placeholder for future integration tests against real DB."""

    pass
