"""
shared.dashboard.server — aiohttp server for the Phase 36 owner
dashboard.

Endpoints:
  GET  /                            → static index.html (mobile UI)
  GET  /healthz                     → 200 OK
  POST /api/auth/request-otp        → {chat_id}
  POST /api/auth/verify-otp         → {chat_id, code} → {token, expires_at}
  POST /api/auth/logout             → revokes caller's session
  GET  /api/snapshot                → DashboardSnapshot (auth required)
  GET  /api/services                → registry view (auth required)
  GET  /api/sessions/active         → caller's own active sessions

Authentication is bearer-token in the ``Authorization`` header, or
(for browser clients that can't set headers easily) a ``?token=``
query-string fallback.

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
from shared.dashboard.snapshot import (
    SnapshotBuilder,
    SnapshotProviders,
    snapshot_to_dict,
)

LOG = logging.getLogger("tickles.dashboard.server")

WEB_DIR = Path(__file__).parent / "web"


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


@web.middleware
async def auth_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    public = request.app.get("_public_paths", set())
    if request.path in public or not request.path.startswith("/api/"):
        return await handler(request)

    auth: DashboardAuth = request.app["_auth"]
    token = _bearer(request)
    if not token:
        return _err(401, "missing bearer token")
    try:
        user, session = await auth.authenticate_token(token)
    except InvalidSession as e:
        return _err(401, str(e))
    except AuthError as e:  # pragma: no cover - defensive
        return _err(getattr(e, "http_status", 401), str(e))
    request["user"] = user
    request["session"] = session
    return await handler(request)


async def handle_index(request: web.Request) -> web.StreamResponse:
    path = WEB_DIR / "index.html"
    if not path.exists():
        return _err(404, "index.html missing")
    return web.FileResponse(path)


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
    return web.json_response({
        "ok": True,
        "token": result.token,
        "expires_at": result.expires_at.isoformat(),
        "session_id": result.session_id,
    })


async def handle_logout(request: web.Request) -> web.Response:
    session = request.get("session")
    if session and session.id is not None:
        auth: DashboardAuth = request.app["_auth"]
        await auth.revoke(session.id)
    return web.json_response({"ok": True})


async def handle_snapshot(request: web.Request) -> web.Response:
    builder: SnapshotBuilder = request.app["_snapshot_builder"]
    snap = await builder.build()
    return web.json_response(snapshot_to_dict(snap))


async def handle_services(request: web.Request) -> web.Response:
    providers: SnapshotProviders = request.app["_providers"]
    if providers.services is None:
        return web.json_response({"services": [], "note": "not wired"})
    data = await providers.services.list_services()
    return web.json_response({"services": data})


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
    app = web.Application(middlewares=[auth_middleware])
    app["_auth"] = auth
    app["_providers"] = providers
    app["_snapshot_builder"] = SnapshotBuilder(providers=providers)
    app["_expose_otp"] = expose_otp
    app["_public_paths"] = {
        "/", "/healthz",
        "/api/auth/request-otp", "/api/auth/verify-otp",
    }
    app.router.add_get("/", handle_index)
    app.router.add_get("/healthz", handle_health)
    app.router.add_post("/api/auth/request-otp", handle_request_otp)
    app.router.add_post("/api/auth/verify-otp", handle_verify_otp)
    app.router.add_post("/api/auth/logout", handle_logout)
    app.router.add_get("/api/snapshot", handle_snapshot)
    app.router.add_get("/api/services", handle_services)
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
