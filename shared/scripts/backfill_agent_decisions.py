"""
Module: backfill_agent_decisions
Purpose: Backfill agent_decisions from tracked_positions and signal_interpretations.
         Creates agent_personas for each distinct actor_id, then populates
         agent_decisions with trade_open/trade_close/opinion rows.
Location: /opt/tickles/shared/scripts/backfill_agent_decisions.py

Idempotent: uses ON CONFLICT DO NOTHING for both tables.
CLI:
    --dry-run (default)  no DB writes, prints planned actions
    --apply              actually write
"""

import argparse
import logging
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from shared.utils.db import DatabasePool, get_shared_pool

logger = logging.getLogger(__name__)

# Mode constants for agent_decisions.mode
MODE_TRADE_OPEN = "trade_open"
MODE_TRADE_CLOSE = "trade_close"
MODE_OPINION = "opinion"

# Role constants for agent_personas.role
ROLE_SURGEON = "surgeon"
ROLE_CHART_HACKER = "chart_hacker"
ROLE_TRADER_HUMAN = "trader_human"
ROLE_AGENT = "agent"


def _actor_role(actor_type: str) -> str:
    """Map actor_type to a persona role string.

    Args:
        actor_type: The actor_type from tracked_positions.

    Returns:
        Role string for agent_personas.role.
    """
    mapping = {
        "surgeon": ROLE_SURGEON,
        "agent": ROLE_AGENT,
        "trader_human": ROLE_TRADER_HUMAN,
    }
    return mapping.get(actor_type, actor_type or ROLE_AGENT)


def _to_decimal(value: Any) -> Optional[Decimal]:
    """Coerce a value to Decimal, returning None when not parseable."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


async def fetch_distinct_actors(pool: DatabasePool) -> List[Dict[str, Any]]:
    """Fetch distinct actor_id/actor_type combos from tracked_positions.

    Also includes 'chart_hacker' as a synthetic persona for chart_hacker_trades.

    Args:
        pool: Shared Postgres pool.

    Returns:
        List of dicts with actor_id and actor_type.
    """
    rows = await pool.fetch_all(
        """
        SELECT DISTINCT actor_id, actor_type
        FROM public.tracked_positions
        WHERE actor_id IS NOT NULL
        ORDER BY actor_id
        """
    )
    # Add synthetic chart_hacker if not already present
    has_ch = any(r["actor_id"] == "chart_hacker" for r in rows)
    if not has_ch:
        rows.append({"actor_id": "chart_hacker", "actor_type": "agent"})
    return rows


async def ensure_personas(
    pool: DatabasePool,
    actors: List[Dict[str, Any]],
    *,
    dry_run: bool = True,
) -> Dict[str, int]:
    """Ensure agent_personas rows exist for each actor, return name→id map.

    Args:
        pool: Shared Postgres pool.
        actors: List of actor dicts from fetch_distinct_actors.
        dry_run: If True, only log planned actions.

    Returns:
        Dict mapping actor_id (name) to agent_personas.id.
    """
    name_to_id: Dict[str, int] = {}

    for actor in actors:
        actor_id = actor["actor_id"]
        actor_type = actor.get("actor_type", "agent")
        role = _actor_role(actor_type)

        if dry_run:
            logger.info(
                "[DRY-RUN] Would ensure persona: name=%s role=%s",
                actor_id,
                role,
            )
            continue

        try:
            row = await pool.fetch_one(
                """
                INSERT INTO public.agent_personas (name, role, description, enabled)
                VALUES ($1, $2, $3, true)
                ON CONFLICT (name) DO UPDATE SET
                    role = EXCLUDED.role,
                    updated_at = now()
                RETURNING id
                """,
                (
                    actor_id,
                    role,
                    f"Backfilled persona for {actor_id} ({actor_type})",
                ),
            )
            name_to_id[actor_id] = int(row["id"])
            logger.info("Ensured persona: name=%s role=%s id=%s", actor_id, role, row["id"])
        except Exception as exc:
            logger.exception("Failed to ensure persona for %s: %s", actor_id, exc)

    return name_to_id


async def backfill_from_positions(
    pool: DatabasePool,
    persona_map: Dict[str, int],
    *,
    dry_run: bool = True,
) -> int:
    """Backfill agent_decisions from tracked_positions.

    For each tracked_position with non-null actor_id:
      - Creates a trade_open decision (mode=trade_open)
      - If status='closed', also creates a trade_close decision

    Args:
        pool: Shared Postgres pool.
        persona_map: Dict mapping actor_id → agent_personas.id.
        dry_run: If True, only count and log.

    Returns:
        Number of rows that would be / were inserted.
    """
    rows = await pool.fetch_all(
        """
        SELECT
            id, actor_id, actor_type, company_id, correlation_id,
            direction, instrument_symbol, detection_confidence,
            entry_reason_agent, entry_reason_llm, entry_reason_trader,
            exit_reason_trader, exit_reason_llm, exit_reason_system,
            status, outcome, created_at, closed_at, signal_timestamp,
            metadata
        FROM public.tracked_positions
        WHERE actor_id IS NOT NULL
        ORDER BY created_at
        """
    )

    count = 0
    inserted = 0
    skipped = 0
    for row in rows:
        actor_id = row["actor_id"]
        persona_id = persona_map.get(actor_id)
        if persona_id is None:
            logger.warning("No persona for actor_id=%s, skipping position %s", actor_id, row["id"])
            continue

        company_id = row.get("company_id") or "jarvais"
        correlation_id = row.get("correlation_id") or f"pos_{row['id']}"
        direction = row.get("direction") or "neutral"
        confidence = _to_decimal(row.get("detection_confidence")) or Decimal("0.5")
        decided_at = row.get("signal_timestamp") or row.get("created_at")
        if decided_at and decided_at.tzinfo is None:
            decided_at = decided_at.replace(tzinfo=timezone.utc)
        elif decided_at is None:
            decided_at = datetime.now(timezone.utc)

        # Build rationale from available reason fields
        rationale = (
            row.get("entry_reason_agent")
            or row.get("entry_reason_llm")
            or row.get("entry_reason_trader")
            or ""
        )

        symbol = row.get("instrument_symbol") or "UNKNOWN"

        # Trade open decision
        open_corr_id = f"{correlation_id}_open"
        if not dry_run:
            try:
                existing = await pool.fetch_one(
                    """
                    SELECT 1 FROM public.agent_decisions
                    WHERE persona_id = $1 AND correlation_id = $2 AND mode = $3
                    LIMIT 1
                    """,
                    (persona_id, open_corr_id, MODE_TRADE_OPEN),
                )
                if existing is None:
                    await pool.execute(
                        """
                        INSERT INTO public.agent_decisions
                            (persona_id, company_id, correlation_id, mode, verdict,
                             confidence, rationale, inputs, outputs, metadata, decided_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb, $10::jsonb, $11)
                        """,
                        (
                            persona_id,
                            company_id,
                            open_corr_id,
                            MODE_TRADE_OPEN,
                            direction,
                            confidence,
                            rationale[:2000] if rationale else None,
                            f'{{"source_table": "tracked_positions", "source_id": {row["id"]}, "symbol": "{symbol}"}}',
                            f'{{"direction": "{direction}", "confidence": {float(confidence)}}}',
                            f'{{"actor_type": "{row.get("actor_type", "")}", "backfilled": true}}',
                            decided_at,
                        ),
                    )
                    inserted += 1
                else:
                    skipped += 1
            except Exception as exc:
                logger.exception("Failed to insert trade_open for position %s: %s", row["id"], exc)
        count += 1

        # Trade close decision (only for closed positions)
        if row.get("status") == "closed":
            close_rationale = (
                row.get("exit_reason_trader")
                or row.get("exit_reason_llm")
                or row.get("exit_reason_system")
                or ""
            )
            closed_at = row.get("closed_at") or decided_at
            if closed_at and closed_at.tzinfo is None:
                closed_at = closed_at.replace(tzinfo=timezone.utc)

            close_corr_id = f"{correlation_id}_close"
            if not dry_run:
                try:
                    existing = await pool.fetch_one(
                        """
                        SELECT 1 FROM public.agent_decisions
                        WHERE persona_id = $1 AND correlation_id = $2 AND mode = $3
                        LIMIT 1
                        """,
                        (persona_id, close_corr_id, MODE_TRADE_CLOSE),
                    )
                    if existing is None:
                        await pool.execute(
                            """
                            INSERT INTO public.agent_decisions
                                (persona_id, company_id, correlation_id, mode, verdict,
                                 confidence, rationale, inputs, outputs, metadata, decided_at)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb, $10::jsonb, $11)
                            """,
                            (
                                persona_id,
                                company_id,
                                close_corr_id,
                                MODE_TRADE_CLOSE,
                                direction,
                                confidence,
                                close_rationale[:2000] if close_rationale else None,
                                f'{{"source_table": "tracked_positions", "source_id": {row["id"]}, "symbol": "{symbol}", "outcome": "{row.get("outcome", "")}"}}',
                                f'{{"direction": "{direction}", "outcome": "{row.get("outcome", "")}"}}',
                                f'{{"actor_type": "{row.get("actor_type", "")}", "backfilled": true}}',
                                closed_at,
                            ),
                        )
                except Exception as exc:
                    logger.exception("Failed to insert trade_close for position %s: %s", row["id"], exc)
            count += 1

    return count


async def backfill_from_opinions(
    pool: DatabasePool,
    persona_map: Dict[str, int],
    *,
    dry_run: bool = True,
) -> int:
    """Backfill agent_decisions from signal_interpretations with chart_hacker_trades.

    Each non-empty chart_hacker_trades JSON array element becomes an opinion row.

    Args:
        pool: Shared Postgres pool.
        persona_map: Dict mapping actor_id → agent_personas.id.
        dry_run: If True, only count and log.

    Returns:
        Number of rows that would be / were inserted.
    """
    chart_hacker_pid = persona_map.get("chart_hacker")
    if chart_hacker_pid is None:
        logger.warning("No chart_hacker persona found, skipping opinions backfill")
        return 0

    rows = await pool.fetch_all(
        """
        SELECT
            id, correlation_id, consensus_direction, consensus_confidence,
            instrument_symbol, llm_reasoning, chart_hacker_trades, created_at
        FROM public.signal_interpretations
        WHERE chart_hacker_trades IS NOT NULL
          AND chart_hacker_trades::text != '[]'
        ORDER BY created_at
        """
    )

    count = 0
    for row in rows:
        trades = row.get("chart_hacker_trades")
        if not trades or (isinstance(trades, list) and len(trades) == 0):
            continue

        # trades is a list of dicts from JSONB
        if isinstance(trades, str):
            import json
            try:
                trades = json.loads(trades)
            except Exception:
                continue

        if not isinstance(trades, list):
            continue

        si_id = row["id"]
        correlation_id = row.get("correlation_id") or f"si_{si_id}"
        llm_reasoning = row.get("llm_reasoning") or ""
        decided_at = row.get("created_at")
        if decided_at and decided_at.tzinfo is None:
            decided_at = decided_at.replace(tzinfo=timezone.utc)
        elif decided_at is None:
            decided_at = datetime.now(timezone.utc)

        for idx, trade in enumerate(trades):
            if not isinstance(trade, dict):
                continue

            direction = trade.get("direction") or row.get("consensus_direction") or "neutral"
            confidence = _to_decimal(trade.get("confidence")) or _to_decimal(
                row.get("consensus_confidence")
            ) or Decimal("0.5")
            rationale = trade.get("rationale") or llm_reasoning
            symbol = trade.get("symbol") or row.get("instrument_symbol") or "UNKNOWN"

            opinion_corr_id = f"{correlation_id}_opinion_{idx}"
            if not dry_run:
                try:
                    existing = await pool.fetch_one(
                        """
                        SELECT 1 FROM public.agent_decisions
                        WHERE persona_id = $1 AND correlation_id = $2 AND mode = $3
                        LIMIT 1
                        """,
                        (chart_hacker_pid, opinion_corr_id, MODE_OPINION),
                    )
                    if existing is None:
                        await pool.execute(
                            """
                            INSERT INTO public.agent_decisions
                                (persona_id, company_id, correlation_id, mode, verdict,
                                 confidence, rationale, inputs, outputs, metadata, decided_at)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb, $10::jsonb, $11)
                            """,
                            (
                                chart_hacker_pid,
                                "jarvais",
                                opinion_corr_id,
                                MODE_OPINION,
                                direction,
                                confidence,
                                (rationale or "")[:2000],
                                f'{{"source_table": "signal_interpretations", "source_id": {si_id}, "symbol": "{symbol}"}}',
                                f'{{"direction": "{direction}", "confidence": {float(confidence)}, "trade_idx": {idx}}}',
                                '{"backfilled": true}',
                                decided_at,
                            ),
                        )
                except Exception as exc:
                    logger.exception(
                        "Failed to insert opinion for si_id=%s idx=%s: %s", si_id, idx, exc
                    )
            count += 1

    return count


async def main(dry_run: bool = True) -> None:
    """Run the backfill.

    Args:
        dry_run: If True, no writes are performed.
    """
    pool: Optional[DatabasePool] = None
    try:
        pool = await get_shared_pool()
    except Exception as exc:
        logger.exception("Failed to connect to shared pool: %s", exc)
        sys.exit(1)

    try:
        # Step 1: Fetch distinct actors
        actors = await fetch_distinct_actors(pool)
        logger.info("Found %s distinct actor_ids", len(actors))

        # Step 2: Ensure personas exist
        persona_map = await ensure_personas(pool, actors, dry_run=dry_run)
        if dry_run:
            logger.info("[DRY-RUN] Would create/update %s personas", len(actors))
        else:
            logger.info("Ensured %s personas (id map has %s entries)", len(actors), len(persona_map))

        # Step 3: Backfill from tracked_positions
        pos_count = await backfill_from_positions(pool, persona_map, dry_run=dry_run)
        logger.info(
            "%s %s agent_decisions rows from tracked_positions",
            "[DRY-RUN] Would process" if dry_run else "Processed",
            pos_count,
        )

        # Step 4: Backfill from signal_interpretations
        op_count = await backfill_from_opinions(pool, persona_map, dry_run=dry_run)
        logger.info(
            "%s %s agent_decisions rows from signal_interpretations (skipped existing)",
            "[DRY-RUN] Would process" if dry_run else "Processed",
            op_count,
        )

        total = pos_count + op_count
        logger.info("Backfill complete. Total agent_decisions in DB: see DB query.")

    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill agent_decisions from existing data")
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Actually write to DB (default: dry-run only)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    import asyncio
    asyncio.run(main(dry_run=not args.apply))
