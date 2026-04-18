"""
shared.assets.schema — pydantic + dataclass models for the catalog tables.

Pure data: no DB, no I/O, cheap to import from anywhere.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class AssetClass(str, Enum):
    """Mirrors the Postgres `asset_class_t` enum verbatim."""
    CFD = "cfd"
    COMMODITY = "commodity"
    CRYPTO = "crypto"
    FOREX = "forex"
    INDEX = "index"
    STOCK = "stock"


class VenueType(str, Enum):
    CRYPTO_CEX = "crypto_cex"
    CRYPTO_DEX = "crypto_dex"
    BROKER_CFD = "broker_cfd"
    BROKER_EQUITY = "broker_equity"
    DATA_ONLY = "data_only"


class AdapterKind(str, Enum):
    CCXT = "ccxt"
    CAPITAL = "capital"
    ALPACA = "alpaca"
    YFINANCE = "yfinance"


class Venue(BaseModel):
    """One exchange / broker / data-provider row."""
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    code: str
    display_name: str
    venue_type: VenueType
    adapter: AdapterKind
    ccxt_id: Optional[str] = None
    supports_spot: bool = False
    supports_perp: bool = False
    supports_margin: bool = False
    api_base_url: Optional[str] = None
    ws_base_url: Optional[str] = None
    priority: int = 100
    is_active: bool = True
    notes: Optional[Dict[str, Any]] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class Asset(BaseModel):
    """One logical asset (BTC, gold, SP500, AAPL, EUR)."""
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    symbol: str
    display_name: str
    asset_class: AssetClass
    alias_of_id: Optional[int] = None
    auto_seeded: bool = True
    curation_notes: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @property
    def is_canonical(self) -> bool:
        """`alias_of_id IS NULL` means this is a canonical asset."""
        return self.alias_of_id is None


class InstrumentRef(BaseModel):
    """Pointer into `instruments` with enough detail for the catalog APIs."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    exchange: str
    asset_class: AssetClass
    base_currency: Optional[str] = None
    quote_currency: Optional[str] = None
    asset_id: Optional[int] = None
    venue_id: Optional[int] = None
    min_size: Optional[Decimal] = None
    size_increment: Optional[Decimal] = None
    contract_multiplier: Decimal = Field(default=Decimal("1"))
    spread_pct: Optional[Decimal] = None
    maker_fee_pct: Optional[Decimal] = None
    taker_fee_pct: Optional[Decimal] = None
    overnight_funding_long_pct: Optional[Decimal] = None
    overnight_funding_short_pct: Optional[Decimal] = None
    max_leverage: Optional[int] = None
    is_active: bool = True


class VenueAssetRow(BaseModel):
    """One row of `v_asset_venues` — asset x venue joined with contract specs."""
    model_config = ConfigDict(from_attributes=True)

    asset_id: int
    asset_symbol: str
    asset_name: str
    asset_class: AssetClass
    venue_id: int
    venue_code: str
    venue_name: str
    venue_type: VenueType
    adapter: AdapterKind
    venue_priority: int
    instrument_id: int
    venue_symbol: str
    min_size: Optional[Decimal] = None
    size_increment: Optional[Decimal] = None
    contract_multiplier: Decimal
    spread_pct: Optional[Decimal] = None
    maker_fee_pct: Optional[Decimal] = None
    taker_fee_pct: Optional[Decimal] = None
    overnight_funding_long_pct: Optional[Decimal] = None
    overnight_funding_short_pct: Optional[Decimal] = None
    max_leverage: Optional[int] = None

    @property
    def total_cost_one_side_pct(self) -> Decimal:
        """Crude cost estimate: spread + taker fee. Used as an arbitrage tiebreak."""
        parts: List[Decimal] = []
        if self.spread_pct is not None:
            parts.append(self.spread_pct)
        if self.taker_fee_pct is not None:
            parts.append(self.taker_fee_pct)
        if not parts:
            return Decimal("0")
        return sum(parts, start=Decimal("0"))
