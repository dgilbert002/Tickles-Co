"""
Module: reason_similarity
Purpose: Compute cosine similarity between trader and LLM entry reasons using pgvector.
Location: /opt/tickles/shared/intelligence/reason_similarity.py
"""

import asyncio
import logging
from typing import Optional

from shared.intelligence.embed import embed

logger = logging.getLogger(__name__)


async def compute_reason_agreement(trader: Optional[str], llm: Optional[str]) -> Optional[float]:
    """Compute cosine similarity between two reason strings.

    Both strings are embedded with the cached sentence-transformer model
    (unit-normalised). The dot product of two unit vectors equals cosine
    similarity, range [-1.0, 1.0].

    Args:
        trader: Trader's stated reason (may be None/empty).
        llm: LLM's inferred reason (may be None/empty).

    Returns:
        Cosine similarity score, or None if either input is missing.
    """
    if not trader or not llm:
        return None
    try:
        a, b = await asyncio.gather(embed(trader), embed(llm))
        # Both are unit-normalised -> dot product == cosine similarity
        score = float(sum(x * y for x, y in zip(a, b)))
        return score
    except Exception as exc:
        logger.warning("reason_similarity: failed to compute agreement: %s", exc)
        return None
