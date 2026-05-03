"""
Module: test_payload_store
Purpose: Tests for shared/intelligence/payload_store.py
Location: /opt/tickles/shared/intelligence/test_payload_store.py
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from shared.intelligence.payload_store import (
    _redact_secrets,
    build_payload_paths,
    build_request_payload,
    build_response_payload,
    compute_prompt_hash,
    compute_prompt_version,
    save_payload_pair,
)


class TestRedactSecrets:
    def test_redacts_authorization(self) -> None:
        payload = {"headers": {"Authorization": "Bearer sk-12345", "Content-Type": "application/json"}}
        result = _redact_secrets(payload)
        assert result["headers"]["Authorization"] == "[REDACTED]"
        assert result["headers"]["Content-Type"] == "application/json"

    def test_redacts_nested_api_key(self) -> None:
        payload = {"config": {"api_key": "secret123", "model": "gpt-4"}}
        result = _redact_secrets(payload)
        assert result["config"]["api_key"] == "[REDACTED]"
        assert result["config"]["model"] == "gpt-4"

    def test_redacts_case_insensitive(self) -> None:
        payload = {"X-API-KEY": "secret", "token": "abc"}
        result = _redact_secrets(payload)
        assert result["X-API-KEY"] == "[REDACTED]"
        assert result["token"] == "[REDACTED]"

    def test_leaves_normal_fields(self) -> None:
        payload = {"model": "claude-sonnet", "temperature": 0.7}
        result = _redact_secrets(payload)
        assert result["model"] == "claude-sonnet"
        assert result["temperature"] == 0.7

    def test_redacts_in_list(self) -> None:
        payload = [{"api_key": "secret"}, {"foo": "bar"}]
        result = _redact_secrets(payload)
        assert result[0]["api_key"] == "[REDACTED]"
        assert result[1]["foo"] == "bar"


class TestComputePromptHash:
    def test_deterministic(self) -> None:
        h1 = compute_prompt_hash("system", "user")
        h2 = compute_prompt_hash("system", "user")
        assert h1 == h2
        assert len(h1) == 16

    def test_different_inputs_different_hashes(self) -> None:
        h1 = compute_prompt_hash("sys1", "user1")
        h2 = compute_prompt_hash("sys2", "user2")
        assert h1 != h2


class TestComputePromptVersion:
    def test_extracts_version(self) -> None:
        cfg = {"chart_analysis": {"version": "2026.04.30-v1"}}
        assert compute_prompt_version(cfg) == "2026.04.30-v1"

    def test_fallback_to_hash(self) -> None:
        cfg = {"chart_analysis": {"system_prompt": "You are an analyst."}}
        v = compute_prompt_version(cfg)
        assert len(v) == 12

    def test_empty_fallback(self) -> None:
        cfg = {}
        v = compute_prompt_version(cfg)
        assert len(v) == 12


class TestBuildPayloadPaths:
    def test_date_bucketed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            req, resp = build_payload_paths("abc123", base_dir=base)
            assert req.name == "abc123.req.json"
            assert resp.name == "abc123.resp.json"
            # Should be under YYYY/MM/DD
            parts = req.parts
            assert len(parts) >= 4
            assert parts[-4].isdigit() and len(parts[-4]) == 4  # year


class TestBuildRequestPayload:
    def test_basic_structure(self) -> None:
        p = build_request_payload(
            provider="openrouter",
            model_requested="anthropic/claude-sonnet-4",
            system_prompt="You are a chart analyst.",
            user_text="Analyze BTCUSDT.",
        )
        assert p["provider"] == "openrouter"
        assert p["model_requested"] == "anthropic/claude-sonnet-4"
        assert p["system_prompt"] == "You are a chart analyst."
        assert p["user_text"] == "Analyze BTCUSDT."
        assert "timestamp_utc" in p

    def test_image_b64_hash(self) -> None:
        p = build_request_payload(
            provider="openrouter",
            model_requested="gemini",
            system_prompt="sys",
            user_text="user",
            image_b64="aGVsbG8=",  # base64 "hello"
        )
        assert "image_b64_sha256" in p
        assert p["image_b64_length"] == 8
        assert p["image_b64_sha256"] != "aGVsbG8="  # Should be hash, not raw

    def test_extra_merged(self) -> None:
        p = build_request_payload(
            provider="p",
            model_requested="m",
            system_prompt="s",
            user_text="u",
            extra={"correlation_id": "cid123"},
        )
        assert p["extra"]["correlation_id"] == "cid123"


class TestBuildResponsePayload:
    def test_basic_structure(self) -> None:
        p = build_response_payload(
            model_resolved="anthropic/claude-sonnet-4-20250501",
            content='{"direction": "long"}',
            usage={"prompt_tokens": 100, "completion_tokens": 50},
            latency_ms=1234,
        )
        assert p["model_resolved"] == "anthropic/claude-sonnet-4-20250501"
        assert p["content"] == '{"direction": "long"}'
        assert p["usage"]["prompt_tokens"] == 100
        assert p["latency_ms"] == 1234
        assert "timestamp_utc" in p


class TestSavePayloadPair:
    @pytest.mark.anyio
    async def test_saves_both_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            req_path, resp_path = await save_payload_pair(
                correlation_id="test_cid_001",
                request_payload={"model": "gpt-4"},
                response_payload={"content": "hello"},
                base_dir=base,
            )
            assert Path(req_path).exists()
            assert Path(resp_path).exists()
            with open(req_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            assert data["model"] == "gpt-4"

    @pytest.mark.anyio
    async def test_redacts_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            req_path, _ = await save_payload_pair(
                correlation_id="test_cid_002",
                request_payload={"api_key": "secret123", "model": "gpt-4"},
                response_payload={"content": "ok"},
                base_dir=base,
            )
            with open(req_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            assert data["api_key"] == "[REDACTED]"
            assert data["model"] == "gpt-4"

    @pytest.mark.anyio
    async def test_exceeds_max_bytes_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            huge = {"data": "x" * 10000}
            with pytest.raises(ValueError):
                await save_payload_pair(
                    correlation_id="test_cid_003",
                    request_payload=huge,
                    response_payload={"ok": True},
                    max_bytes=100,
                    base_dir=base,
                )

    @pytest.mark.anyio
    async def test_uses_env_base_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch(
                "shared.intelligence.payload_store._PAYLOAD_BASE_DIR", tmp_path
            ):
                req_path, resp_path = await save_payload_pair(
                    correlation_id="test_cid_004",
                    request_payload={"a": 1},
                    response_payload={"b": 2},
                )
                assert Path(req_path).exists()
                assert str(Path(req_path)).startswith(tmp)
