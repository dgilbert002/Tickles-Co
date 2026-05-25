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
