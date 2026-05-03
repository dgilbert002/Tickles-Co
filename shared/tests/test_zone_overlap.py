"""
Module: test_zone_overlap
Purpose: Unit tests for Phase 9 image perceptual hash deduplication.
Location: /opt/tickles/shared/tests/test_zone_overlap.py
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from shared.intelligence.image_phash import (
    compute_phash,
    hamming_distance,
    is_near_duplicate,
)


def test_hamming_distance_identical() -> None:
    """Identical hashes have zero Hamming distance."""
    a = "a" * 16
    b = "a" * 16
    assert hamming_distance(a, b) == 0


def test_hamming_distance_different() -> None:
    """Different hashes have non-zero distance."""
    a = "0" * 16
    b = "f" * 16
    assert hamming_distance(a, b) == 64  # 16 hex chars * 4 bits each, all flipped


def test_is_near_duplicate_threshold() -> None:
    """Near-duplicate detection respects max_hamming threshold."""
    a = "0" * 16
    b = "0" * 15 + "1"  # 1 bit different (0 vs 1)
    assert is_near_duplicate(a, b, max_hamming=6) is True
    assert is_near_duplicate(a, b, max_hamming=0) is False

    # 4 bits different (0 vs f)
    c = "0" * 16
    d = "0" * 15 + "f"
    assert is_near_duplicate(c, d, max_hamming=6) is True
    assert is_near_duplicate(c, d, max_hamming=2) is False


def test_compute_phash_import_error() -> None:
    """Graceful fallback when imagehash/PIL not installed."""
    with patch("shared.intelligence.image_phash._IMAGEHASH_AVAILABLE", False):
        result = compute_phash(Path("/fake/path.png"))
    assert result is None


def test_compute_phash_file_error() -> None:
    """Graceful fallback on file read error."""
    from shared.intelligence import image_phash as ip
    if ip._PILImage is None:
        pytest.skip("PIL not available — cannot test file-error path")
    with patch.object(ip._PILImage, "open", side_effect=OSError("bad file")):
        result = compute_phash(Path("/fake/path.png"))
    assert result is None
