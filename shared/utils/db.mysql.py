"""Shared MySQL connection pool for all Tickles V2 services.

Uses aiomysql for async operations. Reads config from `shared.utils.config`.

Singleton pattern — call `await DatabasePool.get_instance()` to get the pool.
"""

import asyncio
import logging
from typing import Optional, List, Dict, Any

import aiomysql

from shared.utils import config

logger = logging.getLogger("tickles.db")


class DatabasePool:
    """Async MySQL Connection Pool singleton."""
    _instance: Optional['DatabasePool'] = None
    _lock = asyncio.Lock()

    def __init__(self) -> None:
        self._pool: Optional[aiomysql.Pool] = None

    @classmethod
    async def get_instance(cls) -> 'DatabasePool':
        """Get the singleton instance of the database pool, with async-safe initialization."""
        # Fast path without lock for already initialized instance
        if cls._instance is not None:
            return cls._instance
            
        async with cls._lock:
            # Double-check inside lock to prevent race conditions
            if cls._instance is None:
                cls._instance = cls()
                await cls._instance.initialize()
        return cls._instance

    async def initialize(self) -> None:
        """Initialize the database connection pool using centralized config."""
        if self._pool:
            return

        try:
            self._pool = await aiomysql.create_pool(
                host=config.DB_HOST,
                port=config.DB_PORT,
                user=config.DB_USER,
                password=config.DB_PASSWORD,
                db=config.DB_NAME,
                minsize=2,
                maxsize=config.DB_POOL_SIZE,
                autocommit=True,
                charset='utf8mb4',
                connect_timeout=10  # Add a reasonable connection timeout
            )
            logger.info(
                "Database pool initialized (host=%s, db=%s, maxsize=%s)",
                config.DB_HOST, config.DB_NAME, config.DB_POOL_SIZE
            )
        except (aiomysql.Error, OSError) as e:
            logger.critical("Failed to initialize database pool: %s", e, exc_info=True)
            # This is a critical failure, the application cannot run without a database.
            # Re-raising allows the app's main loop to catch it and exit gracefully.
            raise

    def _check_initialized(self) -> None:
        """Raise a RuntimeError if the pool hasn't been initialized."""
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized. Call await DatabasePool.get_instance() first.")

    async def execute(self, query: str, params: tuple = None) -> int:
        """Execute a write query (INSERT, UPDATE, DELETE). Returns affected row count."""
        self._check_initialized()
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(query, params)
                    return cur.rowcount
        except aiomysql.Error as e:
            logger.error("DB execute failed! Query: %s", query, exc_info=True)
            raise

    async def fetch_one(self, query: str, params: tuple = None) -> Optional[Dict[str, Any]]:
        """Fetch a single row as a dictionary."""
        self._check_initialized()
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(query, params)
                    return await cur.fetchone()
        except aiomysql.Error as e:
            logger.error("DB fetch_one failed! Query: %s", query, exc_info=True)
            raise

    async def fetch_all(self, query: str, params: tuple = None) -> List[Dict[str, Any]]:
        """Fetch all rows as a list of dictionaries."""
        self._check_initialized()
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(query, params)
                    return await cur.fetchall()
        except aiomysql.Error as e:
            logger.error("DB fetch_all failed! Query: %s", query, exc_info=True)
            raise

    async def execute_many(self, query: str, params_list: List[tuple]) -> int:
        """Execute a query with multiple parameter sets (e.g., for batch inserts)."""
        self._check_initialized()
        if not params_list:
            return 0
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.executemany(query, params_list)
                    return cur.rowcount
        except aiomysql.Error as e:
            # Only log the query and the number of params to avoid huge log entries
            logger.error("DB execute_many failed! Query: %s, Param count: %d", query, len(params_list), exc_info=True)
            raise

    async def close(self) -> None:
        """Gracefully close the database connection pool."""
        if self._pool:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None
            # Reset singleton instance to allow re-initialization, e.g., in tests.
            type(self)._instance = None
            logger.info("Database pool closed successfully.")
