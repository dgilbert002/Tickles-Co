"""
Module: prompt_registry
Purpose: Register every LLM prompt version in tickles_shared.public.prompt_versions.
Location: /opt/tickles/shared/intelligence/prompt_registry.py
"""

import hashlib
import logging
from typing import Optional

from shared.utils.db import get_shared_pool

logger = logging.getLogger(__name__)


def _hash_prompt(system: str | None, body: str, taxonomy_rule: str | None) -> str:
    """Compute a 16-char SHA-256 hash of the prompt content."""
    h = hashlib.sha256()
    h.update((system or "").encode())
    h.update(b"\x1f")
    h.update(body.encode())
    h.update(b"\x1f")
    h.update((taxonomy_rule or "").encode())
    return h.hexdigest()[:16]


async def register_prompt(
    *,
    name: str,
    version: str,
    system: Optional[str],
    body: str,
    taxonomy_rule: Optional[str],
    model_hint: Optional[str],
    created_by: str,
) -> str:
    """Idempotently register a prompt version. Returns the 16-char hash.

    Uses INSERT ... ON CONFLICT (name, version) DO NOTHING so duplicate
    registrations are harmless. The hash is computed from (system, body,
    taxonomy_rule) and can be used to audit whether the prompt body changed
    without bumping the version string.

    Args:
        name: Prompt family name, e.g. 'chart_analysis', 'postmortem'.
        version: Human-readable version, e.g. '2026.04.30-v1'.
        system: System message verbatim (may be None).
        body: User/template body verbatim.
        taxonomy_rule: G1 clause snapshotted at registration (may be None).
        model_hint: Model identifier, e.g. 'requesty/tickles-vision'.
        created_by: Commit author or service name.

    Returns:
        16-character hex hash of the prompt content.
    """
    prompt_hash = _hash_prompt(system, body, taxonomy_rule)
    pool = await get_shared_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO public.prompt_versions
                  (name, version, prompt_hash, system, body, taxonomy_rule, model_hint, created_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (name, version) DO NOTHING
                """,
                name,
                version,
                prompt_hash,
                system,
                body,
                taxonomy_rule,
                model_hint,
                created_by,
            )
    except Exception as exc:
        logger.error("prompt_registry: failed to register %s/%s: %s", name, version, exc)
        raise
    logger.info("prompt_registry: registered %s/%s hash=%s", name, version, prompt_hash)
    return prompt_hash
