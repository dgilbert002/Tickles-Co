"""Read-only market/replay routes for the Tickles dashboard.

These routes deliberately read from Postgres only. They expose candle and
signal-replay data to the browser so the dashboard can reconstruct TradingView-
style call paths without going through CCXT from the browser.

Round 13 (2026-05-24): the entry-radar handler now falls back to a live-price
probe (via :mod:`shared.market_data.live_price`) when the local candle store
has nothing for the symbol. Previously these rows arrived at the dashboard
with ``current_price=None`` and ``distance_to_entry_pct=None``, which the UI
rendered as "TO ENTRY: —" — useless for the operator.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Optional
from urllib.parse import quote

from aiohttp import web

from shared.utils.db import get_shared_pool

logger = logging.getLogger("tickles.dashboard.market_routes")

_SYMBOL_RE = re.compile(r"^[A-Za-z0-9./:_-]{2,40}$")
_ALLOWED_TF = {"1m", "5m", "15m", "30m", "1h", "2h", "3h", "4h", "6h", "8h", "12h", "1d", "3d", "1w"}
_TF_ALIASES = {
    "1M": "1m", "5M": "5m", "15M": "15m", "30M": "30m", "30": "30m",
    "1H": "1h", "2H": "2h", "3H": "3h", "4H": "4h", "6H": "6h", "8H": "8h", "12H": "12h",
    "1D": "1d", "3D": "3d", "1W": "1w", "D": "1d", "W": "1w", "H1": "1h", "H4": "4h",
    "M5": "5m",
}
_TF_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "3h": 10800,
    "4h": 14400, "6h": 21600, "8h": 28800, "12h": 43200, "1d": 86400, "3d": 259200, "1w": 604800,
}
_NATIVE_TF = {"1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"}
# Aggregates built from stored 1m candles (user preference) or native base where noted.
_AGGREGATE_TF = {
    "2h": ("1m", 120),
    "3h": ("1m", 180),
    "6h": ("1m", 360),
    "8h": ("4h", 2),
    "12h": ("1m", 720),
    "3d": ("1d", 3),
}


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _json(payload: Any, status: int = 200) -> web.Response:
    return web.Response(
        text=json.dumps(payload, default=_json_default),
        status=status,
        content_type="application/json",
    )


def _err(status: int, message: str) -> web.Response:
    return _json({"ok": False, "error": message}, status=status)


def _parse_dt(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        s = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _clean_symbol(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    s = raw.strip().upper()
    if not _SYMBOL_RE.match(s):
        return None
    # Dashboard signals often include perpetual suffixes. Instruments table
    # normally stores BTC/USDT style spot/perp-normalized symbols.
    return s.replace(".P", "")


def _resolve_candle_symbol(raw: Optional[str], fallback: Optional[str]) -> str:
    """Map chart axis labels (e.g. TradingView ``BTCUSD``) to stored instruments."""
    fb = _clean_symbol(fallback) or "BTC/USDT"
    if not raw:
        return fb
    try:
        from shared.utils.instrument_normaliser import to_canonical_symbol
        canon = to_canonical_symbol(str(raw).strip()) or _clean_symbol(raw) or fb
    except Exception:
        canon = _clean_symbol(raw) or fb
    # USDT-margined crypto charts often label the axis BTCUSD; we store BTC/USDT.
    _USD_TO_USDT = {
        "BTC/USD": "BTC/USDT",
        "ETH/USD": "ETH/USDT",
        "SOL/USD": "SOL/USDT",
        "XRP/USD": "XRP/USDT",
        "DOGE/USD": "DOGE/USDT",
    }
    if canon in _USD_TO_USDT:
        return _USD_TO_USDT[canon]
    cleaned = _clean_symbol(canon) or fb
    if cleaned in ("BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD"):
        base = cleaned[:-3]
        return f"{base}/USDT"
    return cleaned if cleaned else fb




def _normalise_timeframe(raw: Optional[str]) -> str:
    if not raw:
        return "1m"
    s = str(raw).strip()
    if not s:
        return "1m"
    if s in _ALLOWED_TF:
        return s
    upper = s.upper()
    if upper in _TF_ALIASES:
        return _TF_ALIASES[upper]
    lower = s.lower()
    return lower if lower in _ALLOWED_TF else "1m"


def _timeframe_window(call_ts: datetime, end_ts: datetime, timeframe: str) -> tuple[datetime, datetime, int]:
    # Mimic the trader's chart: enough candles before the call to see pattern,
    # and enough after the call to watch entry/SL/TP development. High timeframe
    # calls get weeks/months; low timeframe calls stay compact.
    sec = _TF_SECONDS.get(timeframe, 60)
    pre_bars = 140
    post_bars = {"1m":360,"5m":288,"15m":240,"30m":220,"1h":200,"2h":180,"3h":160,"4h":160,"6h":120,"8h":140,"12h":90,"1d":120,"3d":80,"1w":60}.get(timeframe, 240)
    limit = min(900, pre_bars + post_bars + 50)
    start = call_ts - timedelta(seconds=sec * pre_bars)
    desired_end = call_ts + timedelta(seconds=sec * post_bars)
    # If a trade is still live, include up to now, but cap huge windows by limit.
    end = max(end_ts, desired_end)
    now = datetime.now(timezone.utc)
    if end > now:
        end = now
    if end <= start:
        end = call_ts + timedelta(seconds=sec * post_bars)
    # Anti-DESC-LIMIT-slide guard: the candle SQL uses ORDER BY timestamp DESC
    # LIMIT $6, so when the (start, end) span exceeds what `limit` candles can
    # hold, the query silently drops the OLDEST candles — including the call
    # itself for long-running open/pending trades (e.g. trade opened yesterday,
    # end=now is 24h later, limit=550 1m candles only covers ~9h ending at
    # now). Clamp the visible window so the trader's call is guaranteed to be
    # inside, and — for closed trades — try to keep the close on-screen too.
    limit_seconds = sec * limit
    span = (end - start).total_seconds()
    if span > limit_seconds:
        # Detect a "real" close (end_ts is in the past), vs. live where end_ts
        # was passed as `now` — a live trade has end_ts ≈ now within ~60s.
        has_close = bool(end_ts) and end_ts <= now + timedelta(seconds=60) and (now - end_ts).total_seconds() > 60
        if has_close:
            # Closed trade — try to keep BOTH call_ts and closed_at visible by
            # compressing the pre-call buffer. If the gap between call and
            # close already exceeds `limit`, fall back to keeping call_ts
            # visible (drawer header anchor); user can switch TF to see close.
            close_ts = end_ts
            gap = (close_ts - call_ts).total_seconds()
            if gap < limit_seconds:
                buffer = max(0.0, (limit_seconds - gap) / 2.0)
                start = call_ts - timedelta(seconds=buffer)
                end = close_ts + timedelta(seconds=buffer)
            else:
                # Span > limit: keep call_ts visible (drawer-header anchor)
                end = start + timedelta(seconds=limit_seconds)
        else:
            # Live/pending trade — keep call_ts visible (Bug 11 original fix)
            end = start + timedelta(seconds=limit_seconds)
    return start, end, limit

def _media_url(media_id: Any, local_path: Any, source_url: Any) -> Optional[str]:
    """Build a relative URL (no leading slash) so the browser resolves it
    against the dashboard's <base href> (e.g. /dashboard/). An absolute path
    like '/api/media/5695' would otherwise resolve against the origin root
    and hit the wrong service when the dashboard is reverse-proxied under
    a sub-path (e.g. https://host/dashboard/)."""
    if media_id and local_path:
        return f"api/media/{int(media_id)}"
    if source_url:
        return f"api/media/proxy?url={quote(str(source_url), safe='')}"
    return None


async def _fetch_native_candles(
    *,
    symbol: str,
    exchange: str,
    timeframe: str,
    start: Optional[datetime],
    end: Optional[datetime],
    limit: int,
) -> list[dict[str, Any]]:
    pool = await get_shared_pool()
    rows = await pool.fetch_all(
        """
        WITH target_instruments AS (
            SELECT id, symbol, exchange
            FROM public.instruments
            WHERE is_active = TRUE
              AND lower(exchange) = lower($2)
              AND (
                    upper(symbol) = upper($1)
                 OR  upper(replace(symbol, '/', '')) = upper(replace($1, '/', ''))
                 OR  upper(symbol) = upper(replace($1, '.P', ''))
              )
            ORDER BY CASE WHEN upper(symbol)=upper($1) THEN 0 ELSE 1 END, id
            LIMIT 5
        )
        SELECT c.timestamp, c.open, c.high, c.low, c.close, c.volume,
               c.is_fake, ti.symbol, ti.exchange
        FROM public.candles c
        JOIN target_instruments ti ON ti.id = c.instrument_id
        WHERE lower(c.timeframe::text) = lower($3)
          AND ($4::timestamptz IS NULL OR c.timestamp >= $4)
          AND ($5::timestamptz IS NULL OR c.timestamp <= $5)
        ORDER BY c.timestamp DESC
        LIMIT $6
        """,
        (symbol, exchange, timeframe, start, end, limit),
    )
    
    if not rows:
        # Fallback: Fetch historical candle data on-the-fly from the exchange via CCXT,
        # write them to our local candles DB cache, and return them.
        try:
            inst = await pool.fetch_one(
                """
                SELECT id, symbol, exchange
                FROM public.instruments
                WHERE is_active = TRUE
                  AND lower(exchange) = lower($2)
                  AND (
                        upper(symbol) = upper($1)
                     OR  upper(replace(symbol, '/', '')) = upper(replace($1, '/', ''))
                     OR  upper(symbol) = upper(replace($1, '.P', ''))
                  )
                ORDER BY CASE WHEN upper(symbol)=upper($1) THEN 0 ELSE 1 END, id
                LIMIT 1
                """,
                (symbol, exchange),
            )
            if inst:
                inst_id = inst["id"]
                inst_symbol = inst["symbol"]
                inst_exchange = inst["exchange"]
                
                from shared.connectors.ccxt_adapter import CCXTAdapter
                from shared.market_data.live_price import _candidate_symbols
                
                exchange_id = exchange.lower()
                adapter = CCXTAdapter(exchange_id)
                try:
                    candidates = _candidate_symbols(symbol)
                    ccxt_candles = []
                    last_err = None
                    
                    start_utc = start
                    if start_utc and start_utc.tzinfo is None:
                        start_utc = start_utc.replace(tzinfo=timezone.utc)
                        
                    for cand_sym in candidates:
                        try:
                            ccxt_candles = await adapter.fetch_ohlcv(
                                symbol=cand_sym,
                                timeframe=timeframe,
                                since=start_utc,
                                limit=limit or 500,
                            )
                            if ccxt_candles:
                                break
                        except Exception as sym_exc:
                            last_err = sym_exc
                            continue
                            
                    if not ccxt_candles and last_err is not None:
                        raise last_err
                        
                    if ccxt_candles:
                        # Write fetched candles to the DB cache
                        async with pool.acquire() as conn:
                            for c in ccxt_candles:
                                c_ts = c.timestamp
                                if c_ts and c_ts.tzinfo is None:
                                    c_ts = c_ts.replace(tzinfo=timezone.utc)
                                    
                                await conn.execute(
                                    """
                                    INSERT INTO public.candles
                                    (instrument_id, timeframe, timestamp, open, high, low, close, volume)
                                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                                    ON CONFLICT (instrument_id, timeframe, timestamp) DO UPDATE
                                    SET open = EXCLUDED.open, high = EXCLUDED.high,
                                        low = EXCLUDED.low, close = EXCLUDED.close,
                                        volume = EXCLUDED.volume
                                    """,
                                    inst_id, timeframe, c_ts,
                                    Decimal(str(c.open)), Decimal(str(c.high)),
                                    Decimal(str(c.low)), Decimal(str(c.close)),
                                    Decimal(str(c.volume)),
                                )
                        
                        # Re-fetch from DB to ensure identical schema and filtering mapping.
                        #
                        # Bug H6 fix (Bug Hunter 1 §H2): the SELECT used
                        # `$7::text as symbol, $8::text as exchange` but the
                        # bound tuple had 7 params (positions $1..$7). asyncpg
                        # raised "too few parameters" on every CCXT-fallback
                        # path, so the very first replay of any new instrument
                        # blew up with HTTP 500. The placeholders are now
                        # $6 (inst_symbol) and $7 (inst_exchange) to match
                        # the actual bound tuple positions.
                        rows = await pool.fetch_all(
                            """
                            SELECT c.timestamp, c.open, c.high, c.low, c.close, c.volume,
                                   c.is_fake, $6::text as symbol, $7::text as exchange
                            FROM public.candles c
                            WHERE c.instrument_id = $1
                              AND lower(c.timeframe::text) = lower($2)
                              AND ($3::timestamptz IS NULL OR c.timestamp >= $3)
                              AND ($4::timestamptz IS NULL OR c.timestamp <= $4)
                            ORDER BY c.timestamp DESC
                            LIMIT $5
                            """,
                            (inst_id, timeframe, start, end, limit, inst_symbol, inst_exchange),
                        )
                finally:
                    await adapter.close()
        except Exception as exc:
            import logging
            logging.getLogger("tickles.dashboard").warning(
                "Dashboard fallback candles fetch failed for %s: %s", symbol, exc
            )
            
    rows = [dict(r) for r in rows]
    rows.reverse()
    return rows


def _aggregate_candles(rows: list[dict[str, Any]], target_tf: str) -> list[dict[str, Any]]:
    if not rows:
        return []
    sec = _TF_SECONDS[target_tf]
    buckets: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        ts = r["timestamp"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        key = int(ts.timestamp()) // sec * sec
        buckets.setdefault(key, []).append(r)
    out: list[dict[str, Any]] = []
    for key in sorted(buckets):
        group = sorted(buckets[key], key=lambda x: x["timestamp"])
        first, last = group[0], group[-1]
        out.append({
            "timestamp": datetime.fromtimestamp(key, tz=timezone.utc),
            "open": first["open"],
            "high": max(x["high"] for x in group),
            "low": min(x["low"] for x in group),
            "close": last["close"],
            "volume": sum((x.get("volume") or 0) for x in group),
            "is_fake": any(bool(x.get("is_fake")) for x in group),
            "symbol": last.get("symbol"),
            "exchange": last.get("exchange"),
            "aggregated": True,
        })
    return out


async def _fetch_candles(
    *,
    symbol: str,
    exchange: str,
    timeframe: str,
    start: Optional[datetime],
    end: Optional[datetime],
    limit: int,
) -> list[dict[str, Any]]:
    if timeframe in _NATIVE_TF:
        return await _fetch_native_candles(
            symbol=symbol, exchange=exchange, timeframe=timeframe,
            start=start, end=end, limit=limit,
        )
    if timeframe in _AGGREGATE_TF:
        base_tf, ratio = _AGGREGATE_TF[timeframe]
        base_sec = _TF_SECONDS.get(base_tf, 60)
        # DESC LIMIT on 1m in a wide window returns only the newest bars — missing
        # the call anchor on high-TF replays. Clamp the SQL window to what we can fetch.
        if start is not None and end is not None:
            max_span = timedelta(seconds=float(limit) * float(ratio) * float(base_sec))
            if end - start > max_span:
                start = end - max_span
        base = await _fetch_native_candles(
            symbol=symbol, exchange=exchange, timeframe=base_tf,
            start=start, end=end, limit=min(limit * ratio + ratio * 4, 5000),
        )
        agg = _aggregate_candles(base, timeframe)
        return agg[-limit:]
    return await _fetch_native_candles(
        symbol=symbol, exchange=exchange, timeframe="1m",
        start=start, end=end, limit=limit,
    )


async def handle_candles(request: web.Request) -> web.Response:
    """GET /api/candles?symbol=BTC/USDT&timeframe=1m&limit=500&start=...&end=...

    Bug H9 fix (Bug Hunter 2 §11.2 + Code Analyzer 2 §2.2):
        Previously when both `start` and `end` were provided, the underlying
        `_fetch_candles` query ran `ORDER BY DESC LIMIT n` over the whole
        window. If the window contained more than `limit` candles, the
        OLDEST ones (closest to `start`) silently disappeared — same DESC+
        LIMIT slide as Bug 11. We now clamp `end` to
        `start + limit × tf_seconds` so the response always anchors at
        `start` and the client gets a contiguous window.
    """
    symbol = _clean_symbol(request.query.get("symbol"))
    if not symbol:
        return _err(400, "symbol is required")
    timeframe = _normalise_timeframe(request.query.get("timeframe", "1m"))
    if timeframe not in _ALLOWED_TF:
        return _err(400, "unsupported timeframe")
    exchange = (request.query.get("exchange") or "bybit").lower()
    try:
        limit = max(10, min(int(request.query.get("limit", "500")), 1500))
    except ValueError:
        limit = 500
    start = _parse_dt(request.query.get("start"))
    end = _parse_dt(request.query.get("end"))

    # Bug H9 — DESC+LIMIT slide guard. Only applies when BOTH bounds are set
    # AND the span is wider than the limit budget.
    if start is not None and end is not None:
        sec = _TF_SECONDS.get(timeframe, 60)
        max_seconds = float(limit) * float(sec)
        span = (end - start).total_seconds()
        if span > max_seconds:
            from datetime import timedelta as _td
            end = start + _td(seconds=max_seconds)

    rows = await _fetch_candles(
        symbol=symbol,
        exchange=exchange,
        timeframe=timeframe,
        start=start,
        end=end,
        limit=limit,
    )
    return _json({"ok": True, "symbol": symbol, "exchange": exchange, "timeframe": timeframe, "count": len(rows), "candles": rows})


def _jsonb_list(val: Any) -> list:
    if val is None:
        return []
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except Exception:
            return []
    return val if isinstance(val, list) else []


def _levels_from_trade(trade: dict[str, Any]) -> dict[str, Any]:
    return {
        "entry": trade.get("entry"),
        "stop_loss": trade.get("stop_loss"),
        "take_profit_1": trade.get("tp1") or trade.get("take_profit"),
        "take_profit_2": trade.get("tp2"),
        "take_profit_3": trade.get("tp3"),
    }


def _levels_from_position(pos: dict[str, Any]) -> dict[str, Any]:
    return {
        "entry": pos.get("entry_price"),
        "stop_loss": pos.get("stop_loss"),
        "take_profit_1": pos.get("take_profit_1"),
        "take_profit_2": pos.get("take_profit_2"),
        "take_profit_3": pos.get("take_profit_3"),
    }


def _entry_close(a: Any, b: Any, tol: float = 0.003) -> bool:
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return False
    if fa <= 0 or fb <= 0:
        return False
    return abs(fa - fb) / max(fa, fb) <= tol


def _trade_matches_position(trade: dict[str, Any], pos: dict[str, Any], source: str) -> bool:
    if str(pos.get("signal_source") or "") != source:
        return False
    if str(trade.get("direction") or "").lower() != str(pos.get("direction") or "").lower():
        return False
    return _entry_close(trade.get("entry"), pos.get("entry_price"))


async def handle_signal_replay(request: web.Request) -> web.Response:
    """GET /api/signal-replay?id=<signal_interpretation_id>&leg=<n>&position_id=<id>

    Returns interpretation + all legs (armed + unarmed) with per-leg candles at
    the chart's detected timeframe (aggregated from 1m when needed).
    """
    try:
        interp_id = int(request.query.get("id", "0"))
    except ValueError:
        interp_id = 0
    if interp_id <= 0:
        return _err(400, "id is required")

    leg_idx = request.query.get("leg")
    position_q = request.query.get("position_id")

    pool = await get_shared_pool()
    row = await pool.fetch_one(
        """
        SELECT
          si.id, si.news_item_id, si.media_item_id, si.trader_profile_id,
          si.instrument_symbol, si.instrument_exchange, si.exchange,
          si.consensus_direction, si.consensus_confidence, si.consensus_method,
          si.llm_direction, si.llm_confidence, si.llm_reasoning, si.llm_levels,
          si.quant_direction, si.quant_confidence, si.quant_indicators,
          si.trader_stated_thesis, si.llm_inferred_thesis, si.reason_agreement_score,
          si.ai_agreement_score, si.ai_comment, si.chart_analysis,
          si.pattern_tags, si.setup_tags, si.regime_tags, si.session_tags,
          si.entry_price, si.stop_loss, si.take_profit_1, si.take_profit_2,
          si.take_profit_3, si.take_profit_4, si.take_profit_5, si.take_profit_6,
          si.created_at, si.timeframe, si.model_version, si.prompt_version,
          si.trader_trades, si.chart_hacker_trades,
          ni.headline AS news_headline, ni.content AS news_content, ni.source AS news_source,
          ni.author AS news_author, ni.channel_name AS news_channel_name,
          ni.published_at AS news_published_at, ni.collected_at AS news_collected_at,
          mi.id AS media_id, mi.local_path AS media_local_path, mi.source_url AS media_source_url,
          mi.thumbnail_path AS media_thumbnail_path, mi.media_type AS media_type, mi.mime_type AS mime_type,
          tr.handle_raw, tr.handle_normalized, tr.display_name, tr.platform, tr.trader_type
        FROM public.signal_interpretations si
        LEFT JOIN public.news_items ni ON ni.id = si.news_item_id
        LEFT JOIN public.media_items mi ON mi.id = si.media_item_id
        LEFT JOIN public.trader_profiles tr ON tr.id = si.trader_profile_id
        WHERE si.id = $1
        """,
        (interp_id,),
    )
    if not row:
        return _err(404, "signal not found")

    row = dict(row)
    default_symbol = _clean_symbol(row.get("instrument_symbol")) or "BTC/USDT"
    default_exchange = (row.get("instrument_exchange") or row.get("exchange") or "bybit").lower()
    default_tf_raw = row.get("timeframe")
    call_ts = row.get("news_published_at") or row.get("created_at")
    if call_ts is None:
        call_ts = datetime.now(timezone.utc)
    if call_ts.tzinfo is None:
        call_ts = call_ts.replace(tzinfo=timezone.utc)

    pos_rows = await pool.fetch_all(
        """
        SELECT id, status, direction, entry_price, stop_loss, take_profit_1, take_profit_2,
               take_profit_3, signal_source, signal_timestamp, created_at, closed_at,
               exit_timestamp, current_price, unrealized_pnl_usd, realized_pnl_usd,
               realized_pnl_usd_final, unrealized_pnl_pct, realized_pnl_pct, outcome,
               exit_reason, max_drawdown_pct, max_profit_pct, distance_to_entry_pct,
               distance_to_sl_pct, distance_to_tp1_pct, instrument_symbol, instrument_exchange,
               timeframe, chart_hacker_endorsed, actor_id,
               entry_reason_trader, entry_reason_llm
        FROM public.tracked_positions
        WHERE signal_interpretation_id = $1
        ORDER BY id
        """,
        (interp_id,),
    )
    positions = [dict(p) for p in pos_rows]
    match_positions = [
        p for p in positions if str(p.get("status") or "").lower() not in ("cancelled",)
    ]
    matched_pos_ids: set[int] = set()

    legs: list[dict[str, Any]] = []
    trader_trades = _jsonb_list(row.get("trader_trades"))
    ch_trades = _jsonb_list(row.get("chart_hacker_trades"))

    def _append_leg(
        *,
        leg_key: str,
        source: str,
        trade: dict[str, Any],
        pos: Optional[dict[str, Any]],
        armed: bool,
        skip_reason: Optional[str] = None,
        endorsed: bool = False,
    ) -> None:
        sym = _resolve_candle_symbol(
            trade.get("symbol") or (pos or {}).get("instrument_symbol") or default_symbol,
            default_symbol,
        )
        exch = (pos or {}).get("instrument_exchange") or default_exchange
        tf_raw = trade.get("timeframe") or (pos or {}).get("timeframe") or default_tf_raw
        chart_tf = _normalise_timeframe(tf_raw or "1m")
        levels = _levels_from_position(pos) if pos else _levels_from_trade(trade)
        legs.append({
            "leg_key": leg_key,
            "source": source,
            "armed": armed,
            "skip_reason": skip_reason,
            "chart_hacker_endorsed": bool(endorsed or (pos or {}).get("chart_hacker_endorsed")),
            "direction": str(trade.get("direction") or (pos or {}).get("direction") or "").lower(),
            "symbol": sym,
            "exchange": exch.lower() if exch else default_exchange,
            "timeframe": chart_tf,
            "timeframe_source": tf_raw,
            "levels": levels,
            "confidence": trade.get("confidence"),
            "evidence": trade.get("evidence"),
            "rationale": trade.get("rationale"),
            "position_id": int(pos["id"]) if pos else None,
            "position": {
                "id": pos.get("id"),
                "status": pos.get("status"),
                "signal_source": pos.get("signal_source"),
                "current_price": pos.get("current_price"),
                "opened_at": pos.get("created_at"),
                "closed_at": pos.get("closed_at"),
                "outcome": pos.get("outcome"),
                "exit_reason": pos.get("exit_reason"),
                "unrealized_pnl_usd": pos.get("unrealized_pnl_usd"),
                "realized_pnl_usd": pos.get("realized_pnl_usd"),
                "realized_pnl_usd_final": pos.get("realized_pnl_usd_final"),
                "unrealized_pnl_pct": pos.get("unrealized_pnl_pct"),
                "realized_pnl_pct": pos.get("realized_pnl_pct"),
                "max_drawdown_pct": pos.get("max_drawdown_pct"),
                "max_profit_pct": pos.get("max_profit_pct"),
                "distance_to_entry_pct": pos.get("distance_to_entry_pct"),
                "distance_to_sl_pct": pos.get("distance_to_sl_pct"),
                "distance_to_tp1_pct": pos.get("distance_to_tp1_pct"),
            } if pos else None,
            "candles": [],
            "position_updates": [],
            "coverage": {"candle_count": 0, "update_count": 0},
        })

    for i, trade in enumerate(trader_trades):
        if not isinstance(trade, dict):
            continue
        pos = next(
            (p for p in match_positions if p["id"] not in matched_pos_ids and _trade_matches_position(trade, p, "trader")),
            None,
        )
        if pos:
            matched_pos_ids.add(int(pos["id"]))
        _append_leg(
            leg_key=f"trader-{i}",
            source="trader",
            trade=trade,
            pos=pos,
            armed=pos is not None,
            skip_reason=None if pos else "not_armed",
        )

    for i, trade in enumerate(ch_trades):
        if not isinstance(trade, dict):
            continue
        pos = next(
            (p for p in match_positions if p["id"] not in matched_pos_ids and _trade_matches_position(trade, p, "chart_hacker")),
            None,
        )
        endorsed = False
        if pos is None:
            for p in match_positions:
                if p["id"] not in matched_pos_ids and _trade_matches_position(trade, p, "trader"):
                    pos = p
                    endorsed = True
                    matched_pos_ids.add(int(p["id"]))
                    break
        elif pos:
            matched_pos_ids.add(int(pos["id"]))
        skip = None
        if pos is None:
            skip = "not_armed"
        elif endorsed:
            skip = "chart_hacker_endorsed_trader_leg"
        _append_leg(
            leg_key=f"chart_hacker-{i}",
            source="chart_hacker",
            trade=trade,
            pos=pos,
            armed=pos is not None,
            skip_reason=skip,
            endorsed=endorsed,
        )

    for p in match_positions:
        if int(p["id"]) in matched_pos_ids:
            continue
        _append_leg(
            leg_key=f"position-{p['id']}",
            source=str(p.get("signal_source") or "trader"),
            trade={
                "direction": p.get("direction"),
                "entry": p.get("entry_price"),
                "stop_loss": p.get("stop_loss"),
                "tp1": p.get("take_profit_1"),
                "symbol": p.get("instrument_symbol"),
                "timeframe": p.get("timeframe"),
            },
            pos=p,
            armed=True,
            endorsed=bool(p.get("chart_hacker_endorsed")),
        )
        matched_pos_ids.add(int(p["id"]))

    # Fetch candles + position updates per leg.
    for leg in legs:
        leg_call = call_ts
        pos = leg.get("position") or {}
        if pos.get("opened_at"):
            leg_call = pos["opened_at"]
        elif positions and leg.get("position_id"):
            pr = next((p for p in positions if p["id"] == leg["position_id"]), None)
            if pr and pr.get("signal_timestamp"):
                leg_call = pr["signal_timestamp"]
        if isinstance(leg_call, datetime) and leg_call.tzinfo is None:
            leg_call = leg_call.replace(tzinfo=timezone.utc)
        leg_end = pos.get("closed_at") or datetime.now(timezone.utc)
        if isinstance(leg_end, datetime) and leg_end.tzinfo is None:
            leg_end = leg_end.replace(tzinfo=timezone.utc)
        tf = leg["timeframe"]
        start, end, candle_limit = _timeframe_window(leg_call, leg_end, tf)
        leg["window"] = {"start": start, "end": end}
        leg["candles"] = await _fetch_candles(
            symbol=leg["symbol"],
            exchange=leg["exchange"],
            timeframe=tf,
            start=start,
            end=end,
            limit=candle_limit,
        )
        leg["coverage"]["candle_count"] = len(leg["candles"])
        if leg.get("position_id"):
            updates = await pool.fetch_all(
                """
                SELECT timestamp, price, unrealized_pnl_pct, unrealized_pnl_usd,
                       distance_to_entry_pct, distance_to_sl_pct, distance_to_tp1_pct,
                       time_in_trade_minutes, update_source
                FROM public.position_updates
                WHERE position_id = $1
                ORDER BY timestamp ASC
                LIMIT 2500
                """,
                (int(leg["position_id"]),),
            )
            leg["position_updates"] = [dict(u) for u in updates]
            leg["coverage"]["update_count"] = len(leg["position_updates"])

    selected = 0
    if position_q:
        try:
            pid = int(position_q)
            for i, leg in enumerate(legs):
                if leg.get("position_id") == pid:
                    selected = i
                    break
        except ValueError:
            pass
    elif leg_idx is not None:
        try:
            selected = max(0, min(int(leg_idx), len(legs) - 1))
        except ValueError:
            selected = 0
    else:
        for i, leg in enumerate(legs):
            if leg.get("armed"):
                selected = i
                break

    active = legs[selected] if legs else {}
    active_pos = active.get("position") or {}
    active_levels = active.get("levels") or {}

    payload = {
        "ok": True,
        "id": interp_id,
        "symbol": active.get("symbol") or default_symbol,
        "exchange": active.get("exchange") or default_exchange,
        "timeframe": active.get("timeframe") or _normalise_timeframe(default_tf_raw or "1m"),
        "timeframe_source": active.get("timeframe_source") or default_tf_raw,
        "candle_seconds": _TF_SECONDS.get(active.get("timeframe") or "1m", 60),
        "call_ts": call_ts,
        "window": active.get("window") or {},
        "annotated_chart_url": f"api/charts/{interp_id}",
        "media_url": _media_url(row.get("media_id"), row.get("media_local_path"), row.get("media_source_url")),
        "media": {
            "id": row.get("media_id"),
            "local_path": row.get("media_local_path"),
            "source_url": row.get("media_source_url"),
            "thumbnail_path": row.get("media_thumbnail_path"),
            "media_type": row.get("media_type"),
            "mime_type": row.get("mime_type"),
        },
        "trader": {
            "profile_id": row.get("trader_profile_id"),
            "handle_raw": row.get("handle_raw"),
            "handle_normalized": row.get("handle_normalized"),
            "display_name": row.get("display_name"),
            "platform": row.get("platform"),
            "trader_type": row.get("trader_type"),
        },
        "news": {
            "id": row.get("news_item_id"),
            "headline": row.get("news_headline"),
            "content": row.get("news_content"),
            "source": row.get("news_source"),
            "author": row.get("news_author"),
            "channel_name": row.get("news_channel_name"),
            "published_at": row.get("news_published_at"),
            "collected_at": row.get("news_collected_at"),
        },
        "signal": {
            "direction": active.get("direction") or row.get("consensus_direction"),
            "confidence": row.get("consensus_confidence"),
            "method": row.get("consensus_method"),
            "llm_direction": row.get("llm_direction"),
            "llm_confidence": row.get("llm_confidence"),
            "llm_reasoning": row.get("llm_reasoning"),
            "quant_direction": row.get("quant_direction"),
            "quant_confidence": row.get("quant_confidence"),
            "trader_stated_thesis": row.get("trader_stated_thesis") if row.get("trader_stated_thesis") is not None else (match_positions[0].get("entry_reason_trader") if match_positions else None),
            "llm_inferred_thesis": row.get("llm_inferred_thesis") if row.get("llm_inferred_thesis") is not None else (match_positions[0].get("entry_reason_llm") if match_positions else None),
            "reason_agreement_score": row.get("reason_agreement_score"),
            "ai_agreement_score": row.get("ai_agreement_score"),
            "ai_comment": row.get("ai_comment"),
            "levels": active_levels,
            "tags": {
                "pattern": row.get("pattern_tags"),
                "setup": row.get("setup_tags"),
                "regime": row.get("regime_tags"),
                "session": row.get("session_tags"),
            },
            "model_version": row.get("model_version"),
            "prompt_version": row.get("prompt_version"),
            "created_at": row.get("created_at"),
        },
        "position": active_pos,
        "legs": legs,
        "selected_leg": selected,
        "stats": {
            "trader_trades_extracted": len(trader_trades),
            "chart_hacker_trades_extracted": len(ch_trades),
            "positions_armed": len(positions),
            "legs_total": len(legs),
            "legs_armed": sum(1 for lg in legs if lg.get("armed")),
        },
        "candles": active.get("candles") or [],
        "position_updates": active.get("position_updates") or [],
        "coverage": active.get("coverage") or {"candle_count": 0, "update_count": 0},
    }
    return _json(payload)



def _entry_hit(candle: dict[str, Any], entry: float) -> bool:
    return float(candle["low"]) <= entry <= float(candle["high"])


def _classify_signal_path(row: dict[str, Any], candles: list[dict[str, Any]]) -> dict[str, Any]:
    entry = row.get("entry_price")
    if entry is None or not candles:
        return {"state": "unknown", "entry_hit": False, "distance_to_entry_pct": None}
    entry_f = float(entry)
    direction = str(row.get("direction") or "").lower()
    sl = float(row["stop_loss"]) if row.get("stop_loss") is not None else None
    tp = float(row["take_profit_1"]) if row.get("take_profit_1") is not None else None
    first_close = float(candles[0]["close"])
    last_close = float(candles[-1]["close"])
    # Phase 2 (2026-05-29): SIGNED distance — (current - entry)/entry. Positive =
    # price is ABOVE entry, negative = BELOW. Standardised across every endpoint
    # so the Trading Floor and the Radar agree on the same number for a symbol.
    # Sorting "closest to entry" is done frontend-side via Math.abs().
    dist = ((last_close - entry_f) / entry_f * 100.0) if entry_f else None
    approaching = abs(last_close - entry_f) < abs(first_close - entry_f) if entry_f else False

    hit_idx = None
    for i, c in enumerate(candles):
        if _entry_hit(c, entry_f):
            hit_idx = i
            break
    if hit_idx is None:
        return {
            "state": "awaiting_entry",
            "entry_hit": False,
            "distance_to_entry_pct": dist,
            "approaching": approaching,
            "current_price": last_close,
            "first_price": first_close,
        }

    # Once entry has traded through, it no longer belongs in Entry Radar.
    exit_state = "entered"
    exit_ts = None
    for c in candles[hit_idx:]:
        high = float(c["high"])
        low = float(c["low"])
        if direction == "short":
            stopped = sl is not None and high >= sl
            target = tp is not None and low <= tp
        else:
            stopped = sl is not None and low <= sl
            target = tp is not None and high >= tp
        if stopped and target:
            exit_state = "entry_then_sl_or_tp_same_candle"
            exit_ts = c["timestamp"]
            break
        if stopped:
            exit_state = "stopped_out"
            exit_ts = c["timestamp"]
            break
        if target:
            exit_state = "tp_hit"
            exit_ts = c["timestamp"]
            break
    return {
        "state": exit_state,
        "entry_hit": True,
        "entry_hit_at": candles[hit_idx]["timestamp"],
        "exit_at": exit_ts,
        "distance_to_entry_pct": dist,
        "current_price": last_close,
        "first_price": first_close,
    }


# ---------------------------------------------------------------------------
# Round 13 (2026-05-24): Live-price fallback for the entry radar.
#
# The radar's primary distance-to-entry calculation comes from the local
# candle store (``_classify_signal_path`` reads ``candle_data_<tf>``). If
# the symbol has no rows in that table — common for newer perp listings
# that the candle collector hasn't reached yet, or for symbols on a
# venue we don't ingest 1m candles from — ``current_price`` ends up
# ``None`` and the UI prints "TO ENTRY: —".
#
# Fix: after the candle pass, fan out parallel live-price probes for
# every row with ``current_price is None`` and ``entry_price is not
# None``. We bound concurrency (default 8) and per-call timeout
# (default 3s) so a slow exchange can't stall the whole radar request.
# We also guard against unsupported exchanges (Capital.com would fail
# the SUPPORTED_EXCHANGES whitelist) by silently skipping them — they
# end up rendering "—" as before.
# ---------------------------------------------------------------------------

_RADAR_LIVE_PRICE_CONCURRENCY = 8
# 6 s gives CCXT enough headroom for its first-request setup cost
# (load_markets() + per-symbol candidate retry inside _probe()) without
# blocking the whole radar response. Each row probes in parallel, so
# the worst-case wall-time is one timeout, not N * timeout.
_RADAR_LIVE_PRICE_TIMEOUT_S = 6.0

# Two-tier in-process cache. The radar refreshes every 30 s on the
# dashboard; we cache positive hits for 30 s so back-to-back radar
# polls don't re-probe the same exchange. We cache negative hits
# ("symbol does not exist on this exchange") for 10 s so a chronically
# bad mapping (e.g. BITTENSOR/USDT:USDT instead of TAO/USDT:USDT)
# stops stalling the radar after a single timeout.
_RADAR_PRICE_TTL_HIT_S = 30.0
_RADAR_PRICE_TTL_MISS_S = 10.0
_RADAR_PRICE_CACHE: Dict[tuple[str, str], tuple[Optional[float], float]] = {}
_RADAR_PRICE_LOCK = asyncio.Lock()


def _radar_cache_get(key: tuple[str, str]) -> tuple[bool, Optional[float]]:
    """Return ``(found, price)`` from the radar live-price cache.

    ``found=True, price=None``  → cached MISS (don't re-probe yet).
    ``found=True, price>0``     → cached HIT.
    ``found=False``             → not cached, must probe.
    """
    import time as _time

    entry = _RADAR_PRICE_CACHE.get(key)
    if entry is None:
        return (False, None)
    price, expires_at = entry
    if _time.monotonic() > expires_at:
        _RADAR_PRICE_CACHE.pop(key, None)
        return (False, None)
    return (True, price)


def _radar_cache_set(key: tuple[str, str], price: Optional[float]) -> None:
    """Cache a probe outcome under the appropriate TTL."""
    import time as _time

    ttl = _RADAR_PRICE_TTL_HIT_S if price else _RADAR_PRICE_TTL_MISS_S
    _RADAR_PRICE_CACHE[key] = (price, _time.monotonic() + ttl)


def _radar_cache_clear() -> None:
    """Test-only: drop the cache."""
    _RADAR_PRICE_CACHE.clear()


async def _radar_live_price_one(
    sem: asyncio.Semaphore,
    symbol: str,
    exchange: str,
) -> Optional[float]:
    """Probe one symbol; return None on any failure.

    Consults the in-process cache first; only hits CCXT when we
    haven't seen this ``(exchange, symbol)`` recently or the cached
    miss has expired.
    """
    from shared.market_data.live_price import (
        SUPPORTED_EXCHANGES,
        LivePriceError,
        UnsupportedExchangeError,
        fetch_live_price,
    )

    if not symbol or not exchange:
        return None
    ex_lower = exchange.lower()
    if ex_lower not in SUPPORTED_EXCHANGES:
        return None

    key = (ex_lower, symbol)
    found, cached = _radar_cache_get(key)
    if found:
        return cached

    async with sem:
        # Re-check inside the semaphore — another concurrent waiter may
        # have populated the cache while we were queued.
        found, cached = _radar_cache_get(key)
        if found:
            return cached
        try:
            res = await fetch_live_price(
                symbol, exchange, timeout_s=_RADAR_LIVE_PRICE_TIMEOUT_S,
            )
            price = float(res.price)
            _radar_cache_set(key, price if price > 0 else None)
            return price if price > 0 else None
        except (LivePriceError, UnsupportedExchangeError, asyncio.TimeoutError, ValueError) as exc:
            logger.debug("radar live-price miss %s@%s: %s", symbol, exchange, exc)
            _radar_cache_set(key, None)
            return None
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("radar live-price unexpected %s@%s: %s", symbol, exchange, exc)
            _radar_cache_set(key, None)
            return None


async def _enrich_radar_with_live_prices(
    rows: list[dict[str, Any]], *, max_probes: int | None = None
) -> int:
    """Mutate ``rows`` in place, filling ``current_price`` from CCXT.

    Strategy:
      1. Walk ``rows`` and collect every ``(exchange, symbol)`` pair
         that needs a price.
      2. Deduplicate — multiple radar cards often share the same symbol
         (e.g. three traders calling BTC at different entries), so one
         CCXT probe can fill many rows at once.
      3. Fan out unique probes in parallel (bounded by
         ``_RADAR_LIVE_PRICE_CONCURRENCY``).
      4. Apply the resulting price to every row that asked for it,
         recomputing ``distance_to_entry_pct`` per row's own
         ``entry_price``.

    Args:
        rows: Radar rows about to be returned. Mutated in place.

    Returns:
        The count of rows that were back-filled.
    """
    by_pair: Dict[tuple[str, str], list[int]] = {}
    for idx, r in enumerate(rows):
        if r.get("current_price") is not None:
            continue
        if r.get("entry_price") is None:
            continue
        sym = r.get("symbol") or r.get("instrument_symbol")
        # Phase 2 (2026-05-29): unified-signals result rows carry the exchange
        # under "exchange"; radar rows carry it under "instrument_exchange".
        # Accept either so this helper works for both callers.
        ex = (r.get("instrument_exchange") or r.get("exchange") or "bybit").lower()
        if not sym:
            continue
        by_pair.setdefault((str(ex), str(sym)), []).append(idx)

    if not by_pair:
        return 0

    sem = asyncio.Semaphore(_RADAR_LIVE_PRICE_CONCURRENCY)
    pairs = list(by_pair.items())
    # Phase 2 (2026-05-29): bound the probe count for high-volume callers
    # (unified-signals can return 120 rows). Insertion order == row order, so
    # the cap keeps the most-recent signals' symbols.
    if max_probes is not None and len(pairs) > max_probes:
        pairs = pairs[:max_probes]
    coros = [_radar_live_price_one(sem, sym, ex) for (ex, sym), _ in pairs]
    results = await asyncio.gather(*coros, return_exceptions=False)

    filled = 0
    for ((_ex, _sym), idxs), price in zip(pairs, results):
        if price is None or price <= 0:
            continue
        for idx in idxs:
            r = rows[idx]
            try:
                entry = float(r["entry_price"])
            except (TypeError, ValueError):
                continue
            if entry <= 0:
                continue
            r["current_price"] = price
            r["distance_to_entry_pct"] = ((price - entry) / entry) * 100.0
            # Stamp the source so the UI / debugger can tell where the
            # number came from. Keeps "honest data" rule from the
            # May-2026 dashboard refresh.
            ps = dict(r.get("path_state") or {})
            ps["current_price"] = price
            ps["distance_to_entry_pct"] = r["distance_to_entry_pct"]
            ps["price_source"] = "live_ccxt_fallback"
            r["path_state"] = ps
            filled += 1
    if filled:
        logger.info(
            "radar live-price fallback filled %d rows from %d unique probes",
            filled, len(pairs),
        )
    return filled


async def handle_entry_radar(request: web.Request) -> web.Response:
    """GET /api/entry-radar — actionable pending calls that have NOT hit entry.

    The endpoint replays candles after each pending call. If price has already
    traded through entry (and especially if it later hit SL/TP), the call is
    excluded from the radar. This keeps the UI focused on entries still waiting
    to trigger.
    """
    try:
        limit = max(10, min(int(request.query.get("limit", "80")), 150))
    except ValueError:
        limit = 80
    pool = await get_shared_pool()
    raw_rows = await pool.fetch_all(
        """
        SELECT tp.id, tp.news_item_id, tp.media_item_id, tp.trader_profile_id,
               tp.signal_interpretation_id, tp.instrument_symbol, tp.instrument_exchange,
               tp.direction, tp.entry_price, tp.stop_loss, tp.take_profit_1,
               tp.take_profit_2, tp.take_profit_3, tp.status, tp.signal_timestamp,
               tp.created_at, tp.actor_id, tp.signal_source, tp.detection_method,
               si.timeframe, si.consensus_confidence, si.consensus_method,
               si.llm_reasoning, si.ai_comment,
               tr.handle_raw, tr.handle_normalized, tr.display_name, tr.platform,
               ni.content AS news_content, ni.headline AS news_headline,
               mi.id AS media_id, mi.local_path AS media_local_path, mi.source_url AS media_source_url
        FROM public.tracked_positions tp
        LEFT JOIN public.signal_interpretations si ON si.id = tp.signal_interpretation_id
        LEFT JOIN public.trader_profiles tr ON tr.id = tp.trader_profile_id
        LEFT JOIN public.news_items ni ON ni.id = tp.news_item_id
        LEFT JOIN public.media_items mi ON mi.id = COALESCE(tp.media_item_id, si.media_item_id)
        WHERE tp.status = 'pending'
          AND tp.entry_price IS NOT NULL
          AND tp.signal_interpretation_id IS NOT NULL
          AND tp.created_at > now() - interval '30 days'
        ORDER BY tp.signal_timestamp DESC NULLS LAST, tp.id DESC
        LIMIT $1
        """,
        (limit,),
    )
    actionable: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for row in raw_rows:
        symbol = _clean_symbol(row.get("instrument_symbol"))
        if not symbol:
            continue
        tf = _normalise_timeframe(row.get("timeframe") or "1m")
        call_ts = row.get("signal_timestamp") or row.get("created_at") or datetime.now(timezone.utc)
        if call_ts.tzinfo is None:
            call_ts = call_ts.replace(tzinfo=timezone.utc)
        # For radar, start at the call and go to now; use trader timeframe.
        # Anti-DESC-LIMIT-slide guard (same class as Bug 11): the candle SQL
        # uses `ORDER BY timestamp DESC LIMIT $6`, so when the (start, end)
        # window exceeds what `radar_limit` candles can hold (e.g. trade
        # opened 24h ago on 1m, end=now is 24h later, limit=260 only fits
        # ~4.3h ending at now), the call_ts itself slides out of the visible
        # window and the radar mini-chart shows the "wrong day".
        now = datetime.now(timezone.utc)
        radar_limit = 260
        sec = _TF_SECONDS.get(tf, 60)
        radar_end = now
        if (radar_end - call_ts).total_seconds() > sec * radar_limit:
            radar_end = call_ts + timedelta(seconds=sec * radar_limit)
        candles = await _fetch_candles(
            symbol=symbol,
            exchange=(row.get("instrument_exchange") or "bybit").lower(),
            timeframe=tf,
            start=call_ts,
            end=radar_end,
            limit=radar_limit,
        )
        path = _classify_signal_path(row, candles)
        base = dict(row)
        base["symbol"] = symbol
        base["timeframe"] = tf
        base["path_state"] = path
        base["current_price"] = path.get("current_price")
        base["distance_to_entry_pct"] = path.get("distance_to_entry_pct")
        base["approaching"] = path.get("approaching")
        base["media_url"] = _media_url(row.get("media_id"), row.get("media_local_path"), row.get("media_source_url"))
        base["mini_candles"] = candles[-80:]
        if path.get("entry_hit"):
            excluded.append({
                "id": row.get("id"),
                "signal_interpretation_id": row.get("signal_interpretation_id"),
                "symbol": symbol,
                "state": path.get("state"),
                "entry_hit_at": path.get("entry_hit_at"),
                "exit_at": path.get("exit_at"),
            })
            continue
        actionable.append(base)
    # Round 13 (2026-05-24): live-price fallback. Rows whose candle
    # replay returned no current_price (often: brand-new perp listings
    # the candle collector hasn't reached yet) get a CCXT live-price
    # probe here so the dashboard can render a real "TO ENTRY %" instead
    # of an unhelpful em-dash.
    try:
        await _enrich_radar_with_live_prices(actionable)
    except Exception as exc:  # pragma: no cover - never block radar on this
        logger.warning("radar live-price fallback raised: %s", exc)

    # Round 11 (2026-05-24): recency tiebreaker now matches aggregate_signals —
    # newest first when distances are equal. Previously the ascending tuple
    # pulled the OLDEST row to the top on a tie, which is the opposite of
    # everywhere else in the dashboard.
    def _radar_sort_key(r: Dict[str, Any]) -> tuple:
        dist = r.get("distance_to_entry_pct")
        if dist is None:
            return (1, 0.0, 0.0)
        try:
            abs_dist = abs(float(dist))
        except (TypeError, ValueError):
            return (1, 0.0, 0.0)
        ts = r.get("signal_timestamp") or r.get("created_at")
        ts_neg = -ts.timestamp() if isinstance(ts, datetime) else 0.0
        return (0, abs_dist, ts_neg)

    actionable.sort(key=_radar_sort_key)
    return _json({
        "ok": True,
        "count": len(actionable),
        "excluded_count": len(excluded),
        "excluded_sample": excluded[:20],
        "rows": actionable,
    })


async def handle_competition_agent(request: web.Request) -> web.Response:
    """GET /api/competition-agent?agent=copy_spot_seq&contest=copy-trade-scenarios

    Returns participant stats + open positions enriched with live price,
    unrealized P&L, distance to entry/SL/TP, trader Discord handle, chart-posted
    time, and signal_interpretation_id for drill-down.
    """
    agent_id = request.query.get("agent", "")
    contest_id = request.query.get("contest", "copy-trade-scenarios")
    if not agent_id:
        return _err(400, "agent is required")

    pool = await get_shared_pool()

    participant = await pool.fetch_one(
        """
        SELECT agent_id, strategy_ref, scores, equity_usd, realized_pnl_usd,
               unrealized_pnl_usd, return_pct, win_rate, total_trades,
               open_positions, total_fees_usd, metadata
        FROM contest_participants
        WHERE contest_id = $1 AND agent_id = $2
        """,
        (contest_id, agent_id),
    )
    if not participant:
        return _err(404, "agent not found in contest")

    history_rows = await pool.fetch_all(
        """
        SELECT ct.symbol, ct.direction, ct.entry_price, ct.exit_price,
               ct.sl_price, ct.tp_price, ct.allocated, ct.leverage, ct.pnl,
               ct.fees, ct.exit_reason, ct.entered_at, ct.exited_at,
               ct.tracked_position_id, ct.signal_interpretation_id,
               tp.signal_timestamp AS chart_posted_at,
               tp.signal_interpretation_id AS tp_signal_interpretation_id,
               tr.handle_raw, tr.handle_normalized, tr.display_name
        FROM competition_trades ct
        LEFT JOIN tracked_positions tp ON tp.id = ct.tracked_position_id
        LEFT JOIN trader_profiles tr ON tr.id = tp.trader_profile_id
        WHERE ct.contest_id = $1 AND ct.agent_id = $2
        ORDER BY COALESCE(ct.exited_at, ct.entered_at) DESC
        LIMIT 100
        """,
        (contest_id, agent_id),
    )

    open_raw = [dict(r) for r in history_rows if r["exited_at"] is None or r["exit_price"] is None]
    closed_trades = [dict(r) for r in history_rows if r["exited_at"] is not None and r["exit_price"] is not None]

    # Enrich open positions with live price and unrealized P&L.
    #
    # Bug H10 fix (Code Analyzer 2 §2.3):
    #   The lookup matched `i.symbol = ANY($1)` against the raw
    #   `competition_trades.symbol` strings (e.g. ``BTCUSDT``) but the
    #   instruments catalog stores canonical slash-form symbols
    #   (``BTC/USDT``). Result: zero matches, ``current_price`` always None,
    #   ``unrealized_pnl`` always 0.0 on the dashboard. We canonicalise
    #   each symbol AND match against canonical instruments, building a
    #   raw->canon map so we can hand the live price back under the
    #   trade's original symbol form.
    if open_raw:
        from shared.utils.instrument_normaliser import to_canonical_symbol  # local import — used only here
        raw_symbols = list({p["symbol"] for p in open_raw if p.get("symbol")})
        # raw -> canonical map (preserves both forms so we can join back)
        raw_to_canon: dict[str, str] = {}
        canon_symbols: list[str] = []
        for s in raw_symbols:
            try:
                c = to_canonical_symbol(s) or s
            except Exception:
                c = s
            raw_to_canon[s] = c
            if c not in canon_symbols:
                canon_symbols.append(c)
        live_prices_by_canon: dict[str, float] = {}
        if canon_symbols:
            rows = await pool.fetch_all(
                """
                SELECT DISTINCT ON (i.symbol) i.symbol, c.close
                FROM public.candles c
                JOIN public.instruments i ON i.id = c.instrument_id
                WHERE i.symbol = ANY($1) AND c.timeframe::text = '1m'
                ORDER BY i.symbol, c.timestamp DESC
                """,
                (canon_symbols,),
            )
            for r in rows:
                live_prices_by_canon[r["symbol"]] = float(r["close"])

        # Fallback: for symbols with no candles, use tracked_positions.current_price
        unpriced = [raw for raw in raw_symbols if raw_to_canon.get(raw, raw) not in live_prices_by_canon]
        if unpriced:
            tp_rows = await pool.fetch_all(
                """
                SELECT DISTINCT ON (instrument_symbol) instrument_symbol, current_price
                FROM public.tracked_positions
                WHERE instrument_symbol = ANY($1) AND current_price IS NOT NULL AND current_price > 0
                ORDER BY instrument_symbol, updated_at DESC
                """,
                (unpriced,),
            )
            for r in tp_rows:
                sym = r["instrument_symbol"] or ""
                if sym and r["current_price"]:
                    # Also try without :USDT suffix
                    clean = sym.replace(':USDT','').replace(':USDC','')
                    live_prices_by_canon[sym] = float(r["current_price"])
                    if clean != sym:
                        live_prices_by_canon[clean] = float(r["current_price"])

        # Resolve each trade's symbol back via raw->canon map.
        live_prices: dict[str, float] = {
            raw: live_prices_by_canon.get(raw_to_canon.get(raw, raw))
            for raw in raw_symbols
            if live_prices_by_canon.get(raw_to_canon.get(raw, raw)) is not None
        }

        for p in open_raw:
            entry = float(p.get("entry_price") or 0)
            sl = float(p.get("sl_price") or 0) if p.get("sl_price") else None
            tp = float(p.get("tp_price") or 0) if p.get("tp_price") else None
            alloc = float(p.get("allocated") or 0)
            lev = float(p.get("leverage") or 1)
            direction = p.get("direction", "long")
            current = live_prices.get(p["symbol"])
            p["current_price"] = current

            if current and entry > 0:
                price_delta_pct = (current - entry) / entry if direction == "long" else (entry - current) / entry
                notional = alloc * lev
                p["unrealized_pnl"] = round(notional * price_delta_pct, 2)
                p["unrealized_pnl_pct"] = round(price_delta_pct * 100, 2)
                p["distance_to_entry_pct"] = round((current - entry) / entry * 100, 2)
                if sl:
                    p["distance_to_sl_pct"] = round((current - sl) / sl * 100, 2)
                if tp:
                    p["distance_to_tp_pct"] = round((current - tp) / tp * 100, 2)
            else:
                p["unrealized_pnl"] = 0.0
                p["unrealized_pnl_pct"] = 0.0

            # Ensure signal_interpretation_id is available for drill-down
            p["signal_interpretation_id"] = p.get("tp_signal_interpretation_id") or p.get("signal_interpretation_id")

    for p in closed_trades:
        p["signal_interpretation_id"] = p.get("tp_signal_interpretation_id") or p.get("signal_interpretation_id")

    return _json({
        "ok": True,
        "agent": dict(participant),
        "open_positions": open_raw,
        "history": closed_trades,
    })

async def handle_unified_signals(request: web.Request) -> web.Response:
    """GET /api/unified-signals?limit=120&status=pending

    Single endpoint powering both list and card views. Returns all signals
    enriched with live prices and real delta-to-entry. Replaces separate
    entry-radar and snapshot.signals data sources.
    """
    try:
        limit = max(1, min(int(request.query.get("limit", "120")), 200))
    except ValueError:
        limit = 120
    status_filter = request.query.get("status", "").strip()

    pool = await get_shared_pool()

    # 1. Query all signals with their metadata
    status_filter_sql = f"AND tp.status = '{status_filter}'" if status_filter else ""
    limit_sql = str(int(limit))

    rows = await pool.fetch_all(
        """
        SELECT DISTINCT ON (si.id) si.id, si.instrument_symbol, si.instrument_exchange,
               si.consensus_direction, si.entry_price, si.stop_loss, si.take_profit_1,
               si.take_profit_2, si.take_profit_3, si.timeframe,
               si.consensus_confidence, si.consensus_method,
               si.llm_reasoning, si.ai_comment, si.created_at,
               si.news_item_id, si.media_item_id, si.trader_profile_id,
               ni.author, ni.headline,
               tr.handle_raw, tr.handle_normalized, tr.display_name, tr.platform,
               mi.id AS media_id, mi.local_path AS media_local_path, mi.source_url AS media_source_url,
               tp.id AS position_id, tp.status AS position_status,
               tp.activated_at AS position_activated_at,
               tp.signal_timestamp, tp.current_price AS tp_current_price,
               tp.distance_to_entry_pct AS tp_distance_to_entry_pct,
               tp.actor_id, tp.signal_source, tp.detection_method
        FROM public.signal_interpretations si
        LEFT JOIN public.tracked_positions tp ON tp.signal_interpretation_id = si.id
        LEFT JOIN public.news_items ni ON ni.id = si.news_item_id
        LEFT JOIN public.trader_profiles tr ON tr.id = si.trader_profile_id
        LEFT JOIN public.media_items mi ON mi.id = si.media_item_id
        WHERE si.created_at > now() - interval '30 days'
          -- Phase 1 overnight cleanup: exclude stale signals (>7d, no tracked_position).
          -- These clutter the radar with dead interpretations that will never trade.
          AND (tp.id IS NOT NULL OR si.created_at > now() - interval '7 days')
        """ + status_filter_sql + f" ORDER BY si.id DESC, tp.id DESC NULLS LAST LIMIT {limit_sql}",
        (),
    )
    # Phase 3 (2026-05-29) ROOT-CAUSE FIX: the LIMIT used to order by `si.id`
    # ASCENDING, so it returned the OLDEST 120 interpretations (whose positions
    # are long closed/expired) and never the recent ones — the radar was
    # perpetually stale/empty regardless of any frontend filtering. We now order
    # `si.id DESC` (newest first) and break DISTINCT ON ties by the newest
    # tracked_position (`tp.id DESC`), so recent pending + just-filled setups
    # are actually fetched.

    # 2. Collect unique symbols and batch-fetch latest prices
    # Build a lookup map: every raw symbol form → cleaned canonical form
    raw_to_clean: dict = {}
    for r in rows:
        raw = (r.get("instrument_symbol") or "").strip()
        clean = _clean_symbol(raw)
        if clean:
            raw_to_clean[raw] = clean
    
    all_cleaned = list(set(raw_to_clean.values()))
    prices: dict = {}
    
    if all_cleaned:
        # Primary: latest 1m candle close (candles table via instruments)
        price_rows = await pool.fetch_all(
            """
            SELECT DISTINCT ON (i.symbol) i.symbol, c.close
            FROM public.candles c
            JOIN public.instruments i ON i.id = c.instrument_id
            WHERE i.symbol = ANY($1) AND c.timeframe::text = '1m'
            ORDER BY i.symbol, c.timestamp DESC
            """,
            (all_cleaned,),
        )
        for pr in price_rows:
            prices[pr["symbol"]] = float(pr["close"])

        # Fallback: tracked_positions for symbols with no candles
        unpriced = [s for s in all_cleaned if s not in prices]
        if unpriced:
            # Try both cleaned and :USDT-suffixed forms
            tp_forms = unpriced + [s + ":USDT" for s in unpriced]
            tp_rows = await pool.fetch_all(
                """
                SELECT DISTINCT ON (instrument_symbol) 
                       REPLACE(REPLACE(instrument_symbol, ':USDT', ''), ':USDC', '') as clean_sym,
                       current_price
                FROM public.tracked_positions
                WHERE (instrument_symbol = ANY($1) OR REPLACE(REPLACE(instrument_symbol, ':USDT', ''), ':USDC', '') = ANY($2))
                  AND current_price IS NOT NULL AND current_price > 0
                ORDER BY instrument_symbol, updated_at DESC
                """,
                (tp_forms, unpriced),
            )
            for tr in tp_rows:
                cs = tr["clean_sym"] or ""
                if cs and tr["current_price"] and cs not in prices:
                    prices[cs] = float(tr["current_price"])

    # 3. Build enriched response (skip mispriced entries)
    result: list = []
    for r in rows:
        entry = float(r.get("entry_price") or 0)
        sym = (r.get("instrument_symbol") or "").strip()
        clean = raw_to_clean.get(sym, _clean_symbol(sym))
        current = prices.get(clean)
        # Skip if entry is 2+ orders of magnitude off from live price
        # (old LLM misread — e.g. $0.74 instead of $656 for BNB)
        if entry > 0 and current and current > 0:
            ratio = entry / current
            if ratio < 0.01 or ratio > 100:
                continue
        raw_sym = (r.get("instrument_symbol") or "").strip()
        sym = raw_to_clean.get(raw_sym, _clean_symbol(raw_sym))
        entry = float(r.get("entry_price") or 0)
        current = prices.get(sym)  # lookup by cleaned symbol
        distance_to_entry = None
        if current and entry > 0:
            # Phase 2 (2026-05-29): SIGNED distance (was abs()). Positive = price
            # above entry, negative = below. Matches _classify_signal_path and the
            # live-price fallback so the Floor and Radar never disagree.
            distance_to_entry = round((current - entry) / entry * 100, 2)

        result.append({
            "id": r["id"],
            "symbol": sym,
            "exchange": r.get("instrument_exchange") or "",
            "direction": r.get("consensus_direction") or "",
            "entry_price": entry,
            "stop_loss": float(r.get("stop_loss") or 0),
            "take_profit_1": float(r.get("take_profit_1") or 0),
            "take_profit_2": float(r.get("take_profit_2") or 0),
            "take_profit_3": float(r.get("take_profit_3") or 0),
            "timeframe": r.get("timeframe") or "",
            "consensus_confidence": float(r.get("consensus_confidence") or 0),
            "consensus_method": r.get("consensus_method") or "",
            "llm_reasoning": r.get("llm_reasoning") or "",
            "current_price": current,
            "distance_to_entry_pct": distance_to_entry,
            "position_status": r.get("position_status") or "signal",
            # B (2026-05-29): "armed" = a real pending tracked_position is
            # waiting to trigger (high-conviction, confidence>=0.4, has a demo
            # order). Read-but-not-armed interpretations (position_status
            # 'signal') are NOT armed and must NOT clutter "Approaching entry".
            "armed": (r.get("position_status") == "pending"),
            "position_activated_at": r.get("position_activated_at").isoformat() if r.get("position_activated_at") else None,
            "signal_timestamp": (r.get("signal_timestamp") or r.get("created_at")).isoformat() if (r.get("signal_timestamp") or r.get("created_at")) else None,
            "created_at": r.get("created_at").isoformat() if r.get("created_at") else None,
            "author": r.get("author") or "",
            "headline": r.get("headline") or "",
            "trader_display_name": r.get("display_name") or r.get("handle_raw") or "",
            "trader_handle_normalized": r.get("handle_normalized") or "",
            "trader_platform": r.get("platform") or "",
            "actor_id": r.get("actor_id") or "",
            "signal_source": r.get("signal_source") or "",
            "detection_method": r.get("detection_method") or "",
            "media_id": r.get("media_id"),
            "media_url": _media_url(r.get("media_id"), r.get("media_local_path"), r.get("media_source_url")),
        })

    # Phase 2 (2026-05-29): live-price CCXT fallback. Rows whose symbol has no
    # local 1m candles (exotic / brand-new perps) arrive here with
    # current_price=None and would render "TO ENTRY: —". Probe CCXT for those
    # (bounded) so the Floor + Radar show a real distance. Same helper the
    # entry-radar endpoint uses, so the price source is identical everywhere.
    try:
        await _enrich_radar_with_live_prices(result, max_probes=40)
        # Round the live-filled distances to match the candle-path precision.
        for r in result:
            d = r.get("distance_to_entry_pct")
            if isinstance(d, float):
                r["distance_to_entry_pct"] = round(d, 2)
    except Exception as exc:  # pragma: no cover - never block the tab on this
        logger.warning("unified-signals live-price fallback raised: %s", exc)

    # Phase 3 (2026-05-29): radar supply stats for the honest empty-state. When
    # 0 setups are waiting, the radar shows "nothing pending — last 24h: X
    # filled, Y cancelled (Z no-setup, W dupes)" instead of looking broken.
    # Cheap: a single grouped COUNT over a 30-day slice.
    radar_meta: dict[str, int] = {
        "pending": 0, "filled_24h": 0, "cancelled_24h": 0,
        "no_setup_24h": 0, "dupes_24h": 0, "unsupported_24h": 0,
        "just_filled_30m": 0,
        # B (2026-05-29): freshness for the honest empty-state — how long since
        # the interpreter last read ANY setup, and how many it read in 24h. Lets
        # the radar say "12 read in 24h, none armed, last read 3h ago" instead
        # of looking broken.
        "last_signal_min": -1, "signals_24h": 0,
    }
    try:
        meta = await pool.fetch_one(
            """
            SELECT
                count(*) FILTER (WHERE status = 'pending') AS pending,
                count(*) FILTER (WHERE status IN ('open','closed')
                                 AND created_at > now() - interval '24 hours') AS filled_24h,
                count(*) FILTER (WHERE status = 'cancelled'
                                 AND updated_at > now() - interval '24 hours') AS cancelled_24h,
                count(*) FILTER (WHERE status = 'cancelled'
                                 AND updated_at > now() - interval '24 hours'
                                 AND status_reason LIKE 'round9_no_explicit_setup%') AS no_setup_24h,
                count(*) FILTER (WHERE status = 'cancelled'
                                 AND updated_at > now() - interval '24 hours'
                                 AND status_reason LIKE 'deduped:%') AS dupes_24h,
                count(*) FILTER (WHERE status = 'cancelled'
                                 AND updated_at > now() - interval '24 hours'
                                 AND status_reason LIKE 'unsupported:%') AS unsupported_24h,
                count(*) FILTER (WHERE status = 'open'
                                 AND activated_at > now() - interval '30 minutes') AS just_filled_30m
            FROM public.tracked_positions
            WHERE created_at > now() - interval '30 days'
            """,
        )
        if meta:
            for k in ("pending", "filled_24h", "cancelled_24h", "no_setup_24h",
                      "dupes_24h", "unsupported_24h", "just_filled_30m"):
                radar_meta[k] = int(meta.get(k) or 0)
    except Exception as exc:  # pragma: no cover - never block the tab on this
        logger.warning("unified-signals radar_meta query raised: %s", exc)

    # B (2026-05-29): interpreter-freshness for the empty-state.
    try:
        fresh = await pool.fetch_one(
            """
            SELECT
                EXTRACT(EPOCH FROM (now() - max(created_at)))/60 AS last_signal_min,
                count(*) FILTER (WHERE created_at > now() - interval '24 hours') AS signals_24h
            FROM public.signal_interpretations
            """,
        )
        if fresh:
            lsm = fresh.get("last_signal_min")
            radar_meta["last_signal_min"] = int(lsm) if lsm is not None else -1
            radar_meta["signals_24h"] = int(fresh.get("signals_24h") or 0)
    except Exception as exc:  # pragma: no cover
        logger.warning("unified-signals freshness query raised: %s", exc)

    return web.json_response({
        "ok": True,
        "count": len(result),
        "signals": result,
        "radar_meta": radar_meta,
    })


async def handle_traders_intel(request: web.Request) -> web.Response:
    """GET /api/traders-intel — enriched trader stats with coins, frequency, avg win/loss.

    Returns per-trader: win_rate, total_trades, total_pnl, avg_win, avg_loss,
    trade_frequency (trades/week), most_traded_coins (top 5), most_profitable_coins (top 5),
    plus handle/display_name from trader_profiles.
    """
    company = request.query.get("company") or None
    from shared.utils.companies import list_active_companies
    try:
        companies = await list_active_companies()
    except Exception:
        companies = []
    if company and company != "all":
        companies = [c for c in companies if c == company]

    pool = await get_shared_pool()

    # ── Per-trader aggregate stats ──
    agg_rows = await pool.fetch_all(
        """
        SELECT
            tp.actor_id,
            tp.company_id,
            COUNT(*) AS total_trades,
            COUNT(*) FILTER (WHERE tp.realized_pnl_usd > 0) AS wins,
            COUNT(*) FILTER (WHERE tp.realized_pnl_usd < 0) AS losses,
            COUNT(*) FILTER (WHERE tp.realized_pnl_usd = 0 AND tp.status NOT IN ('open','pending')) AS breakeven,
            SUM(COALESCE(tp.realized_pnl_usd, 0)) AS total_pnl,
            AVG(COALESCE(tp.realized_pnl_usd, 0)) FILTER (WHERE tp.realized_pnl_usd > 0) AS avg_win,
            AVG(COALESCE(tp.realized_pnl_usd, 0)) FILTER (WHERE tp.realized_pnl_usd < 0) AS avg_loss,
            MIN(tp.signal_timestamp) AS first_trade_ts,
            MAX(tp.signal_timestamp) AS last_trade_ts,
            MIN(tr.handle_normalized) AS handle_normalized,
            MIN(tr.display_name) AS display_name,
            MIN(tr.platform) AS platform,
            MIN(tr.trader_type) AS trader_type
        FROM public.tracked_positions tp
        LEFT JOIN public.trader_profiles tr ON tr.id = tp.trader_profile_id
        WHERE tp.actor_id IS NOT NULL
          AND tp.status NOT IN ('open', 'pending')
          AND tp.realized_pnl_usd IS NOT NULL
        GROUP BY tp.actor_id, tp.company_id
        ORDER BY total_pnl DESC
        LIMIT 100
        """
    )

    # ── Per-trader coin breakdown ──
    coin_rows = await pool.fetch_all(
        """
        SELECT
            tp.actor_id,
            tp.instrument_symbol,
            COUNT(*) AS trades,
            COUNT(*) FILTER (WHERE tp.realized_pnl_usd > 0) AS coin_wins,
            SUM(COALESCE(tp.realized_pnl_usd, 0)) AS coin_pnl
        FROM public.tracked_positions tp
        WHERE tp.actor_id IS NOT NULL
          AND tp.instrument_symbol IS NOT NULL
          AND tp.status NOT IN ('open', 'pending')
          AND tp.realized_pnl_usd IS NOT NULL
        GROUP BY tp.actor_id, tp.instrument_symbol
        ORDER BY trades DESC
        """
    )

    # Index coins by actor
    coins_by_actor: dict[str, list[dict]] = {}
    for cr in coin_rows:
        aid = cr["actor_id"]
        sym = cr["instrument_symbol"] or ""
        coins_by_actor.setdefault(aid, []).append({
            "symbol": sym,
            "trades": int(cr["trades"] or 0),
            "wins": int(cr["coin_wins"] or 0),
            "pnl": float(cr["coin_pnl"] or 0),
        })

    result: list[dict] = []
    now = datetime.now(timezone.utc)
    for r in agg_rows:
        total = int(r["total_trades"] or 0)
        w = int(r["wins"] or 0)
        l_ = int(r["losses"] or 0)
        actor = r["actor_id"] or ""
        win_rate = round(w / max(total, 1) * 100, 1)
        avg_w = float(r["avg_win"] or 0)
        avg_l = float(r["avg_loss"] or 0)
        total_pnl = float(r["total_pnl"] or 0)

        # Trade frequency: trades per week
        first = r["first_trade_ts"]
        last = r["last_trade_ts"]
        freq = 0.0
        if first and last and isinstance(first, datetime) and isinstance(last, datetime):
            span_days = max(1, (last - first).total_seconds() / 86400)
            freq = round(total / (span_days / 7), 1)

        # Coin breakdown
        coins = coins_by_actor.get(actor, [])
        most_traded = sorted(coins, key=lambda x: x["trades"], reverse=True)[:5]
        most_profitable = sorted(coins, key=lambda x: x["pnl"], reverse=True)[:5]

        result.append({
            "actor_id": actor,
            "company": r["company_id"] or "",
            "display_name": r["display_name"] or actor,
            "handle_normalized": r["handle_normalized"] or "",
            "platform": r["platform"] or "",
            "trader_type": r["trader_type"] or "",
            "total_trades": total,
            "wins": w,
            "losses": l_,
            "breakeven": int(r["breakeven"] or 0),
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "avg_win": round(avg_w, 2),
            "avg_loss": round(avg_l, 2),
            "trade_frequency_weekly": freq,
            "most_traded_coins": most_traded,
            "most_profitable_coins": most_profitable,
        })

    return _json({"ok": True, "traders": result})


# ---------------------------------------------------------------------------
# Exchange Accounts API (Phase Demo Bridge)
# ---------------------------------------------------------------------------

async def handle_exchange_accounts(request: web.Request) -> web.Response:
    """GET/POST /api/exchange-accounts — list all or add new."""
    pool = await get_shared_pool()
    
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            return _err(400, "invalid JSON body")
        
        exchange = (body.get("exchange") or "").strip().lower()
        account_name = (body.get("accountName") or "").strip()
        if not exchange or not account_name:
            return _err(400, "exchange and accountName required")
        
        api_key = body.get("apiKey", "")
        api_secret = body.get("apiSecret", "")
        if not api_key or not api_secret:
            return _err(400, "apiKey and apiSecret required")
        
        row = await pool.fetch_one(
            "SELECT id FROM public.exchange_accounts WHERE exchange = $1 AND account_name = $2",
            (exchange, account_name),
        )
        
        cols = ["exchange", "account_name", "account_type", "description", "api_key", "api_secret", "api_passphrase"]
        vals = {
            "exchange": exchange,
            "account_name": account_name,
            "account_type": body.get("accountType", "demo"),
            "description": body.get("description", ""),
            "api_key": api_key,
            "api_secret": api_secret,
            "api_passphrase": body.get("apiPassphrase", ""),
        }
        
        if row:
            await pool.execute(
                "UPDATE public.exchange_accounts SET exchange = %s, account_name = %s, account_type = %s, "
                "description = %s, api_key = %s, api_secret = %s, api_passphrase = %s, updated_at = NOW() WHERE id = %s",
                (exchange, account_name, body.get("accountType", "demo"), body.get("description", ""),
                 api_key, api_secret, body.get("apiPassphrase", ""), row["id"]),
            )
            return _json({"ok": True, "id": row["id"], "message": "Account updated."})
        else:
            new_id = await pool.fetch_val(
                "INSERT INTO public.exchange_accounts "
                "(exchange, account_name, account_type, description, api_key, api_secret, api_passphrase) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (exchange, account_name, body.get("accountType", "demo"), body.get("description", ""),
                 api_key, api_secret, body.get("apiPassphrase", "")),
            )
            return _json({"ok": True, "id": new_id, "message": "Account added."}, status=201)
    
    # GET — list all
    rows = await pool.fetch_all(
        "SELECT id, exchange, account_name, account_type, description, is_active, "
        "last_tested_at, last_balance, last_error, metadata, created_at, updated_at "
        "FROM public.exchange_accounts ORDER BY exchange, account_name"
    )
    accounts = []
    for r in rows:
        accounts.append({
            "id": r["id"],
            "exchange": r["exchange"],
            "accountName": r["account_name"],
            "accountType": r["account_type"],
            "description": r["description"],
            "isActive": r["is_active"],
            "lastTestedAt": r["last_tested_at"].isoformat() if r.get("last_tested_at") else None,
            "lastBalance": float(r["last_balance"]) if r.get("last_balance") else None,
            "lastError": r.get("last_error"),
            "marginUsagePct": float(r["metadata"].get("margin_usage_pct", 100)) if r.get("metadata") else 100.0,
            "marginMode": r["metadata"].get("margin_mode", "cross") if r.get("metadata") else "cross",
            "createdAt": r["created_at"].isoformat() if r.get("created_at") else None,
        })
    return _json({"ok": True, "accounts": accounts})


async def handle_exchange_account(request: web.Request) -> web.Response:
    """GET/PUT/DELETE /api/exchange-accounts/{id}."""
    account_id = int(request.match_info["id"])
    pool = await get_shared_pool()
    
    if request.method == "GET":
        r = await pool.fetch_one(
            "SELECT * FROM public.exchange_accounts WHERE id = $1", (account_id,)
        )
        if not r:
            return _err(404, f"Account #{account_id} not found")
        return _json({"ok": True, "account": {
            "id": r["id"], "exchange": r["exchange"], "accountName": r["account_name"],
            "accountType": r["account_type"], "description": r["description"],
            "apiKey": r["api_key"], "apiSecret": r["api_secret"],
            "apiPassphrase": r.get("api_passphrase") or "",
            "isActive": r["is_active"],
            "lastTestedAt": r["last_tested_at"].isoformat() if r.get("last_tested_at") else None,
            "lastBalance": float(r["last_balance"]) if r.get("last_balance") else None,
            "marginUsagePct": float(r["metadata"].get("margin_usage_pct", 100)) if r.get("metadata") else 100.0,
            "marginMode": r["metadata"].get("margin_mode", "cross") if r.get("metadata") else "cross",
        }})
    
    existing = await pool.fetch_one(
        "SELECT id FROM public.exchange_accounts WHERE id = $1", (account_id,)
    )
    if not existing:
        return _err(404, f"Account #{account_id} not found")
    
    if request.method == "DELETE":
        assigned = await pool.fetch_val(
            "SELECT COUNT(*) FROM public.competition_agent_exchanges WHERE exchange_account_id = $1 AND is_active = TRUE",
            (account_id,),
        )
        if assigned and int(assigned) > 0:
            return _err(409, f"Account is assigned to {assigned} active competition agents. Remove those first.")
        await pool.execute("DELETE FROM public.exchange_accounts WHERE id = $1", (account_id,))
        return _json({"ok": True, "message": "Account removed."})
    
    # PUT — update
    try:
        body = await request.json()
    except Exception:
        return _err(400, "invalid JSON body")
    
    sets = []
    params = []
    for col, field in [("description", "description"), ("account_type", "accountType"),
                        ("api_key", "apiKey"), ("api_secret", "apiSecret"),
                        ("api_passphrase", "apiPassphrase"), ("is_active", "isActive")]:
        if field in body and body[field] is not None:
            sets.append(f"{col} = %s")
            params.append(body[field])

    # Margin settings stored in metadata jsonb
    if "marginUsagePct" in body:
        try:
            pct = max(0.0, min(100.0, float(body["marginUsagePct"])))
            sets.append("metadata = jsonb_set(COALESCE(metadata,'{}'::jsonb), '{margin_usage_pct}', %s::text::jsonb)")
            params.append(str(pct))
        except (ValueError, TypeError):
            pass
    if "marginMode" in body:
        mode = str(body["marginMode"]).lower()
        if mode in ("cross", "isolated"):
            sets.append("metadata = jsonb_set(COALESCE(metadata,'{}'::jsonb), '{margin_mode}', %s::text::jsonb)")
            params.append(mode)
    
    if not sets:
        return _err(400, "No fields to update")
    
    sets.append("updated_at = NOW()")
    params.append(account_id)
    await pool.execute(
        "UPDATE public.exchange_accounts SET " + ", ".join(sets) + " WHERE id = %s",
        tuple(params),
    )
    return _json({"ok": True, "message": "Account updated."})


async def handle_exchange_account_test(request: web.Request) -> web.Response:
    """POST /api/exchange-accounts/{id}/test — test connectivity."""
    account_id = int(request.match_info["id"])
    
    try:
        from shared.mcp.tools.accounts import _handle_test_account
        result = await _handle_test_account({"id": account_id})
        return _json(result)
    except Exception as exc:
        return _err(500, str(exc))


async def handle_exchange_account_sync(request: web.Request) -> web.Response:
    """POST /api/exchange-accounts/{id}/sync-markets — sync perpetuals."""
    account_id = int(request.match_info["id"])
    try:
        from shared.mcp.tools.accounts import _handle_sync_markets
        result = await _handle_sync_markets({"id": account_id})
        return _json(result)
    except Exception as exc:
        return _err(500, str(exc))


async def handle_media(request: web.Request) -> web.Response:
    """GET /api/media/{id} — serve local chart image from media_items table."""
    import os, aiofiles
    try:
        media_id = int(request.match_info["id"])
    except Exception:
        return _err(400, "invalid id")
    pool = await get_shared_pool()
    row = await pool.fetch_one(
        "SELECT local_path, mime_type FROM public.media_items WHERE id = $1", (media_id,)
    )
    if not row or not row["local_path"]:
        return _err(404, f"media #{media_id} not available (no local file)")
    path = row["local_path"]
    if not os.path.exists(path):
        return _err(404, f"file missing on disk: {path}")
    mime = row.get("mime_type") or "image/jpeg"
    try:
        with open(path, "rb") as f:
            data = f.read()
        return web.Response(body=data, content_type=mime)
    except Exception as exc:
        return _err(500, str(exc))


async def _account_agent_map(pool) -> Dict[str, str]:
    """Reverse map: "{exchange}/{account_name}" → agent_id (the scenario the
    demo account mirrors). Used to attribute demo orders to their agent even
    when the demo_orders.agent_id column was written before this attribution
    existed (back-fill at read time)."""
    rows = await pool.fetch_all("""
        SELECT cae.agent_id, ea.exchange, ea.account_name
        FROM public.competition_agent_exchanges cae
        JOIN public.exchange_accounts ea ON ea.id = cae.exchange_account_id
        WHERE cae.is_active = TRUE AND ea.is_active = TRUE
          AND cae.competition_id = 'copy-trade-scenarios'
    """)
    return {f"{r['exchange']}/{r['account_name']}": r["agent_id"] for r in rows}


async def handle_strategies(request: web.Request) -> web.Response:
    """GET /api/strategies — graded technique/strategy ledger.

    Aggregates technique_stats with technique_catalog descriptions:
      * global rows (trader_handle='') => the strategy ledger
      * per-trader rows  => who uses it and who is best with it
    Returns win rate, avg RR, avg win%, avg loss%, total PnL, maturity
    (sample-count based), per-symbol breakdown and per-trader breakdown.
    """
    pool = await get_shared_pool()
    min_samples = max(int(request.query.get("min_samples", "1")), 1)
    rows = await pool.fetch_all("""
        SELECT ts.technique,
               COALESCE(tc.description, '') AS description,
               COALESCE(tc.category, '')    AS category,
               ts.trader_handle, ts.symbol_base, ts.timeframe,
               ts.wins, ts.losses, ts.total_pnl_usd, ts.sample_count,
               ts.sum_rr, ts.sum_win_pct, ts.sum_loss_pct,
               ts.last_outcome, ts.updated_at
        FROM public.technique_stats ts
        LEFT JOIN public.technique_catalog tc ON tc.technique = ts.technique
        WHERE ts.sample_count >= %s
        ORDER BY ts.technique, ts.trader_handle
    """, (min_samples,))

    strategies: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        t = r["technique"]
        if t not in strategies:
            strategies[t] = {
                "technique": t, "description": r["description"],
                "category": r["category"],
                "wins": 0, "losses": 0, "sample_count": 0,
                "total_pnl_usd": 0.0, "sum_rr": 0.0,
                "sum_win_pct": 0.0, "sum_loss_pct": 0.0,
                "traders": [], "symbols": [], "last_outcome": None,
                "updated_at": None,
            }
        s = strategies[t]
        is_global = (r["trader_handle"] or "") == ""
        if is_global:
            s["wins"] += int(r["wins"]); s["losses"] += int(r["losses"])
            s["sample_count"] += int(r["sample_count"])
            s["total_pnl_usd"] += float(r["total_pnl_usd"])
            s["sum_rr"] += float(r["sum_rr"] or 0)
            s["sum_win_pct"] += float(r["sum_win_pct"] or 0)
            s["sum_loss_pct"] += float(r["sum_loss_pct"] or 0)
            s["last_outcome"] = r["last_outcome"]
            s["updated_at"] = r["updated_at"].isoformat() if r["updated_at"] else None
        else:
            total = int(r["wins"]) + int(r["losses"])
            entry = {
                "trader": r["trader_handle"],
                "symbol": r["symbol_base"] or None,
                "timeframe": r["timeframe"] or None,
                "wins": int(r["wins"]), "losses": int(r["losses"]),
                "win_rate": round(int(r["wins"]) / max(total, 1), 3),
                "pnl_usd": float(r["total_pnl_usd"]),
                "samples": int(r["sample_count"]),
            }
            s["traders"].append(entry)
            if r["symbol_base"]:
                s["symbols"].append(r["symbol_base"])

    out = []
    for s in strategies.values():
        total = s["wins"] + s["losses"]
        n = s["sample_count"]
        s["win_rate"] = round(s["wins"] / max(total, 1), 3)
        s["avg_rr"] = round(s["sum_rr"] / max(n, 1), 2)
        s["avg_win_pct"] = round(s["sum_win_pct"] / max(s["wins"], 1), 2)
        s["avg_loss_pct"] = round(s["sum_loss_pct"] / max(s["losses"], 1), 2)
        s["total_pnl_usd"] = round(s["total_pnl_usd"], 2)
        # Maturity: how much evidence backs this strategy
        s["maturity"] = ("proven" if n >= 30 else "established" if n >= 10
                          else "emerging" if n >= 3 else "candidate")
        s["symbols"] = sorted(set(s["symbols"]))[:12]
        s["traders"].sort(key=lambda x: (-x["win_rate"], -x["samples"]))
        s["traders"] = s["traders"][:8]
        for k in ("sum_rr", "sum_win_pct", "sum_loss_pct"):
            s.pop(k, None)
        out.append(s)
    # Sort: maturity desc (by samples) then win rate
    out.sort(key=lambda s: (-s["sample_count"], -s["win_rate"]))
    return _json({"strategies": out, "count": len(out)})


async def handle_paper_vs_demo(request: web.Request) -> web.Response:
    """GET /api/paper-vs-demo — forensic paper-vs-demo(-vs-live) comparison.

    Returns per-order LINE ITEMS (not just counts): exchange, account, agent,
    symbol, direction, status, paper entry vs demo fill, slippage, fees, P&L,
    leverage, notional, timestamps and the FULL reject reason. Plus an
    accuracy block (how faithfully demo reproduces paper) and a reject-reason
    breakdown. The "live" lane is wired structurally (live_* fields) so that
    when a live account is mapped the same shape carries it — no schema change.
    """
    pool = await get_shared_pool()
    acct_agent = await _account_agent_map(pool)

    # Summary stats
    paper = await pool.fetch_one("""
        SELECT COUNT(*) as total, COUNT(*) FILTER (WHERE exit_price IS NOT NULL) as closed,
               COALESCE(SUM(pnl), 0) as pnl
        FROM public.competition_trades
        WHERE contest_id = 'copy-trade-scenarios'
    """)
    
    # Phase 1 (2026-05-29): added filled_notional + demo_pnl so the frontend
    # "Demo Orders $" KPI is real data instead of a hardcoded $0. Also counts
    # cancelled + sums all known fees for the forensic "total fees" KPI.
    demo = await pool.fetch_one("""
        SELECT COUNT(*) as total, COUNT(*) FILTER (WHERE status = 'filled') as filled,
               COUNT(*) FILTER (WHERE status = 'rejected') as rejected,
               COUNT(*) FILTER (WHERE status = 'pending') as pending,
               COUNT(*) FILTER (WHERE status = 'cancelled') as cancelled,
               COALESCE(SUM(notional_usd) FILTER (WHERE status = 'filled'), 0) as filled_notional,
               COALESCE(SUM(demo_pnl) FILTER (WHERE demo_pnl IS NOT NULL), 0) as demo_pnl,
               COALESCE(SUM(COALESCE(entry_fee,0)+COALESCE(exit_fee,0)+COALESCE(funding_fee,0)), 0) as total_fees
        FROM public.demo_orders
    """) or {"total": 0, "filled": 0, "rejected": 0, "pending": 0, "cancelled": 0,
             "filled_notional": 0, "demo_pnl": 0, "total_fees": 0}

    # Phase 1 (2026-05-29): DRIFT = (paper P&L − demo P&L) over MATCHED filled
    # pairs only (apples-to-apples). Comparing total paper P&L across all 388
    # historical trades against forward-only demo orders would be meaningless,
    # so we only sum trades that have BOTH a paper leg and a filled demo leg.
    # Returns NULL (frontend shows "—") until at least one demo order fills.
    drift_row = await pool.fetch_one("""
        SELECT COALESCE(SUM(ct.pnl), 0) AS paper_pnl_matched,
               COALESCE(SUM(dmo.demo_pnl), 0) AS demo_pnl_matched,
               COUNT(*) AS matched
        FROM public.competition_trades ct
        JOIN public.demo_orders dmo
          ON dmo.tracked_position_id = ct.tracked_position_id
        WHERE ct.contest_id = 'copy-trade-scenarios'
          AND dmo.status = 'filled'
          AND dmo.demo_pnl IS NOT NULL
    """) or {"paper_pnl_matched": 0, "demo_pnl_matched": 0, "matched": 0}
    drift = None
    if drift_row["matched"] and int(drift_row["matched"]) > 0:
        drift = float(drift_row["paper_pnl_matched"]) - float(drift_row["demo_pnl_matched"])

    # Joined comparison rows.
    # Phase 1 (2026-05-29) FIX: join demo_orders on tracked_position_id, NOT
    # competition_trade_id. The demo_bridge only ever writes tracked_position_id
    # (competition_trade_id is always NULL), so the old join matched zero rows
    # and the Demo column was permanently blank. Both competition_trades and
    # demo_orders reference the same upstream signal via tracked_position_id, so
    # that is the correct, stable join key.
    rows = await pool.fetch_all("""
        SELECT ct.id, ct.symbol, ct.direction, ct.entry_price as paper_entry,
               ct.exit_price as paper_exit, ct.sl_price, ct.tp_price,
               ct.pnl as paper_pnl, ct.allocated, ct.leverage, ct.agent_id,
               ct.entered_at, ct.exited_at, ct.exit_reason,
               dmo.exchange, dmo.account_name, dmo.exchange_order_id, dmo.agent_id as demo_agent_id,
               dmo.demo_entry, dmo.status as demo_status, dmo.error_message,
               dmo.demo_pnl, dmo.ordered_at, dmo.filled_at, dmo.slippage_entry,
               dmo.entry_fee, dmo.exit_fee, dmo.funding_fee, dmo.notional_usd as demo_notional,
               tp.actor_id, tp.instrument_symbol,
               si.id as signal_id
        FROM public.competition_trades ct
        LEFT JOIN public.demo_orders dmo ON dmo.tracked_position_id = ct.tracked_position_id
        LEFT JOIN public.tracked_positions tp ON tp.id = ct.tracked_position_id
        LEFT JOIN public.signal_interpretations si ON si.id = tp.signal_interpretation_id
        WHERE ct.contest_id = 'copy-trade-scenarios'
        ORDER BY ct.entered_at DESC
        LIMIT 200
    """)
    
    trades = []
    for r in rows:
        demo_acct_key = f"{r['exchange']}/{r['account_name']}" if r["exchange"] else None
        demo_agent = r["demo_agent_id"] or (acct_agent.get(demo_acct_key) if demo_acct_key else None)
        slip = float(r["slippage_entry"]) * 100 if r["slippage_entry"] is not None else None
        fees = sum(float(r[k]) for k in ("entry_fee", "exit_fee", "funding_fee") if r[k] is not None) or None
        trades.append({
            "id": r["id"], "symbol": r["symbol"], "direction": r["direction"],
            "agent_id": r["agent_id"], "actor_id": r["actor_id"],
            "demo_agent_id": demo_agent,
            "paper_entry": float(r["paper_entry"]) if r["paper_entry"] else None,
            "paper_exit": float(r["paper_exit"]) if r["paper_exit"] else None,
            "paper_pnl": float(r["paper_pnl"]) if r["paper_pnl"] else None,
            "sl": float(r["sl_price"]) if r["sl_price"] else None,
            "tp": float(r["tp_price"]) if r["tp_price"] else None,
            "leverage": int(r["leverage"]) if r["leverage"] else None,
            "allocated": float(r["allocated"]) if r["allocated"] else None,
            "entered_at": r["entered_at"].isoformat() if r["entered_at"] else None,
            "exited_at": r["exited_at"].isoformat() if r["exited_at"] else None,
            "exit_reason": r["exit_reason"],
            "demo_exchange": r["exchange"],
            "demo_account": r["account_name"],
            "demo_order_id": r["exchange_order_id"],
            "demo_entry": float(r["demo_entry"]) if r["demo_entry"] else None,
            "demo_notional": float(r["demo_notional"]) if r["demo_notional"] else None,
            "demo_status": r["demo_status"],
            "demo_error": r["error_message"],
            "demo_pnl": float(r["demo_pnl"]) if r["demo_pnl"] is not None else None,
            "slippage_pct": slip,
            "entry_fee": float(r["entry_fee"]) if r["entry_fee"] is not None else None,
            "exit_fee": float(r["exit_fee"]) if r["exit_fee"] is not None else None,
            "funding_fee": float(r["funding_fee"]) if r["funding_fee"] is not None else None,
            "fees_total": fees,
            "demo_ordered_at": r["ordered_at"].isoformat() if r["ordered_at"] else None,
            "demo_filled_at": r["filled_at"].isoformat() if r["filled_at"] else None,
            # live lane — structurally present, populated when a live account is mapped
            "live_exchange": None, "live_account": None, "live_entry": None,
            "live_status": None, "live_pnl": None, "live_slippage_pct": None,
            "signal_id": r["signal_id"],
            "orphan": False,
        })

    # Phase 1 (2026-05-29): surface ORPHAN demo orders — demo orders the bridge
    # placed for a signal that NO paper agent actually traded (e.g. the lone
    # "rejected" DOT order). With only a paper-driven join these were invisible,
    # which is exactly why "1 rejected" was a mystery. We list them with empty
    # paper columns so the operator can see *every* demo order and its reason.
    orphans = await pool.fetch_all("""
        SELECT dmo.id, dmo.symbol, dmo.direction, dmo.exchange, dmo.account_name,
               dmo.exchange_order_id, dmo.demo_entry, dmo.status, dmo.error_message,
               dmo.demo_pnl, dmo.paper_entry, dmo.paper_sl, dmo.paper_tp,
               dmo.leverage, dmo.ordered_at, dmo.filled_at, dmo.tracked_position_id,
               dmo.agent_id as demo_agent_id, dmo.slippage_entry,
               dmo.entry_fee, dmo.exit_fee, dmo.funding_fee, dmo.notional_usd as demo_notional
        FROM public.demo_orders dmo
        WHERE NOT EXISTS (
            SELECT 1 FROM public.competition_trades ct
            WHERE ct.tracked_position_id = dmo.tracked_position_id
              AND ct.contest_id = 'copy-trade-scenarios'
        )
        ORDER BY dmo.ordered_at DESC
        LIMIT 200
    """)
    for r in orphans:
        demo_acct_key = f"{r['exchange']}/{r['account_name']}" if r["exchange"] else None
        demo_agent = r["demo_agent_id"] or (acct_agent.get(demo_acct_key) if demo_acct_key else None)
        slip = float(r["slippage_entry"]) * 100 if r["slippage_entry"] is not None else None
        fees = sum(float(r[k]) for k in ("entry_fee", "exit_fee", "funding_fee") if r[k] is not None) or None
        trades.append({
            "id": f"demo-{r['id']}", "symbol": r["symbol"], "direction": r["direction"],
            "agent_id": None, "actor_id": None, "demo_agent_id": demo_agent,
            # paper leg is empty — no paper agent took this signal
            "paper_entry": float(r["paper_entry"]) if r["paper_entry"] else None,
            "paper_exit": None, "paper_pnl": None,
            "sl": float(r["paper_sl"]) if r["paper_sl"] else None,
            "tp": float(r["paper_tp"]) if r["paper_tp"] else None,
            "leverage": int(r["leverage"]) if r["leverage"] else None,
            "allocated": None, "entered_at": None, "exited_at": None, "exit_reason": None,
            "demo_exchange": r["exchange"], "demo_account": r["account_name"],
            "demo_order_id": r["exchange_order_id"],
            "demo_entry": float(r["demo_entry"]) if r["demo_entry"] else None,
            "demo_notional": float(r["demo_notional"]) if r["demo_notional"] else None,
            "demo_status": r["status"], "demo_error": r["error_message"],
            "demo_pnl": float(r["demo_pnl"]) if r["demo_pnl"] is not None else None,
            "slippage_pct": slip,
            "entry_fee": float(r["entry_fee"]) if r["entry_fee"] is not None else None,
            "exit_fee": float(r["exit_fee"]) if r["exit_fee"] is not None else None,
            "funding_fee": float(r["funding_fee"]) if r["funding_fee"] is not None else None,
            "fees_total": fees,
            "demo_ordered_at": r["ordered_at"].isoformat() if r["ordered_at"] else None,
            "demo_filled_at": r["filled_at"].isoformat() if r["filled_at"] else None,
            "live_exchange": None, "live_account": None, "live_entry": None,
            "live_status": None, "live_pnl": None, "live_slippage_pct": None,
            "signal_id": None,
            "orphan": True,
        })

    # Reject-reason breakdown (so the operator sees WHY, grouped). The raw
    # exchange messages embed a requestTime timestamp so they're all unique —
    # we must classify into a human cause and aggregate in Python, keeping one
    # sample raw message per cause for drill-down.
    rej_rows = await pool.fetch_all("""
        SELECT COALESCE(error_message, 'unknown') as reason
        FROM public.demo_orders WHERE status = 'rejected'
    """)
    _rej_agg: Dict[str, Dict[str, Any]] = {}
    for r in rej_rows:
        cause = _classify_reject(r["reason"])
        slot = _rej_agg.setdefault(cause, {"reason": cause, "count": 0, "raw": (r["reason"] or "")[:200]})
        slot["count"] += 1
    reject_summary = sorted(_rej_agg.values(), key=lambda x: -x["count"])

    # Accuracy block — how faithfully demo reproduces paper, on MATCHED filled
    # pairs (both legs have an entry price). entry_accuracy = 100 − mean(|slip|%).
    acc_rows = await pool.fetch_all("""
        SELECT dmo.slippage_entry
        FROM public.demo_orders dmo
        WHERE dmo.status = 'filled' AND dmo.demo_entry IS NOT NULL
          AND dmo.slippage_entry IS NOT NULL
    """)
    matched_filled = len(acc_rows)
    mean_slip_pct = None
    entry_accuracy = None
    if matched_filled:
        mean_slip_pct = sum(abs(float(r["slippage_entry"])) for r in acc_rows) / matched_filled * 100
        entry_accuracy = max(0.0, 100.0 - mean_slip_pct)
    placed = (demo["filled"] or 0) + (demo["pending"] or 0) + (demo["cancelled"] or 0)
    fill_rate = (float(demo["filled"]) / placed * 100) if placed else None
    accuracy = {
        "entry_accuracy_pct": round(entry_accuracy, 4) if entry_accuracy is not None else None,
        "mean_slippage_pct": round(mean_slip_pct, 4) if mean_slip_pct is not None else None,
        "matched_filled": matched_filled,
        "fill_rate_pct": round(fill_rate, 1) if fill_rate is not None else None,
        "score": round(entry_accuracy, 2) if entry_accuracy is not None else None,
        "note": ("No demo orders have filled yet — accuracy becomes available "
                 "once a resting demo limit order is hit." if not matched_filled else None),
    }

    return _json({
        "ok": True,
        "summary": {
            "paper_total": paper["total"], "paper_closed": paper["closed"],
            "paper_pnl": float(paper["pnl"] or 0),
            "demo_total": demo["total"], "demo_filled": demo["filled"],
            "demo_rejected": demo["rejected"], "demo_pending": demo["pending"],
            "demo_cancelled": demo.get("cancelled", 0),
            # Phase 1 (2026-05-29): real demo $ + drift (were hardcoded stubs)
            "demo_notional": float(demo["filled_notional"] or 0),
            "demo_pnl": float(demo["demo_pnl"] or 0),
            "demo_fees": float(demo.get("total_fees") or 0),
            "drift": drift,
            # live lane totals (zero until a live account is mapped)
            "live_total": 0, "live_filled": 0, "live_pnl": 0.0,
        },
        "accuracy": accuracy,
        "reject_summary": reject_summary,
        "trades": trades,
    })


def _classify_reject(msg: Optional[str]) -> str:
    """Turn a raw exchange error string into a short human-readable cause."""
    m = (msg or "").lower()
    if "110007" in m or "ab not enough" in m or "not enough" in m or "insufficient" in m:
        return "Insufficient margin / balance"
    if "40774" in m or "unilateral position" in m or "position mode" in m:
        return "Position-mode mismatch (one-way vs hedge)"
    if "leverage" in m:
        return "Leverage rejected by exchange"
    if "min" in m and ("qty" in m or "notional" in m or "amount" in m):
        return "Below exchange minimum size"
    if "symbol" in m or "not found" in m or "does not exist" in m:
        return "Symbol not tradable on this exchange"
    if not msg:
        return "Unknown"
    return "Other exchange rejection"


async def handle_paper_demo_log(request: web.Request) -> web.Response:
    """GET /api/paper-demo/log?lines=N&event=mirror_placed — tail the forensic log.

    Reads the append-only JSON-lines transaction log written by the demo bridge
    (/opt/tickles/shared/logs/paper_demo.log). This is the "check the log"
    surface the operator asked for — every mirror placement, rejection, fill,
    and cancel, newest first.
    """
    try:
        n = int(request.query.get("lines", "200") or 200)
    except (TypeError, ValueError):
        n = 200
    ev = request.query.get("event") or None
    try:
        from shared.daemons.demo_forensic_log import read_tail, FORENSIC_LOG_PATH
        events = read_tail(n, ev)
        return _json({"ok": True, "path": FORENSIC_LOG_PATH, "count": len(events),
                      "events": events})
    except Exception as exc:
        return _json({"ok": True, "events": [], "error": str(exc)[:200]})


async def handle_paper_demo_exchange_state(request: web.Request) -> web.Response:
    """GET /api/paper-demo/exchange-state — LIVE pull from each demo/live account.

    On-demand (button-triggered, NOT auto-refreshed because it hits the
    exchange network): for every active demo/live account, fetch the wallet
    balance, open positions, and resting open orders straight from the
    exchange. This is the "what is ACTUALLY on the exchange right now" view so
    the operator can reconcile it against what our demo_orders table thinks.
    """
    pool = await get_shared_pool()
    acct_agent = await _account_agent_map(pool)
    accts = await pool.fetch_all(
        "SELECT exchange, account_name, account_type FROM public.exchange_accounts "
        "WHERE is_active = TRUE AND account_type IN ('demo','live') "
        "ORDER BY account_type, exchange, account_name")
    try:
        from shared.execution.ccxt_adapter import CcxtExecutionAdapter
    except Exception as exc:
        return _json({"ok": False, "error": f"ccxt unavailable: {exc}"})

    out = []
    for a in accts:
        ex, name, atype = a["exchange"], a["account_name"], a["account_type"]
        entry = {"exchange": ex, "account": name, "type": atype,
                 "agent": acct_agent.get(f"{ex}/{name}"),
                 "balance": None, "positions": [], "open_orders": [], "error": None}
        try:
            adapter = CcxtExecutionAdapter(demo_trading=(atype == "demo"))
            bal = await asyncio.wait_for(
                adapter.fetch_balance(exchange=ex, account_name=name), timeout=15)
            entry["balance"] = bal.get("USDT") if bal else None
            entry["positions"] = await asyncio.wait_for(
                adapter.fetch_positions(exchange=ex, account_name=name), timeout=15)
            client = adapter._get_client(ex, name)
            raw_orders = await asyncio.wait_for(
                asyncio.to_thread(client.fetch_open_orders), timeout=15)
            entry["open_orders"] = [{
                "symbol": o.get("symbol"), "side": o.get("side"), "type": o.get("type"),
                "price": float(o["price"]) if o.get("price") is not None else None,
                "amount": float(o["amount"]) if o.get("amount") is not None else None,
                "status": o.get("status"), "id": o.get("id"),
            } for o in (raw_orders or [])]
        except Exception as exc:
            entry["error"] = str(exc)[:200]
        out.append(entry)
    return _json({"ok": True, "accounts": out})


async def handle_mirror_config(request: web.Request) -> web.Response:
    """GET/POST/DELETE /api/mirror-config — agent to demo account assignments."""
    pool = await get_shared_pool()
    if request.method == "GET":
        # Return ALL contest agents, including unassigned ones
        rows = await pool.fetch_all("""
            SELECT cp.agent_id,
                   ea.account_name, ea.id as account_id, ea.exchange,
                   cae.id IS NOT NULL as is_assigned
            FROM public.contest_participants cp
            LEFT JOIN public.competition_agent_exchanges cae
                ON cae.agent_id = cp.agent_id AND cae.is_active = TRUE
            LEFT JOIN public.exchange_accounts ea
                ON ea.id = cae.exchange_account_id AND ea.is_active = TRUE
            WHERE cp.contest_id = 'copy-trade-scenarios'
            ORDER BY cp.agent_id, cae.priority
        """)
        return _json({"ok": True, "mappings": [
            {"agent_id": r["agent_id"], "account_name": r["account_name"],
             "account_id": r["account_id"], "exchange": r["exchange"],
             "is_assigned": r["is_assigned"]}
            for r in rows
        ]})
    if request.method == "DELETE":
        cid = request.query.get("competitionId", "copy-trade-scenarios")
        aid = request.query.get("agentId", "")
        acid = request.query.get("exchangeAccountId", "")
        await pool.execute(
            "UPDATE public.competition_agent_exchanges SET is_active = FALSE "
            "WHERE competition_id = $1 AND agent_id = $2 AND exchange_account_id = $3",
            (cid, aid, int(acid)))
        return _json({"ok": True})
    try: body = await request.json()
    except Exception: return _err(400, "invalid JSON")
    await pool.execute(
        "INSERT INTO public.competition_agent_exchanges "
        "(competition_id, agent_id, exchange_account_id, priority, is_active) "
        "VALUES ($1,$2,$3,$4,TRUE) ON CONFLICT (competition_id, agent_id, exchange_account_id) "
        "DO UPDATE SET priority=$4, is_active=TRUE",
        (body.get("competitionId","copy-trade-scenarios"), body.get("agentId",""),
         int(body.get("exchangeAccountId",0)), int(body.get("priority",0))))
    return _json({"ok": True, "message": "Assigned"})


def attach_routes(app: web.Application, *, prefix: str = "") -> None:
    app.router.add_get(prefix + "/api/candles", handle_candles)
    app.router.add_get(prefix + "/api/signal-replay", handle_signal_replay)
    app.router.add_get(prefix + "/api/entry-radar", handle_entry_radar)
    app.router.add_get(prefix + "/api/unified-signals", handle_unified_signals)
    app.router.add_get(prefix + "/api/competition-agent", handle_competition_agent)
    app.router.add_get(prefix + "/api/traders-intel", handle_traders_intel)
    # Exchange accounts (Phase Demo Bridge)
    app.router.add_get(prefix + "/api/media/{id}", handle_media)
    app.router.add_get(prefix + "/api/strategies", handle_strategies)
    app.router.add_get(prefix + "/api/paper-vs-demo", handle_paper_vs_demo)
    app.router.add_get(prefix + "/api/paper-demo/log", handle_paper_demo_log)
    app.router.add_get(prefix + "/api/paper-demo/exchange-state", handle_paper_demo_exchange_state)
    app.router.add_get(prefix + "/api/exchange-accounts", handle_exchange_accounts)
    app.router.add_post(prefix + "/api/exchange-accounts", handle_exchange_accounts)
    app.router.add_get(prefix + "/api/exchange-accounts/{id}", handle_exchange_account)
    app.router.add_put(prefix + "/api/exchange-accounts/{id}", handle_exchange_account)
    app.router.add_delete(prefix + "/api/exchange-accounts/{id}", handle_exchange_account)
    app.router.add_post(prefix + "/api/exchange-accounts/{id}/test", handle_exchange_account_test)
    app.router.add_post(prefix + "/api/exchange-accounts/{id}/sync-markets", handle_exchange_account_sync)
    # Mirror config
    app.router.add_get(prefix + "/api/mirror-config", handle_mirror_config)
    app.router.add_post(prefix + "/api/mirror-config", handle_mirror_config)
    app.router.add_delete(prefix + "/api/mirror-config", handle_mirror_config)
