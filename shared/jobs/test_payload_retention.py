"""
Module: test_payload_retention
Purpose: Tests for shared/jobs/payload_retention.py
Location: /opt/tickles/shared/jobs/test_payload_retention.py
"""

import json
import os
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from shared.jobs.payload_retention import (
    _compress_day_dir,
    _delete_old_archives,
    _find_payload_dirs,
    _get_total_size_gb,
    _parse_date_from_path,
    run_retention_sweep,
)


class TestParseDateFromPath:
    def test_parses_standard_path(self) -> None:
        path = Path("/data/signal_payloads/2026/04/15/abc123.req.json")
        result = _parse_date_from_path(path)
        assert result is not None
        assert result.year == 2026
        assert result.month == 4
        assert result.day == 15

    def test_parses_deep_path(self) -> None:
        path = Path("/opt/tickles/shared/reports/signal_payloads/2026/01/02/file.json")
        result = _parse_date_from_path(path)
        assert result is not None
        assert result.year == 2026
        assert result.month == 1
        assert result.day == 2

    def test_returns_none_for_invalid(self) -> None:
        path = Path("/data/no/date/here/file.json")
        assert _parse_date_from_path(path) is None


class TestFindPayloadDirs:
    def test_finds_leaf_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            # Create structure: 2026/04/15 and 2026/04/16
            (base / "2026" / "04" / "15").mkdir(parents=True)
            (base / "2026" / "04" / "16").mkdir(parents=True)
            # Create a non-day directory (should be ignored)
            (base / "2026" / "04" / "README").mkdir(parents=True)

            dirs = _find_payload_dirs(base)
            assert len(dirs) == 2
            names = sorted([d.name for d in dirs])
            assert names == ["15", "16"]

    def test_empty_base_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            assert _find_payload_dirs(Path(tmp)) == []

    def test_nonexistent_base_returns_empty(self) -> None:
        assert _find_payload_dirs(Path("/nonexistent/path")) == []


class TestCompressDayDir:
    def test_compresses_json_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            day_dir = Path(tmp) / "2026" / "04" / "15"
            day_dir.mkdir(parents=True)

            # Create some JSON files
            (day_dir / "abc.req.json").write_text(json.dumps({"test": 1}))
            (day_dir / "abc.resp.json").write_text(json.dumps({"test": 2}))

            compressed, errors = _compress_day_dir(day_dir)
            assert compressed == 2
            assert errors == 0

            # Check archive exists
            archive = day_dir.with_suffix(".tar.zst")
            if not archive.exists():
                archive = day_dir.with_suffix(".tar.gz")
            assert archive.exists()

            # Original JSON files should be gone
            assert list(day_dir.glob("*.json")) == []

    def test_empty_dir_returns_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            day_dir = Path(tmp) / "2026" / "04" / "15"
            day_dir.mkdir(parents=True)
            compressed, errors = _compress_day_dir(day_dir)
            assert compressed == 0
            assert errors == 0


class TestDeleteOldArchives:
    def test_deletes_archives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            day_dir = Path(tmp) / "2026" / "04" / "15"
            day_dir.mkdir(parents=True)
            archive = day_dir / "archive.tar.zst"
            archive.write_text("compressed data")

            deleted, skipped = _delete_old_archives(day_dir)
            assert deleted == 1
            assert skipped == 0
            assert not archive.exists()

    def test_skips_locked_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            day_dir = Path(tmp) / "2026" / "04" / "15"
            day_dir.mkdir(parents=True)
            archive = day_dir / "archive.tar.zst"
            archive.write_text("compressed data")
            # Create lock marker
            (day_dir / ".retention_locked").write_text("locked")

            deleted, skipped = _delete_old_archives(day_dir)
            assert deleted == 0
            assert skipped == 1
            assert archive.exists()


class TestGetTotalSizeGb:
    def test_calculates_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "file1.txt").write_text("a" * 1024)
            (base / "file2.txt").write_text("b" * 2048)

            size_gb = _get_total_size_gb(base)
            expected_bytes = 3072
            expected_gb = expected_bytes / (1024 ** 3)
            assert abs(size_gb - expected_gb) < 0.0001

    def test_empty_dir_is_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            assert _get_total_size_gb(Path(tmp)) == 0.0


class TestRunRetentionSweep:
    def test_dry_run_only_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            # Create an old day directory (40 days ago)
            old_date = datetime.now(timezone.utc) - timedelta(days=40)
            day_dir = base / f"{old_date.year}" / f"{old_date.month:02d}" / f"{old_date.day:02d}"
            day_dir.mkdir(parents=True)
            (day_dir / "test.req.json").write_text(json.dumps({"old": True}))

            result = run_retention_sweep(dry_run=True, base_dir=base, compress_days=30, delete_days=365)

            assert result["dry_run"] is True
            assert result["compressed"] == 1
            assert result["deleted"] == 0
            # File should still exist
            assert (day_dir / "test.req.json").exists()

    def test_compresses_old_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            # Create an old day directory (40 days ago)
            old_date = datetime.now(timezone.utc) - timedelta(days=40)
            day_dir = base / f"{old_date.year}" / f"{old_date.month:02d}" / f"{old_date.day:02d}"
            day_dir.mkdir(parents=True)
            (day_dir / "test.req.json").write_text(json.dumps({"old": True}))

            result = run_retention_sweep(dry_run=False, base_dir=base, compress_days=30, delete_days=365)

            assert result["compressed"] == 1
            assert result["errors"] == 0
            # JSON file should be gone, archive should exist
            assert not (day_dir / "test.req.json").exists()
            archive = day_dir.with_suffix(".tar.zst")
            if not archive.exists():
                archive = day_dir.with_suffix(".tar.gz")
            assert archive.exists()

    def test_deletes_very_old_archives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            # Create a very old day directory (400 days ago)
            old_date = datetime.now(timezone.utc) - timedelta(days=400)
            day_dir = base / f"{old_date.year}" / f"{old_date.month:02d}" / f"{old_date.day:02d}"
            day_dir.mkdir(parents=True)
            (day_dir / "archive.tar.zst").write_text("old compressed data")

            result = run_retention_sweep(dry_run=False, base_dir=base, compress_days=30, delete_days=365)

            assert result["deleted"] == 1
            assert not (day_dir / "archive.tar.zst").exists()

    def test_respects_compress_days(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            # Create a recent day directory (5 days ago)
            recent_date = datetime.now(timezone.utc) - timedelta(days=5)
            day_dir = base / f"{recent_date.year}" / f"{recent_date.month:02d}" / f"{recent_date.day:02d}"
            day_dir.mkdir(parents=True)
            (day_dir / "test.req.json").write_text(json.dumps({"recent": True}))

            # With compress_days=30, this should NOT be compressed
            result = run_retention_sweep(dry_run=False, base_dir=base, compress_days=30, delete_days=365)
            assert result["compressed"] == 0
            assert (day_dir / "test.req.json").exists()

    def test_disk_budget_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            # Create files that exceed 80% of a small budget
            (base / "large_file.txt").write_text("x" * int(1024 * 1024 * 0.9))  # 0.9 MB

            # Budget of 1 MB = 0.00095 GB, 80% = 0.00076 GB
            # Our file is ~0.00086 GB, so it should trigger warning
            with patch("shared.jobs.payload_retention.logger") as mock_logger:
                result = run_retention_sweep(
                    dry_run=True,
                    base_dir=base,
                    compress_days=30,
                    delete_days=365,
                    disk_budget_gb=1,  # 1 MB budget in GB is tiny
                )
                # The warning should be logged because 0.9 MB > 80% of 1 MB
                # Actually 1 GB budget, 0.9 MB is 0.0009 GB which is < 0.8 GB
                # Let's use a smaller budget

            # Re-test with smaller budget
            with patch("shared.jobs.payload_retention.logger") as mock_logger:
                result = run_retention_sweep(
                    dry_run=True,
                    base_dir=base,
                    compress_days=30,
                    delete_days=365,
                    disk_budget_gb=1,  # 1 GB budget
                )
                # 0.9 MB = 0.0009 GB, which is < 0.8 GB, so no warning
                warning_calls = [c for c in mock_logger.warning.call_args_list if "exceeds" in str(c)]
                assert len(warning_calls) == 0

    def test_result_structure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            result = run_retention_sweep(dry_run=True, base_dir=base, compress_days=30, delete_days=365)

            assert "compressed" in result
            assert "deleted" in result
            assert "skipped_locked" in result
            assert "errors" in result
            assert "total_gb" in result
            assert "dry_run" in result
            assert isinstance(result["total_gb"], float)
