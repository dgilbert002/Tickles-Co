"""Symbol-level leverage caps aggregated across all connected exchanges.

Paper agents compute leverage from stop-loss distance (lev_after_buffer),
capped only at a global ``leverage_cap`` (default 100).  But real exchanges
enforce per-symbol limits — RENDER caps at 25x, AERO at 20x, etc.  If paper
computes 85x for a symbol the exchange caps at 25x, the demo/live notional
is silently smaller and P&L trajectories diverge.

This module queries ``fetch_markets`` (or CCXT's pre-loaded ``markets`` dict)
from every active exchange account, extracts the per-symbol ``limits.leverage.max``,
and returns the **minimum** across exchanges — the common denominator.

Cached for 5 minutes so it doesn't hammer exchange APIs on every sizing call.
"""

from __future__ import annotations

import asyncio, logging, time
from typing import Dict, Optional

logger = logging.getLogger("tickles.exchange_limits")

_CACHE: Dict[str, float] = {}
_CACHE_TS: float = 0.0
_CACHE_TTL_S: float = 300.0  # 5 min — leverage limits don't change intraday
_LOCK = asyncio.Lock()


def _strip_symbol(sym: str) -> str:
    """Normalise symbol forms for cache lookup: strip :USDT suffix, uppercase."""
    s = sym.upper().replace("/", "").split(":")[0]
    # Rebuild with slash: "BTC/USDT" ← "BTCUSDT"
    if "USDT" in s and "/" not in s:
        s = s.replace("USDT", "/USDT")
    if "/" not in s and "USD" in s:
        s = s.replace("USD", "/USD")
    return s


async def get_max_leverage(symbol: str) -> float:
    """Return the minimum per-symbol max leverage across all configured exchanges.

    Returns 125.0 (CCXT global cap) as a safe fallback when no exchange data
    is available, so the paper engine is never more restrictive than the
    exchange side.
    """
    global _CACHE, _CACHE_TS
    norm = _strip_symbol(symbol)

    # Fast path — cache hit
    if _CACHE_TS > 0 and time.monotonic() - _CACHE_TS < _CACHE_TTL_S:
        return _CACHE.get(norm, 125.0)

    async with _LOCK:
        # Double-check after acquiring lock
        if _CACHE_TS > 0 and time.monotonic() - _CACHE_TS < _CACHE_TTL_S:
            return _CACHE.get(norm, 125.0)

        await _refresh_cache()
        return _CACHE.get(norm, 125.0)


async def _refresh_cache() -> None:
    """Rebuild the per-symbol min-leverage map from all active exchange accounts."""
    global _CACHE, _CACHE_TS
    new_cache: Dict[str, float] = {}

    # Gather all active exchange accounts
    try:
        from shared.utils.db import get_shared_pool
        pool = await get_shared_pool()
        rows = await pool.fetch_all(
            "SELECT exchange, account_name FROM public.exchange_accounts "
            "WHERE is_active = TRUE AND account_type IN ('demo', 'live') "
            "ORDER BY exchange"
        )
    except Exception as exc:
        logger.warning("exchange_limits: DB query failed (%s); keeping stale cache", exc)
        _CACHE_TS = time.monotonic()  # don't keep hammering DB
        return

    if not rows:
        logger.debug("exchange_limits: no active exchange accounts found")
        _CACHE_TS = time.monotonic()
        return

    # Build adapters and fetch markets
    try:
        from shared.execution.ccxt_adapter import CcxtExecutionAdapter
        adapter = CcxtExecutionAdapter(demo_trading=True, default_type="swap")
    except Exception as exc:
        logger.warning("exchange_limits: cannot create adapter (%s)", exc)
        _CACHE_TS = time.monotonic()
        return

    for row in rows:
        ex = row["exchange"]
        acct = row["account_name"]
        try:
            client = adapter._get_client(ex, acct)
            if not client.markets:
                await asyncio.to_thread(client.load_markets)
            for sym, market in (client.markets or {}).items():
                norm = _strip_symbol(sym)
                max_lev = market.get("limits", {}).get("leverage", {}).get("max")
                if max_lev is not None:
                    max_lev = float(max_lev)
                    if norm not in new_cache or max_lev < new_cache[norm]:
                        new_cache[norm] = max_lev
            logger.debug("exchange_limits: loaded %d markets from %s/%s",
                         len(new_cache), ex, acct)
        except Exception as exc:
            logger.debug("exchange_limits: skip %s/%s (%s)", ex, acct, exc)

    if new_cache:
        _CACHE = new_cache
        _CACHE_TS = time.monotonic()
        logger.info("exchange_limits: cache refreshed — %d symbols across %d exchanges",
                    len(_CACHE), len(rows))
    else:
        _CACHE_TS = time.monotonic()


def clear_cache() -> None:
    """Drop the cache so the next read forces a refresh (used after account changes)."""
    global _CACHE, _CACHE_TS
    _CACHE.clear()
    _CACHE_TS = 0.0
