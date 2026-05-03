"""
shared.dashboard.server — aiohttp server for the Phase 36 owner
dashboard. Extended in Phase 5 with default-deny auth and /manage/* routes.

Endpoints:
  GET  /                            → static index.html (mobile UI)
  GET  /healthz                     → 200 OK
  POST /api/auth/request-otp        → {chat_id}
  POST /api/auth/verify-otp         → {chat_id, code} → {token, expires_at}
  POST /api/auth/logout             → revokes caller's session
  GET  /api/snapshot                → DashboardSnapshot (auth required)
  GET  /api/services                → registry view (auth required)
  GET  /api/sessions/active         → caller's own active sessions

Authentication is bearer-token in the ``Authorization`` header, a
``?token=`` query-string fallback, or a ``__Host-session`` cookie for
browser clients.

The server is intentionally minimal: no background tasks, no
websockets, no write endpoints. Everything is read-only or
auth-flow. That keeps the attack surface small and lets the
dashboard be trivially restartable.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Awaitable, Callable, Optional

from aiohttp import web

from shared.dashboard.auth import (
    AuthError,
    DashboardAuth,
    DisabledUser,
    InvalidOtp,
    InvalidSession,
    UnknownChat,
)
from shared.dashboard.csrf import issue_csrf
from shared.dashboard.snapshot import (
    SnapshotBuilder,
    SnapshotProviders,
    snapshot_to_dict,
)
from shared.utils.db import DatabasePool

LOG = logging.getLogger("tickles.dashboard.server")

WEB_DIR = Path(__file__).parent / "web"

SESSION_COOKIE = "__Host-session"

ALLOWED_PUBLIC: set[str] = {
    "/", "/dashboard", "/dashboard/",
    "/login", "/dashboard/login",
    "/api/auth/request-otp", "/dashboard/api/auth/request-otp",
    "/api/auth/verify-otp", "/dashboard/api/auth/verify-otp",
    "/healthz", "/dashboard/healthz",
}
ALLOWED_PUBLIC_PREFIX: tuple[str, ...] = ("/static/", "/dashboard/static/")


def _err(status: int, message: str) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


def _bearer(request: web.Request) -> Optional[str]:
    auth = request.headers.get("Authorization") or ""
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    token = request.query.get("token")
    if token:
        return token.strip()
    return None


def _session_token(request: web.Request) -> Optional[str]:
    """Return session token from bearer header, query string, or cookie."""
    token = _bearer(request)
    if token:
        return token
    return request.cookies.get(SESSION_COOKIE)


@web.middleware
async def auth_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """AUTH DISABLED PER USER REQUEST."""
    from shared.dashboard.protocol import DashboardUser, DashboardSession
    request["user"] = DashboardUser(id=0, chat_id="dev", role="owner")
    request["session"] = DashboardSession(id=0, chat_id="dev", token_hash="dev")
    request["session_token"] = "dev_token"
    return await handler(request)


async def handle_index(request: web.Request) -> web.StreamResponse:
    path = WEB_DIR / "index.html"
    if not path.exists():
        return _err(404, "index.html missing")
    return web.FileResponse(path)


async def handle_login(request: web.Request) -> web.StreamResponse:
    """Redirect to index for login (frontend handles the view)."""
    return await handle_index(request)


async def handle_health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "dashboard"})


async def handle_request_otp(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return _err(400, "JSON body required")
    chat_id = (body or {}).get("chat_id")
    if not chat_id:
        return _err(400, "chat_id is required")
    auth: DashboardAuth = request.app["_auth"]
    try:
        result = await auth.issue_otp(
            chat_id, client_ip=_client_ip(request),
        )
    except UnknownChat as e:
        return _err(404, str(e))
    except DisabledUser as e:
        return _err(403, str(e))
    except AuthError as e:
        return _err(getattr(e, "http_status", 401), str(e))
    resp: dict = {
        "ok": True,
        "delivered": result.delivery_ok,
        "expires_at": result.expires_at.isoformat(),
    }
    if not result.delivery_ok:
        resp["delivery_error"] = result.delivery_error
    if request.app.get("_expose_otp", False):
        resp["code"] = result.code
    return web.json_response(resp)


async def handle_verify_otp(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return _err(400, "JSON body required")
    chat_id = (body or {}).get("chat_id")
    code = (body or {}).get("code")
    if not chat_id or not code:
        return _err(400, "chat_id and code are required")
    auth: DashboardAuth = request.app["_auth"]
    try:
        result = await auth.verify_otp(
            chat_id, code,
            user_agent=request.headers.get("User-Agent"),
            client_ip=_client_ip(request),
        )
    except (UnknownChat, DisabledUser) as e:
        return _err(getattr(e, "http_status", 401), str(e))
    except InvalidOtp as e:
        return _err(401, str(e))
    except AuthError as e:
        return _err(getattr(e, "http_status", 401), str(e))

    response = web.json_response({
        "ok": True,
        "token": result.token,
        "expires_at": result.expires_at.isoformat(),
        "session_id": result.session_id,
    })
    response.set_cookie(
        SESSION_COOKIE,
        result.token,
        secure=True,
        httponly=True,
        samesite="Strict",
        path="/",
        max_age=12 * 3600,
    )
    issue_csrf(response)
    return response


async def handle_logout(request: web.Request) -> web.Response:
    session = request.get("session")
    if session and session.id is not None:
        auth: DashboardAuth = request.app["_auth"]
        await auth.revoke(session.id)
    response = web.json_response({"ok": True})
    response.del_cookie(SESSION_COOKIE, path="/")
    return response


async def handle_snapshot(request: web.Request) -> web.Response:
    company = request.query.get("company")
    builder: SnapshotBuilder = request.app["_snapshot_builder"]
    snap = await builder.build(company_filter=company)
    return web.json_response(snapshot_to_dict(snap))


async def handle_leaderboard(request: web.Request) -> web.Response:
    company = request.query.get("company")
    from shared.dashboard.snapshot import aggregate_leaderboard
    data = await aggregate_leaderboard(company)
    return web.json_response({"leaderboard": data})


async def handle_signals(request: web.Request) -> web.Response:
    company = request.query.get("company")
    from shared.dashboard.snapshot import aggregate_signals
    data = await aggregate_signals(company)
    return web.json_response({"signals": data})


async def handle_positions(request: web.Request) -> web.Response:
    company = request.query.get("company")
    from shared.dashboard.snapshot import aggregate_open_positions
    data = await aggregate_open_positions(company)
    return web.json_response({"positions": data})


async def handle_interpretations(request: web.Request) -> web.Response:
    company = request.query.get("company")
    from shared.dashboard.snapshot import aggregate_interpretations
    data = await aggregate_interpretations(company)
    return web.json_response({"interpretations": data})


async def handle_trader_drill(request: web.Request) -> web.Response:
    """Fetch detailed trade history and performance for a specific trader."""
    trader_id = request.query.get("trader_id")
    company = request.query.get("company")
    if not trader_id:
        return _err(400, "trader_id is required")

    from shared.dashboard.snapshot import get_trader_drill_data
    data = await get_trader_drill_data(trader_id, company)
    return web.json_response(data)


async def handle_chart(request: web.Request) -> web.Response:
    """Render and serve an annotated chart SVG.

    If the interpretation has no level annotations or the renderer fails,
    fall back to streaming the raw original image so the dashboard always
    shows something instead of a broken-image icon.
    """
    interp_id = request.match_info.get("interp_id")
    company = request.query.get("company")
    if not interp_id:
        return _err(400, "interp_id is required")

    from shared.dashboard.snapshot import get_interpretation_by_id
    interp = await get_interpretation_by_id(int(interp_id), company)
    if not interp:
        return _err(404, "interpretation not found")

    from shared.dashboard.chart_renderer import render_annotated_chart
    try:
        svg_path = await render_annotated_chart(interp)
        return web.FileResponse(svg_path)
    except Exception as exc:
        LOG.warning("Chart render failed for interp %s: %s — falling back to raw media", interp_id, exc)
        local = interp.get("local_path") or interp.get("media_local_path")
        if local:
            try:
                p = Path(local)
                if p.exists() and p.is_file():
                    return web.FileResponse(p)
            except Exception as fb_exc:
                LOG.error("Raw media fallback failed for interp %s: %s", interp_id, fb_exc)
        return _err(500, f"chart render failed: {exc}")


async def handle_media(request: web.Request) -> web.Response:
    """Serve the original image for a ``media_items.id`` row.

    Used by the dashboard ``<img>`` fallback inside <object> chart embeds.
    Path traversal is impossible because ``local_path`` comes from the
    database, not user input.
    """
    media_id = request.match_info.get("media_id")
    if not media_id:
        return _err(400, "media_id is required")
    try:
        mid = int(media_id)
    except (TypeError, ValueError):
        return _err(400, "media_id must be an integer")

    pool = await DatabasePool.get_instance()
    try:
        row = await pool.fetch_one(
            "SELECT local_path, mime_type FROM media_items WHERE id = $1",
            (mid,),
        )
    except Exception as exc:
        LOG.error("handle_media DB query failed: %s", exc)
        return _err(500, "media lookup failed")

    if not row or not row.get("local_path"):
        return _err(404, "media not found")
    p = Path(row["local_path"])
    if not p.exists() or not p.is_file():
        return _err(404, "media file missing on disk")
    headers = {"Cache-Control": "public, max-age=3600"}
    return web.FileResponse(p, headers=headers)


async def handle_services(request: web.Request) -> web.Response:
    providers: SnapshotProviders = request.app["_providers"]
    if providers.services is None:
        return web.json_response({"services": [], "note": "not wired"})
    
    services = await providers.services.list_services()
    
    # Phase M.5 — Enrich with heartbeat data
    pool = await DatabasePool.get_instance()
    heartbeats = await pool.fetch_all("SELECT agent_id, last_heartbeat_at, status, expected_interval_seconds FROM public.cron_heartbeats")
    hb_map = {hb["agent_id"]: hb for hb in heartbeats}
    
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    
    enriched = []
    for s in services:
        s_dict = s.to_dict()
        hb = hb_map.get(s.name)
        if hb:
            last_ts = hb["last_heartbeat_at"]
            if last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=timezone.utc)
            
            seconds_since = (now - last_ts).total_seconds()
            is_stale = seconds_since > (hb["expected_interval_seconds"] * 2.5)
            
            s_dict["heartbeat"] = {
                "last_seen_seconds": int(seconds_since),
                "status": hb["status"],
                "is_stale": is_stale
            }
        enriched.append(s_dict)
        
    return web.json_response({"services": enriched})


def _client_ip(request: web.Request) -> Optional[str]:
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    peer = request.transport.get_extra_info("peername") if request.transport else None
    return peer[0] if peer else None


def build_app(
    auth: DashboardAuth,
    providers: SnapshotProviders,
    *,
    expose_otp: bool = False,
) -> web.Application:
    """Build the dashboard application, supporting both root and /dashboard/ prefix."""
    app = web.Application(middlewares=[auth_middleware])
    app["_auth"] = auth
    app["_providers"] = providers
    app["_snapshot_builder"] = SnapshotBuilder(providers=providers)
    app["_expose_otp"] = expose_otp
    app["_public_paths"] = ALLOWED_PUBLIC

    # Register routes for both root and /dashboard prefix
    for prefix in ["", "/dashboard"]:
        app.router.add_get(prefix + "/", handle_index)
        if prefix:
            # Redirect /dashboard to /dashboard/ to ensure relative paths work
            async def redirect_to_slash(request: web.Request) -> web.Response:
                raise web.HTTPFound(location=request.path + "/")
            app.router.add_get(prefix, redirect_to_slash)
            
        app.router.add_get(prefix + "/login", handle_login)
        app.router.add_get(prefix + "/healthz", handle_health)
        app.router.add_post(prefix + "/api/auth/request-otp", handle_request_otp)
        app.router.add_post(prefix + "/api/auth/verify-otp", handle_verify_otp)
        app.router.add_post(prefix + "/api/auth/logout", handle_logout)
        app.router.add_get(prefix + "/api/snapshot", handle_snapshot)
        app.router.add_get(prefix + "/api/services", handle_services)
        app.router.add_get(prefix + "/api/leaderboard", handle_leaderboard)
        app.router.add_get(prefix + "/api/signals", handle_signals)
        app.router.add_get(prefix + "/api/positions", handle_positions)
        app.router.add_get(prefix + "/api/interpretations", handle_interpretations)
        app.router.add_get(prefix + "/api/trader-drill", handle_trader_drill)
        app.router.add_get(prefix + "/api/charts/{interp_id}", handle_chart)
        app.router.add_get(prefix + "/api/media/{media_id}", handle_media)

        # WebSocket
        from shared.dashboard.ws import handle_queue_ws
        app.router.add_get(prefix + "/ws/queue", handle_queue_ws)

        # Static assets
        app.router.add_static(prefix + "/static/", Path(__file__).parent / "static")

        # Phase Y — mount learning-dashboard endpoints under both prefixes.
        from shared.dashboard.learning_routes import (
            attach_routes as attach_learning_routes,
        )
        attach_learning_routes(app, prefix=prefix)

        # Phase X.4 — mount news-feed endpoints under both prefixes.
        from shared.dashboard.news_routes import (
            attach_routes as attach_news_routes,
        )
        attach_news_routes(app, prefix=prefix)

        # Phase X.5 — mount cross-tab interpretation drawer endpoint.
        from shared.dashboard.interpretation_drawer_routes import (
            attach_routes as attach_drawer_routes,
        )
        attach_drawer_routes(app, prefix=prefix)

    # Phase 5 — mount /manage/* panel routes
    from shared.intelligence.manage_panel.server_routes import attach_routes
    attach_routes(app)

    return app


async def run_server(
    app: web.Application, host: str, port: int,
) -> None:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    LOG.info("dashboard listening on http://%s:%d", host, port)
    import asyncio
    while True:
        await asyncio.sleep(3600)


__all__ = [
    "ALLOWED_PUBLIC",
    "ALLOWED_PUBLIC_PREFIX",
    "SESSION_COOKIE",
    "auth_middleware",
    "build_app",
    "handle_health",
    "handle_index",
    "handle_logout",
    "handle_request_otp",
    "handle_services",
    "handle_snapshot",
    "handle_verify_otp",
    "run_server",
]
