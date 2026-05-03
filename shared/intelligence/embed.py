"""
Module: embed
Purpose: Cached sentence-transformer model loader for embedding text.
Location: /opt/tickles/shared/intelligence/embed.py
"""

import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_MODEL_NAME = os.getenv("REASON_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
_DIM = int(os.getenv("REASON_EMBED_DIM", "384"))

_model: Optional[object] = None
_lock = asyncio.Lock()


async def _ensure_model() -> object:
    """Lazy-load the sentence-transformer model once per process."""
    global _model
    if _model is None:
        async with _lock:
            if _model is None:
                try:
                    from sentence_transformers import SentenceTransformer

                    _model = await asyncio.to_thread(SentenceTransformer, _MODEL_NAME)
                    logger.info("embed: loaded model %s", _MODEL_NAME)
                except Exception as exc:
                    logger.error("embed: failed to load model %s: %s", _MODEL_NAME, exc)
                    raise
    return _model


async def embed(text: str) -> list[float]:
    """Return a unit-normalised embedding vector for the given text.

    Args:
        text: Input text to embed.

    Returns:
        List of floats (length == REASON_EMBED_DIM, default 384).
        Returns a zero vector if text is empty/None.
    """
    if not text:
        return [0.0] * _DIM
    m = await _ensure_model()
    try:
        vec = await asyncio.to_thread(m.encode, text, normalize_embeddings=True)
        result = vec.tolist()
        if len(result) != _DIM:
            raise RuntimeError(
                f"Embedding dimension mismatch: got {len(result)}, expected {_DIM}"
            )
        return result
    except Exception as exc:
        logger.error("embed: encoding failed: %s", exc)
        raise
