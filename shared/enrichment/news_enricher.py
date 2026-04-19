"""
NewsEnricher — DB-backed worker that applies :class:`Pipeline` to
pending rows in ``public.news_items``.

  * Reads from ``news_items_pending_enrichment`` view (rows
    where ``enriched_at IS NULL``).
  * Runs the pipeline.
  * Writes ``sentiment`` (text label), ``instruments`` (jsonb),
    ``enrichment`` (jsonb), and ``enriched_at`` back.
  * All DB access uses :class:`shared.utils.db.DatabasePool` if
    available (tickles_shared @ 127.0.0.1:5432). Falls back to a
    direct :mod:`asyncpg` connect via ``TICKLES_SHARED_DSN``.

This worker is idempotent: re-running it on already-enriched rows
skips them via the `WHERE enriched_at IS NULL` filter.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from shared.enrichment.pipeline import Pipeline, build_default_pipeline

logger = logging.getLogger("tickles.enrichment.news")


@dataclass
class EnricherConfig:
    batch_size: int = 100
    max_rows_per_run: int = 1000
    dsn: Optional[str] = None  # overrides DatabasePool when set


class NewsEnricher:
    """Apply the enrichment pipeline to pending news_items."""

    def __init__(
        self,
        config: Optional[EnricherConfig] = None,
        pipeline: Optional[Pipeline] = None,
    ) -> None:
        self.config = config or EnricherConfig()
        self.pipeline = pipeline or build_default_pipeline()

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    async def _connect(self) -> Tuple[Any, bool]:
        """Return (conn_or_pool, is_pool)."""
        if self.config.dsn:
            import asyncpg  # type: ignore

            conn = await asyncpg.connect(self.config.dsn)
            return conn, False
        try:
            from shared.utils.db import get_shared_pool  # type: ignore

            pool = await get_shared_pool()
            return pool, True
        except Exception:
            dsn = os.environ.get("TICKLES_SHARED_DSN")
            if not dsn:
                raise RuntimeError(
                    "No DSN available: set TICKLES_SHARED_DSN or install shared.utils.db"
                )
            import asyncpg  # type: ignore

            conn = await asyncpg.connect(dsn)
            return conn, False

    async def _close(self, handle: Any, is_pool: bool) -> None:
        if is_pool:
            return
        try:
            await handle.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_pending(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        limit = int(limit or self.config.batch_size)
        sql = (
            "SELECT id, headline, content FROM public.news_items "
            "WHERE enriched_at IS NULL ORDER BY collected_at ASC LIMIT $1"
        )
        handle, is_pool = await self._connect()
        try:
            if is_pool:
                async with handle.acquire() as conn:
                    rows = await conn.fetch(sql, limit)
            else:
                rows = await handle.fetch(sql, limit)
            return [dict(r) for r in rows]
        finally:
            await self._close(handle, is_pool)

    async def enrich_batch(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Run the pipeline against an in-memory batch (no DB writes)."""
        summaries: List[Dict[str, Any]] = []
        for row in rows:
            result = self.pipeline.run(
                headline=row.get("headline", "") or "",
                content=row.get("content", "") or "",
                news_item_id=row.get("id"),
            )
            summaries.append(result.summary())
        return summaries

    async def process_pending(self, max_rows: Optional[int] = None) -> Dict[str, Any]:
        max_rows = int(max_rows or self.config.max_rows_per_run)
        total_scanned = 0
        total_updated = 0
        handle, is_pool = await self._connect()
        try:
            while total_scanned < max_rows:
                batch_limit = min(self.config.batch_size, max_rows - total_scanned)
                sql = (
                    "SELECT id, headline, content FROM public.news_items "
                    "WHERE enriched_at IS NULL ORDER BY collected_at ASC LIMIT $1"
                )
                if is_pool:
                    async with handle.acquire() as conn:
                        rows = await conn.fetch(sql, batch_limit)
                else:
                    rows = await handle.fetch(sql, batch_limit)
                if not rows:
                    break
                total_scanned += len(rows)

                for row in rows:
                    result = self.pipeline.run(
                        headline=row["headline"] or "",
                        content=row["content"] or "",
                        news_item_id=row["id"],
                    )
                    db_row = result.to_db_row()
                    update_sql = (
                        "UPDATE public.news_items SET "
                        "sentiment = $1, "
                        "instruments = $2::jsonb, "
                        "enrichment = $3::jsonb, "
                        "enriched_at = $4 "
                        "WHERE id = $5"
                    )
                    params = [
                        db_row["sentiment"],
                        json.dumps(db_row["instruments"]),
                        json.dumps(db_row["enrichment"]),
                        datetime.now(timezone.utc),
                        row["id"],
                    ]
                    if is_pool:
                        async with handle.acquire() as conn:
                            await conn.execute(update_sql, *params)
                    else:
                        await handle.execute(update_sql, *params)
                    total_updated += 1

                if len(rows) < batch_limit:
                    break
        finally:
            await self._close(handle, is_pool)

        return {
            "ok": True,
            "scanned": total_scanned,
            "updated": total_updated,
        }

    async def pending_count(self) -> int:
        sql = "SELECT count(*) AS c FROM public.news_items WHERE enriched_at IS NULL"
        handle, is_pool = await self._connect()
        try:
            if is_pool:
                async with handle.acquire() as conn:
                    row = await conn.fetchrow(sql)
            else:
                row = await handle.fetchrow(sql)
            return int(row["c"]) if row else 0
        finally:
            await self._close(handle, is_pool)


# ---------------------------------------------------------------------------
# Sync convenience
# ---------------------------------------------------------------------------


def enrich_text_once(headline: str, content: str) -> Dict[str, Any]:
    """One-shot helper used by the CLI + tests; no DB required."""
    pipeline = build_default_pipeline()
    return pipeline.run(headline=headline, content=content).summary()


def run_pending_sync(enricher: Optional[NewsEnricher] = None) -> Dict[str, Any]:
    enricher = enricher or NewsEnricher()
    return asyncio.run(enricher.process_pending())
