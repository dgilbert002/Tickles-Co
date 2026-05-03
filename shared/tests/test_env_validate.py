"""
Module: test_env_validate
Purpose: Verify _validate_env.py passes for both providers.
Location: /opt/tickles/shared/tests/test_env_validate.py
"""

import os
import pytest

from shared.utils._validate_env import validate_env_for_provider, EnvValidationError


class TestValidateEnv:
    """Phase 1 benchmark: _validate_env.py passes for both providers."""

    def test_openrouter_required_keys_present(self, monkeypatch):
        """[R] Validate env with LLM_GATEWAY_DEFAULT=openrouter."""
        monkeypatch.setenv("LLM_GATEWAY_DEFAULT", "openrouter")
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-openrouter")
        monkeypatch.setenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        monkeypatch.setenv("DB_HOST", "127.0.0.1")
        monkeypatch.setenv("DB_PORT", "5432")
        monkeypatch.setenv("DB_USER", "admin")
        monkeypatch.setenv("DB_PASSWORD", "test")
        monkeypatch.setenv("DB_NAME_SHARED", "tickles_shared")
        # Should not raise
        validate_env_for_provider("openrouter")

    def test_requesty_required_keys_present(self, monkeypatch):
        """[R] Validate env with LLM_GATEWAY_DEFAULT=requesty."""
        monkeypatch.setenv("LLM_GATEWAY_DEFAULT", "requesty")
        monkeypatch.setenv("REQUESTY_API_KEY", "sk-test-requesty")
        monkeypatch.setenv("REQUESTY_BASE_URL", "https://router.requesty.ai/v1")
        monkeypatch.setenv("DB_HOST", "127.0.0.1")
        monkeypatch.setenv("DB_PORT", "5432")
        monkeypatch.setenv("DB_USER", "admin")
        monkeypatch.setenv("DB_PASSWORD", "test")
        monkeypatch.setenv("DB_NAME_SHARED", "tickles_shared")
        # Should not raise
        validate_env_for_provider("requesty")

    def test_openrouter_missing_key_raises(self, monkeypatch):
        """[R] Missing OPENROUTER_API_KEY must raise."""
        monkeypatch.setenv("LLM_GATEWAY_DEFAULT", "openrouter")
        monkeypatch.setenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setenv("DB_HOST", "127.0.0.1")
        monkeypatch.setenv("DB_PORT", "5432")
        monkeypatch.setenv("DB_USER", "admin")
        monkeypatch.setenv("DB_PASSWORD", "test")
        monkeypatch.setenv("DB_NAME_SHARED", "tickles_shared")
        with pytest.raises(EnvValidationError):
            validate_env_for_provider("openrouter")

    def test_requesty_missing_key_raises(self, monkeypatch):
        """[R] Missing REQUESTY_API_KEY must raise."""
        monkeypatch.setenv("LLM_GATEWAY_DEFAULT", "requesty")
        monkeypatch.setenv("REQUESTY_BASE_URL", "https://router.requesty.ai/v1")
        monkeypatch.delenv("REQUESTY_API_KEY", raising=False)
        monkeypatch.delenv("REQUESTY_API", raising=False)
        monkeypatch.delenv("TICKLES_APP_VISION_API_KEY", raising=False)
        monkeypatch.setenv("DB_HOST", "127.0.0.1")
        monkeypatch.setenv("DB_PORT", "5432")
        monkeypatch.setenv("DB_USER", "admin")
        monkeypatch.setenv("DB_PASSWORD", "test")
        monkeypatch.setenv("DB_NAME_SHARED", "tickles_shared")
        with pytest.raises(EnvValidationError):
            validate_env_for_provider("requesty")
