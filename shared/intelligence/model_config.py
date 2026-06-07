"""Round 10 (2026-05-24) — Runtime-configurable vision model picker.

Why this exists
---------------
Before Round 10, the three model slots used by the InterpretationService
(primary vision LLM, fallback vision LLM, prefilter classifier) were all
read from environment variables once at process startup. Changing a model
required editing the systemd unit file (or the .env file) and restarting
the service — too high a friction for casual A/B testing.

This module gives us:

  * A single source of truth (`get_model(slot)`) the InterpretationService
    asks per interpretation cycle.
  * A 3-tier resolution chain — DB (`system_config`) wins, env var
    overrides the code default, code default last.
  * A 60-second in-process cache so we don't hammer the DB on every call.
  * An allow-list check (against `vision_model_catalogue.json`) so the
    operator can't accidentally pick a non-vision-capable model.
  * Audit-trail writes on every change.

Settings flow:
  * Operator opens Settings tab in the dashboard
  * Picks a model from the curated dropdown
  * `set_model(slot, model)` validates + persists to `system_config`
  * Cache invalidates immediately for that slot
  * Next interpretation cycle (max 60s later) uses the new model
  * No service restart required
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("tickles.intelligence.model_config")


# ---------------------------------------------------------------------------
# Slot definitions — single source of truth.
# Keep this in sync with the dashboard Settings UI and
# `vision_model_catalogue.json`.
# ---------------------------------------------------------------------------
SLOT_PRIMARY = "primary"
SLOT_FALLBACK = "fallback"
SLOT_PREFILTER = "prefilter"
VALID_SLOTS = (SLOT_PRIMARY, SLOT_FALLBACK, SLOT_PREFILTER)

# Storage keys in `public.system_config`. We use a single namespace so all
# three keys share one ACL surface.
_NAMESPACE = "chart_hacker"
_SLOT_KEYS = {
    SLOT_PRIMARY: "model.primary",
    SLOT_FALLBACK: "model.fallback",
    SLOT_PREFILTER: "model.prefilter",
}

# Env-var fallbacks. These match the variables `interpretation_service` has
# always recognised, so existing operators with custom env values keep
# working without touching anything.
_ENV_VARS = {
    SLOT_PRIMARY: "CHART_HACKER_MODEL_PRIMARY",
    SLOT_FALLBACK: "CHART_HACKER_MODEL_FALLBACK",
    SLOT_PREFILTER: "CHART_HACKER_PREFILTER_MODEL",
}

# Code defaults — Round 10 flipped primary to Qwen3-VL-32B (better on charts,
# ~6x cheaper than Claude Sonnet 4). Claude Sonnet 4 retained as fallback so
# any Qwen failure / rate-limit doesn't cost us interpretations. Prefilter
# stays on Gemini Flash 2.5 — it's already cheap and good at the binary task.
_CODE_DEFAULTS = {
    SLOT_PRIMARY: "qwen/qwen3-vl-32b-instruct",
    SLOT_FALLBACK: "anthropic/claude-sonnet-4",
    SLOT_PREFILTER: "google/gemini-2.5-flash",
}

# In-process cache: slot → (value, expires_at_epoch_seconds).
_CACHE: Dict[str, Tuple[str, float]] = {}
_CACHE_TTL_S = float(os.environ.get("MODEL_CONFIG_CACHE_TTL_S", "60"))
_CACHE_LOCK = asyncio.Lock()


# ---------------------------------------------------------------------------
# Curated allow-list — cached at module load.
# ---------------------------------------------------------------------------
_CATALOGUE_PATH = Path(__file__).parent / "vision_model_catalogue.json"
_CATALOGUE_CACHE: Optional[Dict[str, Any]] = None
_CATALOGUE_LOAD_LOCK = asyncio.Lock()


def _load_catalogue_sync() -> Dict[str, Any]:
    """Sync catalogue read — safe at module load time."""
    global _CATALOGUE_CACHE
    if _CATALOGUE_CACHE is not None:
        return _CATALOGUE_CACHE
    if not _CATALOGUE_PATH.exists():
        logger.warning("vision_model_catalogue.json missing at %s; allow-list empty", _CATALOGUE_PATH)
        _CATALOGUE_CACHE = {"models": []}
        return _CATALOGUE_CACHE
    try:
        _CATALOGUE_CACHE = json.loads(_CATALOGUE_PATH.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("vision_model_catalogue.json unreadable: %s", exc)
        _CATALOGUE_CACHE = {"models": []}
    return _CATALOGUE_CACHE


def get_catalogue() -> Dict[str, Any]:
    """Return the curated catalogue dict.

    Shape:
      {
        "models": [
          {
            "id": "qwen/qwen3-vl-32b-instruct",
            "label": "Qwen3-VL 32B Instruct",
            "vendor": "Alibaba",
            "vision": true,
            "input_cost_per_million_tokens_usd": 0.50,
            "output_cost_per_million_tokens_usd": 1.50,
            "notes": "Strong on charts, fast, cheap. Recommended primary.",
            "recommended_for": ["primary"]
          },
          ...
        ]
      }
    """
    return _load_catalogue_sync()


def list_allowed_models() -> List[str]:
    """Flat list of model IDs the operator is allowed to pick."""
    cat = get_catalogue()
    return [m["id"] for m in cat.get("models", []) if m.get("vision") is not False]


def is_allowed(model_id: str) -> bool:
    """Allow-list check."""
    return model_id in list_allowed_models()


# ---------------------------------------------------------------------------
# Resolution: DB row → env var → code default.
# ---------------------------------------------------------------------------
async def _read_db_row(pool, slot: str) -> Optional[str]:
    """Read the system_config row for this slot. Returns None if absent."""
    config_key = _SLOT_KEYS[slot]
    try:
        row = await pool.fetch_one(
            "SELECT config_value FROM public.system_config "
            "WHERE namespace = $1 AND config_key = $2",
            (_NAMESPACE, config_key),
        )
    except Exception as exc:
        logger.warning(
            "model_config: DB read failed for slot=%s key=%s: %s — "
            "falling back to env/default",
            slot, config_key, exc,
        )
        return None
    if not row:
        return None
    val = row.get("config_value") if isinstance(row, dict) else row["config_value"]
    if not val:
        return None
    return str(val).strip() or None


def _read_env(slot: str) -> Optional[str]:
    var = _ENV_VARS[slot]
    raw = os.environ.get(var)
    return raw.strip() if raw and raw.strip() else None


def _code_default(slot: str) -> str:
    return _CODE_DEFAULTS[slot]


async def get_model(slot: str, *, pool=None) -> str:
    """Resolve which model to use for a given slot.

    Lookup order: in-process cache → DB → env var → code default.

    The pool argument is optional; if omitted we lazily import + acquire the
    shared pool. This keeps the function callable from places that don't
    already hold a pool.
    """
    if slot not in VALID_SLOTS:
        raise ValueError(f"unknown model slot: {slot!r} (must be one of {VALID_SLOTS})")

    now = time.monotonic()
    async with _CACHE_LOCK:
        cached = _CACHE.get(slot)
        if cached and cached[1] > now:
            return cached[0]

    # Cache miss or expired — resolve from DB.
    db_val: Optional[str] = None
    if pool is None:
        try:
            from shared.utils.db import get_shared_pool
            pool = await get_shared_pool()
        except Exception as exc:
            logger.warning("model_config: failed to acquire pool: %s", exc)
            pool = None
    if pool is not None:
        db_val = await _read_db_row(pool, slot)

    resolved = db_val or _read_env(slot) or _code_default(slot)

    async with _CACHE_LOCK:
        _CACHE[slot] = (resolved, now + _CACHE_TTL_S)
    return resolved


async def set_model(
    slot: str,
    model: str,
    *,
    pool=None,
    actor_label: Optional[str] = None,
    enforce_allowlist: bool = True,
) -> Dict[str, str]:
    """Persist a model choice to system_config and write an audit row.

    Args:
        slot: one of VALID_SLOTS.
        model: the OpenRouter model id (e.g. "qwen/qwen3-vl-32b-instruct").
        pool: optional shared pool; resolved if None.
        actor_label: free-form string identifying who/what made the change.
            We don't have user auth on this panel, so this is typically
            "dashboard-anon" or the operator's hostname.
        enforce_allowlist: True for the user-facing path (rejects unknown
            models), False only when bootstrapping from a script that knows
            what it's doing.

    Returns:
        {"slot": slot, "model": model, "previous": <previous value>}
    """
    if slot not in VALID_SLOTS:
        raise ValueError(f"unknown model slot: {slot!r}")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be a non-empty string")
    model = model.strip()

    if enforce_allowlist and not is_allowed(model):
        raise ValueError(
            f"model {model!r} is not in the curated vision-capable allow-list. "
            f"Edit shared/intelligence/vision_model_catalogue.json to add it."
        )

    if pool is None:
        from shared.utils.db import get_shared_pool
        pool = await get_shared_pool()

    config_key = _SLOT_KEYS[slot]
    previous = await _read_db_row(pool, slot)

    # Upsert into system_config.
    await pool.execute(
        """
        INSERT INTO public.system_config (namespace, config_key, config_value, is_secret, updated_at)
        VALUES ($1, $2, $3, FALSE, NOW())
        ON CONFLICT (namespace, config_key) DO UPDATE
          SET config_value = EXCLUDED.config_value,
              updated_at   = NOW()
        """,
        (_NAMESPACE, config_key, model),
    )

    # Audit row — best-effort. Doesn't block the change if the audit table
    # is missing (e.g. fresh DB without the migration).
    try:
        await pool.execute(
            """
            INSERT INTO public.model_config_audit
                (slot, model_old, model_new, actor_label, changed_at)
            VALUES ($1, $2, $3, $4, NOW())
            """,
            (slot, previous, model, actor_label or "unknown"),
        )
    except Exception as exc:
        logger.warning("model_config: audit row write failed (non-fatal): %s", exc)

    # Invalidate the cache for this slot so the next get_model() picks up the
    # new value immediately.
    async with _CACHE_LOCK:
        _CACHE.pop(slot, None)

    logger.info(
        "model_config: slot=%s changed %r -> %r by %s",
        slot, previous, model, actor_label or "unknown",
    )
    return {"slot": slot, "model": model, "previous": previous or _read_env(slot) or _code_default(slot)}


async def get_all_slots(*, pool=None) -> Dict[str, Dict[str, Any]]:
    """Return the full state of all 3 slots — used by the Settings UI.

    Each slot reports the resolved value AND where it came from (db / env /
    default), so the dashboard can label the dropdown source.
    """
    if pool is None:
        try:
            from shared.utils.db import get_shared_pool
            pool = await get_shared_pool()
        except Exception:
            pool = None

    out: Dict[str, Dict[str, Any]] = {}
    for slot in VALID_SLOTS:
        db_val: Optional[str] = None
        if pool is not None:
            db_val = await _read_db_row(pool, slot)
        env_val = _read_env(slot)
        default_val = _code_default(slot)
        if db_val:
            resolved, source = db_val, "db"
        elif env_val:
            resolved, source = env_val, "env"
        else:
            resolved, source = default_val, "code_default"
        out[slot] = {
            "slot": slot,
            "model": resolved,
            "source": source,
            "code_default": default_val,
            "env_value": env_val,
            "db_value": db_val,
        }
    return out


def invalidate_cache(slot: Optional[str] = None) -> None:
    """Drop cached value(s). Useful in tests."""
    if slot is None:
        _CACHE.clear()
        return
    _CACHE.pop(slot, None)


# ---------------------------------------------------------------------------
# Sync helpers — used in code paths that aren't async-friendly (e.g. the
# dataclass field defaults). These never touch the DB; they fall through to
# env → default. The async path is canonical.
# ---------------------------------------------------------------------------
def get_model_sync(slot: str) -> str:
    """Return env-or-default for `slot` without touching the DB.

    Used at module import time for dataclass defaults. Real per-cycle
    lookups must use the async `get_model()`.
    """
    if slot not in VALID_SLOTS:
        raise ValueError(f"unknown slot: {slot!r}")
    return _read_env(slot) or _code_default(slot)


# ===========================================================================
# Round 14 (2026-05-29) — Provider-aware, all-services slot registry.
# ---------------------------------------------------------------------------
# Why this exists
# ---------------
# The 3-slot picker above only covered the vision pipeline and only stored a
# *model string* — the *provider* (OpenRouter vs Requesty) was a global env var
# (LLM_GATEWAY_DEFAULT). The operator now wants to choose BOTH provider AND
# model, per slot, from the dashboard — including the text services (postmortem,
# guru, MCP, text-extract, chart-hacker-opinion) and including Requesty routing
# policies like ``policy/tickles-vision``.
#
# This section adds a generalised registry on TOP of the legacy functions, so
# nothing above changes behaviour. Storage:
#
#   * New rows: namespace ``model_slots``, config_key = <slot>, config_value =
#     JSON ``{"provider": "...", "model": "..."}``.
#   * For the 3 vision slots we ALSO mirror the chosen model into the legacy
#     ``chart_hacker/model.<slot>`` row so the existing ``get_model()`` callers
#     (and the Round-10 tests) keep seeing a consistent value during the
#     migration window.
#
# Resolution order (per field):
#   provider: DB JSON → env ``LLM_GATEWAY_<SERVICE>`` → ``LLM_GATEWAY_DEFAULT``
#             → registry default.
#   model:    DB JSON → (vision only) legacy ``chart_hacker/model.<slot>`` →
#             env ``<env_model>`` → registry default.
# ===========================================================================

SLOT_KIND_VISION = "vision"
SLOT_KIND_TEXT = "text"

# Namespace for the new provider-aware rows.
_SLOTS_NS = "model_slots"

# The full registry. ``service`` is the GatewayConfig service name (drives the
# cost-log ``role`` + the per-service env overrides). ``env_gw`` is the legacy
# per-service gateway env var. ``env_model`` is the legacy per-service model env
# var. ``legacy_model_key`` (vision only) bridges to the Round-10 storage.
SLOT_REGISTRY: Dict[str, Dict[str, Any]] = {
    # ---- Vision pipeline (the Round-10 trio) ----
    SLOT_PRIMARY: {
        "kind": SLOT_KIND_VISION,
        "label": "Vision — Primary",
        "service": "interpretation",
        "env_gw": "LLM_GATEWAY_INTERPRETATION",
        "env_model": _ENV_VARS[SLOT_PRIMARY],
        "legacy_model_key": _SLOT_KEYS[SLOT_PRIMARY],
        "def_provider": "openrouter",
        "def_model": _CODE_DEFAULTS[SLOT_PRIMARY],
    },
    SLOT_FALLBACK: {
        "kind": SLOT_KIND_VISION,
        "label": "Vision — Fallback",
        "service": "interpretation",
        "env_gw": "LLM_GATEWAY_INTERPRETATION",
        "env_model": _ENV_VARS[SLOT_FALLBACK],
        "legacy_model_key": _SLOT_KEYS[SLOT_FALLBACK],
        "def_provider": "openrouter",
        "def_model": _CODE_DEFAULTS[SLOT_FALLBACK],
    },
    SLOT_PREFILTER: {
        "kind": SLOT_KIND_VISION,
        "label": "Vision — Prefilter",
        "service": "interpretation",
        "env_gw": "LLM_GATEWAY_PREFILTER",
        "env_model": _ENV_VARS[SLOT_PREFILTER],
        "legacy_model_key": _SLOT_KEYS[SLOT_PREFILTER],
        "def_provider": "openrouter",
        "def_model": _CODE_DEFAULTS[SLOT_PREFILTER],
    },
    # ---- Text services ----
    "postmortem": {
        "kind": SLOT_KIND_TEXT,
        "label": "Postmortem",
        "service": "postmortem",
        "env_gw": "LLM_GATEWAY_POSTMORTEM",
        "env_model": "SIGNAL_POSTMORTEM_MODEL",
        "legacy_model_key": None,
        "def_provider": "openrouter",
        "def_model": "openai/gpt-4o-mini",
    },
    # NOTE (2026-05-29): the 'guru' and 'mcp' slots were intentionally NOT
    # added here. ChartHacker Guru was removed in the earlier surgeon/guru
    # cleanup, and the MCP tools route their LLM calls through the vision slots
    # (intelligence.interpret → InterpretationService) rather than owning a
    # model. Listing dead slots in the picker would mislead the operator, so
    # only slots that a live service actually reads are registered.
    "text_extract": {
        "kind": SLOT_KIND_TEXT,
        "label": "Text Signal Extract",
        "service": "text_extraction",
        "env_gw": "LLM_GATEWAY_TEXT_EXTRACT",
        "env_model": "TEXT_EXTRACTION_MODEL",
        "legacy_model_key": None,
        "def_provider": "openrouter",
        "def_model": "google/gemini-2.0-flash-001",
    },
    "chart_hacker_opinion": {
        "kind": SLOT_KIND_TEXT,
        "label": "ChartHacker Opinion (vision)",
        "service": "chart_hacker_opinion",
        "env_gw": "LLM_GATEWAY_CHART_HACKER_OPINION",
        "env_model": "OPINION_MODEL",
        "legacy_model_key": None,
        # This one reads images too, so the UI should filter it to vision models.
        "def_provider": "openrouter",
        "def_model": "anthropic/claude-sonnet-4",
        "vision_capable": True,
    },
}

ALL_SLOTS = tuple(SLOT_REGISTRY.keys())

# Slot-level cache: slot → ({"provider","model"}, expires_at).
_SLOT_CACHE: Dict[str, Tuple[Dict[str, str], float]] = {}


def _slot_def(slot: str) -> Dict[str, Any]:
    sd = SLOT_REGISTRY.get(slot)
    if sd is None:
        raise ValueError(f"unknown slot: {slot!r} (must be one of {ALL_SLOTS})")
    return sd


def slot_is_vision(slot: str) -> bool:
    """True if the slot consumes images (vision-only model filtering in the UI)."""
    sd = _slot_def(slot)
    return sd["kind"] == SLOT_KIND_VISION or bool(sd.get("vision_capable"))


def list_slots() -> List[Dict[str, Any]]:
    """Static description of every configurable slot (for the Settings UI)."""
    out: List[Dict[str, Any]] = []
    for name, sd in SLOT_REGISTRY.items():
        out.append({
            "slot": name,
            "label": sd["label"],
            "kind": sd["kind"],
            "service": sd["service"],
            "vision": slot_is_vision(name),
            "def_provider": sd["def_provider"],
            "def_model": sd["def_model"],
        })
    return out


def _env_provider(slot: str) -> Optional[str]:
    """Provider from env: LLM_GATEWAY_<SERVICE> → LLM_GATEWAY_DEFAULT."""
    sd = _slot_def(slot)
    raw = os.environ.get(sd["env_gw"]) or os.environ.get("LLM_GATEWAY_DEFAULT")
    if raw and raw.strip():
        return raw.strip().lower()
    return None


def _env_slot_model(slot: str) -> Optional[str]:
    sd = _slot_def(slot)
    raw = os.environ.get(sd["env_model"])
    return raw.strip() if raw and raw.strip() else None


async def _read_slot_db(pool, slot: str) -> Tuple[Optional[str], Optional[str]]:
    """Read the model_slots JSON row → (provider, model). None,None if absent."""
    try:
        row = await pool.fetch_one(
            "SELECT config_value FROM public.system_config "
            "WHERE namespace = $1 AND config_key = $2",
            (_SLOTS_NS, slot),
        )
    except Exception as exc:
        logger.warning("model_config: slot DB read failed for %s: %s", slot, exc)
        return None, None
    if not row:
        return None, None
    val = row.get("config_value") if isinstance(row, dict) else row["config_value"]
    if not val:
        return None, None
    try:
        data = json.loads(val) if isinstance(val, str) else dict(val)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("model_config: slot JSON parse failed for %s: %s", slot, exc)
        return None, None
    prov = (data.get("provider") or "").strip().lower() or None
    mdl = (data.get("model") or "").strip() or None
    return prov, mdl


async def get_slot(slot: str, *, pool=None) -> Dict[str, Any]:
    """Resolve {provider, model} for a slot with full source attribution.

    Returns a dict::

        {"slot","kind","service","vision","provider","model",
         "provider_source","model_source"}
    """
    sd = _slot_def(slot)

    now = time.monotonic()
    async with _CACHE_LOCK:
        cached = _SLOT_CACHE.get(slot)
        if cached and cached[1] > now:
            c = cached[0]
            return {
                "slot": slot, "kind": sd["kind"], "service": sd["service"],
                "vision": slot_is_vision(slot),
                "provider": c["provider"], "model": c["model"],
                "provider_source": c.get("provider_source", "cache"),
                "model_source": c.get("model_source", "cache"),
                "label": sd["label"],
            }

    if pool is None:
        try:
            from shared.utils.db import get_shared_pool
            pool = await get_shared_pool()
        except Exception as exc:
            logger.warning("model_config: slot pool acquire failed: %s", exc)
            pool = None

    db_prov, db_model = (None, None)
    legacy_model: Optional[str] = None
    if pool is not None:
        db_prov, db_model = await _read_slot_db(pool, slot)
        # Vision slots: bridge to the Round-10 legacy row for the model.
        if db_model is None and sd.get("legacy_model_key"):
            try:
                legacy_model = await _read_db_row(pool, slot)
            except Exception:
                legacy_model = None

    # Provider resolution.
    if db_prov:
        provider, provider_source = db_prov, "db"
    elif _env_provider(slot):
        provider, provider_source = _env_provider(slot), "env"
    else:
        provider, provider_source = sd["def_provider"], "code_default"

    # Model resolution.
    if db_model:
        model, model_source = db_model, "db"
    elif legacy_model:
        model, model_source = legacy_model, "legacy_db"
    elif _env_slot_model(slot):
        model, model_source = _env_slot_model(slot), "env"
    else:
        model, model_source = sd["def_model"], "code_default"

    async with _CACHE_LOCK:
        _SLOT_CACHE[slot] = (
            {"provider": provider, "model": model,
             "provider_source": provider_source, "model_source": model_source},
            now + _CACHE_TTL_S,
        )

    return {
        "slot": slot, "kind": sd["kind"], "service": sd["service"],
        "vision": slot_is_vision(slot),
        "provider": provider, "model": model,
        "provider_source": provider_source, "model_source": model_source,
        "label": sd["label"],
    }


async def set_slot(
    slot: str,
    provider: str,
    model: str,
    *,
    pool=None,
    actor_label: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist {provider, model} for a slot. Mirrors model into the legacy row
    for vision slots so old callers stay consistent. Writes an audit row.
    """
    sd = _slot_def(slot)
    provider = (provider or "").strip().lower()
    model = (model or "").strip()
    if provider not in ("openrouter", "requesty"):
        raise ValueError(f"provider must be openrouter|requesty; got {provider!r}")
    if not model:
        raise ValueError("model must be a non-empty string")

    if pool is None:
        from shared.utils.db import get_shared_pool
        pool = await get_shared_pool()

    prev_prov, prev_model = await _read_slot_db(pool, slot)
    payload = json.dumps({"provider": provider, "model": model})

    await pool.execute(
        """
        INSERT INTO public.system_config (namespace, config_key, config_value, is_secret, updated_at)
        VALUES ($1, $2, $3, FALSE, NOW())
        ON CONFLICT (namespace, config_key) DO UPDATE
          SET config_value = EXCLUDED.config_value, updated_at = NOW()
        """,
        (_SLOTS_NS, slot, payload),
    )

    # Vision back-compat mirror: keep chart_hacker/model.<slot> in sync.
    if sd.get("legacy_model_key"):
        await pool.execute(
            """
            INSERT INTO public.system_config (namespace, config_key, config_value, is_secret, updated_at)
            VALUES ($1, $2, $3, FALSE, NOW())
            ON CONFLICT (namespace, config_key) DO UPDATE
              SET config_value = EXCLUDED.config_value, updated_at = NOW()
            """,
            (_NAMESPACE, sd["legacy_model_key"], model),
        )

    # Audit (best-effort).
    try:
        await pool.execute(
            """
            INSERT INTO public.model_config_audit
                (slot, model_old, model_new, actor_label, changed_at)
            VALUES ($1, $2, $3, $4, NOW())
            """,
            (
                slot,
                f"{prev_prov or '?'}::{prev_model or '?'}",
                f"{provider}::{model}",
                actor_label or "unknown",
            ),
        )
    except Exception as exc:
        logger.warning("model_config: slot audit write failed (non-fatal): %s", exc)

    async with _CACHE_LOCK:
        _SLOT_CACHE.pop(slot, None)
        # Also drop the legacy cache entry so get_model() re-reads.
        _CACHE.pop(slot, None)

    logger.info(
        "model_config: slot=%s set provider=%s model=%s by %s",
        slot, provider, model, actor_label or "unknown",
    )
    return {
        "slot": slot, "provider": provider, "model": model,
        "previous": {"provider": prev_prov, "model": prev_model},
    }


async def get_all_slots_v2(*, pool=None) -> Dict[str, Dict[str, Any]]:
    """Resolve every slot (provider + model + sources) — for the Settings UI."""
    if pool is None:
        try:
            from shared.utils.db import get_shared_pool
            pool = await get_shared_pool()
        except Exception:
            pool = None
    out: Dict[str, Dict[str, Any]] = {}
    for name in SLOT_REGISTRY:
        out[name] = await get_slot(name, pool=pool)
    return out


def invalidate_slot_cache(slot: Optional[str] = None) -> None:
    """Drop cached slot value(s)."""
    if slot is None:
        _SLOT_CACHE.clear()
        return
    _SLOT_CACHE.pop(slot, None)
