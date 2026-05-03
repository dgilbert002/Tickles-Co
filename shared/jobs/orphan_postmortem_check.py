"""
Module: orphan_postmortem_check
Purpose: Nightly cron that flags position_postmortems rows whose position_id
         is missing from tickles_shared.public.tracked_positions.
Location: /opt/tickles/shared/jobs/orphan_postmortem_check.py
"""

import logging
from typing import Any, Dict, List, Optional

from shared.utils.companies import for_each_company
from shared.utils.db import get_shared_pool

# Imported at module level so tests can patch it
import asyncpg  # noqa: E402

logger = logging.getLogger(__name__)

# Soft-FK validation: position_postmortems.position_id should reference
# tracked_positions.id in the shared ledger.  Postgres cannot enforce cross-DB
# foreign keys, so this cron job performs the check nightly and logs warnings.


async def _find_orphans_for_company(
    company: str,
    conn: Any,
    shared_pool: Any,
) -> List[Dict[str, Any]]:
    """Return orphan postmortem rows for a single company.

    An orphan is a row in position_postmortems whose position_id does not
    exist in tickles_shared.public.tracked_positions.

    Args:
        company: Company short name (e.g. 'rubicon').
        conn: Connection to the per-company database.
        shared_pool: Connection pool to tickles_shared.

    Returns:
        List of orphan rows as dicts with keys: id, position_id, created_at.
    """
    try:
        # Fetch all position_ids from the company's postmortems
        rows = await conn.fetch(
            "SELECT id, position_id, created_at FROM position_postmortems"
        )
    except Exception as exc:
        logger.warning(
            "Cannot read position_postmortems for %s: %s", company, exc
        )
        return []

    if not rows:
        return []

    position_ids = [r["position_id"] for r in rows]

    # Batch-check existence in shared tracked_positions
    try:
        async with shared_pool.acquire() as shared_conn:
            # Use ANY with an array for efficient batch lookup
            found_rows = await shared_conn.fetch(
                "SELECT id FROM public.tracked_positions WHERE id = ANY($1)",
                position_ids,
            )
    except Exception as exc:
        logger.warning(
            "Cannot query tracked_positions in shared DB for %s: %s",
            company,
            exc,
        )
        return []

    found_ids = {r["id"] for r in found_rows}
    orphans = [
        {
            "id": r["id"],
            "position_id": r["position_id"],
            "created_at": r["created_at"],
        }
        for r in rows
        if r["position_id"] not in found_ids
    ]

    return orphans


async def run_orphan_check(
    *,
    dry_run: bool = False,
    companies: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Run the orphan postmortem check across all (or specified) companies.

    Args:
        dry_run: If True, log orphans but do not write to the orphan_log table.
        companies: Optional list of company names to check.  If None, all active
            companies are checked via for_each_company.

    Returns:
        Summary dict with keys:
            - companies_checked: int
            - total_orphans: int
            - orphans_by_company: dict[str, list[dict]]
            - errors: list[str]
    """
    logger.info("Starting orphan postmortem check (dry_run=%s)", dry_run)

    shared_pool = await get_shared_pool()
    errors: List[str] = []
    orphans_by_company: Dict[str, List[Dict[str, Any]]] = {}
    total_orphans = 0

    async def _check_one(company: str, conn: Any) -> None:
        nonlocal total_orphans
        orphans = await _find_orphans_for_company(company, conn, shared_pool)
        if orphans:
            orphans_by_company[company] = orphans
            total_orphans += len(orphans)
            for orphan in orphans:
                logger.warning(
                    "Orphan postmortem detected: company=%s postmortem_id=%s "
                    "position_id=%s created_at=%s",
                    company,
                    orphan["id"],
                    orphan["position_id"],
                    orphan["created_at"],
                )
        else:
            logger.info("No orphan postmortems for %s", company)

    try:
        if companies:
            from shared.utils.companies import get_company_dsn

            for company in companies:
                # We need a connection per company; reuse for_each_company logic
                dsn = await get_company_dsn(company)
                conn = await asyncpg.connect(dsn)
                try:
                    await _check_one(company, conn)
                finally:
                    await conn.close()
        else:
            await for_each_company(_check_one)
    except Exception as exc:
        logger.exception("Orphan check failed: %s", exc)
        errors.append(str(exc))

    summary = {
        "companies_checked": len(orphans_by_company),
        "total_orphans": total_orphans,
        "orphans_by_company": orphans_by_company,
        "errors": errors,
    }

    logger.info(
        "Orphan check complete: %s companies, %s orphans, %s errors",
        summary["companies_checked"],
        total_orphans,
        len(errors),
    )
    return summary


async def main() -> None:
    """CLI entry point for the orphan check cron."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Check for orphan position_postmortems rows"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log orphans but do not write to orphan_log",
    )
    parser.add_argument(
        "--company",
        action="append",
        dest="companies",
        help="Specific company to check (can be given multiple times)",
    )
    args = parser.parse_args()

    result = await run_orphan_check(
        dry_run=args.dry_run,
        companies=args.companies or None,
    )
    print(result)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
