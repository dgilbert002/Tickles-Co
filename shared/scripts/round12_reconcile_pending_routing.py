"""
Module: round12_reconcile_pending_routing
Purpose: One-shot script that re-routes existing pending tracked_positions
         through the Round 12 exchange_router and cancels (or rewrites)
         rows whose symbols cannot be routed.

         Pre-Round-12 inserts defaulted ``instrument_exchange='bybit'`` and
         never populated ``epic_code``. The router-aware INSERT path only
         applies to NEW signals; this script back-fills the existing
         pending queue so PositionMonitor stops hammering CCXT for symbols
         that don't exist on Bybit (GOLD, US100, USDT.D, HUSDT.P, etc.).

Location: /opt/tickles/shared/scripts/round12_reconcile_pending_routing.py
Round:    Round 12 (2026-05-24)

Behaviour
---------
1. Selects every ``tracked_positions`` row with ``status='pending'`` (and
   optionally ``status='open'`` if --include-open is passed).
2. For each row, runs ``resolve_market(instrument_symbol)``.
3. If the router returns:
   * ``supported=True`` and the routed exchange differs from the stored
     ``instrument_exchange`` → UPDATE instrument_exchange + epic_code.
     The row stays pending (or open). PositionMonitor will use the new
     routing on its next cycle.
   * ``supported=False`` → UPDATE status='cancelled' + status_reason
     ``unsupported:<reason>:<utc>``. This stops the monitor from polling.
   * ``supported=True`` AND already correctly routed → no-op (counted).

Flags
-----
* ``--apply`` — actually write. Default is dry-run, prints what WOULD
  happen without touching the DB. Always start dry.
* ``--include-open`` — also re-route currently OPEN positions. Default
  is pending-only because OPEN rows are mid-trade and we don't want to
  surprise the operator. CFD opens with no epic_code WILL be routed
  through with --include-open if you trust the routing.
* ``--limit N`` — cap row count for incremental rollouts.

Usage
-----
::

    # Dry-run, show what would happen for all pending rows
    python3 -m shared.scripts.round12_reconcile_pending_routing

    # Actually apply the change to pending rows only
    python3 -m shared.scripts.round12_reconcile_pending_routing --apply

    # Include open rows (more aggressive)
    python3 -m shared.scripts.round12_reconcile_pending_routing --apply --include-open

The script is idempotent — running it twice produces the same outcome
modulo any new pending rows that arrived between runs.

The script does NOT touch ``positions_current`` (broker fills); only
``tracked_positions`` (signal-derived shadow positions).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import List

from shared.utils.db import get_shared_pool
from shared.utils.exchange_router import (
    UNSUPPORTED_REASONS,
    clear_cache,
    resolve_market,
    unsupported_status_reason,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("round12.reconcile")


async def fetch_target_rows(include_open: bool, limit: int):
    """Pull rows that need re-routing from tracked_positions."""
    pool = await get_shared_pool()
    statuses = ["pending"]
    if include_open:
        statuses.append("open")
        statuses.append("partial_exit")
    rows = await pool.fetch_all(
        """
        SELECT id,
               instrument_symbol,
               instrument_exchange,
               instrument_symbol_normalised,
               epic_code,
               status,
               status_reason,
               direction,
               news_item_id,
               trader_profile_id,
               created_at
        FROM public.tracked_positions
        WHERE status = ANY($1::text[])
        ORDER BY id ASC
        LIMIT $2
        """,
        (statuses, limit),
    )
    return rows


async def reconcile(*, apply: bool, include_open: bool, limit: int) -> int:
    """Main entry. Returns process exit code."""
    rows = await fetch_target_rows(include_open=include_open, limit=limit)
    logger.info(
        "Round 12 reconcile: scanning %d rows (apply=%s include_open=%s)",
        len(rows), apply, include_open,
    )

    pool = await get_shared_pool()
    counter: Counter[str] = Counter()
    actions: List[str] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for r in rows:
        clear_cache()
        sym = r["instrument_symbol"]
        cur_ex = r["instrument_exchange"]
        cur_epic = r.get("epic_code")
        routed = await resolve_market(sym, cur_ex)

        if routed.supported:
            new_ex = routed.exchange
            new_epic = routed.epic_code
            if new_ex == cur_ex and new_epic == cur_epic:
                counter["no_change"] += 1
                continue
            counter["rerouted"] += 1
            actions.append(
                f"REROUTE id={r['id']} sym={sym!r} {cur_ex} -> {new_ex} "
                f"(epic_code: {cur_epic} -> {new_epic})"
            )
            if apply:
                await pool.execute(
                    """
                    UPDATE public.tracked_positions
                    SET instrument_exchange = $1,
                        epic_code = $2,
                        instrument_symbol = $3,
                        instrument_symbol_normalised = $4,
                        updated_at = NOW()
                    WHERE id = $5
                      AND status = ANY($6::text[])
                    """,
                    (
                        new_ex,
                        new_epic,
                        routed.canonical_symbol or sym,
                        routed.canonical_symbol or sym,
                        r["id"],
                        ["pending", "open", "partial_exit"],
                    ),
                )
        else:
            reason_str = unsupported_status_reason(routed, now_iso)
            counter[f"cancelled:{routed.unsupported_reason}"] += 1
            actions.append(
                f"CANCEL  id={r['id']} sym={sym!r} status={r['status']} "
                f"-> cancelled (reason={reason_str[:80]})"
            )
            if apply:
                await pool.execute(
                    """
                    UPDATE public.tracked_positions
                    SET status = 'cancelled',
                        status_reason = $1,
                        updated_at = NOW(),
                        closed_at = COALESCE(closed_at, NOW())
                    WHERE id = $2
                      AND status = ANY($3::text[])
                    """,
                    (reason_str[:512], r["id"], ["pending"]),
                )
                # Note: we deliberately scope the cancel UPDATE to status='pending'
                # only. An open position with an unsupported symbol is a real
                # exposure — we don't auto-close those, even on --include-open.
                # The router rerouting still applies; only the final 'cancel'
                # statement is gated.

    logger.info("Round 12 reconcile summary: %s", dict(counter))
    for line in actions:
        print(line)
    if not apply:
        logger.info("Dry-run only — re-run with --apply to persist.")
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true",
                   help="Actually write changes. Default is dry-run.")
    p.add_argument("--include-open", action="store_true",
                   help="Also re-route currently-open positions (rerouting "
                        "only — open rows are NOT auto-cancelled).")
    p.add_argument("--limit", type=int, default=10000,
                   help="Max rows to scan in this run (default 10000).")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    rc = asyncio.run(reconcile(
        apply=args.apply,
        include_open=args.include_open,
        limit=args.limit,
    ))
    sys.exit(rc)


if __name__ == "__main__":
    main()
