"""Read-only market/replay routes for the Tickles dashboard.

These routes deliberately read from Postgres only. They expose candle and
signal-replay data to the browser so the dashboard can reconstruct TradingView-
style call paths without going through CCXT from the browser.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional
from urllib.parse import quote

from aiohttp import web

from shared.utils.db import get_shared_pool

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
    return start, end, limit

def _media_url(media_id: Any, local_path: Any, source_url: Any) -> Optional[str]:
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
                        
                        # Re-fetch from DB to ensure identical schema and filtering mapping
                        rows = await pool.fetch_all(
                            """
                            SELECT c.timestamp, c.open, c.high, c.low, c.close, c.volume,
                                   c.is_fake, $7::text as symbol, $8::text as exchange
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
    """GET /api/candles?symbol=BTC/USDT&timeframe=1m&limit=500"""
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
        now = datetime.now(timezone.utc)
        candles = await _fetch_candles(
            symbol=symbol,
            exchange=(row.get("instrument_exchange") or "bybit").lower(),
            timeframe=tf,
            start=call_ts,
            end=now,
            limit=260,
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
    actionable.sort(key=lambda r: (
        r.get("distance_to_entry_pct") is None,
        abs(float(r.get("distance_to_entry_pct") or 999999)),
        r.get("signal_timestamp") or r.get("created_at"),
    ))
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

    # Enrich open positions with live price and unrealized P&L
    if open_raw:
        symbols = list({p["symbol"] for p in open_raw if p.get("symbol")})
        live_prices: dict[str, float] = {}
        if symbols:
            rows = await pool.fetch_all(
                """
                SELECT DISTINCT ON (i.symbol) i.symbol, c.close
                FROM public.candles c
                JOIN public.instruments i ON i.id = c.instrument_id
                WHERE i.symbol = ANY($1) AND c.timeframe::text = '1m'
                ORDER BY i.symbol, c.timestamp DESC
                """,
                (symbols,),
            )
            for r in rows:
                live_prices[r["symbol"]] = float(r["close"])

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

def attach_routes(app: web.Application, *, prefix: str = "") -> None:
    app.router.add_get(prefix + "/api/candles", handle_candles)
    app.router.add_get(prefix + "/api/signal-replay", handle_signal_replay)
    app.router.add_get(prefix + "/api/entry-radar", handle_entry_radar)
    app.router.add_get(prefix + "/api/competition-agent", handle_competition_agent)
