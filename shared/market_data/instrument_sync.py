"""
Module: instrument_sync
Purpose: Populate public.unified_instruments from all .env-configured exchanges.
         Syncs instruments from Bybit, BloFin, Bitget (via CCXT) and Capital.com
         (via REST API). Idempotent — safe to run frequently.
Location: /opt/tickles/shared/market_data/instrument_sync.py
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

import aiohttp

from shared.utils.db import get_shared_pool
from shared.utils.instrument_normaliser import normalise_venue, to_canonical_symbol

logger = logging.getLogger(__name__)

# Quote currencies accepted for crypto pairs (USDT/USDC/BUSD only).
_CRYPTO_QUOTES: Set[str] = {"USDT", "USDC", "BUSD"}

# Capital.com API base URLs.
_CAPITAL_DEMO_URL = "https://demo-api-capital.backend-capital.com/api/v1"
_CAPITAL_LIVE_URL = "https://api-capital.backend-capital.com/api/v1"

# Capital.com search terms for market discovery.
_CAPITAL_SEARCH_TERMS = [
    "EUR", "GBP", "USD", "JPY", "BTC", "ETH", "GOLD",
    "OIL", "NASDAQ", "SP500", "DOW", "DAX", "FTSE",
    "XAU", "XAG", "US30", "US100", "US500", "GER40",
    "AUD", "NZD", "CAD", "CHF", "SGD", "HKD",
    "NATGAS", "COPPER", "PLATINUM", "PALLADIUM",
    "SOYBEAN", "WHEAT", "CORN", "SUGAR", "COFFEE",
    "TESLA", "APPLE", "AMAZON", "MICROSOFT", "GOOGLE",
    "META", "NETFLIX", "NVIDIA", "VISA", "JPMORGAN",
]

# Mapping from Capital.com instrument type to our asset_type enum.
_CAPITAL_TYPE_MAP: Dict[str, str] = {
    "currencies": "forex",
    "commodities": "commodity",
    "indices": "index",
    "shares": "stock",
    "cryptocurrencies": "crypto",
}


def _get_configured_crypto_exchanges() -> Dict[str, Dict[str, str]]:
    """Discover which crypto exchanges have API keys configured in .env.

    Returns:
        Dict mapping exchange name to CCXT config dict (apiKey, secret, password).
    """
    exchanges: Dict[str, Dict[str, str]] = {}

    # Bybit
    bybit_key = os.environ.get("BYBIT_DEMO_API_KEY") or os.environ.get("BYBIT_API_KEY", "")
    bybit_secret = os.environ.get("BYBIT_DEMO_API_SECRET") or os.environ.get("BYBIT_API_SECRET", "")
    if bybit_key and bybit_secret:
        exchanges["bybit"] = {"apiKey": bybit_key, "secret": bybit_secret}

    # BloFin
    blofin_key = os.environ.get("BLOFIN_API_KEY") or os.environ.get("BLOFIN_DEMO_API_KEY", "")
    blofin_secret = os.environ.get("BLOFIN_API_SECRET") or os.environ.get("BLOFIN_DEMO_API_SECRET", "")
    if blofin_key and blofin_secret:
        exchanges["blofin"] = {
            "apiKey": blofin_key,
            "secret": blofin_secret,
            "password": os.environ.get("BLOFIN_API_PHRASE", ""),
        }

    # Bitget
    bitget_key = os.environ.get("BITGET_API_KEY", "")
    bitget_secret = os.environ.get("BITGET_API_SECRET", "")
    if bitget_key and bitget_secret:
        exchanges["bitget"] = {
            "apiKey": bitget_key,
            "secret": bitget_secret,
            "password": os.environ.get("BITGET_API_PASSPHRASE", os.environ.get("BITGET_API_PHASE", "")),
        }

    return exchanges


def _is_capital_configured() -> bool:
    """Check whether Capital.com credentials are present in .env."""
    email = os.environ.get("CAPITAL_EMAIL", "")
    password = os.environ.get("CAPITAL_PASSWORD", "")
    api_key = os.environ.get("CAPITAL_API_KEY", "")
    return bool(email and password and api_key)


def _capital_base_url() -> str:
    """Return the Capital.com base URL based on CAPITAL_ENV."""
    env = os.environ.get("CAPITAL_ENV", "demo").lower()
    return _CAPITAL_LIVE_URL if env == "live" else _CAPITAL_DEMO_URL


def _derive_asset_type(
    base: str, quote: str, exchange: str, market_type: Optional[str] = None
) -> str:
    """Derive the asset_type enum value from market metadata.

    Args:
        base: Base currency (e.g. 'BTC', 'XAU').
        quote: Quote currency (e.g. 'USDT', 'USD').
        exchange: Exchange name.
        market_type: Optional CCXT market type ('spot', 'swap', etc.).

    Returns:
        One of 'crypto', 'forex', 'commodity', 'index', 'stock'.
    """
    if exchange in ("bybit", "blofin", "bitget"):
        return "crypto"

    # For Capital.com, use the market_type if provided.
    if market_type:
        return _CAPITAL_TYPE_MAP.get(market_type.lower(), "forex")

    # Heuristic fallback for unknown exchanges.
    if quote in ("USDT", "USDC", "BUSD"):
        return "crypto"
    return "forex"


async def _fetch_ccxt_markets(
    exchange_name: str, config: Dict[str, str]
) -> List[Dict[str, Any]]:
    """Fetch all active markets from a CCXT-supported exchange.

    Args:
        exchange_name: CCXT exchange id (e.g. 'bybit', 'blofin', 'bitget').
        config: Dict with apiKey, secret, and optional password.

    Returns:
        List of normalized instrument dicts ready for upsert.
    """
    try:
        import ccxt.async_support as ccxt_async
    except ImportError as exc:
        logger.error("CCXT not installed; cannot fetch %s markets: %s", exchange_name, exc)
        return []

    exchange_class = getattr(ccxt_async, exchange_name, None)
    if exchange_class is None:
        logger.error("Unknown CCXT exchange id: %s", exchange_name)
        return []

    ccxt_config: Dict[str, Any] = {
        "enableRateLimit": True,
        "apiKey": config.get("apiKey", ""),
        "secret": config.get("secret", ""),
    }
    if config.get("password"):
        ccxt_config["password"] = config["password"]

    # Try with provided credentials first; fall back to unauthenticated
    # (public market data does not require API keys on most exchanges).
    instruments: List[Dict[str, Any]] = []
    for attempt, use_auth in enumerate((True, False)):
        if attempt == 1:
            logger.info("Retrying %s without authentication (public data only)...", exchange_name)
            ccxt_config.pop("apiKey", None)
            ccxt_config.pop("secret", None)
            ccxt_config.pop("password", None)

        exchange = exchange_class(ccxt_config)
        try:
            markets = await exchange.load_markets()
            for raw_symbol, market in markets.items():
                if not market or not market.get("active", True):
                    continue

                base = (market.get("base") or "").upper()
                quote = (market.get("quote") or "").upper()

                if not base or not quote:
                    continue

                # Filter crypto to USDT/USDC/BUSD quoted pairs only.
                if quote not in _CRYPTO_QUOTES:
                    continue

                canonical_symbol = to_canonical_symbol(raw_symbol)
                if not canonical_symbol:
                    continue

                instruments.append({
                    "canonical_asset": base,
                    "asset_type": "crypto",
                    "base_currency": base,
                    "quote_currency": quote,
                    "exchange": normalise_venue(exchange_name),
                    "exchange_symbol": raw_symbol,
                    "canonical_symbol": canonical_symbol,
                })

            logger.info("CCXT %s: %d instruments fetched", exchange_name, len(instruments))
            break  # success — don't retry
        except Exception as exc:
            logger.warning("load_markets failed for %s (attempt %d): %s", exchange_name, attempt + 1, exc)
        finally:
            try:
                await exchange.close()
            except Exception:
                pass

        if instruments:
            break  # got data, stop retrying

    return instruments


async def _fetch_capital_markets() -> List[Dict[str, Any]]:
    """Fetch all available CFD instruments from Capital.com.

    Authenticates using .env credentials, then searches for instruments
    across multiple categories and returns normalized results.

    Returns:
        List of normalized instrument dicts ready for upsert.
    """
    if not _is_capital_configured():
        logger.info("Capital.com credentials not configured; skipping.")
        return []

    email = os.environ["CAPITAL_EMAIL"]
    password = os.environ["CAPITAL_PASSWORD"]
    api_key = os.environ["CAPITAL_API_KEY"]
    base_url = _capital_base_url()

    # Step 1: Authenticate to get session tokens.
    auth_payload = {"identifier": email, "password": password}
    auth_headers = {
        "Content-Type": "application/json",
        "X-CAP-API-KEY": api_key,
    }

    cst: Optional[str] = None
    x_security_token: Optional[str] = None

    try:
        async with aiohttp.ClientSession(headers=auth_headers) as session:
            async with session.post(
                f"{base_url}/session",
                json=auth_payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error("Capital.com auth failed: %s — %s", resp.status, body[:200])
                    return []
                cst = resp.headers.get("CST")
                x_security_token = resp.headers.get("X-SECURITY-TOKEN")
                if not cst or not x_security_token:
                    logger.error("Capital.com auth response missing tokens.")
                    return []

        logger.info("Capital.com authenticated successfully.")
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.exception("Capital.com auth network error: %s", exc)
        return []

    # Step 2: Search for markets.
    instruments: List[Dict[str, Any]] = []
    seen_epics: Set[str] = set()

    search_headers = {
        "Content-Type": "application/json",
        "X-CAP-API-KEY": api_key,
        "CST": cst,
        "X-SECURITY-TOKEN": x_security_token,
    }

    async with aiohttp.ClientSession(headers=search_headers) as session:
        for term in _CAPITAL_SEARCH_TERMS:
            try:
                url = f"{base_url}/markets"
                async with session.get(
                    url,
                    params={"searchTerm": term},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "Capital.com /markets search for '%s' returned %d",
                            term, resp.status,
                        )
                        continue

                    data = await resp.json()
                    markets = data.get("markets", [])

                    for market in markets:
                        epic = market.get("epic", "")
                        if not epic or epic in seen_epics:
                            continue
                        seen_epics.add(epic)

                        instrument_data = market.get("instrument", {})
                        inst_type = instrument_data.get("type", "").lower()
                        name = instrument_data.get("name", epic)

                        # Derive base/quote from epic (e.g. CS.D.EURUSD.CFD.IP).
                        parts = epic.split(".")
                        base = ""
                        quote = ""
                        if len(parts) >= 3:
                            symbol_part = parts[2]
                            if len(symbol_part) == 6:
                                base = symbol_part[:3]
                                quote = symbol_part[3:]
                            else:
                                base = symbol_part
                                quote = "USD"

                        if not base:
                            base = name

                        canonical_symbol = to_canonical_symbol(f"{base}/{quote}" if quote else base)
                        asset_type = _derive_asset_type(base, quote, "capital.com", inst_type)

                        instruments.append({
                            "canonical_asset": base.upper(),
                            "asset_type": asset_type,
                            "base_currency": base.upper(),
                            "quote_currency": quote.upper() or "USD",
                            "exchange": "capital.com",
                            "exchange_symbol": epic,
                            "canonical_symbol": canonical_symbol or f"{base}/{quote}",
                        })

                await asyncio.sleep(0.3)  # Rate limiting between search terms.

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning("Capital.com search for '%s' failed: %s", term, exc)
                continue

    logger.info("Capital.com: %d instruments fetched", len(instruments))
    return instruments


async def _upsert_instruments(instruments: List[Dict[str, Any]]) -> int:
    """Upsert instruments into public.unified_instruments.

    Uses INSERT ON CONFLICT (exchange, exchange_symbol) DO UPDATE to make
    the sync idempotent. Updates last_synced_at and is_active for existing
    rows; inserts new rows.

    Args:
        instruments: List of normalized instrument dicts.

    Returns:
        Number of rows inserted or updated.
    """
    if not instruments:
        return 0

    pool = await get_shared_pool()
    now = datetime.now(timezone.utc)

    query = """
        INSERT INTO public.unified_instruments
            (canonical_asset, asset_type, base_currency, quote_currency,
             exchange, exchange_symbol, canonical_symbol, is_active,
             last_synced_at, created_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, TRUE, $8, $9)
        ON CONFLICT (exchange, exchange_symbol)
        DO UPDATE SET
            canonical_asset = EXCLUDED.canonical_asset,
            asset_type = EXCLUDED.asset_type,
            base_currency = EXCLUDED.base_currency,
            quote_currency = EXCLUDED.quote_currency,
            canonical_symbol = EXCLUDED.canonical_symbol,
            is_active = TRUE,
            last_synced_at = EXCLUDED.last_synced_at
    """

    count = 0
    for inst in instruments:
        try:
            await pool.execute(
                query,
                (
                    inst["canonical_asset"],
                    inst["asset_type"],
                    inst["base_currency"],
                    inst["quote_currency"],
                    inst["exchange"],
                    inst["exchange_symbol"],
                    inst["canonical_symbol"],
                    now,
                    now,
                ),
            )
            count += 1
        except Exception as exc:
            logger.warning(
                "Failed to upsert instrument %s/%s: %s",
                inst.get("exchange"), inst.get("exchange_symbol"), exc,
            )

    logger.info("Upserted %d instruments into unified_instruments.", count)
    return count


async def _mark_stale_instruments(
    exchange: str, active_symbols: Set[str]
) -> int:
    """Mark instruments not in the active set as is_active = FALSE.

    Args:
        exchange: Exchange name to scope the update.
        active_symbols: Set of exchange_symbol values that are still active.

    Returns:
        Number of rows marked inactive.
    """
    if not active_symbols:
        return 0

    pool = await get_shared_pool()

    # Build a safe IN clause using asyncpg array parameter.
    result = await pool.execute(
        """
        UPDATE public.unified_instruments
        SET is_active = FALSE
        WHERE exchange = $1
          AND exchange_symbol != ALL($2::varchar[])
          AND is_active = TRUE
        """,
        (exchange, list(active_symbols)),
    )

    # asyncpg returns the affected row count as an int.
    count = result if isinstance(result, int) else 0
    if count:
        logger.info("Marked %d stale instruments inactive for %s.", count, exchange)
    return count


async def sync_instruments() -> Dict[str, int]:
    """Sync instruments from all configured exchanges into unified_instruments.

    Workflow:
        1. Discover configured crypto exchanges from .env.
        2. For each: call CCXT fetch_markets(), filter to USDT/USDC/BUSD pairs.
        3. For Capital.com: authenticate and search for CFD instruments.
        4. Upsert all instruments into public.unified_instruments.
        5. Mark instruments no longer returned as is_active = FALSE.

    Returns:
        Dict mapping exchange name to count of instruments synced.
    """
    results: Dict[str, int] = {}

    # --- Crypto exchanges via CCXT ---
    crypto_exchanges = _get_configured_crypto_exchanges()
    logger.info(
        "Configured crypto exchanges: %s",
        list(crypto_exchanges.keys()) if crypto_exchanges else "none",
    )

    for exchange_name, config in crypto_exchanges.items():
        logger.info("Fetching markets for %s...", exchange_name)
        instruments = await _fetch_ccxt_markets(exchange_name, config)
        if instruments:
            count = await _upsert_instruments(instruments)
            results[exchange_name] = count

            # Mark stale instruments.
            active_symbols = {inst["exchange_symbol"] for inst in instruments}
            await _mark_stale_instruments(exchange_name, active_symbols)
        else:
            logger.warning("No instruments fetched for %s.", exchange_name)
            results[exchange_name] = 0

    # --- Capital.com CFDs ---
    if _is_capital_configured():
        logger.info("Fetching Capital.com markets...")
        capital_instruments = await _fetch_capital_markets()
        if capital_instruments:
            count = await _upsert_instruments(capital_instruments)
            results["capital.com"] = count

            active_symbols = {inst["exchange_symbol"] for inst in capital_instruments}
            await _mark_stale_instruments("capital.com", active_symbols)
        else:
            logger.warning("No instruments fetched for capital.com.")
            results["capital.com"] = 0

    logger.info("Instrument sync complete: %s", results)
    return results


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    """Run instrument sync as a standalone script."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    results = await sync_instruments()
    total = sum(results.values())
    logger.info("Total instruments synced: %d across %d exchanges.", total, len(results))


if __name__ == "__main__":
    asyncio.run(main())
