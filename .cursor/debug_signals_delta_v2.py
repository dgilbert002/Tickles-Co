"""
v2 diagnostic — captures the full perp/spot/cross-exchange picture so we can
plan a correct fix WITHOUT editing dashboard code yet.

What it adds on top of v1:
  * Catalog inventory — row counts + sample symbol forms in BOTH
    public.instruments AND public.unified_instruments, broken down by
    exchange. Tells us which table is "real" for the snapshot path and
    whether existing seeders touched it.
  * Active feed inventory — for every distinct symbol that appears in the
    last 30 days of signal_interpretations + tracked_positions, log the
    raw symbol form, the normalised form, exchange, and the trader.
  * Per-symbol catalog probe — for each missing-delta symbol, check
    BOTH tables, AND check by canonical_symbol (unified_instruments).
  * Cross-exchange CCXT probe — for each missing-delta symbol, try Bybit,
    BloFin, Bitget, Binance, OKX in parallel (with hard 4s budget each)
    and log which ones can quote a perp/swap vs spot. Resolves the user's
    requirement: "ALL exchanges, perps preferred but spot OK as fallback."
  * CCXT market type — for each successful quote, log market_type
    ("spot" / "swap" / "future") so we can prove perps are reachable.

Read-only. Writes NDJSON to /opt/tickles/.cursor/debug-c8d268.log.
"""

from __future__ import annotations

# region agent log
import asyncio
import json
import sys
import time
import traceback
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

LOG_PATH = Path("/opt/tickles/.cursor/debug-c8d268.log")
SESSION_ID = "c8d268"
SNAPSHOT_URL = "http://127.0.0.1:3101/api/snapshot"
RUN_ID = f"diag2-{int(time.time())}"
EXCHANGES_TO_PROBE = ("bybit", "blofin", "bitget", "binance", "okx")


def log(location: str, message: str, data: Dict[str, Any] | None = None,
        *, hypothesis: str = "", run_id: str = RUN_ID) -> None:
    payload = {
        "sessionId": SESSION_ID, "runId": run_id, "hypothesisId": hypothesis,
        "location": location, "message": message, "data": data or {},
        "timestamp": int(time.time() * 1000),
    }
    try:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, default=str) + "\n")
    except Exception as exc:
        sys.stderr.write(f"[log-failure] {exc}\n")


# endregion


async def main() -> None:
    log("v2:start", "v2 diagnostic starting", {"snapshot_url": SNAPSHOT_URL,
        "exchanges": list(EXCHANGES_TO_PROBE)})

    # ---- 1) Snapshot -----------------------------------------------------------
    try:
        with urllib.request.urlopen(SNAPSHOT_URL, timeout=10) as resp:
            snap = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log("v2:snapshot_fetch", "snapshot fetch failed",
            {"err": repr(exc), "tb": traceback.format_exc()})
        return

    signals = snap.get("signals") or []
    missing: List[Dict[str, Any]] = []
    for s in signals:
        if s.get("current_price") in (None, 0, "0", "0.0") and \
           s.get("distance_to_entry_pct") in (None, 0, "0", "0.0"):
            missing.append(s)

    miss_symbols = sorted({s.get("instrument_symbol") for s in missing
                           if s.get("instrument_symbol")})
    log("v2:missing_symbols", "unique missing-delta symbols",
        {"count": len(miss_symbols), "symbols": miss_symbols})

    # ---- 2) DB inventory -------------------------------------------------------
    try:
        sys.path.insert(0, "/opt/tickles")
        from shared.utils.db import get_shared_pool  # type: ignore
    except Exception as exc:
        log("v2:db_import", "import failed", {"err": repr(exc)})
        return

    pool = await get_shared_pool()
    async with pool.acquire() as conn:
        await _catalog_inventory(conn)
        await _feed_inventory(conn)
        for sym in miss_symbols:
            await _probe_symbol_db(conn, sym)

    # ---- 3) Cross-exchange CCXT probe -----------------------------------------
    try:
        import ccxt.async_support as ccxt_async  # type: ignore
    except Exception as exc:
        log("v2:ccxt_import", "ccxt not available", {"err": repr(exc)})
        return

    # Limit per exchange — load_markets() is expensive; do it ONCE per exchange.
    markets_by_exchange: Dict[str, Dict[str, Any]] = {}
    for ex in EXCHANGES_TO_PROBE:
        await _load_markets(ccxt_async, ex, markets_by_exchange)

    # For each missing symbol, find the BEST market across exchanges.
    for sym in miss_symbols:
        _resolve_cross_exchange(sym, markets_by_exchange)

    log("v2:done", "diagnostic complete", {})


# ---- helpers ---------------------------------------------------------------
async def _catalog_inventory(conn) -> None:
    """Count + sample symbols in BOTH catalog tables, per exchange."""
    # public.instruments
    try:
        rows = await conn.fetch(
            "SELECT exchange, COUNT(*) AS n, "
            "SUM(CASE WHEN is_active THEN 1 ELSE 0 END) AS active_n "
            "FROM public.instruments GROUP BY exchange ORDER BY n DESC"
        )
        log("db:instruments_table", "row counts per exchange in public.instruments",
            {"rows": [dict(r) for r in rows]}, hypothesis="H7")
    except Exception as exc:
        log("db:instruments_table", "lookup failed", {"err": repr(exc)})

    try:
        rows = await conn.fetch(
            "SELECT exchange, symbol FROM public.instruments "
            "WHERE is_active=TRUE ORDER BY random() LIMIT 30"
        )
        log("db:instruments_samples", "random symbol samples from public.instruments",
            {"rows": [dict(r) for r in rows]}, hypothesis="H7")
    except Exception as exc:
        log("db:instruments_samples", "lookup failed", {"err": repr(exc)})

    # public.unified_instruments
    try:
        rows = await conn.fetch(
            "SELECT exchange, COUNT(*) AS n, "
            "SUM(CASE WHEN is_active THEN 1 ELSE 0 END) AS active_n "
            "FROM public.unified_instruments GROUP BY exchange ORDER BY n DESC"
        )
        log("db:unified_instruments_table",
            "row counts per exchange in public.unified_instruments",
            {"rows": [dict(r) for r in rows]}, hypothesis="H7")
    except Exception as exc:
        log("db:unified_instruments_table", "lookup failed", {"err": repr(exc)})

    try:
        rows = await conn.fetch(
            "SELECT exchange, exchange_symbol, canonical_symbol, asset_type "
            "FROM public.unified_instruments WHERE is_active=TRUE "
            "ORDER BY random() LIMIT 30"
        )
        log("db:unified_samples",
            "random samples from public.unified_instruments",
            {"rows": [dict(r) for r in rows]}, hypothesis="H7")
    except Exception as exc:
        log("db:unified_samples", "lookup failed", {"err": repr(exc)})


async def _feed_inventory(conn) -> None:
    """List every distinct symbol the active feed actually uses (30d)."""
    try:
        rows = await conn.fetch(
            "SELECT instrument_symbol AS raw_sym, "
            "       instrument_symbol_normalised AS norm_sym, "
            "       instrument_exchange AS exch, COUNT(*) AS n "
            "FROM public.tracked_positions "
            "WHERE created_at > now() - interval '30 days' "
            "GROUP BY instrument_symbol, instrument_symbol_normalised, instrument_exchange "
            "ORDER BY n DESC LIMIT 100"
        )
        log("db:tracked_positions_symbols",
            "tracked_positions distinct symbol forms (30d)",
            {"rows": [dict(r) for r in rows]}, hypothesis="H8")
    except Exception as exc:
        log("db:tracked_positions_symbols", "lookup failed", {"err": repr(exc)})

    try:
        rows = await conn.fetch(
            "SELECT instrument_symbol AS raw_sym, "
            "       instrument_exchange AS exch, COUNT(*) AS n "
            "FROM public.signal_interpretations "
            "WHERE created_at > now() - interval '30 days' "
            "GROUP BY instrument_symbol, instrument_exchange "
            "ORDER BY n DESC LIMIT 100"
        )
        log("db:signal_interpretations_symbols",
            "signal_interpretations distinct symbol forms (30d)",
            {"rows": [dict(r) for r in rows]}, hypothesis="H8")
    except Exception as exc:
        log("db:signal_interpretations_symbols", "lookup failed",
            {"err": repr(exc)})


async def _probe_symbol_db(conn, symbol: str) -> None:
    """Look up one symbol in both catalog tables, with multiple match attempts."""
    # 1) public.instruments — exact and stripped-of-slash forms.
    variants = _symbol_variants(symbol)
    try:
        rows = await conn.fetch(
            "SELECT id, exchange, symbol, is_active FROM public.instruments "
            "WHERE symbol = ANY($1::text[])", variants,
        )
        log("db:probe_instruments", "instruments multi-variant lookup",
            {"symbol": symbol, "variants": variants,
             "rows": [dict(r) for r in rows]}, hypothesis="H7")
    except Exception as exc:
        log("db:probe_instruments", "lookup failed",
            {"symbol": symbol, "err": repr(exc)})

    # 2) public.unified_instruments — by canonical_symbol AND exchange_symbol.
    try:
        rows = await conn.fetch(
            "SELECT id, exchange, exchange_symbol, canonical_symbol, asset_type, "
            "is_active FROM public.unified_instruments "
            "WHERE canonical_symbol = ANY($1::text[]) "
            "   OR exchange_symbol = ANY($1::text[])", variants,
        )
        log("db:probe_unified",
            "unified_instruments multi-variant lookup",
            {"symbol": symbol, "variants": variants,
             "rows": [dict(r) for r in rows]}, hypothesis="H7")
    except Exception as exc:
        log("db:probe_unified", "lookup failed",
            {"symbol": symbol, "err": repr(exc)})


def _symbol_variants(symbol: str) -> List[str]:
    s = (symbol or "").strip().upper()
    out: List[str] = []
    def add(v: str) -> None:
        if v and v not in out:
            out.append(v)
    add(s)
    if "/" in s:
        add(s.replace("/", ""))
    else:
        for q in ("USDT", "USDC", "USD", "BUSD", "BTC", "ETH"):
            if s.endswith(q) and len(s) > len(q):
                add(s[:-len(q)] + "/" + q)
                break
    if s.endswith(".P"):
        add(s[:-2])
        if "/" in s[:-2]:
            q = s[:-2].split("/", 1)[1]
            add(f"{s[:-2]}:{q}")
    if "/" in s and ":" not in s:
        q = s.split("/", 1)[1]
        add(f"{s}:{q}")
    return out


async def _load_markets(ccxt_async, exchange: str,
                        markets_by_exchange: Dict[str, Dict[str, Any]]) -> None:
    """Call load_markets() once per exchange. Best-effort, hard 8s timeout."""
    cls = getattr(ccxt_async, exchange, None)
    if cls is None:
        log("ccxt:load_markets", "no ccxt class",
            {"exchange": exchange}, hypothesis="H8")
        return
    client = cls({"enableRateLimit": True})
    try:
        mkts = await asyncio.wait_for(client.load_markets(), timeout=8.0)
        markets_by_exchange[exchange] = mkts
        # Bucket by market type.
        n_spot = sum(1 for m in mkts.values() if (m or {}).get("type") == "spot")
        n_swap = sum(1 for m in mkts.values() if (m or {}).get("type") in ("swap", "future"))
        active_swap = sum(1 for m in mkts.values()
                          if (m or {}).get("type") in ("swap", "future")
                          and (m or {}).get("active"))
        log("ccxt:load_markets", "loaded markets",
            {"exchange": exchange, "total": len(mkts),
             "spot": n_spot, "swap_or_future": n_swap,
             "active_swap_or_future": active_swap},
            hypothesis="H8")
    except Exception as exc:
        log("ccxt:load_markets", "load_markets failed",
            {"exchange": exchange, "err": repr(exc)}, hypothesis="H8")
    finally:
        try:
            await client.close()
        except Exception:
            pass


def _resolve_cross_exchange(symbol: str,
                            markets_by_exchange: Dict[str, Dict[str, Any]]) -> None:
    """For one symbol, find the best match across every loaded exchange.
    Prefer perp/swap markets, fall back to spot."""
    variants = _symbol_variants(symbol)
    found: List[Dict[str, Any]] = []
    for ex, mkts in markets_by_exchange.items():
        if not mkts:
            continue
        for v in variants:
            m = mkts.get(v)
            if m:
                found.append({
                    "exchange": ex,
                    "variant_matched": v,
                    "market_id": m.get("id"),
                    "market_symbol": m.get("symbol"),
                    "type": m.get("type"),
                    "active": m.get("active"),
                    "linear": m.get("linear"),
                    "contract": m.get("contract"),
                    "base": m.get("base"),
                    "quote": m.get("quote"),
                    "settle": m.get("settle"),
                })
                break
        # Also scan markets dict for base/quote match if no variant hit.
        if not any(f["exchange"] == ex for f in found):
            base = symbol.replace(".P", "").split("/", 1)[0].upper()
            quote = symbol.replace(".P", "").split("/", 1)[1].upper() \
                if "/" in symbol else None
            for m in mkts.values():
                if not m:
                    continue
                if (m.get("base") or "").upper() == base and \
                   (not quote or (m.get("quote") or "").upper() == quote):
                    if m.get("active") and m.get("type") in ("swap", "future"):
                        found.append({
                            "exchange": ex,
                            "variant_matched": "(by base/quote scan, perp)",
                            "market_id": m.get("id"),
                            "market_symbol": m.get("symbol"),
                            "type": m.get("type"),
                            "active": m.get("active"),
                            "linear": m.get("linear"),
                            "contract": m.get("contract"),
                            "base": m.get("base"),
                            "quote": m.get("quote"),
                            "settle": m.get("settle"),
                        })
                        break
    log("ccxt:cross_exchange_resolve",
        "cross-exchange market resolution for symbol",
        {"symbol": symbol, "variants": variants, "matches": found,
         "count": len(found),
         "best_perp": next((f for f in found
                           if f["type"] in ("swap", "future")
                           and f["active"]), None)},
        hypothesis="H8")


if __name__ == "__main__":
    asyncio.run(main())
