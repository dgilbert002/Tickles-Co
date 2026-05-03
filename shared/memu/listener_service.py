"""
Module: listener_service
Purpose: MemU outbox listener daemon — durable broadcast pipeline.
Location: /opt/tickles/shared/memu/listener_service.py
"""

import asyncio
import json
import logging
import os
import signal
import sys
from typing import Optional

import asyncpg

sys.path.insert(0, "/opt/tickles")

from shared.memu.broadcast_payload import BroadcastPayload
from shared.memu.client import get_memu

logger = logging.getLogger("tickles.memu_listener")

_DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
_DB_PORT = int(os.getenv("DB_PORT", "5432"))
_DB_USER = os.getenv("DB_USER", "admin")
_DB_PASSWORD = os.getenv("DB_PASSWORD", "")
_DB_NAME = os.getenv("DB_NAME_SHARED", "tickles_shared")


class MemuListenerService:
    """Listens to pg_notify('memu_broadcast') and processes outbox rows."""

    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self._conn: Optional[asyncpg.Connection] = None

    async def _connect(self) -> asyncpg.Connection:
        return await asyncpg.connect(
            host=_DB_HOST,
            port=_DB_PORT,
            user=_DB_USER,
            password=_DB_PASSWORD,
            database=_DB_NAME,
        )

    async def _backfill(self, conn: asyncpg.Connection) -> None:
        """Process every unprocessed outbox row (belt-and-braces sweep)."""
        rows = await conn.fetch(
            "SELECT id FROM public.memu_outbox WHERE processed_at IS NULL ORDER BY id"
        )
        for row in rows:
            if self._stop.is_set():
                break
            await self._process_row(conn, row["id"])

    async def _process_row(self, conn: asyncpg.Connection, row_id: int) -> None:
        """Fetch outbox row, write to MemU, mark processed."""
        row = await conn.fetchrow(
            "SELECT id, payload FROM public.memu_outbox "
            "WHERE id=$1 AND processed_at IS NULL FOR UPDATE SKIP LOCKED",
            row_id,
        )
        if not row:
            return
        raw = row["payload"]
        # asyncpg deserialises JSONB to dict automatically; json.loads only if string
        payload: BroadcastPayload = json.loads(raw) if isinstance(raw, str) else raw
        try:
            memu = get_memu()
            # Map BroadcastPayload fields to MemU write_insight signature
            memu.write_insight(
                kind=payload["insight_kind"],
                content=payload["body_md"],
                source_agent=payload["actor_id"],
                metadata={
                    "company": payload["company"],
                    "actor_type": payload["actor_type"],
                    "summary": payload["summary"],
                    "instrument_symbol_normalised": payload.get("instrument_symbol_normalised"),
                    "instrument_exchange": payload.get("instrument_exchange"),
                    "position_id": payload.get("position_id"),
                    "correlation_id": payload["correlation_id"],
                    "created_at_iso": payload["created_at_iso"],
                    "schema_version": payload["schema_version"],
                },
            )
            await conn.execute(
                "UPDATE public.memu_outbox SET processed_at=now() WHERE id=$1", row_id
            )
            logger.info("memu_listener: processed outbox row %s", row_id)
        except Exception as exc:
            await conn.execute(
                "UPDATE public.memu_outbox SET attempt_count=attempt_count+1, last_error=$2 "
                "WHERE id=$1",
                row_id,
                str(exc)[:500],
            )
            logger.warning("memu_listener: row %s failed (attempt++): %s", row_id, exc)
            raise

    def _on_notify(self, connection, pid, channel, payload) -> None:
        """Asyncpg listener callback — schedules row processing."""
        try:
            row_id = int(payload)
        except (ValueError, TypeError):
            logger.warning("memu_listener: invalid notify payload: %s", payload)
            return
        # Schedule processing on the running event loop
        task = asyncio.create_task(self._process_row(connection, row_id))

        def _log_exc(t: asyncio.Task) -> None:
            exc = t.exception()
            if exc:
                logger.exception(
                    "memu_listener: notify task failed for row %s: %s", row_id, exc
                )

        task.add_done_callback(_log_exc)

    async def run_forever(self) -> None:
        """Main loop: connect, backfill, listen, reconnect on failure."""
        backoff = 1.0
        while not self._stop.is_set():
            conn: Optional[asyncpg.Connection] = None
            try:
                conn = await self._connect()
                self._conn = conn
                backoff = 1.0
                await conn.add_listener("memu_broadcast", self._on_notify)
                await self._backfill(conn)
                while not self._stop.is_set():
                    await asyncio.sleep(60)
                    await self._backfill(conn)
            except (asyncpg.PostgresConnectionError, OSError) as exc:
                logger.warning("memu_listener disconnected: %s; backoff=%.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
            except Exception as exc:
                logger.exception("memu_listener: unexpected error: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
            finally:
                if conn is not None:
                    try:
                        await conn.close()
                    except Exception:
                        pass
                self._conn = None

    def stop(self) -> None:
        self._stop.set()


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    svc = MemuListenerService()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, svc.stop)
    await svc.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
