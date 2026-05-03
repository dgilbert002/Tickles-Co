"""
Module: heartbeat
Purpose: Universal heartbeat helper for cron-driven agents.
Location: /opt/tickles/shared/intelligence/heartbeat.py
"""

import logging
from typing import Literal, Optional
from datetime import datetime
from shared.utils.db import get_shared_pool

logger = logging.getLogger(__name__)

Status = Literal["ok", "error", "partial"]

async def record_heartbeat(
    agent_id: str,
    status: Status,
    expected_interval_seconds: int,
    message: Optional[str] = None,
) -> None:
    """
    Record a heartbeat for a cron-driven agent.
    
    Args:
        agent_id: Unique identifier for the agent.
        status: Current status of the agent run.
        expected_interval_seconds: How often the agent is expected to run.
        message: Optional status message or error details.
    """
    pool = await get_shared_pool()
    try:
        # We use a native asyncpg UPSERT
        await pool.execute(
            """
            INSERT INTO public.cron_heartbeats (
                agent_id, last_run_at, last_status, last_message, 
                expected_interval_seconds, consecutive_failures, updated_at
            )
            VALUES ($1, now(), $2, $3, $4, CASE WHEN $2 = 'ok' THEN 0 ELSE 1 END, now())
            ON CONFLICT (agent_id) DO UPDATE
            SET 
                consecutive_failures = CASE 
                    WHEN EXCLUDED.last_status = 'ok' THEN 0 
                    ELSE public.cron_heartbeats.consecutive_failures + 1 
                END,
                last_run_at = EXCLUDED.last_run_at,
                last_status = EXCLUDED.last_status,
                last_message = EXCLUDED.last_message,
                expected_interval_seconds = EXCLUDED.expected_interval_seconds,
                updated_at = now()
            """,
            (agent_id, status, message, expected_interval_seconds),
        )
        logger.debug(f"Heartbeat recorded for {agent_id}: {status}")
    except Exception as e:
        logger.error(f"Failed to record heartbeat for {agent_id}: {e}")
        # We don't raise here to avoid crashing the agent just because heartbeat failed
