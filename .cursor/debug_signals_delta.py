"""
Read-only diagnostic for "Signals Watch Δ ENTRY = +0.00%" bug.
Writes NDJSON to /opt/tickles/.cursor/debug-c8d268.log so we can decide which
hypothesis is true WITHOUT editing any dashboard / snapshot code.

What it does (purely observational):
  1) Pulls the live /api/snapshot JSON (no auth on this dashboard).
  2) For every "signal" row returned by the snapshot, logs:
       _source, instrument_symbol, entry_price, current_price, distance_to_entry_pct
     -> proves H1 (interpreted vs pending) and H5 (frontend silent 0).
  3) For each unique symbol with missing/zero delta, runs three DB probes:
        a) public.instruments row + is_active                    -> H2
        b) symbol-form variants tested against public.instruments -> H3
        c) MAX(timestamp) on public.candles for that instrument   -> H4
  4) Tries shared.market_data.live_price.fetch_live_price(symbol, "bybit")
     for each affected symbol with a 4s timeout                   -> H6
  5) Every observation is one NDJSON line in the debug log.

No code in /opt/tickles/shared/** is modified. Safe to re-run.
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
from typing import Any, Dict, List, Optional

LOG_PATH = Path("/opt/tickles/.cursor/debug-c8d268.log")
SESSION_ID = "c8d268"
SNAPSHOT_URL = "http://127.0.0.1:3101/api/snapshot"
DEFAULT_RUN_ID = f"diag-{int(time.time())}"


def log(
    location: str,
    message: str,
    data: Dict[str, Any] | None = None,
    *,
    hypothesis: str = "",
    run_id: str = DEFAULT_RUN_ID,
) -> None:
    payload = {
        "sessionId": SESSION_ID,
        "runId": run_id,
        "hypothesisId": hypothesis,
        "location": location,
        "message": message,
        "data": data or {},
        "timestamp": int(time.time() * 1000),
    }
    try:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, default=str) + "\n")
    except Exception as exc:
        sys.stderr.write(f"[log-failure] {exc}\n")


# endregion


# region agent log
def fetch_snapshot() -> Dict[str, Any]:
    """Return the dashboard's /api/snapshot payload (no auth — Tailscale local)."""
    req = urllib.request.Request(SNAPSHOT_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body)


# endregion


async def main() -> None:
    log("debug_signals_delta:start", "diagnostic starting", {"snapshot_url": SNAPSHOT_URL})

    # ---- 1) Pull live snapshot --------------------------------------------------
    try:
        snap = fetch_snapshot()
    except Exception as exc:
        log(
            "debug_signals_delta:snapshot_fetch",
            "snapshot fetch failed",
            {"err": repr(exc), "traceback": traceback.format_exc()},
        )
        return

    signals: List[Dict[str, Any]] = snap.get("signals") or []
    log(
        "debug_signals_delta:snapshot_ok",
        "snapshot fetched",
        {
            "signal_count": len(signals),
            "positions_count": len(snap.get("positions") or []),
            "open_positions_count": snap.get("open_positions_count"),
        },
    )

    # ---- 2) Per-row inventory: who has a delta, who doesn't ---------------------
    missing_rows: List[Dict[str, Any]] = []
    with_delta: List[Dict[str, Any]] = []
    for s in signals:
        sym = s.get("instrument_symbol")
        entry = s.get("entry_price") or (s.get("levels") or {}).get("entry")
        current = s.get("current_price")
        dist = s.get("distance_to_entry_pct")
        source = s.get("_source") or "interpretation"
        row_data = {
            "id": s.get("signal_interpretation_id") or s.get("id"),
            "_source": source,
            "instrument_symbol": sym,
            "entry_price": entry,
            "current_price": current,
            "distance_to_entry_pct": dist,
            "status": s.get("status"),
            "trader": s.get("trader_display_name") or s.get("trader_handle_raw"),
        }
        # H1+H5: a row is "missing delta" when current_price is null AND
        # distance_to_entry_pct is null/0.
        has_live = current not in (None, 0, "0", "0.0")
        has_dist = dist not in (None, 0, "0", "0.0")
        if has_live or has_dist:
            with_delta.append(row_data)
            log("snapshot:row_has_delta", "row has delta", row_data, hypothesis="H1")
        else:
            missing_rows.append(row_data)
            log("snapshot:row_missing_delta", "row missing delta", row_data, hypothesis="H1")

    log(
        "snapshot:summary",
        "delta breakdown",
        {
            "rows_total": len(signals),
            "rows_with_delta": len(with_delta),
            "rows_missing_delta": len(missing_rows),
            "missing_by_source": _count_by(missing_rows, "_source"),
            "with_delta_by_source": _count_by(with_delta, "_source"),
        },
        hypothesis="H1",
    )

    # Build the set of distinct symbols we need to probe.
    missing_symbols = sorted({r["instrument_symbol"] for r in missing_rows if r["instrument_symbol"]})
    log(
        "snapshot:missing_symbols",
        "unique symbols with no delta",
        {"count": len(missing_symbols), "symbols": missing_symbols},
    )

    # ---- 3) DB probes (H2, H3, H4) ---------------------------------------------
    # We import inside the function so the script also tells us via the log if
    # the db module itself is broken on the VPS.
    try:
        sys.path.insert(0, "/opt/tickles")
        from shared.utils.db import get_shared_pool  # type: ignore
    except Exception as exc:
        log(
            "debug_signals_delta:db_import",
            "shared.utils.db import failed",
            {"err": repr(exc), "traceback": traceback.format_exc()},
        )
        return

    try:
        pool = await get_shared_pool()
    except Exception as exc:
        log(
            "debug_signals_delta:db_pool",
            "get_shared_pool() failed",
            {"err": repr(exc), "traceback": traceback.format_exc()},
        )
        return

    async with pool.acquire() as conn:
        for sym in missing_symbols:
            await _probe_symbol(conn, sym)

    # ---- 4) CCXT live price probe (H6) -----------------------------------------
    try:
        from shared.market_data.live_price import fetch_live_price, LivePriceError, UnsupportedExchangeError  # type: ignore
    except Exception as exc:
        log(
            "debug_signals_delta:ccxt_import",
            "shared.market_data.live_price import failed",
            {"err": repr(exc), "traceback": traceback.format_exc()},
            hypothesis="H6",
        )
        return

    for sym in missing_symbols:
        await _probe_ccxt(sym, fetch_live_price, LivePriceError, UnsupportedExchangeError)

    log("debug_signals_delta:done", "diagnostic complete", {})


def _count_by(rows: List[Dict[str, Any]], field: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for r in rows:
        k = str(r.get(field) or "_none_")
        out[k] = out.get(k, 0) + 1
    return out


async def _probe_symbol(conn, symbol: str) -> None:
    """Run H2 / H3 / H4 probes for one symbol."""
    # H2: exact-match instruments row(s).
    try:
        rows = await conn.fetch(
            "SELECT id, exchange, symbol, is_active, last_candle_ts "
            "FROM public.instruments WHERE symbol = $1",
            symbol,
        )
        log(
            "db:instruments_exact",
            "instruments rows for symbol (exact match)",
            {"symbol": symbol, "rows": [dict(r) for r in rows]},
            hypothesis="H2",
        )
    except Exception as exc:
        # last_candle_ts may not exist on the table — retry without it.
        try:
            rows = await conn.fetch(
                "SELECT id, exchange, symbol, is_active FROM public.instruments WHERE symbol = $1",
                symbol,
            )
            log(
                "db:instruments_exact",
                "instruments rows for symbol (exact match, no last_candle_ts col)",
                {"symbol": symbol, "rows": [dict(r) for r in rows], "warn": repr(exc)},
                hypothesis="H2",
            )
        except Exception as exc2:
            log(
                "db:instruments_exact",
                "instruments lookup failed",
                {"symbol": symbol, "err": repr(exc2)},
                hypothesis="H2",
            )
            rows = []

    # H3: try alt symbol forms.
    variants = _symbol_variants(symbol)
    try:
        alt_rows = await conn.fetch(
            "SELECT symbol, exchange, is_active FROM public.instruments "
            "WHERE symbol = ANY($1::text[])",
            variants,
        )
        log(
            "db:instruments_variants",
            "instruments rows for symbol form variants",
            {"symbol": symbol, "variants": variants, "rows": [dict(r) for r in alt_rows]},
            hypothesis="H3",
        )
    except Exception as exc:
        log(
            "db:instruments_variants",
            "variant lookup failed",
            {"symbol": symbol, "variants": variants, "err": repr(exc)},
            hypothesis="H3",
        )
        alt_rows = []

    # H4: latest 1m candle, joining via the instruments rows we have.
    candidate_ids = [r["id"] for r in rows] + [
        # variant rows don't have ids in our query above; re-fetch.
    ]
    try:
        candle_row = await conn.fetchrow(
            """
            SELECT c.close, c.timestamp, i.symbol, i.exchange
            FROM public.candles c
            JOIN public.instruments i ON i.id = c.instrument_id
            WHERE i.symbol = ANY($1::text[])
              AND c.timeframe::text = '1m'
            ORDER BY c.timestamp DESC
            LIMIT 1
            """,
            variants,
        )
        log(
            "db:latest_1m_candle",
            "latest 1m candle for any variant",
            {
                "symbol": symbol,
                "variants_tested": variants,
                "candle": dict(candle_row) if candle_row else None,
            },
            hypothesis="H4",
        )
    except Exception as exc:
        log(
            "db:latest_1m_candle",
            "candle lookup failed",
            {"symbol": symbol, "err": repr(exc)},
            hypothesis="H4",
        )


def _symbol_variants(symbol: str) -> List[str]:
    """Return common symbol form variants for the strict-equality probe."""
    s = (symbol or "").strip().upper()
    out: List[str] = []

    def _add(v: str) -> None:
        if v and v not in out:
            out.append(v)

    _add(s)
    # Slashed vs unslashed.
    if "/" in s:
        _add(s.replace("/", ""))
    else:
        for quote in ("USDT", "USDC", "USD", "BUSD", "BTC", "ETH"):
            if s.endswith(quote) and len(s) > len(quote):
                _add(s[: -len(quote)] + "/" + quote)
                break
    # Strip .P perp marker.
    if s.endswith(".P"):
        _add(s[:-2])
    # Add perp form.
    if "/" in s and ":" not in s:
        q = s.split("/", 1)[1]
        _add(f"{s}:{q}")
    return out


async def _probe_ccxt(
    symbol: str,
    fetch_live_price,
    LivePriceError,
    UnsupportedExchangeError,
) -> None:
    """H6: can Bybit (via CCXT) actually quote this symbol RIGHT NOW?"""
    try:
        # 4s budget per probe — keep the total diagnostic bounded.
        res = await fetch_live_price(symbol, exchange="bybit", timeout_s=4.0)
        log(
            "ccxt:bybit_live_price",
            "bybit live price OK",
            {
                "input_symbol": symbol,
                "resolved_symbol": res.symbol,
                "price": res.price,
                "ts_ms": res.ts_ms,
            },
            hypothesis="H6",
        )
    except UnsupportedExchangeError as exc:
        log(
            "ccxt:bybit_live_price",
            "unsupported exchange",
            {"symbol": symbol, "err": repr(exc)},
            hypothesis="H6",
        )
    except LivePriceError as exc:
        log(
            "ccxt:bybit_live_price",
            "bybit refused / no usable ticker",
            {"symbol": symbol, "err": repr(exc)},
            hypothesis="H6",
        )
    except Exception as exc:
        log(
            "ccxt:bybit_live_price",
            "ccxt probe raised",
            {"symbol": symbol, "err": repr(exc), "traceback": traceback.format_exc()},
            hypothesis="H6",
        )


if __name__ == "__main__":
    asyncio.run(main())
