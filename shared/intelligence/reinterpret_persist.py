"""Persist MCP / CLI reinterpret results onto signal_interpretations and re-arm legs."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from shared.utils.correlation import new_correlation_id
from shared.utils.db import get_shared_pool
from shared.intelligence.text_signal_extractor import strip_reply_prefix
from shared.intelligence.interpretation_service import (
    InterpretationConfig,
    _flatten_llm_levels,
    _format_context_window,
    _get_chart_hacker_profile_id,
    _is_explicit_trader_setup,
    _param_hash,
    _parse_price_level,
    _recall_relevant_memories,
    _resolve_entry_price,
    _trades_agree,
    create_tracked_position_from_interpretation,
    prepare_chart_hacker_quant,
    resolve_company_for_source,
    run_consensus,
    run_llm_track,
)

logger = logging.getLogger(__name__)


async def load_interp_context(pool, interpretation_id: int) -> Dict[str, Any]:
    row = await pool.fetch_one(
        """
        SELECT
          si.id AS signal_interpretation_id,
          si.news_item_id, si.media_item_id, si.trader_profile_id,
          si.instrument_symbol, si.exchange,
          m.local_path, m.source_url,
          n.headline, n.content, n.source, n.author, n.channel_name,
          n.instruments, n.source_id, n.context_window
        FROM public.signal_interpretations si
        JOIN public.media_items m ON m.id = si.media_item_id
        JOIN public.news_items n ON n.id = si.news_item_id
        WHERE si.id = $1
        """,
        (interpretation_id,),
    )
    if not row:
        raise ValueError(f"interpretation {interpretation_id} not found")
    return dict(row)


async def cancel_existing_positions(
    pool,
    interpretation_id: int,
    *,
    reason: str = "reprocessed: reinterpret re-arm",
) -> int:
    result = await pool.execute(
        """
        UPDATE public.tracked_positions
        SET status = 'cancelled',
            status_reason = $2,
            updated_at = NOW()
        WHERE signal_interpretation_id = $1
          AND status IN ('pending', 'open', 'tracking')
        """,
        (interpretation_id, reason),
    )
    cancelled = int(str(result).split()[-1]) if result else 0
    # Remove cancelled rows for this interpretation so re-arm INSERTs are not
    # blocked by the (news_item_id, trader, symbol, direction, entry_price) unique
    # key or frozen entry_reason triggers on stale cancelled rows.
    await pool.execute(
        """
        DELETE FROM public.tracked_positions
        WHERE signal_interpretation_id = $1
          AND status = 'cancelled'
        """,
        (interpretation_id,),
    )
    return cancelled


async def update_interpretation_from_llm(
    pool,
    interpretation_id: int,
    *,
    consensus,
    llm,
    quant,
    param_hash: str,
) -> None:
    chart_analysis_obj = llm.chart_analysis if llm and llm.chart_analysis else {}
    raw_patterns = chart_analysis_obj.get("chart_patterns") if isinstance(chart_analysis_obj, dict) else None
    pattern_tags: List[str] = []
    if isinstance(raw_patterns, list):
        seen: set = set()
        for p in raw_patterns:
            if isinstance(p, str) and (t := p.strip().lower()) and t not in seen:
                seen.add(t)
                pattern_tags.append(t)
    raw_levels = chart_analysis_obj.get("key_levels") if isinstance(chart_analysis_obj, dict) else None
    setup_tags: List[str] = []
    if isinstance(raw_levels, list):
        seen_s: set = set()
        for lvl in raw_levels:
            if isinstance(lvl, dict) and isinstance(lvl.get("type"), str):
                t = lvl["type"].strip().lower()
                if t and t not in seen_s:
                    seen_s.add(t)
                    setup_tags.append(t)
    levels_flat = _flatten_llm_levels(llm.levels if llm else None)
    primary = (llm.trader_trades or [{}])[0] if llm and llm.trader_trades else {}
    consensus_dir = (
        (primary.get("direction") or "").lower()
        if (primary.get("direction") or "").lower() in ("long", "short")
        else consensus.direction
    )

    await pool.execute(
        """
        UPDATE public.signal_interpretations SET
          model_version = $2,
          param_hash = $3,
          llm_direction = $4,
          llm_confidence = $5,
          llm_reasoning = $6,
          llm_levels = $7::jsonb,
          quant_direction = $8,
          quant_confidence = $9,
          quant_indicators = $10::jsonb,
          consensus_direction = $11,
          consensus_confidence = $12,
          consensus_method = $13,
          instrument_symbol = $14,
          exchange = $15,
          prompt_version = $16,
          prompt_hash = $17,
          llm_raw_request_path = $18,
          llm_raw_response_path = $19,
          timeframe = $20,
          chart_analysis = $21::jsonb,
          trader_trades = $22::jsonb,
          chart_hacker_trades = $23::jsonb,
          ai_agreement_score = $24,
          ai_comment = $25,
          entry_price = $26,
          stop_loss = $27,
          take_profit_1 = $28,
          take_profit_2 = $29,
          take_profit_3 = $30,
          pattern_tags = $31::jsonb,
          setup_tags = $32::jsonb,
          prompt_source = $33,
          vision_provider = $34,
          vision_model_requested = $35,
          vision_model_resolved = $36
        WHERE id = $1
        """,
        (
            interpretation_id,
            llm.model_used if llm else "",
            param_hash,
            llm.direction if llm else "unclear",
            round(llm.confidence, 4) if llm else 0.0,
            llm.reasoning if llm else "",
            json.dumps(llm.levels if llm else {}),
            quant.direction if quant else "unclear",
            round(quant.confidence, 4) if quant else 0.0,
            json.dumps(quant.indicators if quant else {}),
            consensus_dir,
            round(consensus.confidence, 4),
            consensus.method,
            llm.instrument or "",
            "bybit",
            llm.prompt_version if llm else "",
            llm.prompt_hash if llm else "",
            llm.request_path if llm else "",
            llm.response_path if llm else "",
            llm.timeframe if llm else None,
            json.dumps(chart_analysis_obj),
            json.dumps(llm.trader_trades if llm else []),
            json.dumps(llm.chart_hacker_trades if llm else []),
            round(llm.ai_agreement, 2) if llm and llm.ai_agreement else None,
            llm.ai_comment if llm else None,
            levels_flat.get("entry_price") or (primary.get("entry") if primary else None),
            levels_flat.get("stop_loss") or (primary.get("stop_loss") or primary.get("stop") if primary else None),
            levels_flat.get("take_profit_1") or (primary.get("take_profit") or primary.get("target") if primary else None),
            levels_flat.get("take_profit_2"),
            levels_flat.get("take_profit_3"),
            json.dumps(pattern_tags),
            json.dumps(setup_tags),
            llm.prompt_source if llm else "",
            getattr(llm, "provider", "") if llm else "",
            llm.model_used if llm else "",
            llm.model_resolved if llm else "",
        ),
    )


async def arm_legs_from_llm(
    pool,
    *,
    sig_id: int,
    ctx: Dict[str, Any],
    llm,
    consensus,
    quant,
    company: str,
    symbol: str,
    exchange: str,
    cid: str,
    clean_headline: str,
    clean_content: str,
    news_source: str,
) -> List[int]:
    positions: List[int] = []
    trader_profile_id = int(ctx["trader_profile_id"])
    news_item_id = int(ctx["news_item_id"])
    media_id = ctx.get("media_item_id") or ctx.get("media_id")
    chart_hacker_pid = await _get_chart_hacker_profile_id(pool)
    trader_armed: List[Tuple[int, Dict[str, Any]]] = []
    news_text = f"{clean_headline}\n{clean_content}"
    prompt_ver = getattr(llm, "prompt_version", "") or ""

    async def _write_one(
        trade: Dict[str, Any],
        source: str,
        profile_id: int,
        actor_type_val: str,
        actor_id_val: str,
        reason_trader: Optional[str],
        reason_agent: Optional[str],
    ) -> Optional[int]:
        direction = str(trade.get("direction", "")).lower().strip()
        if direction not in ("long", "short"):
            return None
        leg_symbol = symbol
        raw_sym = trade.get("symbol")
        if raw_sym and str(raw_sym).strip().upper() not in ("", "UNKNOWN"):
            try:
                from shared.utils.instrument_normaliser import to_canonical_symbol
                leg_symbol = to_canonical_symbol(str(raw_sym)) or symbol
            except Exception:
                leg_symbol = str(raw_sym)
        entry_p, entry_src = await _resolve_entry_price(
            pool,
            {"trader_entry": trade.get("entry"), "llm_entry": None},
            leg_symbol,
            exchange,
        )
        pid = await create_tracked_position_from_interpretation(
            shared_pool=pool,
            signal_interpretation_id=sig_id,
            news_item_id=news_item_id,
            media_item_id=media_id,
            trader_profile_id=profile_id,
            instrument_symbol=leg_symbol,
            instrument_exchange=exchange,
            direction=direction,
            entry_price=entry_p,
            stop_loss=_parse_price_level(
                # The chart-analysis prompt asks for "stop_loss" but the LLM
                # sometimes returns "stop" (bare) or nests it inside
                # trade.levels.stop — accept all three forms.
                trade.get("stop_loss")
                or trade.get("stop")
                or (trade.get("levels") or {}).get("stop")
                or (trade.get("levels") or {}).get("stop_loss")
            ),
            take_profit_1=_parse_price_level(
                trade.get("tp1")
                or trade.get("take_profit")
                or trade.get("target")
            ),
            detection_method="llm_vision",
            detection_confidence=float(
                trade.get("confidence") or trade.get("trader_confidence") or consensus.confidence or 0.0
            ),
            raw_signal_text=f"{clean_headline}\n{clean_content}"[:2000],
            company_id=company,
            entry_reason_trader=reason_trader,
            entry_reason_llm=str(trade.get("rationale") or "")[:2000] or None,
            entry_reason_agent=reason_agent,
            correlation_id=cid,
            signal_source=source,
            actor_type=actor_type_val,
            actor_id=actor_id_val,
            trade_type=str(trade.get("trade_type") or "")[:20] or None,
            timeframe=str(trade.get("timeframe") or llm.timeframe or "")[:8] or None,
            take_profit_2=_parse_price_level(trade.get("tp2")),
            take_profit_3=_parse_price_level(trade.get("tp3")),
            take_profit_4=_parse_price_level(trade.get("tp4")),
            take_profit_5=_parse_price_level(trade.get("tp5")),
            take_profit_6=_parse_price_level(trade.get("tp6")),
            entry_price_source=entry_src,
            market_price=(quant.indicators or {}).get("current_price") if quant else None,
        )
        if pid:
            positions.append(pid)
        return pid

    trader_actor = f"{company}_trader_{trader_profile_id}"
    for trade in llm.trader_trades or []:
        ok, evidence = _is_explicit_trader_setup(trade, news_text, prompt_ver)
        if not ok:
            logger.info("gate skip trader leg: %s", evidence)
            continue
        pid = await _write_one(
            trade, "trader", trader_profile_id, "trader_human", trader_actor,
            clean_content[:2000] if clean_content else None, None,
        )
        if pid:
            trader_armed.append((pid, trade))

    if chart_hacker_pid is not None:
        ch_actor = f"{company}_rose_ch" if news_source == "telegram" else f"{company}_chart_hacker"
        trader_dirs = {
            str(t.get("direction") or "").lower()
            for t in (llm.trader_trades or [])
            if isinstance(t, dict)
        }
        for trade in llm.chart_hacker_trades or []:
            ev = str(trade.get("evidence") or "").lower().strip()
            try:
                cc = float(trade.get("confidence") or 0.0)
            except (TypeError, ValueError):
                cc = 0.0
            if ev == "inferred" and cc < 0.6:
                continue
            ch_dir = str(trade.get("direction") or "").lower()
            if (
                ev == "inferred"
                and trader_dirs
                and ch_dir in ("long", "short")
                and ch_dir not in trader_dirs
            ):
                logger.info(
                    "skip CH inferred %s — conflicts with trader dirs %s",
                    ch_dir, trader_dirs,
                )
                continue
            endorsed: Optional[int] = None
            for tpid, ttrade in trader_armed:
                if _trades_agree(trade, ttrade):
                    endorsed = tpid
                    break
            if endorsed:
                await pool.execute(
                    "UPDATE tracked_positions SET chart_hacker_endorsed = TRUE, updated_at = NOW() WHERE id = $1",
                    (endorsed,),
                )
                continue
            await _write_one(
                trade, "chart_hacker", chart_hacker_pid, "agent", ch_actor,
                None, str(trade.get("rationale") or "")[:2000] or None,
            )
    return positions


async def _resolve_company(pool, ctx: Dict[str, Any]) -> str:
    company = "jarvais"
    if ctx.get("source_id"):
        resolved = await resolve_company_for_source(pool, ctx["source_id"])
        if resolved:
            company = resolved
    return company


async def persist_reinterpret_from_llm(
    pool,
    interpretation_id: int,
    ctx: Dict[str, Any],
    llm,
    consensus,
    quant,
    *,
    rearm: bool = False,
    correlation_id: Optional[str] = None,
    cancel_reason: str = "reprocessed: MCP reinterpret re-arm",
) -> Dict[str, Any]:
    """Write LLM result to an existing interpretation; optionally cancel + re-arm legs."""
    company = await _resolve_company(pool, ctx)
    symbol = llm.instrument or ctx.get("instrument_symbol") or ctx.get("prior_symbol") or "BTC/USDT"
    exchange = ctx.get("exchange") or ctx.get("prior_exchange") or "bybit"
    news_source = (ctx.get("source") or "discord").lower()
    clean_headline = strip_reply_prefix(ctx.get("headline") or "")
    clean_content = strip_reply_prefix(ctx.get("content") or "")
    cid = correlation_id or new_correlation_id("mcp_persist")

    param_hash = _param_hash(
        llm.model_used,
        InterpretationConfig().primary_model,
        InterpretationConfig().fallback_model,
        InterpretationConfig().freshness_threshold_s,
        InterpretationConfig().max_age_hours,
        extra=f"reprocess:{interpretation_id}:{llm.prompt_version}",
    )

    cancelled = 0
    if rearm:
        cancelled = await cancel_existing_positions(
            pool, interpretation_id, reason=cancel_reason,
        )

    await update_interpretation_from_llm(
        pool, interpretation_id,
        consensus=consensus, llm=llm, quant=quant, param_hash=param_hash,
    )

    positions_created: List[int] = []
    note: Optional[str] = None
    if rearm:
        if (llm.setup_state or "").lower().strip() == "commentary":
            note = "setup_state=commentary — no positions armed"
        else:
            positions_created = await arm_legs_from_llm(
                pool,
                sig_id=interpretation_id,
                ctx=ctx,
                llm=llm,
                consensus=consensus,
                quant=quant,
                company=company,
                symbol=symbol,
                exchange=exchange,
                cid=cid,
                clean_headline=clean_headline,
                clean_content=clean_content,
                news_source=news_source,
            )

    rows = await pool.fetch_all(
        """
        SELECT id, direction, entry_price, stop_loss, take_profit_1, status, chart_hacker_endorsed
        FROM tracked_positions
        WHERE signal_interpretation_id = $1 AND status = 'pending'
        ORDER BY id
        """,
        (interpretation_id,),
    )
    return {
        "ok": True,
        "interpretation_id": interpretation_id,
        "prompt_version": llm.prompt_version,
        "persisted": True,
        "rearmed": rearm,
        "cancelled_positions": cancelled,
        "positions_created": positions_created,
        "pending_positions": [dict(r) for r in rows],
        "trader_trades": len(llm.trader_trades or []),
        "chart_hacker_trades": len(llm.chart_hacker_trades or []),
        "consensus_direction": consensus.direction,
        **({"note": note} if note else {}),
    }


async def persist_reinterpret_and_rearm(
    interpretation_id: int,
    prompt_version: str,
    *,
    include_recall: bool = False,
) -> Dict[str, Any]:
    """CLI helper: run LLM with prompt override, persist, and re-arm."""
    pool = await get_shared_pool()
    ctx = await load_interp_context(pool, interpretation_id)
    cfg = InterpretationConfig()
    company = await _resolve_company(pool, ctx)

    symbol = ctx.get("instrument_symbol") or "BTC/USDT"
    exchange = ctx.get("exchange") or "bybit"
    news_source = (ctx.get("source") or "discord").lower()
    clean_headline = strip_reply_prefix(ctx.get("headline") or "")
    clean_content = strip_reply_prefix(ctx.get("content") or "")
    news_context = f"{clean_headline}\n{clean_content}"[:1000]
    ctx_txt = _format_context_window(ctx.get("context_window"), ctx.get("author") or "")
    if ctx_txt:
        news_context = f"{news_context}\n\n{ctx_txt}"

    cid = new_correlation_id("reprocess")

    local_path = ctx["local_path"]
    if not local_path:
        raise ValueError("media has no local_path")

    from shared.utils.db import get_company_pool

    company_pool = await get_company_pool(company)
    quant, _prefilter, quant_symbol, _quant_src = await prepare_chart_hacker_quant(
        cfg=cfg,
        shared_pool=pool,
        company_pool=company_pool,
        image_path=local_path,
        text_symbol=symbol,
        exchange=exchange,
        correlation_id=cid,
        news_context=news_context,
    )

    recall = ""
    if include_recall:
        recall_symbol = quant_symbol if quant_symbol and quant_symbol != "UNKNOWN" else symbol
        recall = await _recall_relevant_memories(
            company=company,
            symbol=recall_symbol,
            direction=None,
            trader_handle=ctx.get("author"),
            shared_pool=pool,
            correlation_id=cid,
        )

    llm = await run_llm_track(
        cfg=cfg,
        image_path=local_path,
        news_context=news_context,
        instrument_symbol=symbol,
        correlation_id=cid,
        recall_context=recall,
        news_source=news_source,
        channel_name=str(ctx.get("channel_name") or ""),
        trader_profile_id=int(ctx["trader_profile_id"]),
        shared_pool=pool,
        prompt_version_override=prompt_version,
        chart_hacker_quant=quant,
        skip_prefilter=True,
    )

    consensus = run_consensus(llm, quant)

    ctx["prior_symbol"] = ctx.get("instrument_symbol")
    ctx["exchange"] = exchange
    ctx["headline"] = ctx.get("headline")
    ctx["content"] = ctx.get("content")

    return await persist_reinterpret_from_llm(
        pool,
        interpretation_id,
        ctx,
        llm,
        consensus,
        quant,
        rearm=True,
        correlation_id=cid,
        cancel_reason="reprocessed: prompt re-arm",
    )
