"""
Module: critic_context
Purpose: Phase A/B enrichment for chart_hacker critic opinions — live quant,
         at-entry interpretation snapshot, funding, timeframe RSI, trader stats.
Location: /opt/tickles/shared/intelligence/critic_context.py
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Postgres timeframe_t enum — aggregated TFs (2h/6h/8h) are mapped to these.
_CANDLE_TF = frozenset({"1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"})
_TF_ALIASES = {
    "2h": "1h",
    "3h": "1h",
    "6h": "4h",
    "8h": "4h",
    "12h": "4h",
    "3d": "1d",
}
_TRADE_TYPE_TF = {
    "scalp": "15m",
    "intraday": "1h",
    "swing": "4h",
    "position": "1d",
    "degen": "1h",
}
_TF_RSI_FALLBACKS = ("4h", "1h", "15m", "1m")
_AI_COMMENT_MAX = 300
_CHART_ANALYSIS_KEYS = ("market_structure", "indicators", "patterns", "key_levels")


def normalize_candle_timeframe(tf: Optional[str], default: str = "4h") -> str:
    """Map chart/position timeframe labels to a Postgres ``timeframe_t`` value."""
    raw = str(tf or "").strip().lower()
    if not raw:
        return default
    if raw in _CANDLE_TF:
        return raw
    return _TF_ALIASES.get(raw, default)


def resolve_critic_timeframe(
    position_tf: Optional[str],
    interp_tf: Optional[str],
    trade_type: Optional[str],
) -> str:
    """Pick a candle timeframe for indicator preview (DB-safe enum)."""
    for raw in (position_tf, interp_tf):
        if raw:
            tf = normalize_candle_timeframe(raw, default="")
            if tf:
                return tf
    tt = (trade_type or "").strip().lower()
    return _TRADE_TYPE_TF.get(tt, "4h")


def resolve_critic_symbol(raw: Optional[str]) -> str:
    """Normalize instrument symbol to canonical slash form for candle reads."""
    if not raw:
        return ""
    try:
        from shared.dashboard.market_routes import _resolve_candle_symbol
        from shared.utils.instrument_normaliser import to_canonical_symbol

        stripped = str(raw).strip()
        canon = to_canonical_symbol(stripped) or stripped
        return _resolve_candle_symbol(canon, canon)
    except Exception:
        return str(raw).strip()


def trim_chart_analysis(chart_analysis: Any) -> Optional[Dict[str, Any]]:
    """Keep a compact chart_analysis slice for the critic prompt."""
    if not isinstance(chart_analysis, dict):
        return None
    out = {k: chart_analysis[k] for k in _CHART_ANALYSIS_KEYS if k in chart_analysis}
    return out or None


def serialize_quant(quant: Any) -> Optional[Dict[str, Any]]:
    """Convert QuantResult to a JSON-safe dict."""
    if quant is None:
        return None
    indicators = getattr(quant, "indicators", None) or {}
    return {
        "direction": getattr(quant, "direction", "unclear"),
        "confidence": round(float(getattr(quant, "confidence", 0.0) or 0.0), 4),
        "indicators": indicators,
    }


def build_at_entry_snapshot(row: Any) -> Optional[Dict[str, Any]]:
    """Build at-entry interpretation snapshot from a joined SQL row."""
    if row is None or not row.get("interp_id"):
        return None

    quant_indicators = row.get("quant_indicators")
    if isinstance(quant_indicators, str):
        try:
            import json

            quant_indicators = json.loads(quant_indicators)
        except Exception:
            quant_indicators = None

    chart_analysis = row.get("chart_analysis")
    if isinstance(chart_analysis, str):
        try:
            import json

            chart_analysis = json.loads(chart_analysis)
        except Exception:
            chart_analysis = None

    ai_comment = (row.get("ai_comment") or "").strip()
    if len(ai_comment) > _AI_COMMENT_MAX:
        ai_comment = ai_comment[: _AI_COMMENT_MAX - 3] + "..."

    snap: Dict[str, Any] = {
        "interpretation_id": int(row["interp_id"]),
        "timeframe": row.get("interp_timeframe"),
        "quant": {
            "direction": row.get("quant_direction"),
            "confidence": _float_or_none(row.get("quant_confidence")),
            "indicators": quant_indicators,
        },
        "consensus": {
            "direction": row.get("consensus_direction"),
            "confidence": _float_or_none(row.get("consensus_confidence")),
            "method": row.get("consensus_method"),
        },
        "ai_agreement_score": _float_or_none(row.get("ai_agreement_score")),
        "ai_comment": ai_comment or None,
        "chart_analysis": trim_chart_analysis(chart_analysis),
    }
    return snap


def _float_or_none(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        return round(float(val), 4)
    except (TypeError, ValueError):
        return None


async def fetch_interpretation_row(pool: Any, signal_interpretation_id: Optional[int]) -> Optional[Any]:
    """Load at-entry interpretation fields for critic enrichment."""
    if not signal_interpretation_id:
        return None
    try:
        return await pool.fetch_one(
            """
            SELECT
                si.id AS interp_id,
                si.timeframe AS interp_timeframe,
                si.quant_direction,
                si.quant_confidence,
                si.quant_indicators,
                si.consensus_direction,
                si.consensus_confidence,
                si.consensus_method,
                si.ai_agreement_score,
                si.ai_comment,
                si.chart_analysis
            FROM public.signal_interpretations si
            WHERE si.id = $1
            """,
            (int(signal_interpretation_id),),
        )
    except Exception as exc:
        logger.warning(
            "critic_context: interpretation fetch failed id=%s: %s",
            signal_interpretation_id,
            exc,
        )
        return None


async def fetch_quant_now(pool: Any, symbol: str, exchange: str) -> Optional[Dict[str, Any]]:
    """Run live 1m quant track (RSI/EMA/ATR/Bollinger ensemble)."""
    symbol = resolve_critic_symbol(symbol) or symbol
    if not symbol or symbol == "UNKNOWN":
        return None
    try:
        from shared.intelligence.interpretation_service import (
            InterpretationConfig,
            run_quant_track,
        )

        cfg = InterpretationConfig()
        quant = await run_quant_track(
            pool,
            pool,
            symbol,
            exchange or "bybit",
            cfg.freshness_threshold_s,
        )
        return serialize_quant(quant)
    except Exception as exc:
        logger.warning("critic_context: quant_now failed symbol=%s: %s", symbol, exc)
        return None


async def fetch_funding(pool: Any, symbol: str, exchange: str) -> Optional[Dict[str, Any]]:
    """Latest funding rate + open interest from derivatives_snapshots."""
    symbol = resolve_critic_symbol(symbol) or symbol
    if not symbol:
        return None
    exch = (exchange or "bybit").strip().lower()
    try:
        from shared.intelligence.interpretation_service import _quant_symbol_candidates

        instrument_id: Optional[int] = None
        for cand in _quant_symbol_candidates(symbol):
            row = await pool.fetch_one(
                "SELECT id FROM public.instruments WHERE symbol = $1 AND exchange = $2",
                (cand, exch),
            )
            if row:
                instrument_id = int(row["id"])
                break
        if instrument_id is None:
            return None

        snap = await pool.fetch_one(
            """
            SELECT snapshot_at, funding_rate, open_interest, source
            FROM public.derivatives_snapshots
            WHERE instrument_id = $1
            ORDER BY snapshot_at DESC
            LIMIT 1
            """,
            (instrument_id,),
        )
        if not snap:
            return None

        ts = snap.get("snapshot_at")
        return {
            "funding_rate": _float_or_none(snap.get("funding_rate")),
            "open_interest": _float_or_none(snap.get("open_interest")),
            "snapshot_at": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            "source": snap.get("source"),
        }
    except Exception as exc:
        logger.warning("critic_context: funding fetch failed symbol=%s: %s", symbol, exc)
        return None


def _fetch_tf_rsi_sync(symbol: str, venue: str, timeframe: str) -> Optional[Dict[str, Any]]:
    """RSI(14) last value on the position timeframe (sync — runs in thread)."""
    from shared.mcp.tools.backtest import _compute_preview

    tf = normalize_candle_timeframe(timeframe)
    preview = _compute_preview(
        name="rsi",
        params={"period": 14},
        symbol=symbol,
        venue=venue,
        timeframe=tf,
        window=200,
    )
    if preview.get("status") != "ok":
        return None
    values: List[Dict[str, Any]] = preview.get("values") or []
    if not values:
        return None
    last = values[-1]
    return {
        "timeframe": tf,
        "rsi14": last.get("value"),
        "as_of": last.get("timestamp"),
    }


async def fetch_timeframe_rsi(
    symbol: str,
    venue: str,
    timeframe: str,
    *,
    position_tf: Optional[str] = None,
    interp_tf: Optional[str] = None,
    trade_type: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Async wrapper for timeframe-matched RSI preview with DB-safe TF + fallbacks."""
    canon = resolve_critic_symbol(symbol)
    if not canon:
        return None
    requested = resolve_critic_timeframe(
        position_tf or timeframe, interp_tf, trade_type,
    )
    tried: set[str] = set()
    candidates = [normalize_candle_timeframe(requested)]
    for fb in _TF_RSI_FALLBACKS:
        if fb not in candidates:
            candidates.append(fb)
    for tf in candidates:
        if tf in tried:
            continue
        tried.add(tf)
        try:
            result = await asyncio.to_thread(
                _fetch_tf_rsi_sync, canon, venue or "bybit", tf,
            )
        except Exception as exc:
            logger.warning(
                "critic_context: tf_rsi failed symbol=%s tf=%s: %s", symbol, tf, exc,
            )
            continue
        if result:
            result["requested_timeframe"] = requested
            return result
    return None


async def fetch_trader_stats(pool: Any, trader_profile_id: Optional[int]) -> Optional[Dict[str, Any]]:
    """30d trader_performance row for credibility context."""
    if not trader_profile_id or pool is None:
        return None
    try:
        row = await pool.fetch_one(
            """
            SELECT
                accuracy_pct, avg_confidence, total_signals, validated_signals,
                correct_direction, sharpe_ratio, max_drawdown_pct, scored_at
            FROM public.trader_performance
            WHERE trader_profile_id = $1 AND score_period = '30d'
            ORDER BY scored_at DESC
            LIMIT 1
            """,
            (int(trader_profile_id),),
        )
        if not row:
            return None
        scored_at = row.get("scored_at")
        return {
            "score_period": "30d",
            "accuracy_pct": _float_or_none(row.get("accuracy_pct")),
            "avg_confidence": _float_or_none(row.get("avg_confidence")),
            "total_signals": row.get("total_signals"),
            "validated_signals": row.get("validated_signals"),
            "correct_direction": row.get("correct_direction"),
            "sharpe_ratio": _float_or_none(row.get("sharpe_ratio")),
            "max_drawdown_pct": _float_or_none(row.get("max_drawdown_pct")),
            "scored_at": scored_at.isoformat() if hasattr(scored_at, "isoformat") else str(scored_at),
        }
    except Exception as exc:
        logger.warning(
            "critic_context: trader_stats failed profile_id=%s: %s",
            trader_profile_id,
            exc,
        )
        return None


async def build_critic_enrichment(
    shared_pool: Any,
    company_pool: Any,
    row: Any,
) -> Dict[str, Any]:
    """Assemble Phase A/B context blocks for the critic LLM."""
    symbol = row.get("instrument_symbol") or ""
    exchange = row.get("instrument_exchange") or "bybit"
    position_tf = row.get("timeframe")
    trade_type = row.get("trade_type")

    interp_row = await fetch_interpretation_row(
        shared_pool, row.get("signal_interpretation_id"),
    )
    at_entry = build_at_entry_snapshot(interp_row)
    interp_tf = interp_row.get("interp_timeframe") if interp_row else None
    tf = resolve_critic_timeframe(position_tf, interp_tf, trade_type)

    quant_now, funding, tf_rsi, trader_stats = await asyncio.gather(
        fetch_quant_now(shared_pool, symbol, exchange),
        fetch_funding(shared_pool, symbol, exchange),
        fetch_timeframe_rsi(
            symbol,
            exchange,
            tf,
            position_tf=position_tf,
            interp_tf=interp_tf,
            trade_type=trade_type,
        ),
        fetch_trader_stats(company_pool, row.get("trader_profile_id")),
        return_exceptions=False,
    )

    enrichment: Dict[str, Any] = {}
    if quant_now:
        enrichment["quant_now"] = quant_now
    if at_entry:
        enrichment["at_entry"] = at_entry
    if funding:
        enrichment["funding"] = funding
    if tf_rsi:
        enrichment["timeframe_rsi"] = tf_rsi
    if trader_stats:
        enrichment["trader_stats_30d"] = trader_stats

    logger.debug(
        "build_critic_enrichment position_id=%s keys=%s tf=%s",
        row.get("position_id"),
        list(enrichment.keys()),
        tf,
    )
    return enrichment
