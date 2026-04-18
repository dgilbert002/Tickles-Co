"""Shared Postgres connection pools for all Tickles V2 services.

V2 Postgres edition — replaces aiomysql-based version. Uses asyncpg for
native async Postgres access. Exposes the same interface as the previous
MySQL pool (execute / fetch_one / fetch_all / execute_many) so callers
barely need to change anything.

Key differences from the MySQL version:
  * Two named pools instead of one: `shared` (tickles_shared) and `company`
    (tickles_<company>). Each service grabs whichever it needs.
  * Placeholder translation: legacy "%s" style is auto-converted to the
    native "$1, $2, ..." asyncpg style so old queries still work.
  * Records act like dicts for compatibility with code that does `row['col']`.

Usage:
    from shared.utils import db

    shared = await db.get_shared_pool()
    rows = await shared.fetch_all(
        "SELECT id, symbol FROM instruments WHERE exchange = %s", ("bybit",)
    )

    company = await db.get_company_pool("jarvais")
    await company.execute(
        "INSERT INTO accounts (exchange, account_type) VALUES ($1, $2)",
        ("bybit", "demo"),
    )
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import asyncpg

from shared.utils import config

logger = logging.getLogger("tickles.db")

# Match MySQL-style "%s" placeholders BUT not inside quoted strings.
# Keep it simple: translate outside of single/double-quoted substrings.
_PLACEHOLDER_RX = re.compile(r"%s")


def _translate_placeholders(query: str) -> str:
    """Convert `%s` placeholders to `$1, $2, ...` (asyncpg native).

    This is a pragmatic translator: we count `%s` outside of single-quoted
    regions. If you already wrote `$1` style, the query is returned unchanged.
    """
    if "%s" not in query:
        return query

    # Split by single quotes and only translate in "code" regions (even indices)
    parts = query.split("'")
    out: List[str] = []
    counter = 1
    for i, part in enumerate(parts):
        if i % 2 == 0:
            def _sub(_m: re.Match) -> str:
                nonlocal counter
                token = f"${counter}"
                counter += 1
                return token
            out.append(_PLACEHOLDER_RX.sub(_sub, part))
        else:
            out.append(part)
    return "'".join(out)


class DatabasePool:
    """Async Postgres connection pool targeting a single database.

    One instance per logical database (tickles_shared, tickles_jarvais, etc).

    Legacy compatibility:
      * `DatabasePool.get_instance()` still works — it returns the shared
        pool (tickles_shared) so existing MySQL-era code keeps running.
    """

    # Backwards-compat singleton (shared pool)
    _legacy_instance: Optional["DatabasePool"] = None
    _legacy_lock = asyncio.Lock()

    def __init__(self, dbname: Optional[str] = None) -> None:
        # Default to the shared DB when called with no args (legacy constructor)
        self._dbname: str = dbname or config.DB_NAME_SHARED
        self._pool: Optional[asyncpg.Pool] = None
        self._init_lock = asyncio.Lock()

    @classmethod
    async def get_instance(cls) -> "DatabasePool":
        """Legacy singleton accessor — returns the shared pool.

        New code should use ``get_shared_pool()`` / ``get_company_pool()``.
        """
        if cls._legacy_instance is not None and cls._legacy_instance._pool is not None:
            return cls._legacy_instance
        async with cls._legacy_lock:
            if cls._legacy_instance is None:
                cls._legacy_instance = cls(config.DB_NAME_SHARED)
            await cls._legacy_instance.initialize()
        return cls._legacy_instance

    @property
    def dbname(self) -> str:
        return self._dbname

    async def initialize(self) -> None:
        """Create the underlying asyncpg pool if it hasn't been created yet."""
        if self._pool is not None:
            return
        async with self._init_lock:
            if self._pool is not None:
                return
            try:
                self._pool = await asyncpg.create_pool(
                    host=config.DB_HOST,
                    port=config.DB_PORT,
                    user=config.DB_USER,
                    password=config.DB_PASSWORD,
                    database=self._dbname,
                    min_size=2,
                    max_size=config.DB_POOL_SIZE,
                    command_timeout=30,
                    # Postgres-specific tuning
                    statement_cache_size=200,
                )
                logger.info(
                    "PG pool initialized (host=%s, db=%s, max=%s)",
                    config.DB_HOST, self._dbname, config.DB_POOL_SIZE,
                )
            except (asyncpg.PostgresError, OSError) as exc:
                logger.critical("Failed to init PG pool (%s): %s",
                                self._dbname, exc, exc_info=True)
                raise

    def _check_initialized(self) -> None:
        if self._pool is None:
            raise RuntimeError(
                f"DatabasePool[{self._dbname}] not initialized. "
                f"Call await pool.initialize() first."
            )

    # ------------------------------------------------------------------
    # Main query interface
    # ------------------------------------------------------------------
    async def execute(self, query: str, params: Optional[Tuple] = None) -> int:
        """Run an INSERT / UPDATE / DELETE and return the affected row count.

        Notes:
            asyncpg returns a status string like 'INSERT 0 1'; we parse the
            trailing integer. For statements that don't produce a count
            (e.g. DDL) we return 0.
        """
        self._check_initialized()
        q = _translate_placeholders(query)
        args = params or ()
        try:
            async with self._pool.acquire() as conn:
                status = await conn.execute(q, *args)
            # "INSERT 0 1", "UPDATE 3", "DELETE 2"
            try:
                return int(status.rsplit(" ", 1)[-1])
            except (ValueError, AttributeError):
                return 0
        except asyncpg.PostgresError:
            logger.error("PG execute failed! Query: %s", query, exc_info=True)
            raise

    async def fetch_one(
        self, query: str, params: Optional[Tuple] = None
    ) -> Optional[Dict[str, Any]]:
        """Fetch a single row as a dict (or None if no rows)."""
        self._check_initialized()
        q = _translate_placeholders(query)
        args = params or ()
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(q, *args)
            return dict(row) if row is not None else None
        except asyncpg.PostgresError:
            logger.error("PG fetch_one failed! Query: %s", query, exc_info=True)
            raise

    async def fetch_all(
        self, query: str, params: Optional[Tuple] = None
    ) -> List[Dict[str, Any]]:
        """Fetch all rows as a list of dicts."""
        self._check_initialized()
        q = _translate_placeholders(query)
        args = params or ()
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(q, *args)
            return [dict(r) for r in rows]
        except asyncpg.PostgresError:
            logger.error("PG fetch_all failed! Query: %s", query, exc_info=True)
            raise

    async def fetch_val(
        self, query: str, params: Optional[Tuple] = None
    ) -> Any:
        """Fetch a single scalar value (first column of first row)."""
        self._check_initialized()
        q = _translate_placeholders(query)
        args = params or ()
        try:
            async with self._pool.acquire() as conn:
                return await conn.fetchval(q, *args)
        except asyncpg.PostgresError:
            logger.error("PG fetch_val failed! Query: %s", query, exc_info=True)
            raise

    async def execute_many(
        self, query: str, params_list: List[Tuple]
    ) -> int:
        """Execute one query repeatedly with a list of parameter tuples.

        Uses asyncpg's `executemany` which pipelines the writes — far
        faster than a Python-level loop for batch inserts.
        """
        self._check_initialized()
        if not params_list:
            return 0
        q = _translate_placeholders(query)
        try:
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    await conn.executemany(q, params_list)
            return len(params_list)
        except asyncpg.PostgresError:
            logger.error(
                "PG execute_many failed! Query: %s, Params: %d",
                query, len(params_list), exc_info=True,
            )
            raise

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------
    async def listen(self, channel: str):
        """Return an async context manager for a LISTEN/NOTIFY channel.

        Usage:
            async with pool.listen('candle_inserted') as ch:
                async for notification in ch:
                    print(notification.payload)
        """
        self._check_initialized()
        # Thin wrapper; callers use asyncpg.connect directly for LISTEN
        # because listen loops span a whole connection.
        conn = await asyncpg.connect(
            host=config.DB_HOST, port=config.DB_PORT, user=config.DB_USER,
            password=config.DB_PASSWORD, database=self._dbname,
        )
        await conn.add_listener(channel, lambda *a: None)
        return conn

    def acquire(self):
        """Return the underlying asyncpg pool's connection context manager.

        Use this when you need a lower-level connection for things like
        transactions or prepared statements:

            async with pool.acquire() as conn:
                async with conn.transaction():
                    ...
        """
        self._check_initialized()
        return self._pool.acquire()

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            logger.info("PG pool closed (db=%s)", self._dbname)


# ------------------------------------------------------------------
# Module-level pool registry — one pool per database
# ------------------------------------------------------------------
_pools: Dict[str, DatabasePool] = {}
_registry_lock = asyncio.Lock()


async def _get_pool(dbname: str) -> DatabasePool:
    async with _registry_lock:
        pool = _pools.get(dbname)
        if pool is None:
            pool = DatabasePool(dbname)
            _pools[dbname] = pool
    await pool.initialize()
    return pool


async def get_shared_pool() -> DatabasePool:
    """Pool for the shared-infrastructure database (tickles_shared)."""
    return await _get_pool(config.DB_NAME_SHARED)


async def get_company_pool(company: str) -> DatabasePool:
    """Pool for a specific company database (tickles_<company>)."""
    return await _get_pool(f"tickles_{company}")


async def get_pool(dbname: str) -> DatabasePool:
    """Pool for an arbitrary database (power-user use)."""
    return await _get_pool(dbname)


async def close_all_pools() -> None:
    """Close every pool — typically only used in tests/shutdown hooks."""
    async with _registry_lock:
        for pool in list(_pools.values()):
            await pool.close()
        _pools.clear()
