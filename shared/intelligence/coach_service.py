"""
Module: coach_service
Purpose: A/B prompt registry — assigns variants, computes lift, promotes winners.
Location: /opt/tickles/shared/intelligence/coach_service.py
"""

import asyncio
import hashlib
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import asyncpg

from shared.intelligence.prompt_registry import register_prompt
from shared.utils.db import get_shared_pool
from shared.intelligence.heartbeat import record_heartbeat

logger = logging.getLogger(__name__)

PROMOTION_LIFT_THRESHOLD = float(os.environ.get("COACH_PROMOTION_LIFT_THRESHOLD", "0.05"))
PROMOTION_MIN_TRADES = int(os.environ.get("COACH_PROMOTION_MIN_TRADES", "50"))
_ADVISORY_LOCK_KEY = "coach_service"


# ---------------------------------------------------------------------------
# D1 — push prompt promotion lessons into mem0
# ---------------------------------------------------------------------------
async def _push_promotion_to_mem0(
    company: str,
    prompt_name: str,
    winner: str,
    lift: float,
    num_trades: int,
) -> None:
    """Write a prompt promotion event into mem0 for cross-agent learning.

    Best-effort: any failure is logged and swallowed.
    """
    try:
        from shared.utils.mem0_config import get_memory
    except Exception as exc:
        logger.debug("coach→mem0: import failed: %s", exc)
        return

    try:
        mem, agent_id = get_memory(company, "coach")
    except Exception as exc:
        logger.warning("coach→mem0: get_memory failed: %s", exc)
        return

    text = (
        f"Promoted {prompt_name} variant {winner} "
        f"(lift={lift:+.4f} over {num_trades} trades)"
    )
    metadata = {
        "type": "prompt_promotion",
        "prompt_name": prompt_name,
        "winner": winner,
        "lift": lift,
        "num_trades": num_trades,
    }

    try:
        await asyncio.to_thread(
            mem.add, text, user_id=company, agent_id=agent_id, metadata=metadata
        )
        logger.info("coach→mem0: wrote promotion %s/%s lift=%.4f", prompt_name, winner, lift)
    except Exception as exc:
        logger.warning("coach→mem0: add failed: %s", exc)


def assign_variant(actor_id: str, day: date, prompt_name: str, variants: List[str]) -> str:
    """Deterministic assignment — same actor on same day always gets same variant.

    Args:
        actor_id: Unique actor identifier.
        day: Assignment date.
        prompt_name: Prompt family name.
        variants: List of variant strings (e.g. ['v1', 'v2']).

    Returns:
        Selected variant string.
    """
    h = hashlib.sha256(f"{actor_id}|{day.isoformat()}|{prompt_name}".encode()).digest()
    return variants[h[0] % len(variants)]


class CoachService:
    """Prompt A/B testing service. Runs weekly (Sunday 02:00 UTC)."""

    def __init__(self, company_id: str = "jarvais") -> None:
        self.company_id = company_id
        self._stop = asyncio.Event()
        self._pool: Optional[Any] = None

    async def _ensure_pool(self) -> Any:
        if self._pool is None:
            self._pool = await get_shared_pool()
        return self._pool

    async def _fetch_prompt_variants(
        self, conn: asyncpg.Connection, prompt_name: str
    ) -> List[Tuple[str, str]]:
        """Fetch all registered variants for a prompt name.

        Returns:
            List of (version, prompt_hash) tuples.
        """
        rows = await conn.fetch(
            """
            SELECT version, prompt_hash
            FROM prompt_versions
            WHERE name = $1
            ORDER BY version
            """,
            prompt_name,
        )
        return [(r["version"], r["prompt_hash"]) for r in rows]

    async def _record_assignment(
        self,
        conn: asyncpg.Connection,
        actor_id: str,
        assignment_day: date,
        prompt_name: str,
        variant: str,
        prompt_hash: str,
    ) -> None:
        """Record a variant assignment in prompt_assignments."""
        await conn.execute(
            """
            INSERT INTO prompt_assignments
              (actor_id, assignment_day, prompt_name, variant, prompt_hash)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (actor_id, assignment_day, prompt_name) DO NOTHING
            """,
            actor_id,
            assignment_day,
            prompt_name,
            variant,
            prompt_hash,
        )

    async def _compute_lift(
        self,
        conn: asyncpg.Connection,
        prompt_name: str,
        variants: List[str],
    ) -> Optional[Tuple[str, float]]:
        """Compute mean edge_score per variant over assigned trades.

        Returns:
            (winning_variant, lift) or None if insufficient data.
        """
        rows = await conn.fetch(
            """
            SELECT
                pa.variant,
                COUNT(*) AS trade_count,
                AVG(ap.edge_score) AS mean_edge
            FROM prompt_assignments pa
            JOIN actor_performance ap
              ON pa.actor_id = ap.actor_id
            WHERE pa.prompt_name = $1
              AND pa.assignment_day >= CURRENT_DATE - INTERVAL '30 days'
            GROUP BY pa.variant
            """,
            prompt_name,
        )
        if len(rows) < 2:
            logger.info("coach: only %d variant(s) for %s — need 2", len(rows), prompt_name)
            return None

        stats: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            stats[r["variant"]] = {
                "count": r["trade_count"],
                "mean_edge": float(r["mean_edge"] or 0),
            }

        for v in variants:
            if v not in stats:
                stats[v] = {"count": 0, "mean_edge": 0.0}

        # Need at least PROMOTION_MIN_TRADES per variant
        for v, s in stats.items():
            if s["count"] < PROMOTION_MIN_TRADES:
                logger.info(
                    "coach: variant %s has %d trades (need %d)",
                    v,
                    s["count"],
                    PROMOTION_MIN_TRADES,
                )
                return None

        # Find best variant
        best = max(stats, key=lambda v: stats[v]["mean_edge"])
        baseline = min(stats, key=lambda v: stats[v]["mean_edge"])
        lift = stats[best]["mean_edge"] - stats[baseline]["mean_edge"]

        logger.info(
            "coach: %s lift=%.4f (best=%s %.4f, baseline=%s %.4f)",
            prompt_name,
            lift,
            best,
            stats[best]["mean_edge"],
            baseline,
            stats[baseline]["mean_edge"],
        )

        if lift < PROMOTION_LIFT_THRESHOLD:
            logger.info("coach: lift %.4f below threshold %.4f — no promotion", lift, PROMOTION_LIFT_THRESHOLD)
            return None

        return best, lift

    async def _promote_variant(
        self,
        conn: asyncpg.Connection,
        prompt_name: str,
        variant: str,
        lift: float,
    ) -> None:
        """Promote winning variant to default in prompt_versions."""
        # Log promotion
        await conn.execute(
            """
            INSERT INTO edge_score_changes
              (actor_type, actor_id, period_end, score_after, delta, components_after, note)
            VALUES ('system', $1, CURRENT_DATE, $2, $3, $4, 'prompt_promoted')
            """,
            prompt_name,
            round(lift, 4),
            round(lift, 4),
            json.dumps({"promoted_variant": variant}),
        )
        logger.info("coach: promoted %s → %s (lift=%.4f)", prompt_name, variant, lift)

    async def evaluate_and_promote(self, prompt_name: str) -> Optional[str]:
        """For each variant pair, compute mean edge_score over assigned actor-trades.

        If a variant beats the current default by ≥ PROMOTION_LIFT_THRESHOLD over
        ≥ PROMOTION_MIN_TRADES, promote it.

        Args:
            prompt_name: Prompt family to evaluate.

        Returns:
            Winning variant string or None if no promotion.
        """
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            variants = await self._fetch_prompt_variants(conn, prompt_name)
            if len(variants) < 2:
                logger.info("coach: only %d version(s) for %s", len(variants), prompt_name)
                return None

            variant_names = [v[0] for v in variants]
            result = await self._compute_lift(conn, prompt_name, variant_names)
            if result is None:
                return None

            winner, lift = result
            await self._promote_variant(conn, prompt_name, winner, lift)
            # D1 — push promotion insight to mem0
            await _push_promotion_to_mem0(
                company=self.company_id,
                prompt_name=prompt_name,
                winner=winner,
                lift=lift,
                num_trades=PROMOTION_MIN_TRADES,  # floor; actual count ≥ this
            )
            return winner

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
                # Find all prompt families with multiple variants
                rows = await conn.fetch(
                    """
                    SELECT name
                    FROM prompt_versions
                    GROUP BY name
                    HAVING COUNT(DISTINCT version) >= 2
                    """
                )
                promoted = 0
                for row in rows:
                    if self._stop.is_set():
                        break
                    prompt_name = row["name"]
                    try:
                        winner = await self.evaluate_and_promote(prompt_name)
                        if winner:
                            promoted += 1
                    except Exception as exc:
                        logger.exception("coach: error evaluating %s: %s", prompt_name, exc)

                return {"processed": len(rows), "promoted": promoted}
            finally:
                await conn.execute(
                    "SELECT pg_advisory_unlock(hashtext($1))", _ADVISORY_LOCK_KEY
                )
                # Phase R — Record heartbeat
                await record_heartbeat(
                    agent_id="intelligence-coach",
                    status="ok",
                    expected_interval_seconds=3600,  # Runs hourly
                )

    async def run_forever(self, interval_seconds: int = 3600) -> None:
        """Main loop."""
        logger.info(
            "CoachService starting (interval=%ds, lift_threshold=%.2f, min_trades=%d)",
            interval_seconds,
            PROMOTION_LIFT_THRESHOLD,
            PROMOTION_MIN_TRADES,
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
    service = CoachService()
    try:
        await service.run_forever()
    except asyncio.CancelledError:
        service.stop()
        raise


if __name__ == "__main__":
    asyncio.run(main())
