"""Module: run_phase_migration
Purpose: Idempotent phase migration runner with schema_migrations ledger.
Location: /opt/tickles/shared/migration/run_phase_migration.py
"""

import argparse
import asyncio
import hashlib
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import asyncpg

from shared.utils.companies import for_each_company, list_active_companies
from shared.utils.db import get_shared_pool

logger = logging.getLogger(__name__)

MIGRATION_DIR = Path(__file__).parent


class MigrationChecksumMismatch(Exception):
    """Raised when a migration file's checksum differs from the ledger."""
    pass


class MigrationNotFound(Exception):
    """Raised when no migration file matches the requested phase."""
    pass


def _compute_checksum(filepath: Path) -> str:
    """Return SHA-256 hex digest of file contents."""
    h = hashlib.sha256()
    h.update(filepath.read_bytes())
    return h.hexdigest()


def _find_migration_file(phase: int) -> Path:
    """Find the SQL migration file for a given phase.

    Pattern: 2026_04_29_phase{phase}_*.sql
    Raises MigrationNotFound if no file or multiple files match.
    """
    pattern = f"2026_04_29_phase{phase}_*.sql"
    candidates = list(MIGRATION_DIR.glob(pattern))
    # Also accept non-dated phase files for flexibility
    if not candidates:
        pattern = f"phase{phase}_*.sql"
        candidates = list(MIGRATION_DIR.glob(pattern))

    if not candidates:
        raise MigrationNotFound(
            f"No migration file found for phase {phase} in {MIGRATION_DIR}"
        )
    if len(candidates) > 1:
        raise MigrationNotFound(
            f"Multiple migration files for phase {phase}: {[c.name for c in candidates]}"
        )
    return candidates[0]


async def _is_applied(
    conn: asyncpg.Connection, phase: int, checksum: str
) -> bool:
    """Return True if this exact migration has already been applied."""
    row = await conn.fetchrow(
        "SELECT checksum FROM schema_migrations WHERE phase = $1",
        phase,
    )
    if row is None:
        return False
    if row["checksum"] != checksum:
        raise MigrationChecksumMismatch(
            f"Phase {phase} already applied with checksum {row['checksum']}, "
            f"but file has {checksum}.  Migration file changed after apply!"
        )
    return True


async def _record_migration(
    conn: asyncpg.Connection, phase: int, checksum: str, target: str
) -> None:
    """Insert or update the schema_migrations ledger row."""
    await conn.execute(
        """
        INSERT INTO schema_migrations (phase, checksum, target, applied_at)
        VALUES ($1, $2, $3, NOW())
        ON CONFLICT (phase)
        DO UPDATE SET
            checksum = EXCLUDED.checksum,
            target = EXCLUDED.target,
            applied_at = EXCLUDED.applied_at
        """,
        phase,
        checksum,
        target,
    )


async def apply_migration(
    phase: int,
    target: str = "shared",
    dry_run: bool = False,
) -> dict[str, str]:
    """Apply a phase migration to the specified target.

    Args:
        phase: Phase number (e.g. 0, 1, 2).
        target: 'shared' | 'all_companies' | specific company name.
        dry_run: If True, print what would be done without executing.

    Returns:
        Dictionary mapping database name to result status:
        {'tickles_shared': 'applied', 'tickles_rubicon': 'skipped (already applied)'}

    Raises:
        MigrationNotFound: If no SQL file matches the phase.
        MigrationChecksumMismatch: If checksum differs from ledger.
        asyncpg.PostgresError: On database failure.
    """
    migration_file = _find_migration_file(phase)
    checksum = _compute_checksum(migration_file)
    sql = migration_file.read_text()

    results: dict[str, str] = {}

    if target == "shared":
        pool = await get_shared_pool()
        async with pool.acquire() as conn:
            if await _is_applied(conn, phase, checksum):
                results["tickles_shared"] = "skipped (already applied)"
                return results

            if dry_run:
                results["tickles_shared"] = "dry_run (would apply)"
                return results

            async with conn.transaction():
                await conn.execute(sql)
                await _record_migration(conn, phase, checksum, target)
            results["tickles_shared"] = "applied"

    elif target == "all_companies":
        async def _apply_to_company(company: str, conn: asyncpg.Connection) -> str:
            try:
                if await _is_applied(conn, phase, checksum):
                    return "skipped (already applied)"
                if dry_run:
                    return "dry_run (would apply)"
                async with conn.transaction():
                    await conn.execute(sql)
                    await _record_migration(conn, phase, checksum, company)
                return "applied"
            except MigrationChecksumMismatch:
                raise
            except Exception as exc:
                logger.exception("Migration phase %d failed for %s", phase, company)
                return f"failed: {exc}"

        company_results = await for_each_company(_apply_to_company)
        for company, result in company_results.items():
            db_name = f"tickles_{company}"
            if isinstance(result, Exception):
                results[db_name] = f"failed: {result}"
            else:
                results[db_name] = result

    else:
        # Single company target
        dsn = os.environ.get(
            "TICKLES_DB_DSN_TEMPLATE",
            "postgresql://{user}:{password}@{host}:{port}/{db}",
        )
        if "{user}" in dsn:
            dsn = dsn.format(
                user=os.getenv("DB_USER", "tickles"),
                password=os.getenv("DB_PASSWORD", ""),
                host=os.getenv("DB_HOST", "localhost"),
                port=os.getenv("DB_PORT", "5432"),
                db="{db}",
            )
        company_dsn = dsn.format(db=f"tickles_{target}")
        conn = await asyncpg.connect(company_dsn)
        try:
            if await _is_applied(conn, phase, checksum):
                results[f"tickles_{target}"] = "skipped (already applied)"
                return results
            if dry_run:
                results[f"tickles_{target}"] = "dry_run (would apply)"
                return results
            async with conn.transaction():
                await conn.execute(sql)
                await _record_migration(conn, phase, checksum, target)
            results[f"tickles_{target}"] = "applied"
        finally:
            await conn.close()

    return results


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Apply a phase migration to shared or company databases."
    )
    parser.add_argument("--phase", type=int, required=True, help="Phase number")
    parser.add_argument(
        "--target",
        required=True,
        choices=["shared", "all_companies"],
        help="Target database(s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without executing",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable debug logging"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    async def _run() -> None:
        results = await apply_migration(args.phase, args.target, args.dry_run)
        for db, status in results.items():
            print(f"  {db}: {status}")
        # Exit non-zero if any failed
        if any("failed" in s for s in results.values()):
            sys.exit(1)

    asyncio.run(_run())


if __name__ == "__main__":
    main()
