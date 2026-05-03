"""
Module: db_pools
Purpose: Per-company connection-pool cache for read-only dashboard queries.
Location: /opt/tickles/shared/dashboard/db_pools.py

Phase L is a strict CONSUMER. It NEVER writes. The writer-domain registry
([BC], Phase 10) does NOT list 'dashboard' as an authorised writer for any table.
"""

import logging
from typing import Dict

from shared.utils.db import DatabasePool, get_company_pool as _open

logger = logging.getLogger(__name__)

# Registry of active company pools
_pools: Dict[str, DatabasePool] = {}


async def get_company_pool(company: str) -> DatabasePool:
    """Retrieve or initialize a connection pool for a specific company.

    Args:
        company: Short name of the company (e.g., 'rubicon').

    Returns:
        An initialized DatabasePool instance for tickles_<company>.
    """
    if company not in _pools:
        logger.info("Initializing dashboard connection pool for company: %s", company)
        _pools[company] = await _open(company)
    return _pools[company]


async def close_all_dashboard_pools() -> None:
    """Close all cached company pools."""
    for company, pool in _pools.items():
        logger.info("Closing dashboard connection pool for company: %s", company)
        await pool.close()
    _pools.clear()
