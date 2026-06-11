# PHASE 4 — API (serve the tree, the feed, the media, the config)

> Prereq: Phase 3 DONE. Read rules in `00_MASTER_INDEX.md`.
> Goal: add read endpoints for the channel tree + message feed, a media endpoint that
> serves locally-saved attachments (so clicking an image loads instantly, no expired
> CDN), and a read/write config endpoint for the control room. PRESERVE all existing
> endpoints exactly.

## ARCHITECTURE YOU MUST FOLLOW (verified live)
The dashboard is aiohttp, modular: a `*_provider.py` does DB reads and shapes data; a
`*_routes.py` exposes handlers; `server.py::build_app` mounts them under BOTH `""` and
`"/dashboard"` prefixes like:
```python
        app.router.add_get(prefix + "/api/news/feed", handle_news_feed)
```
There is already `news_provider.py` + `news_routes.py` (read-only `/api/news/feed`),
and a `media_proxy.py`. **Mirror these patterns. Do not invent a new web framework or a
parallel app.** New code goes in two new files plus a few lines in `server.py`.

Back up:
```bash
cp /opt/tickles/shared/dashboard/server.py /opt/tickles/shared/dashboard/server.py.bak-bible
```

## STEP 1 — Create `shared/dashboard/discord_feed_provider.py`
Pure DB reads. Create the file with these functions (use the SAME pool accessor the
existing providers use — grep `news_provider.py` for how it gets a pool; copy that):
```python
"""BIBLE-P4: Discord/Telegram feed + tree + config providers (read/write helpers)."""
from __future__ import annotations
import json, logging
from typing import Any, Optional
logger = logging.getLogger(__name__)

# Reuse the dashboard's existing pool accessor — COPY the import the other providers use.
# e.g.:  from shared.dashboard.db_pools import get_shared_pool   (verify exact name!)
from shared.dashboard.db_pools import get_shared_pool  # <-- VERIFY this matches news_provider

async def get_source_tree(source_type: str = "discord") -> list[dict]:
    """Return the server→group→channel tree from collector_sources, with freshness +
    follow + media policy, so the sidebar and control room share one source of truth."""
    pool = await get_shared_pool()
    rows = await pool.fetch(
        "SELECT id, parent_id, entity_type, platform_id, name, enabled, "
        "       media_policy, allowed_users, blocked_users, last_collected_at, "
        "       last_error, error_count, "
        "       (platform_config->>'last_hwm') AS last_hwm "
        "FROM collector_sources WHERE source_type = $1 ORDER BY priority NULLS LAST, name",
        source_type,
    )
    return [dict(r) for r in rows]

async def get_feed(channel_name: Optional[str], source: str = "discord",
                   limit: int = 100, before_id: Optional[int] = None) -> list[dict]:
    """Most-recent messages for a channel (or all channels of a source), newest-first,
    keyset-paginated by id for the slow auto-refresh + infinite scroll."""
    pool = await get_shared_pool()
    clauses = ["source = $1"]
    args: list[Any] = [source]
    if channel_name:
        args.append(channel_name); clauses.append(f"channel_name = ${len(args)}")
    if before_id:
        args.append(before_id); clauses.append(f"id < ${len(args)}")
    args.append(max(1, min(limit, 200)))
    where = " AND ".join(clauses)
    rows = await pool.fetch(
        f"SELECT id, message_id, channel_name, author, author_id, author_role_color, "
        f"       content, headline, collected_at, has_media, media_count, "
        f"       local_media_paths, reply_to_msg_id, reply_to_author, reply_to_content, "
        f"       enrichment, enrichment_status, instruments "
        f"FROM news_items WHERE {where} ORDER BY id DESC LIMIT ${len(args)}",
        *args,
    )
    return [dict(r) for r in rows]

async def set_source_config(source_id: int, *, enabled: Optional[bool] = None,
                            media_policy: Optional[str] = None,
                            allowed_users: Optional[list] = None,
                            blocked_users: Optional[list] = None) -> dict:
    """Control-room write: update follow + media policy + user filters for one source."""
    pool = await get_shared_pool()
    sets, args = [], []
    if enabled is not None:      args.append(enabled);            sets.append(f"enabled = ${len(args)}")
    if media_policy is not None: args.append(media_policy);       sets.append(f"media_policy = ${len(args)}")
    if allowed_users is not None: args.append(json.dumps(allowed_users)); sets.append(f"allowed_users = ${len(args)}::jsonb")
    if blocked_users is not None: args.append(json.dumps(blocked_users)); sets.append(f"blocked_users = ${len(args)}::jsonb")
    if not sets:
        return {"updated": False, "reason": "no fields"}
    args.append(source_id)
    await pool.execute(
        f"UPDATE collector_sources SET {', '.join(sets)}, updated_at = now() WHERE id = ${len(args)}",
        *args,
    )
    return {"updated": True, "source_id": source_id}
```
> VALID `media_policy` values (define + enforce): `none`, `text`, `text_images`,
> `everything`. The collector (Phase 7 wires it) reads this to decide what to download.

## STEP 2 — Create `shared/dashboard/discord_feed_routes.py`
```python
"""BIBLE-P4: Discord feed/tree/media/config routes. Mirrors news_routes.py shape."""
from __future__ import annotations
import os, json, logging
from aiohttp import web
from shared.dashboard.discord_feed_provider import (
    get_source_tree, get_feed, set_source_config,
)
logger = logging.getLogger(__name__)

MEDIA_BASE_DIR = os.environ.get("DISCORD_MEDIA_DIR", "/opt/tickles/shared/media")  # VERIFY vs collector MEDIA_BASE_DIR

def _json(data, status=200):
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, default=str))

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
    return _json({"channel": channel, "source": src,
                  "items": await get_feed(channel, src, limit, before_id)})

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
```

## STEP 3 — Mount the routes in `server.py::build_app`
Find the block of `app.router.add_get(prefix + "/api/...")` lines (around line 918+).
Add, in the SAME style, inside the same prefix loop:
```python
        # BIBLE-P4: Discord/Telegram feed, tree, media, config
        app.router.add_get(prefix + "/api/discord/tree", handle_discord_tree)
        app.router.add_get(prefix + "/api/discord/feed", handle_discord_feed)
        app.router.add_get(prefix + "/api/discord/media/{path:.*}", handle_discord_media)
        app.router.add_get(prefix + "/api/discord/config", handle_discord_config)
        app.router.add_post(prefix + "/api/discord/config", handle_discord_config)
```
And import the handlers at the top of `server.py` next to the other route imports:
```python
        from shared.dashboard.discord_feed_routes import (
            handle_discord_tree, handle_discord_feed, handle_discord_media, handle_discord_config,
        )
```
> If `server.py` imports route handlers at module top vs inside build_app, MATCH the
> existing convention. Grep `from shared.dashboard.news_routes import` to see where/how.

## STEP 4 — Restart dashboard
```bash
systemctl restart tickles-dashboard
sleep 5
```

---

## VERIFY (all GREEN before Phase 5)
```bash
B=http://127.0.0.1:3101
echo "=== tree ==="; curl -s "$B/api/discord/tree" | head -c 400; echo
echo "=== feed (trading-zone) ==="; curl -s "$B/api/discord/feed?channel=%F0%9F%A6%A7%E3%83%BB trading-zone&limit=3" | head -c 600; echo
echo "=== feed all discord ==="; curl -s "$B/api/discord/feed?source=discord&limit=2" | head -c 600; echo
echo "=== config GET ==="; curl -s "$B/api/discord/config" | head -c 300; echo
```
All MUST return valid JSON (not 404/500). The feed items MUST include
`local_media_paths`, `reply_to_author`, `enrichment`.

Media endpoint: take a real relative path from a feed item's `local_media_paths` and:
```bash
curl -s -o /tmp/t.bin -w "%{http_code} %{content_type}\n" "$B/api/discord/media/<that-relative-path>"
```
MUST be `200 image/...`. A traversal attempt MUST be blocked:
```bash
curl -s -o /dev/null -w "%{http_code}\n" "$B/api/discord/media/../../etc/passwd"   # MUST be 403
```

## DOWNSTREAM SAFETY
- Existing endpoints unchanged: `curl -s $B/api/news/feed?limit=1` still returns 200 JSON;
  `curl -s $B/api/snapshot | head -c 100` still works.
- Dashboard didn't crash: `systemctl is-active tickles-dashboard` = active; no Traceback in
  its log on startup.

## ON SUCCESS
Append to PROGRESS.md:
`Phase 4 — DONE <iso> — added /api/discord/{tree,feed,media,config}; provider+routes mirror news_*; media served locally with traversal guard; existing APIs untouched.`
Then open `05_PHASE_CSS_TOKENS.md`.
