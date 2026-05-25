"""
One-shot: enforce per-trader pending-position uniqueness.

Operator rule (Round 13.6, 2026-05-24)
--------------------------------------
"only one long and one short can be opened per coin per trader, whether
the AI charthacker or a discord trader."

Translation: at any given moment, each (trader_profile_id, normalised
symbol, direction) tuple may have AT MOST ONE row in
``status='pending'``. When this script finds more than one, it keeps
the NEWEST and cancels the rest with audit-friendly status reasons.

Why "newest wins"
-----------------
Operator preference: "we can update with the newest ones (freshest) if
there's a new one that comes in." The freshest row reflects the most
up-to-date entry/SL/TP from that trader.

Replaces (deliberately)
-----------------------
* The Round-13.5 1%-tolerance / 4h-freshness logic, which collapsed
  cross-trader confluence (e.g. trader_1's BTC long + chart_hacker's
  BTC long would merge) and lost provenance.
* The silently-broken Phase-8 ``fetch_one`` positional-args dedup.

Safety
------
* ``--dry-run`` is the default. ``--apply`` is the only mutating mode.
* Symbol matching uses the same compact-form rule as
  ``trade_dedup._compact_symbol`` so legacy ``BTCUSDT`` rows cluster
  with new ``BTC/USDT:USDT`` rows.
* Rows in any status other than ``pending`` are NEVER touched.
* Each cancelled row gets ``status_reason='deduped:per_trader:<KEEPER_ID>'``
  and ``deduped_at=NOW()`` for full audit trail.

Usage
-----

::

    # Preview the proposed cancels.
    python3 -m shared.scripts.round13_dedup_pendings --dry-run

    # Apply the cancels.
    python3 -m shared.scripts.round13_dedup_pendings --apply

    # Limit to a single symbol or trader.
    python3 -m shared.scripts.round13_dedup_pendings --dry-run --symbol XAU/USDT:USDT
    python3 -m shared.scripts.round13_dedup_pendings --dry-run --trader-id 610

Author: 2026-05-24 (Round 13.6 — per-trader uniqueness backfill)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("round13_dedup_pendings")


@dataclass
class PendingRow:
    id: int
    trader_profile_id: int
    actor_id: Optional[str]
    signal_source: str
    symbol: str
    symbol_normalised: Optional[str]
    direction: str
    entry_price: float
    created_at: datetime
    news_item_id: int


def compact_symbol(symbol: Optional[str]) -> str:
    """Mirror of ``trade_dedup._compact_symbol`` — strips every legacy
    separator so ``BTCUSDT`` / ``BTC/USDT`` / ``BTC/USDT:USDT`` /
    ``BTC_USDT`` all collapse to the same uppercase compact form."""
    if not symbol:
        return ""
    return (
        symbol.replace("/", "")
        .replace("-", "")
        .replace("_", "")
        .replace(":", "")
        .strip()
        .upper()
    )


def find_per_trader_clusters(
    rows: List[PendingRow],
) -> List[Tuple[PendingRow, List[PendingRow]]]:
    """Group by (trader_profile_id, compact_symbol, direction).

    Inside each group, the newest row is the keeper; everyone else in the
    group is a duplicate. Returns only groups with at least one duplicate.
    """
    groups: Dict[Tuple[int, str, str], List[PendingRow]] = defaultdict(list)
    for r in rows:
        key = (r.trader_profile_id, compact_symbol(r.symbol), r.direction)
        groups[key].append(r)

    out: List[Tuple[PendingRow, List[PendingRow]]] = []
    for _key, bucket in groups.items():
        if len(bucket) < 2:
            continue
        bucket.sort(key=lambda r: r.created_at, reverse=True)
        keeper = bucket[0]
        dups = bucket[1:]
        out.append((keeper, dups))
    return out


def render_report(
    clusters: List[Tuple[PendingRow, List[PendingRow]]],
) -> str:
    if not clusters:
        return "No per-trader duplicate clusters found.\n"
    lines: List[str] = []
    total_dups = sum(len(d) for _, d in clusters)
    lines.append(
        f"Found {len(clusters)} (trader, symbol, direction) cluster(s) "
        f"with duplicates. Will cancel {total_dups} row(s) total."
    )
    lines.append("=" * 96)
    for keeper, dups in clusters:
        lines.append(
            f"\nKEEP   pid={keeper.id:>6}  trader={keeper.trader_profile_id:<4} "
            f"({keeper.actor_id or '?':<25}) {keeper.symbol:<22} {keeper.direction:<5} "
            f"entry={keeper.entry_price:>14.6f}  created={keeper.created_at:%Y-%m-%d %H:%M}"
        )
        for d in dups:
            delta_pct = (
                abs(d.entry_price - keeper.entry_price) / keeper.entry_price * 100.0
                if keeper.entry_price > 0 else 0.0
            )
            lines.append(
                f"  CXL  pid={d.id:>6}  trader={d.trader_profile_id:<4} "
                f"({d.actor_id or '?':<25}) {d.symbol:<22} {d.direction:<5} "
                f"entry={d.entry_price:>14.6f}  created={d.created_at:%Y-%m-%d %H:%M}  "
                f"Δ={delta_pct:.3f}%"
            )
    lines.append("\n" + "=" * 96)
    lines.append(
        f"DRY RUN — {total_dups} row(s) would be set to status='cancelled' "
        f"with status_reason='deduped:per_trader:<keeper_id>'."
    )
    return "\n".join(lines)


async def fetch_pending_rows(
    pool,
    *,
    only_symbol: Optional[str] = None,
    only_trader_id: Optional[int] = None,
) -> List[PendingRow]:
    """Pull every pending row, optionally filtered by symbol/trader."""
    base = """
        SELECT id,
               trader_profile_id,
               actor_id,
               COALESCE(signal_source, '?') AS signal_source,
               instrument_symbol,
               instrument_symbol_normalised,
               direction,
               entry_price,
               created_at,
               news_item_id
        FROM public.tracked_positions
        WHERE status = 'pending'
          AND entry_price IS NOT NULL
          AND entry_price > 0
    """
    params: List = []
    if only_symbol:
        params.append(only_symbol)
        base += f"""
            AND (instrument_symbol = ${len(params)}
                 OR instrument_symbol_normalised = ${len(params)}
                 OR UPPER(instrument_symbol) = UPPER(${len(params)})
                 OR UPPER(instrument_symbol_normalised) = UPPER(${len(params)}))
        """
    if only_trader_id is not None:
        params.append(int(only_trader_id))
        base += f" AND trader_profile_id = ${len(params)}\n"
    base += " ORDER BY created_at DESC"

    raw = await pool.fetch_all(base, tuple(params) if params else None)
    return [
        PendingRow(
            id=int(r["id"]),
            trader_profile_id=int(r["trader_profile_id"]),
            actor_id=r.get("actor_id"),
            signal_source=r.get("signal_source") or "?",
            symbol=r["instrument_symbol"],
            symbol_normalised=r.get("instrument_symbol_normalised"),
            direction=r["direction"],
            entry_price=float(r["entry_price"]),
            created_at=r["created_at"],
            news_item_id=int(r["news_item_id"]),
        )
        for r in raw
    ]


async def cancel_duplicates(
    pool, clusters: List[Tuple[PendingRow, List[PendingRow]]]
) -> int:
    """Cancel each duplicate; returns count of UPDATE statements that succeeded."""
    n = 0
    for keeper, dups in clusters:
        for d in dups:
            reason = f"deduped:per_trader:{keeper.id}"[:100]
            try:
                rc = await pool.execute(
                    """
                    UPDATE public.tracked_positions
                    SET status        = 'cancelled',
                        status_reason = $1,
                        deduped_at    = NOW(),
                        updated_at    = NOW()
                    WHERE id = $2 AND status = 'pending'
                    """,
                    (reason, d.id),
                )
                logger.info(
                    "cancelled pid=%s -> keeper=%s (trader=%s, rc=%s)",
                    d.id, keeper.id, keeper.trader_profile_id, rc,
                )
                n += int(rc) if isinstance(rc, int) else 1
            except Exception as exc:
                logger.exception(
                    "FAILED to cancel pid=%s (keeper=%s): %s",
                    d.id, keeper.id, exc,
                )
    return n


async def _amain(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from shared.utils.db import get_shared_pool
    pool = await get_shared_pool()

    rows = await fetch_pending_rows(
        pool,
        only_symbol=args.symbol,
        only_trader_id=args.trader_id,
    )
    print(
        f"Fetched {len(rows)} pending row(s)"
        f"{' for symbol=' + args.symbol if args.symbol else ''}"
        f"{' for trader=' + str(args.trader_id) if args.trader_id is not None else ''}."
    )

    clusters = find_per_trader_clusters(rows)
    print(render_report(clusters))

    if args.apply:
        if not clusters:
            print("Nothing to apply.")
            return 0
        total = sum(len(d) for _, d in clusters)
        print(f"\nAPPLY: cancelling {total} duplicate(s)...")
        n = await cancel_duplicates(pool, clusters)
        print(f"Cancelled {n} row(s).")
    else:
        print("\n(no changes made — re-run with --apply to commit)")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Print the proposed cancel list without modifying the DB (default).",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually run the cancels. Mutually exclusive with the default dry-run posture.",
    )
    p.add_argument(
        "--symbol",
        type=str,
        default=None,
        help="Limit to a single instrument (raw or canonical form).",
    )
    p.add_argument(
        "--trader-id",
        type=int,
        default=None,
        help="Limit to a single trader_profile_id.",
    )
    args = p.parse_args()
    rc = asyncio.run(_amain(args))
    sys.exit(rc)


if __name__ == "__main__":
    main()
