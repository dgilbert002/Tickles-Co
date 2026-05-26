"""
Module: exchange_router
Purpose: Resolve a free-text symbol from a trader signal to a concrete
         (exchange, exchange_symbol, asset_class, epic_code) tuple that
         downstream code (PositionMonitor candle paths, live_price helper,
         interpretation_service INSERT) can act on without guessing.
Location: /opt/tickles/shared/utils/exchange_router.py
Round:    Round 13 (2026-05-24) — crypto-first preference + perp-form persist.

Why this exists
---------------
Before Round 12, every new tracked_position row defaulted to
``instrument_exchange='bybit'`` regardless of whether the symbol was a
crypto pair, a CFD index, a forex major, or a TradingView aggregate. This
caused PositionMonitor to spam ~20,000 "bybit does not have market symbol"
errors per day on four symbols (GOLD, US100, 1000PEPE/USDT, COTI/USDT).

Round 12 introduced the first version of this router (unified_instruments
lookup + alias table + Capital.com path).

Round 13 (2026-05-24) reshapes the policy in three ways based on
operator review of Round 12 output:

  1. **Crypto-first.** Every trader signal is routed to a crypto
     exchange (bybit / bitget / blofin) when ANY of those venues lists
     a tokenised equivalent. Capital.com is intentionally parked for
     now — its rows are still in ``unified_instruments`` and live-price
     polling continues, but ``resolve_market`` will not return a
     Capital route. Symbols that ONLY exist on Capital.com (forex
     majors, EU/Asian indices, untokenised commodities) get
     ``unsupported_reason='capital_only_parked'`` so they surface in
     the dashboard but don't burn cycles. The flip to "actually use
     Capital" will be a one-line change here.

  2. **Crypto-first remapping.** GOLD/SILVER/SPX/NAS100/NQ/etc. now
     redirect to the tokenised crypto equivalent (XAU/USDT,
     XAG/USDT, SPX/USDT, QQQ/USDT). All three crypto venues
     populate these in unified_instruments. The user-confirmed
     preference for gold is the synthetic perp ``XAU/USDT:USDT``
     (deepest cross-venue liquidity); ``PAXG/USDT`` and ``XAUT/USDT``
     remain reachable by their explicit names but are not the
     default for "GOLD".

  3. **Persist the perp form.** Trader signals are virtually always
     for perpetual swaps, not spot. Round 12 stored the SPOT
     canonical (``BTC/USDT``) as ``tracked_positions.instrument_symbol``
     even though PositionMonitor was actually polling the perp
     (``BTC/USDT:USDT``). Round 13 stores the perp form so the
     dashboard, postmortem, and edge scorer all see the truthful
     instrument we're tracking. Dashboard renderers can transform
     ``BTC/USDT:USDT`` back to ``BTCUSDT.P`` (Bybit-on-TradingView
     convention) for display.

Other Round 13 fixes
--------------------
  * ``BTCUSDT.P`` no longer flagged ``malformed_symbol``. The malformed
    regex previously fired on any base+USDT+.P combo — that hit every
    Bybit-style perp ticker traders write on TradingView. Tightened
    to only match base-less junk (``USDT.P``, ``HUSDT.P``).

  * Bare crypto tickers default to USDT. Trader writes ``BTC`` or
    ``PAXG`` → router resolves to ``BTC/USDT:USDT`` /
    ``PAXG/USDT:USDT`` instead of "unsupported". Driven by an
    explicit known-crypto-base set so we don't silently catch
    arbitrary 3-letter strings.

  * ``BTC/USD`` / ``BTCUSD`` / ``ETHUSD`` redirect to the crypto perp.
    Capital.com lists these as forex CFDs but a trader writing
    ``BTC/USD`` virtually always means the crypto market.

  * ``BTCUSDT.P`` style normaliser output (canonical = ``BTC/USDT-P``)
    is recognised as an explicit perp request — strip the ``-P`` tag
    and use the perp form directly without going through the
    spot-then-synthesise dance.

Public API
----------
* ``resolve_market(symbol, hint_exchange=None)`` -> ``RoutedMarket``
  Async coroutine. The single entry point — every consumer goes through
  this. Behaviour:
    - Always returns a ``RoutedMarket`` (never raises).
    - For supported crypto routes: ``canonical_symbol`` and
      ``ccxt_perp_symbol`` are BOTH set to the perp swap form
      (``BTC/USDT:USDT``). Same string by design — Round 13 collapses
      the spot/perp distinction at the routing layer.
    - For Capital-only matches: ``supported=False``,
      ``unsupported_reason='capital_only_parked'``. The matched epic
      is preserved in ``epic_code`` for the future re-enable.
* ``RoutedMarket`` dataclass — see field docs below.
* ``UNSUPPORTED_REASONS`` — string enum-style constants.
* ``unsupported_status_reason(routed, ts_iso)`` — formatter for
  ``tracked_positions.status_reason``.

What this is NOT
----------------
* Not a price source. Routing only — actually fetching the candle stays
  in CCXTAdapter / CapitalAdapter / live_price.
* Not a writer. Read-only.
* Not coupled to specific exchange names beyond the crypto-vs-CFD
  binary. Adding a new crypto exchange means populating it in
  ``unified_instruments`` — the router picks it up automatically as
  long as ``ORDER BY exchange`` priority lists it.

Logging follows the project rule: function name + parameters + return
value at debug level for every resolution.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Dict, FrozenSet, Optional, Tuple

from shared.utils.db import get_shared_pool
from shared.utils.instrument_normaliser import to_canonical_symbol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
class UNSUPPORTED_REASONS:
    """Stable string identifiers for the reason a symbol cannot be routed.

    These are intentionally short + machine-readable because they end up
    in ``tracked_positions.status_reason`` as ``unsupported:<reason>``.
    """

    AGGREGATE = "tradingview_aggregate"          # USDT.D, BTC.D, TOTAL3
    MALFORMED = "malformed_symbol"               # HUSDT.P, USDT.P (base-less only)
    NOT_LISTED = "not_in_unified_instruments"    # Real-looking but no exchange has it
    EMPTY = "empty_symbol"                       # Defensive only
    CAPITAL_ONLY = "capital_only_parked"         # Round 13: only Capital.com has it; we don't route there yet
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Symbol classifiers
# ---------------------------------------------------------------------------
# TradingView aggregates / dominance indices — not tradable anywhere
# natively. Refused upfront rather than letting them sit pending forever.
_AGGREGATE_SYMBOLS: FrozenSet[str] = frozenset({
    "USDT.D", "USDC.D", "BTC.D", "ETH.D",
    "TOTAL", "TOTAL2", "TOTAL3", "OTHERS",
    "DXY",  # No clean canonical in our tables today.
})

# Round 13 (2026-05-24): Tightened from the Round 12 ``^[A-Z]+USDT[./-]P$``
# pattern, which incorrectly flagged real perp tickers like ``BTCUSDT.P``,
# ``ETHUSDT.P``, ``LDOUSDT.P``. The new pattern only matches base-less or
# single-char-base junk (``USDT.P``, ``HUSDT.P``, ``XUSDT.P``). Any ticker
# with a 2+ char base falls through to the normal resolver.
_MALFORMED_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"^[A-Z]?USDT[./-]P$", re.IGNORECASE),
)


# ---------------------------------------------------------------------------
# Round 13 — crypto-first remapping table
# ---------------------------------------------------------------------------
# Trader writes a CFD-style symbol; we redirect to the tokenised crypto
# equivalent so the position lands on bybit/bitget/blofin instead of
# Capital. Every value here MUST exist (verified live) on at least one
# crypto venue in unified_instruments. Inputs are uppercased before lookup.
#
# Key naming:
#   * Index aliases: NAS100/NASDAQ/NDX/NQ/US100 → QQQ/USDT
#                    SPX/SP500/US500           → SPX/USDT
#   * Commodity aliases: GOLD/XAU/XAUUSD/XAU.USD → XAU/USDT  (synthetic perp)
#                        SILVER/XAG/XAGUSD       → XAG/USDT
#   * Bypassed PAXG/XAUT: explicit ticker reaches the resolver via the
#     bare-crypto-base path, NOT here. Operator wanting PAXG writes
#     PAXG and we resolve PAXG/USDT.
_CRYPTO_FIRST_REMAP: Dict[str, str] = {
    # Gold
    "GOLD":     "XAU/USDT",
    "XAU/USD":  "XAU/USDT",  # gold CFD → perp
    "BTC/USD":  "BTC/USDT",  # BTC CFD → perp
    "XAU":      "XAU/USDT",
    "XAU/USD":  "XAU/USDT",
    "XAUUSD":   "XAU/USDT",
    # Silver
    "SILVER":   "XAG/USDT",
    "XAG":      "XAG/USDT",
    "XAG/USD":  "XAG/USDT",
    "XAGUSD":   "XAG/USDT",
    # S&P 500 (tokenised perp on bybit/bitget/blofin)
    "SPX":      "SPX/USDT",
    "SP500":    "SPX/USDT",
    "US500":    "SPX/USDT",
    # Nasdaq 100 → QQQ ETF token (tokenised perp on bybit/bitget/blofin)
    "NAS100":   "QQQ/USDT",
    "NASDAQ":   "QQQ/USDT",
    "NDX":      "QQQ/USDT",
    "NQ":       "QQQ/USDT",
    "US100":    "QQQ/USDT",
    "QQQ":      "QQQ/USDT",
    # Crypto aliases — TradingView/full-name → exchange ticker
    "BITTENSOR/USDT": "TAO/USDT",
    "ZCASH/USDT":     "ZEC/USDT",
    "BEAMX/USDT":     "BEAM/USDT",
    # Index remaps — also with /USDT suffix produced by the cleanup block
    "NQ/USDT":         "QQQ/USDT",
    "NAS100/USDT":     "QQQ/USDT",
    "US100/USDT":      "QQQ/USDT",
    # US30 (Dow Jones) has no tokenized perp — leave for capital.com routing
    # Tokenised stocks (NVDA, MSTR, TSLA, AAPL etc.) reach the resolver
    # by their bare ticker via the bare-crypto-base path. Aliases here
    # are reserved for cross-asset-class translations.
}

# Bases that we know are "definitely crypto" and should default to USDT
# when:
#   (a) the trader writes the base alone (``BTC``, ``ETH``, ``PAXG``)
#   (b) the trader writes ``base/USD`` or ``baseUSD`` (the Capital.com
#       form — we redirect to the crypto perp instead)
#
# This list intentionally does NOT include forex bases (EUR, USD, GBP)
# or commodity bases (XAU, XAG) — those have explicit aliases above.
# Tokenised stocks are deliberately included because crypto exchanges
# offer them as perps too (verified in unified_instruments).
_CRYPTO_BASES: FrozenSet[str] = frozenset({
    # Majors
    "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "AVAX", "DOT", "LINK",
    "LTC", "BCH", "MATIC", "POL", "NEAR", "ATOM", "TRX", "TON", "SUI",
    "APT", "ARB", "OP", "FIL", "ICP", "ETC", "ALGO", "VET", "EGLD",
    "HBAR", "INJ", "TAO", "RNDR", "RENDER", "GRT", "AAVE", "MKR",
    "SNX", "CRV", "COMP", "UNI", "1INCH", "DYDX",
    # Meme / new
    "DOGE", "SHIB", "PEPE", "FLOKI", "BONK", "WIF", "TURBO",
    # Layer-2 / new chains
    "STRK", "JUP", "JTO", "PYTH", "JST", "ENA", "ETHFI", "EIGEN",
    # AI tokens
    "FET", "AGIX", "OCEAN", "WLD", "AKT",
    # Gold-backed tokens (treated as crypto for routing purposes —
    # they're TRC20/ERC20 tokens, not synthetic perps)
    "PAXG", "XAUT",
    # Tokenised stocks on bybit/bitget/blofin
    "TSLA", "AAPL", "NVDA", "MSTR", "COIN", "META", "GOOGL", "MSFT",
    "AMZN", "AMD", "INTC", "PLTR", "PYPL", "SHOP", "DIS", "BABA",
    "IONQ",
    # Tokenised indices / commodity tokens — bare ticker resolves to
    # /USDT perp (e.g. trader writes "QQQ" → QQQ/USDT:USDT)
    "QQQ", "SPX", "XAU", "XAG",
    # Commonly seen but specifically scoped
    "KAS", "BTT", "TRUMP", "FARTCOIN",
})


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RoutedMarket:
    """Result of a single ``resolve_market`` call.

    Attributes:
        supported: True if a downstream caller can fetch candles / quote
            for this routing. False means callers should mark the
            position unsupported. **Round 13:** Capital-only matches are
            ``supported=False`` with reason ``capital_only_parked``.
        exchange: ``unified_instruments.exchange`` value when supported.
            One of: ``bybit``, ``bitget``, ``blofin``. Always None when
            ``supported=False``. (Capital.com is intentionally never
            returned in the ``exchange`` field while parked.)
        exchange_symbol: The exact ``unified_instruments.exchange_symbol``
            string the CCXT adapter expects (e.g. ``BTC/USDT:USDT``).
        asset_class: ``crypto`` for every supported route in Round 13.
            Reserved for future re-enable of CFD routing.
        epic_code: For Capital.com matches (even when parked), the epic
            string we WOULD send. Useful for future re-enable. None for
            crypto routes.
        ccxt_perp_symbol: Same as ``exchange_symbol`` for crypto routes
            in Round 13 (we always return the perp form). Kept as a
            distinct field for back-compat with Round 12 callers.
        canonical_symbol: The symbol form to persist on
            ``tracked_positions.instrument_symbol``. **Round 13:** always
            the perp swap form (``BTC/USDT:USDT``) for crypto routes.
            Dashboard renderers convert to ``BTCUSDT.P`` for display.
        unsupported_reason: One of UNSUPPORTED_REASONS when
            supported=False.
        raw_input: The trader's original symbol string, untouched.
            Audit-only.
    """

    supported: bool
    exchange: Optional[str] = None
    exchange_symbol: Optional[str] = None
    asset_class: Optional[str] = None
    epic_code: Optional[str] = None
    ccxt_perp_symbol: Optional[str] = None
    canonical_symbol: Optional[str] = None
    unsupported_reason: Optional[str] = None
    raw_input: str = ""


# ---------------------------------------------------------------------------
# In-process cache
# ---------------------------------------------------------------------------
_CACHE: Dict[str, Tuple[float, RoutedMarket]] = {}
_CACHE_TTL_S: float = 300.0  # 5 minutes


def _cache_get(key: str) -> Optional[RoutedMarket]:
    """Return a cached result if still within TTL, else None."""
    hit = _CACHE.get(key)
    if hit is None:
        return None
    inserted_at, result = hit
    if (time.monotonic() - inserted_at) > _CACHE_TTL_S:
        _CACHE.pop(key, None)
        return None
    return result


def _cache_put(key: str, result: RoutedMarket) -> None:
    """Insert a result into the in-process cache."""
    _CACHE[key] = (time.monotonic(), result)


def clear_cache() -> None:
    """Drop all cached resolutions. Tests + reconciler use this."""
    _CACHE.clear()


# ---------------------------------------------------------------------------
# Pre-flight classifiers
# ---------------------------------------------------------------------------
def _is_aggregate(sym: str) -> bool:
    """True if ``sym`` is a TradingView aggregate / dominance index."""
    return sym.upper() in _AGGREGATE_SYMBOLS


def _is_malformed(sym: str) -> bool:
    """True if ``sym`` matches a known-malformed pattern (zero/single-char
    base before ``USDT.P``, etc.). Round 13 tightened — see comment on
    ``_MALFORMED_PATTERNS``."""
    return any(p.match(sym) for p in _MALFORMED_PATTERNS)


def _apply_crypto_first_remap(sym: str) -> str:
    """Round 13: redirect CFD-style names to the crypto-tokenised perp.

    Inputs are matched on uppercased form. If no remap applies the input
    is returned unchanged (preserving the trader's original casing).
    """
    upper = sym.upper()
    return _CRYPTO_FIRST_REMAP.get(upper, sym)


def _to_perp_swap_form(canonical: str) -> str:
    """Convert a spot canonical (``BTC/USDT``) to the CCXT perp swap form
    (``BTC/USDT:USDT``).

    Idempotent: ``BTC/USDT:USDT`` returns unchanged. Inputs without a
    slash (bare bases) are returned unchanged — caller is expected to
    have applied the bare-base default-quote rule first.
    """
    if "/" not in canonical or ":" in canonical:
        return canonical
    quote = canonical.split("/", 1)[1]
    return f"{canonical}:{quote}"


def _maybe_default_quote(canonical: str) -> str:
    """If ``canonical`` is a bare crypto base, append ``/USDT``.

    Examples:
        BTC          -> BTC/USDT
        PAXG         -> PAXG/USDT
        BTC/USD      -> BTC/USDT (also handled here because ``USD``
                                  appended to a known crypto base
                                  shouldn't have happened)
        EUR          -> EUR (not in _CRYPTO_BASES, untouched)
        BTC/USDT     -> BTC/USDT (already has a quote)
    """
    if "/" in canonical:
        # Already has a quote. Apply USD→USDT override for known crypto
        # bases so trader writing "BTC/USD" (Capital style) reaches the
        # crypto perp.
        base, quote = canonical.split("/", 1)
        if quote == "USD" and base in _CRYPTO_BASES:
            return f"{base}/USDT"
        return canonical

    # Bare base → default to USDT iff in known crypto set.
    if canonical in _CRYPTO_BASES:
        return f"{canonical}/USDT"
    return canonical


# ---------------------------------------------------------------------------
# unified_instruments lookup
# ---------------------------------------------------------------------------
async def _lookup_unified(
    perp_form: str,
    spot_form: str,
    bare: str,
    hint_exchange: Optional[str],
) -> Optional[Dict[str, str]]:
    """Find a matching ``unified_instruments`` row.

    Round 13 lookup strategy (refines Round 12):
      1. Match ``exchange_symbol = perp_form`` (the CCXT swap form like
         ``BTC/USDT:USDT``). This is what the perps actually live as.
      2. Failing that, match the spot form (``BTC/USDT``).
      3. Failing that, the bare uppercased form (``BTCUSDT``).

    Crypto-exchange priority via ``ORDER BY exchange``: bybit > bitget >
    blofin > capital.com. Capital.com is INCLUDED in the lookup (we
    surface it via ``epic_code`` for the future re-enable) but the
    caller filters it out into ``unsupported_reason='capital_only_parked'``.

    Args:
        perp_form: CCXT swap form (``BTC/USDT:USDT``).
        spot_form: Slash form (``BTC/USDT``).
        bare: Uppercased / unspaced fallback (``BTCUSDT``, ``USDJPY``).
        hint_exchange: Optional preferred exchange.

    Returns:
        Dict with keys exchange, exchange_symbol, canonical_symbol,
        asset_type — or None.
    """
    pool = await get_shared_pool()
    rows = await pool.fetch_all(
        """
        SELECT exchange, exchange_symbol, canonical_symbol, asset_type
        FROM public.unified_instruments
        WHERE is_active = TRUE
          AND (
                exchange_symbol  = $1
             OR exchange_symbol  = $2
             OR exchange_symbol  = $3
             OR canonical_symbol = $1
             OR canonical_symbol = $2
             OR canonical_symbol = $3
              )
        ORDER BY
            CASE WHEN exchange = $4 THEN 0 ELSE 1 END,
            CASE exchange
                WHEN 'bybit'       THEN 1
                WHEN 'bitget'      THEN 2
                WHEN 'blofin'      THEN 3
                WHEN 'capital.com' THEN 4
                ELSE 5
            END,
            -- Within a venue, prefer the perp form over spot when
            -- both happen to match. The perp form has a colon.
            CASE WHEN exchange_symbol LIKE '%:%' THEN 0 ELSE 1 END
        LIMIT 1
        """,
        (perp_form, spot_form, bare, hint_exchange or ""),
    )
    if not rows:
        return None
    r = rows[0]
    return {
        "exchange": r["exchange"],
        "exchange_symbol": r["exchange_symbol"],
        "canonical_symbol": r["canonical_symbol"],
        "asset_type": r["asset_type"],
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
async def resolve_market(
    symbol: str,
    hint_exchange: Optional[str] = None,
) -> RoutedMarket:
    """Resolve a raw symbol into a routable ``RoutedMarket``.

    Round 13 flow:
      1. Empty-string defence
      2. Aggregate / malformed pre-flight (return supported=False)
      3. Crypto-first remap (GOLD → XAU/USDT, SPX → SPX/USDT, ...)
      4. Normalise via ``instrument_normaliser`` (BTC-USDT → BTC/USDT)
      5. Strip a stray ``-P`` perp tag from the normaliser output
      6. Bare crypto base default-quote (BTC → BTC/USDT) + USD→USDT
         override for known crypto bases
      7. Convert spot canonical to CCXT perp swap form (BTC/USDT →
         BTC/USDT:USDT)
      8. unified_instruments lookup (perp form first, then spot, then bare)
      9. Result handling:
         - Crypto match → supported, persist perp form
         - Capital match → supported=False, capital_only_parked
         - No match + USDT-pattern → bybit-perp synthesis fallback
         - Else → not_in_unified_instruments

    Args:
        symbol: Raw trader-signal text (e.g. ``GOLD``, ``btc``,
            ``COTI/USDT``, ``BTCUSDT.P``).
        hint_exchange: Optional preferred exchange (e.g. trader's
            historical default).

    Returns:
        RoutedMarket. Always returns — never raises. Callers branch on
        ``.supported`` and use the rest of the fields when True.

    Caching:
        Results cached on ``(symbol, hint_exchange)`` for 5 minutes.
        Tests must call ``clear_cache()`` between resolution attempts
        when seeding different unified_instruments state.
    """
    # ---- 1. defence ---------------------------------------------------
    if not isinstance(symbol, str) or not symbol.strip():
        logger.debug("resolve_market(empty/whitespace) -> unsupported")
        return RoutedMarket(
            supported=False,
            unsupported_reason=UNSUPPORTED_REASONS.EMPTY,
            raw_input=str(symbol) if symbol else "",
        )

    raw = symbol.strip()
    cache_key = f"{raw.upper()}|{(hint_exchange or '').lower()}"
    hit = _cache_get(cache_key)
    if hit is not None:
        return hit

    # ---- 2. pre-flight ------------------------------------------------
    if _is_aggregate(raw):
        result = RoutedMarket(
            supported=False,
            unsupported_reason=UNSUPPORTED_REASONS.AGGREGATE,
            raw_input=raw,
        )
        _cache_put(cache_key, result)
        logger.info("resolve_market(symbol=%r) -> unsupported (aggregate)", raw)
        return result

    if _is_malformed(raw):
        result = RoutedMarket(
            supported=False,
            unsupported_reason=UNSUPPORTED_REASONS.MALFORMED,
            raw_input=raw,
        )
        _cache_put(cache_key, result)
        logger.info("resolve_market(symbol=%r) -> unsupported (malformed)", raw)
        return result

    # ---- 3. crypto-first remap ---------------------------------------
    remapped = _apply_crypto_first_remap(raw)

    # ---- 4. normalise -------------------------------------------------
    canonical = to_canonical_symbol(remapped)

    # ---- 5. strip perp tag the normaliser preserved ------------------
    # Trader writes ``BTCUSDT.P`` → normaliser returns ``BTC/USDT-P``.
    # Drop the ``-P`` tag because we always treat crypto-USDT signals as
    # perps anyway.
    if canonical.endswith("-P"):
        canonical = canonical[:-2]

    # ---- 6. default-quote / USD→USDT override -----------------------
    canonical = _maybe_default_quote(canonical)

    # ---- 7. convert to CCXT perp swap form ---------------------------
    perp_form = _to_perp_swap_form(canonical)
    bare = re.sub(r"[\s/\-_:.]", "", canonical.upper())

    # ---- 8. unified_instruments lookup -------------------------------
    try:
        row = await _lookup_unified(perp_form, canonical, bare, hint_exchange)
    except Exception as exc:
        logger.error(
            "resolve_market: unified_instruments lookup failed for %r: %s",
            raw, exc,
        )
        row = None

    # ---- 9. result handling ------------------------------------------
    if row:
        ex = row["exchange"]
        if ex == "capital.com":
            # Capital is parked. Surface as unsupported but keep the
            # epic so the future re-enable can flip a flag without
            # losing data.
            result = RoutedMarket(
                supported=False,
                unsupported_reason=UNSUPPORTED_REASONS.CAPITAL_ONLY,
                exchange=None,
                exchange_symbol=None,
                asset_class="cfd",
                epic_code=row["exchange_symbol"],
                canonical_symbol=row["canonical_symbol"],
                raw_input=raw,
            )
            _cache_put(cache_key, result)
            logger.info(
                "resolve_market(symbol=%r) -> capital-only PARKED "
                "(epic=%s, canonical=%s)",
                raw, row["exchange_symbol"], row["canonical_symbol"],
            )
            return result

        # Crypto match. Persist the perp swap form on canonical_symbol
        # so dashboards and downstream consumers see what we actually
        # track.
        ex_sym = row["exchange_symbol"]
        # If we matched a SPOT row (no colon) but a perp variant exists
        # (the ORDER BY in _lookup_unified prefers perps when present,
        # so this is a defence-in-depth) — synthesise the perp form.
        persist_form = ex_sym if ":" in ex_sym else _to_perp_swap_form(ex_sym)
        result = RoutedMarket(
            supported=True,
            exchange=ex,
            exchange_symbol=persist_form,
            asset_class="crypto",
            epic_code=None,
            ccxt_perp_symbol=persist_form,
            canonical_symbol=persist_form,
            raw_input=raw,
        )
        _cache_put(cache_key, result)
        logger.debug(
            "resolve_market(symbol=%r, hint=%r) -> %s/%s",
            raw, hint_exchange, result.exchange, result.exchange_symbol,
        )
        return result

    # ---- 10. not listed — give up cleanly ------------------------------
    # Symbol was not in unified_instruments and no aliases matched.
    # Return NOT_LISTED so the caller marks it unsupported rather than
    # inventing a fake route that would spam "market not found" errors.
    result = RoutedMarket(
        supported=False,
        unsupported_reason=UNSUPPORTED_REASONS.NOT_LISTED,
        canonical_symbol=canonical,
        raw_input=raw,
    )
    _cache_put(cache_key, result)
    logger.info(
        "resolve_market(symbol=%r, canonical=%r, bare=%r) -> "
        "unsupported (not in unified_instruments, no fallback)",
        raw, canonical, bare,
    )
    return result


# ---------------------------------------------------------------------------
# Convenience: status_reason formatter
# ---------------------------------------------------------------------------
def unsupported_status_reason(routed: RoutedMarket, ts_iso: str) -> str:
    """Build a deterministic ``tracked_positions.status_reason`` string for
    rows we cancel because the symbol can't be routed.

    Format: ``unsupported:<reason>:<utc_iso>``

    This matches the existing ``retro_activated:<utc>`` pattern from
    Round 11 so the dashboard's status_reason parsing stays uniform.

    Args:
        routed: The RoutedMarket returned by resolve_market().
        ts_iso: ISO-8601 UTC timestamp string (e.g. now().isoformat()).
            Caller controls the timestamp so tests are reproducible.

    Returns:
        Status reason string. Never raises.
    """
    reason = routed.unsupported_reason or UNSUPPORTED_REASONS.UNKNOWN
    return f"unsupported:{reason}:{ts_iso}"
