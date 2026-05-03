"""Module: companies
Purpose: Canonical company enumeration helper for per-company iteration.
Location: /opt/tickles/shared/utils/companies.py
"""

import asyncio
import logging
import os
import time
from typing import Awaitable, Callable, TypeVar

import asyncpg

from shared.utils.db import get_shared_pool

logger = logging.getLogger(__name__)

T = TypeVar("T")
_CACHE: tuple[float, list[str]] | None = None
_CACHE_TTL = 300  # 5 minutes


async def list_active_companies() -> list[str]:
    """Return list of active company short-names (e.g., ['rubicon']).

    Source of truth: tickles_shared.public.companies WHERE is_active=true.
    Falls back to env ACTIVE_COMPANIES if table missing.
    Caches for 5 minutes to avoid N+1 queries in loops.

    Returns:
        List of active company short names, sorted alphabetically.
        'jarvais' is always excluded (frozen legacy V1).
    """
    global _CACHE
    now = time.monotonic()
    if _CACHE and now - _CACHE[0] < _CACHE_TTL:
        return _CACHE[1]

    pool = await get_shared_pool()
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                "SELECT short_name FROM companies WHERE is_active = true ORDER BY short_name"
            )
            names = [r["short_name"] for r in rows if r["short_name"] != "jarvais"]
        except asyncpg.UndefinedTableError:
            raw = os.getenv("ACTIVE_COMPANIES", "rubicon")
            names = sorted(n.strip() for n in raw.split(",") if n.strip())
            logger.warning(
                "companies table missing — falling back to ACTIVE_COMPANIES=%s",
                names,
            )

    _CACHE = (now, names)
    logger.debug("list_active_companies() -> %s (cached for %ds)", names, _CACHE_TTL)
    return names


def invalidate_cache() -> None:
    """Force the next call to hit the database.

    Call this after any DDL or DML that changes company activation status.
    """
    global _CACHE
    _CACHE = None
    logger.info("Company list cache invalidated")


async def get_company_dsn(company: str) -> str:
    """Build a company DSN from TICKLES_DB_DSN_TEMPLATE.

    Template must contain a {db} placeholder, e.g.:
        postgresql://user:pass@host:5432/{db}

    Args:
        company: Short company name (e.g. 'rubicon').

    Returns:
        Connection string for tickles_<company> database.

    Raises:
        KeyError: If TICKLES_DB_DSN_TEMPLATE env var is missing and no default
            template can be constructed from DB_* env vars.
    """
    template = os.environ.get(
        "TICKLES_DB_DSN_TEMPLATE",
        "postgresql://{user}:{password}@{host}:{port}/{db}",
    )
    # If template still has legacy placeholders, fill from env
    if "{user}" in template:
        template = template.format(
            user=os.getenv("DB_USER", "tickles"),
            password=os.getenv("DB_PASSWORD", ""),
            host=os.getenv("DB_HOST", "localhost"),
            port=os.getenv("DB_PORT", "5432"),
            db="{db}",
        )
    return template.format(db=f"tickles_{company}")


async def for_each_company(
    fn: Callable[[str, asyncpg.Connection], Awaitable[T]],
) -> dict[str, T | Exception]:
    """Fan-out a function across all active companies.

    Returns {company: result_or_exception}.  Exceptions are captured,
    not raised, so one failing company does not abort the rest.

    Args:
        fn: Async callable taking (company_name, connection).

    Returns:
        Dictionary mapping company name to either the function result
        or the Exception that was raised for that company.

    Example::

        async def count_positions(company: str, conn: asyncpg.Connection) -> int:
            row = await conn.fetchrow("SELECT COUNT(*) FROM tracked_positions")
            return row["count"]

        results = await for_each_company(count_positions)
        for company, result in results.items():
            if isinstance(result, Exception):
                logger.error("%s failed: %s", company, result)
            else:
                logger.info("%s has %d positions", company, result)
    """
    names = await list_active_companies()

    async def _one(c: str) -> T | Exception:
        try:
            dsn = await get_company_dsn(c)
            async with asyncpg.connect(dsn) as conn:
                return await fn(c, conn)
        except Exception as exc:
            logger.exception("for_each_company: %s failed", c)
            return exc

    results = await asyncio.gather(*[_one(c) for c in names], return_exceptions=False)
    return dict(zip(names, results))
