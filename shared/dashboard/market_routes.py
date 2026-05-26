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
_ALLOWED_TF = {"1m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "1d", "3d", "1w"}
_TF_ALIASES = {"1M":"1m","5M":"5m","15M":"15m","30M":"30m","1H":"1h","2H":"2h","4H":"4h","8H":"8h","1D":"1d","3D":"3d","1W":"1w","D":"1d","W":"1w"}
_TF_SECONDS = {"1m":60,"5m":300,"15m":900,"30m":1800,"1h":3600,"2h":7200,"4h":14400,"8h":28800,"1d":86400,"3d":259200,"1w":604800}
_NATIVE_TF = {"1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"}
_AGGREGATE_TF = {"2h": ("1h", 2), "8h": ("4h", 2), "3d": ("1d", 3)}


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
    post_bars = {"1m":360,"5m":288,"15m":240,"30m":220,"1h":200,"2h":180,"4h":160,"8h":140,"1d":120,"3d":80,"1w":60}.get(timeframe, 240)
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
        base = await _fetch_native_candles(
            symbol=symbol, exchange=exchange, timeframe=base_tf,
            start=start, end=end, limit=min(limit * ratio + ratio * 4, 3000),
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


async def handle_signal_replay(request: web.Request) -> web.Response:
    """GET /api/signal-replay?id=<signal_interpretation_id>

    Returns a joined signal/position/news/media payload plus 1m candles around
    the call and position update points where available.
    """
    try:
        interp_id = int(request.query.get("id", "0"))
    except ValueError:
        interp_id = 0
    if interp_id <= 0:
        return _err(400, "id is required")

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
          ni.headline AS news_headline, ni.content AS news_content, ni.source AS news_source,
          ni.author AS news_author, ni.channel_name AS news_channel_name,
          ni.published_at AS news_published_at, ni.collected_at AS news_collected_at,
          mi.id AS media_id, mi.local_path AS media_local_path, mi.source_url AS media_source_url,
          mi.thumbnail_path AS media_thumbnail_path, mi.media_type AS media_type, mi.mime_type AS mime_type,
          tp.id AS position_id, tp.status AS position_status, tp.current_price,
          tp.signal_timestamp, tp.created_at AS opened_at, tp.closed_at, tp.exit_timestamp,
          tp.unrealized_pnl_usd, tp.realized_pnl_usd, tp.realized_pnl_usd_final,
          tp.unrealized_pnl_pct, tp.realized_pnl_pct, tp.outcome, tp.exit_reason,
          tp.max_drawdown_pct, tp.max_profit_pct, tp.distance_to_entry_pct,
          tp.distance_to_sl_pct, tp.distance_to_tp1_pct,
          tr.handle_raw, tr.handle_normalized, tr.display_name, tr.platform, tr.trader_type
        FROM public.signal_interpretations si
        LEFT JOIN public.news_items ni ON ni.id = si.news_item_id
        LEFT JOIN public.media_items mi ON mi.id = si.media_item_id
        LEFT JOIN public.tracked_positions tp ON tp.signal_interpretation_id = si.id
        LEFT JOIN public.trader_profiles tr ON tr.id = si.trader_profile_id
        WHERE si.id = $1
        ORDER BY tp.id DESC NULLS LAST
        LIMIT 1
        """,
        (interp_id,),
    )
    if not row:
        return _err(404, "signal not found")

    symbol = _clean_symbol(row.get("instrument_symbol")) or "BTC/USDT"
    exchange = (row.get("instrument_exchange") or row.get("exchange") or "bybit").lower()
    call_ts = row.get("signal_timestamp") or row.get("news_published_at") or row.get("created_at")
    if call_ts is None:
        call_ts = datetime.now(timezone.utc)
    if call_ts.tzinfo is None:
        call_ts = call_ts.replace(tzinfo=timezone.utc)
    end_ts = row.get("closed_at") or row.get("exit_timestamp") or datetime.now(timezone.utc)
    if end_ts.tzinfo is None:
        end_ts = end_ts.replace(tzinfo=timezone.utc)
    chart_timeframe = _normalise_timeframe(row.get("timeframe") or "1m")
    start, end, candle_limit = _timeframe_window(call_ts, end_ts, chart_timeframe)

    candles = await _fetch_candles(
        symbol=symbol,
        exchange=exchange,
        timeframe=chart_timeframe,
        start=start,
        end=end,
        limit=candle_limit,
    )
    updates: list[dict[str, Any]] = []
    if row.get("position_id"):
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
            (int(row["position_id"]),),
        )

    levels = {
        "entry": row.get("entry_price"),
        "stop_loss": row.get("stop_loss"),
        "take_profit_1": row.get("take_profit_1"),
        "take_profit_2": row.get("take_profit_2"),
        "take_profit_3": row.get("take_profit_3"),
        "take_profit_4": row.get("take_profit_4"),
        "take_profit_5": row.get("take_profit_5"),
        "take_profit_6": row.get("take_profit_6"),
    }
    payload = {
        "ok": True,
        "id": interp_id,
        "symbol": symbol,
        "exchange": exchange,
        "timeframe": chart_timeframe,
        "timeframe_source": row.get("timeframe"),
        "candle_seconds": _TF_SECONDS.get(chart_timeframe, 60),
        "call_ts": call_ts,
        "window": {"start": start, "end": end},
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
            "direction": row.get("consensus_direction"),
            "confidence": row.get("consensus_confidence"),
            "method": row.get("consensus_method"),
            "llm_direction": row.get("llm_direction"),
            "llm_confidence": row.get("llm_confidence"),
            "llm_reasoning": row.get("llm_reasoning"),
            "quant_direction": row.get("quant_direction"),
            "quant_confidence": row.get("quant_confidence"),
            "trader_stated_thesis": row.get("trader_stated_thesis"),
            "llm_inferred_thesis": row.get("llm_inferred_thesis"),
            "reason_agreement_score": row.get("reason_agreement_score"),
            "ai_agreement_score": row.get("ai_agreement_score"),
            "ai_comment": row.get("ai_comment"),
            "levels": levels,
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
        "position": {
            "id": row.get("position_id"),
            "status": row.get("position_status"),
            "current_price": row.get("current_price"),
            "opened_at": row.get("opened_at"),
            "closed_at": row.get("closed_at"),
            "outcome": row.get("outcome"),
            "exit_reason": row.get("exit_reason"),
            "unrealized_pnl_usd": row.get("unrealized_pnl_usd"),
            "realized_pnl_usd": row.get("realized_pnl_usd"),
            "realized_pnl_usd_final": row.get("realized_pnl_usd_final"),
            "unrealized_pnl_pct": row.get("unrealized_pnl_pct"),
            "realized_pnl_pct": row.get("realized_pnl_pct"),
            "max_drawdown_pct": row.get("max_drawdown_pct"),
            "max_profit_pct": row.get("max_profit_pct"),
            "distance_to_entry_pct": row.get("distance_to_entry_pct"),
            "distance_to_sl_pct": row.get("distance_to_sl_pct"),
            "distance_to_tp1_pct": row.get("distance_to_tp1_pct"),
        },
        "candles": candles,
        "position_updates": updates,
        "coverage": {"candle_count": len(candles), "update_count": len(updates)},
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
    dist = abs((last_close - entry_f) / entry_f * 100.0) if entry_f else None
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


async def _enrich_radar_with_live_prices(rows: list[dict[str, Any]]) -> int:
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
        ex = (r.get("instrument_exchange") or "bybit").lower()
        if not sym:
            continue
        by_pair.setdefault((str(ex), str(sym)), []).append(idx)

    if not by_pair:
        return 0

    sem = asyncio.Semaphore(_RADAR_LIVE_PRICE_CONCURRENCY)
    pairs = list(by_pair.items())
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
               tp.signal_timestamp, tp.current_price AS tp_current_price,
               tp.distance_to_entry_pct AS tp_distance_to_entry_pct,
               tp.actor_id, tp.signal_source, tp.detection_method
        FROM public.signal_interpretations si
        LEFT JOIN public.tracked_positions tp ON tp.signal_interpretation_id = si.id
        LEFT JOIN public.news_items ni ON ni.id = si.news_item_id
        LEFT JOIN public.trader_profiles tr ON tr.id = si.trader_profile_id
        LEFT JOIN public.media_items mi ON mi.id = si.media_item_id
        WHERE si.created_at > now() - interval '30 days'
        """ + status_filter_sql + f" ORDER BY si.id, si.created_at DESC LIMIT {limit_sql}",
        (),
    )

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
            distance_to_entry = round(abs((current - entry) / entry * 100), 2)

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

    return web.json_response({
        "ok": True,
        "count": len(result),
        "signals": result,
    })


def attach_routes(app: web.Application, *, prefix: str = "") -> None:
    app.router.add_get(prefix + "/api/candles", handle_candles)
    app.router.add_get(prefix + "/api/signal-replay", handle_signal_replay)
    app.router.add_get(prefix + "/api/entry-radar", handle_entry_radar)
    app.router.add_get(prefix + "/api/unified-signals", handle_unified_signals)
    app.router.add_get(prefix + "/api/competition-agent", handle_competition_agent)
