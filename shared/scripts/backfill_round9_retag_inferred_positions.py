"""Round 9 (2026-05-24) — Backfill: retag/cancel falsely trader-attributed positions.

Why this exists
---------------
Before Round 9 the chart_analysis prompt instructed the LLM to "construct a
trade" from any chart that had support/resistance zones — even when the
trader had only posted level commentary like "btc must get above here".
The constructed trade was placed in `trader_trades`, which booked a
`tracked_positions` row with `signal_source='trader'` falsely attributed
to the original Discord poster. The trader's accuracy stats (and the
dashboard's risk view) then double-counted the AI's inferences.

This one-shot script walks every open / pending `tracked_positions` row
with `signal_source='trader'`, applies the same `_is_explicit_trader_setup`
gate the live pipeline now uses, and:

  * If the post fails the gate (commentary, chart-only, no setup keywords)
    AND a chart_hacker twin already exists for the same
    signal_interpretation_id → CANCEL the trader-attributed row (status
    becomes 'cancelled', exit_reason='round9_no_explicit_setup'). The
    chart_hacker twin remains and represents the AI's independent opinion.

  * If the post fails the gate AND there is no chart_hacker twin →
    RETAG the row in place: signal_source='chart_hacker',
    actor_type='agent', actor_id=<company>_chart_hacker,
    trader_profile_id=<chart_hacker profile id>. This preserves the
    pipeline's downstream work (P&L tracking, postmortems) while moving
    attribution to the AI agent where it belongs.

  * If the post passes the gate → leave it alone (this was a real trader
    call).

Defaults to dry-run; pass --apply to actually mutate the DB.

Usage
-----
    python3 -m shared.scripts.backfill_round9_retag_inferred_positions --dry-run
    python3 -m shared.scripts.backfill_round9_retag_inferred_positions --apply
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ensure shared imports resolve when run as a script.
_HERE = Path(__file__).resolve().parent
_SHARED = _HERE.parent
_ROOT = _SHARED.parent
for p in (_ROOT, _SHARED):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from shared.intelligence.interpretation_service import (  # noqa: E402
    _is_explicit_trader_setup,
    _get_chart_hacker_profile_id,
)
from shared.utils.config import load_env  # noqa: E402
from shared.utils.db import DatabasePool, close_all_pools, get_shared_pool  # noqa: E402

# We use a synthetic prompt-version string that triggers the gate. Live
# rows in the DB right now don't carry the prompt version on the
# tracked_positions row itself, so we ignore "legacy" handling and treat
# every row as "must pass the gate". This is intentional for the backfill —
# the whole point is to clean up rows that were created under the OLD
# prompt that hallucinated trades.
_FAKE_PROMPT_VERSION = "2026.05.24-trader-explicit-only-v1-backfill"


async def _fetch_candidates(pool: DatabasePool) -> List[Dict[str, Any]]:
    """Return every open/pending trader-attributed position with its news text."""
    sql = """
        SELECT
            tp.id                        AS pid,
            tp.signal_interpretation_id  AS sig_id,
            tp.news_item_id              AS news_id,
            tp.status                    AS status,
            tp.direction                 AS direction,
            tp.actor_id                  AS actor_id,
            tp.trader_profile_id         AS trader_profile_id,
            tp.company_id                AS company_id,
            tp.entry_price               AS entry_price,
            tp.stop_loss                 AS stop_loss,
            tp.signal_source             AS signal_source,
            ni.headline                  AS headline,
            ni.content                   AS content,
            ch.id IS NOT NULL            AS has_ch_twin,
            ch.id                        AS ch_pid
          FROM public.tracked_positions tp
     LEFT JOIN public.news_items ni
            ON ni.id = tp.news_item_id
     LEFT JOIN public.tracked_positions ch
            ON ch.signal_interpretation_id = tp.signal_interpretation_id
           AND ch.signal_source = 'chart_hacker'
         WHERE tp.signal_source = 'trader'
           AND tp.status IN ('pending', 'open')
         ORDER BY tp.id ASC
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql)
    return [dict(r) for r in rows]


def _gate(row: Dict[str, Any]) -> Tuple[bool, str]:
    """Apply the same trader-setup gate the live pipeline uses."""
    text = f"{row.get('headline') or ''}\n{row.get('content') or ''}"
    return _is_explicit_trader_setup({}, text, _FAKE_PROMPT_VERSION)


async def _cancel_position(pool: DatabasePool, pid: int) -> None:
    """Mark a falsely trader-attributed position as cancelled (twin exists)."""
    await pool.execute(
        """
        UPDATE public.tracked_positions
           SET status        = 'cancelled',
               status_reason = 'round9_no_explicit_setup',
               exit_reason   = 'round9_no_explicit_setup',
               closed_at     = NOW(),
               updated_at    = NOW()
         WHERE id = $1
        """,
        (pid,),
    )


async def _retag_to_chart_hacker(
    pool: DatabasePool,
    pid: int,
    company_id: Optional[str],
    chart_hacker_profile_id: int,
) -> None:
    """Move a position from signal_source='trader' to 'chart_hacker' in-place."""
    company = (company_id or "jarvais").strip().lower() or "jarvais"
    await pool.execute(
        """
        UPDATE public.tracked_positions
           SET signal_source     = 'chart_hacker',
               actor_type        = 'agent',
               actor_id          = $2,
               trader_profile_id = $3,
               updated_at        = NOW()
         WHERE id = $1
        """,
        (pid, f"{company}_chart_hacker", chart_hacker_profile_id),
    )


async def main(*, apply: bool, limit: Optional[int]) -> int:
    load_env()
    pool = await get_shared_pool()
    try:
        rows = await _fetch_candidates(pool)
        if limit:
            rows = rows[:limit]

        ch_profile_id = await _get_chart_hacker_profile_id(pool)
        if ch_profile_id is None:
            print(
                "ERROR: chart_hacker trader_profiles row not found. Cannot retag rows. "
                "Run the Phase J seed migration first."
            )
            return 2

        kept: List[Dict[str, Any]] = []
        cancel_plan: List[Dict[str, Any]] = []
        retag_plan: List[Dict[str, Any]] = []
        evidence_counter: Counter = Counter()

        for row in rows:
            ok, evidence = _gate(row)
            evidence_counter[evidence] += 1
            if ok:
                kept.append(row)
                continue
            # Failed the gate → action depends on whether a chart_hacker twin exists.
            if row.get("has_ch_twin"):
                cancel_plan.append(row)
            else:
                retag_plan.append(row)

        # Print scope summary.
        print("=" * 70)
        print("Round 9 backfill — scope")
        print("=" * 70)
        print(f"Total candidates (signal_source='trader', open|pending): {len(rows)}")
        print(f"  PASS gate (real trader calls, untouched):    {len(kept)}")
        print(f"  FAIL gate, has chart_hacker twin → CANCEL:   {len(cancel_plan)}")
        print(f"  FAIL gate, no chart_hacker twin   → RETAG:   {len(retag_plan)}")
        print()
        print("Gate evidence breakdown:")
        for ev, cnt in evidence_counter.most_common():
            print(f"  {ev:>26s}  {cnt:>5d}")
        print()

        # Show first 10 of each plan for sanity.
        if cancel_plan:
            print("Sample CANCEL targets (first 10):")
            for r in cancel_plan[:10]:
                txt = (r.get("content") or r.get("headline") or "")[:80]
                print(f"  pid={r['pid']:>6d} sig={r['sig_id']} dir={r['direction']:>5s} ch_twin={r['ch_pid']} text={txt!r}")
            print()
        if retag_plan:
            print("Sample RETAG targets (first 10):")
            for r in retag_plan[:10]:
                txt = (r.get("content") or r.get("headline") or "")[:80]
                print(f"  pid={r['pid']:>6d} sig={r['sig_id']} dir={r['direction']:>5s} no twin → become chart_hacker text={txt!r}")
            print()

        if not apply:
            print("Dry-run only. Re-run with --apply to mutate the DB.")
            return 0

        # Apply.
        print("Applying changes…")
        for r in cancel_plan:
            await _cancel_position(pool, int(r["pid"]))
        for r in retag_plan:
            await _retag_to_chart_hacker(
                pool,
                int(r["pid"]),
                r.get("company_id"),
                ch_profile_id,
            )
        print(
            f"Done. Cancelled {len(cancel_plan)} duplicate trader rows; "
            f"retagged {len(retag_plan)} orphan trader rows to chart_hacker."
        )
        return 0
    finally:
        await close_all_pools()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Mutate the DB. Default is dry-run.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicit dry-run flag (default).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of candidates inspected (for testing).",
    )
    args = parser.parse_args()
    if args.apply and args.dry_run:
        print("Pass either --apply OR --dry-run, not both.")
        sys.exit(2)
    sys.exit(asyncio.run(main(apply=args.apply, limit=args.limit)))
