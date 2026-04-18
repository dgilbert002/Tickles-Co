"""
Data Catalog Service — Tickles & Co V2.0 (hardened 2026-04-17)
==============================================================

REST service that answers "what data / indicators / backtests do we have?".

HARDENING (audit 2026-04-17):
  * All responses go through `_json` helper — consistent encoding of
    datetimes, decimals, and UUIDs. [P1-E4]
  * Query params validated (int bounds, whitelisted `sort`). [P0-E4]
  * Synchronous ClickHouse work wrapped in `loop.run_in_executor` so
    aiohttp's event loop is never blocked. [P0-E5]
  * Single shared `ClickHouseWriter` (created lazily, reused) — no new
    connection per cache miss. Cached via app state. [P2-E4]
  * `on_cleanup` hook closes the shared CH client and shared DB pool.
    [P1-E5]
  * Access log is enabled (level INFO). [P1-E6]
  * TTL cache has a hard size cap to prevent memory runaway from path
    enumeration attacks. [P2-E7]
  * `{symbol}` is clamped to a max length, stripped of control chars
    before use.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from typing import Any, Dict, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_SHARED = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_SHARED)
for p in (_ROOT, _SHARED):
    if p not in sys.path:
        sys.path.insert(0, p)

from aiohttp import web

from shared.utils.db import get_shared_pool

from backtest.indicators import summary as indicator_summary, INDICATORS
from backtest.strategies import list_all as list_strategies
from backtest.accessible import lookup as bt_lookup, top as bt_top, _SORT_ALIAS

log = logging.getLogger("tickles.catalog")


_APP_CH_KEY = "ch_writer"


_ALLOWED_SORT = set(_SORT_ALIAS.keys())
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9._/\-]{1,40}$")
_HASH_ID_RE = re.compile(r"^[A-Za-z0-9\-]{1,64}$")


# ---------------------------------------------------------------------------
# Tiny async cache with a hard size cap
# ---------------------------------------------------------------------------
class _TTLCache:
    def __init__(self, ttl_s: float = 60.0, max_entries: int = 256):
        self.ttl = ttl_s
        self.max_entries = max_entries
        self._store: Dict[str, tuple[float, Any]] = {}

    async def get_or_set(self, key: str, loader):
        now = time.time()
        hit = self._store.get(key)
        if hit and (now - hit[0]) < self.ttl:
            return hit[1]
        val = await loader()
        # Evict oldest if we would exceed the cap.
        if len(self._store) >= self.max_entries:
            oldest = min(self._store.items(), key=lambda kv: kv[1][0])[0]
            self._store.pop(oldest, None)
        self._store[key] = (now, val)
        return val


_cache = _TTLCache(ttl_s=60.0, max_entries=256)


def _json(obj, status: int = 200) -> web.Response:
    """Consistent JSON encoder — handles datetimes, decimals, UUIDs."""
    body = json.dumps(obj, default=str)
    return web.Response(status=status, body=body, content_type="application/json")


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def health(_req):
    return _json({"ok": True, "service": "catalog", "ts": time.time()})


async def exchanges(_req):
    async def load():
        pool = await get_shared_pool()
        rows = await pool.fetch_all(
            "SELECT exchange, COUNT(*) AS n "
            "FROM instruments WHERE is_active=TRUE "
            "GROUP BY exchange ORDER BY n DESC"
        )
        return rows
    return _json(await _cache.get_or_set("exchanges", load))


async def instruments(_req):
    async def load():
        pool = await get_shared_pool()
        rows = await pool.fetch_all(
            "SELECT symbol, exchange, asset_class, base_currency, quote_currency, "
            "max_leverage, spread_pct "
            "FROM instruments WHERE is_active=TRUE ORDER BY exchange, symbol"
        )
        return rows
    return _json(await _cache.get_or_set("instruments", load))


async def instrument_coverage(req):
    raw = req.match_info.get("symbol", "")
    if not _SYMBOL_RE.match(raw):
        return _json({"error": "invalid symbol"}, status=400)
    symbol = raw
    pool = await get_shared_pool()
    rows = await pool.fetch_all(
        """
        SELECT i.symbol, i.exchange, c.timeframe, c.source,
               MIN(c."timestamp") AS first_ts,
               MAX(c."timestamp") AS last_ts,
               COUNT(*)             AS n
        FROM instruments i
        JOIN candles c ON c.instrument_id = i.id
        WHERE i.symbol = $1
        GROUP BY i.symbol, i.exchange, c.timeframe, c.source
        ORDER BY c.timeframe
        """,
        (symbol,),
    )
    if not rows:
        return _json({"symbol": symbol, "coverage": []})
    return _json({"symbol": symbol, "coverage": rows})


async def timeframes(_req):
    async def load():
        pool = await get_shared_pool()
        rows = await pool.fetch_all(
            """
            SELECT timeframe, COUNT(*) AS n,
                   MIN("timestamp") AS first_ts,
                   MAX("timestamp") AS last_ts
            FROM candles GROUP BY timeframe ORDER BY timeframe
            """
        )
        return rows
    return _json(await _cache.get_or_set("timeframes", load))


async def indicators(_req):
    return _json(indicator_summary())


async def strategies(_req):
    return _json({"strategies": list_strategies()})


def _parse_int(raw: Optional[str], default: int, lo: int, hi: int) -> int:
    try:
        v = int(raw) if raw is not None else default
    except Exception:
        v = default
    return max(lo, min(hi, v))


async def backtests_top(req):
    sort = req.query.get("sort", "sharpe")
    if sort not in _ALLOWED_SORT:
        return _json({"error": f"sort must be one of {sorted(_ALLOWED_SORT)}"},
                     status=400)
    n = _parse_int(req.query.get("n"), 20, 1, 500)
    min_trades = _parse_int(req.query.get("min_trades"), 5, 0, 1_000_000)
    sym = req.query.get("symbol")
    if sym and not _SYMBOL_RE.match(sym):
        return _json({"error": "invalid symbol"}, status=400)
    strat = req.query.get("strategy")
    if strat and not re.match(r"^[A-Za-z0-9_]{1,60}$", strat):
        return _json({"error": "invalid strategy"}, status=400)
    loop = asyncio.get_running_loop()
    try:
        # bt_top is sync ClickHouse — off-load to executor.
        result = await loop.run_in_executor(
            None,
            lambda: bt_top(n=n, sort=sort, symbol=sym, strategy=strat,
                           min_trades=min_trades),
        )
        return _json(result)
    except Exception as e:
        # Pass 2 fix: don't leak internal exception detail to the HTTP
        # client. Log server-side; return generic message + 500.
        log.exception("backtests_top failed: %s", e)
        return _json({"error": "internal server error"}, status=500)


async def backtests_lookup(req):
    h = req.match_info.get("hash_or_id", "")
    if not _HASH_ID_RE.match(h):
        return _json({"error": "invalid hash_or_id"}, status=400)
    loop = asyncio.get_running_loop()
    try:
        row = await loop.run_in_executor(None, bt_lookup, h)
    except Exception as e:
        log.exception("backtests_lookup failed: %s", e)
        return _json({"error": "internal server error"}, status=500)
    if not row:
        return _json({"error": "not found"}, status=404)
    return _json(row)


async def _ch_count(app) -> int:
    """Return COUNT(*) on backtest_runs using the shared CH client."""
    ch = app.get(_APP_CH_KEY)
    if ch is None:
        try:
            from backtest.ch_writer import ClickHouseWriter
            ch = ClickHouseWriter()
            app[_APP_CH_KEY] = ch
        except Exception as e:
            log.warning("catalog: ClickHouse unavailable: %s", e)
            return 0
    loop = asyncio.get_running_loop()
    try:
        rows = await loop.run_in_executor(
            None, lambda: ch.client.execute("SELECT count() FROM backtest_runs"))
        return int(rows[0][0]) if rows else 0
    except Exception as e:
        log.warning("catalog: ClickHouse count failed: %s", e)
        return 0


async def stats(req):
    async def load():
        pool = await get_shared_pool()
        instruments_n = await pool.fetch_val("SELECT COUNT(*) FROM instruments")
        candles_n = await pool.fetch_val("SELECT COUNT(*) FROM candles")
        indicators_n = len(INDICATORS)
        strategies_n = len(list_strategies())
        backtests = await _ch_count(req.app)
        return {
            "instruments":  instruments_n,
            "candles":      candles_n,
            "indicators":   indicators_n,
            "strategies":   strategies_n,
            "backtest_runs": backtests,
        }
    return _json(await _cache.get_or_set("stats", load))


# ---------------------------------------------------------------------------
# App construction & lifecycle
# ---------------------------------------------------------------------------
async def _on_cleanup(app: web.Application) -> None:
    log.info("catalog: on_cleanup")
    ch = app.pop(_APP_CH_KEY, None)
    if ch is not None:
        try:
            ch.close()
        except Exception:
            log.debug("catalog: ch.close() failed", exc_info=True)
    try:
        pool = await get_shared_pool()
        close = getattr(pool, "close", None)
        if callable(close):
            maybe = close()
            if asyncio.iscoroutine(maybe):
                await maybe
    except Exception:
        log.debug("catalog: pool close failed", exc_info=True)


def make_app() -> web.Application:
    app = web.Application()
    app.add_routes([
        web.get("/health", health),
        web.get("/exchanges", exchanges),
        web.get("/instruments", instruments),
        web.get("/instruments/{symbol}", instrument_coverage),
        web.get("/timeframes", timeframes),
        web.get("/indicators", indicators),
        web.get("/strategies", strategies),
        web.get("/backtests/top", backtests_top),
        web.get("/backtests/lookup/{hash_or_id}", backtests_lookup),
        web.get("/stats", stats),
    ])
    app.on_cleanup.append(_on_cleanup)
    return app


def main():
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    port = int(os.getenv("CATALOG_PORT", "8765"))
    host = os.getenv("CATALOG_HOST", "127.0.0.1")
    log.info("catalog: listening on %s:%d", host, port)
    # aiohttp's default access logger is fine; route it through our logger.
    web.run_app(make_app(), host=host, port=port,
                access_log=logging.getLogger("tickles.catalog.access"))


if __name__ == "__main__":
    main()
