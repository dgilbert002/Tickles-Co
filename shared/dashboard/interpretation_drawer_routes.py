"""
Module: interpretation_drawer_routes
Purpose: aiohttp routes for the Phase X.5 cross-tab Interpretation Drawer.
Location: /opt/tickles/shared/dashboard/interpretation_drawer_routes.py

Exposes a single read-only endpoint:

- ``GET /api/interpretations/drawer`` — returns 0..N interpretation
  rows for either a news-item id or a single interpretation id.

Query parameters (exactly ONE of ``news_item_id`` / ``id`` must be
supplied):

- ``news_item_id`` — primary key of ``public.news_items``. Returns
                      every interpretation attached to that news row,
                      newest first.
- ``id``           — primary key of ``public.signal_interpretations``.
                      Returns 0 or 1 row.
- ``limit``        — only honoured for ``news_item_id`` queries.
                      1..:data:`DRAWER_LIMIT_CAP` (default
                      :data:`DRAWER_DEFAULT_LIMIT`).

The routes are mounted by :func:`shared.dashboard.server.build_app`
under both ``""`` (root) and ``"/dashboard"`` prefixes so the SPA
works in either deployment shape. Phase L is a strict CONSUMER — no
PATCH / POST / DELETE handlers live here.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from aiohttp import web

from shared.dashboard.interpretation_drawer_provider import (
    DRAWER_DEFAULT_LIMIT,
    DRAWER_LIMIT_CAP,
    InterpretationDrawerProvider,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Query-parsing helpers — kept tiny so the handler stays under 50 lines.
# ---------------------------------------------------------------------------


def _err(status: int, message: str) -> web.Response:
    """Return a JSON error response with a stable shape.

    Args:
        status: HTTP status code (4xx / 5xx).
        message: Human-readable message; never contains internals.

    Returns:
        ``aiohttp.web.Response`` with ``{"ok": False, "error": ...}``.
    """
    return web.json_response({"ok": False, "error": message}, status=status)


def _parse_positive_int(value: Optional[str]) -> Optional[int]:
    """Parse a query-string positive integer.

    Args:
        value: Raw query-string value.

    Returns:
        ``int(value)`` if it is a strictly positive integer, else
        ``None`` (covers missing / blank / non-numeric / non-positive).
    """
    if value is None:
        return None
    token = value.strip()
    if not token:
        return None
    try:
        n = int(token)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return n


def _parse_limit(value: Optional[str]) -> int:
    """Parse the ``?limit=`` integer with default + cap.

    Args:
        value: Raw query-string value.

    Returns:
        Integer in ``[1, DRAWER_LIMIT_CAP]``.
    """
    if value is None or value == "":
        return DRAWER_DEFAULT_LIMIT
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return DRAWER_DEFAULT_LIMIT
    return max(1, min(DRAWER_LIMIT_CAP, parsed))


def _jsonify(value: Any) -> Any:
    """Recursively coerce a payload to JSON-safe primitives.

    asyncpg returns ``datetime``/``date``/``Decimal`` objects that
    are not natively JSON-serialisable. We walk dicts/lists once so
    the handler can call :func:`web.json_response` without a custom
    encoder.

    Args:
        value: Arbitrary Python object.

    Returns:
        A representation built from ``str``/``int``/``float``/``bool``
        /``list``/``dict``/``None`` only.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonify(v) for v in value]
    return str(value)


def _provider(request: web.Request) -> InterpretationDrawerProvider:
    """Return a cached :class:`InterpretationDrawerProvider`.

    The provider is stateless, but caching the instance on
    ``request.app`` keeps the build_app wiring lean and lets tests
    swap it out via ``app["_interp_drawer"] = ...`` before mounting.

    Args:
        request: Incoming aiohttp request.

    Returns:
        Cached provider instance.
    """
    cached = request.app.get("_interp_drawer")
    if cached is None:
        cached = InterpretationDrawerProvider()
        request.app["_interp_drawer"] = cached
    return cached


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def handle_drawer(request: web.Request) -> web.Response:
    """``GET /api/interpretations/drawer`` — see module docstring."""
    news_item_id = _parse_positive_int(request.query.get("news_item_id"))
    interp_id = _parse_positive_int(request.query.get("id"))
    limit = _parse_limit(request.query.get("limit"))

    if news_item_id is None and interp_id is None:
        return _err(400, "must supply exactly one of: news_item_id, id")
    if news_item_id is not None and interp_id is not None:
        return _err(400, "supply only one of: news_item_id, id")

    provider = _provider(request)
    rows, media_gallery, news_item_header = await _fetch_drawer_payload(
        provider, news_item_id, interp_id, limit,
    )
    if rows is None:
        return _err(500, "interpretation-drawer fetch failed")

    payload: dict = {
        "ok": True,
        "news_item_id": news_item_id,
        "id": interp_id,
        "limit": limit if news_item_id is not None else None,
        "rows": _jsonify(rows),
    }
    if media_gallery is not None:
        payload["media_gallery"] = _jsonify(media_gallery)
    # Slice 1 §3.5 — only include the news_item header when there are no
    # interpretation rows to render. The frontend uses this to render an
    # informative empty state instead of "No interpretation found.".
    if news_item_header is not None and not rows:
        payload["news_item"] = _jsonify(news_item_header)

    # Slice 2 §F — best-effort mem0 recall keyed off the first row's
    # resolved instrument. Failures (timeout, missing dep, miss) are
    # silently dropped so the drawer never blocks on the vector store.
    memories = await _fetch_memories_for_rows(provider, rows)
    if memories:
        payload["memories"] = _jsonify(memories)
    return web.json_response(payload)


async def _fetch_memories_for_rows(
    provider: InterpretationDrawerProvider,
    rows: Any,
) -> list:
    """Best-effort mem0 recall using the first row's instrument symbol.

    Args:
        provider: Drawer provider instance.
        rows: Interpretation rows already fetched (may be empty).

    Returns:
        Memory list (possibly empty). Never raises.
    """
    fetch_fn = getattr(provider, "fetch_memories_for_symbol", None)
    if not callable(fetch_fn) or not rows:
        return []
    try:
        first = rows[0] if isinstance(rows, list) else None
        if not isinstance(first, dict):
            return []
        symbol = first.get("instrument_symbol")
        if not symbol:
            return []
        result = await fetch_fn(symbol, limit=5)
        return list(result) if isinstance(result, list) else []
    except Exception as exc:
        logger.debug("handle_drawer memories fetch failed: %s", exc)
        return []


async def _fetch_drawer_payload(
    provider: InterpretationDrawerProvider,
    news_item_id: Optional[int],
    interp_id: Optional[int],
    limit: int,
) -> tuple:
    """Fetch interpretation rows, media gallery, and news_item header.

    For ``news_item_id`` queries, all three are fetched concurrently so
    the drawer can render a gallery and an informative empty state
    even when no interpretations exist yet (Slice 1 §3.3, §3.5).

    For ``id`` queries, only the single interpretation row is fetched;
    gallery / header are returned as ``None`` since the consumer
    already has a specific interpretation in mind.

    Args:
        provider: Drawer provider instance.
        news_item_id: Validated id, or ``None``.
        interp_id: Validated id, or ``None``.
        limit: Validated row cap.

    Returns:
        Tuple of ``(rows, media_gallery, news_item_header)`` where each
        element is either a list/dict on success or ``None`` if the
        underlying call failed. ``rows`` being ``None`` triggers a 500
        in the caller; the gallery / header are best-effort and a
        ``None`` simply means they are omitted from the response.
    """
    if news_item_id is not None:
        return await _fetch_by_news_item_payload(provider, news_item_id, limit)
    try:
        rows = await provider.fetch_by_id(interp_id)  # type: ignore[arg-type]
        return rows, None, None
    except Exception as exc:
        logger.exception("handle_drawer fetch_by_id failed: %s", exc)
        return None, None, None


async def _fetch_by_news_item_payload(
    provider: InterpretationDrawerProvider,
    news_item_id: int,
    limit: int,
) -> tuple:
    """Concurrently fetch rows, media gallery, and news_item header.

    The rows fetch is the only required call: if it fails, the handler
    surfaces a 500. Gallery and header are best-effort — a failure (or
    a provider that lacks the optional methods, e.g. legacy test
    mocks) yields ``None`` for that field while the rows still render.

    Args:
        provider: Drawer provider instance.
        news_item_id: Validated news_items.id.
        limit: Validated row cap.

    Returns:
        ``(rows, media_gallery, news_item_header)``.
    """
    tasks: list = [provider.fetch_by_news_item(news_item_id, limit=limit)]
    gallery_fn = getattr(provider, "fetch_media_for_news_item", None)
    header_fn = getattr(provider, "fetch_news_item_header", None)
    tasks.append(gallery_fn(news_item_id) if callable(gallery_fn) else _none_coro())
    tasks.append(header_fn(news_item_id) if callable(header_fn) else _none_coro())
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as exc:
        logger.exception("handle_drawer gather failed: %s", exc)
        return None, None, None

    if isinstance(results[0], BaseException):
        logger.exception(
            "handle_drawer fetch_by_news_item failed: %s", results[0],
        )
        rows = None
    else:
        rows = results[0]
    media = results[1] if not isinstance(results[1], BaseException) else None
    header = results[2] if not isinstance(results[2], BaseException) else None
    return rows, media, header


async def _none_coro() -> None:
    """Awaitable that resolves to ``None``; used as a placeholder when
    a provider lacks an optional method."""
    return None


def attach_routes(app: web.Application, *, prefix: str = "") -> None:
    """Register the drawer endpoint on ``app`` under ``prefix``.

    Called by :func:`shared.dashboard.server.build_app` once per
    served prefix (``""`` and ``"/dashboard"``).

    Args:
        app: aiohttp application.
        prefix: URL prefix (no trailing slash).
    """
    app.router.add_get(prefix + "/api/interpretations/drawer", handle_drawer)


__all__ = ["attach_routes", "handle_drawer"]
