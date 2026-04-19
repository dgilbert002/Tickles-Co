"""
Resolve free-text asset mentions to instrument candidates.

The resolver matches on three passes:

  1. **Pair form** — ``BTC/USDT``, ``ETH-PERP``, ``XAU/USD``.
     These are unambiguous.
  2. **Ticker form** — ``$BTC``, ``$eth``. Ticker with ``$`` is
     high-confidence.
  3. **Bare word form** — ``BTC pumping``. Matches only if the
     token is in a small curated whitelist to avoid false
     positives on English words like ``MOON`` or common
     abbreviations.

The resolver does not hit Postgres in its hot path — it consumes
a preloaded list of instrument dicts (shape mirrors
``instruments`` table). If no loader is provided, a compiled-in
whitelist of liquid majors (BTC/USDT, ETH/USDT, SOL/USDT, XRP/USDT,
ADA/USDT, BNB/USDT, DOGE/USDT, XAU/USD, EUR/USD, SPX, NDX) is
used. This keeps the pipeline usable on any box, and unit tests
can inject their own loader.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Protocol

from shared.enrichment.schema import EnrichmentResult, EnrichmentStage, SymbolMatch


class InstrumentsLoader(Protocol):
    def load(self) -> List[Dict[str, Any]]: ...


_DEFAULT_INSTRUMENTS: List[Dict[str, Any]] = [
    # crypto majors
    {"symbol": "BTC/USDT", "exchange": "binance", "asset_class": "crypto",
     "base_currency": "BTC", "quote_currency": "USDT", "instrument_id": None,
     "aliases": ["BTC", "XBT", "BITCOIN"]},
    {"symbol": "ETH/USDT", "exchange": "binance", "asset_class": "crypto",
     "base_currency": "ETH", "quote_currency": "USDT", "instrument_id": None,
     "aliases": ["ETH", "ETHER", "ETHEREUM"]},
    {"symbol": "SOL/USDT", "exchange": "binance", "asset_class": "crypto",
     "base_currency": "SOL", "quote_currency": "USDT", "instrument_id": None,
     "aliases": ["SOL", "SOLANA"]},
    {"symbol": "XRP/USDT", "exchange": "binance", "asset_class": "crypto",
     "base_currency": "XRP", "quote_currency": "USDT", "instrument_id": None,
     "aliases": ["XRP", "RIPPLE"]},
    {"symbol": "ADA/USDT", "exchange": "binance", "asset_class": "crypto",
     "base_currency": "ADA", "quote_currency": "USDT", "instrument_id": None,
     "aliases": ["ADA", "CARDANO"]},
    {"symbol": "BNB/USDT", "exchange": "binance", "asset_class": "crypto",
     "base_currency": "BNB", "quote_currency": "USDT", "instrument_id": None,
     "aliases": ["BNB"]},
    {"symbol": "DOGE/USDT", "exchange": "binance", "asset_class": "crypto",
     "base_currency": "DOGE", "quote_currency": "USDT", "instrument_id": None,
     "aliases": ["DOGE", "DOGECOIN"]},
    # commodity / FX / equity index proxies
    {"symbol": "XAU/USD", "exchange": "capital", "asset_class": "commodity",
     "base_currency": "XAU", "quote_currency": "USD", "instrument_id": None,
     "aliases": ["XAU", "GOLD"]},
    {"symbol": "EUR/USD", "exchange": "capital", "asset_class": "fx",
     "base_currency": "EUR", "quote_currency": "USD", "instrument_id": None,
     "aliases": ["EURUSD", "EUR"]},
    {"symbol": "SPX", "exchange": "capital", "asset_class": "index",
     "base_currency": "SPX", "quote_currency": "USD", "instrument_id": None,
     "aliases": ["SPX", "SP500"]},
    {"symbol": "NDX", "exchange": "capital", "asset_class": "index",
     "base_currency": "NDX", "quote_currency": "USD", "instrument_id": None,
     "aliases": ["NDX", "NASDAQ100"]},
]


_PAIR_RE = re.compile(
    r"\b([A-Z]{2,6})\s*[/\-]\s*([A-Z]{2,6})\b"
)
_TICKER_RE = re.compile(r"\$([A-Za-z]{2,6})\b")
_BARE_WORD_RE = re.compile(r"\b([A-Z]{2,6})\b")


class SymbolResolver(EnrichmentStage):
    """Populate ``result.symbols`` with best-effort instrument matches."""

    name_ = "symbol_resolver"

    def __init__(
        self,
        instruments_loader: Optional[InstrumentsLoader] = None,
        instruments: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        if instruments is not None:
            self._instruments: List[Dict[str, Any]] = list(instruments)
        elif instruments_loader is not None:
            self._instruments = list(instruments_loader.load())
        else:
            self._instruments = list(_DEFAULT_INSTRUMENTS)
        self._alias_map = self._build_alias_map(self._instruments)

    @property
    def name(self) -> str:
        return self.name_

    @staticmethod
    def _build_alias_map(instruments: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        alias: Dict[str, List[Dict[str, Any]]] = {}
        for inst in instruments:
            keys = set()
            if inst.get("symbol"):
                keys.add(str(inst["symbol"]).upper())
                keys.add(str(inst["symbol"]).upper().replace("/", "-"))
            base = inst.get("base_currency") or inst.get("base")
            if base:
                keys.add(str(base).upper())
            for a in inst.get("aliases", []) or []:
                keys.add(str(a).upper())
            for k in keys:
                alias.setdefault(k, []).append(inst)
        return alias

    def _match_dict_to_instrument(self, inst: Dict[str, Any], *, match_text: str, confidence: float) -> SymbolMatch:
        return SymbolMatch(
            symbol=str(inst.get("symbol", "")),
            exchange=str(inst.get("exchange", "")),
            asset_class=str(inst.get("asset_class", "")),
            base=inst.get("base_currency") or inst.get("base"),
            quote=inst.get("quote_currency") or inst.get("quote"),
            match_text=match_text,
            confidence=confidence,
            instrument_id=inst.get("instrument_id") or inst.get("id"),
        )

    def process(self, result: EnrichmentResult) -> None:
        text = f"{result.headline}\n{result.content}"
        if not text.strip():
            return

        seen: set[tuple[str, str]] = set()
        matches: List[SymbolMatch] = []

        for m in _PAIR_RE.finditer(text):
            key = f"{m.group(1).upper()}/{m.group(2).upper()}"
            if key in self._alias_map:
                for inst in self._alias_map[key]:
                    sig = (str(inst.get("symbol")), str(inst.get("exchange")))
                    if sig in seen:
                        continue
                    seen.add(sig)
                    matches.append(
                        self._match_dict_to_instrument(
                            inst, match_text=m.group(0), confidence=1.0
                        )
                    )

        for m in _TICKER_RE.finditer(text):
            key = m.group(1).upper()
            if key in self._alias_map:
                for inst in self._alias_map[key]:
                    sig = (str(inst.get("symbol")), str(inst.get("exchange")))
                    if sig in seen:
                        continue
                    seen.add(sig)
                    matches.append(
                        self._match_dict_to_instrument(
                            inst, match_text=m.group(0), confidence=0.9
                        )
                    )

        for m in _BARE_WORD_RE.finditer(text):
            key = m.group(1).upper()
            if key in self._alias_map:
                for inst in self._alias_map[key]:
                    sig = (str(inst.get("symbol")), str(inst.get("exchange")))
                    if sig in seen:
                        continue
                    seen.add(sig)
                    matches.append(
                        self._match_dict_to_instrument(
                            inst, match_text=m.group(0), confidence=0.6
                        )
                    )

        result.symbols.extend(matches)
