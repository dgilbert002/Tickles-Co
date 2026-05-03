"""
Module: news_routes
Purpose: aiohttp routes for the Phase X.4 News Feed tab.
Location: /opt/tickles/shared/dashboard/news_routes.py

Exposes a single read-only endpoint:

- ``GET /api/news/feed`` — most-recent news items inside a window.

Query parameters:

- ``window``       — one of ``"24h"``, ``"7d"``, ``"30d"`` (default ``"24h"``)
- ``company``      — company short-name to narrow by traded instruments,
                     or ``"all"`` / omitted for the shared feed.
- ``source``       — ``discord`` | ``telegram`` | ``twitter`` | ``rss``
                     | ``web`` | ``manual``. Unknown values silently
                     drop the filter (no 500).
- ``has_media``    — ``"true"`` / ``"1"`` requires media; ``"false"`` /
                     ``"0"`` excludes media; missing means both.
- ``limit``        — 1..500 (default 100). Caller-supplied values are
                     clamped at the route layer.

The routes are mounted by :func:`shared.dashboard.server.build_app`
under both ``""`` (root) and ``"/dashboard"`` prefixes so the SPA
works in either deployment shape. Phase L is a strict CONSUMER — no
PATCH / POST / DELETE handlers live here.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from aiohttp import web

from shared.dashboard.news_provider import (
    ALLOWED_NEWS_WINDOWS,
    ALLOWED_SOURCE_KINDS,
    NEWS_DEFAULT_LIMIT,
    NEWS_LIMIT_CAP,
    NewsFeedProvider,
)

logger = logging.getLogger(__name__)

# Map of UI window labels to the integer day count consumed by the
# provider. ``24h`` is news-specific; ``7d`` / ``30d`` mirror the
# Learning tab so users see consistent windowing across tabs.
_WINDOW_LABEL_TO_DAYS: dict[str, int] = {"24h": 1, "7d": 7, "30d": 30}
_DEFAULT_WINDOW_LABEL: str = "24h"


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


def _parse_window(value: Optional[str]) -> int:
    """Parse a ``?window=`` label into integer days.

    Accepts the labels ``24h``, ``7d``, ``30d``. Unknown / missing
    values fall back to :data:`_DEFAULT_WINDOW_LABEL` (``24h``) so a
    typo in the URL never surfaces as a 4xx.

    Args:
        value: Raw query-string value.

    Returns:
        Integer day count guaranteed to be in
        :data:`shared.dashboard.news_provider.ALLOWED_NEWS_WINDOWS`.
    """
    if value is None:
        return _WINDOW_LABEL_TO_DAYS[_DEFAULT_WINDOW_LABEL]
    label = value.strip().lower()
    if label not in _WINDOW_LABEL_TO_DAYS:
        return _WINDOW_LABEL_TO_DAYS[_DEFAULT_WINDOW_LABEL]
    days = _WINDOW_LABEL_TO_DAYS[label]
    # Belt-and-braces: provider also validates this set.
    if days not in ALLOWED_NEWS_WINDOWS:
        return _WINDOW_LABEL_TO_DAYS[_DEFAULT_WINDOW_LABEL]
    return days


def _parse_company(value: Optional[str]) -> Optional[str]:
    """Parse the ``?company=`` filter; ``None`` / ``all`` disable it.

    Args:
        value: Raw query-string value.

    Returns:
        ``None`` when no narrowing should apply, else a trimmed
        short-name (provider re-validates against the active list).
    """
    if value is None:
        return None
    trimmed = value.strip()
    if not trimmed or trimmed.lower() == "all":
        return None
    return trimmed


def _parse_source(value: Optional[str]) -> Optional[str]:
    """Parse the ``?source=`` filter against the allow-list.

    Args:
        value: Raw query-string value.

    Returns:
        Lower-cased source label when valid, else ``None`` (no filter).
    """
    if value is None:
        return None
    cleaned = value.strip().lower()
    if not cleaned:
        return None
    if cleaned not in ALLOWED_SOURCE_KINDS:
        return None
    return cleaned


def _parse_bool(value: Optional[str]) -> Optional[bool]:
    """Parse a tri-state ``?has_media=`` style boolean.

    Recognises ``true``/``false``/``1``/``0``/``yes``/``no`` (case-
    insensitive). Anything else means "no filter".

    Args:
        value: Raw query-string value.

    Returns:
        ``True`` / ``False`` / ``None``.
    """
    if value is None:
        return None
    token = value.strip().lower()
    if token in {"true", "1", "yes", "y"}:
        return True
    if token in {"false", "0", "no", "n"}:
        return False
    return None


def _parse_limit(value: Optional[str]) -> int:
    """Parse the ``?limit=`` integer with default + cap.

    Args:
        value: Raw query-string value.

    Returns:
        Integer in ``[1, NEWS_LIMIT_CAP]``.
    """
    if value is None or value == "":
        return NEWS_DEFAULT_LIMIT
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return NEWS_DEFAULT_LIMIT
    return max(1, min(NEWS_LIMIT_CAP, parsed))


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


def _provider(request: web.Request) -> NewsFeedProvider:
    """Return a cached :class:`NewsFeedProvider` for ``request.app``.

    The provider is stateless, but caching the instance on
    ``request.app`` keeps the build_app wiring lean and lets tests
    swap it out via ``app["_news_feed"] = ...`` before mounting.

    Args:
        request: Incoming aiohttp request.

    Returns:
        Cached provider instance.
    """
    cached = request.app.get("_news_feed")
    if cached is None:
        cached = NewsFeedProvider()
        request.app["_news_feed"] = cached
    return cached


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def handle_news_feed(request: web.Request) -> web.Response:
    """``GET /api/news/feed`` — see module docstring for parameters."""
    window = _parse_window(request.query.get("window"))
    company = _parse_company(request.query.get("company"))
    source = _parse_source(request.query.get("source"))
    has_media = _parse_bool(request.query.get("has_media"))
    limit = _parse_limit(request.query.get("limit"))
    provider = _provider(request)
    try:
        rows = await provider.fetch(
            window_days=window,
            company_filter=company,
            source_kind=source,
            has_media=has_media,
            limit=limit,
        )
    except Exception as exc:
        logger.exception("handle_news_feed failed: %s", exc)
        return _err(500, "news-feed fetch failed")
    return web.json_response({
        "ok": True,
        "window_days": window,
        "company": company,
        "source": source,
        "has_media": has_media,
        "limit": limit,
        "rows": _jsonify(rows),
    })


def attach_routes(app: web.Application, *, prefix: str = "") -> None:
    """Register the news endpoints on ``app`` under ``prefix``.

    Called by :func:`shared.dashboard.server.build_app` once per
    served prefix (``""`` and ``"/dashboard"``).

    Args:
        app: aiohttp application.
        prefix: URL prefix (no trailing slash).
    """
    app.router.add_get(prefix + "/api/news/feed", handle_news_feed)


__all__ = ["attach_routes", "handle_news_feed"]
