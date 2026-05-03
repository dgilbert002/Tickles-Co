"""
Module: config_routes
Purpose: aiohttp routes for the Phase X.6 Config tab.
Location: /opt/tickles/shared/dashboard/config_routes.py

Exposes a single read-only endpoint:

- ``GET /api/config/snapshot`` — combined ``system_config`` (shared)
  and ``company_config`` (per active company) snapshot.

Query parameters:

- ``company`` — short-name to narrow the per-company subset to one
  company, or ``"all"`` / omitted for every active company. Unknown
  values degrade to an empty per-company subset (no 4xx).

The routes are mounted by :func:`shared.dashboard.server.build_app`
under both ``""`` (root) and ``"/dashboard"`` prefixes so the SPA
works in either deployment shape. Phase L is a strict CONSUMER —
no PATCH / POST / DELETE handlers live here.

Secrets are redacted at the provider layer (any row with
``is_secret = true`` has its value rewritten to ``"***"``) so this
module never has to reason about disclosure.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from aiohttp import web

from shared.dashboard.config_provider import ConfigSnapshotProvider

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


def _parse_company(value: Optional[str]) -> Optional[str]:
    """Parse the ``?company=`` filter; ``None`` / ``all`` disable it.

    Short-names are lower-cased here so ``?company=RUBICON`` matches
    the lower-case identifiers returned by
    :func:`shared.utils.companies.list_active_companies`.

    Args:
        value: Raw query-string value.

    Returns:
        ``None`` when no narrowing should apply, else a trimmed
        lower-case short-name (provider re-validates against the
        active list).
    """
    if value is None:
        return None
    trimmed = value.strip().lower()
    if not trimmed or trimmed == "all":
        return None
    return trimmed


def _jsonify(value: Any) -> Any:
    """Recursively coerce a payload to JSON-safe primitives.

    asyncpg returns ``datetime``/``date``/``Decimal`` objects that are
    not natively JSON-serialisable. We walk dicts/lists once so the
    handler can call :func:`web.json_response` without a custom encoder.

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


def _provider(request: web.Request) -> ConfigSnapshotProvider:
    """Return a cached :class:`ConfigSnapshotProvider` for ``request.app``.

    The provider is stateless, but caching the instance on
    ``request.app`` keeps the build_app wiring lean and lets tests
    swap it out via ``app["_config_snapshot"] = ...`` before mounting.

    Args:
        request: Incoming aiohttp request.

    Returns:
        Cached provider instance.
    """
    cached = request.app.get("_config_snapshot")
    if cached is None:
        cached = ConfigSnapshotProvider()
        request.app["_config_snapshot"] = cached
    return cached


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def handle_config_snapshot(request: web.Request) -> web.Response:
    """``GET /api/config/snapshot`` — see module docstring for parameters."""
    company = _parse_company(request.query.get("company"))
    provider = _provider(request)
    try:
        snapshot = await provider.fetch(company_filter=company)
    except Exception as exc:
        logger.exception("handle_config_snapshot failed: %s", exc)
        return _err(500, "config snapshot fetch failed")
    return web.json_response({
        "ok": True,
        "company": company,
        "shared": _jsonify(snapshot.get("shared", {})),
        "companies": _jsonify(snapshot.get("companies", {})),
    })


def attach_routes(app: web.Application, *, prefix: str = "") -> None:
    """Register the config endpoints on ``app`` under ``prefix``.

    Called by :func:`shared.dashboard.server.build_app` once per
    served prefix (``""`` and ``"/dashboard"``).

    Args:
        app: aiohttp application.
        prefix: URL prefix (no trailing slash).
    """
    app.router.add_get(prefix + "/api/config/snapshot", handle_config_snapshot)


__all__ = ["attach_routes", "handle_config_snapshot"]
