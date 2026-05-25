"""
Module: dedup_config
Purpose: Centralised settings for the cross-actor pending-position deduplication
         logic in InterpretationService.

Why this exists
---------------
Round 13.5 (2026-05-24): the user noticed the Entry Radar was showing 6+
near-identical XAU long cards within 0.09% entry spread because:

  1. The pre-Phase-8 backlog never had any dedup at all (those rows still
     sit in tracked_positions today as ``status='pending'``).
  2. The Phase-8 dedup that shipped on 2026-05-22 21:41 UTC used a hard-coded
     ±1% / 24h window that was never operator-tunable.
  3. Phase-8 also did "UPDATE the existing row in place", which keeps the
     OLDER row id as the live one. The user wants the NEWER signal to
     replace the older one once the older one ages out, with a clear
     audit trail (status='cancelled', status_reason='deduped:replaced_by:<id>').

Storage strategy
----------------
We re-use the existing ``public.system_config`` table (same one used by
the vision-model settings) under a dedicated ``dedup`` namespace. That
keeps the operator-facing settings UI surface uniform: every operator
knob is one POST /api/settings/<thing> away.

Resolution chain (highest precedence first)
-------------------------------------------
1. ``public.system_config`` row in namespace=``dedup``  (operator-set)
2. Environment variable                                 (sysadmin override)
3. Hard-coded default in this file                      (last resort)

A 60-second in-process cache mirrors `model_config.py` so the dedup hot
path doesn't hammer the DB on every interpretation cycle.

Operator semantics
------------------
* ``entry_pct_threshold``     — float, default 1.0  (i.e. ±1.0% entry tolerance)
* ``freshness_hours``         — int,   default 4    (skip-vs-replace boundary)
* ``lookback_hours``          — int,   default 24   (how far back to look for
                                                     duplicates in the first
                                                     place)

Author: 2026-05-24 (Round 13.5 — duplicate radar cluster fix)
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Storage keys + defaults.
# ---------------------------------------------------------------------------
_NAMESPACE = "dedup"

# (config_key, env_var, default, type, label, description)
_KNOBS: Dict[str, Tuple[str, Any, type, str, str]] = {
    "entry_pct_threshold": (
        "DEDUP_ENTRY_PCT",
        1.0,
        float,
        "Entry tolerance (%)",
        "Two pending positions on the same (symbol, direction) are considered "
        "duplicates if their entry prices are within this percent of each other. "
        "Default 1.0 means ±1%.",
    ),
    "freshness_hours": (
        "DEDUP_FRESHNESS_HOURS",
        4,
        int,
        "Freshness window (h)",
        "If an existing pending duplicate is younger than this many hours, the "
        "new signal is SKIPPED (the older one stays). If the existing one is "
        "older, the new signal REPLACES it (old becomes 'cancelled' with "
        "status_reason='deduped:replaced_by:<new_id>').",
    ),
    "lookback_hours": (
        "DEDUP_LOOKBACK_HOURS",
        24,
        int,
        "Lookback window (h)",
        "How far back the dedup query looks for an existing pending duplicate. "
        "Anything older than this is considered a separate, new setup even if "
        "it has the same symbol/direction/entry.",
    ),
}


# ---------------------------------------------------------------------------
# In-process cache — mirrors model_config.py semantics.
# ---------------------------------------------------------------------------
_CACHE_TTL_S = 60.0
_CACHE: Dict[str, Tuple[float, Any]] = {}


def _cache_get(key: str) -> Optional[Any]:
    hit = _CACHE.get(key)
    if hit is None:
        return None
    expires_at, value = hit
    if expires_at < time.monotonic():
        _CACHE.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: Any) -> None:
    _CACHE[key] = (time.monotonic() + _CACHE_TTL_S, value)


def clear_cache() -> None:
    """Drop every cached value. Called by ``set_value`` after a successful POST."""
    _CACHE.clear()


# ---------------------------------------------------------------------------
# DB read.
# ---------------------------------------------------------------------------
async def _read_db_row(pool, config_key: str) -> Optional[str]:
    """Return the raw string from system_config or None if absent / on error."""
    try:
        row = await pool.fetch_one(
            "SELECT config_value FROM public.system_config "
            "WHERE namespace = $1 AND config_key = $2",
            (_NAMESPACE, config_key),
        )
    except Exception as exc:
        logger.warning(
            "dedup_config: DB read for %s.%s failed (%s); falling back",
            _NAMESPACE, config_key, exc,
        )
        return None
    if not row:
        return None
    return row.get("config_value") if isinstance(row, dict) else row["config_value"]


def _coerce(value: str, dest_type: type, key: str) -> Optional[Any]:
    """Best-effort string -> dest_type. Returns None on parse failure."""
    try:
        if dest_type is bool:
            return str(value).strip().lower() in ("1", "true", "yes", "on")
        return dest_type(value)
    except (TypeError, ValueError) as exc:
        logger.warning(
            "dedup_config: could not parse %r as %s for key %s (%s); ignoring",
            value, dest_type.__name__, key, exc,
        )
        return None


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------
async def get_value(key: str, *, pool=None) -> Any:
    """Resolve one knob through the DB -> env -> default chain.

    Args:
        key: One of the keys in ``_KNOBS`` (e.g. ``'entry_pct_threshold'``).
        pool: Optional shared DB pool. If None, lazily acquired.

    Returns:
        Coerced value of the correct Python type. Never raises — falls back
        to the hard-coded default on every error path so the dedup hot loop
        is never broken by a bad config value.
    """
    if key not in _KNOBS:
        raise KeyError(f"unknown dedup knob: {key!r}")

    cached = _cache_get(key)
    if cached is not None:
        return cached

    env_var, default, dest_type, _label, _desc = _KNOBS[key]

    # Tier 1 — DB
    if pool is None:
        try:
            from shared.utils.db import get_shared_pool
            pool = await get_shared_pool()
        except Exception as exc:
            logger.debug("dedup_config: cannot acquire pool (%s); skipping DB tier", exc)
            pool = None

    if pool is not None:
        raw = await _read_db_row(pool, key)
        if raw is not None:
            coerced = _coerce(raw, dest_type, key)
            if coerced is not None:
                _cache_set(key, coerced)
                return coerced

    # Tier 2 — env
    raw_env = os.environ.get(env_var)
    if raw_env is not None:
        coerced = _coerce(raw_env, dest_type, key)
        if coerced is not None:
            _cache_set(key, coerced)
            return coerced

    # Tier 3 — default
    _cache_set(key, default)
    return default


async def get_all(*, pool=None) -> Dict[str, Dict[str, Any]]:
    """Return the full state of every dedup knob — used by the Settings UI.

    Shape::

        {
          "entry_pct_threshold": {
            "key": "entry_pct_threshold",
            "value": 1.0,
            "default": 1.0,
            "label": "Entry tolerance (%)",
            "description": "...",
            "env_var": "DEDUP_ENTRY_PCT",
            "type": "float",
            "source": "default" | "env" | "db"
          },
          "freshness_hours": {...},
          ...
        }
    """
    if pool is None:
        try:
            from shared.utils.db import get_shared_pool
            pool = await get_shared_pool()
        except Exception:
            pool = None

    out: Dict[str, Dict[str, Any]] = {}
    for key, (env_var, default, dest_type, label, desc) in _KNOBS.items():
        # Determine effective source by replicating the chain manually so we
        # can attribute it accurately. We can't ask `get_value` to also tell
        # us the source without polluting its return type.
        source = "default"
        value = default

        if pool is not None:
            raw_db = await _read_db_row(pool, key)
            if raw_db is not None:
                coerced = _coerce(raw_db, dest_type, key)
                if coerced is not None:
                    value, source = coerced, "db"

        if source == "default":
            raw_env = os.environ.get(env_var)
            if raw_env is not None:
                coerced = _coerce(raw_env, dest_type, key)
                if coerced is not None:
                    value, source = coerced, "env"

        out[key] = {
            "key": key,
            "value": value,
            "default": default,
            "label": label,
            "description": desc,
            "env_var": env_var,
            "type": dest_type.__name__,
            "source": source,
        }
    return out


# Bounds for operator-set values. Soft guard rails — keep operators from
# accidentally setting a 100%/9999h tolerance and silently disabling
# everything.
_BOUNDS: Dict[str, Tuple[float, float]] = {
    "entry_pct_threshold": (0.05, 5.0),     # 0.05% to 5%
    "freshness_hours":     (0,    72),      # up to 3 days
    "lookback_hours":      (1,    168),     # 1h to 1 week
}


async def set_value(
    key: str,
    value: Any,
    *,
    pool=None,
    actor_label: str = "anonymous",
) -> Dict[str, Any]:
    """Persist one knob to ``system_config`` and invalidate the cache.

    Args:
        key: Must be a known knob.
        value: Will be coerced to the knob's declared type.
        pool: Optional pool override.
        actor_label: Stamped into a structured log line for forensic trace.
            (We don't write to model_config_audit because that table is
            scoped to the chart-hacker namespace; if the user later asks
            for a generic audit table we can add one — for now the log
            line + the system_config.updated_at column is sufficient.)

    Returns:
        ``{"ok": True, "key": ..., "old_value": ..., "new_value": ...}``

    Raises:
        ValueError on unknown key, bad type, or out-of-bounds value.
    """
    if key not in _KNOBS:
        raise ValueError(f"unknown dedup knob: {key!r}")

    env_var, default, dest_type, _label, _desc = _KNOBS[key]
    coerced = _coerce(str(value), dest_type, key)
    if coerced is None:
        raise ValueError(f"value {value!r} is not a valid {dest_type.__name__}")

    lo, hi = _BOUNDS[key]
    if not (lo <= coerced <= hi):
        raise ValueError(
            f"{key}={coerced} is outside the allowed range [{lo}, {hi}]"
        )

    if pool is None:
        from shared.utils.db import get_shared_pool
        pool = await get_shared_pool()

    old_raw = await _read_db_row(pool, key)
    old_coerced = _coerce(old_raw, dest_type, key) if old_raw is not None else None

    await pool.execute(
        """
        INSERT INTO public.system_config (namespace, config_key, config_value, is_secret, updated_at)
        VALUES ($1, $2, $3, FALSE, NOW())
        ON CONFLICT (namespace, config_key) DO UPDATE
            SET config_value = EXCLUDED.config_value,
                updated_at   = NOW()
        """,
        (_NAMESPACE, key, str(coerced)),
    )

    clear_cache()

    logger.info(
        "dedup_config: %s changed %s -> %s by %s",
        key, old_coerced, coerced, actor_label,
    )

    return {
        "ok": True,
        "key": key,
        "old_value": old_coerced if old_coerced is not None else default,
        "new_value": coerced,
        "actor": actor_label,
    }
