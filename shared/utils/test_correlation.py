"""
Module: test_correlation
Purpose: Smoke tests for shared.utils.correlation.
Location: /opt/tickles/shared/utils/test_correlation.py
"""

import re

import pytest

from shared.utils.correlation import new_correlation_id


def test_new_correlation_id_default() -> None:
    """Default call returns 12-char hex string."""
    cid = new_correlation_id()
    assert len(cid) == 12
    assert re.fullmatch(r"[0-9a-f]{12}", cid) is not None


def test_new_correlation_id_with_prefix() -> None:
    """Prefix is prepended with a hyphen separator."""
    cid = new_correlation_id("sig")
    assert cid.startswith("sig-")
    assert len(cid) == 16  # "sig-" + 12 hex chars
    assert re.fullmatch(r"sig-[0-9a-f]{12}", cid) is not None


def test_new_correlation_id_uniqueness() -> None:
    """1000 sequential calls produce unique IDs."""
    ids = {new_correlation_id() for _ in range(1000)}
    assert len(ids) == 1000


def test_new_correlation_id_prefix_variations() -> None:
    """Different prefixes produce different prefixes but same hex length."""
    for prefix in ("sig", "pm", "ch", ""):
        cid = new_correlation_id(prefix)
        if prefix:
            assert cid.startswith(f"{prefix}-")
            assert len(cid) == len(prefix) + 1 + 12
        else:
            assert len(cid) == 12
