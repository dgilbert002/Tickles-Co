"""
Module: chart_hacker_opinion_service
Purpose: Phase 8 ChartHackerOpinionService daemon — critic role watching open
         tracked_positions, calling vision LLM, writing agent_opinions.
         Never opens/closes positions. Never writes tracked_positions.
Location: /opt/tickles/shared/intelligence/chart_hacker_opinion_service.py
"""

import asyncio
import json
import logging
import os
import re
import signal
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import asyncpg

from shared.intelligence.gateway_config import GatewayConfig, chat_completion
from shared.intelligence.opinion_budget import get_opinion_budget
from shared.intelligence.prompt_registry import register_prompt
from shared.utils.api_cost_log import log_api_call
from shared.utils.db import get_shared_pool
from shared.intelligence.heartbeat import record_heartbeat
from shared.intelligence.critic_audit_log import (
    enrichment_summary,
    log_critic_opinion,
    trigger_reason_for,
)

logger = logging.getLogger(__name__)

# Tunables from environment
_MIN_CONFIDENCE = float(os.environ.get("OPINION_MIN_CONFIDENCE", "0.45"))
# Reasoning models (e.g. gemini-3-flash) burn tokens inside max_tokens; too low
# truncates JSON (finish_reason=length) → nothing saved → retry storm.
_OPINION_MAX_TOKENS = int(os.environ.get("OPINION_MAX_TOKENS", "8192"))
_TRADER_REASON_MAX_CHARS = int(os.environ.get("OPINION_TRADER_REASON_MAX_CHARS", "1500"))

# Advisory lock key — must be unique per service
_ADVISORY_LOCK_KEY = "chart_hacker_opinion"

_CRITIC_SYSTEM_PROMPT = (
    "You are a trading critic. Review the position context (symbol, direction, "
    "entry, current price, stop_loss, take_profit), any trader_reason text, "
    "and optional enrichment blocks (quant_now, at_entry, funding, "
    "timeframe_rsi, trader_stats_30d).\n\n"
    "IMPORTANT — missing or empty trader_reason is NORMAL (chart-only signals, "
    "TradingView links, emoji posts). Do NOT penalize would_take_trade or "
    "memo_confidence solely because reason is blank.\n\n"
    "Use quant_now (live 1m RSI/EMA/ATR) and timeframe_rsi (chart TF RSI) "
    "for momentum validation. Compare quant_now vs at_entry when present. "
    "Use funding for crowded-long/short context. trader_stats_30d is "
    "historical credibility — informative, not decisive.\n\n"
    "would_take_trade = whether YOU would take this setup given levels, R:R, "
    "entry timing vs current price, direction sanity, and quant — not whether "
    "the trader wrote a paragraph.\n\n"
    "Reply with STRICT JSON only (no markdown, no preamble): "
    '{"memo": string (<=500 chars), "memo_confidence": float in [0,1], '
    '"would_take_trade": bool, "suggested_sl": number|null, "suggested_tp": number|null}'
)


# ---------------------------------------------------------------------------
# D1 — push critic opinion insight into mem0 (recall surface for interpretation_service)
# ---------------------------------------------------------------------------
async def _push_opinion_to_mem0(
    company: str,
    row: asyncpg.Record,
    llm_result: Dict[str, Any],
) -> None:
    """Write a chart_hacker critic opinion into the chart_hacker mem0 namespace.

    Best-effort: any failure is logged and swallowed — the agent_opinions row
    is already in Postgres as the source of truth.  mem0 is the recall surface
    that interpretation_service queries when processing new signals.
    """
    try:
        from shared.utils.mem0_config import get_memory
    except Exception as exc:
        logger.debug("opinion→mem0: mem0_config import failed: %s", exc)
        return

    memo = str(llm_result.get("memo", "")).strip()
    if not memo:
        return

    symbol = row["instrument_symbol"]
    direction = row["direction"]
    position_id = int(row["position_id"])
    confidence = float(llm_result.get("memo_confidence", 0.0))
    would_take = bool(llm_result.get("would_take_trade"))
    actor = (row["actor_id"] or "unknown").strip()

    metadata = {
        "about": f"position:{position_id}",
        "symbol": symbol,
        "direction": direction,
        "confidence": confidence,
        "would_take_trade": would_take,
        "suggested_sl": llm_result.get("suggested_sl"),
        "suggested_tp": llm_result.get("suggested_tp"),
        "position_id": position_id,
    }

    try:
        mem, agent_id = get_memory(company, "chart_hacker")
    except Exception as exc:
        logger.warning("opinion→mem0: get_memory failed: %s", exc)
        return

    text = (
        f"Opinion on {symbol} {direction} (pos #{position_id}, "
        f"confidence={confidence:.2f}, would_take={would_take}): {memo}"
    )
    try:
        await asyncio.to_thread(
            mem.add,
            text,
            user_id=company,
            agent_id=agent_id,
            metadata=metadata,
        )
        logger.info(
            "opinion→mem0: wrote opinion for position_id=%s symbol=%s confidence=%.2f",
            position_id, symbol, confidence,
        )
    except Exception as exc:
        logger.warning(
            "opinion→mem0: mem.add failed (position_id=%s): %s",
            position_id, exc,
        )


class ChartHackerOpinionService:
    """Daemon that watches open positions and writes critic opinions to agent_opinions."""

    def __init__(self, company_id: str = "jarvais") -> None:
        self.company_id = company_id
        self._stop = asyncio.Event()
        self._pool: Optional[Any] = None
        self.prompt_version: Optional[str] = None

    async def _ensure_pool(self) -> Any:
        if self._pool is None:
            self._pool = await get_shared_pool()
        return self._pool

    async def _register_prompt(self) -> str:
        """Idempotently register the opinion prompt version."""
        # Reuse chart_analysis.json with mode='opinion_on_existing' switch
        system = _CRITIC_SYSTEM_PROMPT
        body = "mode=opinion_on_existing"
        self.prompt_version = await register_prompt(
            name="chart_analysis_opinion",
            version="2026.05.30-v3",
            system=system,
            body=body,
            taxonomy_rule=None,
            model_hint=None,
            created_by="chart_hacker_opinion_service",
        )
        return self.prompt_version

    async def _eligible_positions(self, conn: asyncpg.Connection) -> List[asyncpg.Record]:
        """Fetch open positions eligible for critic opinion.

        Bug H4 fix (part 1/3): the previous filter was
        ``actor_type IN ('trader', 'copy_bot', 'self')`` but the live DB only
        ever stores ``'agent'`` (autonomous bots) and ``'trader_human'``
        (Discord/Telegram traders). The query returned 0 rows for 2+ weeks,
        which is why this service had written 0 opinions despite being active.
        """
        rows = await conn.fetch(
            """
            SELECT
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
                (SELECT MAX(created_at)
                 FROM agent_opinions
                 WHERE position_id = p.id AND agent_name = 'chart_hacker'
                ) AS last_opinion_at
            FROM tracked_positions p
            WHERE p.status = 'open'
              AND p.actor_type IN ('trader_human', 'agent')
            """
        )
        return rows

    def _should_fire(self, row: asyncpg.Record) -> bool:
        """Determine if an opinion should be generated for this position.

        Fires only:
          1. First critic pass (no prior chart_hacker opinion).
          2. Trader/plan change after that (position ``updated_at`` advanced
             beyond ``price_updated_at`` — e.g. SL/TP edit, not a price tick).

        No hourly re-checks and no per-1% price-move polls; postmortem handles
        learning after the trade closes.
        """
        if row["last_opinion_at"] is None:
            return True

        last_opinion_at = row["last_opinion_at"]
        position_updated_at = row.get("position_updated_at")
        price_updated_at = row.get("price_updated_at")

        if position_updated_at and position_updated_at > last_opinion_at:
            # Price-only monitor ticks bump both timestamps together; level
            # edits (dedup refresh, manual SL/TP) advance updated_at alone.
            if price_updated_at is None or position_updated_at > price_updated_at:
                return True

        return False

    async def _run_vision_opinion(
        self,
        row: asyncpg.Record,
        enrichment: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Call the LLM for a critic opinion on an existing position.

        Bug H4 fix (parts 2/3 + 3/3):
          * Part 2: previous code called ``call_vision_llm(system_prompt=...,
            user_prompt=..., image_path=None, model=None, temperature=...)``,
            but the real signature requires ``cfg, model, system_prompt,
            user_text, image_b64, image_mime``. Every call raised TypeError,
            silently swallowed by the wrapping ``except Exception``. Since the
            ``_eligible_positions`` filter (part 1) was returning 0 rows the
            error never surfaced, but it would have crashed every call. We
            switch to ``chat_completion`` (text-only) — the critic reviews the
            position context + trader reason, no chart image required.
          * Part 3: the LLM JSON payload lives inside ``response["content"]``,
            not at the top level of the response object. Previous code read
            ``llm_result.get("memo_confidence")`` etc. directly, so every
            field came back ``None`` and every opinion had NULL SL/TP and an
            empty memo. We now parse the JSON body and merge usage metadata.
        """
        position_id = row["position_id"]

        context: Dict[str, Any] = {
            "position_id": position_id,
            "symbol": row["instrument_symbol"],
            "exchange": row["instrument_exchange"],
            "direction": row["direction"],
            "entry_price": float(row["entry_price"]) if row["entry_price"] else None,
            "current_price": float(row["current_price"]) if row["current_price"] else None,
            "stop_loss": float(row["stop_loss"]) if row["stop_loss"] else None,
            "take_profit": float(row["take_profit"]) if row["take_profit"] else None,
            "trader_reason": (row["entry_reason_trader"] or "")[:_TRADER_REASON_MAX_CHARS],
        }
        if enrichment:
            context.update(enrichment)
        user_text = json.dumps(context, separators=(",", ":"))
        system_prompt = _CRITIC_SYSTEM_PROMPT

        try:
            # Round 14: provider + model from the "chart_hacker_opinion" slot.
            from shared.intelligence.gateway_config import resolve_slot_gateway
            cfg, model = await resolve_slot_gateway("chart_hacker_opinion")
        except Exception as exc:
            logger.warning(
                "chart_hacker_opinion: resolve_slot_gateway failed (pos=%s): %s",
                position_id,
                exc,
            )
            return None

        try:
            response = await chat_completion(
                cfg=cfg,
                model=model,
                system_prompt=system_prompt,
                user_text=user_text,
                max_tokens=_OPINION_MAX_TOKENS,
                operation="chart_hacker_opinion",
                company_id=self.company_id,
                agent_id="chart_hacker",
            )
        except Exception as exc:
            logger.warning(
                "chart_hacker_opinion: chat_completion failed for pos=%s: %s",
                position_id,
                exc,
            )
            return None

        # Parse the JSON body inside ``content``.
        # Bug E fix (2026-05-24 second-round audit): the previous regex used
        # a GREEDY ``\{[\s\S]*\}`` which matches from the FIRST ``{`` all the
        # way to the LAST ``}`` in the response. If the LLM emitted preamble
        # + valid JSON + trailing commentary that happened to contain braces
        # (e.g. "{note: ...}"), or two JSON blocks in one response, the regex
        # would slurp everything between them and json.loads would either
        # crash or — worse — silently parse the wrong object and we'd write
        # garbage suggested_sl / suggested_tp.
        #
        # Strategy: prefer json.loads(content) directly; if that fails (code
        # fence, preamble, or trailing text), strip ```json fences then walk
        # candidate JSON objects starting at each ``{`` and return the first
        # one that parses successfully. Reject anything that isn't a dict.
        content_str = (response or {}).get("content", "") or ""
        parsed: Optional[Dict[str, Any]] = None
        if content_str:
            cleaned = content_str.strip()
            # Strip common ```json … ``` fences.
            fence = re.match(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, re.IGNORECASE)
            if fence:
                cleaned = fence.group(1).strip()
            try:
                candidate = json.loads(cleaned)
                if isinstance(candidate, dict):
                    parsed = candidate
            except Exception:
                # Fall back to scanning for the first VALID JSON object.
                for start in (i for i, ch in enumerate(cleaned) if ch == "{"):
                    # Find balanced object via incremental brace counting; use
                    # raw_decode (handles trailing junk gracefully).
                    try:
                        candidate, _end = json.JSONDecoder().raw_decode(cleaned[start:])
                    except Exception:
                        continue
                    if isinstance(candidate, dict):
                        parsed = candidate
                        break
            if parsed is None:
                logger.warning(
                    "chart_hacker_opinion: JSON parse failed for pos=%s | raw=%r",
                    position_id,
                    content_str[:200],
                )
                return None

        if parsed is None or not isinstance(parsed, dict):
            logger.warning(
                "chart_hacker_opinion: parsed payload is not an object for pos=%s",
                position_id,
            )
            return None

        # Reject empty / placeholder payloads — these used to write a useless
        # opinion row but still consume the daily opinion budget.
        memo_text = str(parsed.get("memo", "")).strip()
        if not memo_text:
            logger.warning(
                "chart_hacker_opinion: parsed payload has empty memo for pos=%s",
                position_id,
            )
            return None

        usage = (response or {}).get("usage", {}) or {}
        merged: Dict[str, Any] = {
            "memo": str(parsed.get("memo", ""))[:2000],
            "memo_confidence": float(parsed.get("memo_confidence", 0.0) or 0.0),
            "would_take_trade": bool(parsed.get("would_take_trade", False)),
            "suggested_sl": parsed.get("suggested_sl"),
            "suggested_tp": parsed.get("suggested_tp"),
            # Pass-through metadata so _process_position can log cost.
            "model_used": (response or {}).get("model", model),
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "cost_usd": float(usage.get("cost_usd", 0.0) or 0.0),
            "latency_ms": int(usage.get("latency_ms", 0) or 0),
            "provider": getattr(cfg, "gateway", "openrouter"),
        }
        return merged

    async def _write_opinion(
        self,
        conn: asyncpg.Connection,
        row: asyncpg.Record,
        llm_result: Dict[str, Any],
    ) -> None:
        """Write one agent_opinions row with memo confidence gate."""
        memo_conf = float(llm_result.get("memo_confidence", 0.0))
        _ = memo_conf >= _MIN_CONFIDENCE  # is_published gate (dashboard filters later)

        await conn.execute(
            """
            INSERT INTO agent_opinions (
                position_id, agent_name, agent_version,
                would_take_trade, agent_stop_loss, agent_take_profit,
                agent_confidence, reasoning, opinion_source,
                created_at, updated_at
            ) VALUES (
                $1, 'chart_hacker', $2,
                $3, $4, $5,
                $6, $7, 'daemon',
                now(), now()
            )
            ON CONFLICT (position_id, agent_name)
            DO UPDATE SET
                would_take_trade   = EXCLUDED.would_take_trade,
                agent_stop_loss    = EXCLUDED.agent_stop_loss,
                agent_take_profit  = EXCLUDED.agent_take_profit,
                agent_confidence   = EXCLUDED.agent_confidence,
                reasoning          = EXCLUDED.reasoning,
                updated_at         = now()
            """,
            row["position_id"],
            self.prompt_version or "unknown",
            llm_result.get("would_take_trade"),
            llm_result.get("suggested_sl"),
            llm_result.get("suggested_tp"),
            memo_conf,
            str(llm_result.get("memo", ""))[:2000],
        )

    async def _backfill_position_levels_from_critic(
        self,
        conn: asyncpg.Connection,
        row: asyncpg.Record,
        llm_result: Dict[str, Any],
    ) -> None:
        """Fill missing SL/TP on tracked_positions from critic suggestions."""
        if not llm_result.get("would_take_trade"):
            return
        if float(llm_result.get("memo_confidence", 0.0) or 0.0) < _MIN_CONFIDENCE:
            return

        from shared.intelligence.critic_levels import pick_critic_levels

        entry = float(row["entry_price"]) if row.get("entry_price") else 0.0
        direction = str(row.get("direction") or "")
        cur_sl = row.get("stop_loss")
        cur_tp = row.get("take_profit")
        missing_sl = cur_sl is None or float(cur_sl or 0) <= 0
        missing_tp = cur_tp is None or float(cur_tp or 0) <= 0
        if not missing_sl and not missing_tp:
            return

        new_sl, new_tp = pick_critic_levels(
            direction=direction,
            entry=entry,
            missing_sl=missing_sl,
            missing_tp=missing_tp,
            critic_sl=llm_result.get("suggested_sl"),
            critic_tp=llm_result.get("suggested_tp"),
        )
        if new_sl is None and new_tp is None:
            return

        await conn.execute(
            """
            UPDATE public.tracked_positions
            SET stop_loss = COALESCE($2, stop_loss),
                take_profit_1 = COALESCE($3, take_profit_1)
            WHERE id = $1
              AND status IN ('pending', 'open', 'tracking')
            """,
            int(row["position_id"]),
            new_sl,
            new_tp,
        )
        logger.info(
            "chart_hacker_opinion: backfilled levels position_id=%s sl=%s tp=%s (was missing sl=%s tp=%s)",
            row["position_id"], new_sl, new_tp, missing_sl, missing_tp,
        )

    async def _finalise_position(
        self, conn: asyncpg.Connection, position_id: int
    ) -> None:
        """End-of-life writeback: compute agent_pnl_pct and performance_delta."""
        await conn.execute(
            """
            WITH last_opinion AS (
                SELECT id FROM agent_opinions
                WHERE position_id = $1 AND agent_name = 'chart_hacker'
                ORDER BY created_at DESC LIMIT 1
            ),
            pos AS (
                SELECT entry_price, exit_price, direction, realized_pnl_pct
                FROM tracked_positions
                WHERE id = $1
            )
            UPDATE agent_opinions
            SET
                agent_pnl_pct = CASE
                    WHEN pos.direction = 'long' THEN
                        (pos.exit_price - pos.entry_price)
                        / NULLIF(pos.entry_price, 0) * 100
                    ELSE
                        (pos.entry_price - pos.exit_price)
                        / NULLIF(pos.entry_price, 0) * 100
                END,
                performance_delta = (
                    CASE
                        WHEN pos.direction = 'long' THEN
                            (pos.exit_price - pos.entry_price)
                            / NULLIF(pos.entry_price, 0) * 100
                        ELSE
                            (pos.entry_price - pos.exit_price)
                            / NULLIF(pos.entry_price, 0) * 100
                    END
                ) - COALESCE(pos.realized_pnl_pct, 0),
                updated_at = now()
            FROM last_opinion, pos
            WHERE agent_opinions.id = last_opinion.id
            """,
            position_id,
        )

    async def _process_position(self, conn: asyncpg.Connection, row: asyncpg.Record) -> None:
        """Process a single eligible position: budget check, LLM call, write."""
        position_id = row["position_id"]

        if not self._should_fire(row):
            return

        # [AY] Budget check
        # Round-6 sweep: try_acquire now returns a token; we use it with
        # release(token) so we refund the EXACT slot we acquired even under
        # interleaved acquires (BH2 #3, CA1 #3 follow-up). The new try/finally
        # below also closes the round-3 review gap (BH2 #4) where an exception
        # AFTER the LLM call but BEFORE record_cost() left the slot leaked.
        budget = get_opinion_budget()
        ok, why, token = await budget.try_acquire(position_id)
        if not ok:
            logger.info("chart_hacker_opinion: skipped position_id=%s reason=%s", position_id, why)
            return

        opinion_written = False
        llm_result: Optional[Dict[str, Any]] = None
        enrichment: Optional[Dict[str, Any]] = None
        trigger_reason = trigger_reason_for(row)
        audit_position_id = int(row["position_id"])
        audit_sig_id = row.get("signal_interpretation_id")
        audit_symbol = str(row.get("instrument_symbol") or "")
        audit_direction = str(row.get("direction") or "")
        audit_entry = float(row["entry_price"]) if row.get("entry_price") else None
        audit_sl = float(row["stop_loss"]) if row.get("stop_loss") else None
        audit_tp = float(row["take_profit"]) if row.get("take_profit") else None
        audit_cp = float(row["current_price"]) if row.get("current_price") else None
        audit_trader = str(row.get("actor_id") or "")[:80]
        audit_status = "ok"
        audit_error = ""
        audit_enrich = ""
        audit_model = ""
        try:
            from shared.intelligence.critic_context import build_critic_enrichment
            from shared.utils.db import get_company_pool

            shared_pool = await self._ensure_pool()
            company_pool = await get_company_pool(self.company_id)
            enrichment = await build_critic_enrichment(shared_pool, company_pool, row)
            audit_enrich = enrichment_summary(enrichment)
        except Exception as enrich_exc:
            logger.warning(
                "chart_hacker_opinion: enrichment failed for pos=%s: %s",
                position_id, enrich_exc,
            )
            audit_status = "enrichment_failed"
            audit_error = str(enrich_exc)[:300]
            enrichment = None

        if enrichment is not None:
            try:
                # [AZ] Vision LLM call
                llm_result = await self._run_vision_opinion(row, enrichment=enrichment)
                if llm_result is None:
                    logger.warning(
                        "chart_hacker_opinion: LLM failed for position_id=%s — "
                        "releasing budget slot (token=%s) to avoid call-rate cap "
                        "exhaustion.",
                        position_id, token,
                    )
                    audit_status = "llm_failed"
                    audit_error = "chat_completion returned None"
                    try:
                        log_critic_opinion(
                            position_id=audit_position_id,
                            signal_interpretation_id=int(audit_sig_id) if audit_sig_id else None,
                            symbol=audit_symbol,
                            direction=audit_direction,
                            entry=audit_entry,
                            sl=audit_sl,
                            tp=audit_tp,
                            current_price=audit_cp,
                            trader_handle=audit_trader,
                            trigger_reason=trigger_reason,
                            enrichment_present=audit_enrich,
                            status="llm_failed",
                            error="no_response",
                        )
                    except Exception:
                        pass
                    return  # finally-block refund handles it

                # Record cost in budget
                await budget.record_cost(float(llm_result.get("cost_usd", 0.0)))

                # Log cost
                try:
                    await log_api_call(
                        role="chart_hacker_opinion",
                        correlation_id=f"opinion-{position_id}-{datetime.now(timezone.utc).isoformat()}",
                        context=f"position_id={position_id}",
                        provider=llm_result.get("provider", ""),
                        model=llm_result.get("model_used", ""),
                        tokens_in=llm_result.get("input_tokens", 0),
                        tokens_out=llm_result.get("output_tokens", 0),
                        cost_usd=llm_result.get("cost_usd", 0.0),
                        latency_ms=llm_result.get("latency_ms", 0),
                        operation="chart_hacker_opinion",
                        success=True,
                    )
                except Exception as exc:
                    logger.warning("Failed to log cost for position_id=%s: %s", position_id, exc)

                # Write opinion row
                await self._write_opinion(conn, row, llm_result)
                await self._backfill_position_levels_from_critic(conn, row, llm_result)
                opinion_written = True
                logger.info(
                    "chart_hacker_opinion: wrote opinion for position_id=%s published=%s",
                    position_id,
                    float(llm_result.get("memo_confidence", 0.0)) >= _MIN_CONFIDENCE,
                )
            finally:
                if not opinion_written:
                    try:
                        await budget.release(position_id, token=token)
                    except Exception as refund_exc:
                        logger.exception(
                            "chart_hacker_opinion: budget refund failed for "
                            "position_id=%s token=%s: %s",
                            position_id, token, refund_exc,
                        )

        # D1 — push opinion insight into mem0 so interpretation_service can recall it
        if llm_result is not None:
            await _push_opinion_to_mem0(
                company=self.company_id,
                row=row,
                llm_result=llm_result,
            )
            audit_model = str(llm_result.get("model_used") or "")
            audit_status = "ok" if opinion_written else "write_failed"
        elif not audit_error:
            audit_status = "llm_failed"
            audit_error = audit_error or "no_response"

        try:
            log_critic_opinion(
                position_id=audit_position_id,
                signal_interpretation_id=int(audit_sig_id) if audit_sig_id else None,
                symbol=audit_symbol,
                direction=audit_direction,
                entry=audit_entry,
                sl=audit_sl,
                tp=audit_tp,
                current_price=audit_cp,
                trader_handle=audit_trader,
                trigger_reason=trigger_reason,
                enrichment_present=audit_enrich,
                model=audit_model,
                would_take_trade=llm_result.get("would_take_trade") if llm_result else None,
                memo_confidence=float(llm_result.get("memo_confidence", 0)) if llm_result else None,
                memo_preview=str(llm_result.get("memo", "") or "") if llm_result else "",
                suggested_sl=llm_result.get("suggested_sl") if llm_result else None,
                suggested_tp=llm_result.get("suggested_tp") if llm_result else None,
                cost_usd=float(llm_result.get("cost_usd", 0)) if llm_result else 0.0,
                tokens_in=int(llm_result.get("input_tokens", 0)) if llm_result else 0,
                tokens_out=int(llm_result.get("output_tokens", 0)) if llm_result else 0,
                latency_ms=int(llm_result.get("latency_ms", 0)) if llm_result else 0,
                status=audit_status,
                error=audit_error,
            )
        except Exception as exc:
            logger.debug("critic_audit: log call failed (non-fatal): %s", exc)

    async def tick(self) -> Dict[str, Any]:
        """Single processing tick with advisory lock."""
        pool = await self._ensure_pool()
        rows: List[asyncpg.Record] = []
        async with pool.acquire() as conn:
            got_lock = await conn.fetchval(
                "SELECT pg_try_advisory_lock(hashtext($1))", _ADVISORY_LOCK_KEY
            )
            if not got_lock:
                logger.debug("Advisory lock held — skipping tick")
                return {"processed": 0, "skipped_lock": True}

            try:
                if self.prompt_version is None:
                    await self._register_prompt()

                rows = await self._eligible_positions(conn)
                processed = 0
                for row in rows:
                    if self._stop.is_set():
                        break
                    try:
                        await self._process_position(conn, row)
                        processed += 1
                    except Exception as exc:
                        logger.exception(
                            "chart_hacker_opinion: error processing position_id=%s: %s",
                            row["position_id"],
                            exc,
                        )

                return {"processed": processed, "eligible": len(rows)}
            finally:
                await conn.execute(
                    "SELECT pg_advisory_unlock(hashtext($1))", _ADVISORY_LOCK_KEY
                )
                # Phase M.4 — Record heartbeat
                await record_heartbeat(
                    agent_id="chart-hacker-opinion",
                    status="ok",
                    expected_interval_seconds=60,  # Default interval
                )

    async def run_forever(self, interval_seconds: int = 60) -> None:
        """Main loop."""
        logger.info(
            "ChartHackerOpinionService starting (interval=%ds, min_confidence=%.2f)",
            interval_seconds,
            _MIN_CONFIDENCE,
        )
        while not self._stop.is_set():
            try:
                stats = await self.tick()
                logger.info("Tick complete: %s", stats)
            except Exception as exc:
                logger.exception("Tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    company_id = os.environ.get("COMPANY_ID", "jarvais")
    service = ChartHackerOpinionService(company_id=company_id)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, service.stop)

    try:
        await service.run_forever(interval_seconds=60)
    finally:
        logger.info("ChartHackerOpinionService shut down")


if __name__ == "__main__":
    asyncio.run(main())
