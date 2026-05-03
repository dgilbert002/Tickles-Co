"""
Module: test_prompt_registry
Purpose: Unit tests for shared.intelligence.prompt_registry.
Location: /opt/tickles/shared/tests/test_prompt_registry.py
"""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "/opt/tickles")

from shared.intelligence.prompt_registry import _hash_prompt, register_prompt


def test_hash_prompt_deterministic():
    h1 = _hash_prompt("system", "body", "taxonomy")
    h2 = _hash_prompt("system", "body", "taxonomy")
    assert h1 == h2
    assert len(h1) == 16


def test_hash_prompt_different_inputs():
    h1 = _hash_prompt("a", "b", "c")
    h2 = _hash_prompt("a", "b", "d")
    assert h1 != h2


import asyncio


def test_register_prompt_idempotent():
    mock_conn = MagicMock()
    mock_conn.execute = AsyncMock()

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock()
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch("shared.intelligence.prompt_registry.get_shared_pool", new_callable=AsyncMock) as mock_get_pool:
        mock_get_pool.return_value = mock_pool
        result = asyncio.run(
            register_prompt(
                name="test",
                version="v1",
                system="sys",
                body="body",
                taxonomy_rule="tax",
                model_hint="model",
                created_by="test",
            )
        )

    assert len(result) == 16
    mock_conn.execute.assert_called_once()
