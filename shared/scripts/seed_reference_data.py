"""
Reference Data Seeder — Phase 1
================================

Seeds/upserts:
  * tickles_shared.instruments    — 20 most-traded crypto pairs on
                                    binance + bybit, and 10 Capital.com CFDs.
  * tickles_shared.indicator_catalog — everything registered in the new
                                    backtest.indicators dict.

Idempotent: every insert is an UPSERT on the natural key. Running twice is
a no-op.

Usage (on the VPS):
    sudo -u root python3 /opt/tickles/shared/scripts/seed_reference_data.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

# Allow running directly from the script folder.
# /opt/tickles is the project root so imports work as `shared.xxx`.
# Also add /opt/tickles/shared for the new `backtest` package.
sys.path.insert(0, "/opt/tickles")
sys.path.insert(0, "/opt/tickles/shared")

from backtest.indicators import INDICATORS  # registers all
from shared.utils.db import get_shared_pool

log = logging.getLogger("tickles.seed")


# ---------------------------------------------------------------------------
# Instruments — start small, curated list that covers the most liquid assets
# across crypto + CFD. Agents can add more via the catalog API later.
# ---------------------------------------------------------------------------
CRYPTO_PAIRS = [
    # Symbol          Base     Quote
    ("BTC/USDT",      "BTC",   "USDT"),
    ("ETH/USDT",      "ETH",   "USDT"),
    ("SOL/USDT",      "SOL",   "USDT"),
    ("BNB/USDT",      "BNB",   "USDT"),
    ("XRP/USDT",      "XRP",   "USDT"),
    ("DOGE/USDT",     "DOGE",  "USDT"),
    ("ADA/USDT",      "ADA",   "USDT"),
    ("AVAX/USDT",     "AVAX",  "USDT"),
    ("LINK/USDT",     "LINK",  "USDT"),
    ("MATIC/USDT",    "MATIC", "USDT"),
    ("DOT/USDT",      "DOT",   "USDT"),
    ("UNI/USDT",      "UNI",   "USDT"),
    ("LTC/USDT",      "LTC",   "USDT"),
    ("ATOM/USDT",     "ATOM",  "USDT"),
    ("ARB/USDT",      "ARB",   "USDT"),
    ("OP/USDT",       "OP",    "USDT"),
    ("SUI/USDT",      "SUI",   "USDT"),
    ("APT/USDT",      "APT",   "USDT"),
    ("FIL/USDT",      "FIL",   "USDT"),
    ("NEAR/USDT",     "NEAR",  "USDT"),
]

CRYPTO_EXCHANGES = ["binance", "bybit"]

# Capital.com CFDs (the ones you live-traded with previously).
CAPITAL_CFDS = [
    # (symbol, asset_class, base, quote, spread_pct, max_leverage)
    ("GOLD",    "commodity", "XAU", "USD", 0.0003, 100),
    ("SILVER",  "commodity", "XAG", "USD", 0.0012, 100),
    ("OIL",     "commodity", "BRT", "USD", 0.0030, 20),
    ("DE40",    "index",     "DE40","EUR", 0.0002, 100),
    ("US500",   "index",     "US500","USD",0.00015,100),
    ("US100",   "index",     "US100","USD",0.00020,100),
    ("EURUSD",  "forex",     "EUR", "USD", 0.00006, 200),
    ("GBPUSD",  "forex",     "GBP", "USD", 0.00010, 200),
    ("USDJPY",  "forex",     "USD", "JPY", 0.00010, 200),
    ("SOXL",    "stock",     "SOXL","USD", 0.0005,  5),
]


async def seed_instruments(pool) -> int:
    """Upsert all instruments. Returns number of NEW rows inserted."""
    n_new = 0
    async with pool.acquire() as conn:
        # Crypto × 2 exchanges.
        for (sym, base, quote) in CRYPTO_PAIRS:
            for exchange in CRYPTO_EXCHANGES:
                row = await conn.fetchrow("""
                    INSERT INTO instruments
                      (symbol, exchange, asset_class,
                       base_currency, quote_currency,
                       min_size, max_size, size_increment,
                       contract_multiplier,
                       spread_pct, maker_fee_pct, taker_fee_pct,
                       max_leverage, is_active)
                    VALUES
                      ($1, $2, 'crypto',
                       $3, $4,
                       0.00000001, 1000000, 0.00000001,
                       1.0,
                       0.0002, 0.0001, 0.0005,
                       10, TRUE)
                    ON CONFLICT (symbol, exchange) DO UPDATE SET
                      is_active = TRUE,
                      updated_at = CURRENT_TIMESTAMP
                    RETURNING (xmax = 0) AS inserted
                """, sym, exchange, base, quote)
                if row and row["inserted"]:
                    n_new += 1

        # Capital.com CFDs.
        for (sym, ac, base, quote, spread, lev) in CAPITAL_CFDS:
            row = await conn.fetchrow("""
                INSERT INTO instruments
                  (symbol, exchange, asset_class,
                   base_currency, quote_currency,
                   contract_multiplier,
                   spread_pct, maker_fee_pct, taker_fee_pct,
                   max_leverage, is_active)
                VALUES
                  ($1, 'capital', $2::asset_class_t,
                   $3, $4,
                   1.0,
                   $5, 0, 0,
                   $6, TRUE)
                ON CONFLICT (symbol, exchange) DO UPDATE SET
                  spread_pct  = EXCLUDED.spread_pct,
                  max_leverage = EXCLUDED.max_leverage,
                  is_active   = TRUE,
                  updated_at  = CURRENT_TIMESTAMP
                RETURNING (xmax = 0) AS inserted
            """, sym, ac, base, quote, spread, lev)
            if row and row["inserted"]:
                n_new += 1
    log.info("seed_instruments: %d new, %d total",
             n_new,
             (await pool.fetch_val("SELECT COUNT(*) FROM instruments")))
    return n_new


async def seed_indicator_catalog(pool) -> int:
    """Upsert every indicator from the in-process registry."""
    n_new = 0
    async with pool.acquire() as conn:
        for name, spec in INDICATORS.items():
            row = await conn.fetchrow("""
                INSERT INTO indicator_catalog
                  (name, category, direction, description,
                   default_params, param_ranges, source_system, is_active)
                VALUES
                  ($1, $2::indicator_category_t, $3::indicator_direction_t,
                   $4, $5::jsonb, $6::jsonb, 'jarvais_v2', TRUE)
                ON CONFLICT (name) DO UPDATE SET
                  category       = EXCLUDED.category,
                  direction      = EXCLUDED.direction,
                  description    = EXCLUDED.description,
                  default_params = EXCLUDED.default_params,
                  param_ranges   = EXCLUDED.param_ranges,
                  is_active      = TRUE,
                  updated_at     = CURRENT_TIMESTAMP
                RETURNING (xmax = 0) AS inserted
            """,
                name, spec.category, spec.direction, spec.description,
                json.dumps(spec.defaults),
                json.dumps(spec.param_ranges),
            )
            if row and row["inserted"]:
                n_new += 1
    total = await pool.fetch_val("SELECT COUNT(*) FROM indicator_catalog")
    log.info("seed_indicator_catalog: %d new, %d total", n_new, total)
    return n_new


async def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.info("seed_reference_data: starting")
    pool = await get_shared_pool()
    n1 = await seed_instruments(pool)
    n2 = await seed_indicator_catalog(pool)
    log.info("seed_reference_data: done (%d instruments, %d indicators new)",
             n1, n2)


if __name__ == "__main__":
    asyncio.run(main())
