"""
Module: epic_resolver
Purpose: Map common trading symbols to Capital.com epic codes for CFD channels.
Location: /opt/tickles/shared/intelligence/epic_resolver.py

Capital.com epic codes follow patterns like:
  CS.D.<SYMBOL>.CFD.IP  — most CFDs
  IX.D.<INDEX>.<TF>.IP   — indices (varies)

Discord traders often use informal names:
  "GOLD" -> "XAUUSD" -> "CS.D.XAUUSD.CFD.IP"
  "NAS100" -> "US100" -> "IX.D.NASDAQ.CFD.IP" (or similar)
  "BTCUSD" -> "CS.D.BTCUSD.CFD.IP"

This module provides fuzzy matching and a seed mapping table.
"""

import logging
import os
import re
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Seed mapping: common Discord names -> Capital.com epics
# These are the MOST LIKELY mappings; the actual epic may vary slightly
# depending on Capital.com's current instrument list.
# ---------------------------------------------------------------------------
_SEED_EPIC_MAP: Dict[str, str] = {
    # Metals
    "GOLD": "CS.D.XAUUSD.CFD.IP",
    "XAUUSD": "CS.D.XAUUSD.CFD.IP",
    "XAU/USD": "CS.D.XAUUSD.CFD.IP",
    "SILVER": "CS.D.XAGUSD.CFD.IP",
    "XAGUSD": "CS.D.XAGUSD.CFD.IP",
    "XAG/USD": "CS.D.XAGUSD.CFD.IP",
    "PLATINUM": "CS.D.PLAT.CFD.IP",
    "PALLADIUM": "CS.D.PALL.CFD.IP",

    # Indices
    "NAS100": "IX.D.NASDAQ.CFD.IP",
    "US100": "IX.D.NASDAQ.CFD.IP",
    "NASDAQ": "IX.D.NASDAQ.CFD.IP",
    "US30": "IX.D.DOW.CFD.IP",
    "DJ30": "IX.D.DOW.CFD.IP",
    "DOW": "IX.D.DOW.CFD.IP",
    "US500": "IX.D.SPX.CFD.IP",
    "SPX": "IX.D.SPX.CFD.IP",
    "SP500": "IX.D.SPX.CFD.IP",
    "GER40": "IX.D.DAX.CFD.IP",
    "DAX": "IX.D.DAX.CFD.IP",
    "UK100": "IX.D.FTSE.CFD.IP",
    "FTSE": "IX.D.FTSE.CFD.IP",
    "JP225": "IX.D.NIKKEI.CFD.IP",
    "NIKKEI": "IX.D.NIKKEI.CFD.IP",

    # Forex majors
    "EURUSD": "CS.D.EURUSD.CFD.IP",
    "EUR/USD": "CS.D.EURUSD.CFD.IP",
    "GBPUSD": "CS.D.GBPUSD.CFD.IP",
    "GBP/USD": "CS.D.GBPUSD.CFD.IP",
    "USDJPY": "CS.D.USDJPY.CFD.IP",
    "USD/JPY": "CS.D.USDJPY.CFD.IP",
    "AUDUSD": "CS.D.AUDUSD.CFD.IP",
    "AUD/USD": "CS.D.AUDUSD.CFD.IP",
    "USDCAD": "CS.D.USDCAD.CFD.IP",
    "USD/CAD": "CS.D.USDCAD.CFD.IP",
    "USDCHF": "CS.D.USDCHF.CFD.IP",
    "USD/CHF": "CS.D.USDCHF.CFD.IP",
    "NZDUSD": "CS.D.NZDUSD.CFD.IP",
    "NZD/USD": "CS.D.NZDUSD.CFD.IP",

    # Crypto
    "BTCUSD": "CS.D.BTCUSD.CFD.IP",
    "BTC/USD": "CS.D.BTCUSD.CFD.IP",
    "ETHUSD": "CS.D.ETHUSD.CFD.IP",
    "ETH/USD": "CS.D.ETHUSD.CFD.IP",
    "SOLUSD": "CS.D.SOLUSD.CFD.IP",
    "SOL/USD": "CS.D.SOLUSD.CFD.IP",
    "XRPUSD": "CS.D.XRPUSD.CFD.IP",
    "XRP/USD": "CS.D.XRPUSD.CFD.IP",

    # Commodities
    "OIL": "CS.D.USOIL.CFD.IP",
    "USOIL": "CS.D.USOIL.CFD.IP",
    "WTI": "CS.D.USOIL.CFD.IP",
    "BRENT": "CS.D.BRENT.CFD.IP",
    "UKOIL": "CS.D.BRENT.CFD.IP",
    "NATGAS": "CS.D.NATGAS.CFD.IP",
    "NG": "CS.D.NATGAS.CFD.IP",
    "COPPER": "CS.D.COPPER.CFD.IP",
}

# Compile reverse index for fuzzy search
_REVERSE_INDEX: List[Tuple[str, str]] = []
for k, v in _SEED_EPIC_MAP.items():
    _REVERSE_INDEX.append((k.upper(), v))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def resolve_epic(symbol: str) -> Optional[str]:
    """Resolve a symbol string to a Capital.com epic code.

    Args:
        symbol: Raw symbol from Discord message (e.g., 'GOLD', 'XAUUSD', 'NAS100').

    Returns:
        Capital.com epic code, or None if no match.
    """
    if not symbol:
        return None

    # Direct lookup
    normalized = _normalize_symbol(symbol)
    direct = _SEED_EPIC_MAP.get(normalized)
    if direct:
        return direct

    # Fuzzy: try stripping common suffixes/prefixes
    variants = _generate_variants(normalized)
    for v in variants:
        if v in _SEED_EPIC_MAP:
            return _SEED_EPIC_MAP[v]

    # Try reverse index partial match
    for idx_key, idx_val in _REVERSE_INDEX:
        if normalized in idx_key or idx_key in normalized:
            return idx_val

    return None


def resolve_epic_with_fallback(symbol: str, channel_mappings: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Resolve epic with optional channel-specific override mappings.

    Args:
        symbol: Raw symbol.
        channel_mappings: Per-channel epic overrides from watched_channels.epic_mappings.

    Returns:
        Epic code, or None.
    """
    normalized = _normalize_symbol(symbol)

    # Channel overrides take precedence
    if channel_mappings:
        override = channel_mappings.get(normalized) or channel_mappings.get(symbol.upper())
        if override:
            return override

    return resolve_epic(symbol)


def list_known_epics() -> List[Tuple[str, str]]:
    """Return all known (symbol, epic) pairs."""
    return list(_SEED_EPIC_MAP.items())


def add_custom_mapping(symbol: str, epic: str) -> None:
    """Add a runtime custom mapping (not persisted).

    Args:
        symbol: Symbol to map.
        epic: Capital.com epic code.
    """
    _SEED_EPIC_MAP[_normalize_symbol(symbol)] = epic
    _REVERSE_INDEX.append((_normalize_symbol(symbol), epic))
    logger.info("Added custom epic mapping: %s -> %s", symbol, epic)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _normalize_symbol(symbol: str) -> str:
    """Normalize a symbol for lookup: uppercase, strip whitespace, remove /-."""
    return re.sub(r"[\s/\-_.]", "", symbol.upper())


def _generate_variants(symbol: str) -> List[str]:
    """Generate likely variants of a symbol for fuzzy matching."""
    variants = [symbol]
    # Common suffixes
    for suffix in ("USD", "USDT", "CFD", "PERP", "SPOT"):
        if symbol.endswith(suffix):
            variants.append(symbol[: -len(suffix)])
        else:
            variants.append(symbol + suffix)
    # Slash form
    if len(symbol) == 6 and symbol.isalpha():
        variants.append(symbol[:3] + "/" + symbol[3:])
    return list(set(variants))


# ---------------------------------------------------------------------------
# Capital.com epic validation
# ---------------------------------------------------------------------------
def is_valid_epic_format(epic: str) -> bool:
    """Check if a string looks like a Capital.com epic code.

    Args:
        epic: String to validate.

    Returns:
        True if it matches expected epic patterns.
    """
    if not epic:
        return False
    # Common patterns: CS.D.XXX.CFD.IP, IX.D.XXX.CFD.IP, etc.
    return bool(re.match(r"^[A-Z]{2}\.[A-Z]\.[A-Z0-9]+\.(CFD|INDEX|CASH)\.[A-Z]{2}$", epic))


def extract_symbol_from_epic(epic: str) -> Optional[str]:
    """Extract the human-readable symbol from an epic code.

    Args:
        epic: Capital.com epic (e.g., 'CS.D.XAUUSD.CFD.IP').

    Returns:
        Symbol portion (e.g., 'XAUUSD'), or None.
    """
    if not epic:
        return None
    parts = epic.split(".")
    if len(parts) >= 3:
        return parts[2]
    return None
