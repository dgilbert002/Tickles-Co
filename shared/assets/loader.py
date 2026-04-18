"""
shared.assets.loader — ingester that populates venues / assets / instruments
/ instrument_aliases from every API we have access to.

Adapters wired:
    * ccxt      -> binance, bybit, okx, coinbase, kraken, binanceus
    * capital   -> Capital.com CFDs (via existing shared.connectors.capital_adapter)
    * alpaca    -> US equities + crypto  (stub: Phase 22 wires real client)
    * yfinance  -> FX majors, gold/silver, S&P 500, indices (stub: Phase 22)

Run:
    python -m shared.assets.loader --venue binance --dry-run
    python -m shared.assets.loader --venue all --limit 100

Design:
    * Idempotent. Every upsert uses ON CONFLICT DO UPDATE.
    * Dry-run prints the plan (insert/update counts per table) without touching
      the DB. Safe to run anytime.
    * Each venue's fetcher returns a list of `LoaderInstrument` — the DB writer
      is shared. Phase 22 wraps the crypto fetchers behind a scheduler for
      continuous re-harvest.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger("tickles.assets.loader")


# ---------------------------------------------------------------------------
# Data transfer dataclass: one normalised row shape across every adapter
# ---------------------------------------------------------------------------

@dataclass
class LoaderInstrument:
    """Adapter-agnostic intermediate shape. The writer translates this into
    rows for `assets`, `instruments`, and `instrument_aliases`.
    """
    venue_code: str
    venue_symbol: str
    asset_symbol: str            # UPPER, e.g. 'BTC', 'GOLD', 'AAPL'
    asset_class: str             # matches asset_class_t enum values
    base_currency: Optional[str] = None
    quote_currency: Optional[str] = None
    min_size: Optional[Decimal] = None
    size_increment: Optional[Decimal] = None
    contract_multiplier: Decimal = Decimal("1")
    maker_fee_pct: Optional[Decimal] = None
    taker_fee_pct: Optional[Decimal] = None
    spread_pct: Optional[Decimal] = None
    overnight_funding_long_pct: Optional[Decimal] = None
    overnight_funding_short_pct: Optional[Decimal] = None
    max_leverage: Optional[int] = None
    is_active: bool = True
    aliases: Dict[str, str] = field(default_factory=dict)  # alias_type -> alias_value
    display_name: Optional[str] = None  # for the asset


# ---------------------------------------------------------------------------
# Adapter interface
# ---------------------------------------------------------------------------

class VenueAdapter:
    """Base class for per-venue fetchers. Implementations return LoaderInstrument."""
    code: str = "unknown"

    async def fetch(self, *, limit: Optional[int] = None) -> List[LoaderInstrument]:
        raise NotImplementedError


class CcxtAdapter(VenueAdapter):
    """Fetch markets from any ccxt-supported exchange."""

    def __init__(self, venue_code: str, ccxt_id: Optional[str] = None) -> None:
        self.code = venue_code
        self._ccxt_id = ccxt_id or venue_code

    async def fetch(self, *, limit: Optional[int] = None) -> List[LoaderInstrument]:
        log.info("ccxt fetch venue=%s ccxt_id=%s limit=%s", self.code, self._ccxt_id, limit)
        try:
            import ccxt.async_support as ccxt_async  # deferred import
        except ImportError as exc:
            log.error("ccxt not installed: %s", exc)
            return []
        cls = getattr(ccxt_async, self._ccxt_id, None)
        if cls is None:
            log.error("unknown ccxt exchange id: %s", self._ccxt_id)
            return []
        ex = cls({"enableRateLimit": True})
        try:
            markets = await ex.load_markets()
        except Exception as exc:
            log.exception("load_markets failed for %s: %s", self.code, exc)
            return []
        finally:
            try:
                await ex.close()
            except Exception:
                pass

        out: List[LoaderInstrument] = []
        for venue_symbol, market in markets.items():
            if not market or not market.get("active", True):
                continue
            base = market.get("base") or ""
            quote = market.get("quote") or ""
            if not base:
                continue
            asset_class = "crypto"  # ccxt is crypto-only
            maker = market.get("maker")
            taker = market.get("taker")
            limits = market.get("limits", {}) or {}
            amount = (limits.get("amount") or {})
            precision = market.get("precision", {}) or {}
            out.append(LoaderInstrument(
                venue_code=self.code,
                venue_symbol=venue_symbol,
                asset_symbol=base.upper(),
                asset_class=asset_class,
                base_currency=base,
                quote_currency=quote,
                min_size=_d(amount.get("min")),
                size_increment=_d(precision.get("amount")),
                contract_multiplier=_d(market.get("contractSize")) or Decimal("1"),
                maker_fee_pct=_pct(maker),
                taker_fee_pct=_pct(taker),
                max_leverage=_i(limits.get("leverage", {}).get("max") if isinstance(limits.get("leverage"), dict) else None),
                aliases={
                    "ccxt": venue_symbol,
                    "venue_native": market.get("id") or venue_symbol,
                },
                display_name=base.upper(),
            ))
            if limit is not None and len(out) >= limit:
                break
        log.info("ccxt %s: %d instruments fetched", self.code, len(out))
        return out


class CapitalAdapter(VenueAdapter):
    """Capital.com CFDs. Uses existing shared.connectors.capital_adapter when
    available; otherwise returns an empty list so the loader stays safe to run.
    """
    code = "capital"

    async def fetch(self, *, limit: Optional[int] = None) -> List[LoaderInstrument]:
        log.info("capital fetch limit=%s", limit)
        try:
            from shared.connectors.capital_adapter import CapitalAdapter as CapReader  # deferred
        except Exception as exc:
            log.warning("capital_adapter not available (%s); skipping", exc)
            return []
        reader = CapReader()
        list_markets = getattr(reader, "list_markets", None)
        if list_markets is None:
            log.warning(
                "shared.connectors.capital_adapter.CapitalAdapter has no "
                "list_markets() method yet; Phase 22 will add it. Returning [] for now.",
            )
            return []
        try:
            markets = await list_markets()  # expected shape: list of dicts
        except Exception as exc:
            log.exception("capital list_markets failed: %s", exc)
            return []
        out: List[LoaderInstrument] = []
        for m in markets:
            symbol = m.get("epic") or m.get("symbol")
            if not symbol:
                continue
            inst_type = (m.get("instrumentType") or "").lower()
            asset_class = _capital_class(inst_type)
            asset_symbol = re.sub(r"[^A-Z0-9]", "", symbol.upper()) or symbol.upper()
            out.append(LoaderInstrument(
                venue_code="capital",
                venue_symbol=symbol,
                asset_symbol=asset_symbol,
                asset_class=asset_class,
                min_size=_d(m.get("minSize")),
                size_increment=_d(m.get("sizeIncrement")),
                spread_pct=_pct(m.get("spread")),
                overnight_funding_long_pct=_pct(m.get("overnightFeeLong")),
                overnight_funding_short_pct=_pct(m.get("overnightFeeShort")),
                max_leverage=_i(m.get("maxLeverage")),
                aliases={"venue_native": symbol, "display": m.get("name") or symbol},
                display_name=m.get("name") or symbol,
            ))
            if limit is not None and len(out) >= limit:
                break
        log.info("capital: %d instruments fetched", len(out))
        return out


class AlpacaAdapter(VenueAdapter):
    """Stub until Phase 22 wires the real alpaca-py client."""
    code = "alpaca"

    async def fetch(self, *, limit: Optional[int] = None) -> List[LoaderInstrument]:
        log.info("alpaca fetch stubbed (Phase 22 wires real client); returning []")
        return []


class YFinanceAdapter(VenueAdapter):
    """Seed a curated handful of FX / commodity / index symbols.

    yfinance doesn't need API keys so we can safely ingest a starter set here.
    Phase 22 expands this to a full universe.
    """
    code = "yfinance"

    CURATED: Sequence[Dict[str, str]] = (
        {"venue_symbol": "GC=F",     "asset_symbol": "GOLD",   "asset_class": "commodity", "display": "Gold Futures"},
        {"venue_symbol": "SI=F",     "asset_symbol": "SILVER", "asset_class": "commodity", "display": "Silver Futures"},
        {"venue_symbol": "^GSPC",    "asset_symbol": "SP500",  "asset_class": "index",     "display": "S&P 500 Index"},
        {"venue_symbol": "^NDX",     "asset_symbol": "NDX",    "asset_class": "index",     "display": "NASDAQ 100"},
        {"venue_symbol": "^DJI",     "asset_symbol": "DJI",    "asset_class": "index",     "display": "Dow Jones Industrial"},
        {"venue_symbol": "EURUSD=X", "asset_symbol": "EURUSD", "asset_class": "forex",     "display": "EUR/USD"},
        {"venue_symbol": "GBPUSD=X", "asset_symbol": "GBPUSD", "asset_class": "forex",     "display": "GBP/USD"},
        {"venue_symbol": "USDJPY=X", "asset_symbol": "USDJPY", "asset_class": "forex",     "display": "USD/JPY"},
        {"venue_symbol": "CL=F",     "asset_symbol": "WTI",    "asset_class": "commodity", "display": "WTI Crude Oil"},
        {"venue_symbol": "BZ=F",     "asset_symbol": "BRENT",  "asset_class": "commodity", "display": "Brent Crude Oil"},
    )

    async def fetch(self, *, limit: Optional[int] = None) -> List[LoaderInstrument]:
        out: List[LoaderInstrument] = []
        for entry in self.CURATED:
            out.append(LoaderInstrument(
                venue_code="yfinance",
                venue_symbol=entry["venue_symbol"],
                asset_symbol=entry["asset_symbol"],
                asset_class=entry["asset_class"],
                aliases={
                    "venue_native": entry["venue_symbol"],
                    "display": entry["display"],
                    "tradingview": entry["asset_symbol"],
                },
                display_name=entry["display"],
            ))
            if limit is not None and len(out) >= limit:
                break
        log.info("yfinance: %d instruments (curated seed)", len(out))
        return out


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

UPSERT_ASSET_SQL = """
INSERT INTO assets (symbol, display_name, asset_class, auto_seeded)
VALUES ($1, $2, $3, true)
ON CONFLICT (symbol) DO UPDATE SET
    display_name = CASE WHEN assets.auto_seeded THEN EXCLUDED.display_name ELSE assets.display_name END,
    updated_at   = CURRENT_TIMESTAMP
RETURNING id
"""

UPSERT_INSTRUMENT_SQL = """
INSERT INTO instruments (
    symbol, exchange, asset_class, base_currency, quote_currency,
    min_size, size_increment, contract_multiplier,
    spread_pct, maker_fee_pct, taker_fee_pct,
    overnight_funding_long_pct, overnight_funding_short_pct,
    max_leverage, is_active, last_synced_at, asset_id, venue_id
) VALUES (
    $1, $2, $3::asset_class_t, $4, $5,
    $6, $7, $8,
    $9, $10, $11,
    $12, $13,
    $14, $15, CURRENT_TIMESTAMP, $16, $17
)
ON CONFLICT (exchange, symbol) DO UPDATE SET
    base_currency              = EXCLUDED.base_currency,
    quote_currency             = EXCLUDED.quote_currency,
    min_size                   = COALESCE(EXCLUDED.min_size, instruments.min_size),
    size_increment             = COALESCE(EXCLUDED.size_increment, instruments.size_increment),
    contract_multiplier        = EXCLUDED.contract_multiplier,
    spread_pct                 = COALESCE(EXCLUDED.spread_pct, instruments.spread_pct),
    maker_fee_pct              = COALESCE(EXCLUDED.maker_fee_pct, instruments.maker_fee_pct),
    taker_fee_pct              = COALESCE(EXCLUDED.taker_fee_pct, instruments.taker_fee_pct),
    overnight_funding_long_pct = COALESCE(EXCLUDED.overnight_funding_long_pct, instruments.overnight_funding_long_pct),
    overnight_funding_short_pct= COALESCE(EXCLUDED.overnight_funding_short_pct, instruments.overnight_funding_short_pct),
    max_leverage               = COALESCE(EXCLUDED.max_leverage, instruments.max_leverage),
    is_active                  = EXCLUDED.is_active,
    asset_id                   = COALESCE(EXCLUDED.asset_id, instruments.asset_id),
    venue_id                   = COALESCE(EXCLUDED.venue_id, instruments.venue_id),
    last_synced_at             = CURRENT_TIMESTAMP,
    updated_at                 = CURRENT_TIMESTAMP
RETURNING id
"""

UPSERT_ALIAS_SQL = """
INSERT INTO instrument_aliases (instrument_id, alias_type, alias_value, source)
VALUES ($1, $2, $3, 'loader')
ON CONFLICT (alias_type, alias_value, instrument_id) DO NOTHING
"""


class CatalogWriter:
    """Writes LoaderInstrument rows into venues/assets/instruments/aliases.

    The writer caches venue_id and asset_id lookups to save round trips.
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool
        self._venue_id_cache: Dict[str, int] = {}

    async def _venue_id(self, code: str) -> Optional[int]:
        if code in self._venue_id_cache:
            return self._venue_id_cache[code]
        row = await self._pool.fetch_one("SELECT id FROM venues WHERE code=$1", (code,))
        if row is None:
            log.warning("venue code=%s missing from venues table", code)
            return None
        vid = int(row["id"])
        self._venue_id_cache[code] = vid
        return vid

    async def _upsert_asset(self, instrument: LoaderInstrument) -> Optional[int]:
        display = instrument.display_name or instrument.asset_symbol
        row = await self._pool.fetch_one(
            UPSERT_ASSET_SQL,
            (instrument.asset_symbol, display, instrument.asset_class),
        )
        return int(row["id"]) if row else None

    async def upsert(self, instrument: LoaderInstrument) -> Dict[str, Any]:
        venue_id = await self._venue_id(instrument.venue_code)
        asset_id = await self._upsert_asset(instrument)
        inst_row = await self._pool.fetch_one(
            UPSERT_INSTRUMENT_SQL,
            (
                instrument.venue_symbol,
                instrument.venue_code,
                instrument.asset_class,
                instrument.base_currency,
                instrument.quote_currency,
                instrument.min_size,
                instrument.size_increment,
                instrument.contract_multiplier,
                instrument.spread_pct,
                instrument.maker_fee_pct,
                instrument.taker_fee_pct,
                instrument.overnight_funding_long_pct,
                instrument.overnight_funding_short_pct,
                instrument.max_leverage,
                instrument.is_active,
                asset_id,
                venue_id,
            ),
        )
        if inst_row is None:
            return {"instrument_id": None, "asset_id": asset_id, "venue_id": venue_id}
        instrument_id = int(inst_row["id"])
        for alias_type, alias_value in (instrument.aliases or {}).items():
            if not alias_value:
                continue
            await self._pool.fetch_one(
                UPSERT_ALIAS_SQL,
                (instrument_id, alias_type, alias_value),
            )
        return {"instrument_id": instrument_id, "asset_id": asset_id, "venue_id": venue_id}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

ADAPTERS: Dict[str, VenueAdapter] = {
    "binance":   CcxtAdapter("binance", "binance"),
    "binanceus": CcxtAdapter("binanceus", "binanceus"),
    "bybit":     CcxtAdapter("bybit", "bybit"),
    "okx":       CcxtAdapter("okx", "okx"),
    "coinbase":  CcxtAdapter("coinbase", "coinbase"),
    "kraken":    CcxtAdapter("kraken", "kraken"),
    "capital":   CapitalAdapter(),
    "alpaca":    AlpacaAdapter(),
    "yfinance":  YFinanceAdapter(),
}


async def run_load(
    pool: Any,
    venue_codes: Sequence[str],
    *,
    limit: Optional[int] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    totals: Dict[str, Any] = {
        "by_venue": {},
        "total_fetched": 0,
        "total_written": 0,
        "dry_run": dry_run,
    }
    writer = None if dry_run else CatalogWriter(pool)
    for code in venue_codes:
        adapter = ADAPTERS.get(code)
        if adapter is None:
            log.warning("no adapter registered for venue=%s", code)
            totals["by_venue"][code] = {"error": "no_adapter"}
            continue
        try:
            rows = await adapter.fetch(limit=limit)
        except Exception as exc:
            log.exception("adapter fetch failed for %s: %s", code, exc)
            totals["by_venue"][code] = {"error": str(exc)}
            continue
        totals["by_venue"][code] = {"fetched": len(rows), "written": 0}
        totals["total_fetched"] += len(rows)
        if dry_run or writer is None:
            continue
        for inst in rows:
            try:
                await writer.upsert(inst)
                totals["by_venue"][code]["written"] += 1
                totals["total_written"] += 1
            except Exception as exc:
                log.exception("upsert failed for %s/%s: %s", code, inst.venue_symbol, exc)
    return totals


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------

def _d(v: Any) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


def _pct(v: Any) -> Optional[Decimal]:
    """Convert a ratio (0.001) or a percent (0.1 meaning 0.1%) — ccxt uses ratio.

    We store percent (0.001 -> 0.1). Heuristic: if abs(v) <= 1 treat as ratio.
    """
    d = _d(v)
    if d is None:
        return None
    if abs(d) <= Decimal("1"):
        return d * Decimal("100")
    return d


def _i(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except Exception:
        return None


def _capital_class(inst_type: str) -> str:
    mapping = {
        "shares":      "stock",
        "stock":       "stock",
        "equity":      "stock",
        "indices":     "index",
        "index":       "index",
        "currencies":  "forex",
        "forex":       "forex",
        "fx":          "forex",
        "commodities": "commodity",
        "commodity":   "commodity",
        "crypto":      "crypto",
    }
    return mapping.get(inst_type, "cfd")


# ---------------------------------------------------------------------------
# CLI entry point (also reused by shared.cli.assets_cli)
# ---------------------------------------------------------------------------

def _argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="shared.assets.loader",
        description="Populate venues/assets/instruments/aliases from every adapter.",
    )
    p.add_argument("--venue", default="all",
                   help='comma-separated venue codes, or "all" (default: all)')
    p.add_argument("--limit", type=int, default=None,
                   help="max instruments per venue (for smoke tests)")
    p.add_argument("--dry-run", action="store_true",
                   help="fetch only; no DB writes")
    p.add_argument("--verbose", "-v", action="store_true")
    return p


def _venue_codes(raw: str) -> List[str]:
    if raw == "all":
        return list(ADAPTERS.keys())
    return [c.strip() for c in raw.split(",") if c.strip()]


async def _amain(ns: argparse.Namespace) -> int:
    codes = _venue_codes(ns.venue)
    if ns.dry_run:
        pool = None
        totals = await run_load(pool, codes, limit=ns.limit, dry_run=True)
    else:
        from shared.utils.db import get_shared_pool
        pool = await get_shared_pool()
        totals = await run_load(pool, codes, limit=ns.limit, dry_run=False)
    sys.stdout.write(json.dumps(totals, sort_keys=True, default=str, indent=2) + "\n")
    return 0


def main() -> int:
    ns = _argparser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if ns.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    return asyncio.run(_amain(ns))


if __name__ == "__main__":
    raise SystemExit(main())
