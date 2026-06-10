"""
Module: technique_tracker
Purpose: Turn the vision LLM's per-chart technique observations into a graded,
         queryable strategy ledger -- the "up-vote" learning loop.

How it works
------------
1.  The chart_analysis prompt asks the LLM for up to 5 ``techniques`` it
    observes on each chart (order blocks, anchored VWAP, fib retracement,
    session open play, ...).  They land in
    ``signal_interpretations.pattern_tags`` (existing column, jsonb array).
2.  When a tracked position closes, ``grade_techniques_for_position`` walks
    the originating interpretation's tags and upserts a win/loss row per
    (technique, trader, symbol_base, timeframe) into ``technique_stats``.
3.  ``top_techniques_context`` renders the proven performers as a prompt
    block injected into ``{recall_context}`` -- chart_hacker literally sees
    "order_block_retest on SOL by xnimrod: 8W/2L (+$312)" before reading the
    next chart.

Everything is best-effort: failures log and return, never raise into the
pipelines that call this.

Location: /opt/tickles/shared/intelligence/technique_tracker.py
"""

from __future__ import annotations

import logging
import re
from typing import Any, List, Optional

logger = logging.getLogger("tickles.intelligence.technique_tracker")

_NORM_RE = re.compile(r"[^a-z0-9]+")

# Tags that carry no strategy information -- don't grade them.
_GENERIC_TAGS = {
    "support", "resistance", "trend", "chart", "price", "long", "short",
    "bullish", "bearish", "up", "down", "buy", "sell", "unknown",
}


def _norm_technique(raw: str) -> Optional[str]:
    """Normalise a free-text technique tag to snake_case; drop generics."""
    if not raw or not isinstance(raw, str):
        return None
    t = _NORM_RE.sub("_", raw.strip().lower()).strip("_")[:80]
    if not t or t in _GENERIC_TAGS or len(t) < 3:
        return None
    return t


def _symbol_base(symbol: Optional[str]) -> str:
    if not symbol:
        return ""
    s = str(symbol).upper()
    for sep in ("/", ":", "-"):
        if sep in s:
            s = s.split(sep)[0]
    return s[:20]


async def grade_techniques_for_position(
    conn: Any,
    position_id: int,
    validations: Any = None,
) -> int:
    """Grade every technique observed on the chart that armed ``position_id``.

    Reads the position's outcome + the originating signal_interpretation's
    pattern_tags / setup_tags, and upserts one technique_stats row per tag.

    ``validations`` (optional) is the postmortem's ``techniques_validated``
    list: [{"technique": str, "played_out": bool|None, "note": str}].
    When provided, it refines the vote: a technique the postmortem REFUTED
    (played_out=false) is graded as a loss even on a winning trade, and a
    CONFIRMED technique (played_out=true) is graded as a win even on a
    losing trade (correct read, overwhelmed by other factors). Unknown/null
    falls back to the trade outcome.
    Returns the number of techniques graded (0 on any failure).
    """
    try:
        row = await conn.fetchrow(
            """
            SELECT tp2.id, tp2.realized_pnl_usd, tp2.instrument_symbol,
                   tp2.signal_interpretation_id, tp2.status,
                   tp2.realized_pnl_pct, tp2.entry_price, tp2.stop_loss,
                   tp2.take_profit_1,
                   t.handle_normalized AS trader_handle,
                   si.pattern_tags, si.setup_tags, si.timeframe
            FROM public.tracked_positions tp2
            LEFT JOIN public.trader_profiles t ON t.id = tp2.trader_profile_id
            LEFT JOIN public.signal_interpretations si
                   ON si.id = tp2.signal_interpretation_id
            WHERE tp2.id = $1
            """,
            position_id,
        )
        if not row or row["status"] not in ("closed",):
            return 0

        pnl = float(row["realized_pnl_usd"] or 0)
        if pnl == 0:
            return 0  # expired / zero outcome teaches nothing

        import json as _json
        tags: List[str] = []
        for col in ("pattern_tags", "setup_tags"):
            v = row[col]
            if isinstance(v, str):
                try:
                    v = _json.loads(v)
                except Exception:
                    v = []
            if isinstance(v, list):
                tags.extend(str(x) for x in v)

        techniques = []
        seen = set()
        for raw in tags:
            t = _norm_technique(raw)
            if t and t not in seen:
                seen.add(t)
                techniques.append(t)
        techniques = techniques[:5]
        if not techniques:
            return 0

        outcome = "win" if pnl > 0 else "loss"
        # Realised pct + planned RR (reward:risk from the signal's levels)
        pnl_pct = float(row["realized_pnl_pct"] or 0)
        entry_f = float(row["entry_price"] or 0)
        sl_f = float(row["stop_loss"] or 0)
        tp_f = float(row["take_profit_1"] or 0)
        planned_rr = 0.0
        if entry_f > 0 and sl_f > 0 and tp_f > 0 and abs(entry_f - sl_f) > 1e-12:
            planned_rr = abs(tp_f - entry_f) / abs(entry_f - sl_f)
        # Validation map from the postmortem (technique -> played_out)
        vmap = {}
        if isinstance(validations, list):
            for v in validations:
                if isinstance(v, dict) and v.get("technique"):
                    t_norm = _norm_technique(str(v["technique"]))
                    if t_norm:
                        vmap[t_norm] = v.get("played_out")
        trader = (row["trader_handle"] or "")[:120]
        base = _symbol_base(row["instrument_symbol"])
        tf = (row["timeframe"] or "")[:8]

        # Validation notes (technique -> note) for auto-describing new techniques
        nmap = {}
        if isinstance(validations, list):
            for v in validations:
                if isinstance(v, dict) and v.get("technique") and v.get("note"):
                    t_norm = _norm_technique(str(v["technique"]))
                    if t_norm:
                        nmap[t_norm] = str(v["note"])[:300]

        graded = 0
        for tech in techniques:
            # Auto-catalog: ensure every observed technique has a catalog row.
            # Seeded rows keep their curated description; NEW techniques the
            # LLM invents get the postmortem's validation note as an initial
            # description (free — no extra LLM call), upgradeable later.
            try:
                await conn.execute(
                    """INSERT INTO public.technique_catalog (technique, description, category)
                       VALUES ($1, $2, 'observed')
                       ON CONFLICT (technique) DO UPDATE
                       SET description = EXCLUDED.description, updated_at = NOW()
                       WHERE technique_catalog.description = ''""",
                    tech, nmap.get(tech, ""),
                )
            except Exception as _cat_exc:
                logger.debug("technique catalog upsert failed for %s: %s", tech, _cat_exc)

            # Per-technique outcome: postmortem validation overrides the
            # trade-level outcome when it gave a definitive verdict.
            t_outcome = outcome
            if tech in vmap and vmap[tech] is not None:
                t_outcome = "win" if vmap[tech] else "loss"
            # Two rows per technique: trader-specific and global ('' trader)
            for handle in (trader, ""):
                await conn.execute(
                    """
                    INSERT INTO public.technique_stats
                        (technique, trader_handle, symbol_base, timeframe,
                         wins, losses, total_pnl_usd, sample_count,
                         last_outcome, last_position_id,
                         sum_rr, sum_win_pct, sum_loss_pct, updated_at)
                    VALUES ($1, $2, $3, $4,
                            CASE WHEN $5 = 'win' THEN 1 ELSE 0 END,
                            CASE WHEN $5 = 'loss' THEN 1 ELSE 0 END,
                            $6, 1, $5, $7,
                            $8,
                            CASE WHEN $9 > 0 THEN $9 ELSE 0 END,
                            CASE WHEN $9 < 0 THEN -$9 ELSE 0 END,
                            NOW())
                    ON CONFLICT (technique, trader_handle, symbol_base, timeframe)
                    DO UPDATE SET
                        wins         = technique_stats.wins   + CASE WHEN $5 = 'win'  THEN 1 ELSE 0 END,
                        losses       = technique_stats.losses + CASE WHEN $5 = 'loss' THEN 1 ELSE 0 END,
                        total_pnl_usd = technique_stats.total_pnl_usd + $6,
                        sample_count = technique_stats.sample_count + 1,
                        last_outcome = $5,
                        last_position_id = $7,
                        sum_rr       = technique_stats.sum_rr + $8,
                        sum_win_pct  = technique_stats.sum_win_pct  + CASE WHEN $9 > 0 THEN $9 ELSE 0 END,
                        sum_loss_pct = technique_stats.sum_loss_pct + CASE WHEN $9 < 0 THEN -$9 ELSE 0 END,
                        updated_at   = NOW()
                    """,
                    tech, handle, base if handle else "", tf if handle else "",
                    t_outcome, pnl, position_id, planned_rr, pnl_pct,
                )
            graded += 1

        logger.info(
            "technique_tracker: graded %d techniques for position_id=%s (%s, pnl=%.2f)",
            graded, position_id, outcome, pnl,
        )
        return graded
    except Exception as exc:
        logger.warning("technique_tracker: grading failed for position_id=%s: %s",
                       position_id, exc)
        return 0


async def top_techniques_context(
    pool: Any,
    *,
    trader_handle: Optional[str] = None,
    symbol: Optional[str] = None,
    min_samples: int = 3,
    limit: int = 6,
) -> str:
    """Render proven techniques as a prompt block for {recall_context}.

    Prefers trader-specific rows when a handle is given; falls back to global
    rows. Only techniques with >= min_samples closed trades qualify.
    Returns '' when nothing qualifies (never raises).
    """
    try:
        base = _symbol_base(symbol)
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT technique, trader_handle, symbol_base,
                       wins, losses, total_pnl_usd, sample_count
                FROM public.technique_stats
                WHERE sample_count >= $1
                  AND ($2 = '' OR trader_handle = $2 OR trader_handle = '')
                  AND ($3 = '' OR symbol_base = $3 OR symbol_base = '')
                ORDER BY (wins::float / GREATEST(wins + losses, 1)) DESC,
                         total_pnl_usd DESC
                LIMIT $4
                """,
                min_samples, (trader_handle or "").lower()[:120], base, limit,
            )
        if not rows:
            return ""
        lines = []
        for r in rows:
            wr = r["wins"] / max(r["wins"] + r["losses"], 1) * 100
            who = f" ({r['trader_handle']})" if r["trader_handle"] else ""
            what = f" on {r['symbol_base']}" if r["symbol_base"] else ""
            lines.append(
                f"{r['technique']}{who}{what}: {r['wins']}W/{r['losses']}L "
                f"({wr:.0f}%, ${float(r['total_pnl_usd']):+.0f} over {r['sample_count']} trades)"
            )
        return "Technique track record (graded from closed trades):\n- " + "\n- ".join(lines)
    except Exception as exc:
        logger.debug("technique_tracker: context build failed: %s", exc)
        return ""
