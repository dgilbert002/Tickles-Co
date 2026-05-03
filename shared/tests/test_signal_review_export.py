"""
Module: test_signal_review_export
Purpose: Tests for signal_review_export.py — thumbnail policy, CSV, atomic swap, truncation.
Location: /opt/tickles/shared/tests/test_signal_review_export.py
"""

import base64
import csv
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from shared.intelligence.signal_review_export import (
    CSV_COLUMNS,
    HTML_AGGREGATE_MAX_BYTES,
    THUMB_INLINE_MAX_BYTES,
    THUMB_LINK_MAX_BYTES,
    SignalReviewExporter,
    _assert_symlink_integrity,
    _render_html,
    _write_csv,
    _write_html,
    render_thumb,
)


# ---------------------------------------------------------------------------
# render_thumb policy tests [AM]
# ---------------------------------------------------------------------------
class TestRenderThumbPolicy:
    """Tests for the 3-tier thumbnail size-cap policy."""

    def test_missing_path(self) -> None:
        """Missing path returns placeholder."""
        html = render_thumb(None)
        assert "missing" in html

    def test_nonexistent_file(self) -> None:
        """Nonexistent file returns placeholder."""
        html = render_thumb("/tmp/does_not_exist_12345.png")
        assert "missing" in html

    def test_small_inline_base64(self, tmp_path: Path) -> None:
        """Files under 200 KB render as inline base64."""
        img = tmp_path / "small.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 100)
        html = render_thumb(str(img))
        assert "data:image/png;base64," in html
        assert "thumb-inline" in html

    def test_medium_lazy_link(self, tmp_path: Path) -> None:
        """Files 200 KB–5 MB render as file:// link with lazy loading."""
        img = tmp_path / "medium.png"
        size = THUMB_INLINE_MAX_BYTES + 1024
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * size)
        html = render_thumb(str(img))
        assert "file://" in html
        assert 'loading="lazy"' in html
        assert "thumb-link" in html
        assert "base64" not in html

    def test_oversized_placeholder(self, tmp_path: Path) -> None:
        """Files over 5 MB render as oversized placeholder + warning."""
        img = tmp_path / "huge.png"
        size = THUMB_LINK_MAX_BYTES + 1024
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * size)
        html = render_thumb(str(img))
        assert "oversized" in html
        assert "thumb-oversized" in html
        assert "base64" not in html


# ---------------------------------------------------------------------------
# CSV writer tests
# ---------------------------------------------------------------------------
class TestWriteCsv:
    """Tests for _write_csv atomic writes and column contract."""

    def test_writes_all_columns(self, tmp_path: Path) -> None:
        """CSV contains all documented columns in order."""
        rows: List[Dict[str, Any]] = [
            {c: f"val_{i}" for i, c in enumerate(CSV_COLUMNS)}
        ]
        csv_path = _write_csv(rows, tmp_path)
        assert csv_path.exists()
        with csv_path.open("r", newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            header = reader.fieldnames or []
            assert header == list(CSV_COLUMNS)
            row = next(reader)
            for col in CSV_COLUMNS:
                assert col in row

    def test_atomic_latest_symlink(self, tmp_path: Path) -> None:
        """latest.csv symlink points to the timestamped file."""
        rows: List[Dict[str, Any]] = [{c: "x" for c in CSV_COLUMNS}]
        csv_path = _write_csv(rows, tmp_path)
        latest = tmp_path / "latest.csv"
        assert latest.is_symlink()
        assert latest.readlink().name == csv_path.name

    def test_multiple_ticks_increments(self, tmp_path: Path) -> None:
        """Each tick creates a new timestamped file; latest.csv follows."""
        rows: List[Dict[str, Any]] = [{c: "x" for c in CSV_COLUMNS}]
        p1 = _write_csv(rows, tmp_path)
        p2 = _write_csv(rows, tmp_path)
        assert p1 != p2
        latest = tmp_path / "latest.csv"
        assert latest.readlink().name == p2.name


# ---------------------------------------------------------------------------
# HTML writer tests
# ---------------------------------------------------------------------------
class TestWriteHtml:
    """Tests for _write_html atomic writes, truncation, and rendering."""

    def test_atomic_latest_html(self, tmp_path: Path) -> None:
        """latest.html is atomically replaced."""
        rows: List[Dict[str, Any]] = [
            {
                "created_at": "2024-01-01",
                "llm_direction": "long",
                "llm_confidence": 0.85,
                "quant_direction": "short",
                "quant_confidence": 0.7,
                "consensus_direction": "long",
                "consensus_confidence": 0.8,
                "platform": "discord",
                "trader_handle": "alice",
                "symbol": "BTCUSDT",
                "media_local_path": None,
                "llm_raw_request_path": None,
                "llm_raw_response_path": None,
                "discord_url": None,
                "news_item_id": 1,
                "media_item_id": 2,
            }
        ]
        html_path, truncated, banner = _write_html(rows, tmp_path, 24)
        latest = tmp_path / "latest.html"
        assert latest.exists()
        assert latest.read_text(encoding="utf-8") == html_path.read_text(encoding="utf-8")
        assert not truncated
        assert banner == ""

    def test_truncation_when_oversized(self, tmp_path: Path) -> None:
        """If HTML exceeds 50 MB, oldest rows are dropped until it fits."""
        # Synthesize many rows with large JSON payload to force truncation
        big_json = "A" * (200 * 1024)
        rows: List[Dict[str, Any]] = []
        for i in range(300):
            rows.append(
                {
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "llm_direction": "long",
                    "media_local_path": None,
                    "llm_raw_request_path": "/tmp/req.json",
                    "llm_raw_response_path": "/tmp/resp.json",
                    "discord_url": None,
                    "platform": "discord",
                    "trader_handle": f"trader_{i}",
                    "symbol": "BTCUSDT",
                    "llm_confidence": 0.85,
                    "quant_confidence": 0.7,
                    "consensus_confidence": 0.8,
                    "llm_levels_json": big_json,
                }
            )
        html_path, truncated, banner = _write_html(rows, tmp_path, 24)
        assert truncated
        assert "cap" in banner.lower()
        assert html_path.stat().st_size <= HTML_AGGREGATE_MAX_BYTES

    def test_render_html_no_rows(self) -> None:
        """Rendering with empty rows produces valid HTML."""
        html = _render_html([], 24)
        assert "Signal Review" in html
        assert "tbody" in html


# ---------------------------------------------------------------------------
# Symlink integrity tests
# ---------------------------------------------------------------------------
class TestSymlinkIntegrity:
    """Tests for _assert_symlink_integrity."""

    def test_passes_with_valid_symlink(self, tmp_path: Path) -> None:
        """Valid symlink passes the check."""

        class _MockPath:
            def __init__(self, p: str) -> None:
                self._p = p

            def is_symlink(self) -> bool:
                return True

            def resolve(self) -> Path:
                return Path("/opt/tickles/shared/reports/signal_review")

        with patch(
            "shared.intelligence.signal_review_export.Path",
            _MockPath,
        ):
            _assert_symlink_integrity()

    def test_raises_when_not_symlink(self, tmp_path: Path) -> None:
        """Non-symlink path raises RuntimeError."""

        class _MockPath:
            def __init__(self, p: str) -> None:
                self._p = p

            def is_symlink(self) -> bool:
                return False

        with patch(
            "shared.intelligence.signal_review_export.Path",
            _MockPath,
        ):
            with pytest.raises(RuntimeError):
                _assert_symlink_integrity()

    def test_raises_when_wrong_target(self, tmp_path: Path) -> None:
        """Symlink pointing to wrong directory raises RuntimeError."""

        class _MockPath:
            def __init__(self, p: str) -> None:
                self._p = p

            def is_symlink(self) -> bool:
                return True

            def resolve(self) -> Path:
                if self._p == "/opt/tickles/opticals/signal_review":
                    return Path("/some/other/path")
                return Path("/opt/tickles/shared/reports/signal_review")

        with patch(
            "shared.intelligence.signal_review_export.Path",
            _MockPath,
        ):
            with pytest.raises(RuntimeError):
                _assert_symlink_integrity()


# ---------------------------------------------------------------------------
# SignalReviewExporter integration tests
# ---------------------------------------------------------------------------
class TestSignalReviewExporter:
    """Tests for the exporter class."""

    @pytest.mark.anyio
    async def test_run_once_with_no_rows(self, tmp_path: Path) -> None:
        """Tick with no database rows returns zero summary."""
        exporter = SignalReviewExporter(report_dir=tmp_path, lookback_h=24)
        with patch(
            "shared.intelligence.signal_review_export._assert_symlink_integrity"
        ):
            with patch(
                "shared.intelligence.signal_review_export._fetch_all_rows",
                new_callable=AsyncMock,
                return_value=[],
            ):
                result = await exporter.run_once()
        assert result["ok"] is True
        assert result["rows"] == 0
        assert result["csv"] is None
        assert result["html"] is None

    @pytest.mark.anyio
    async def test_run_once_writes_files(self, tmp_path: Path) -> None:
        """Tick with rows writes CSV + HTML files."""
        row: Dict[str, Any] = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "llm_direction": "long",
            "platform": "discord",
            "trader_handle": "alice",
            "symbol": "BTCUSDT",
            "llm_confidence": 0.9,
            "quant_confidence": 0.8,
            "consensus_confidence": 0.85,
            "media_local_path": None,
            "llm_raw_request_path": None,
            "llm_raw_response_path": None,
            "discord_url": None,
            "news_item_id": 1,
            "media_item_id": 2,
        }
        exporter = SignalReviewExporter(report_dir=tmp_path, lookback_h=24)
        with patch(
            "shared.intelligence.signal_review_export._assert_symlink_integrity"
        ):
            with patch(
                "shared.intelligence.signal_review_export._fetch_all_rows",
                new_callable=AsyncMock,
                return_value=[row],
            ):
                result = await exporter.run_once()
        assert result["ok"] is True
        assert result["rows"] == 1
        assert result["csv"] is not None
        assert result["html"] is not None
        assert Path(result["csv"]).exists()
        assert Path(result["html"]).exists()


# ---------------------------------------------------------------------------
# CSV column contract freeze test
# ---------------------------------------------------------------------------
def test_csv_columns_match_documented_contract() -> None:
    """CSV_COLUMNS must match the Phase 4 documented contract exactly."""
    expected = (
        "created_at",
        "news_item_id",
        "media_item_id",
        "platform",
        "trader_handle",
        "symbol",
        "instrument_symbol_normalised",
        "instrument_exchange",
        "prefilter_result",
        "vision_provider",
        "vision_model_resolved",
        "prompt_version",
        "llm_direction",
        "llm_confidence",
        "quant_direction",
        "quant_confidence",
        "consensus_direction",
        "consensus_confidence",
        "llm_levels_json",
        "discord_url",
        "media_local_path",
        "llm_raw_request_path",
        "llm_raw_response_path",
        "correlation_id",
        "total_cost_usd",
    )
    assert CSV_COLUMNS == expected
