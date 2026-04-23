"""
Module: db_helper
Purpose: Lightweight synchronous Postgres access for MCP data tools.
Location: /opt/tickles/shared/mcp/tools/db_helper.py

Uses psycopg2 for simple read queries (md.quote, md.candles, candles.coverage).
Connection pooling is overkill for the MCP daemon's low query volume; instead
we open/close per-call with a 10-second connect timeout to avoid blocking
the event loop if Postgres is unreachable.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

_CONN_KWARGS: Optional[Dict[str, Any]] = None


def _get_conn_kwargs() -> Dict[str, Any]:
    """Build Postgres connection kwargs from environment variables (loaded once).

    Uses keyword arguments instead of a DSN string so that passwords
    containing special characters (spaces, #, =) are handled correctly.
    """
    global _CONN_KWARGS
    if _CONN_KWARGS is not None:
        return _CONN_KWARGS
    _CONN_KWARGS = {
        "host": os.environ.get("DB_HOST", "127.0.0.1"),
        "port": os.environ.get("DB_PORT", "5432"),
        "user": os.environ.get("DB_USER", "admin"),
        "password": os.environ.get("DB_PASSWORD", ""),
        "dbname": os.environ.get("DB_NAME_SHARED", "tickles_shared"),
        "connect_timeout": 10,
    }
    return _CONN_KWARGS


def query(sql: str, params: tuple | None = None) -> List[Dict[str, Any]]:
    """Execute a read-only SQL query and return rows as list of dicts.

    Args:
        sql: SQL query with %s placeholders.
        params: Query parameters (use tuple for safety).

    Returns:
        List of row dicts keyed by column name.
    """
    try:
        conn = psycopg2.connect(**_get_conn_kwargs())
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql, params)
            rows = cur.fetchall()
            cur.close()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except psycopg2.OperationalError as exc:
        logger.error("[db_helper] connection failed: %s", exc)
        raise RuntimeError(f"Database connection failed: {exc}") from exc
    except psycopg2.Error as exc:
        logger.error("[db_helper] query failed: %s", exc)
        raise RuntimeError(f"Database query failed: {exc}") from exc


def execute(sql: str, params: tuple | None = None) -> int:
    """Execute a write SQL statement and return affected row count.

    Args:
        sql: SQL statement with %s placeholders.
        params: Statement parameters.

    Returns:
        Number of affected rows.
    """
    try:
        conn = psycopg2.connect(**_get_conn_kwargs())
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            affected = cur.rowcount
            conn.commit()
            cur.close()
            return affected
        finally:
            conn.close()
    except psycopg2.OperationalError as exc:
        logger.error("[db_helper] connection failed: %s", exc)
        raise RuntimeError(f"Database connection failed: {exc}") from exc
    except psycopg2.Error as exc:
        logger.error("[db_helper] execute failed: %s", exc)
        raise RuntimeError(f"Database execute failed: {exc}") from exc


def resolve_instrument_id(symbol: str, venue: str) -> Optional[int]:
    """Look up instrument_id for a symbol/exchange pair.

    Args:
        symbol: Trading pair (e.g., 'BTC/USDT').
        venue: Exchange name (e.g., 'bybit').

    Returns:
        instrument_id or None if not found.
    """
    rows = query(
        "SELECT id FROM instruments WHERE symbol = %s AND exchange = %s AND is_active = true LIMIT 1",
        (symbol, venue),
    )
    return rows[0]["id"] if rows else None
