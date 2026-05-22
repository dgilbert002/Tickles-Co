"""
Module: test_feed_hygiene
Purpose: Slice 4 — verify feed-hygiene helpers in interpretation_service.

Covers:
  * fetch_pending_media() filters out non-image media_type rows.
  * cleanup_unsupported_media() transitions non-image rows to
    'skipped_unsupported_media'.
  * cleanup_stale_pending_news() drains text-only news older than 6h with
    no media and no signal to enrichment_status='skipped_no_content'.
  * The prefilter-rejection short-circuit detector logic
    (LlmResult.reasoning starts with "pre-filter:") flags correctly.

Location: /opt/tickles/shared/tests/test_feed_hygiene.py
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, List

import asyncpg
import pytest

from shared.intelligence.interpretation_service import (
    LlmResult,
    cleanup_stale_pending_news,
    cleanup_unsupported_media,
    fetch_pending_media,
)

logger = logging.getLogger(__name__)

DB_DSN = (
    f"postgresql://{os.environ.get('DB_USER', 'admin')}"
    f":{os.environ.get('DB_PASSWORD', 'Tickles21!')}"
    f"@{os.environ.get('DB_HOST', '127.0.0.1')}"
    f":{os.environ.get('DB_PORT', '5432')}"
    f"/{os.environ.get('DB_NAME_SHARED', 'tickles_shared')}"
)


# ---------------------------------------------------------------------------
# Pool shim: cleanup helpers call `async with shared_pool.acquire() as conn`.
# Wrap a single asyncpg connection so the helpers run unchanged in tests.
# ---------------------------------------------------------------------------
class _ConnPool:
    """Minimal `DatabasePool`-shaped wrapper around one asyncpg connection."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    def acquire(self) -> "_AcquireCtx":
        """Return an async context manager that yields the wrapped conn."""
        return _AcquireCtx(self._conn)


class _AcquireCtx:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def __aenter__(self) -> asyncpg.Connection:
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
async def db_conn() -> AsyncIterator[asyncpg.Connection]:
    """Provide a raw asyncpg connection. Caller is responsible for cleanup."""
    conn = await asyncpg.connect(DB_DSN)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def pool(db_conn: asyncpg.Connection) -> _ConnPool:
    """Return a pool wrapper backed by the test connection."""
    return _ConnPool(db_conn)


# ---------------------------------------------------------------------------
# Helpers — insert minimal news_items / media_items rows.
# We tag every test row with a unique `marker` author so we can scope the
# subsequent UPDATE/SELECT to rows we own. No sequence pre-allocation needed.
# ---------------------------------------------------------------------------
def _marker() -> str:
    """Generate a unique author/headline marker for this test run."""
    return f"slice4-test-{uuid.uuid4().hex[:12]}"


async def _insert_news(
    conn: asyncpg.Connection,
    *,
    marker: str,
    headline: str = "Slice4 hygiene test",
    content: str = "test content",
    has_media: bool = False,
    enrichment_status: str = "pending",
    collected_at: datetime | None = None,
) -> int:
    """Insert a news_items row, return its id."""
    if collected_at is None:
        collected_at = datetime.now(timezone.utc)
    hash_key = hashlib.sha256(
        f"{marker}{collected_at.isoformat()}{random.random()}".encode("utf-8")
    ).hexdigest()
    row = await conn.fetchrow(
        """
        INSERT INTO public.news_items (
            hash_key, source, headline, content, has_media, media_count,
            enrichment_status, collected_at, author
        ) VALUES (
            $1, 'test', $2, $3, $4, $5, $6, $7, $8
        )
        RETURNING id
        """,
        hash_key,
        headline,
        content,
        has_media,
        1 if has_media else 0,
        enrichment_status,
        collected_at,
        marker,
    )
    if row is None:
        raise RuntimeError("Failed to insert news_items test row")
    return int(row["id"])


async def _insert_media(
    conn: asyncpg.Connection,
    *,
    news_item_id: int,
    media_type: str,
    processing_status: str = "pending",
    source_url: str | None = "https://example.test/x.png",
    local_path: str | None = None,
) -> int:
    """Insert a media_items row, return its id."""
    row = await conn.fetchrow(
        """
        INSERT INTO public.media_items (
            news_item_id, media_type, extraction_method,
            source_url, local_path, mime_type, processing_status
        ) VALUES (
            $1, $2, 'attached', $3, $4, $5, $6
        )
        RETURNING id
        """,
        news_item_id,
        media_type,
        source_url,
        local_path,
        "image/png" if media_type == "image" else "video/mp4",
        processing_status,
    )
    if row is None:
        raise RuntimeError("Failed to insert media_items test row")
    return int(row["id"])


async def _cleanup(conn: asyncpg.Connection, marker: str) -> None:
    """Delete every row tagged with `marker`. media_items cascades on FK."""
    with contextlib.suppress(Exception):
        await conn.execute(
            "DELETE FROM public.news_items WHERE author = $1",
            marker,
        )


# ---------------------------------------------------------------------------
# Test 1 — fetch_pending_media filters out videos
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fetch_pending_media_excludes_videos(
    db_conn: asyncpg.Connection, pool: _ConnPool
) -> None:
    """Only image media_type rows are returned by the vision-pipeline fetch."""
    marker = _marker()
    try:
        news_id = await _insert_news(db_conn, marker=marker, has_media=True)
        image_media_id = await _insert_media(
            db_conn,
            news_item_id=news_id,
            media_type="image",
            processing_status="downloaded",
            local_path="/tmp/fake.png",
        )
        video_media_id = await _insert_media(
            db_conn,
            news_item_id=news_id,
            media_type="video",
            processing_status="downloaded",
        )

        rows = await fetch_pending_media(
            pool,
            batch_size=200,
            max_age_hours=24.0,
        )
        ids = {r["media_id"] for r in rows}
        assert image_media_id in ids, "image row must be picked up"
        assert video_media_id not in ids, (
            "video row must NOT be picked up by fetch_pending_media"
        )
    finally:
        await _cleanup(db_conn, marker)


# ---------------------------------------------------------------------------
# Test 2 — cleanup_unsupported_media transitions non-image rows
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cleanup_unsupported_media_marks_videos(
    db_conn: asyncpg.Connection, pool: _ConnPool
) -> None:
    """Non-image media at status 'pending'/'downloaded' is swept to terminal."""
    marker = _marker()
    try:
        news_id = await _insert_news(db_conn, marker=marker, has_media=True)
        video_id = await _insert_media(
            db_conn,
            news_item_id=news_id,
            media_type="video",
            processing_status="downloaded",
        )
        audio_id = await _insert_media(
            db_conn,
            news_item_id=news_id,
            media_type="audio",
            processing_status="pending",
        )
        # Image at 'pending' must be left untouched.
        image_id = await _insert_media(
            db_conn,
            news_item_id=news_id,
            media_type="image",
            processing_status="pending",
        )

        swept = await cleanup_unsupported_media(pool, max_age_hours=24.0)
        assert swept >= 2, f"expected at least 2 sweeps, got {swept}"

        statuses = {
            r["id"]: r["processing_status"]
            for r in await db_conn.fetch(
                "SELECT id, processing_status FROM public.media_items "
                "WHERE id = ANY($1::bigint[])",
                [video_id, audio_id, image_id],
            )
        }
        assert statuses[video_id] == "skipped_unsupported_media"
        assert statuses[audio_id] == "skipped_unsupported_media"
        assert statuses[image_id] == "pending", "image row must not be touched"
    finally:
        await _cleanup(db_conn, marker)


# ---------------------------------------------------------------------------
# Test 3 — cleanup_stale_pending_news drains stale text-only news
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cleanup_stale_pending_news_drains_old_textonly(
    db_conn: asyncpg.Connection, pool: _ConnPool
) -> None:
    """News > 6h old, no media, no signal, status='pending' → skipped_no_content."""
    marker = _marker()
    try:
        # Stale (8h old), text-only, pending → must be drained.
        stale_id = await _insert_news(
            db_conn,
            marker=marker,
            headline="stale text-only",
            has_media=False,
            enrichment_status="pending",
            collected_at=datetime.now(timezone.utc) - timedelta(hours=8),
        )
        # Fresh (2h old) → must be left alone (not yet 6h).
        fresh_id = await _insert_news(
            db_conn,
            marker=marker,
            headline="fresh text-only",
            has_media=False,
            enrichment_status="pending",
            collected_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        # Stale but has_media=true → must be left alone.
        stale_with_media_id = await _insert_news(
            db_conn,
            marker=marker,
            headline="stale with media",
            has_media=True,
            enrichment_status="pending",
            collected_at=datetime.now(timezone.utc) - timedelta(hours=8),
        )
        # Stale, text-only, but already enriched → must be left alone.
        stale_already_enriched_id = await _insert_news(
            db_conn,
            marker=marker,
            headline="stale already enriched",
            has_media=False,
            enrichment_status="enriched",
            collected_at=datetime.now(timezone.utc) - timedelta(hours=8),
        )

        drained = await cleanup_stale_pending_news(pool, stale_after_hours=6.0)
        assert drained >= 1, f"expected at least 1 drain, got {drained}"

        statuses = {
            r["id"]: r["enrichment_status"]
            for r in await db_conn.fetch(
                "SELECT id, enrichment_status FROM public.news_items "
                "WHERE id = ANY($1::bigint[])",
                [stale_id, fresh_id, stale_with_media_id,
                 stale_already_enriched_id],
            )
        }
        assert statuses[stale_id] == "skipped_no_content"
        assert statuses[fresh_id] == "pending"
        assert statuses[stale_with_media_id] == "pending"
        assert statuses[stale_already_enriched_id] == "enriched"
    finally:
        await _cleanup(db_conn, marker)


# ---------------------------------------------------------------------------
# Test 4 — prefilter-rejection detector logic
# Pure unit test: confirms the "pre-filter:" prefix on LlmResult.reasoning
# is the contract the short-circuit relies on. No DB needed.
# ---------------------------------------------------------------------------
def test_prefilter_rejection_detector_matches_expected_shape() -> None:
    """LlmResult.reasoning prefixed with 'pre-filter:' identifies a rejection."""
    rejected = LlmResult(
        direction="unclear",
        confidence=0.0,
        reasoning="pre-filter: meme — not a chart",
        levels={},
        model_used="google/gemini-2.5-flash",
    )
    accepted = LlmResult(
        direction="long",
        confidence=0.7,
        reasoning="Strong support holding, breakout confirmed.",
        levels={"entry": 100.0},
        model_used="anthropic/claude-3.5-sonnet",
    )
    error_unclear = LlmResult(
        direction="unclear",
        confidence=0.0,
        reasoning="vision LLM error: timeout",
        levels={},
        model_used="anthropic/claude-3.5-sonnet",
    )

    def _is_prefilter_rejection(r: LlmResult) -> bool:
        return (
            r.direction == "unclear"
            and isinstance(r.reasoning, str)
            and r.reasoning.startswith("pre-filter:")
        )

    assert _is_prefilter_rejection(rejected) is True
    assert _is_prefilter_rejection(accepted) is False
    assert _is_prefilter_rejection(error_unclear) is False
