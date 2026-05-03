"""
Module: test_correlation_propagation
Purpose: Verify correlation_id is generated once and threaded through the pipeline.
Location: /opt/tickles/shared/tests/test_correlation_propagation.py
"""

import pytest

from shared.utils.correlation import new_correlation_id


class TestCorrelationId:
    """Correlation ID generation and format tests."""

    def test_returns_string(self) -> None:
        """new_correlation_id must return a non-empty string."""
        cid = new_correlation_id()
        assert isinstance(cid, str)
        assert len(cid) > 0

    def test_prefix_included(self) -> None:
        """Prefix must appear at the start of the ID."""
        cid = new_correlation_id(prefix="sig")
        assert cid.startswith("sig-")

    def test_unique_per_call(self) -> None:
        """100 sequential calls must produce unique IDs."""
        ids = {new_correlation_id() for _ in range(100)}
        assert len(ids) == 100

    def test_no_prefix_when_none(self) -> None:
        """Without prefix, ID must not contain a hyphen."""
        cid = new_correlation_id()
        assert "-" not in cid
