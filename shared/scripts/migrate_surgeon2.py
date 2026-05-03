"""
Module: migrate_surgeon2
Purpose: Phase 10 one-shot migration driver — legacy surgeon2 tables → canonical schema.
Location: /opt/tickles/shared/scripts/migrate_surgeon2.py
"""

import argparse
import logging
import os
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.append("/opt/tickles")
from shared.utils.config import load_env

load_env()

logger = logging.getLogger(__name__)

MIGRATION_SQL = Path(__file__).parent.parent / "intelligence" / "migrations" / "2026_05_01_phase10_surgeon2_migration.sql"


def _connect(company: str) -> psycopg2.extensions.connection:
    """Connect to a company database."""
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DB_PORT", "5432")),
        user=os.environ.get("DB_USER", "admin"),
        password=os.environ.get("DB_PASSWORD", ""),
        dbname=f"tickles_{company}",
    )


def _count_legacy_rows(conn: psycopg2.extensions.connection) -> dict[str, int]:
    """Count rows in legacy tables."""
    counts: dict[str, int] = {}
    with conn.cursor() as cur:
        for tbl in ("surgeon2_state", "surgeon2_positions", "surgeon2_trade_log"):
            cur.execute(
                "SELECT COUNT(*) FROM pg_tables WHERE schemaname='public' AND tablename=%s",
                (tbl,),
            )
            row = cur.fetchone()
            exists = row[0] if row else 0
            if exists:
                cur.execute(f"SELECT COUNT(*) FROM {tbl}")
                counts[tbl] = cur.fetchone()[0]
            else:
                counts[tbl] = 0
    return counts


def _count_new_rows(conn: psycopg2.extensions.connection) -> dict[str, int]:
    """Count rows created by migration in canonical tables."""
    counts: dict[str, int] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM public.tracked_positions WHERE actor_type='agent' AND actor_id='surgeon2'"
        )
        counts["tracked_positions"] = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM agent_state WHERE agent_name='surgeon2'"
        )
        counts["agent_state"] = cur.fetchone()[0]
        cur.execute(
            """
            SELECT COUNT(*) FROM position_updates pu
            JOIN public.tracked_positions tp ON tp.id = pu.position_id
            WHERE tp.actor_type='agent' AND tp.actor_id='surgeon2'
            """
        )
        counts["position_updates"] = cur.fetchone()[0]
    return counts


def run_migration(company: str, dry_run: bool = True) -> dict[str, int]:
    """Run the Phase 10 migration for a single company.

    Args:
        company: Company slug (e.g. 'rubicon').
        dry_run: If True, print expected row counts but do not execute.

    Returns:
        Dict of legacy table names to row counts.
    """
    conn = _connect(company)
    try:
        legacy = _count_legacy_rows(conn)
        logger.info("[%s] Legacy rows: %s", company, legacy)

        if dry_run:
            logger.info("[%s] DRY RUN — skipping execution", company)
            return legacy

        sql = MIGRATION_SQL.read_text()
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()

        new_counts = _count_new_rows(conn)
        logger.info("[%s] New rows after migration: %s", company, new_counts)

        # Parity check
        if new_counts["tracked_positions"] != legacy.get("surgeon2_positions", 0):
            logger.error(
                "[%s] PARITY MISMATCH: tracked_positions=%s vs surgeon2_positions=%s",
                company,
                new_counts["tracked_positions"],
                legacy.get("surgeon2_positions", 0),
            )
        else:
            logger.info("[%s] Parity check PASSED", company)

        return legacy
    finally:
        conn.close()


def main() -> None:
    """CLI entry-point."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Phase 10 Surgeon2 migration driver")
    parser.add_argument("--company", default="rubicon", help="Company slug")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Print counts without executing")
    parser.add_argument("--execute", action="store_true", dest="dry_run", help="Actually run the migration")
    args = parser.parse_args()

    run_migration(args.company, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
