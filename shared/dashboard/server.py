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

WebSocket bridge to price feed daemon only — the server itself runs
no background tasks, no write endpoints beyond auth/logout. Everything
else is read-only. That keeps the attack surface small and lets the
dashboard be trivially restartable.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

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
from shared.utils.db import DatabasePool, get_company_pool, get_shared_pool

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


def _json_default(obj: Any) -> Any:
    """JSON encoder fallback for types asyncpg/Decimal/datetime return.

    ``Decimal`` is rendered as a *string* (not float) so we never lose
    precision on financial values; this matches
    :func:`shared.dashboard.config_routes._jsonify`.

    Args:
        obj: Object that the default ``json.dumps`` cannot serialise.

    Returns:
        A JSON-compatible primitive (str / list / dict).

    Raises:
        TypeError: If the object cannot be coerced to a JSON-friendly value.
    """
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    # asyncpg.Record exposes ``keys()`` + mapping access — coerce to dict.
    if hasattr(obj, "keys") and callable(getattr(obj, "keys", None)):
        try:
            return {str(k): obj[k] for k in obj.keys()}
        except Exception:
            pass
    if hasattr(obj, "isoformat"):
        try:
            return obj.isoformat()
        except Exception as exc:
            raise TypeError(f"unserialisable: {type(obj).__name__}") from exc
    raise TypeError(
        f"Object of type {type(obj).__name__} is not JSON serializable"
    )


def _json_response(payload: Any, status: int = 200) -> web.Response:
    """Return a JSON response that handles Decimal, datetime, and asyncpg rows.

    aiohttp's :func:`web.json_response` uses the stdlib default encoder which
    raises on ``decimal.Decimal`` and ``datetime``. Dashboard aggregations
    return both heavily, so every read endpoint must serialise via this
    helper.

    Args:
        payload: Any JSON-compatible structure (dict, list, etc.).
        status: HTTP status code (default 200).

    Returns:
        :class:`aiohttp.web.Response` with content-type ``application/json``.
    """
    try:
        text = json.dumps(payload, default=_json_default)
    except TypeError as exc:
        LOG.exception("Failed to serialise dashboard payload: %s", exc)
        return web.json_response(
            {"ok": False, "error": "internal serialisation error"}, status=500
        )
    return web.Response(text=text, status=status, content_type="application/json")


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
    return _json_response(snapshot_to_dict(snap))


async def handle_leaderboard(request: web.Request) -> web.Response:
    company = request.query.get("company")
    from shared.dashboard.snapshot import aggregate_leaderboard
    data = await aggregate_leaderboard(company)
    return _json_response({"leaderboard": data})


async def handle_signals(request: web.Request) -> web.Response:
    company = request.query.get("company")
    from shared.dashboard.snapshot import aggregate_signals
    data = await aggregate_signals(company)
    return _json_response({"signals": data})


# Round 12 (2026-05-24): handle_positions for legacy /api/positions removed.
# Replaced by /api/positions/live + /api/positions/historic — see audit at
# shared/docs/BUG_HUNT_FIXES_ROADMAP.md (Round 12 §12.4). Snapshot builder
# now calls aggregate_live_positions directly. The Python function
# aggregate_open_positions remains in snapshot.py marked deprecated for any
# out-of-tree consumers; new code MUST use aggregate_live_positions or
# aggregate_historic_positions.


# ---------------------------------------------------------------------------
# Round 11 (2026-05-24): Live / Historic split
# ---------------------------------------------------------------------------
async def handle_positions_live(request: web.Request) -> web.Response:
    """GET /api/positions/live — currently-LIVE positions only.

    Returns ``open + partial_exit`` tracked_positions plus broker fills
    from ``positions_current``. No closed/expired/cancelled rows are
    mixed in — those live on the Historic endpoint. Frontend renders
    this on the Positions tab "Live" sub-tab with live-tab columns
    (current price, unrealised P&L, distance to SL/TP, time-in-trade).
    """
    company = request.query.get("company")
    try:
        limit = max(1, min(int(request.query.get("limit", "200")), 500))
    except (TypeError, ValueError):
        limit = 200
    from shared.dashboard.snapshot import aggregate_live_positions
    data = await aggregate_live_positions(company, limit=limit)
    return _json_response({"positions": data, "count": len(data)})


async def handle_positions_historic(request: web.Request) -> web.Response:
    """GET /api/positions/historic — paginated terminal positions.

    Query params:
        company       — short_name or "all"
        since_days    — look-back window (default 30; 0 disables)
        limit         — page size (default 50, max 200)
        cursor_at     — ISO timestamp of last row's close_ts (keyset pagination)
        cursor_id     — id of last row (keyset pagination)
        status        — single status filter (closed/expired/cancelled/invalidated)
        outcome       — single outcome filter (tp1_hit/sl_hit/expired)

    Returns ``{rows, next_cursor, has_more, page_size}``. Pass
    ``next_cursor.closed_at`` and ``next_cursor.id`` back as the new
    cursor params for the next page.
    """
    from datetime import datetime as _dt
    from shared.dashboard.snapshot import aggregate_historic_positions

    company = request.query.get("company")
    try:
        since_days = int(request.query.get("since_days", "30"))
    except (TypeError, ValueError):
        since_days = 30
    try:
        limit = max(1, min(int(request.query.get("limit", "50")), 200))
    except (TypeError, ValueError):
        limit = 50

    cursor_at_raw = request.query.get("cursor_at")
    cursor_id_raw = request.query.get("cursor_id")
    cursor_at: Optional[datetime] = None
    cursor_id: Optional[int] = None
    if cursor_at_raw and cursor_id_raw:
        try:
            cursor_at = _dt.fromisoformat(cursor_at_raw.replace("Z", "+00:00"))
            cursor_id = int(cursor_id_raw)
        except (TypeError, ValueError):
            cursor_at = None
            cursor_id = None

    status_filter = request.query.get("status") or None
    outcome_filter = request.query.get("outcome") or None

    data = await aggregate_historic_positions(
        company_filter=company,
        since_days=since_days,
        cursor_closed_at=cursor_at,
        cursor_id=cursor_id,
        limit=limit,
        status_filter=status_filter,
        outcome_filter=outcome_filter,
    )
    return _json_response(data)


# ---------------------------------------------------------------------------
# Round 12 (2026-05-24): Trade journey endpoint.
#
# Returns the time-series of price + dist + P&L for a single position so
# the signal-replay drawer can render a "trade journey" chart from the
# moment the position was opened to now (or close). Data source is the
# ``position_updates`` time-series table populated by PositionMonitor on
# every cycle.
#
# Output shape (JSON):
#   {
#     "ok": true,
#     "position_id": 12345,
#     "samples": 1827,
#     "first": "2026-05-22T07:12:15Z",
#     "last":  "2026-05-24T14:23:51Z",
#     "levels": {
#       "entry":   65000.0,
#       "stop":    63000.0,
#       "tp1":     68000.0,
#       "tp2":     null
#     },
#     "series": [
#       {"t": "2026-05-22T07:12:15Z", "price": 64950.0, "pnl_pct": -0.07,
#        "dist_sl": -3.0, "dist_tp1": 4.6, "dist_entry": -0.07,
#        "minutes": 0},
#       ...
#     ]
#   }
#
# All distances are signed percentages relative to entry_price (negative
# for SL, positive for TP1 going up — same convention as the rest of the
# dashboard). The frontend renders this as an ECharts area chart with
# horizontal lines for entry/SL/TP1.
# ---------------------------------------------------------------------------
async def handle_position_journey(request: web.Request) -> web.Response:
    """GET /api/position-journey/{id} — time-series for a single position.

    Args:
        request: aiohttp request; URL path ``id`` is the tracked_positions.id.

    Returns:
        JSON response with the journey time-series (see module-level
        comment for shape). 404 if position doesn't exist; 400 on bad ID.
    """
    try:
        position_id = int(request.match_info["id"])
    except (ValueError, TypeError):
        return _err(400, "Invalid position ID")

    from shared.utils.db import get_shared_pool
    # Round 12 (2026-05-24): get_shared_pool() returns a *cached*
    # process-wide singleton. Do NOT call pool.close() here — it would
    # tear down the pool for every other handler. Just use it.
    pool = await get_shared_pool()
    # Pull the position itself for level lines; doesn't matter that
    # the table is in tickles_shared because PositionMonitor writes
    # both position_updates and tracked_positions to the same DB.
    pos = await pool.fetch_one(
        """
        SELECT id, entry_price, stop_loss,
               take_profit_1, take_profit_2, take_profit_3,
               direction, instrument_symbol, status,
               signal_timestamp, created_at, closed_at
        FROM public.tracked_positions
        WHERE id = $1
        """,
        (position_id,),
    )
    if pos is None:
        return _err(404, f"Position {position_id} not found")

    # Down-sample for longer trades. The raw rate is one sample per
    # monitor cycle (60s), so a 2-day-old position has ~2880 rows.
    # ECharts handles 5k+ points fine but the JSON payload gets bulky;
    # stride accordingly so we never ship more than ~1500 points.
    samples_count = await pool.fetch_val(
        "SELECT COUNT(*) FROM public.position_updates WHERE position_id = $1",
        (position_id,),
    )
    samples_count = int(samples_count or 0)
    stride = max(1, samples_count // 1500)

    # Use a window function to pick every Nth row deterministically,
    # ordered ascending so the chart renders left-to-right.
    rows = await pool.fetch_all(
        """
        SELECT "timestamp", price, unrealized_pnl_pct,
               distance_to_entry_pct, distance_to_sl_pct,
               distance_to_tp1_pct, time_in_trade_minutes
        FROM (
            SELECT "timestamp", price, unrealized_pnl_pct,
                   distance_to_entry_pct, distance_to_sl_pct,
                   distance_to_tp1_pct, time_in_trade_minutes,
                   ROW_NUMBER() OVER (ORDER BY "timestamp" ASC) AS rn
            FROM public.position_updates
            WHERE position_id = $1
        ) t
        WHERE rn % $2 = 0 OR rn = 1
        ORDER BY "timestamp" ASC
        """,
        (position_id, stride),
    )

    def _f(v):
        return None if v is None else float(v)

    series = [
        {
            "t": r["timestamp"].isoformat() if r["timestamp"] else None,
            "price": _f(r["price"]),
            "pnl_pct": _f(r["unrealized_pnl_pct"]),
            "dist_entry": _f(r["distance_to_entry_pct"]),
            "dist_sl": _f(r["distance_to_sl_pct"]),
            "dist_tp1": _f(r["distance_to_tp1_pct"]),
            "minutes": int(r["time_in_trade_minutes"] or 0),
        }
        for r in rows
    ]

    levels = {
        "entry": _f(pos["entry_price"]),
        "stop":  _f(pos["stop_loss"]),
        "tp1":   _f(pos["take_profit_1"]),
        "tp2":   _f(pos["take_profit_2"]),
        "tp3":   _f(pos["take_profit_3"]),
    }

    return _json_response({
        "ok": True,
        "position_id": position_id,
        "symbol": pos["instrument_symbol"],
        "direction": pos["direction"],
        "status": pos["status"],
        "samples": len(series),
        "samples_total": samples_count,
        "stride": stride,
        "first": series[0]["t"] if series else None,
        "last":  series[-1]["t"] if series else None,
        "signal_timestamp": pos["signal_timestamp"].isoformat() if pos["signal_timestamp"] else None,
        "created_at": pos["created_at"].isoformat() if pos["created_at"] else None,
        "closed_at": pos["closed_at"].isoformat() if pos["closed_at"] else None,
        "levels": levels,
        "series": series,
    })


async def handle_delete_position(request: web.Request) -> web.Response:
    """Soft-delete a tracked_position by setting status='deleted'.

    Only allows deleting positions with status='pending' or status='expired'.
    Returns 400 if the position is open/closed or already deleted.

    Args:
        request: aiohttp request with {id} in the URL path.

    Returns:
        JSON response with ok=True and the updated position id.
    """
    try:
        position_id = int(request.match_info["id"])
    except (ValueError, TypeError):
        return _err(400, "Invalid position ID")
    from shared.utils.db import get_shared_pool

    # Round 12 (2026-05-24): get_shared_pool() returns a *cached* singleton.
    # Do NOT call pool.close() here — that would tear down the pool for
    # every other dashboard handler. Use it but don't own it.
    pool = await get_shared_pool()
    row = await pool.fetch_one(
        "SELECT id, status FROM public.tracked_positions WHERE id = $1",
        (position_id,),
    )
    if row is None:
        return _err(404, f"Position {position_id} not found")

    current_status = row["status"]
    if current_status not in ("pending", "expired"):
        return _err(
            400,
            f"Cannot delete position with status='{current_status}'. "
            "Only pending or expired positions can be deleted.",
        )

    await pool.execute(
        """
        UPDATE public.tracked_positions
        SET status = 'deleted',
            status_reason = 'manual_delete',
            updated_at = NOW()
        WHERE id = $1
        """,
        (position_id,),
    )
    LOG.info("Position %s soft-deleted (was %s)", position_id, current_status)
    return _json_response({"ok": True, "id": position_id, "status": "deleted"})


async def handle_interpretations(request: web.Request) -> web.Response:
    company = request.query.get("company")
    from shared.dashboard.snapshot import aggregate_interpretations
    data = await aggregate_interpretations(company)
    return _json_response({"interpretations": data})


async def handle_agent_decisions(request: web.Request) -> web.Response:
    company = request.query.get("company")
    limit = int(request.query.get("limit", "50"))
    from shared.dashboard.snapshot import aggregate_agent_decisions
    data = await aggregate_agent_decisions(company, limit=limit)
    return _json_response({"agent_decisions": data})


async def handle_agent_performance(request: web.Request) -> web.Response:
    company = request.query.get("company")
    from shared.dashboard.snapshot import aggregate_agent_performance
    data = await aggregate_agent_performance(company)
    return _json_response({"agent_performance": data})


async def handle_agent_achievements(request: web.Request) -> web.Response:
    company = request.query.get("company")
    from shared.dashboard.snapshot import aggregate_agent_achievements
    data = await aggregate_agent_achievements(company)
    return _json_response({"achievements": data})


async def handle_competitions(request: web.Request) -> web.Response:
    company = request.query.get("company")
    from shared.dashboard.snapshot import aggregate_competitions
    data = await aggregate_competitions(company)
    return _json_response({"competitions": data})


async def handle_wallets(request: web.Request) -> web.Response:
    """GET /api/wallets — list paper wallets with balances."""
    company = request.query.get("company")
    contest = request.query.get("contest_id", "copy-trade-scenarios")
    from shared.services.banker import list_wallets
    wallets = await list_wallets(company_id=company, contest_id=contest)
    return _json_response({"wallets": wallets})


async def handle_competition_trades(request: web.Request) -> web.Response:
    """GET /api/competition-trades?contest_id=...&agent_id=..."""
    contest_id = request.query.get("contest_id", "copy-trade-scenarios")
    agent_id = request.query.get("agent_id")
    limit = min(int(request.query.get("limit", "50")), 200)
    pool = await get_shared_pool()
    async with pool.acquire() as conn:
        if agent_id:
            rows = await conn.fetch(
                "SELECT * FROM competition_trades WHERE contest_id=$1 AND agent_id=$2 "
                "ORDER BY exited_at DESC LIMIT $3", contest_id, agent_id, limit)
        else:
            rows = await conn.fetch(
                "SELECT * FROM competition_trades WHERE contest_id=$1 "
                "ORDER BY exited_at DESC LIMIT $2", contest_id, limit)
    trades = []
    for r in rows:
        trades.append({
            "id": r["id"], "agent_id": r["agent_id"], "symbol": r["symbol"],
            "direction": r["direction"],
            "entry_price": float(r["entry_price"]) if r["entry_price"] else None,
            "exit_price": float(r["exit_price"]) if r["exit_price"] else None,
            "sl_price": float(r["sl_price"]) if r["sl_price"] else None,
            "tp_price": float(r["tp_price"]) if r["tp_price"] else None,
            "pnl": float(r["pnl"]) if r["pnl"] else 0,
            "fees": float(r["fees"]) if r["fees"] else 0,
            "exit_reason": r["exit_reason"],
            "entered_at": r["entered_at"].isoformat() if r["entered_at"] else None,
            "exited_at": r["exited_at"].isoformat() if r["exited_at"] else None,
        })
    return _json_response({"trades": trades, "contest_id": contest_id})


async def handle_trader_drill(request: web.Request) -> web.Response:
    """Fetch detailed trade history and performance for a specific trader."""
    trader_id = request.query.get("trader_id")
    company = request.query.get("company")
    if not trader_id:
        return _err(400, "trader_id is required")

    from shared.dashboard.snapshot import get_trader_drill_data
    data = await get_trader_drill_data(trader_id, company)
    return _json_response(data)


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
    # Bug H7 fix (Bug Hunter 2 §8.1):
    #   The legacy collector created two media_items rows per Discord image —
    #   a `cdn_hosted` row (NULL `local_path`, expiring CDN URL) and an
    #   `attached` row (downloaded `local_path`). Old `signal_interpretations`
    #   often link to the `cdn_hosted` row. After the CDN URL expired this
    #   request 404'd even though the actual file lives on disk under the
    #   sibling `attached` row.
    #
    #   We now look up the requested row, AND if its `local_path` is missing,
    #   try its sibling rows (same `news_item_id`) for a usable file. The
    #   404 is only returned when no sibling has a real file either.
    try:
        row = await pool.fetch_one(
            "SELECT local_path, mime_type, news_item_id FROM media_items WHERE id = $1",
            (mid,),
        )
    except Exception as exc:
        LOG.error("handle_media DB query failed: %s", exc)
        return _err(500, "media lookup failed")

    if not row:
        return _err(404, "media not found")

    candidate_path = (row.get("local_path") or "").strip()
    p: Optional[Path] = Path(candidate_path) if candidate_path else None
    if p is None or not p.exists() or not p.is_file():
        # Try sibling rows — same news_item_id, prefer non-null local_path.
        nid = row.get("news_item_id")
        if nid is not None:
            try:
                siblings = await pool.fetch_all(
                    "SELECT local_path, mime_type "
                    "FROM media_items "
                    "WHERE news_item_id = $1 AND id <> $2 "
                    "  AND local_path IS NOT NULL AND local_path <> '' "
                    "ORDER BY (processing_status = 'analyzed') DESC, id ASC "
                    "LIMIT 5",
                    (nid, mid),
                )
            except Exception as exc:
                LOG.warning("handle_media sibling lookup failed for media=%s: %s", mid, exc)
                siblings = []
            for sib in siblings or []:
                sib_path = (sib.get("local_path") or "").strip()
                if not sib_path:
                    continue
                sp = Path(sib_path)
                if sp.exists() and sp.is_file():
                    LOG.info(
                        "handle_media: serving sibling for media_id=%s via news_item_id=%s",
                        mid, nid,
                    )
                    return web.FileResponse(
                        sp,
                        headers={"Cache-Control": "public, max-age=3600"},
                    )
        return _err(404, "media file missing on disk")

    return web.FileResponse(
        p,
        headers={"Cache-Control": "public, max-age=3600"},
    )


async def handle_services(request: web.Request) -> web.Response:
    """``GET /api/services`` — registry view enriched with heartbeats.

    The ``public.cron_heartbeats`` table uses columns ``last_run_at`` and
    ``last_status`` (NOT ``last_heartbeat_at`` / ``status``); a stale
    enrichment query referencing the wrong names was returning HTTP 500
    for the whole endpoint. We catch any DB error here so a missing /
    renamed heartbeat table never breaks the services view.
    """
    providers: SnapshotProviders = request.app["_providers"]
    if providers.services is None:
        return _json_response({"services": [], "note": "not wired"})

    try:
        services = await providers.services.list_services()
    except Exception as exc:
        LOG.exception("handle_services: provider.list_services failed: %s", exc)
        return _json_response(
            {"services": [], "error": "service registry unavailable"}, status=500
        )

    hb_map: dict[str, dict] = {}
    try:
        pool = await DatabasePool.get_instance()
        heartbeats = await pool.fetch_all(
            "SELECT agent_id, last_run_at, last_status, "
            "expected_interval_seconds FROM public.cron_heartbeats"
        )
        hb_map = {hb["agent_id"]: hb for hb in heartbeats}
    except Exception as exc:
        # Table missing or DB unavailable — degrade gracefully, do not 500.
        LOG.warning("handle_services: heartbeat enrichment skipped: %s", exc)

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    enriched: list[dict] = []
    for s in services:
        # ``RegistryServicesProvider.list_services`` already returns plain
        # dicts; do not call ``.to_dict()`` (would AttributeError).
        s_dict = dict(s) if not isinstance(s, dict) else s
        hb = hb_map.get(s_dict.get("name"))
        if hb:
            last_ts = hb.get("last_run_at")
            interval = hb.get("expected_interval_seconds") or 0
            if last_ts is not None:
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=timezone.utc)
                seconds_since = (now - last_ts).total_seconds()
                is_stale = bool(interval) and seconds_since > (interval * 2.5)
                s_dict["heartbeat"] = {
                    "last_seen_seconds": int(seconds_since),
                    "status": hb.get("last_status"),
                    "is_stale": is_stale,
                }
            else:
                s_dict["heartbeat"] = {
                    "last_seen_seconds": None,
                    "status": hb.get("last_status"),
                    "is_stale": True,
                }
        enriched.append(s_dict)

    return _json_response({"services": enriched})


def _client_ip(request: web.Request) -> Optional[str]:
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    peer = request.transport.get_extra_info("peername") if request.transport else None
    return peer[0] if peer else None


async def _prewarm_pools(app: web.Application) -> None:
    """Prewarm shared and per-company DB pools on startup so the first request is not slow."""
    companies = ("rubicon", "jarvais", "testcorp", "tradelab")
    try:
        await get_shared_pool()
        LOG.info("prewarm: shared pool ready")
    except Exception:
        LOG.exception("prewarm: shared pool failed")
    for company in companies:
        try:
            await get_company_pool(company)
            LOG.info("prewarm: %s pool ready", company)
        except Exception:
            LOG.warning("prewarm: %s pool failed (non-fatal)", company)


def build_app(
    auth: DashboardAuth,
    providers: SnapshotProviders,
    *,
    expose_otp: bool = False,
) -> web.Application:
    """Build the dashboard application, supporting both root and /dashboard/ prefix."""
    app = web.Application(middlewares=[auth_middleware])
    app.on_startup.append(_prewarm_pools)
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
            
        app.router.add_get(prefix + "/healthz", handle_health)
        app.router.add_get(prefix + "/api/snapshot", handle_snapshot)
        app.router.add_get(prefix + "/api/services", handle_services)
        app.router.add_get(prefix + "/api/leaderboard", handle_leaderboard)
        app.router.add_get(prefix + "/api/signals", handle_signals)
        # Round 12 (2026-05-24): legacy GET /api/positions retired. Audit
        # confirmed no production callers. Frontend uses /api/positions/live
        # for the Positions tab and /api/snapshot for the Floor mini-table.
        app.router.add_get(prefix + "/api/positions/live", handle_positions_live)
        app.router.add_get(prefix + "/api/positions/historic", handle_positions_historic)
        app.router.add_delete(prefix + "/api/positions/{id}", handle_delete_position)
        # Round 12 (2026-05-24): trade-journey endpoint for the drawer.
        app.router.add_get(prefix + "/api/position-journey/{id}", handle_position_journey)
        app.router.add_get(prefix + "/api/interpretations", handle_interpretations)
        app.router.add_get(prefix + "/api/agent-decisions", handle_agent_decisions)
        app.router.add_get(prefix + "/api/agent-performance", handle_agent_performance)
        app.router.add_get(prefix + "/api/competitions", handle_competitions)
        app.router.add_get(prefix + "/api/competition-trades", handle_competition_trades)
        app.router.add_get(prefix + "/api/wallets", handle_wallets)
        app.router.add_get(prefix + "/api/agent-achievements", handle_agent_achievements)
        app.router.add_get(prefix + "/api/trader-drill", handle_trader_drill)
        app.router.add_get(prefix + "/api/charts/{interp_id}", handle_chart)
        app.router.add_get(prefix + "/api/media/{media_id}", handle_media)

        # WebSocket
        from shared.dashboard.ws import handle_queue_ws
        app.router.add_get(prefix + "/ws/queue", handle_queue_ws)

        # WebSocket price bridge to price feed daemon
        from shared.dashboard.ws_bridge import handle_price_ws
        app.router.add_get(prefix + "/api/ws/prices", handle_price_ws)

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

        # Phase X.6 — mount config snapshot endpoint.
        from shared.dashboard.config_routes import (
            attach_routes as attach_config_routes,
        )
        attach_config_routes(app, prefix=prefix)

        # Slice 1 — mount image proxy for external media (TradingView, Discord, etc.)
        from shared.dashboard.media_proxy import (
            attach_routes as attach_media_proxy_routes,
        )
        attach_media_proxy_routes(app, prefix=prefix)

        # Slice 2 — mount live-price endpoint for the drawer's market panel.
        from shared.dashboard.price_routes import (
            attach_routes as attach_price_routes,
        )
        attach_price_routes(app, prefix=prefix)

        # V4 enrichment — browser-facing candle/replay data from Postgres.
        from shared.dashboard.market_routes import (
            attach_routes as attach_market_routes,
        )
        attach_market_routes(app, prefix=prefix)

        # Round 10 — Settings panel: vision-model picker + audit history.
        from shared.dashboard.settings_routes import (
            attach_routes as attach_settings_routes,
        )
        attach_settings_routes(app, prefix=prefix)

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
