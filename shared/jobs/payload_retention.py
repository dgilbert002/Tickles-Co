"""
Module: payload_retention
Purpose: Daily cron that compresses >30-day payloads and deletes >365-day archives.
Location: /opt/tickles/shared/jobs/payload_retention.py
"""

import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_RETENTION_COMPRESS_DAYS = int(os.environ.get("PAYLOAD_RETENTION_COMPRESS_DAYS", "30"))
_RETENTION_DELETE_DAYS = int(os.environ.get("PAYLOAD_RETENTION_DELETE_DAYS", "365"))
_DISK_BUDGET_GB = int(os.environ.get("PAYLOAD_DISK_BUDGET_GB", "50"))
_PAYLOAD_BASE_DIR = Path(
    os.environ.get("PAYLOAD_BASE_DIR", "/opt/tickles/shared/reports/signal_payloads")
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_date_from_path(path: Path) -> Optional[datetime]:
    """Extract YYYY/MM/DD from a payload path like .../2026/04/30/<cid>.req.json."""
    parts = path.parts
    # Walk backwards looking for three consecutive numeric parts (year/month/day)
    for i in range(len(parts) - 1, 2, -1):
        if (
            parts[i - 2].isdigit()
            and parts[i - 1].isdigit()
            and parts[i].isdigit()
            and len(parts[i - 2]) == 4
            and len(parts[i - 1]) == 2
            and len(parts[i]) == 2
        ):
            try:
                return datetime(
                    int(parts[i - 2]), int(parts[i - 1]), int(parts[i]),
                    tzinfo=timezone.utc,
                )
            except ValueError:
                continue
    return None


def _find_payload_dirs(base_dir: Path) -> List[Path]:
    """Return all YYYY/MM/DD leaf directories under base_dir."""
    if not base_dir.exists():
        return []
    leaf_dirs: List[Path] = []
    for year_dir in sorted(base_dir.iterdir()):
        if not year_dir.is_dir() or not year_dir.name.isdigit():
            continue
        for month_dir in sorted(year_dir.iterdir()):
            if not month_dir.is_dir() or not month_dir.name.isdigit():
                continue
            for day_dir in sorted(month_dir.iterdir()):
                if day_dir.is_dir() and day_dir.name.isdigit():
                    leaf_dirs.append(day_dir)
    return leaf_dirs


def _has_zstd() -> bool:
    """Check if zstd CLI is available."""
    return shutil.which("zstd") is not None


def _compress_day_dir(day_dir: Path) -> Tuple[int, int]:
    """Compress all .json files in a day directory into a single .tar.zst.

    Returns:
        (compressed_count, errors_count)
    """
    archive_path = day_dir.with_suffix(".tar.zst")
    json_files = sorted(day_dir.glob("*.json"))
    if not json_files:
        return 0, 0

    compressed = 0
    errors = 0

    try:
        if _has_zstd():
            # Use tar + zstd for best compression
            with tempfile.NamedTemporaryFile(
                suffix=".tar", dir=str(day_dir.parent), delete=False
            ) as tmp_tar:
                tmp_tar_path = Path(tmp_tar.name)
                with tarfile.open(tmp_tar.name, "w") as tar:
                    for jf in json_files:
                        tar.add(jf, arcname=jf.name)
                # Compress with zstd
                subprocess.run(
                    ["zstd", "-q", "-19", "-f", str(tmp_tar_path), "-o", str(archive_path)],
                    check=True,
                    capture_output=True,
                )
                tmp_tar_path.unlink(missing_ok=True)
        else:
            # Fallback to gzip tar
            archive_path = day_dir.with_suffix(".tar.gz")
            with tarfile.open(archive_path, "w:gz") as tar:
                for jf in json_files:
                    tar.add(jf, arcname=jf.name)

        # Verify archive exists and has content, then delete originals
        if archive_path.exists() and archive_path.stat().st_size > 0:
            for jf in json_files:
                try:
                    jf.unlink()
                    compressed += 1
                except OSError as exc:
                    logger.warning("Failed to delete %s after compression: %s", jf, exc)
                    errors += 1
            # Remove empty day directory
            try:
                day_dir.rmdir()
            except OSError:
                pass  # Directory not empty (other files)
        else:
            logger.warning("Archive creation failed for %s", day_dir)
            errors += 1

    except subprocess.CalledProcessError as exc:
        logger.error("zstd compression failed for %s: %s", day_dir, exc)
        errors += 1
    except OSError as exc:
        logger.error("Archive I/O error for %s: %s", day_dir, exc)
        errors += 1

    return compressed, errors


def _delete_old_archives(day_dir: Path) -> Tuple[int, int]:
    """Delete .tar.zst or .tar.gz archives older than retention limit.

    Returns:
        (deleted_count, skipped_locked_count)
    """
    deleted = 0
    skipped = 0

    for archive in day_dir.glob("*.tar.*"):
        # Check if any correlation_id in this archive is retention-locked
        # We can't peek inside tar without extracting, so we use a heuristic:
        # if the day_dir has a .retention_lock marker file, skip deletion
        lock_marker = day_dir / ".retention_locked"
        if lock_marker.exists():
            skipped += 1
            continue

        try:
            archive.unlink()
            deleted += 1
        except OSError as exc:
            logger.warning("Failed to delete archive %s: %s", archive, exc)

    # Try to remove empty parent directories
    try:
        day_dir.rmdir()
    except OSError:
        pass
    for parent in [day_dir.parent, day_dir.parent.parent]:
        try:
            parent.rmdir()
        except OSError:
            break

    return deleted, skipped


def _get_total_size_gb(path: Path) -> float:
    """Return total size of directory in gigabytes."""
    total = 0
    for entry in path.rglob("*"):
        if entry.is_file():
            try:
                total += entry.stat().st_size
            except OSError:
                pass
    return total / (1024 ** 3)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def run_retention_sweep(
    *,
    dry_run: bool = False,
    base_dir: Optional[Path] = None,
    compress_days: Optional[int] = None,
    delete_days: Optional[int] = None,
    disk_budget_gb: Optional[int] = None,
) -> Dict[str, Any]:
    """Run the payload retention sweep.

    Compresses raw JSON files older than compress_days into .tar.zst archives.
    Deletes archives older than delete_days unless retention-locked.
    Alerts if total disk usage exceeds disk_budget_gb.

    Args:
        dry_run: If True, only log actions without touching files.
        base_dir: Override default payload base directory.
        compress_days: Days before compression (default 30).
        delete_days: Days before deletion (default 365).
        disk_budget_gb: Alert threshold in GB (default 50).

    Returns:
        Dict with counts: compressed, deleted, skipped_locked, errors, total_gb.
    """
    bucket = base_dir or _PAYLOAD_BASE_DIR
    compress_after = compress_days or _RETENTION_COMPRESS_DAYS
    delete_after = delete_days or _RETENTION_DELETE_DAYS
    budget = disk_budget_gb or _DISK_BUDGET_GB

    now = datetime.now(timezone.utc)
    compress_cutoff = now - timedelta(days=compress_after)
    delete_cutoff = now - timedelta(days=delete_after)

    compressed_count = 0
    deleted_count = 0
    skipped_locked = 0
    errors_count = 0

    leaf_dirs = _find_payload_dirs(bucket)

    for day_dir in leaf_dirs:
        day_date = _parse_date_from_path(day_dir)
        if day_date is None:
            logger.warning("Could not parse date from path: %s", day_dir)
            continue

        # --- Compression phase: raw JSON older than compress_days ---
        if day_date < compress_cutoff:
            json_files = list(day_dir.glob("*.json"))
            if json_files:
                if dry_run:
                    logger.info(
                        "[DRY-RUN] Would compress %d files in %s",
                        len(json_files),
                        day_dir,
                    )
                    compressed_count += len(json_files)
                else:
                    c, e = _compress_day_dir(day_dir)
                    compressed_count += c
                    errors_count += e

        # --- Deletion phase: archives older than delete_days ---
        if day_date < delete_cutoff:
            archives = list(day_dir.glob("*.tar.*"))
            if archives:
                if dry_run:
                    logger.info(
                        "[DRY-RUN] Would delete %d archives in %s",
                        len(archives),
                        day_dir,
                    )
                    deleted_count += len(archives)
                else:
                    d, s = _delete_old_archives(day_dir)
                    deleted_count += d
                    skipped_locked += s

    total_gb = _get_total_size_gb(bucket)
    if total_gb > budget * 0.8:
        logger.warning(
            "Payload disk usage %.1f GB exceeds 80%% of budget %d GB",
            total_gb,
            budget,
        )

    result = {
        "compressed": compressed_count,
        "deleted": deleted_count,
        "skipped_locked": skipped_locked,
        "errors": errors_count,
        "total_gb": round(total_gb, 3),
        "dry_run": dry_run,
    }

    logger.info(
        "Retention sweep complete: compressed=%d deleted=%d skipped=%d errors=%d total_gb=%.2f",
        compressed_count,
        deleted_count,
        skipped_locked,
        errors_count,
        total_gb,
    )
    return result


async def run_retention_sweep_async(
    *,
    dry_run: bool = False,
    base_dir: Optional[Path] = None,
    compress_days: Optional[int] = None,
    delete_days: Optional[int] = None,
    disk_budget_gb: Optional[int] = None,
) -> Dict[str, Any]:
    """Async wrapper for run_retention_sweep (runs in thread pool)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        run_retention_sweep,
        dry_run,
        base_dir,
        compress_days,
        delete_days,
        disk_budget_gb,
    )


def main() -> None:
    """CLI entry point for manual / cron invocation."""
    import argparse

    parser = argparse.ArgumentParser(description="Payload retention sweep")
    parser.add_argument("--dry-run", action="store_true", help="Log actions without touching files")
    parser.add_argument("--base-dir", type=str, default=None, help="Override payload base directory")
    parser.add_argument("--compress-days", type=int, default=None, help="Days before compression")
    parser.add_argument("--delete-days", type=int, default=None, help="Days before deletion")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    base = Path(args.base_dir) if args.base_dir else None
    result = run_retention_sweep(
        dry_run=args.dry_run,
        base_dir=base,
        compress_days=args.compress_days,
        delete_days=args.delete_days,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
