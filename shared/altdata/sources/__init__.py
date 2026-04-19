"""Built-in alt-data sources (Phase 29)."""
from __future__ import annotations

from shared.altdata.sources.ccxt_funding import CcxtFundingRateSource
from shared.altdata.sources.ccxt_open_interest import CcxtOpenInterestSource
from shared.altdata.sources.manual import ManualAltDataSource
from shared.altdata.sources.static import StaticAltDataSource

__all__ = [
    "CcxtFundingRateSource",
    "CcxtOpenInterestSource",
    "ManualAltDataSource",
    "StaticAltDataSource",
]
