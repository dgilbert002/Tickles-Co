"""
Module: round13_reconcile_routing
Purpose: One-shot script that re-routes existing tracked_positions
         through the Round 13 crypto-first ``exchange_router`` and
         updates rows whose symbol/exchange combination changes under
         the new policy.

         Round 13 (2026-05-24) flipped three things:
           1. BTC/USD, ETH/USD, GOLD, SPX, NQ etc. now route to the
              crypto-tokenised perp instead of Capital.com.
           2. The persisted ``instrument_symbol`` is the perp swap form
              (``BTC/USDT:USDT``) instead of the spot canonical
              (``BTC/USDT``).
           3. Capital-only matches are parked
              (``unsupported:capital_only_parked``) — operator's choice
              to push everything through crypto venues for now.

         The router-aware INSERT path only applies to NEW signals; this
         script back-fills the existing pending + open queue so:
           * The 72 Capital.com pending rows from Round 12 either move
             to a crypto exchange (BTC/USD → bybit) or get marked
             parked.
           * The 100+ bybit pending rows still stored as spot canonical
             (``BTC/USDT``, ``ETH/USDT``) get rewritten to perp form
             (``BTC/USDT:USDT``).
           * The handful of ``BTCUSDT`` (no slash) rows from pre-Round-12
             get cosmetically normalised.

Location: /opt/tickles/shared/scripts/round13_reconcile_routing.py
Round:    Round 13 (2026-05-24)

Behaviour
---------
1. Select tracked_positions with ``status='pending'`` (and optionally
   ``status='open'`` and/or ``status='partial_exit'``).
2. For each row run ``resolve_market(instrument_symbol)``.
3. Possible outcomes:

   ============================  ==========================  =================
   Resolver outcome              Existing row state          Action
   ============================  ==========================  =================
   supported, route differs      ANY                         UPDATE exchange,
                                                             epic_code,
                                                             instrument_symbol,
                                                             instrument_symbol_normalised
   supported, route same,        ANY                         UPDATE instrument_symbol
   canonical_symbol differs                                  to perp form
   supported, identical          ANY                         no-op
   unsupported (capital_only)    pending                     CANCEL with reason
   unsupported (capital_only)    open / partial_exit         skip with WARN
   unsupported (other reason)    pending                     CANCEL with reason
   unsupported (other reason)    open / partial_exit         skip with WARN
   ============================  ==========================  =================

   Open positions are NEVER auto-cancelled by the reconciler. A genuine
   unsupported open row is a real exposure — manual handling only.

Flags
-----
* ``--apply``              actually write. Default is dry-run.
* ``--include-open``       also process status='open' / 'partial_exit'.
                            Default is pending-only.
* ``--limit N``            cap row count for incremental rollouts.

Usage
-----
::

    # Dry-run all pending rows
    python3 -m shared.scripts.round13_reconcile_routing

    # Apply to pending only
    python3 -m shared.scripts.round13_reconcile_routing --apply

    # Apply to pending + open (rewrites instrument_symbol to perp form
    # for currently-tracked rows so dashboards see the perp form)
    python3 -m shared.scripts.round13_reconcile_routing --apply --include-open

The script is idempotent — running twice produces the same outcome
modulo any new rows that arrived between runs.

This script does NOT touch ``positions_current`` (broker fills); only
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
logger = logging.getLogger("round13.reconcile")


async def fetch_target_rows(include_open: bool, limit: int):
    """Pull rows that need re-routing from tracked_positions.

    Args:
        include_open: When True, also pulls ``status='open'`` and
            ``'partial_exit'`` rows so their ``instrument_symbol`` can be
            rewritten to perp form. These rows are NEVER cancelled even
            when the resolver returns unsupported.
        limit: Maximum row count.

    Returns:
        List of asyncpg Records.
    """
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
        "Round 13 reconcile: scanning %d rows (apply=%s include_open=%s)",
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
        cur_epic = r["epic_code"] if "epic_code" in r else None
        cur_status = r["status"]
        routed = await resolve_market(sym, cur_ex)

        if routed.supported:
            new_ex = routed.exchange
            new_epic = routed.epic_code
            new_sym = routed.canonical_symbol or sym
            unchanged = (
                new_ex == cur_ex
                and new_epic == cur_epic
                and new_sym == sym
            )
            if unchanged:
                counter["no_change"] += 1
                continue

            if new_ex == cur_ex and new_epic == cur_epic and new_sym != sym:
                counter["repersist_perp_form"] += 1
                actions.append(
                    f"PERP-FORM id={r['id']} sym={sym!r} -> {new_sym!r}"
                )
            else:
                counter["rerouted"] += 1
                actions.append(
                    f"REROUTE   id={r['id']} sym={sym!r} {cur_ex} -> {new_ex} "
                    f"sym={new_sym!r} (epic_code: {cur_epic} -> {new_epic})"
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
                        new_sym,
                        new_sym,
                        r["id"],
                        ["pending", "open", "partial_exit"],
                    ),
                )
        else:
            # Unsupported under Round 13 — capital_only_parked or
            # not_in_unified_instruments etc.
            if cur_status in ("open", "partial_exit"):
                # Real exposure: never auto-cancel. Skip with WARN.
                counter[f"open_unsupported_skipped:{routed.unsupported_reason}"] += 1
                actions.append(
                    f"SKIP-OPEN id={r['id']} sym={sym!r} status={cur_status} "
                    f"unsupported={routed.unsupported_reason} "
                    f"(operator must close manually if intended)"
                )
                continue

            reason_str = unsupported_status_reason(routed, now_iso)
            counter[f"cancelled:{routed.unsupported_reason}"] += 1
            actions.append(
                f"CANCEL    id={r['id']} sym={sym!r} status={cur_status} "
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
                      AND status = 'pending'
                    """,
                    (reason_str[:512], r["id"]),
                )

    logger.info("Round 13 reconcile summary: %s", dict(counter))
    for line in actions:
        print(line)
    if not apply:
        logger.info("Dry-run only — re-run with --apply to persist.")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument(
        "--apply", action="store_true",
        help="Actually write changes. Default is dry-run.",
    )
    p.add_argument(
        "--include-open", action="store_true",
        help="Also process status='open' and 'partial_exit' rows "
             "(rewrites instrument_symbol to perp form; never cancels).",
    )
    p.add_argument(
        "--limit", type=int, default=10_000,
        help="Maximum rows to process. Default 10000.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    return asyncio.run(reconcile(
        apply=args.apply,
        include_open=args.include_open,
        limit=args.limit,
    ))


if __name__ == "__main__":
    sys.exit(main())
