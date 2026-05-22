"""
Module: project_unified_to_instruments
Purpose: Project active rows from ``public.unified_instruments`` into
         ``public.instruments`` so the dashboard snapshot's pending-position
         price lookup can find them. Idempotent — safe to run repeatedly.

Background (2026-05-22):
    Two catalog tables coexisted but were never connected:

      * ``public.instruments`` (used by the dashboard snapshot and the
        candle daemon's instrument enumerator). Had only ~50 hand-seeded
        spot pairs.
      * ``public.unified_instruments`` (written by
        ``shared/market_data/instrument_sync.py``). Has 5000+ rows from
        Bybit, BloFin, Bitget, and Capital.com — both spot and perp.

    The signals dashboard's pending-position branch in ``snapshot.py`` joins
    ``candles`` via ``instruments`` to fetch the live price. When the
    instrument row is missing, the join returns nothing and the front-end
    silently renders ``+0.00%``. This job bridges the two tables.

Design:

  * **Catalog presence vs. collection activity.** Every unified row is
    projected (so the catalog is comprehensive — useful for the MCP
    ``instruments.refresh`` tool and for diagnostics), but only ONE
    row per symbol is marked ``is_active=TRUE`` — the row whose exchange
    is the preferred venue for that symbol AND whose symbol has appeared
    in the trader feed (``tracked_positions`` or ``signal_interpretations``)
    in the last 90 days. Existing rows that were already active are kept
    active (we never deactivate the original 50).
  * **Symbol form.** We project the canonical slash form ``{BASE}/{QUOTE}``
    derived from the unified row's ``base_currency`` / ``quote_currency``
    columns. We deliberately ignore ``unified_instruments.canonical_symbol``
    because the normaliser had a bug for CCXT perp form (Phase 6 fixes
    that — projector doesn't depend on it).
  * **Exchange preference order.** The trader-signal pipeline currently
    stamps ``instrument_exchange='bybit'`` regardless of the venue the
    trader actually called — so Bybit comes first. BloFin is perps-only and
    has the best low-cap alt coverage, so it's the next preference. The
    remaining tie-break is alphabetical-ish: bitget, binance, okx,
    capital.com.
  * **Activity rule.** A (symbol, exchange) row is marked
    ``is_active=TRUE`` iff:
        (a) it was already active before this run (we never deactivate), OR
        (b) the symbol appears in the 90-day feed AND this is the most
            preferred exchange that has the symbol.
    All other rows are ``is_active=FALSE`` — catalog-only.

Location: /opt/tickles/shared/jobs/project_unified_to_instruments.py
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Preferred exchange order for the "activate one row per feed-symbol" pass.
# Index 0 wins ties.
_EXCHANGE_PREFERENCE: Tuple[str, ...] = (
    "bybit",
    "blofin",
    "bitget",
    "binance",
    "okx",
    "capital.com",
    "capital",
)

# Map ``unified_instruments.asset_type`` -> ``asset_class_t`` enum.
# asset_class_t has: 'crypto', 'cfd', 'stock', 'forex', 'commodity', 'index'
_ASSET_TYPE_MAP: Dict[str, str] = {
    "crypto": "crypto",
    "forex": "forex",
    "commodity": "commodity",
    "index": "index",
    "stock": "stock",
}
_DEFAULT_ASSET_CLASS = "crypto"

# Window for "the trader feed has used this symbol recently".
_FEED_LOOKBACK_DAYS = 90


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _slash_symbol(base: Optional[str], quote: Optional[str]) -> Optional[str]:
    """Return canonical slash form ``BASE/QUOTE`` or ``None`` if either is empty.

    Args:
        base: Base currency (e.g. ``BTC``).
        quote: Quote currency (e.g. ``USDT``).

    Returns:
        ``'BTC/USDT'`` or None.
    """
    if not base or not quote:
        return None
    b = base.strip().upper()
    q = quote.strip().upper()
    if not b or not q:
        return None
    return f"{b}/{q}"


def _exchange_rank(exchange: str) -> int:
    """Lower number = more preferred. Unknown venues sort last."""
    try:
        return _EXCHANGE_PREFERENCE.index(exchange)
    except ValueError:
        return len(_EXCHANGE_PREFERENCE) + 1


def _map_asset_class(unified_asset_type: Optional[str]) -> str:
    """Map unified.asset_type -> asset_class_t enum value."""
    if not unified_asset_type:
        return _DEFAULT_ASSET_CLASS
    return _ASSET_TYPE_MAP.get(unified_asset_type.lower(), _DEFAULT_ASSET_CLASS)


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------
async def _fetch_unified_rows(conn) -> List[Dict]:
    """Pull every active unified_instruments row.

    Returns:
        List of dicts with keys: exchange, exchange_symbol, base_currency,
        quote_currency, asset_type, is_active.
    """
    rows = await conn.fetch(
        """
        SELECT exchange, exchange_symbol, base_currency, quote_currency,
               asset_type, is_active
        FROM public.unified_instruments
        WHERE is_active = TRUE
        """
    )
    return [dict(r) for r in rows]


async def _fetch_feed_symbols(conn) -> Set[str]:
    """Symbols that have appeared in the trader feed within the lookback.

    Returns:
        Set of canonical slash-form symbols (UPPERCASE).
    """
    rows = await conn.fetch(
        f"""
        SELECT DISTINCT UPPER(instrument_symbol_normalised) AS sym
        FROM public.tracked_positions
        WHERE created_at > now() - interval '{_FEED_LOOKBACK_DAYS} days'
          AND instrument_symbol_normalised IS NOT NULL
        UNION
        SELECT DISTINCT UPPER(instrument_symbol) AS sym
        FROM public.signal_interpretations
        WHERE created_at > now() - interval '{_FEED_LOOKBACK_DAYS} days'
          AND instrument_symbol IS NOT NULL
        """
    )
    return {r["sym"] for r in rows if r["sym"]}


async def _fetch_existing_instruments(conn) -> Dict[Tuple[str, str], bool]:
    """Return mapping of (symbol, exchange) -> is_active for everything
    already in public.instruments.
    """
    rows = await conn.fetch(
        "SELECT symbol, exchange, is_active FROM public.instruments"
    )
    return {(r["symbol"], r["exchange"]): r["is_active"] for r in rows}


# ---------------------------------------------------------------------------
# Core projection
# ---------------------------------------------------------------------------
def _decide_active_per_symbol(
    projected: List[Dict],
    feed_symbols: Set[str],
) -> Set[Tuple[str, str]]:
    """Pick the (symbol, exchange) tuples that should become active.

    Rule: for each symbol that appears in the feed AND has at least one
    candidate row in ``projected``, pick the most preferred exchange.
    Returns the set of selected ``(symbol, exchange)`` keys.
    """
    # Group candidate exchanges by symbol.
    by_symbol: Dict[str, List[Dict]] = {}
    for row in projected:
        by_symbol.setdefault(row["symbol"], []).append(row)

    activate: Set[Tuple[str, str]] = set()
    for sym, rows in by_symbol.items():
        if sym.upper() not in feed_symbols:
            continue
        rows_sorted = sorted(rows, key=lambda r: _exchange_rank(r["exchange"]))
        chosen = rows_sorted[0]
        activate.add((chosen["symbol"], chosen["exchange"]))
    return activate


async def _upsert_one(
    conn,
    *,
    symbol: str,
    exchange: str,
    asset_class: str,
    base_currency: Optional[str],
    quote_currency: Optional[str],
    is_active: bool,
    dry_run: bool,
) -> str:
    """Insert or update one (symbol, exchange) row.

    Returns:
        'inserted', 'updated', or 'noop' (dry-run only).
    """
    if dry_run:
        return "noop"
    # NOTE: ``last_synced_at`` is bumped on every run so we can audit
    # freshness via ``SELECT max(last_synced_at) FROM instruments``.
    # ``is_active`` uses GREATEST-style OR so we never deactivate a row
    # that was already active (per design rule above).
    result = await conn.execute(
        """
        INSERT INTO public.instruments
            (symbol, exchange, asset_class, base_currency, quote_currency,
             is_active, last_synced_at, created_at, updated_at)
        VALUES ($1, $2, $3::asset_class_t, $4, $5, $6, now(), now(), now())
        ON CONFLICT (symbol, exchange) DO UPDATE
        SET asset_class    = EXCLUDED.asset_class,
            base_currency  = COALESCE(EXCLUDED.base_currency,
                                      public.instruments.base_currency),
            quote_currency = COALESCE(EXCLUDED.quote_currency,
                                      public.instruments.quote_currency),
            is_active      = public.instruments.is_active OR EXCLUDED.is_active,
            last_synced_at = now(),
            updated_at     = now()
        """,
        symbol, exchange, asset_class, base_currency, quote_currency, is_active,
    )
    # asyncpg returns e.g. 'INSERT 0 1' or 'UPDATE 1' — sniff for which.
    if isinstance(result, str) and result.startswith("INSERT"):
        return "inserted"
    return "updated"


async def project_unified_to_instruments(
    *, dry_run: bool = False,
) -> Dict[str, int]:
    """Run the projection.

    Steps:
        1. Snapshot existing ``public.instruments`` and the 90-day feed.
        2. Build the projection set from ``unified_instruments`` (one row per
           ``(symbol_slash, exchange)``).
        3. Compute which rows should be active (one per feed-symbol).
        4. Upsert. Existing active rows stay active.

    Args:
        dry_run: If True, log what would happen but do not write.

    Returns:
        Dict with counts: unified_rows, projected, activated, inserted,
        updated, skipped.
    """
    from shared.utils.db import get_shared_pool

    pool = await get_shared_pool()
    async with pool.acquire() as conn:
        unified = await _fetch_unified_rows(conn)
        feed_symbols = await _fetch_feed_symbols(conn)
        existing = await _fetch_existing_instruments(conn)

        logger.info(
            "Projection inputs: %d unified_instruments rows, %d feed symbols, "
            "%d existing instruments rows",
            len(unified), len(feed_symbols), len(existing),
        )

        # Step 2: build the projection set. De-duplicate within an exchange
        # because the unified table contains both spot and perp forms for the
        # same base/quote (e.g. AAVE/USDT and AAVE/USDT:USDT) — we collapse
        # them to the slash form intentionally.
        seen: Set[Tuple[str, str]] = set()
        projected: List[Dict] = []
        skipped_no_pair = 0
        for u in unified:
            sym = _slash_symbol(u.get("base_currency"), u.get("quote_currency"))
            if not sym:
                skipped_no_pair += 1
                continue
            key = (sym, u["exchange"])
            if key in seen:
                continue
            seen.add(key)
            projected.append({
                "symbol": sym,
                "exchange": u["exchange"],
                "asset_class": _map_asset_class(u.get("asset_type")),
                "base_currency": (u.get("base_currency") or "").upper() or None,
                "quote_currency": (u.get("quote_currency") or "").upper() or None,
            })

        # Step 3: decide active subset.
        to_activate = _decide_active_per_symbol(projected, feed_symbols)
        logger.info(
            "Activation plan: %d (symbol, exchange) rows will be flagged active",
            len(to_activate),
        )

        # Step 4: upsert. Track counts.
        counts = {
            "unified_rows": len(unified),
            "projected": len(projected),
            "activated": len(to_activate),
            "inserted": 0,
            "updated": 0,
            "skipped_no_pair": skipped_no_pair,
        }

        for row in projected:
            key = (row["symbol"], row["exchange"])
            existing_active = existing.get(key, False)
            # Never deactivate something that was previously active.
            want_active = existing_active or (key in to_activate)
            try:
                action = await _upsert_one(
                    conn,
                    symbol=row["symbol"],
                    exchange=row["exchange"],
                    asset_class=row["asset_class"],
                    base_currency=row["base_currency"],
                    quote_currency=row["quote_currency"],
                    is_active=want_active,
                    dry_run=dry_run,
                )
                if action == "inserted":
                    counts["inserted"] += 1
                elif action == "updated":
                    counts["updated"] += 1
            except Exception as exc:
                logger.warning(
                    "Failed to upsert (%s, %s): %s",
                    row["symbol"], row["exchange"], exc,
                )

        logger.info("Projection done: %s", counts)
        return counts


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------
def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Project public.unified_instruments rows into public.instruments "
            "so the dashboard snapshot query can resolve them."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compute the plan and log it, but write nothing.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Verbose logging (DEBUG level).",
    )
    return parser.parse_args(argv)


async def _amain(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    counts = await project_unified_to_instruments(dry_run=args.dry_run)
    print(counts)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    sys.exit(main())
