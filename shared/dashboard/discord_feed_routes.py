"""BIBLE-P4: Discord feed/tree/media/config routes. Mirrors news_routes.py shape."""
from __future__ import annotations
import os, json, logging
from aiohttp import web
from shared.dashboard.discord_feed_provider import (
    get_source_tree, get_feed, set_source_config, get_tracked_traders,
    get_user_config, set_user_config,
)
logger = logging.getLogger(__name__)

MEDIA_BASE_DIR = os.environ.get("DISCORD_MEDIA_DIR", "/opt/tickles/shared/collectors/discord/data/discord_media")

def _json(data, status=200):
    def _default(obj):
        if hasattr(obj, "isoformat"):
            s = obj.isoformat()
            if "+" in s or "Z" in s:
                return s
            return s + "Z"
        return str(obj)
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, default=_default))

async def handle_discord_tree(request: web.Request) -> web.Response:
    src = request.query.get("source", "discord")
    if src not in ("discord", "telegram"):
        src = "discord"
    return _json({"source": src, "tree": await get_source_tree(src)})

async def handle_discord_feed(request: web.Request) -> web.Response:
    channel = request.query.get("channel") or None
    src = request.query.get("source", "discord")
    try:
        limit = int(request.query.get("limit", "100"))
    except ValueError:
        limit = 100
    before_id = request.query.get("before_id")
    before_id = int(before_id) if (before_id and before_id.isdigit()) else None
    after_id = request.query.get("after_id")
    after_id = int(after_id) if (after_id and after_id.isdigit()) else None
    
    following_only = request.query.get("following_only") in ("true", "1")
    try:
        since_days = int(request.query.get("since_days", "7"))
    except ValueError:
        since_days = 7
    return _json({"channel": channel, "source": src,
                  "items": await get_feed(channel, src, limit, before_id, after_id, following_only, since_days)})

async def handle_discord_followed(request: web.Request) -> web.Response:
    return _json({"followed": await get_tracked_traders()})

async def handle_discord_media(request: web.Request) -> web.StreamResponse:
    """Serve a locally-saved attachment by relative path. Path-traversal guarded."""
    rel = request.match_info.get("path", "")
    # Hard guard: no '..', must resolve under MEDIA_BASE_DIR.
    safe = os.path.normpath(os.path.join(MEDIA_BASE_DIR, rel))
    if not safe.startswith(os.path.realpath(MEDIA_BASE_DIR)) or ".." in rel:
        return web.Response(status=403, text="forbidden")
    if not os.path.isfile(safe):
        return web.Response(status=404, text="not found")
    return web.FileResponse(safe)

async def handle_discord_config(request: web.Request) -> web.Response:
    if request.method == "GET":
        src = request.query.get("source", "discord")
        return _json({"tree": await get_source_tree(src)})
    # POST/PATCH: update one source
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid json"}, 400)
    sid = body.get("source_id")
    if not isinstance(sid, int):
        return _json({"error": "source_id (int) required"}, 400)
    mp = body.get("media_policy")
    if mp is not None and mp not in ("none", "text", "text_images", "everything"):
        return _json({"error": "bad media_policy"}, 400)
    return _json(await set_source_config(
        sid, enabled=body.get("enabled"), media_policy=mp,
        allowed_users=body.get("allowed_users"), blocked_users=body.get("blocked_users"),
    ))

async def handle_discord_user_config(request):
    if request.method == "GET":
        return _json({"traders": await get_user_config()})
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid json"}, 400)
    tid = body.get("trader_id")
    if not isinstance(tid, int) or isinstance(tid, bool):
        return _json({"error": "trader_id (int) required"}, 400)
    mt = body.get("tracked_media_types")
    if mt is not None and mt not in ("none", "text", "text_images", "everything", "all"):
        return _json({"error": "bad tracked_media_types"}, 400)
    return _json(await set_user_config(
        tid, is_tracked=body.get("is_tracked") if isinstance(body.get("is_tracked"), bool) else None, tracked_media_types=mt))
