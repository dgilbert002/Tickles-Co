"""
shared.data_sufficiency.profiles — built-in sufficiency profiles + loader.

Profiles are persisted in `system_config` under namespace
`sufficiency.profiles` so operators can tune thresholds without a code
deploy. If the table is empty (fresh box) we fall back to these
built-ins.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from shared.data_sufficiency.schema import Profile, Timeframe

log = logging.getLogger("tickles.sufficiency.profiles")

NAMESPACE = "sufficiency.profiles"

# ---------------------------------------------------------------------------
# Built-in library
#
# Six curated profiles that cover every strategy family we know about so far.
# Tuned for 24/7 crypto by default; equities / CFD variants override
# `daily_bar_target` to reflect market-hour density.
# ---------------------------------------------------------------------------

BUILTIN_PROFILES: Dict[str, Profile] = {
    # --- crypto 24/7 ----------------------------------------------------
    "scalp_1m_crypto": Profile(
        name="scalp_1m_crypto",
        timeframe=Timeframe.M1,
        min_bars=20_160,            # 14d x 24h x 60
        min_days=14,
        max_gap_ratio=0.005,        # 0.5%
        max_gap_minutes=5,
        fresh_lag_max_minutes=10,
        daily_bar_target=1440,
        notes="Scalper profile for crypto majors; strict on freshness + tiny gaps.",
    ),
    "swing_15m_crypto": Profile(
        name="swing_15m_crypto",
        timeframe=Timeframe.M15,
        min_bars=17_280,            # 180d x 96 bars
        min_days=180,
        max_gap_ratio=0.01,
        max_gap_minutes=60,
        fresh_lag_max_minutes=60,
        daily_bar_target=96,
        notes="Swing 15m, 6-month lookback, 1% gaps tolerated.",
    ),
    "swing_1h_crypto": Profile(
        name="swing_1h_crypto",
        timeframe=Timeframe.H1,
        min_bars=8_760,             # 1y
        min_days=365,
        max_gap_ratio=0.02,
        max_gap_minutes=240,
        fresh_lag_max_minutes=180,
        daily_bar_target=24,
        notes="Swing 1h, 12-month lookback.",
    ),
    "position_4h_crypto": Profile(
        name="position_4h_crypto",
        timeframe=Timeframe.H4,
        min_bars=4_380,             # 2y
        min_days=730,
        max_gap_ratio=0.02,
        max_gap_minutes=960,
        fresh_lag_max_minutes=720,
        daily_bar_target=6,
        notes="Position 4h, 2y lookback. Daily freshness fine.",
    ),
    "position_1d_crypto": Profile(
        name="position_1d_crypto",
        timeframe=Timeframe.D1,
        min_bars=730,               # ~2y of daily bars
        min_days=730,
        max_gap_ratio=0.03,
        max_gap_minutes=4320,       # 3 days
        fresh_lag_max_minutes=2880,
        daily_bar_target=1,
        notes="Daily position. Multi-year lookback, coarse freshness.",
    ),
    # --- equities / CFD (session-aware via daily_bar_target) ------------
    "swing_15m_equities": Profile(
        name="swing_15m_equities",
        timeframe=Timeframe.M15,
        min_bars=6_300,             # ~180 sessions x ~26 bars
        min_days=252,               # ~1y trading-calendar
        max_gap_ratio=0.02,
        max_gap_minutes=60,
        fresh_lag_max_minutes=60,
        daily_bar_target=26,        # ~6.5h session = 26 bars
        notes="Equities/CFD 15m. Session-aware density target.",
    ),
}


def list_builtin_names() -> List[str]:
    """Return the sorted built-in profile names."""
    return sorted(BUILTIN_PROFILES.keys())


def get_builtin(name: str) -> Optional[Profile]:
    """Look up a built-in by name. Returns None if absent."""
    return BUILTIN_PROFILES.get(name)


async def load_from_db(pool: Any) -> Dict[str, Profile]:
    """Load all operator-overridden profiles from system_config.

    Rows whose JSON fails validation are logged and skipped; the built-in
    of the same name (if any) is then used instead.
    """
    log.debug("load_from_db(pool=%r)", pool)
    rows = await pool.fetch_all(
        "SELECT config_key, config_value FROM system_config WHERE namespace=$1",
        (NAMESPACE,),
    )
    out: Dict[str, Profile] = {}
    for r in rows:
        key = r["config_key"]
        raw = r["config_value"]
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
            # Stored rows may omit the 'name' field; patch from config_key.
            data.setdefault("name", key)
            out[key] = Profile.model_validate(data)
        except Exception as exc:
            log.warning(
                "profile %s in system_config failed validation: %s", key, exc
            )
    return out


async def resolve(pool: Optional[Any], name: str) -> Optional[Profile]:
    """Resolve a profile name against DB overrides first, then built-ins."""
    log.debug("resolve(name=%s)", name)
    if pool is not None:
        overrides = await load_from_db(pool)
        if name in overrides:
            return overrides[name]
    return BUILTIN_PROFILES.get(name)


async def all_profiles(pool: Optional[Any]) -> Dict[str, Profile]:
    """Return the effective profile set — DB overrides on top of built-ins."""
    merged: Dict[str, Profile] = dict(BUILTIN_PROFILES)
    if pool is not None:
        merged.update(await load_from_db(pool))
    return merged
