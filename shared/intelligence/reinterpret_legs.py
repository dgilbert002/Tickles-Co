"""Build replay-style legs + candles for MCP reinterpret (matches dashboard replay API)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from shared.dashboard.market_routes import (
    _fetch_candles,
    _levels_from_position,
    _levels_from_trade,
    _normalise_timeframe,
    _resolve_candle_symbol,
    _timeframe_window,
    _trade_matches_position,
)

logger = logging.getLogger(__name__)


def _utc(dt: Optional[datetime]) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def build_legs_from_trades(
    *,
    trader_trades: List[Any],
    chart_hacker_trades: List[Any],
    default_symbol: str,
    default_exchange: str,
    default_timeframe: Optional[str],
    positions: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Build leg list from LLM trades, optionally matching armed DB positions."""
    positions = positions or []
    match_positions = [
        p for p in positions if str(p.get("status") or "").lower() not in ("cancelled",)
    ]
    matched_pos_ids: set[int] = set()
    legs: List[Dict[str, Any]] = []

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
        tf_raw = trade.get("timeframe") or (pos or {}).get("timeframe") or default_timeframe
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
            } if pos else None,
            "candles": [],
            "position_updates": [],
            "coverage": {"candle_count": 0, "update_count": 0},
        })

    for i, trade in enumerate(trader_trades or []):
        if not isinstance(trade, dict):
            continue
        pos = next(
            (
                p for p in match_positions
                if p["id"] not in matched_pos_ids and _trade_matches_position(trade, p, "trader")
            ),
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

    for i, trade in enumerate(chart_hacker_trades or []):
        if not isinstance(trade, dict):
            continue
        pos = next(
            (
                p for p in match_positions
                if p["id"] not in matched_pos_ids and _trade_matches_position(trade, p, "chart_hacker")
            ),
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

    return legs


async def attach_candles_to_legs(
    legs: List[Dict[str, Any]],
    call_ts: datetime,
    pool,
    *,
    include_position_updates: bool = False,
    all_positions: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Fetch per-leg candles using the same window logic as signal-replay."""
    call_ts = _utc(call_ts)
    all_positions = all_positions or []

    for leg in legs:
        leg_call = call_ts
        pos = leg.get("position") or {}
        if pos.get("opened_at"):
            leg_call = _utc(pos["opened_at"])
        elif leg.get("position_id") and all_positions:
            pr = next((p for p in all_positions if p["id"] == leg["position_id"]), None)
            if pr and pr.get("signal_timestamp"):
                leg_call = _utc(pr["signal_timestamp"])

        leg_end = _utc(pos.get("closed_at")) if pos.get("closed_at") else datetime.now(timezone.utc)
        tf = leg["timeframe"]
        start, end, candle_limit = _timeframe_window(leg_call, leg_end, tf)
        leg["window"] = {"start": start.isoformat(), "end": end.isoformat()}
        leg["candles"] = await _fetch_candles(
            symbol=leg["symbol"],
            exchange=leg["exchange"],
            timeframe=tf,
            start=start,
            end=end,
            limit=candle_limit,
        )
        leg["coverage"]["candle_count"] = len(leg["candles"])

        if include_position_updates and leg.get("position_id"):
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

    return legs


async def fetch_positions_for_interp(pool, interpretation_id: int) -> List[Dict[str, Any]]:
    rows = await pool.fetch_all(
        """
        SELECT id, status, direction, entry_price, stop_loss, take_profit_1, take_profit_2,
               take_profit_3, signal_source, signal_timestamp, created_at, closed_at,
               instrument_symbol, instrument_exchange, timeframe, chart_hacker_endorsed, actor_id
        FROM public.tracked_positions
        WHERE signal_interpretation_id = $1
        ORDER BY id
        """,
        (interpretation_id,),
    )
    return [dict(r) for r in rows]


async def enrich_reinterpret_with_legs(
    response: Dict[str, Any],
    *,
    pool,
    default_symbol: str,
    default_exchange: str,
    call_ts: datetime,
    interpretation_id: Optional[int] = None,
    include_candles: bool = True,
    include_position_updates: bool = False,
) -> Dict[str, Any]:
    """Attach legs[] and chart metadata to a reinterpret response."""
    default_tf = response.get("timeframe")
    positions: List[Dict[str, Any]] = []
    if interpretation_id:
        positions = await fetch_positions_for_interp(pool, interpretation_id)

    legs = build_legs_from_trades(
        trader_trades=response.get("trader_trades") or [],
        chart_hacker_trades=response.get("chart_hacker_trades") or [],
        default_symbol=default_symbol,
        default_exchange=default_exchange,
        default_timeframe=default_tf,
        positions=positions,
    )

    if include_candles:
        legs = await attach_candles_to_legs(
            legs,
            call_ts,
            pool,
            include_position_updates=include_position_updates,
            all_positions=positions,
        )

    response["legs"] = legs
    response["chart"] = {
        "default_symbol": _resolve_candle_symbol(
            response.get("instrument") or default_symbol,
            default_symbol,
        ),
        "default_exchange": default_exchange.lower(),
        "default_timeframe": _normalise_timeframe(default_tf or "1m"),
        "timeframe_source": default_tf,
        "call_timestamp": _utc(call_ts).isoformat(),
    }
    return response
