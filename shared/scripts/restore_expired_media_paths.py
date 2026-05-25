"""
Script: restore_expired_media_paths.py
Purpose: Restore missing local_path on cdn_hosted media items that have duplicate attached rows with downloaded files.
Location: /opt/tickles/shared/scripts/restore_expired_media_paths.py

During Discord collection, dual cdn_hosted and attached rows were created for the same attachment.
InterpretationService often processed the cdn_hosted row, leaving its local_path NULL.
When the Discord CDN URLs expired, these charts would fail to load on the dashboard.
This script scans for those mismatches, verifies the downloaded file exists on disk,
and updates the cdn_hosted row's local_path and metadata.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Dict, List, Any

# Ensure we can import from shared
sys.path.append(str(Path(__file__).resolve().parents[2]))

from shared.utils.db import DatabasePool

# Configure logging to console
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("restore_expired_media_paths")


async def get_mismatched_media_pairs(pool: DatabasePool) -> List[Dict[str, Any]]:
    """Fetch media_item pairs where the referenced one has NULL local_path but a duplicate has a valid path.

    Args:
        pool: The DatabasePool connection.

    Returns:
        List of dicts containing details of bad and good media items.
    """
    logger.info("get_mismatched_media_pairs() - Fetching mismatched media pairs from DB")
    sql = """
        SELECT si.id AS interp_id, si.news_item_id, si.media_item_id AS bad_media_id,
               mi2.id AS good_media_id, mi2.local_path AS good_local_path,
               mi2.mime_type, mi2.file_size_bytes, mi2.file_hash, mi2.dimensions
        FROM public.signal_interpretations si
        JOIN public.media_items mi1 ON mi1.id = si.media_item_id
        JOIN public.media_items mi2 ON mi2.news_item_id = si.news_item_id
        WHERE mi1.local_path IS NULL
          AND mi2.local_path IS NOT NULL
    """
    rows = await pool.fetch_all(sql)
    result = [dict(r) for r in rows]
    logger.info("get_mismatched_media_pairs() - Found %d mismatched items", len(result))
    return result


async def restore_media_paths(apply: bool = False) -> int:
    """Scan and update media_items rows to set correct local_path from duplicates.

    Args:
        apply: If True, execute the database updates. Otherwise, dry-run only.

    Returns:
        Number of successfully restored media items.
    """
    logger.info("restore_media_paths(apply=%s) - Starting restoration process", apply)
    pool = DatabasePool()
    await pool.initialize()

    try:
        pairs = await get_mismatched_media_pairs(pool)
        restored_count = 0

        for pair in pairs:
            interp_id = pair["interp_id"]
            bad_id = pair["bad_media_id"]
            good_id = pair["good_media_id"]
            good_path = pair["good_local_path"]

            # Verify the file actually exists on disk
            if not good_path:
                logger.warning(
                    "restore_media_paths() - Skipped bad_id=%d: good_path is empty",
                    bad_id
                )
                continue

            if not os.path.exists(good_path):
                logger.warning(
                    "restore_media_paths() - Skipped bad_id=%d: good_path '%s' does not exist on disk",
                    bad_id, good_path
                )
                continue

            # File is verified on disk
            file_size = os.path.getsize(good_path)
            logger.info(
                "restore_media_paths() - Verified file exists for Interp ID %d: bad_id=%d, good_id=%d, path=%s (%d bytes)",
                interp_id, bad_id, good_id, good_path, file_size
            )

            if apply:
                # Update the bad media item to restore its local path and other metadata
                update_sql = """
                    UPDATE public.media_items
                    SET local_path = $1,
                        mime_type = COALESCE(mime_type, $2),
                        file_size_bytes = COALESCE(file_size_bytes, $3),
                        file_hash = COALESCE(file_hash, $4),
                        dimensions = COALESCE(dimensions, $5)
                    WHERE id = $6
                """
                async with pool.acquire() as conn:
                    await conn.execute(
                        update_sql,
                        good_path,
                        pair["mime_type"],
                        pair["file_size_bytes"],
                        pair["file_hash"],
                        pair["dimensions"],
                        bad_id
                    )
                logger.info("restore_media_paths() - RESTORED bad_id=%d with path=%s", bad_id, good_path)
            
            restored_count += 1

        logger.info(
            "restore_media_paths() - Done. Restored total of %d/%d items (apply=%s)",
            restored_count, len(pairs), apply
        )
        return restored_count

    finally:
        # Avoid closing pool if it wasn't initialized, but here we always initialize it
        pass


if __name__ == "__main__":
    apply_mode = "--apply" in sys.argv
    if not apply_mode:
        logger.info("Running in DRY-RUN mode. Pass '--apply' to execute DB updates.")
    
    asyncio.run(restore_media_paths(apply=apply_mode))
