"""
Module: learning_routes
Purpose: aiohttp HTTP handlers that expose the six Phase Y learning
providers as JSON endpoints. Stateless thin wrappers — every business
rule lives in :mod:`shared.dashboard.learning_providers`.
Location: /opt/tickles/shared/dashboard/learning_routes.py

Endpoints (mounted under both "" and "/dashboard" prefixes by
:func:`shared.dashboard.server.build_app`):

    GET /api/learning/skill-summary       ?window=&company=
    GET /api/learning/memory-feed         ?window=&company=&dimension=&limit=
    GET /api/learning/agent-brain         ?window=&company=
    GET /api/learning/guard-activity      ?company=
    GET /api/learning/prompt-evolution    ?window=&company=&limit=
    GET /api/learning/failed-trades       ?window=&company=

The ``?window=`` parameter accepts ``7``, ``14``, ``30`` or the UI
label ``1M`` (mapped to 30 per PHASE_Y §11 Q3 — the dashboard shows
"1M" but the SQL views are ``v_*_30d``). Anything else falls back to
the per-route default and a 200 response with an empty payload (the
providers themselves treat invalid windows as "return [] with a
warning log").

The handlers never raise into aiohttp — every JSON-encoding edge case
returns ``500`` with a ``{"ok": false, "error": ...}`` body so the
dashboard JS can render a non-fatal banner.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from aiohttp import web

from shared.dashboard.learning_providers import (
    ALLOWED_WINDOWS,
    DEFAULT_FEED_LIMIT_CAP,
    PROMPT_EVOLUTION_LIMIT_CAP,
    AgentBrainProvider,
    FailedTradesProvider,
    GuardActivityProvider,
    MemoryFeedProvider,
    PromptEvolutionProvider,
    SkillSummaryProvider,
)

logger = logging.getLogger(__name__)

# Hard-coded default windows match the providers' own defaults so a
# missing ``?window=`` query parameter is never treated as "0d".
_DEFAULT_SKILL_WINDOW: int = 7
_DEFAULT_FEED_WINDOW: int = 7
_DEFAULT_BRAIN_WINDOW: int = 30
_DEFAULT_FAILED_WINDOW: int = 7
_DEFAULT_PROMPT_WINDOW: int = 30
_DEFAULT_PROMPT_LIMIT: int = 100

# UI-label → numeric-window map. PHASE_Y §11 Q3 ratified that the
# dashboard shows "1M" for the 30-day bucket; the providers stay
# numeric. Any token not in this map falls back to numeric parsing.
_WINDOW_LABELS: dict[str, int] = {"1m": 30, "30d": 30, "14d": 14, "7d": 7}


def _err(status: int, message: str) -> web.Response:
    """Return a JSON error response with the canonical envelope.

    Args:
        status: HTTP status code.
        message: Human-readable error message.

    Returns:
        ``aiohttp.web.Response`` with ``application/json`` body.
    """
    return web.json_response({"ok": False, "error": message}, status=status)


def _parse_window(value: Optional[str], default: int) -> int:
    """Parse a ``?window=`` query parameter.

    Accepts integer strings (``"7"``/``"14"``/``"30"``) and the UI
    label ``1M`` (case-insensitive, alongside ``7d``/``14d``/``30d``).

    Args:
        value: Raw query-string value (or ``None`` if absent).
        default: Fallback when ``value`` is missing or unparseable.

    Returns:
        An integer in :data:`ALLOWED_WINDOWS` whenever possible. If
        the input cannot be coerced into one of those values the
        ``default`` is returned (callers expect the providers to
        validate and treat unsupported windows as "return []").
    """
    if value is None or value == "":
        return default
    label = value.strip().lower()
    if label in _WINDOW_LABELS:
        return _WINDOW_LABELS[label]
    try:
        parsed = int(label)
    except (TypeError, ValueError):
        logger.debug("learning_routes: unparseable window=%r — using default", value)
        return default
    if parsed in ALLOWED_WINDOWS:
        return parsed
    logger.debug("learning_routes: window=%d not in %s — using default",
                 parsed, sorted(ALLOWED_WINDOWS))
    return default


def _parse_company(value: Optional[str]) -> Optional[str]:
    """Normalise ``?company=`` to ``None`` / single short-name.

    Args:
        value: Raw query-string value.

    Returns:
        ``None`` for missing / blank / ``all``; otherwise the trimmed
        short-name. Provider layer cross-checks against active list.
    """
    if value is None:
        return None
    trimmed = value.strip()
    if not trimmed or trimmed.lower() == "all":
        return None
    return trimmed


def _parse_positive_int(
    value: Optional[str],
    *,
    default: int,
    max_cap: int,
) -> int:
    """Parse a non-negative integer query parameter.

    Args:
        value: Raw query-string value.
        default: Fallback when missing or unparseable.
        max_cap: Hard upper bound — returned values never exceed this.

    Returns:
        An integer in ``[1, max_cap]``.
    """
    if value is None or value == "":
        return min(max(1, default), max_cap)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return min(max(1, default), max_cap)
    return min(max(1, parsed), max_cap)


def _jsonify(value: Any) -> Any:
    """Recursively coerce a payload to JSON-safe primitives.

    asyncpg returns ``datetime``, ``date``, and ``Decimal`` objects
    that aren't natively JSON-serialisable. We walk dicts/lists once
    so the route handlers can call :func:`web.json_response` without
    a custom encoder.

    Args:
        value: Arbitrary Python object.

    Returns:
        A representation built from ``str``/``int``/``float``/``bool``
        /``list``/``dict``/``None`` only.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        # Decimals stringify to preserve precision through the wire.
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonify(v) for v in value]
    # Fallback: stringify unknown types so we never 500 on JSON encode.
    return str(value)


def _provider(request: web.Request, key: str, factory: Any) -> Any:
    """Return a cached per-app provider instance, creating on demand.

    The providers are stateless dataclass-style objects, but caching
    them on ``request.app`` keeps the build_app wiring lean and lets
    tests override individual providers via ``app["_learning_*"] = ``
    before mounting the routes.

    Args:
        request: Incoming aiohttp request.
        key: ``app``-dict key for the cached provider.
        factory: Zero-arg callable that builds a new provider.

    Returns:
        Provider instance (cached for the lifetime of the app).
    """
    cached = request.app.get(key)
    if cached is None:
        cached = factory()
        request.app[key] = cached
    return cached


# ---------------------------------------------------------------------------
# Handler functions — one per provider.
# ---------------------------------------------------------------------------


async def handle_skill_summary(request: web.Request) -> web.Response:
    """``GET /api/learning/skill-summary`` — see module docstring."""
    window = _parse_window(request.query.get("window"), _DEFAULT_SKILL_WINDOW)
    company = _parse_company(request.query.get("company"))
    provider: SkillSummaryProvider = _provider(
        request, "_learning_skill", SkillSummaryProvider,
    )
    try:
        rows = await provider.fetch(window_days=window, company_filter=company)
    except Exception as exc:
        logger.exception("handle_skill_summary failed: %s", exc)
        return _err(500, "skill-summary fetch failed")
    return web.json_response({
        "ok": True,
        "window_days": window,
        "rows": _jsonify(rows),
    })


async def handle_memory_feed(request: web.Request) -> web.Response:
    """``GET /api/learning/memory-feed`` — see module docstring."""
    window = _parse_window(request.query.get("window"), _DEFAULT_FEED_WINDOW)
    company = _parse_company(request.query.get("company"))
    dimension = request.query.get("dimension")
    if dimension is not None:
        dimension = dimension.strip() or None
    limit = _parse_positive_int(
        request.query.get("limit"),
        default=DEFAULT_FEED_LIMIT_CAP,
        max_cap=DEFAULT_FEED_LIMIT_CAP,
    )
    provider: MemoryFeedProvider = _provider(
        request, "_learning_feed", MemoryFeedProvider,
    )
    try:
        rows = await provider.fetch(
            window_days=window,
            company_filter=company,
            dimension_filter=dimension,
            limit=limit,
        )
    except Exception as exc:
        logger.exception("handle_memory_feed failed: %s", exc)
        return _err(500, "memory-feed fetch failed")
    return web.json_response({
        "ok": True,
        "window_days": window,
        "dimension": dimension,
        "rows": _jsonify(rows),
    })


async def handle_agent_brain(request: web.Request) -> web.Response:
    """``GET /api/learning/agent-brain`` — see module docstring."""
    window = _parse_window(request.query.get("window"), _DEFAULT_BRAIN_WINDOW)
    company = _parse_company(request.query.get("company"))
    provider: AgentBrainProvider = _provider(
        request, "_learning_brain", AgentBrainProvider,
    )
    try:
        rows = await provider.fetch(window_days=window, company_filter=company)
    except Exception as exc:
        logger.exception("handle_agent_brain failed: %s", exc)
        return _err(500, "agent-brain fetch failed")
    return web.json_response({
        "ok": True,
        "window_days": window,
        "rows": _jsonify(rows),
    })


async def handle_guard_activity(request: web.Request) -> web.Response:
    """``GET /api/learning/guard-activity`` — see module docstring."""
    company = _parse_company(request.query.get("company"))
    provider: GuardActivityProvider = _provider(
        request, "_learning_guard", GuardActivityProvider,
    )
    try:
        rows = await provider.fetch(company_filter=company)
    except Exception as exc:
        logger.exception("handle_guard_activity failed: %s", exc)
        return _err(500, "guard-activity fetch failed")
    return web.json_response({"ok": True, "rows": _jsonify(rows)})


async def handle_prompt_evolution(request: web.Request) -> web.Response:
    """``GET /api/learning/prompt-evolution`` — see module docstring."""
    window = _parse_positive_int(
        request.query.get("window"),
        default=_DEFAULT_PROMPT_WINDOW,
        max_cap=365,
    )
    company = _parse_company(request.query.get("company"))
    limit = _parse_positive_int(
        request.query.get("limit"),
        default=_DEFAULT_PROMPT_LIMIT,
        max_cap=PROMPT_EVOLUTION_LIMIT_CAP,
    )
    provider: PromptEvolutionProvider = _provider(
        request, "_learning_prompt", PromptEvolutionProvider,
    )
    try:
        rows = await provider.fetch(
            window_days=window, company_filter=company, limit=limit,
        )
    except Exception as exc:
        logger.exception("handle_prompt_evolution failed: %s", exc)
        return _err(500, "prompt-evolution fetch failed")
    return web.json_response({
        "ok": True,
        "window_days": window,
        "rows": _jsonify(rows),
    })


async def handle_failed_trades(request: web.Request) -> web.Response:
    """``GET /api/learning/failed-trades`` — see module docstring."""
    window = _parse_window(request.query.get("window"), _DEFAULT_FAILED_WINDOW)
    company = _parse_company(request.query.get("company"))
    provider: FailedTradesProvider = _provider(
        request, "_learning_failed", FailedTradesProvider,
    )
    try:
        rows = await provider.fetch(window_days=window, company_filter=company)
    except Exception as exc:
        logger.exception("handle_failed_trades failed: %s", exc)
        return _err(500, "failed-trades fetch failed")
    return web.json_response({
        "ok": True,
        "window_days": window,
        "rows": _jsonify(rows),
    })


def attach_routes(app: web.Application, *, prefix: str = "") -> None:
    """Register the six learning endpoints on ``app`` under ``prefix``.

    Called by :func:`shared.dashboard.server.build_app` once per
    served prefix (``""`` and ``"/dashboard"``).

    Args:
        app: aiohttp application.
        prefix: URL prefix (no trailing slash).
    """
    routes = [
        ("/api/learning/skill-summary", handle_skill_summary),
        ("/api/learning/memory-feed", handle_memory_feed),
        ("/api/learning/agent-brain", handle_agent_brain),
        ("/api/learning/guard-activity", handle_guard_activity),
        ("/api/learning/prompt-evolution", handle_prompt_evolution),
        ("/api/learning/failed-trades", handle_failed_trades),
    ]
    for path, handler in routes:
        app.router.add_get(prefix + path, handler)


__all__ = [
    "attach_routes",
    "handle_agent_brain",
    "handle_failed_trades",
    "handle_guard_activity",
    "handle_memory_feed",
    "handle_prompt_evolution",
    "handle_skill_summary",
]
