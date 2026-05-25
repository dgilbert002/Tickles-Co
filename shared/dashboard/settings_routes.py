"""
Module: settings_routes
Purpose: Dashboard Settings panel API — Round 10 + Round 13.5 (2026-05-24).

Endpoints
---------
* ``GET  /api/settings/vision-models``
    Returns the curated vision-model catalogue plus the currently-resolved
    model in each of the three slots (primary / fallback / prefilter), so
    the dropdowns can render with the correct selected option and a price
    label next to each option.

* ``POST /api/settings/vision-model``
    Body: ``{"slot": "primary"|"fallback"|"prefilter", "model": "<openrouter-id>"}``.
    Validates the model id against the curated allow-list, persists to
    ``public.system_config``, writes an audit row, and invalidates the cache
    so the next interpretation cycle picks it up.

* ``POST /api/settings/test-vision-model``
    Body: ``{"model": "<openrouter-id>", "media_id": <int|optional>}``.
    Runs ONE off-the-record vision-LLM call against the requested model
    using the most recent successfully-interpreted chart (or the explicit
    ``media_id`` if provided). Returns the JSON the model produced so the
    operator can compare extraction quality side-by-side with the current
    interpretation BEFORE switching the dropdown.

* ``GET  /api/settings/vision-model-history``
    Returns the most recent N rows from ``public.model_config_audit`` for
    forensic correlation when extraction quality changes.

* ``GET  /api/settings/dedup``  (Round 13.5)
    Returns the current state of every dedup knob (entry_pct_threshold,
    freshness_hours, lookback_hours) including which tier (db/env/default)
    the value came from.

* ``POST /api/settings/dedup``  (Round 13.5)
    Body: ``{"key": "entry_pct_threshold", "value": 1.0}``.
    Validates the knob name + bounds, upserts ``public.system_config``,
    invalidates the in-process cache so the next interpretation cycle
    picks up the new value.

Auth posture
------------
Per Round 10 user decision: this private-server dashboard runs without auth,
so these routes are mounted on the public router. We DO write an audit row
on every change so the change history is recoverable.

Rate limiting
-------------
Mutations are inherently low-frequency (operator clicks). We piggy-back on
the dashboard rate-limit middleware which already counts ``write`` ops, so
nothing extra needed here.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aiohttp import web

from shared.intelligence.model_config import (
    SLOT_PREFILTER,
    SLOT_PRIMARY,
    SLOT_FALLBACK,
    VALID_SLOTS,
    get_all_slots,
    get_catalogue,
    is_allowed,
    set_model,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _json_response(payload: Dict[str, Any], status: int = 200) -> web.Response:
    """JSON response with a no-cache header so the dropdown is always fresh."""
    return web.json_response(
        payload,
        status=status,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


def _err(message: str, *, status: int = 400, **extra) -> web.Response:
    return _json_response({"ok": False, "error": message, **extra}, status=status)


def _actor_label(request: web.Request) -> str:
    """Best-effort identity tag for the audit row.

    Without auth we can only stamp the peer IP and a 'dashboard-anon' marker.
    When auth ships later, the auth middleware can swap this for a session id.
    """
    peer = request.remote or "unknown-peer"
    return f"dashboard-anon@{peer}"


# ---------------------------------------------------------------------------
# GET /api/settings/vision-models
# ---------------------------------------------------------------------------
async def handle_list_vision_models(request: web.Request) -> web.Response:
    """Return the curated catalogue plus the currently-resolved per-slot model.

    Shape::

        {
          "ok": true,
          "slots": {
            "primary":   {"slot": "primary",   "model": "qwen/qwen3-vl-32b-instruct", "source": "code_default", ...},
            "fallback":  {...},
            "prefilter": {...}
          },
          "catalogue": {
            "models": [
              {"id": "...", "label": "...", "vendor": "...", "vision": true,
               "input_cost_per_million_tokens_usd": 0.50, "recommended_for": [...], ...}
            ]
          }
        }
    """
    try:
        slots = await get_all_slots()
        catalogue = get_catalogue()
    except Exception as exc:
        logger.exception("settings: failed to read state")
        return _err(f"failed to read settings state: {exc}", status=500)

    return _json_response({
        "ok": True,
        "slots": slots,
        "catalogue": catalogue,
    })


# ---------------------------------------------------------------------------
# POST /api/settings/vision-model
# ---------------------------------------------------------------------------
async def handle_set_vision_model(request: web.Request) -> web.Response:
    """Persist a model choice and return the new state of all 3 slots."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _err("body must be JSON")
    if not isinstance(body, dict):
        return _err("body must be a JSON object")

    slot = (body.get("slot") or "").strip().lower()
    model = (body.get("model") or "").strip()

    if slot not in VALID_SLOTS:
        return _err(
            f"slot must be one of {sorted(VALID_SLOTS)}; got {slot!r}",
            status=400,
        )
    if not model:
        return _err("model is required and must be a non-empty string")
    if not is_allowed(model):
        return _err(
            f"model {model!r} is not in the curated allow-list. "
            f"Edit shared/intelligence/vision_model_catalogue.json to add it.",
            status=400,
            allowed_count=len(get_catalogue().get("models", [])),
        )

    try:
        result = await set_model(slot, model, actor_label=_actor_label(request))
    except ValueError as exc:
        return _err(str(exc), status=400)
    except Exception as exc:
        logger.exception("settings: set_model failed")
        return _err(f"failed to persist: {exc}", status=500)

    # Re-read so the UI knows the full state (including how the other 2 slots are now resolved).
    slots = await get_all_slots()
    return _json_response({
        "ok": True,
        "changed": result,
        "slots": slots,
    })


# ---------------------------------------------------------------------------
# POST /api/settings/test-vision-model
# ---------------------------------------------------------------------------
# Body: {"model": "<id>", "media_id": <optional int>}
# Picks the latest interpretation if media_id missing, re-runs the LLM call
# with the requested model, returns parsed JSON + raw response for visual
# A/B comparison BEFORE the operator commits the dropdown change.
async def handle_test_vision_model(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _err("body must be JSON")
    if not isinstance(body, dict):
        return _err("body must be a JSON object")

    model = (body.get("model") or "").strip()
    media_id = body.get("media_id")
    if not model:
        return _err("model is required")
    if not is_allowed(model):
        return _err(f"model {model!r} not in allow-list", status=400)
    if media_id is not None:
        try:
            media_id = int(media_id)
        except (TypeError, ValueError):
            return _err("media_id must be an integer")

    # Pick the test target.
    from shared.utils.db import get_shared_pool
    pool = await get_shared_pool()

    # Schema notes:
    #   signal_interpretations.media_item_id  -> media_items.id
    #   instrument is exposed as instrument_symbol on signal_interpretations.
    if media_id is None:
        row = await pool.fetch_one(
            """
            SELECT si.media_item_id AS media_id,
                   si.instrument_symbol AS instrument,
                   si.news_item_id
              FROM public.signal_interpretations si
             WHERE si.media_item_id IS NOT NULL
               AND COALESCE(si.llm_direction, '') NOT IN ('', 'unclear')
             ORDER BY si.created_at DESC
             LIMIT 1
            """,
            (),
        )
        if not row:
            return _err(
                "no recent interpretation found to test against. "
                "Wait for at least one media_items row to be interpreted, "
                "or pass an explicit media_id.",
                status=404,
            )
        media_id = row["media_id"]
        instrument = row.get("instrument") or "UNKNOWN"
        news_item_id = row.get("news_item_id")
    else:
        row = await pool.fetch_one(
            """
            SELECT si.instrument_symbol AS instrument, si.news_item_id
              FROM public.signal_interpretations si
              JOIN public.media_items mi ON mi.id = si.media_item_id
             WHERE mi.id = $1
             LIMIT 1
            """,
            (media_id,),
        )
        instrument = (row or {}).get("instrument") or "UNKNOWN"
        news_item_id = (row or {}).get("news_item_id")

    # Pull the local image bytes.
    img_row = await pool.fetch_one(
        "SELECT local_path, mime_type FROM public.media_items WHERE id = $1",
        (media_id,),
    )
    if not img_row or not img_row.get("local_path"):
        return _err(f"media_id={media_id} has no local file on disk", status=404)
    local_path = Path(img_row["local_path"])
    if not local_path.exists():
        return _err(f"media file missing on disk: {local_path}", status=404)
    img_bytes = local_path.read_bytes()
    img_b64 = base64.b64encode(img_bytes).decode("ascii")
    img_mime = img_row.get("mime_type") or "image/png"

    # Pull the same prompt the InterpretationService would use, so this is an
    # apples-to-apples test. We re-use the service's loader to keep
    # cache/version semantics identical, and the payload_store helper for
    # the version hash (matches what gets persisted in real interpretations).
    from shared.intelligence.interpretation_service import _load_prompts as _load_chart_prompts
    from shared.intelligence.payload_store import compute_prompt_version

    raw_prompts = _load_chart_prompts()
    chart_section = raw_prompts.get("chart_analysis", {}) or {}
    system_prompt = chart_section.get("system_prompt") or ""
    user_template = chart_section.get(
        "user_prompt_template",
        "Analyze this chart. Identify the trading instrument from the chart itself.",
    )
    try:
        user_text = user_template.format(
            symbol=instrument or "UNKNOWN",
            context="",
            recall_context="",
        )
    except (KeyError, IndexError):
        user_text = f"Analyze this chart for {instrument or 'UNKNOWN'}."
    prompt_version = compute_prompt_version(raw_prompts)

    # Run the LLM call.
    from shared.intelligence.gateway_config import GatewayConfig, call_vision_llm
    gw = GatewayConfig.for_service("interpretation")

    started = time.monotonic()
    try:
        resp = await call_vision_llm(
            cfg=gw,
            model=model,
            system_prompt=system_prompt,
            user_text=user_text,
            image_b64=img_b64,
            image_mime=img_mime,
            correlation_id=f"settings-test-{int(time.time())}",
            operation="vision_settings_test",
        )
    except Exception as exc:
        logger.exception("settings: test-vision-model failed")
        return _err(f"LLM call failed: {exc}", status=502)
    elapsed_s = time.monotonic() - started

    raw_content = resp.get("content", "")
    parsed: Optional[Dict[str, Any]] = None
    parse_error: Optional[str] = None
    try:
        # Strip markdown fences the model sometimes wraps around JSON.
        cleaned = raw_content.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```", 2)[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
            cleaned = cleaned.rstrip("`").strip()
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, IndexError) as exc:
        parse_error = f"could not parse JSON: {exc}"

    return _json_response({
        "ok": True,
        "model_requested": model,
        "model_resolved": resp.get("model", model),
        "media_id": media_id,
        "instrument": instrument,
        "news_item_id": news_item_id,
        "prompt_version": prompt_version,
        "elapsed_seconds": round(elapsed_s, 2),
        "raw_content": raw_content,
        "parsed": parsed,
        "parse_error": parse_error,
        "usage": resp.get("usage"),
    })


# ---------------------------------------------------------------------------
# GET /api/settings/vision-model-history
# ---------------------------------------------------------------------------
async def handle_history(request: web.Request) -> web.Response:
    """Return the last N audit rows. Default N=50."""
    try:
        limit = int(request.query.get("limit", "50"))
    except ValueError:
        limit = 50
    limit = max(1, min(limit, 500))

    from shared.utils.db import get_shared_pool
    pool = await get_shared_pool()
    rows = await pool.fetch_all(
        """
        SELECT id, slot, model_old, model_new, actor_label, changed_at
          FROM public.model_config_audit
         ORDER BY changed_at DESC
         LIMIT $1
        """,
        (limit,),
    )
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append({
            "id": r["id"],
            "slot": r["slot"],
            "model_old": r.get("model_old"),
            "model_new": r["model_new"],
            "actor_label": r.get("actor_label"),
            "changed_at": r["changed_at"].isoformat() if r.get("changed_at") else None,
        })
    return _json_response({"ok": True, "history": out, "limit": limit})


# ---------------------------------------------------------------------------
# GET /api/settings/dedup  (Round 13.5)
# ---------------------------------------------------------------------------
async def handle_get_dedup(request: web.Request) -> web.Response:
    """Return the full state of every dedup knob.

    Shape::

        {
          "ok": true,
          "knobs": {
            "entry_pct_threshold": {"value": 1.0, "default": 1.0, "source": "default", ...},
            "freshness_hours":     {"value": 4,   "default": 4,   "source": "default", ...},
            "lookback_hours":      {"value": 24,  "default": 24,  "source": "default", ...}
          }
        }
    """
    try:
        from shared.intelligence.dedup_config import get_all
        knobs = await get_all()
    except Exception as exc:
        logger.exception("settings_routes: get_dedup failed")
        return _err(f"failed to read dedup state: {exc}", status=500)
    return _json_response({"ok": True, "knobs": knobs})


# ---------------------------------------------------------------------------
# POST /api/settings/dedup  (Round 13.5)
# ---------------------------------------------------------------------------
async def handle_set_dedup(request: web.Request) -> web.Response:
    """Persist one dedup knob to ``system_config``.

    Body: ``{"key": "<knob>", "value": <number>}``. Bounds are enforced by
    ``dedup_config.set_value`` (entry_pct_threshold ∈ [0.05, 5.0],
    freshness_hours ∈ [0, 72], lookback_hours ∈ [1, 168]).
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _err("body must be JSON")
    if not isinstance(body, dict):
        return _err("body must be a JSON object")

    key = (body.get("key") or "").strip()
    value = body.get("value")
    if not key:
        return _err("key is required")
    if value is None:
        return _err("value is required")

    try:
        from shared.intelligence.dedup_config import set_value, get_all
        result = await set_value(key, value, actor_label=_actor_label(request))
    except ValueError as exc:
        return _err(str(exc), status=400)
    except Exception as exc:
        logger.exception("settings_routes: set_dedup failed")
        return _err(f"failed to persist: {exc}", status=500)

    knobs = await get_all()
    return _json_response({"ok": True, "changed": result, "knobs": knobs})


# ---------------------------------------------------------------------------
# GET /api/settings/sources — tree of sources→channels→users
# ---------------------------------------------------------------------------
async def handle_get_sources(request: web.Request) -> web.Response:
    """Return the full source→channel→user tree for the Settings UI."""
    from shared.utils.db import get_shared_pool
    pool = await get_shared_pool()

    rows = await pool.fetch_all("""
        SELECT tp.id, tp.platform, tp.handle_raw, tp.display_name,
               tp.trader_type, tp.is_tracked, tp.tracked_media_types,
               tp.prompt_id, tp.channel_id, tp.accuracy_score,
               tp.accuracy_samples
        FROM trader_profiles tp
        ORDER BY tp.platform, tp.channel_id NULLS FIRST, tp.display_name
    """)

    sources: Dict[str, Dict] = {}
    for r in rows:
        platform = r["platform"] or "unknown"
        channel_id = r["channel_id"] or "_direct"
        display_name = r["display_name"] or r["handle_raw"] or "unknown"

        if platform not in sources:
            sources[platform] = {"source": platform, "channels": {}}

        src = sources[platform]
        if channel_id not in src["channels"]:
            # Derive a friendly channel name
            if "1755624949" in str(channel_id):
                ch_name = "Rose ⚡"
            elif channel_id == "_direct":
                ch_name = "Direct"
            else:
                ch_name = channel_id
            src["channels"][channel_id] = {
                "channel_id": channel_id,
                "channel_name": ch_name,
                "users": [],
            }

        src["channels"][channel_id]["users"].append({
            "id": r["id"],
            "handle": r["handle_raw"],
            "display_name": display_name,
            "trader_type": r["trader_type"],
            "is_tracked": bool(r["is_tracked"]) if r["is_tracked"] is not None else True,
            "tracked_media_types": r["tracked_media_types"] or "all",
            "prompt_id": r["prompt_id"],
            "accuracy_score": float(r["accuracy_score"]) if r["accuracy_score"] else None,
            "accuracy_samples": r["accuracy_samples"],
        })

    return _json_response({"ok": True, "sources": list(sources.values())})


# ---------------------------------------------------------------------------
# PUT /api/settings/track — update trader tracking/prompt
# ---------------------------------------------------------------------------
async def handle_put_track(request: web.Request) -> web.Response:
    """Update is_tracked, tracked_media_types, or prompt_id for a trader."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _err("body must be JSON")

    trader_id = body.get("id")
    if not trader_id:
        return _err("id (trader_profile id) is required")

    from shared.utils.db import get_shared_pool
    pool = await get_shared_pool()

    updates = []
    params = []
    n = 1
    for field in ("is_tracked", "tracked_media_types", "prompt_id"):
        if field in body:
            val = body[field]
            if field == "tracked_media_types" and val not in ("all", "media", "text", None):
                return _err(f"tracked_media_types must be all/media/text")
            updates.append(f"{field} = ${n}")
            params.append(val)
            n += 1
    if not updates:
        return _err("at least one field to update is required")

    params.append(trader_id)
    sql = f"UPDATE trader_profiles SET {', '.join(updates)} WHERE id = ${n}"
    await pool.execute(sql, tuple(params))
    return _json_response({"ok": True, "updated": trader_id})


# ---------------------------------------------------------------------------
# GET /api/settings/prompts — list prompts
# ---------------------------------------------------------------------------
async def handle_get_prompts(request: web.Request) -> web.Response:
    from shared.utils.db import get_shared_pool
    pool = await get_shared_pool()
    rows = await pool.fetch_all("""
        SELECT config_key, config_value FROM system_config
        WHERE namespace = 'chart_prompts' AND (config_key LIKE '%/prompt' OR config_key = 'default')
        ORDER BY config_key
    """)
    prompts = []
    for r in rows:
        cfg = r["config_value"]
        if isinstance(cfg, str):
            cfg = json.loads(cfg)
        sp = cfg.get("system_prompt", "")
        prompts.append({
            "key": r["config_key"],
            "preview": sp[:150] + "…" if len(sp) > 150 else sp,
        })
    return _json_response({"ok": True, "prompts": prompts})


# ---------------------------------------------------------------------------
# GET /api/settings/prompts/{key}
# ---------------------------------------------------------------------------
async def handle_get_prompt(request: web.Request) -> web.Response:
    key = request.match_info.get("key", "")
    from shared.utils.db import get_shared_pool
    pool = await get_shared_pool()
    row = await pool.fetch_one(
        "SELECT config_value FROM system_config WHERE namespace = 'chart_prompts' AND config_key = $1",
        (key,),
    )
    if not row:
        return _err(f"not found: {key}", status=404)
    cfg = row["config_value"]
    if isinstance(cfg, str):
        cfg = json.loads(cfg)
    return _json_response({
        "ok": True, "key": key,
        "system_prompt": cfg.get("system_prompt", ""),
        "user_prompt_template": cfg.get("user_prompt_template", ""),
    })


# ---------------------------------------------------------------------------
# PUT /api/settings/prompts/{key}
# ---------------------------------------------------------------------------
async def handle_put_prompt(request: web.Request) -> web.Response:
    key = request.match_info.get("key", "")
    if not key:
        return _err("prompt key is required")
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _err("body must be JSON")
    sp = body.get("system_prompt", "")
    ut = body.get("user_prompt_template", "")
    if not sp or not ut:
        return _err("system_prompt and user_prompt_template are required")
    from shared.utils.db import get_shared_pool
    pool = await get_shared_pool()
    cfg = json.dumps({"system_prompt": sp, "user_prompt_template": ut})
    await pool.execute(
        "INSERT INTO system_config (namespace, config_key, config_value, is_secret) "
        "VALUES ('chart_prompts', $1, $2, false) "
        "ON CONFLICT (namespace, config_key) DO UPDATE SET config_value = $2",
        (key, cfg),
    )
    return _json_response({"ok": True, "saved": key})


# ---------------------------------------------------------------------------
# DELETE /api/settings/prompts/{key}
# ---------------------------------------------------------------------------
async def handle_delete_prompt(request: web.Request) -> web.Response:
    key = request.match_info.get("key", "")
    if not key:
        return _err("prompt key is required")
    from shared.utils.db import get_shared_pool
    pool = await get_shared_pool()
    await pool.execute(
        "DELETE FROM system_config WHERE namespace = 'chart_prompts' AND config_key = $1",
        (key,),
    )
    return _json_response({"ok": True, "deleted": key})


# ---------------------------------------------------------------------------
# PUT /api/settings/prompts/{key}/rename
# ---------------------------------------------------------------------------
async def handle_rename_prompt(request: web.Request) -> web.Response:
    key = request.match_info.get("key", "")
    if not key:
        return _err("prompt key is required")
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _err("body must be JSON")
    new_key = (body.get("new_key") or "").strip()
    if not new_key:
        return _err("new_key is required")
    if new_key == key:
        return _json_response({"ok": True, "renamed": key})

    from shared.utils.db import get_shared_pool
    pool = await get_shared_pool()

    # Check target doesn't exist
    existing = await pool.fetch_one(
        "SELECT 1 FROM system_config WHERE namespace = 'chart_prompts' AND config_key = $1",
        (new_key,),
    )
    if existing:
        return _err(f"target key {new_key!r} already exists")

    # Copy then delete
    await pool.execute(
        "INSERT INTO system_config (namespace, config_key, config_value, is_secret) "
        "SELECT namespace, $2, config_value, is_secret FROM system_config "
        "WHERE namespace = 'chart_prompts' AND config_key = $1",
        (key, new_key),
    )
    await pool.execute(
        "DELETE FROM system_config WHERE namespace = 'chart_prompts' AND config_key = $1",
        (key,),
    )
    return _json_response({"ok": True, "renamed": key, "to": new_key})


# ---------------------------------------------------------------------------
# GET /api/settings/prompts/versions — prompt_versions table
# ---------------------------------------------------------------------------
async def handle_get_prompt_versions(request: web.Request) -> web.Response:
    from shared.utils.db import get_shared_pool
    pool = await get_shared_pool()
    rows = await pool.fetch_all("""
        SELECT name, version, prompt_hash, source, times_used, success_rate, created_at
        FROM prompt_versions
        WHERE name = 'chart_analysis'
        ORDER BY created_at DESC
        LIMIT 50
    """)
    versions = []
    for r in rows:
        versions.append({
            "name": r["name"],
            "version": r["version"],
            "prompt_hash": r["prompt_hash"],
            "source": r["source"],
            "times_used": r["times_used"] or 0,
            "success_rate": float(r["success_rate"]) if r["success_rate"] else None,
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        })
    return _json_response({"ok": True, "versions": versions})


# ---------------------------------------------------------------------------
# PUT /api/settings/prompts/versions/save — write to prompt_versions
# ---------------------------------------------------------------------------
async def handle_save_prompt_version(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _err("body must be JSON")

    version = (body.get("version") or "").strip()
    sp = (body.get("system_prompt") or "").strip()
    ut = (body.get("user_prompt_template") or "").strip()
    source = (body.get("source") or "manual").strip()

    if not version or not sp or not ut:
        return _err("version, system_prompt, and user_prompt_template are required")

    import hashlib
    h = hashlib.sha256()
    h.update(sp.encode())
    h.update(b"\x1f")
    h.update(ut.encode())
    h.update(b"\x1f")
    h.update(b"")  # no taxonomy_rule
    prompt_hash = h.hexdigest()[:16]

    from shared.utils.db import get_shared_pool
    pool = await get_shared_pool()
    await pool.execute(
        "INSERT INTO prompt_versions (name, version, prompt_hash, system, body, source, created_by) "
        "VALUES ('chart_analysis', $1, $2, $3, $4, $5, 'dashboard') "
        "ON CONFLICT (name, version) DO UPDATE SET "
        "  prompt_hash = $2, system = $3, body = $4, source = $5",
        (version, prompt_hash, sp, ut, source),
    )
    return _json_response({"ok": True, "saved": version, "hash": prompt_hash})


# ---------------------------------------------------------------------------
# GET /api/settings/prompts/versions/{version} — single prompt
# ---------------------------------------------------------------------------
async def handle_get_prompt_version(request: web.Request) -> web.Response:
    version = request.match_info.get("version", "")
    if not version:
        return _err("version is required")
    from shared.utils.db import get_shared_pool
    pool = await get_shared_pool()
    row = await pool.fetch_one(
        "SELECT system, body, prompt_hash, source, version FROM prompt_versions "
        "WHERE name = 'chart_analysis' AND version = $1",
        (version,),
    )
    if not row:
        return _err(f"version {version!r} not found", status=404)
    return _json_response({
        "ok": True,
        "version": row["version"],
        "system_prompt": row["system"],
        "user_prompt_template": row["body"],
        "prompt_hash": row["prompt_hash"],
        "source": row["source"],
    })


# ---------------------------------------------------------------------------
# DELETE /api/settings/prompts/versions/{version}
# ---------------------------------------------------------------------------
async def handle_delete_prompt_version(request: web.Request) -> web.Response:
    version = request.match_info.get("version", "")
    if not version:
        return _err("version is required")
    from shared.utils.db import get_shared_pool
    pool = await get_shared_pool()
    await pool.execute(
        "DELETE FROM prompt_versions WHERE name = 'chart_analysis' AND version = $1",
        (version,),
    )
    return _json_response({"ok": True, "deleted": version})


# ---------------------------------------------------------------------------
# PUT /api/settings/prompts/versions/rename
# ---------------------------------------------------------------------------
async def handle_rename_prompt_version(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _err("body must be JSON")
    old_ver = (body.get("old_version") or "").strip()
    new_ver = (body.get("new_version") or "").strip()
    if not old_ver or not new_ver:
        return _err("old_version and new_version are required")
    if old_ver == new_ver:
        return _json_response({"ok": True, "from": old_ver, "to": new_ver})

    from shared.utils.db import get_shared_pool
    pool = await get_shared_pool()

    existing = await pool.fetch_one(
        "SELECT 1 FROM prompt_versions WHERE name = 'chart_analysis' AND version = $1",
        (new_ver,),
    )
    if existing:
        return _err(f"version {new_ver!r} already exists")

    await pool.execute(
        "UPDATE prompt_versions SET version = $2 "
        "WHERE name = 'chart_analysis' AND version = $1",
        (old_ver, new_ver),
    )
    return _json_response({"ok": True, "from": old_ver, "to": new_ver})


# ---------------------------------------------------------------------------
# Mount
# ---------------------------------------------------------------------------
def attach_routes(app: web.Application, *, prefix: str = "") -> None:
    """Wire the /api/settings/* endpoints into ``app``."""
    app.router.add_get(f"{prefix}/api/settings/vision-models", handle_list_vision_models)
    app.router.add_post(f"{prefix}/api/settings/vision-model", handle_set_vision_model)
    app.router.add_post(f"{prefix}/api/settings/test-vision-model", handle_test_vision_model)
    app.router.add_get(f"{prefix}/api/settings/vision-model-history", handle_history)
    # Round 13.5 — dedup knobs
    app.router.add_get(f"{prefix}/api/settings/dedup", handle_get_dedup)
    app.router.add_post(f"{prefix}/api/settings/dedup", handle_set_dedup)
    # Prompt library + source tracking
    app.router.add_get(f"{prefix}/api/settings/sources", handle_get_sources)
    app.router.add_put(f"{prefix}/api/settings/track", handle_put_track)
    app.router.add_get(f"{prefix}/api/settings/prompts/versions", handle_get_prompt_versions)
    app.router.add_get(f"{prefix}/api/settings/prompts/versions/{{version}}", handle_get_prompt_version)
    app.router.add_put(f"{prefix}/api/settings/prompts/versions/save", handle_save_prompt_version)
    app.router.add_delete(f"{prefix}/api/settings/prompts/versions/{{version}}", handle_delete_prompt_version)
    app.router.add_put(f"{prefix}/api/settings/prompts/versions/rename", handle_rename_prompt_version)
    app.router.add_get(f"{prefix}/api/settings/prompts", handle_get_prompts)
    app.router.add_get(f"{prefix}/api/settings/prompts/{{key}}", handle_get_prompt)
    app.router.add_put(f"{prefix}/api/settings/prompts/{{key}}", handle_put_prompt)
    app.router.add_delete(f"{prefix}/api/settings/prompts/{{key}}", handle_delete_prompt)
    app.router.add_put(f"{prefix}/api/settings/prompts/{{key}}/rename", handle_rename_prompt)
    logger.info(
        "settings_routes: mounted GET/POST/PUT endpoints at %s/api/settings/* "
        "(vision-models + dedup + sources + prompts)",
        prefix or "(root)",
    )
