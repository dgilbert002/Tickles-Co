"""
shared.assets — Universal Asset Catalog (Phase 14).

Purpose
-------
Give the rest of the platform one place to ask:

    "What is BTC?"                         -> Asset(id=1, symbol='BTC', class='crypto', ...)
    "Where can we trade BTC?"              -> [VenueAssetRow(binance, spot), (binance, perp), (bybit, perp), ...]
    "Resolve 'BTCUSDT' on binance"         -> InstrumentRef(id=42, asset=BTC, venue=binance, ...)
    "Give me live fee/spread for BTC"      -> VenueAssetRow with maker/taker + spread

Three layers:

    venues     -- 'binance', 'bybit', 'capital', 'alpaca', 'yfinance', ...
    assets     -- 'BTC', 'ETH', 'GOLD', 'SP500', 'EUR', 'AAPL', ...
    instruments (existing) -- venue x symbol x contract specs, joined to both

Design notes
------------
* Additive schema only — migration 2026_04_10_phase14 does NOT drop anything.
  `candles.instrument_id` joins keep working; the new FKs are nullable.
* `alias_of_id` on `assets` is a soft-dedup pointer. If the LLM reviewer or
  Dean notices that "BTC" and "XBT" both exist, the duplicate is pointed at
  the canonical row; nothing is deleted.
* Arbitrage-aware: venues carry a `priority` (smaller = preferred) and a
  `v_asset_venues` view joins asset<->venue<->instrument fee/spread rows so
  Phase 33 (arbitrage) can read one table.
* LLM-assisted curation path: `curation_notes` + `auto_seeded=false` lets
  an agent upgrade rows that were guessed by the loader.
"""
from shared.assets.schema import (
    Asset,
    AssetClass,
    InstrumentRef,
    Venue,
    VenueAssetRow,
)
from shared.assets.service import AssetCatalogService

__all__ = [
    "Asset",
    "AssetCatalogService",
    "AssetClass",
    "InstrumentRef",
    "Venue",
    "VenueAssetRow",
]
