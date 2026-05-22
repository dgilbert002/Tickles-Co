"""
Module: instrument_normaliser
Purpose: Single source of truth for symbol / venue normalisation.
         Canonical symbol form is the slash form (e.g. ``BTC/USDT``) which
         matches the CCXT / instruments-table convention. Per D6 (handoff
         2026-05-02), every pipeline write of a symbol MUST go through
         ``normalise_instrument`` so that downstream JOINs against
         ``public.instruments(symbol, exchange)`` succeed.
Location: /opt/tickles/shared/utils/instrument_normaliser.py
"""

from __future__ import annotations

import logging
import re
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Venue alias map. Lowercased. Lossy MERGE between cash/perp venues is forbidden;
# only cosmetic suffixes that mean the same venue are mapped here.
_VENUE_ALIASES = {
    "binance-futures": "binance",
    "bybit-perp": "bybit",
    "okx-swap": "okx",
    "kraken-futures": "kraken",
}

# Quote currencies recognised when splitting a concatenated symbol like
# "BTCUSDT" into "BTC/USDT". Order matters: longer suffixes first so we don't
# greedily eat "USD" out of "USDT". Add new quotes here as they appear.
_KNOWN_QUOTES: Tuple[str, ...] = (
    "USDT",
    "USDC",
    "BUSD",
    "TUSD",
    "FDUSD",
    "DAI",
    "USD",
    "EUR",
    "GBP",
    "JPY",
    "BTC",
    "ETH",
    "BNB",
)

# Perpetual / contract suffix tokens we strip back into a trailing "-<TAG>"
# (e.g. ``BTCUSDT.P`` → base ``BTCUSDT``, suffix ``P``). These are NOT part of
# the base/quote split — they are venue-specific contract tags.
_PERP_SUFFIXES: Tuple[str, ...] = ("PERP", "P", "SWAP", "FUT")

_NON_ALNUM_RX = re.compile(r"[^A-Z0-9]+")

# CCXT-style perpetual / swap symbols look like ``BTC/USDT:USDT`` (slash
# separator AND a colon-marked settle currency). When we see this exact
# pattern we strip the colon-and-settle tail BEFORE collapsing separators,
# otherwise the redundant settle currency (USDT) gets merged into the base
# (``BTCUSDTUSDT`` → wrongly splits as ``BTCUSDT/USDT``).
_CCXT_PERP_RX = re.compile(
    r"^([A-Z0-9]+)/([A-Z0-9]+):([A-Z0-9]+)(?:-.+)?$"
)


def _strip_contract_tag(token: str) -> Tuple[str, Optional[str]]:
    """Split off a trailing perpetual/contract tag, if any.

    Args:
        token: Already-uppercased symbol with all separators collapsed (e.g.
            ``BTCUSDTP``, ``BTCUSDTPERP``, ``BTCUSDT``).

    Returns:
        ``(core_token, contract_tag_or_None)``.
    """
    for tag in _PERP_SUFFIXES:
        if token.endswith(tag) and len(token) > len(tag):
            return token[: -len(tag)], tag
    return token, None


def _pre_strip_ccxt_perp(raw_upper: str) -> Tuple[str, bool]:
    """If ``raw_upper`` is a CCXT perp / swap, strip the ``:settle[-tail]``
    portion before the rest of normalisation runs.

    Args:
        raw_upper: Uppercased input (may still contain ``/`` and ``:``).

    Returns:
        ``(cleaned, was_perp)``. ``cleaned`` is ``raw_upper`` with the
        colon-and-settle tail removed (e.g. ``BTC/USDT:USDT`` becomes
        ``BTC/USDT``). ``was_perp`` is True when the strip happened.
    """
    m = _CCXT_PERP_RX.match(raw_upper)
    if not m:
        return raw_upper, False
    base, quote, _settle = m.group(1), m.group(2), m.group(3)
    return f"{base}/{quote}", True


def _split_base_quote(token: str) -> Optional[Tuple[str, str]]:
    """Split a concatenated symbol into ``(base, quote)`` using known quotes.

    Args:
        token: Uppercase, alnum-only symbol such as ``BTCUSDT``.

    Returns:
        ``(base, quote)`` if a known quote suffix matches, else ``None``.
    """
    for quote in _KNOWN_QUOTES:
        if token.endswith(quote) and len(token) > len(quote):
            base = token[: -len(quote)]
            return base, quote
    return None


def to_canonical_symbol(raw_symbol: str | None) -> str:
    """Return the canonical slash-form symbol, e.g. ``BTC/USDT``.

    Rules (applied in order):
        1. Uppercase the input and drop everything that isn't alnum.
        2. If the cleaned token already contains nothing recognisable
           (empty), return ``""``.
        3. Strip a trailing perpetual/contract tag (``P``, ``PERP``, ``SWAP``,
           ``FUT``).
        4. If the remaining token splits cleanly into ``base + known_quote``,
           return ``base/quote`` (and re-append the contract tag with a dash
           if one was stripped).
        5. Otherwise return the cleaned uppercase token unchanged so we never
           silently lose information.

    Args:
        raw_symbol: Free-form symbol such as ``btc/usdt``, ``BTCUSDT``,
            ``BTC-USDT``, ``BTCUSDT.P``.

    Returns:
        Canonical symbol string. Slash form for cleanly-splittable spot pairs.

    Examples:
        >>> to_canonical_symbol("btc/usdt")
        'BTC/USDT'
        >>> to_canonical_symbol("BTCUSDT")
        'BTC/USDT'
        >>> to_canonical_symbol("BTC-USDT")
        'BTC/USDT'
        >>> to_canonical_symbol("BTCUSDT.P")
        'BTC/USDT-P'
        >>> to_canonical_symbol("XAUUSD")
        'XAU/USD'
        >>> to_canonical_symbol("UNKNOWN123")
        'UNKNOWN123'
        >>> to_canonical_symbol("")
        ''
        >>> to_canonical_symbol(None)
        ''
    """
    try:
        if not raw_symbol:
            return ""
        raw_upper = raw_symbol.upper().strip()
        # CCXT-perp short-circuit: if the input is ``BTC/USDT:USDT`` (or
        # ``BTC/USDT:USDT-260626-90000-C`` for options) we can lift base
        # and quote directly without going through the alnum-collapse path
        # that was historically merging the settle currency into the base.
        # Returns the *spot* slash form (``BTC/USDT``) — the perp/swap
        # marker is intentionally dropped because downstream tables join
        # on the spot form and the per-venue contract tag lives elsewhere.
        cleaned_pre, was_ccxt_perp = _pre_strip_ccxt_perp(raw_upper)
        if was_ccxt_perp:
            return cleaned_pre
        cleaned = _NON_ALNUM_RX.sub("", raw_upper)
        if not cleaned:
            return ""
        core, tag = _strip_contract_tag(cleaned)
        split = _split_base_quote(core)
        if split is None:
            # Couldn't recognise a quote — return unmodified uppercase form.
            return cleaned if tag is None else f"{core}-{tag}"
        base, quote = split
        slash = f"{base}/{quote}"
        return slash if tag is None else f"{slash}-{tag}"
    except (AttributeError, TypeError) as exc:
        logger.warning("to_canonical_symbol failed for %r: %s", raw_symbol, exc)
        return ""


def normalise_venue(raw_exchange: str | None) -> str:
    """Lower-case and alias-map a venue / exchange string.

    Args:
        raw_exchange: Raw exchange name (e.g. ``BYBIT``, ``binance-futures``).

    Returns:
        Canonical venue string. Returns ``"unknown"`` for None / empty input.
    """
    try:
        v = (raw_exchange or "unknown").strip().lower() or "unknown"
        return _VENUE_ALIASES.get(v, v)
    except AttributeError as exc:
        logger.warning("normalise_venue failed for %r: %s", raw_exchange, exc)
        return "unknown"


def normalise_instrument(
    raw_symbol: str, raw_exchange: str | None
) -> Tuple[str, str]:
    """Return ``(canonical_symbol, canonical_exchange)``.

    Canonical form per D6 (2026-05-02 handoff): slash form for the symbol
    (``BTC/USDT``), lowercase + alias-mapped for the exchange. This MUST be
    used by every pipeline writer that touches ``instrument_symbol`` and is
    the form persisted into ``instruments.symbol``.

    Args:
        raw_symbol: Raw symbol string (e.g. ``btc/usdt``, ``BTCUSDT``).
        raw_exchange: Raw exchange string (e.g. ``BYBIT``,
            ``binance-futures``).

    Returns:
        Tuple of ``(canonical_symbol, canonical_exchange)``.

    Examples:
        >>> normalise_instrument("btc/usdt", "BYBIT")
        ('BTC/USDT', 'bybit')
        >>> normalise_instrument("BTCUSDT", "binance-futures")
        ('BTC/USDT', 'binance')
        >>> normalise_instrument("BTC-USDT", "BYBIT")
        ('BTC/USDT', 'bybit')
        >>> normalise_instrument("ETH-PERP", None)
        ('ETH-PERP', 'unknown')
    """
    return to_canonical_symbol(raw_symbol), normalise_venue(raw_exchange)
