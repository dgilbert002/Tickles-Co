"""
Module: edge_scorer_service
Purpose: Daemon that computes edge_score for every actor across rolling windows.
Location: /opt/tickles/shared/intelligence/edge_scorer_service.py
"""

import asyncio
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import asyncpg

from shared.intelligence.edge_scorer import (
    FORMULA_VERSION,
    MIN_POSITIONS_FOR_DISPLAY,
    ScorerInputs,
    compute_edge_score,
)
from shared.utils.db import get_shared_pool
from shared.intelligence.heartbeat import record_heartbeat

logger = logging.getLogger(__name__)

_ADVISORY_LOCK_KEY = "edge_scorer"
_DELTA_THRESHOLD = float(os.environ.get("EDGE_SCORE_DELTA_THRESHOLD", "0.05"))


# ---------------------------------------------------------------------------
# D1 — push notable edge score results into mem0
# ---------------------------------------------------------------------------
async def _push_edge_summary_to_mem0(
    company: str,
    scored: int,
    top_actor: str,
    top_window: str,
    top_score: float,
) -> None:
    """Write a periodic edge-scoring summary into mem0.

    Best-effort: any failure is logged and swallowed.
    """
    try:
        from shared.utils.mem0_config import get_memory
    except Exception as exc:
        logger.debug("edge_scorer→mem0: import failed: %s", exc)
        return

    try:
        mem, agent_id = get_memory(company, "edge_scorer")
    except Exception as exc:
        logger.warning("edge_scorer→mem0: get_memory failed: %s", exc)
        return

    text = (
        f"Edge score cycle: scored {scored} actors. "
        f"Top: {top_actor} ({top_window}) edge_score={top_score:.4f}"
    )
    metadata = {
        "type": "edge_score_summary",
        "actors_scored": scored,
        "top_actor": top_actor,
        "top_window": top_window,
        "top_score": top_score,
    }

    try:
        await asyncio.to_thread(
            mem.add, text, user_id=company, agent_id=agent_id, metadata=metadata
        )
        logger.info("edge_scorer→mem0: pushed summary (%d actors)", scored)
    except Exception as exc:
        logger.warning("edge_scorer→mem0: add failed: %s", exc)


class EdgeScorerService:
    """Daemon that runs daily at 01:00 UTC; computes 7d/30d/90d/all windows."""

    def __init__(self, company_id: str = "jarvais") -> None:
        self.company_id = company_id
        self._stop = asyncio.Event()
        self._pool: Optional[Any] = None

    async def _ensure_pool(self) -> Any:
        if self._pool is None:
            self._pool = await get_shared_pool()
        return self._pool

    async def _fetch_actors(self, conn: asyncpg.Connection) -> List[asyncpg.Record]:
        """Fetch distinct actors with closed positions."""
        rows = await conn.fetch(
            """
            SELECT DISTINCT actor_type, actor_id
            FROM tracked_positions
            WHERE status = 'closed'
            """
        )
        return rows

    async def _fetch_inputs(
        self,
        conn: asyncpg.Connection,
        actor_type: str,
        actor_id: str,
        window: str,
    ) -> ScorerInputs:
        """Fetch all data needed for edge_score computation."""
        now = datetime.now(timezone.utc)
        if window == "7d":
            start = now - timedelta(days=7)
        elif window == "30d":
            start = now - timedelta(days=30)
        elif window == "90d":
            start = now - timedelta(days=90)
        else:
            start = datetime.min.replace(tzinfo=timezone.utc)

        closed_positions = await conn.fetch(
            """
            SELECT id,
                   realized_pnl_pct AS realised_pnl_pct,
                   stop_loss,
                   exit_price,
                   direction,
                   signal_timestamp AS opened_at,
                   closed_at,
                   0::numeric AS total_fees_pct
            FROM tracked_positions
            WHERE actor_type = $1 AND actor_id = $2
              AND status = 'closed'
              AND closed_at >= $3
            """,
            actor_type,
            actor_id,
            start,
        )

        postmortems = await conn.fetch(
            """
            SELECT position_id,
                   regime_at_entry AS market_regime,
                   NULL::numeric    AS edge_score,
                   NULL::numeric    AS reasoning_clarity_score
            FROM position_postmortems
            WHERE position_id IN (
                SELECT id FROM tracked_positions
                WHERE actor_type = $1 AND actor_id = $2
                  AND status = 'closed' AND closed_at >= $3
            )
            """,
            actor_type,
            actor_id,
            start,
        )

        opinions = await conn.fetch(
            """
            SELECT position_id, would_take_trade
            FROM agent_opinions
            WHERE position_id IN (
                SELECT id FROM tracked_positions
                WHERE actor_type = $1 AND actor_id = $2
                  AND status = 'closed' AND closed_at >= $3
            )
            """,
            actor_type,
            actor_id,
            start,
        )

        # Compute skill_score from the C1-C5 components
        # C1: consistency, C2: discipline, C3: reasoning_clarity, C4: recall_hit, C5: agreement
        c1 = 0.0; c1_avail = False
        c2 = 0.0; c2_avail = False
        c3 = 0.0; c3_avail = False
        c5 = 0.0; c5_avail = False
        # C3 reasoning_clarity: the column is hardcoded NULL in the SELECT.
        # Skip it - renormalization handles missing components.
        c3_avail = False
        for op in opinions:
            if op.get("would_take_trade") is not None:
                c5 = 0.5 + (0.5 if op["would_take_trade"] else 0.0)
                c5_avail = True
                break
        if recall_hit_rate is not None:
            c4 = recall_hit_rate
            c4_avail = True
        else:
            c4 = None
            c4_avail = False
        
        components = {}
        if c1_avail: components["consistency"] = c1
        if c2_avail: components["discipline"] = c2
        if c3_avail: components["reasoning_clarity"] = c3
        if c4_avail: components["recall_hit"] = c4
        if c5_avail: components["agreement_with_critic"] = c5
        
        if components:
            skill_score, _dropped = _compute_skill(components)
        else:
            skill_score = None

        return ScorerInputs(
            closed_positions=list(closed_positions),
            postmortems=list(postmortems),
            opinions=list(opinions),
            skill_score=skill_score,
        )

    async def _upsert(
        self,
        conn: asyncpg.Connection,
        actor_type: str,
        actor_id: str,
        window: str,
        out: Any,
    ) -> None:
        """Upsert actor_performance row."""
        now = datetime.now(timezone.utc)
        if window == "7d":
            period_start = (now - timedelta(days=7)).date()
        elif window == "30d":
            period_start = (now - timedelta(days=30)).date()
        elif window == "90d":
            period_start = (now - timedelta(days=90)).date()
        else:
            period_start = date.min
        period_end = now.date()

        await conn.execute(
            """
            INSERT INTO actor_performance
              (actor_type, actor_id, period_start, period_end,
               closed_position_count, edge_score, components_jsonb,
               weights_used_jsonb, confidence_low, formula_version, computed_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, now())
            ON CONFLICT (actor_type, actor_id, period_start, period_end, formula_version)
            DO UPDATE SET
              closed_position_count = EXCLUDED.closed_position_count,
              edge_score = EXCLUDED.edge_score,
              components_jsonb = EXCLUDED.components_jsonb,
              weights_used_jsonb = EXCLUDED.weights_used_jsonb,
              confidence_low = EXCLUDED.confidence_low,
              computed_at = EXCLUDED.computed_at
            """,
            actor_type,
            actor_id,
            period_start,
            period_end,
            len(out.components),
            out.edge_score,
            json.dumps(out.components),
            json.dumps(out.weights_used),
            out.confidence_low,
            out.formula_version,
        )

        # Phase 11 [K] dual-write to trader_performance for Discord-actor backward compat
        if actor_type == "trader":
            try:
                trader_profile_id = int(actor_id)
            except ValueError:
                trader_profile_id = None
            if trader_profile_id is not None:
                try:
                    await conn.execute(
                        """
                        INSERT INTO trader_performance
                          (trader_profile_id, total_positions, win_rate, calculated_at)
                        VALUES ($1, $2, $3, now())
                        ON CONFLICT (trader_profile_id)
                        DO UPDATE SET
                          total_positions = EXCLUDED.total_positions,
                          win_rate = EXCLUDED.win_rate,
                          calculated_at = EXCLUDED.calculated_at
                        """,
                        trader_profile_id,
                        len(out.components),
                        out.edge_score,
                    )
                    logger.debug(
                        "trader_performance dual-write: trader_profile_id=%s edge_score=%.4f",
                        trader_profile_id,
                        out.edge_score,
                    )
                except Exception as exc:
                    logger.warning(
                        "trader_performance dual-write failed for trader_profile_id=%s: %s",
                        trader_profile_id,
                        exc,
                    )

    async def _maybe_log_change(
        self,
        conn: asyncpg.Connection,
        actor_type: str,
        actor_id: str,
        window: str,
        out: Any,
    ) -> None:
        """Log to edge_score_changes if delta > threshold."""
        now = datetime.now(timezone.utc)
        period_end = now.date()

        prev = await conn.fetchrow(
            """
            SELECT edge_score, components_jsonb
            FROM actor_performance
            WHERE actor_type = $1 AND actor_id = $2
              AND period_end = $3
              AND formula_version = $4
            ORDER BY computed_at DESC
            LIMIT 1 OFFSET 1
            """,
            actor_type,
            actor_id,
            period_end,
            FORMULA_VERSION,
        )
        if prev is None:
            return

        delta = abs(out.edge_score - float(prev["edge_score"]))
        if delta > _DELTA_THRESHOLD:
            await conn.execute(
                """
                INSERT INTO edge_score_changes
                  (actor_type, actor_id, period_end, score_before, score_after,
                   delta, components_before, components_after)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                actor_type,
                actor_id,
                period_end,
                float(prev["edge_score"]),
                out.edge_score,
                round(delta, 4),
                prev["components_jsonb"],
                json.dumps(out.components),
            )
            logger.info(
                "edge_score change logged: %s/%s delta=%.4f",
                actor_type,
                actor_id,
                delta,
            )

    async def tick(self) -> Dict[str, Any]:
        """Single processing tick with advisory lock."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            got_lock = await conn.fetchval(
                "SELECT pg_try_advisory_lock(hashtext($1))", _ADVISORY_LOCK_KEY
            )
            if not got_lock:
                logger.debug("Advisory lock held — skipping tick")
                return {"processed": 0, "skipped_lock": True}

            try:
                actors = await self._fetch_actors(conn)
                scored = 0
                top_actor = ""
                top_window = ""
                top_score = -999.0
                for row in actors:
                    if self._stop.is_set():
                        break
                    actor_type = row["actor_type"]
                    actor_id = row["actor_id"]
                    for window in ("7d", "30d", "90d", "all"):
                        try:
                            inputs = await self._fetch_inputs(
                                conn, actor_type, actor_id, window
                            )
                            if len(inputs.closed_positions) < MIN_POSITIONS_FOR_DISPLAY:
                                continue
                            out = compute_edge_score(inputs)
                            await self._upsert(conn, actor_type, actor_id, window, out)
                            await self._maybe_log_change(
                                conn, actor_type, actor_id, window, out
                            )
                            if out.edge_score > top_score:
                                top_score = out.edge_score
                                top_actor = f"{actor_type}:{actor_id}"
                                top_window = window
                        except Exception as exc:
                            logger.exception(
                                "edge_scorer: error scoring %s/%s/%s: %s",
                                actor_type,
                                actor_id,
                                window,
                                exc,
                            )
                    scored += 1
                # D1 — push cycle summary to mem0
                if scored > 0:
                    await _push_edge_summary_to_mem0(
                        company=self.company_id,
                        scored=scored,
                        top_actor=top_actor,
                        top_window=top_window,
                        top_score=top_score,
                    )
                return {"processed": scored, "actors_scored": len(actors)}
            finally:
                await conn.execute(
                    "SELECT pg_advisory_unlock(hashtext($1))", _ADVISORY_LOCK_KEY
                )
                # Phase R — Record heartbeat
                await record_heartbeat(
                    agent_id="intelligence-edge-scorer",
                    status="ok",
                    expected_interval_seconds=3600,  # Runs hourly
                )

    async def run_forever(self, interval_seconds: int = 3600) -> None:
        """Main loop."""
        logger.info("EdgeScorerService starting (interval=%ds)", interval_seconds)
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
    service = EdgeScorerService()
    try:
        await service.run_forever()
    except asyncio.CancelledError:
        service.stop()
        raise


if __name__ == "__main__":
    asyncio.run(main())
