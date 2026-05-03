"""
Module: test_reason_similarity
Purpose: Verify cosine similarity computation for reason agreement.
Location: /opt/tickles/shared/tests/test_reason_similarity.py
"""

import pytest
from shared.intelligence.reason_similarity import compute_reason_agreement


@pytest.mark.anyio
async def test_cosine_range() -> None:
    """Score must be in [-1.0, 1.0] for two real strings."""
    score = await compute_reason_agreement("buy because RSI oversold", "long on oversold momentum")
    assert score is not None
    assert -1.0 <= score <= 1.0


@pytest.mark.anyio
async def test_none_when_either_empty() -> None:
    """None when either side is NULL/empty."""
    assert await compute_reason_agreement(None, "reason") is None
    assert await compute_reason_agreement("reason", None) is None
    assert await compute_reason_agreement("", "reason") is None
    assert await compute_reason_agreement("reason", "") is None


@pytest.mark.anyio
async def test_semantic_similarity_high() -> None:
    """[AU] Paraphrased reasons score > 0.75 (was < 0.20 under pg_trgm)."""
    score = await compute_reason_agreement(
        "RSI oversold bounce long",
        "RSI oversold bounce entry long",
    )
    assert score is not None
    assert score > 0.75, f"Expected > 0.75, got {score}"


@pytest.mark.anyio
async def test_different_reasons_low() -> None:
    """Unrelated reasons should score low."""
    score = await compute_reason_agreement(
        "buying because RSI oversold",
        "selling because of macro inflation fears",
    )
    assert score is not None
    assert score < 0.5, f"Expected < 0.5, got {score}"
