"""
Module: copy_sizing_config
Purpose: Operator-tunable position-sizing knobs for the copy-trade competition
         agents (the 12 paper agents in copy_trade_monitor.py).

Why this exists
---------------
2026-05-29: the per-agent sizing rules — 5% risk, 3% risk, the 20/33 max-open
caps, the 100x leverage cap, and the 3x spot-leverage multiplier — were
HARDCODED constants inside `copy_trade_monitor._enter_agent_position`. The
operator wanted to tune them (e.g. "make the 5% agents use 4%", "let the 3%
agent run 40 trades") WITHOUT a code change or redeploy. This module lifts them
into `public.system_config` (namespace ``copy_sizing``) using the exact same
DB → env → default resolution chain + 60s cache as `dedup_config.py`, so the
Settings panel can edit them live.

Resolution chain (highest precedence first)
-------------------------------------------
1. ``public.system_config`` row in namespace=``copy_sizing``  (operator-set)
2. Environment variable                                       (sysadmin override)
3. Hard-coded default in this file                            (last resort)

Units
-----
Risk percentages are stored as PERCENT (e.g. ``5.0`` means 5%), matching the
dedup ``entry_pct_threshold`` convention. The monitor divides by 100 when it
multiplies the wallet balance. Defaults reproduce the pre-2026-05-29 behaviour
EXACTLY, so enabling this module changes nothing until an operator edits a value.

Author: 2026-05-29 (Phase 1.5 — agent sizing tunability)
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_NAMESPACE = "copy_sizing"

# (config_key) -> (env_var, default, type, label, description)
_KNOBS: Dict[str, Tuple[str, Any, type, str, str]] = {
    "risk_pct_5": (
        "COPY_RISK_PCT_5", 5.0, float, "Leveraged risk per trade (%)",
        "Margin committed per trade, as a percent of the agent's wallet, for the "
        "standard leveraged agents (B: Lev Parallel, C: +BE Lock, their +Opt "
        "variants, and Rose B/C). Default 5.0 = 5%.",
    ),
    "risk_pct_3": (
        "COPY_RISK_PCT_3", 3.0, float, "3%-agent risk per trade (%)",
        "Margin committed per trade for the D: 3% Lev Par agent. Default 3.0 = 3%.",
    ),
    "max_concurrent_5": (
        "COPY_MAX_CONCURRENT_5", 20, int, "Max open trades (5% agents)",
        "How many positions a 5%-risk leveraged agent may hold at once. The "
        "(N+1)th signal is skipped until a slot frees up. Default 20.",
    ),
    "max_concurrent_3": (
        "COPY_MAX_CONCURRENT_3", 33, int, "Max open trades (3% agent)",
        "How many positions the 3%-risk agent (D) may hold at once. Default 33.",
    ),
    "leverage_cap": (
        "COPY_LEVERAGE_CAP", 100.0, float, "Leverage cap (x)",
        "Upper bound on the leverage derived from a signal's stop-loss distance "
        "for the leveraged agents. Default 100x.",
    ),
    "spot_lev_3x": (
        "COPY_SPOT_LEV_3X", 3.0, float, "Spot-Lev-3x leverage (x)",
        "Fixed leverage for the A×3: Spot Lev 3x agent (full wallet at this "
        "multiplier, one trade at a time). Default 3x.",
    ),
    "demo_be_lock_threshold_pct": (
        "DEMO_BE_LOCK_THRESHOLD_PCT", 5.0, float, "Demo BE-lock profit threshold (%)",
        "When a demo position's unrealised PnL exceeds this percentage of entry, "
        "the bridge moves its stop-loss to breakeven (entry ± offset below).  "
        "Mirrors the paper agents' BE-lock behaviour.  Default 5.0 = 5%.",
    ),
    "demo_be_lock_offset_pct": (
        "DEMO_BE_LOCK_OFFSET_PCT", 0.002, float, "Demo BE-lock stop offset (%)",
        "When the SL is moved to breakeven, it is placed this far PAST entry "
        "(e.g. long entry $100, offset=0.2% → SL=$100.20) so that fees and "
        "slippage don't turn a BE exit into a small loss.  Default 0.2%.",
    ),
}

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


async def _read_db_row(pool, config_key: str) -> Optional[str]:
    try:
        row = await pool.fetch_one(
            "SELECT config_value FROM public.system_config "
            "WHERE namespace = $1 AND config_key = $2",
            (_NAMESPACE, config_key),
        )
    except Exception as exc:
        logger.warning("copy_sizing_config: DB read for %s.%s failed (%s); falling back",
                       _NAMESPACE, config_key, exc)
        return None
    if not row:
        return None
    return row.get("config_value") if isinstance(row, dict) else row["config_value"]


def _coerce(value: str, dest_type: type, key: str) -> Optional[Any]:
    try:
        if dest_type is bool:
            return str(value).strip().lower() in ("1", "true", "yes", "on")
        return dest_type(value)
    except (TypeError, ValueError) as exc:
        logger.warning("copy_sizing_config: could not parse %r as %s for key %s (%s)",
                       value, dest_type.__name__, key, exc)
        return None


async def get_value(key: str, *, pool=None) -> Any:
    """Resolve one knob through DB -> env -> default. Never raises on bad config."""
    if key not in _KNOBS:
        raise KeyError(f"unknown copy_sizing knob: {key!r}")

    cached = _cache_get(key)
    if cached is not None:
        return cached

    env_var, default, dest_type, _label, _desc = _KNOBS[key]

    if pool is None:
        try:
            from shared.utils.db import get_shared_pool
            pool = await get_shared_pool()
        except Exception:
            pool = None

    if pool is not None:
        raw = await _read_db_row(pool, key)
        if raw is not None:
            coerced = _coerce(raw, dest_type, key)
            if coerced is not None:
                _cache_set(key, coerced)
                return coerced

    raw_env = os.environ.get(env_var)
    if raw_env is not None:
        coerced = _coerce(raw_env, dest_type, key)
        if coerced is not None:
            _cache_set(key, coerced)
            return coerced

    _cache_set(key, default)
    return default


async def get_all(*, pool=None) -> Dict[str, Dict[str, Any]]:
    """Return the full state of every sizing knob — used by the Settings UI."""
    if pool is None:
        try:
            from shared.utils.db import get_shared_pool
            pool = await get_shared_pool()
        except Exception:
            pool = None

    out: Dict[str, Dict[str, Any]] = {}
    for key, (env_var, default, dest_type, label, desc) in _KNOBS.items():
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
            "key": key, "value": value, "default": default,
            "label": label, "description": desc, "env_var": env_var,
            "type": dest_type.__name__, "source": source,
        }
    return out


# Soft guard rails so an operator can't accidentally set a 9999% risk or 500x.
_BOUNDS: Dict[str, Tuple[float, float]] = {
    "risk_pct_5":       (0.5, 100.0),
    "risk_pct_3":       (0.5, 100.0),
    "max_concurrent_5": (1,   200),
    "max_concurrent_3": (1,   200),
    "leverage_cap":     (1.0, 125.0),
    "spot_lev_3x":      (1.0, 25.0),
    "demo_be_lock_threshold_pct": (1.0, 50.0),
    "demo_be_lock_offset_pct":    (0.001, 1.0),
}


async def set_value(key: str, value: Any, *, pool=None, actor_label: str = "anonymous") -> Dict[str, Any]:
    """Persist one knob to system_config and invalidate the cache. Raises ValueError on bad input."""
    if key not in _KNOBS:
        raise ValueError(f"unknown copy_sizing knob: {key!r}")

    env_var, default, dest_type, _label, _desc = _KNOBS[key]
    coerced = _coerce(str(value), dest_type, key)
    if coerced is None:
        raise ValueError(f"value {value!r} is not a valid {dest_type.__name__}")

    lo, hi = _BOUNDS[key]
    if not (lo <= coerced <= hi):
        raise ValueError(f"{key}={coerced} is outside the allowed range [{lo}, {hi}]")

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
            SET config_value = EXCLUDED.config_value, updated_at = NOW()
        """,
        (_NAMESPACE, key, str(coerced)),
    )

    clear_cache()
    logger.info("copy_sizing_config: %s changed %s -> %s by %s",
                key, old_coerced, coerced, actor_label)
    return {
        "ok": True, "key": key,
        "old_value": old_coerced if old_coerced is not None else default,
        "new_value": coerced, "actor": actor_label,
    }
