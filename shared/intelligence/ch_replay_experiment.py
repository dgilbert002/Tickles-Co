"""
Module: ch_replay_experiment
Purpose: A/B replay — chart_hacker with/without quant, multi-model, candle P&L sim.
Location: /opt/tickles/shared/intelligence/ch_replay_experiment.py
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

DEFAULT_OPUS_MODEL = "anthropic/claude-opus-4-8"
SIM_MAX_HOURS = 168  # 7 days of 1m bars max


@dataclass
class ReplayArm:
    arm_id: str
    provider: Optional[str]  # None = dashboard primary slot
    model: Optional[str]
    quant_enabled: bool


DEFAULT_ARMS: Tuple[ReplayArm, ...] = (
    ReplayArm("primary_quant_on", None, None, True),
    ReplayArm("primary_quant_off", None, None, False),
    ReplayArm("opus_quant_on", "requesty", DEFAULT_OPUS_MODEL, True),
    ReplayArm("opus_quant_off", "requesty", DEFAULT_OPUS_MODEL, False),
)


@dataclass
class ReplayRow:
    run_id: str
    interpretation_id: int
    signal_at: str
    symbol: str
    exchange: str
    arm_id: str
    provider: str
    model_requested: str
    model_resolved: str
    quant_enabled: bool
    quant_as_of: Optional[str] = None
    quant_direction: Optional[str] = None
    quant_rsi14: Optional[float] = None
    ch_trade_index: int = 0
    ch_direction: Optional[str] = None
    ch_entry: Optional[float] = None
    ch_sl: Optional[float] = None
    ch_tp: Optional[float] = None
    ch_confidence: Optional[float] = None
    ch_evidence: Optional[str] = None
    candle_count: int = 0
    sim_entry: Optional[float] = None
    sim_exit: Optional[float] = None
    sim_outcome: Optional[str] = None
    sim_pnl_pct: Optional[float] = None
    sim_bars: int = 0
    cost_usd: float = 0.0
    error: Optional[str] = None
    raw_response_path: Optional[str] = None


def _parse_trade_level(trade: Dict[str, Any], key: str) -> Optional[float]:
    from shared.intelligence.interpretation_service import _parse_price_level

    if key == "take_profit":
        for k in ("tp1", "take_profit", "take_profit_1", "tp"):
            if trade.get(k) is not None:
                return _parse_price_level(trade.get(k))
        return None
    if key == "stop_loss":
        for k in ("stop_loss", "sl"):
            if trade.get(k) is not None:
                return _parse_price_level(trade.get(k))
        return None
    return _parse_price_level(trade.get(key))


def pick_chart_hacker_trade(trades: Sequence[Any]) -> Optional[Dict[str, Any]]:
    """First actionable chart_hacker trade from Lens output."""
    for t in trades or []:
        if isinstance(t, dict) and str(t.get("direction", "")).lower() in ("long", "short"):
            return t
    return None


def simulate_trade_pnl(
    *,
    direction: str,
    entry: float,
    stop_loss: Optional[float],
    take_profit: Optional[float],
    candles: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Walk 1m candles after entry; conservative SL-first on same-bar ambiguity."""
    if entry <= 0 or not candles:
        return {
            "outcome": "no_candles",
            "pnl_pct": None,
            "exit_price": None,
            "bars": 0,
        }

    d = direction.lower()
    bars = 0
    for c in candles:
        bars += 1
        high = float(c["high"])
        low = float(c["low"])
        close = float(c["close"])

        if d == "long":
            sl_hit = stop_loss is not None and low <= stop_loss
            tp_hit = take_profit is not None and high >= take_profit
            if sl_hit and tp_hit:
                exit_p = stop_loss
                outcome = "sl_hit"
            elif sl_hit:
                exit_p = stop_loss
                outcome = "sl_hit"
            elif tp_hit:
                exit_p = take_profit
                outcome = "tp_hit"
            else:
                continue
            pnl = (exit_p - entry) / entry * 100
        else:
            sl_hit = stop_loss is not None and high >= stop_loss
            tp_hit = take_profit is not None and low <= take_profit
            if sl_hit and tp_hit:
                exit_p = stop_loss
                outcome = "sl_hit"
            elif sl_hit:
                exit_p = stop_loss
                outcome = "sl_hit"
            elif tp_hit:
                exit_p = take_profit
                outcome = "tp_hit"
            else:
                continue
            pnl = (entry - exit_p) / entry * 100

        return {
            "outcome": outcome,
            "pnl_pct": round(pnl, 4),
            "exit_price": exit_p,
            "bars": bars,
        }

    last = candles[-1]
    exit_p = float(last["close"])
    if d == "long":
        pnl = (exit_p - entry) / entry * 100
    else:
        pnl = (entry - exit_p) / entry * 100
    return {
        "outcome": "timeout",
        "pnl_pct": round(pnl, 4),
        "exit_price": exit_p,
        "bars": bars,
    }


async def fetch_replay_candles(
    *,
    symbol: str,
    exchange: str,
    start: datetime,
    hours: int = SIM_MAX_HOURS,
) -> List[Dict[str, Any]]:
    from shared.dashboard.market_routes import _fetch_candles, _resolve_candle_symbol
    from shared.utils.instrument_normaliser import to_canonical_symbol

    canon = to_canonical_symbol(symbol) or symbol
    canon = _resolve_candle_symbol(canon, canon)
    end = start + timedelta(hours=hours)
    limit = min(hours * 60 + 10, 10080)
    rows = await _fetch_candles(
        symbol=canon,
        exchange=exchange or "bybit",
        timeframe="1m",
        start=start,
        end=end,
        limit=limit,
    )
    out: List[Dict[str, Any]] = []
    for r in rows:
        ts = r.get("timestamp")
        if ts and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts and ts < start:
            continue
        out.append({
            "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
        })
    return out


async def select_chart_interpretations(pool: Any, limit: int = 50) -> List[Dict[str, Any]]:
    """Recent chart interpretations with local image paths."""
    rows = await pool.fetch_all(
        """
        SELECT
            si.id AS interpretation_id,
            si.created_at,
            si.instrument_symbol,
            COALESCE(si.exchange, si.instrument_exchange, 'bybit') AS exchange,
            si.trader_profile_id,
            m.local_path,
            n.headline, n.content, n.source, n.author, n.channel_name,
            n.context_window
        FROM public.signal_interpretations si
        JOIN public.media_items m ON m.id = si.media_item_id
        JOIN public.news_items n ON n.id = si.news_item_id
        WHERE m.local_path IS NOT NULL
          AND m.local_path <> ''
          AND si.media_item_id IS NOT NULL
          AND si.created_at < NOW() - INTERVAL '24 hours'
        ORDER BY si.created_at DESC
        LIMIT $1
        """,
        (int(limit),),
    )
    return [dict(r) for r in rows]


async def _resolve_primary_slot() -> Tuple[str, str]:
    from shared.intelligence.gateway_config import resolve_slot_gateway
    from shared.intelligence.model_config import SLOT_PRIMARY

    cfg, model = await resolve_slot_gateway(SLOT_PRIMARY)
    return cfg.gateway, model


async def replay_one_arm(
    pool: Any,
    ctx: Dict[str, Any],
    arm: ReplayArm,
    *,
    primary_provider: str,
    primary_model: str,
) -> ReplayRow:
    """Single reinterpret arm for one chart."""
    from shared.intelligence.interpretation_service import (
        InterpretationConfig,
        _format_context_window,
        run_llm_track,
        run_quant_track,
    )
    from shared.intelligence.text_signal_extractor import strip_reply_prefix
    from shared.utils.correlation import new_correlation_id

    interp_id = int(ctx["interpretation_id"])
    raw_symbol = ctx.get("instrument_symbol") or "UNKNOWN"
    from shared.utils.instrument_normaliser import to_canonical_symbol
    from shared.dashboard.market_routes import _resolve_candle_symbol

    symbol = _resolve_candle_symbol(
        to_canonical_symbol(raw_symbol) or raw_symbol,
        raw_symbol,
    )
    exchange = ctx.get("exchange") or "bybit"
    signal_at = ctx.get("created_at")
    if isinstance(signal_at, datetime) and signal_at.tzinfo is None:
        signal_at = signal_at.replace(tzinfo=timezone.utc)

    provider = arm.provider or primary_provider
    model = arm.model or primary_model
    run_id = f"{interp_id}_{arm.arm_id}"

    row = ReplayRow(
        run_id=run_id,
        interpretation_id=interp_id,
        signal_at=signal_at.isoformat() if hasattr(signal_at, "isoformat") else str(signal_at),
        symbol=symbol,
        exchange=exchange,
        arm_id=arm.arm_id,
        provider=provider,
        model_requested=model,
        model_resolved="",
        quant_enabled=arm.quant_enabled,
    )

    local_path = ctx.get("local_path")
    if not local_path or not Path(local_path).is_file():
        row.error = "missing_image"
        return row

    cfg = InterpretationConfig()
    clean_headline = strip_reply_prefix(ctx.get("headline") or "")
    clean_content = strip_reply_prefix(ctx.get("content") or "")
    news_context = f"{clean_headline}\n{clean_content}"[:1000]
    ctx_txt = _format_context_window(ctx.get("context_window"), ctx.get("author") or "")
    if ctx_txt:
        news_context = f"{news_context}\n\n{ctx_txt}"

    quant = None
    if arm.quant_enabled and symbol and symbol != "UNKNOWN" and signal_at:
        row.quant_as_of = signal_at.isoformat() if hasattr(signal_at, "isoformat") else str(signal_at)
        try:
            quant = await run_quant_track(
                pool, pool, symbol, exchange, cfg.freshness_threshold_s,
                as_of=signal_at,
            )
            row.quant_direction = quant.direction
            ind = quant.indicators or {}
            row.quant_rsi14 = float(ind["rsi14"]) if ind.get("rsi14") is not None else None
        except Exception as exc:
            logger.warning("replay quant failed interp=%s: %s", interp_id, exc)

    override = [(provider, model, arm.arm_id)]
    cid = new_correlation_id(f"ch_ab_{interp_id}")

    try:
        llm = await run_llm_track(
            cfg=cfg,
            image_path=local_path,
            news_context=news_context,
            instrument_symbol=symbol,
            correlation_id=cid,
            recall_context="",
            news_source=(ctx.get("source") or "discord").lower(),
            channel_name=str(ctx.get("channel_name") or ""),
            trader_profile_id=int(ctx.get("trader_profile_id") or 0),
            shared_pool=pool,
            chart_hacker_quant=quant if arm.quant_enabled else None,
            skip_prefilter=True,
            model_plan_override=override,
        )
    except Exception as exc:
        row.error = str(exc)[:500]
        return row

    row.model_resolved = llm.model_resolved or llm.model_used or model
    row.raw_response_path = llm.response_path or None

    trade = pick_chart_hacker_trade(llm.chart_hacker_trades)
    if not trade:
        row.error = "no_chart_hacker_trade"
        return row

    direction = str(trade.get("direction", "")).lower()
    entry = _parse_trade_level(trade, "entry")
    sl = _parse_trade_level(trade, "stop_loss")
    tp = _parse_trade_level(trade, "take_profit")
    try:
        row.ch_confidence = float(trade.get("confidence") or 0)
    except (TypeError, ValueError):
        row.ch_confidence = None

    row.ch_direction = direction
    row.ch_entry = entry
    row.ch_sl = sl
    row.ch_tp = tp
    row.ch_evidence = str(trade.get("evidence") or "")[:120]

    if not signal_at:
        row.error = "no_signal_timestamp"
        return row

    candles = await fetch_replay_candles(
        symbol=symbol, exchange=exchange, start=signal_at,
    )
    row.candle_count = len(candles)

    sim_entry = entry
    if sim_entry is None and candles:
        sim_entry = float(candles[0]["close"])
    row.sim_entry = sim_entry

    if sim_entry and direction in ("long", "short") and candles:
        sim = simulate_trade_pnl(
            direction=direction,
            entry=sim_entry,
            stop_loss=sl,
            take_profit=tp,
            candles=candles,
        )
        row.sim_outcome = sim.get("outcome")
        row.sim_pnl_pct = sim.get("pnl_pct")
        row.sim_exit = sim.get("exit_price")
        row.sim_bars = int(sim.get("bars") or 0)
    elif not candles:
        row.sim_outcome = "no_candles"
    else:
        row.sim_outcome = "incomplete_levels"

    return row


def rows_to_csv(path: Path, rows: Sequence[ReplayRow]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(asdict(rows[0]).keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(asdict(r))


def summarize_rows(rows: Sequence[ReplayRow]) -> Dict[str, Any]:
    by_arm: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        bucket = by_arm.setdefault(r.arm_id, {
            "arm_id": r.arm_id,
            "runs": 0,
            "errors": 0,
            "trades": 0,
            "total_pnl_pct": 0.0,
            "wins": 0,
            "losses": 0,
            "timeouts": 0,
            "avg_pnl_pct": None,
            "win_rate": None,
        })
        bucket["runs"] += 1
        if r.error:
            bucket["errors"] += 1
            continue
        if r.sim_pnl_pct is None:
            continue
        bucket["trades"] += 1
        bucket["total_pnl_pct"] += float(r.sim_pnl_pct)
        if r.sim_pnl_pct > 0:
            bucket["wins"] += 1
        elif r.sim_pnl_pct < 0:
            bucket["losses"] += 1
        if r.sim_outcome == "timeout":
            bucket["timeouts"] += 1
    for b in by_arm.values():
        if b["trades"]:
            b["avg_pnl_pct"] = round(b["total_pnl_pct"] / b["trades"], 4)
            b["win_rate"] = round(b["wins"] / b["trades"], 4)
    ranked = sorted(
        by_arm.values(),
        key=lambda x: (x.get("avg_pnl_pct") is not None, x.get("avg_pnl_pct") or -999),
        reverse=True,
    )
    return {"by_arm": by_arm, "ranked": ranked}


async def run_experiment(
    *,
    limit: int = 50,
    output_dir: Path,
    arms: Sequence[ReplayArm] = DEFAULT_ARMS,
    concurrency: int = 4,
) -> Dict[str, Any]:
    """Run full A/B replay experiment and write CSV + JSON artifacts."""
    from shared.utils.db import get_shared_pool

    pool = await get_shared_pool()
    charts = await select_chart_interpretations(pool, limit=limit)
    primary_provider, primary_model = await _resolve_primary_slot()

    output_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(max(1, concurrency))
    all_rows: List[ReplayRow] = []

    async def _one(ctx: Dict[str, Any], arm: ReplayArm) -> ReplayRow:
        async with sem:
            logger.info(
                "replay interp=%s arm=%s",
                ctx["interpretation_id"], arm.arm_id,
            )
            return await replay_one_arm(
                pool, ctx, arm,
                primary_provider=primary_provider,
                primary_model=primary_model,
            )

    tasks = [
        _one(ctx, arm)
        for ctx in charts
        for arm in arms
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for res in results:
        if isinstance(res, Exception):
            logger.exception("replay task failed: %s", res)
            continue
        all_rows.append(res)

    csv_path = output_dir / "results.csv"
    jsonl_path = output_dir / "results.jsonl"
    summary_path = output_dir / "summary.json"
    manifest_path = output_dir / "manifest.json"

    rows_to_csv(csv_path, all_rows)
    with jsonl_path.open("w", encoding="utf-8") as f:
        for r in all_rows:
            f.write(json.dumps(asdict(r), default=str) + "\n")

    summary = summarize_rows(all_rows)
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "experiment_version": "2_historical_quant",
        "quant_mode": "historical_at_signal",
        "chart_count": len(charts),
        "arm_count": len(arms),
        "run_count": len(all_rows),
        "primary_slot": {"provider": primary_provider, "model": primary_model},
        "arms": [asdict(a) for a in arms],
        "output_files": {
            "csv": str(csv_path),
            "jsonl": str(jsonl_path),
            "summary": str(summary_path),
        },
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return {"manifest": manifest, "summary": summary, "rows": len(all_rows)}
