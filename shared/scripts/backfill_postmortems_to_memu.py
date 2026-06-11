"""
Module: backfill_postmortems_to_memu
Purpose: One-shot back-fill that promotes EXISTING graded postmortem lessons
         (position_postmortems) into MemU institutional memory via the durable
         memu_outbox, the same path the live PostMortemService now uses (Phase A).
Location: /opt/tickles/shared/scripts/backfill_postmortems_to_memu.py

Why:
  Before Phase A the 456 graded postmortems only landed in per-company mem0;
  MemU (cross-company institutional memory) held outcome-less signal snapshots.
  This script back-fills the historical lessons so a fresh search of MemU returns
  real, graded, outcome-tagged lessons instead of signal noise.

Safety:
  * Dry-run by DEFAULT. Pass --apply to actually queue outbox rows.
  * Reuses postmortem_service._build_postmortem_broadcast so the payload format
    is byte-identical to the live path. MemU dedups on (kind, content_hash),
    so re-running --apply is idempotent at the insights layer.
  * Only postmortems with a company lesson OR a detected edge are promoted
    (rows without institutional-value content are skipped).

Usage:
    python3 -m shared.scripts.backfill_postmortems_to_memu            # dry-run
    python3 -m shared.scripts.backfill_postmortems_to_memu --apply    # execute
    python3 -m shared.scripts.backfill_postmortems_to_memu --company jarvais --apply
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any, Dict, List, Optional

from shared.intelligence.postmortem_service import (
    _broadcast_to_memu,
    _build_postmortem_broadcast,
)
from shared.utils.db import get_shared_pool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("backfill_postmortems_to_memu")

_FETCH_SQL = """
    SELECT pm.position_id,
           pm.lessons_for_company,
           pm.lessons_for_actor,
           pm.edge_detected,
           pm.why_it_failed,
           pm.why_it_worked,
           pm.correlation_id,
           pm.created_at,
           tp.instrument_symbol,
           tp.instrument_exchange,
           tp.direction,
           tp.outcome,
           tp.signal_source,
           tp.company_id,
           prof.handle_normalized AS trader_handle
      FROM public.position_postmortems pm
      JOIN public.tracked_positions   tp   ON tp.id = pm.position_id
 LEFT JOIN public.trader_profiles     prof ON prof.id = tp.trader_profile_id
     WHERE (pm.lessons_for_company IS NOT NULL
            AND length(trim(pm.lessons_for_company)) > 0)
        OR (pm.edge_detected IS NOT NULL
            AND length(trim(pm.edge_detected)) > 0)
     {company_filter}
  ORDER BY pm.created_at ASC
"""


async def backfill(*, apply: bool, company: Optional[str]) -> Dict[str, int]:
    """Promote historical postmortem lessons into MemU.

    Args:
        apply: When False (default) only counts/previews; when True queues
            outbox rows for the memu-listener to write into MemU.
        company: Optional company_id filter (e.g. 'jarvais'). None = all.

    Returns:
        Summary dict: {"candidates", "promotable", "queued", "skipped"}.
    """
    logger.info("backfill: start apply=%s company=%s", apply, company or "<all>")
    pool = await get_shared_pool()

    company_filter = "AND tp.company_id = $1" if company else ""
    sql = _FETCH_SQL.format(company_filter=company_filter)

    async with pool.acquire() as conn:
        rows = await (conn.fetch(sql, company) if company else conn.fetch(sql))

    candidates = len(rows)
    promotable = 0
    queued = 0
    skipped = 0
    sample: List[str] = []

    for r in rows:
        parsed: Dict[str, Any] = {
            "lessons_for_company": r["lessons_for_company"],
            "lessons_for_actor": r["lessons_for_actor"],
            "edge_detected": r["edge_detected"],
            "why_it_failed": r["why_it_failed"],
            "why_it_worked": r["why_it_worked"],
        }
        payload = _build_postmortem_broadcast(
            company=r["company_id"],
            position_id=int(r["position_id"]),
            symbol=r["instrument_symbol"] or "unknown",
            exchange=r["instrument_exchange"],
            direction=r["direction"] or "",
            outcome=r["outcome"] or "",
            signal_source=r["signal_source"] or "trader",
            trader_handle=r["trader_handle"] or "",
            parsed=parsed,
            correlation_id=r["correlation_id"],
        )
        if payload is None:
            skipped += 1
            continue
        promotable += 1
        if len(sample) < 5:
            sample.append(f"  - pos={r['position_id']} {payload['summary']}")
        if apply:
            await _broadcast_to_memu(pool, payload)
            queued += 1

    logger.info(
        "backfill: candidates=%d promotable=%d queued=%d skipped=%d",
        candidates, promotable, queued, skipped,
    )
    if sample:
        logger.info("backfill: sample payloads:\n%s", "\n".join(sample))
    if not apply:
        logger.info(
            "backfill: DRY-RUN — no outbox rows written. "
            "Re-run with --apply to queue %d lessons into MemU.",
            promotable,
        )
    return {
        "candidates": candidates,
        "promotable": promotable,
        "queued": queued,
        "skipped": skipped,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually queue outbox rows (default: dry-run).",
    )
    parser.add_argument(
        "--company", default=None,
        help="Optional company_id filter (e.g. jarvais). Default: all.",
    )
    args = parser.parse_args(argv)
    summary = asyncio.run(backfill(apply=args.apply, company=args.company))
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
