#!/usr/bin/env python3
"""
Cron job: resolve pending unknown symbols every 12 hours.

Called by cron/hermes-cron. Uses Gemini Flash to map unknown
LLM-extracted symbols to known exchange instruments.

Usage:
  python3 -m shared.utils.symbol_learner_cron  [--dry-run]
"""
from __future__ import annotations

import asyncio
import logging
import sys
import os
from pathlib import Path

# Ensure /opt/tickles is on path
sys.path.insert(0, "/opt/tickles")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("symbol_learner_cron")


async def main(dry_run: bool = False):
    from shared.utils.db import DatabasePool
    from shared.utils.symbol_learner import resolve_pending

    # DatabasePool reads connection from env vars (DB_HOST, DB_PORT, DB_USER, DB_PASSWORD)
    pool = DatabasePool(dbname="tickles_shared")

    try:
        if dry_run:
            rows = await pool.fetch_all(
                "SELECT raw_symbol, cleaned_base, seen_count FROM unresolved_symbols WHERE status='pending'"
            )
            if rows:
                logger.info("DRY RUN — would resolve %d unknowns:", len(rows))
                for r in rows:
                    logger.info("  %s (seen %d times)", r["raw_symbol"], r["seen_count"])
            else:
                logger.info("DRY RUN — no pending unknowns")
            return

        count = await resolve_pending(pool)
        if count > 0:
            logger.info("Resolved %d unknowns successfully", count)
        else:
            logger.info("No pending unknowns to resolve")
    finally:
        await pool.close()


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    asyncio.run(main(dry_run=dry))
