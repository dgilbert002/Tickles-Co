"""
Module: asset_register
Purpose: Unified asset register lookup utilities. Provides resolve_asset() for
         symbol+exchange → canonical asset resolution and get_active_exchanges()
         for discovering which exchanges have active API keys.
Location: /opt/tickles/shared/utils/asset_register.py
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from shared.utils.db import get_shared_pool
from shared.utils.instrument_normaliser import normalise_venue, to_canonical_symbol

logger = logging.getLogger(__name__)

# Quote currencies accepted for crypto pairs.
_CRYPTO_QUOTES = {"USDT", "USDC", "BUSD"}


async def resolve_asset(
    symbol: str,
    exchange: str,
) -> Optional[Dict[str, Any]]:
    """Look up the canonical asset for a symbol+exchange pair.

    Normalizes the input symbol and exchange, then queries
    public.unified_instruments for a matching row.

    Args:
        symbol: Raw symbol string (e.g. 'BTC/USDT', 'BTCUSDT', 'XAU/USD').
        exchange: Raw exchange name (e.g. 'bybit', 'capital.com').

    Returns:
        Dict with keys canonical_asset, asset_type, base_currency,
        quote_currency, exchange, exchange_symbol, canonical_symbol,
        is_active, or None if no match found.

    Examples:
        >>> await resolve_asset('BTC/USDT', 'bybit')
        {'canonical_asset': 'BTC', 'asset_type': 'crypto', ...}
        >>> await resolve_asset('XAU/USD', 'capital.com')
        {'canonical_asset': 'XAU', 'asset_type': 'commodity', ...}
    """
    canonical_symbol = to_canonical_symbol(symbol)
    canonical_exchange = normalise_venue(exchange)

    if not canonical_symbol or canonical_exchange == "unknown":
        logger.warning(
            "resolve_asset: invalid input symbol=%r exchange=%r", symbol, exchange
        )
        return None

    pool = await get_shared_pool()

    # Try exact match on canonical_symbol + exchange first.
    row = await pool.fetch_one(
        """
        SELECT canonical_asset, asset_type, base_currency, quote_currency,
               exchange, exchange_symbol, canonical_symbol, is_active
        FROM public.unified_instruments
        WHERE canonical_symbol = $1 AND exchange = $2 AND is_active = TRUE
        LIMIT 1
        """,
        canonical_symbol,
        canonical_exchange,
    )

    if row:
        return dict(row)

    # Fallback: try matching by exchange_symbol (the raw exchange-specific symbol).
    row = await pool.fetch_one(
        """
        SELECT canonical_asset, asset_type, base_currency, quote_currency,
               exchange, exchange_symbol, canonical_symbol, is_active
        FROM public.unified_instruments
        WHERE exchange_symbol = $1 AND exchange = $2 AND is_active = TRUE
        LIMIT 1
        """,
        canonical_symbol,
        canonical_exchange,
    )

    if row:
        return dict(row)

    logger.debug(
        "resolve_asset: no match for symbol=%r exchange=%r",
        canonical_symbol, canonical_exchange,
    )
    return None


async def get_instruments_for_asset(canonical_asset: str) -> List[Dict[str, Any]]:
    """Get all exchange-specific instruments for a canonical asset.

    Args:
        canonical_asset: Canonical asset identifier (e.g. 'BTC', 'XAU').

    Returns:
        List of instrument dicts across all exchanges.
    """
    pool = await get_shared_pool()
    rows = await pool.fetch_all(
        """
        SELECT canonical_asset, asset_type, base_currency, quote_currency,
               exchange, exchange_symbol, canonical_symbol, is_active
        FROM public.unified_instruments
        WHERE canonical_asset = $1 AND is_active = TRUE
        ORDER BY exchange, canonical_symbol
        """,
        canonical_asset.upper(),
    )
    return [dict(row) for row in rows]


def get_active_exchanges() -> List[str]:
    """Return list of exchanges with active API keys configured in .env.

    Checks for the presence of API credentials for Bybit, BloFin, Bitget,
    and Capital.com. Does NOT require a database connection.

    Returns:
        List of exchange names (lowercase) that have credentials configured.
    """
    active: List[str] = []

    # Bybit
    bybit_key = os.environ.get("BYBIT_DEMO_API_KEY") or os.environ.get("BYBIT_API_KEY", "")
    bybit_secret = os.environ.get("BYBIT_DEMO_API_SECRET") or os.environ.get("BYBIT_API_SECRET", "")
    if bybit_key and bybit_secret:
        active.append("bybit")

    # BloFin
    blofin_key = os.environ.get("BLOFIN_API_KEY") or os.environ.get("BLOFIN_DEMO_API_KEY", "")
    blofin_secret = os.environ.get("BLOFIN_API_SECRET") or os.environ.get("BLOFIN_DEMO_API_SECRET", "")
    if blofin_key and blofin_secret:
        active.append("blofin")

    # Bitget
    bitget_key = os.environ.get("BITGET_API_KEY", "")
    bitget_secret = os.environ.get("BITGET_API_SECRET", "")
    if bitget_key and bitget_secret:
        active.append("bitget")

    # Capital.com
    capital_email = os.environ.get("CAPITAL_EMAIL", "")
    capital_password = os.environ.get("CAPITAL_PASSWORD", "")
    capital_api_key = os.environ.get("CAPITAL_API_KEY", "")
    if capital_email and capital_password and capital_api_key:
        active.append("capital.com")

    return active


async def get_exchange_instrument_count(exchange: str) -> int:
    """Get the count of active instruments for a specific exchange.

    Args:
        exchange: Exchange name (e.g. 'bybit', 'capital.com').

    Returns:
        Number of active instruments.
    """
    pool = await get_shared_pool()
    row = await pool.fetch_one(
        """
        SELECT COUNT(*) AS cnt
        FROM public.unified_instruments
        WHERE exchange = $1 AND is_active = TRUE
        """,
        normalise_venue(exchange),
    )
    return row["cnt"] if row else 0


async def get_all_active_instruments(
    exchange: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Get all active instruments, optionally filtered by exchange.

    Args:
        exchange: Optional exchange filter.

    Returns:
        List of active instrument dicts.
    """
    pool = await get_shared_pool()

    if exchange:
        rows = await pool.fetch_all(
            """
            SELECT canonical_asset, asset_type, base_currency, quote_currency,
                   exchange, exchange_symbol, canonical_symbol, is_active
            FROM public.unified_instruments
            WHERE exchange = $1 AND is_active = TRUE
            ORDER BY canonical_symbol
            """,
            normalise_venue(exchange),
        )
    else:
        rows = await pool.fetch_all(
            """
            SELECT canonical_asset, asset_type, base_currency, quote_currency,
                   exchange, exchange_symbol, canonical_symbol, is_active
            FROM public.unified_instruments
            WHERE is_active = TRUE
            ORDER BY exchange, canonical_symbol
            """,
        )

    return [dict(row) for row in rows]
