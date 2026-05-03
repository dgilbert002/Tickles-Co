"""
Module: news_provider
Purpose: 250ms-budgeted SnapshotBuilder provider for the Phase X.4 News Feed tab.
Location: /opt/tickles/shared/dashboard/news_provider.py

Reads from the shared ``public.news_items`` table (and joined ``public.media_items``
for media counts). News is a SHARED resource — every company sees the same
collected items — but per-company filtering is supported via the ``instruments``
JSONB array: a company is "interested" in a news row when the row's
``instruments`` overlap the company's tracked instruments.

Acceptance criteria (mirrored from PHASE_Y §4.3 and applied here):

- 250ms hard budget enforced via ``asyncio.wait_for``.
- Provider never raises into the SnapshotBuilder; on timeout / error it
  returns the documented fallback (an empty list) and logs a warning.
- Returns plain Python primitives ready for ``aiohttp.web.json_response``.
- All SQL is parameterised. No string interpolation of user input.
- Phase L is a strict CONSUMER — this module never writes.

Window selector accepts the same UI labels as the Learning tab plus a
news-specific ``24h``: ``{"24h": 1, "7d": 7, "30d": 30}``. The route
layer maps the label; the provider takes a numeric ``window_days``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, FrozenSet, List, Optional

from shared.utils.companies import list_active_companies
from shared.utils.db import get_company_pool, get_shared_pool

logger = logging.getLogger(__name__)

# 250ms hard budget per provider, matching PHASE_Y §4.3.
NEWS_PROVIDER_TIMEOUT_S: float = 0.25

# Allowed window sizes in days. ``1`` (24h) is news-specific; 7/30 mirror
# the learning tab. ``14`` is intentionally not offered for news — the
# UI sticks with the three buckets agreed in §11.
ALLOWED_NEWS_WINDOWS: FrozenSet[int] = frozenset({1, 7, 30})

# Hard ceiling on per-call ``limit`` — defends against pathological
# clients while leaving headroom for normal pagination.
NEWS_LIMIT_CAP: int = 500

# Default per-call limit when the caller doesn't supply one. Sized to
# fill the dashboard list without exhausting the heap merge.
NEWS_DEFAULT_LIMIT: int = 100

# Allowed source kinds. Anything else is rejected at the route layer
# (the provider also defends, just in case the route is bypassed).
ALLOWED_SOURCE_KINDS: FrozenSet[str] = frozenset(
    {"discord", "telegram", "twitter", "rss", "web", "manual"}
)


def _validate_window(window_days: int) -> int:
    """Validate that ``window_days`` is one of {1, 7, 30}.

    Args:
        window_days: Window size in days.

    Returns:
        ``window_days`` unchanged if valid.

    Raises:
        ValueError: If ``window_days`` is not in :data:`ALLOWED_NEWS_WINDOWS`.
    """
    if window_days not in ALLOWED_NEWS_WINDOWS:
        raise ValueError(
            f"window_days must be one of {sorted(ALLOWED_NEWS_WINDOWS)}, "
            f"got {window_days!r}"
        )
    return window_days


def _validate_source(source_kind: Optional[str]) -> Optional[str]:
    """Validate an optional ``source_kind`` filter.

    Args:
        source_kind: Source label to filter by (case-insensitive).
            ``None`` / empty means no filter.

    Returns:
        Lower-cased source string if valid, otherwise ``None`` (treated
        as "no filter"). Invalid values are NOT raised — they're logged
        and treated as no-filter so a typo in the URL doesn't surface
        as a 500.
    """
    if source_kind is None:
        return None
    cleaned = source_kind.strip().lower()
    if not cleaned:
        return None
    if cleaned not in ALLOWED_SOURCE_KINDS:
        logger.debug(
            "news_provider: rejecting unknown source_kind=%r — treating as no-filter",
            source_kind,
        )
        return None
    return cleaned


def _normalise_limit(limit: Optional[int]) -> int:
    """Clamp ``limit`` to ``[1, NEWS_LIMIT_CAP]`` with a sensible default.

    Args:
        limit: Caller-supplied limit (may be ``None``).

    Returns:
        Integer in ``[1, NEWS_LIMIT_CAP]``.
    """
    if limit is None:
        return NEWS_DEFAULT_LIMIT
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return NEWS_DEFAULT_LIMIT
    return max(1, min(NEWS_LIMIT_CAP, n))


async def _resolve_company(company_filter: Optional[str]) -> Optional[str]:
    """Resolve a single company short-name from a filter token.

    Unlike the learning providers which fan out across companies, the news
    feed surfaces a SHARED feed regardless of company filter, BUT a single
    company name narrows the result to news that overlaps that company's
    instruments. ``"all"`` / ``None`` keeps every row.

    Args:
        company_filter: ``None`` / ``"all"`` to disable company filtering,
            or a short-name to scope to one company.

    Returns:
        ``None`` when no filtering should be applied. Otherwise the
        validated short-name (must be in the active list — unknown
        names degrade gracefully to ``None`` so a typo never leaks).
    """
    if company_filter is None or company_filter.lower() == "all":
        return None
    try:
        active = await list_active_companies()
    except Exception as exc:
        logger.warning("news_provider: list_active_companies failed: %s", exc)
        return None
    if company_filter not in active:
        logger.debug(
            "news_provider: company=%r not in active list — ignoring filter",
            company_filter,
        )
        return None
    return company_filter


async def _company_instruments(company: str) -> List[str]:
    """Return the instrument symbols ``company`` actively trades.

    The list is derived from the company's own ``trades`` table —
    ``SELECT DISTINCT instrument_id`` — joined against the shared
    ``public.instruments`` table to resolve symbols. There is no
    ``company_tracked_instruments`` table; the trades table IS the
    source of truth for which instruments a company touches.

    Both reads are best-effort. If either fails (DB unreachable,
    schema drift, empty company DB) the function returns an empty
    list, which the caller treats as "no instrument filter".

    .. note:: When a real company has zero trades yet, this returns
        ``[]`` and the news feed therefore shows the **shared** stream
        rather than an empty result. This is intentional — a brand-new
        company should still see the news that's flowing in. UI
        surfaces should rely on the response shape, not assume a
        company filter has narrowed the rows.

    Args:
        company: Company short-name (must already be validated against
            the active list — this function does no validation).

    Returns:
        List of symbol strings (e.g. ``["BTC/USDT", "ETH/USDT"]``).
        Empty list on any read failure or when the company hasn't
        traded anything yet.
    """
    instrument_ids = await _read_company_instrument_ids(company)
    if not instrument_ids:
        return []
    return await _read_symbols_for_ids(instrument_ids)


async def _read_company_instrument_ids(company: str) -> List[int]:
    """Read DISTINCT instrument_ids from a company's ``trades`` table.

    Args:
        company: Validated company short-name.

    Returns:
        List of integer instrument IDs (may be empty). Never raises.
    """
    try:
        pool = await get_company_pool(company)
    except Exception as exc:
        logger.debug(
            "news_provider: get_company_pool(%s) failed: %s — "
            "skipping instrument narrowing",
            company,
            exc,
        )
        return []
    sql = (
        "SELECT DISTINCT instrument_id "
        "FROM trades "
        "WHERE instrument_id IS NOT NULL"
    )
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql)
    except Exception as exc:
        logger.debug(
            "news_provider: trades read failed for %s (%s) — "
            "skipping instrument narrowing",
            company,
            exc,
        )
        return []
    return [int(r["instrument_id"]) for r in rows if r["instrument_id"] is not None]


async def _read_symbols_for_ids(instrument_ids: List[int]) -> List[str]:
    """Resolve instrument IDs to canonical symbols via the shared DB.

    Args:
        instrument_ids: Non-empty list of instrument IDs.

    Returns:
        Sorted, de-duplicated list of symbol strings. Empty on failure.
    """
    try:
        pool = await get_shared_pool()
    except Exception as exc:
        logger.debug(
            "news_provider: get_shared_pool failed (%s) — "
            "skipping instrument narrowing",
            exc,
        )
        return []
    sql = "SELECT DISTINCT symbol FROM instruments WHERE id = ANY($1::bigint[])"
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, instrument_ids)
    except Exception as exc:
        logger.debug(
            "news_provider: instruments read failed (%s) — "
            "skipping instrument narrowing",
            exc,
        )
        return []
    return sorted({r["symbol"] for r in rows if r["symbol"]})


async def _run_with_budget(
    coro_factory: Callable[[], Awaitable[Any]],
    *,
    label: str,
    fallback: Any,
) -> Any:
    """Run an async callable under :data:`NEWS_PROVIDER_TIMEOUT_S`.

    Args:
        coro_factory: Zero-arg callable returning a coroutine.
        label: Short identifier used in log messages.
        fallback: Value returned on timeout / cancellation / exception.

    Returns:
        The coroutine's result on success, otherwise ``fallback``.

    Raises:
        asyncio.CancelledError: Propagated so the host event loop can
            shut cleanly. All other exceptions are swallowed.
    """
    try:
        return await asyncio.wait_for(
            coro_factory(), timeout=NEWS_PROVIDER_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        logger.warning(
            "news_provider[%s]: exceeded %.0fms budget — returning fallback",
            label,
            NEWS_PROVIDER_TIMEOUT_S * 1000,
        )
        return fallback
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("news_provider[%s]: provider failed: %s", label, exc)
        return fallback


class NewsFeedProvider:
    """Reads ``public.news_items`` with optional per-company narrowing.

    Output shape (per row)::

        {
            "id":             int,
            "source":         str,
            "headline":       str | None,
            "content":        str | None,    # truncated to 240 chars
            "instruments":    list[str],
            "sentiment":      str | None,    # 'positive' | 'neutral' | 'negative'
            "has_media":      bool,
            "media_count":    int,
            "channel_name":   str | None,
            "author":         str | None,
            "published_at":   str | None,    # ISO 8601 UTC
            "collected_at":   str,           # ISO 8601 UTC (always set)
            "enrichment_status": str,        # 'pending' | 'enriched' | 'failed'
        }

    Sort order: ``collected_at DESC`` (most recent first).
    """

    # 240 chars is enough for a dashboard preview without dragging
    # multi-KB messages over the wire on every poll.
    PREVIEW_CHARS: int = 240

    async def fetch(
        self,
        *,
        window_days: int = 1,
        company_filter: Optional[str] = None,
        source_kind: Optional[str] = None,
        has_media: Optional[bool] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return the news feed for the requested window.

        Args:
            window_days: Time window in days. Must be in
                :data:`ALLOWED_NEWS_WINDOWS`.
            company_filter: ``None`` / ``"all"`` for no narrowing, else
                a single company short-name. Filters by instrument overlap.
            source_kind: Optional source label to narrow to. Unknown
                values are silently dropped (treated as no-filter).
            has_media: ``True`` to require media, ``False`` to exclude
                media, ``None`` to include both.
            limit: Maximum rows to return. Capped at :data:`NEWS_LIMIT_CAP`.

        Returns:
            List of dict rows (see class docstring). Empty list on
            timeout, validation failure, or unrecoverable DB error —
            the provider never raises into the SnapshotBuilder.
        """
        try:
            window = _validate_window(window_days)
        except ValueError as exc:
            logger.warning("NewsFeedProvider: %s", exc)
            return []

        company = await _resolve_company(company_filter)
        source = _validate_source(source_kind)
        max_rows = _normalise_limit(limit)

        async def _do_fetch() -> List[Dict[str, Any]]:
            # Resolve instruments INSIDE the budget — instrument lookup is
            # itself two DB roundtrips (company.trades + shared.instruments)
            # so it must count toward the 250ms ceiling, otherwise a slow
            # company DB can quietly stall the SnapshotBuilder.
            instruments: List[str] = []
            if company is not None:
                instruments = await _company_instruments(company)
            return await self._query(
                window_days=window,
                instruments=instruments,
                source=source,
                has_media=has_media,
                limit=max_rows,
            )

        return await _run_with_budget(
            _do_fetch, label="news_feed", fallback=[]
        )

    async def _query(
        self,
        *,
        window_days: int,
        instruments: List[str],
        source: Optional[str],
        has_media: Optional[bool],
        limit: int,
    ) -> List[Dict[str, Any]]:
        """Run the parameterised SELECT against ``public.news_items``.

        Split out so it can be unit-tested without the budget wrapper.

        Args:
            window_days: Validated window size.
            instruments: List of canonical symbols (empty = no filter).
            source: Validated source label (None = no filter).
            has_media: Tri-state media filter.
            limit: Validated row cap.

        Returns:
            List of dict rows in the documented shape.
        """
        try:
            pool = await get_shared_pool()
        except Exception as exc:
            logger.warning("NewsFeedProvider: get_shared_pool failed: %s", exc)
            return []

        sql, params = self._build_query(
            window_days=window_days,
            instruments=instruments,
            source=source,
            has_media=has_media,
            limit=limit,
        )

        try:
            async with pool.acquire() as conn:
                records = await conn.fetch(sql, *params)
        except Exception as exc:
            logger.warning("NewsFeedProvider: query failed: %s", exc)
            return []

        return [self._row_to_dict(r) for r in records]

    def _build_query(
        self,
        *,
        window_days: int,
        instruments: List[str],
        source: Optional[str],
        has_media: Optional[bool],
        limit: int,
    ) -> tuple[str, List[Any]]:
        """Assemble the parameterised SELECT for :meth:`_query`.

        All placeholders are positional ``$N`` references — no caller
        value is ever interpolated into the SQL string.

        Args:
            window_days: Validated window size.
            instruments: List of canonical symbols (empty = no filter).
            source: Validated source label (None = no filter).
            has_media: Tri-state media filter.
            limit: Validated row cap.

        Returns:
            ``(sql, params)`` ready to pass to ``conn.fetch(sql, *params)``.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
        clauses: List[str] = ["collected_at >= $1"]
        params: List[Any] = [cutoff]

        if source is not None:
            params.append(source)
            clauses.append(f"lower(source) = ${len(params)}")

        if has_media is True:
            clauses.append("has_media IS TRUE")
        elif has_media is False:
            clauses.append("has_media IS FALSE")

        if instruments:
            # JSONB ?| operator: "any of these strings is a top-level key
            # OR top-level array element". news_items.instruments is a
            # JSON array of symbol strings, so ?| is exactly what we want.
            params.append(instruments)
            clauses.append(f"instruments ?| ${len(params)}::text[]")

        params.append(limit)
        sql = (
            "SELECT id, source, headline, content, instruments, sentiment, "
            "       has_media, media_count, channel_name, author, "
            "       published_at, collected_at, enrichment_status "
            f"FROM news_items WHERE {' AND '.join(clauses)} "
            f"ORDER BY collected_at DESC LIMIT ${len(params)}"
        )
        return sql, params

    def _row_to_dict(self, rec: Any) -> Dict[str, Any]:
        """Convert an asyncpg Record to the documented output dict.

        Args:
            rec: asyncpg.Record from the SELECT.

        Returns:
            Dict with all primitives JSON-safe.
        """
        content = rec["content"]
        if content is not None and len(content) > self.PREVIEW_CHARS:
            content = content[: self.PREVIEW_CHARS - 1].rstrip() + "…"
        instruments = rec["instruments"] or []
        if not isinstance(instruments, list):
            # Defensive: jsonb may decode as dict on malformed rows.
            instruments = []
        return {
            "id": int(rec["id"]),
            "source": rec["source"],
            "headline": rec["headline"],
            "content": content,
            "instruments": list(instruments),
            "sentiment": rec["sentiment"],
            "has_media": bool(rec["has_media"]),
            "media_count": int(rec["media_count"] or 0),
            "channel_name": rec["channel_name"],
            "author": rec["author"],
            "published_at": (
                rec["published_at"].isoformat()
                if rec["published_at"] is not None
                else None
            ),
            "collected_at": rec["collected_at"].isoformat(),
            "enrichment_status": rec["enrichment_status"],
        }


__all__ = [
    "ALLOWED_NEWS_WINDOWS",
    "ALLOWED_SOURCE_KINDS",
    "NEWS_DEFAULT_LIMIT",
    "NEWS_LIMIT_CAP",
    "NEWS_PROVIDER_TIMEOUT_S",
    "NewsFeedProvider",
]
