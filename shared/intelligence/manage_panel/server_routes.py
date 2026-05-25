"""
Module: server_routes
Purpose: aiohttp route handlers for the /manage/* panel.
Location: /opt/tickles/shared/intelligence/manage_panel/server_routes.py
"""

import logging
import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from aiohttp import web
from jinja2 import Environment, FileSystemLoader, select_autoescape

from shared.dashboard.csrf import csrf_required
from shared.dashboard.rate_limit import rate_limit
from shared.intelligence.manage_panel import db_views
from shared.intelligence.signal_review_export import render_thumb
from shared.utils.api_cost_log import log_api_call
from shared.utils.correlation import new_correlation_id

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"

def _fmt_dt(value: Any, fmt: str = "%m-%d %H:%M") -> str:
    """Format a datetime or ISO string for Jinja2 templates."""
    if value is None:
        return "-"
    if hasattr(value, "strftime"):
        return value.strftime(fmt)
    # ISO string fallback
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.strftime(fmt)
    except Exception:
        return str(value)


_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(enabled_extensions=("html", "jinja2"), default_for_string=True),
)
_env.filters["fmt_dt"] = _fmt_dt


def _render(template_name: str, **ctx: Any) -> web.Response:
    """Render a Jinja2 template to an HTML response."""
    tpl = _env.get_template(template_name)
    html = tpl.render(**ctx)
    return web.Response(text=html, content_type="text/html")


async def _audit_panel_mutation(
    request: web.Request,
    operation: str,
    entity_type: str,
    entity_id: int,
    success: bool,
    status: int,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Fire-and-forget audit log for a panel mutation.

    Args:
        request: The aiohttp request (used for path + remote).
        operation: e.g. 'source_add', 'source_disable', 'channel_enable'.
        entity_type: 'source', 'channel', 'user'.
        entity_id: Primary key of the affected row.
        success: Whether the DB operation succeeded.
        status: HTTP status returned to the client.
        extra: JSON-serialisable dict for additional context.
    """
    try:
        await log_api_call(
            provider="manage_panel",
            model="",
            role="panel_mutation",
            context=f"{operation} {entity_type}={entity_id}",
            tokens_in=0,
            tokens_out=0,
            cost_usd=Decimal("0"),
            latency_ms=0,
            company_id="",
            operation=operation,
            agent_id="manage_panel",
            temperature=None,
            correlation_id=new_correlation_id(),
            request_path=request.path,
            response_path="",
            success=success,
            http_status=status,
            extra={"entity_type": entity_type, "entity_id": entity_id, **(extra or {})},
        )
    except Exception:
        logger.exception("Audit log failed for %s", operation)


# ---------------------------------------------------------------------------
# Read-only views
# ---------------------------------------------------------------------------

async def handle_manage_index(request: web.Request) -> web.Response:
    """Redirect /manage[/] to the sources view.

    Bug Hunter 2 §9.2 — use a relative redirect so the manage panel can be
    mounted under any path prefix (Tailscale Serve sub-path, Cloudflare,
    nginx) without a 404 loop.

    Bug I fix (2026-05-24 second-round audit): the bare relative target
    ``"sources"`` resolves correctly when the user requests ``/manage/``
    (trailing slash) — the browser keeps ``/manage/`` as the base. But a
    request to ``/manage`` (no trailing slash) makes ``sources`` resolve to
    ``/sources`` per RFC 3986, which 404s. We now build the redirect target
    by appending to the request path, so it lands under whichever mount
    prefix the request came in on regardless of trailing-slash state.
    """
    base = request.path.rstrip("/")
    target = f"{base}/sources"
    raise web.HTTPFound(location=target)


async def handle_sources_view(request: web.Request) -> web.Response:
    """Render the sources management page."""
    sources = await db_views.list_sources()
    return _render("sources.html.jinja2", sources=sources, active_tab="sources")


async def handle_signals_view(request: web.Request) -> web.Response:
    """Render the signals review page."""
    hours = int(request.query.get("hours", "24"))
    signals = await db_views.list_recent_signals(hours=hours)
    return _render("signals.html.jinja2", signals=signals, hours=hours, active_tab="signals")


async def handle_positions_view(request: web.Request) -> web.Response:
    """Render the positions page (open + recent closed)."""
    open_pos = await db_views.list_open_positions()
    closed_pos = await db_views.list_closed_positions(days=7)
    return _render("positions.html.jinja2", open_positions=open_pos, closed_positions=closed_pos, active_tab="positions")


async def handle_leaderboard_view(request: web.Request) -> web.Response:
    """Render the trader leaderboard page."""
    days = int(request.query.get("days", "30"))
    rows = await db_views.get_leaderboard(days=days)
    return _render("leaderboard.html.jinja2", rows=rows, days=days, active_tab="leaderboard")


async def handle_trader_drill(request: web.Request) -> web.Response:
    """Render the trader drill-down page."""
    trader_id = int(request.match_info["trader_id"])
    profile = await db_views.get_trader_profile(trader_id)
    if not profile:
        raise web.HTTPNotFound(text="Trader not found")
    signals = await db_views.get_trader_signals(trader_id)
    positions = await db_views.get_trader_positions(trader_id)
    return _render(
        "trader_drill.html.jinja2",
        profile=profile,
        signals=signals,
        positions=positions,
        active_tab="leaderboard",
        render_thumb=render_thumb,
    )


# ---------------------------------------------------------------------------
# Mutating API endpoints (CSRF + rate limit)
# ---------------------------------------------------------------------------

async def handle_sources_add(request: web.Request) -> web.Response:
    """Add a new source to collector_catalog.

    Required body fields:
        source_type (str): One of discord, telegram, rss, tradingview, twitter, api, webhook.
        source_name (str): Human-readable name.
        source_slug (str): Machine identifier (unique).
        connection_config (dict): JSON connection parameters.

    Optional:
        priority (int): Lower = higher priority (default 100).
        primary_asset_class (str): crypto, cfd, stock, forex, commodity, index, mixed.
        notes (str): Description.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "JSON body required"}, status=400)

    source_type = body.get("source_type", "").strip().lower()
    source_name = body.get("source_name", "").strip()
    source_slug = body.get("source_slug", "").strip().lower()
    connection_config = body.get("connection_config", {})

    if not source_type or not source_name or not source_slug:
        return web.json_response(
            {"error": "source_type, source_name, source_slug are required"}, status=400
        )

    valid_types = {"discord", "telegram", "rss", "tradingview", "twitter", "api", "webhook"}
    if source_type not in valid_types:
        return web.json_response(
            {"error": f"source_type must be one of {sorted(valid_types)}"}, status=400
        )

    priority = int(body.get("priority", 100))
    primary_asset_class = body.get("primary_asset_class", "").strip().lower() or None
    notes = body.get("notes", "").strip() or None

    try:
        new_id = await db_views.add_source(
            source_type=source_type,
            source_name=source_name,
            source_slug=source_slug,
            connection_config=connection_config,
            priority=priority,
            primary_asset_class=primary_asset_class,
            notes=notes,
        )
    except Exception as exc:
        logger.exception("Failed to add source %s", source_slug)
        await _audit_panel_mutation(
            request, "source_add", "source", 0, False, 500,
            extra={"source_slug": source_slug, "error": str(exc)},
        )
        return web.json_response({"error": str(exc)}, status=500)

    await _audit_panel_mutation(
        request, "source_add", "source", new_id, True, 200,
        extra={"source_slug": source_slug, "source_type": source_type},
    )
    return web.json_response({"ok": True, "id": new_id})


async def handle_channels_view(request: web.Request) -> web.Response:
    """Render the channels management page."""
    catalog_id = request.query.get("catalog_id")
    catalog_id_int = int(catalog_id) if catalog_id and catalog_id.isdigit() else None
    channels = await db_views.list_channels(catalog_id=catalog_id_int)
    return _render("channels.html.jinja2", channels=channels, active_tab="channels")


async def handle_users_view(request: web.Request) -> web.Response:
    """Render the users management page."""
    channel_id = request.query.get("channel_id")
    channel_id_int = int(channel_id) if channel_id and channel_id.isdigit() else None
    users = await db_views.list_users(channel_id=channel_id_int)
    return _render("users.html.jinja2", users=users, active_tab="users")


async def handle_sources_disable(request: web.Request) -> web.Response:
    """Disable a source by ID."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "JSON body required"}, status=400)
    source_id = body.get("source_id")
    if not source_id:
        return web.json_response({"error": "source_id required"}, status=400)
    try:
        affected = await db_views.toggle_source(int(source_id), enabled=False)
    except Exception as exc:
        logger.exception("Failed to disable source %s", source_id)
        await _audit_panel_mutation(
            request, "source_disable", "source", int(source_id), False, 500,
            extra={"error": str(exc)},
        )
        return web.json_response({"error": str(exc)}, status=500)
    await _audit_panel_mutation(
        request, "source_disable", "source", int(source_id), True, 200,
        extra={"affected": affected},
    )
    return web.json_response({"ok": True, "affected": affected})


async def handle_sources_enable(request: web.Request) -> web.Response:
    """Enable a source by ID."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "JSON body required"}, status=400)
    source_id = body.get("source_id")
    if not source_id:
        return web.json_response({"error": "source_id required"}, status=400)
    try:
        affected = await db_views.toggle_source(int(source_id), enabled=True)
    except Exception as exc:
        logger.exception("Failed to enable source %s", source_id)
        await _audit_panel_mutation(
            request, "source_enable", "source", int(source_id), False, 500,
            extra={"error": str(exc)},
        )
        return web.json_response({"error": str(exc)}, status=500)
    await _audit_panel_mutation(
        request, "source_enable", "source", int(source_id), True, 200,
        extra={"affected": affected},
    )
    return web.json_response({"ok": True, "affected": affected})


async def handle_channel_disable(request: web.Request) -> web.Response:
    """Disable a channel by ID."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "JSON body required"}, status=400)
    channel_id = body.get("channel_id")
    if not channel_id:
        return web.json_response({"error": "channel_id required"}, status=400)
    try:
        affected = await db_views.toggle_channel(int(channel_id), enabled=False)
    except Exception as exc:
        logger.exception("Failed to disable channel %s", channel_id)
        await _audit_panel_mutation(
            request, "channel_disable", "channel", int(channel_id), False, 500,
            extra={"error": str(exc)},
        )
        return web.json_response({"error": str(exc)}, status=500)
    await _audit_panel_mutation(
        request, "channel_disable", "channel", int(channel_id), True, 200,
        extra={"affected": affected},
    )
    return web.json_response({"ok": True, "affected": affected})


async def handle_channel_enable(request: web.Request) -> web.Response:
    """Enable a channel by ID."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "JSON body required"}, status=400)
    channel_id = body.get("channel_id")
    if not channel_id:
        return web.json_response({"error": "channel_id required"}, status=400)
    try:
        affected = await db_views.toggle_channel(int(channel_id), enabled=True)
    except Exception as exc:
        logger.exception("Failed to enable channel %s", channel_id)
        await _audit_panel_mutation(
            request, "channel_enable", "channel", int(channel_id), False, 500,
            extra={"error": str(exc)},
        )
        return web.json_response({"error": str(exc)}, status=500)
    await _audit_panel_mutation(
        request, "channel_enable", "channel", int(channel_id), True, 200,
        extra={"affected": affected},
    )
    return web.json_response({"ok": True, "affected": affected})


async def handle_user_disable(request: web.Request) -> web.Response:
    """Disable a user by ID."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "JSON body required"}, status=400)
    user_id = body.get("user_id")
    if not user_id:
        return web.json_response({"error": "user_id required"}, status=400)
    try:
        affected = await db_views.toggle_user(int(user_id), enabled=False)
    except Exception as exc:
        logger.exception("Failed to disable user %s", user_id)
        await _audit_panel_mutation(
            request, "user_disable", "user", int(user_id), False, 500,
            extra={"error": str(exc)},
        )
        return web.json_response({"error": str(exc)}, status=500)
    await _audit_panel_mutation(
        request, "user_disable", "user", int(user_id), True, 200,
        extra={"affected": affected},
    )
    return web.json_response({"ok": True, "affected": affected})


async def handle_user_enable(request: web.Request) -> web.Response:
    """Enable a user by ID."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "JSON body required"}, status=400)
    user_id = body.get("user_id")
    if not user_id:
        return web.json_response({"error": "user_id required"}, status=400)
    try:
        affected = await db_views.toggle_user(int(user_id), enabled=True)
    except Exception as exc:
        logger.exception("Failed to enable user %s", user_id)
        await _audit_panel_mutation(
            request, "user_enable", "user", int(user_id), False, 500,
            extra={"error": str(exc)},
        )
        return web.json_response({"error": str(exc)}, status=500)
    await _audit_panel_mutation(
        request, "user_enable", "user", int(user_id), True, 200,
        extra={"affected": affected},
    )
    return web.json_response({"ok": True, "affected": affected})


# ---------------------------------------------------------------------------
# Route attachment
# ---------------------------------------------------------------------------

def attach_routes(app: web.Application) -> None:
    """Mount /manage/* routes on an existing aiohttp app.

    Args:
        app: The aiohttp Application to attach routes to.
    """
    # Read-only views (rate limited)
    app.router.add_get("/manage", handle_manage_index)
    app.router.add_get("/manage/sources", rate_limit("read", handle_sources_view))
    app.router.add_get("/manage/signals", rate_limit("read", handle_signals_view))
    app.router.add_get("/manage/positions", rate_limit("read", handle_positions_view))
    app.router.add_get("/manage/leaderboard", rate_limit("read", handle_leaderboard_view))
    app.router.add_get("/manage/trader/{trader_id}", rate_limit("read", handle_trader_drill))
    app.router.add_get("/manage/channels", rate_limit("read", handle_channels_view))
    app.router.add_get("/manage/users", rate_limit("read", handle_users_view))

    # Mutating API (CSRF + rate limited)
    app.router.add_post("/manage/api/sources/add", csrf_required(rate_limit("write", handle_sources_add)))
    app.router.add_post("/manage/api/sources/disable", csrf_required(rate_limit("write", handle_sources_disable)))
    app.router.add_post("/manage/api/sources/enable", csrf_required(rate_limit("write", handle_sources_enable)))
    app.router.add_post("/manage/api/channels/disable", csrf_required(rate_limit("write", handle_channel_disable)))
    app.router.add_post("/manage/api/channels/enable", csrf_required(rate_limit("write", handle_channel_enable)))
    app.router.add_post("/manage/api/users/disable", csrf_required(rate_limit("write", handle_user_disable)))
    app.router.add_post("/manage/api/users/enable", csrf_required(rate_limit("write", handle_user_enable)))

    # Static assets
    if _STATIC_DIR.exists():
        app.router.add_static("/manage/static/", path=str(_STATIC_DIR))


__all__ = ["attach_routes"]
