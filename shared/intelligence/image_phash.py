"""
Module: image_phash
Purpose: Phase 9 perceptual hash for image deduplication across collectors.
Location: /opt/tickles/shared/intelligence/image_phash.py
"""

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Module-level import attempt so tests can patch attributes
try:
    import imagehash as _imagehash  # type: ignore[import-untyped]
    import PIL.Image as _PILImage  # type: ignore[import-untyped]
    _IMAGEHASH_AVAILABLE = True
except ImportError:
    _imagehash = None  # type: ignore[misc]
    _PILImage = None  # type: ignore[misc]
    _IMAGEHASH_AVAILABLE = False


def compute_phash(image_path: Path) -> Optional[str]:
    """Compute 16-char hex perceptual hash of an image.

    Near-duplicates collide at hamming-distance <= 6.

    Args:
        image_path: Path to the image file.

    Returns:
        16-character hex string, or None if imagehash/PIL unavailable.
    """
    if not _IMAGEHASH_AVAILABLE:
        logger.warning("imagehash or PIL not installed")
        return None

    try:
        with _PILImage.open(image_path) as img:
            return str(_imagehash.phash(img))
    except Exception as exc:
        logger.warning("Failed to compute phash for %s: %s", image_path, exc)
        return None


def hamming_distance(a: str, b: str) -> int:
    """Compute Hamming distance between two hex phash strings."""
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def is_near_duplicate(a: str, b: str, *, max_hamming: int = 6) -> bool:
    """Check if two perceptual hashes are near-duplicates.

    Args:
        a: First 16-char hex phash.
        b: Second 16-char hex phash.
        max_hamming: Maximum Hamming distance to consider a match.

    Returns:
        True if the images are likely near-duplicates.
    """
    return hamming_distance(a, b) <= max_hamming
