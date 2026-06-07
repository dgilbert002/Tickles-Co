"""
Module: critic_compare
Purpose: On-demand fresh chart_hacker critic runs vs trader setup + stored opinion.
Location: /opt/tickles/shared/intelligence/critic_compare.py
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

_POSITION_COLUMNS = """
    p.id AS position_id,
    p.actor_type,
    p.actor_id,
    p.entry_price,
    p.current_price,
    p.stop_loss,
    p.take_profit_1 AS take_profit,
    p.price_updated_at,
    p.updated_at AS position_updated_at,
    p.entry_reason_trader,
    p.instrument_symbol,
    p.instrument_exchange,
    p.direction,
    p.signal_interpretation_id,
    p.trader_profile_id,
    p.timeframe,
    p.trade_type,
    p.status,
    p.company_id,
    ao.would_take_trade AS stored_would_take,
    ao.agent_confidence AS stored_confidence,
    ao.reasoning AS stored_memo,
    ao.agent_stop_loss AS stored_sl,
    ao.agent_take_profit AS stored_tp,
    ao.updated_at AS stored_opinion_at
"""


def json_safe(val: Any) -> Any:
    """Recursively convert DB types to JSON-serializable values."""
    if isinstance(val, dict):
        return {k: json_safe(v) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return [json_safe(v) for v in val]
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, date):
        return val.isoformat()
    if isinstance(val, Decimal):
        return float(val)
    return val


def _float(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def compute_pnl_pct(direction: str, entry: Optional[float], current: Optional[float]) -> Optional[float]:
    """Unrealized PnL % for an open position."""
    if not entry or not current or entry <= 0:
        return None
    if (direction or "").lower() == "long":
        return round((current - entry) / entry * 100, 2)
    return round((entry - current) / entry * 100, 2)


def quant_aligns_trader(quant_dir: Optional[str], trader_direction: Optional[str]) -> Optional[bool]:
    """True when live quant direction matches the trader's side."""
    q = (quant_dir or "").lower()
    d = (trader_direction or "").lower()
    if q not in ("long", "short") or d not in ("long", "short"):
        return None
    return q == d


def build_comparison_row(row: Any, enrichment: Dict[str, Any], opinion: Dict[str, Any]) -> Dict[str, Any]:
    """Shape one critic-vs-trader comparison result."""
    entry = _float(row.get("entry_price"))
    current = _float(row.get("current_price"))
    direction = row.get("direction")
    pnl_pct = compute_pnl_pct(direction or "", entry, current)

    qnow = enrichment.get("quant_now") or {}
    qdir = qnow.get("direction")
    stats = enrichment.get("trader_stats_30d") or {}
    stored_take = row.get("stored_would_take")
    fresh_take = bool(opinion.get("would_take_trade"))
    fresh_conf = round(float(opinion.get("memo_confidence", 0) or 0), 4)

    verdict_changed = (
        stored_take is not None and bool(stored_take) != fresh_take
    )

    return {
        "position_id": int(row["position_id"]),
        "status": row.get("status"),
        "trader": {
            "actor_id": row.get("actor_id"),
            "actor_type": row.get("actor_type"),
            "trader_profile_id": row.get("trader_profile_id"),
            "company_id": row.get("company_id"),
            "direction": direction,
            "symbol": row.get("instrument_symbol"),
            "exchange": row.get("instrument_exchange"),
            "entry_price": entry,
            "current_price": current,
            "unrealized_pnl_pct": pnl_pct,
            "stop_loss": _float(row.get("stop_loss")),
            "take_profit": _float(row.get("take_profit")),
            "reason_chars": len((row.get("entry_reason_trader") or "").strip()),
            "timeframe": row.get("timeframe"),
            "trade_type": row.get("trade_type"),
        },
        "trader_score_30d": stats or None,
        "enrichment": enrichment,
        "stored_critic": {
            "would_take_trade": stored_take,
            "confidence": _float(row.get("stored_confidence")),
            "memo": (row.get("stored_memo") or "").strip() or None,
            "suggested_sl": _float(row.get("stored_sl")),
            "suggested_tp": _float(row.get("stored_tp")),
            "updated_at": json_safe(row.get("stored_opinion_at")),
        },
        "fresh_critic": {
            "would_take_trade": fresh_take,
            "confidence": fresh_conf,
            "memo": str(opinion.get("memo", "")).strip(),
            "suggested_sl": _float(opinion.get("suggested_sl")),
            "suggested_tp": _float(opinion.get("suggested_tp")),
            "model_used": opinion.get("model_used"),
            "cost_usd": _float(opinion.get("cost_usd")),
        },
        "comparison": {
            "critic_agrees_with_trader": fresh_take,
            "quant_aligns_trader": quant_aligns_trader(qdir, direction),
            "quant_now_direction": qdir,
            "quant_now_confidence": qnow.get("confidence"),
            "rsi_1m": (qnow.get("indicators") or {}).get("rsi14"),
            "rsi_timeframe": (enrichment.get("timeframe_rsi") or {}).get("rsi14"),
            "funding_rate": (enrichment.get("funding") or {}).get("funding_rate"),
            "verdict_changed_from_stored": verdict_changed,
            "trader_accuracy_30d": stats.get("accuracy_pct") if stats else None,
        },
    }


def summarize_comparisons(items: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Roll-up stats for a batch of comparison rows."""
    ok = [i for i in items if "error" not in i]
    errors = [i for i in items if "error" in i]
    fresh_takes = sum(1 for i in ok if i.get("fresh_critic", {}).get("would_take_trade"))
    stored_takes = sum(
        1 for i in ok if i.get("stored_critic", {}).get("would_take_trade") is True
    )
    flips = sum(
        1 for i in ok if i.get("comparison", {}).get("verdict_changed_from_stored")
    )
    quant_align = sum(
        1 for i in ok if i.get("comparison", {}).get("quant_aligns_trader") is True
    )
    quant_against = sum(
        1 for i in ok if i.get("comparison", {}).get("quant_aligns_trader") is False
    )
    return {
        "requested": len(items),
        "succeeded": len(ok),
        "errors": len(errors),
        "fresh_would_take": fresh_takes,
        "stored_would_take": stored_takes,
        "verdict_flips": flips,
        "quant_aligns_trader": quant_align,
        "quant_against_trader": quant_against,
    }


async def fetch_positions_for_compare(
    shared_pool: Any,
    *,
    position_ids: Optional[Sequence[int]] = None,
    limit: int = 10,
    status: str = "open",
    trader_profile_id: Optional[int] = None,
    actor_id: Optional[str] = None,
    actor_ids: Optional[Sequence[str]] = None,
    actor_id_prefix: Optional[str] = None,
    company_id: Optional[str] = None,
) -> List[Any]:
    """Load tracked_positions rows for critic comparison."""
    conditions = ["p.status = $1", "p.actor_type IN ('trader_human', 'agent')"]
    args: List[Any] = [status]
    idx = 2

    if company_id:
        conditions.append(f"p.company_id = ${idx}")
        args.append(company_id)
        idx += 1

    if position_ids:
        conditions.append(f"p.id = ANY(${idx}::bigint[])")
        args.append(list(int(x) for x in position_ids))
        idx += 1
    elif trader_profile_id:
        conditions.append(f"p.trader_profile_id = ${idx}")
        args.append(int(trader_profile_id))
        idx += 1

    if actor_id:
        conditions.append(f"p.actor_id = ${idx}")
        args.append(actor_id)
        idx += 1
    elif actor_ids:
        conditions.append(f"p.actor_id = ANY(${idx}::text[])")
        args.append(list(actor_ids))
        idx += 1
    elif actor_id_prefix:
        conditions.append(f"p.actor_id LIKE ${idx}")
        args.append(f"{actor_id_prefix}%")
        idx += 1

    limit_clause = ""
    if not position_ids:
        cap = max(1, min(int(limit), 25))
        limit_clause = f" LIMIT ${idx}"
        args.append(cap)

    sql = f"""
        SELECT {_POSITION_COLUMNS}
        FROM tracked_positions p
        LEFT JOIN agent_opinions ao
          ON ao.position_id = p.id AND ao.agent_name = 'chart_hacker'
        WHERE {' AND '.join(conditions)}
        ORDER BY p.id DESC
        {limit_clause}
    """
    logger.debug(
        "fetch_positions_for_compare status=%s ids=%s limit=%s company=%s",
        status, position_ids, limit, company_id,
    )
    return await shared_pool.fetch_all(sql, tuple(args))


async def run_critic_compare(
    *,
    company_id: Optional[str] = None,
    position_ids: Optional[Sequence[int]] = None,
    limit: int = 10,
    status: str = "open",
    trader_profile_id: Optional[int] = None,
    actor_id: Optional[str] = None,
    actor_ids: Optional[Sequence[str]] = None,
    actor_id_prefix: Optional[str] = None,
    persist: bool = False,
) -> Dict[str, Any]:
    """Run fresh critic opinions and compare to trader setup + stored critic row."""
    from shared.intelligence.chart_hacker_opinion_service import ChartHackerOpinionService
    from shared.intelligence.critic_context import build_critic_enrichment
    from shared.utils.db import get_company_pool, get_shared_pool

    shared_pool = await get_shared_pool()

    rows = await fetch_positions_for_compare(
        shared_pool,
        position_ids=position_ids,
        limit=limit,
        status=status,
        trader_profile_id=trader_profile_id,
        actor_id=actor_id,
        actor_ids=actor_ids,
        actor_id_prefix=actor_id_prefix,
        company_id=company_id,
    )
    if not rows:
        return json_safe({
            "ok": True,
            "count": 0,
            "items": [],
            "summary": summarize_comparisons([]),
            "message": "No matching positions",
            "filters": {
                "company_id": company_id,
                "position_ids": list(position_ids) if position_ids else None,
                "actor_id": actor_id,
                "actor_ids": list(actor_ids) if actor_ids else None,
            },
        })

    items: List[Dict[str, Any]] = []
    total_cost = 0.0
    company_pools: Dict[str, Any] = {}
    services: Dict[str, ChartHackerOpinionService] = {}

    for row in rows:
        pid = int(row["position_id"])
        row_company = str(row.get("company_id") or company_id or "").strip()
        try:
            if row_company not in company_pools:
                company_pools[row_company] = (
                    await get_company_pool(row_company) if row_company else None
                )
            if row_company not in services:
                services[row_company] = ChartHackerOpinionService(
                    company_id=row_company or "unknown",
                )
            service = services[row_company]
            company_pool = company_pools.get(row_company)

            enrichment = await build_critic_enrichment(
                shared_pool, company_pool, row,
            )
            opinion = await service._run_vision_opinion(row, enrichment=enrichment)
            if not opinion:
                items.append({"position_id": pid, "error": "critic_llm_failed"})
                continue

            total_cost += float(opinion.get("cost_usd") or 0.0)
            item = build_comparison_row(row, enrichment, opinion)
            items.append(item)

            if persist:
                async with shared_pool.acquire() as conn:
                    if service.prompt_version is None:
                        await service._register_prompt()
                    await service._write_opinion(conn, row, opinion)
                    await service._backfill_position_levels_from_critic(conn, row, opinion)
                item["persisted"] = True
        except Exception as exc:
            logger.exception("run_critic_compare failed position_id=%s", pid)
            items.append({"position_id": pid, "error": str(exc)})

    summary = summarize_comparisons(items)
    summary["total_cost_usd"] = round(total_cost, 6)

    return json_safe({
        "ok": True,
        "count": len(items),
        "persisted": persist,
        "filters": {
            "company_id": company_id,
            "position_ids": list(position_ids) if position_ids else None,
            "trader_profile_id": trader_profile_id,
            "actor_id": actor_id,
            "actor_ids": list(actor_ids) if actor_ids else None,
            "actor_id_prefix": actor_id_prefix,
            "status": status,
            "limit": limit,
        },
        "items": items,
        "summary": summary,
    })
