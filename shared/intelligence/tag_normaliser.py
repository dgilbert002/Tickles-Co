"""
Module: tag_normaliser
Purpose: Stub for Phase 11 tag clustering. Exports threshold constants now.
Location: /opt/tickles/shared/intelligence/tag_normaliser.py
"""

import logging
import os

logger = logging.getLogger(__name__)

TAG_CLUSTER_COSINE_THRESHOLD = float(os.getenv("TAG_CLUSTER_COSINE_THRESHOLD", "0.78"))
TAG_BORDERLINE_BAND = float(os.getenv("TAG_BORDERLINE_BAND", "0.05"))


def cluster_tags(tags: list[str]) -> dict[str, list[str]]:
    """Phase 11: replace stub with real sentence-transformer + agglomerative clustering.

    Phase 6 returns identity mapping so call-sites can be wired without
    behaviour change.

    Args:
        tags: List of free-form tag strings.

    Returns:
        Dict mapping each tag to a singleton list containing itself.
    """
    return {t: [t] for t in tags}
