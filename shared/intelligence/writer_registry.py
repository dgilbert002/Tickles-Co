"""
Module: writer_registry
Purpose: Writer-domain registry — the single source of truth for "which service is allowed to write to which table".
Location: /opt/tickles/shared/intelligence/writer_registry.py
"""

import logging
from typing import List
from shared.utils.db import get_shared_pool

logger = logging.getLogger(__name__)

async def register_writer(table_name: str, service_name: str) -> None:
    """
    Idempotently add `service_name` to the allow-list for `table_name`.
    
    Args:
        table_name: The name of the table.
        service_name: The name of the service to authorise.
    """
    pool = await get_shared_pool()
    # Note: get_shared_pool() returns a wrapper that has execute()
    try:
        await pool.execute(
            """
            INSERT INTO public.table_writers (table_name, allowed_writer_services)
            VALUES ($1, ARRAY[$2]::TEXT[])
            ON CONFLICT (table_name) DO UPDATE
              SET allowed_writer_services = (
                    SELECT ARRAY_AGG(DISTINCT x)
                    FROM unnest(public.table_writers.allowed_writer_services || EXCLUDED.allowed_writer_services) AS x
                  ),
                  updated_at = now()
            """,
            (table_name, service_name),
        )
    except Exception as e:
        logger.error(f"Failed to register writer {service_name} for table {table_name}: {e}")
        raise

async def assert_authorised(table_name: str, service_name: str) -> None:
    """
    Runtime assertion (called from service boot). Raises on mismatch.
    
    Args:
        table_name: The name of the table.
        service_name: The name of the service to check.
        
    Raises:
        RuntimeError: If the service is not authorised to write to the table.
    """
    pool = await get_shared_pool()
    row = await pool.fetch_one(
        "SELECT allowed_writer_services FROM public.table_writers WHERE table_name=$1",
        (table_name,),
    )
    
    if row is None:
        logger.warning("table_writers: no entry for %s — registering %s", table_name, service_name)
        await register_writer(table_name, service_name)
        return
        
    allowed = row.get("allowed_writer_services") or []
    if service_name not in allowed:
        raise RuntimeError(
            f"writer_registry violation: service '{service_name}' is not authorised "
            f"to write to '{table_name}'. Allowed: {allowed}"
        )
