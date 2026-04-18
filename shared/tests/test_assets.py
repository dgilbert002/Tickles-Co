"""Phase 14 — tests for the Universal Asset Catalog.

Divided into three concerns:
    1. Pydantic schema round-trips + invariants.
    2. AssetCatalogService read paths against a FakePool (no real DB needed).
    3. Loader helpers (_d, _pct, _i, _capital_class) + LoaderInstrument shape.

Integration tests that hit real Postgres live in shared/tests/integration/
(added in Phase 38 when the full test suite is re-authored).
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from shared.assets import (
    Asset,
    AssetCatalogService,
    AssetClass,
    InstrumentRef,
    Venue,
    VenueAssetRow,
)
from shared.assets.loader import (
    CcxtAdapter,
    LoaderInstrument,
    YFinanceAdapter,
    _capital_class,
    _d,
    _i,
    _pct,
)
from shared.assets.schema import AdapterKind, VenueType


# ---------------------------------------------------------------------------
# 1. Schema
# ---------------------------------------------------------------------------

def test_asset_class_enum_values_match_db() -> None:
    """Every value must match the Postgres asset_class_t enum verbatim."""
    assert {ac.value for ac in AssetClass} == {
        "cfd", "commodity", "crypto", "forex", "index", "stock",
    }


def test_asset_is_canonical_property() -> None:
    canonical = Asset(symbol="BTC", display_name="Bitcoin", asset_class=AssetClass.CRYPTO)
    alias = Asset(symbol="XBT", display_name="Bitcoin (alias)", asset_class=AssetClass.CRYPTO, alias_of_id=1)
    assert canonical.is_canonical
    assert not alias.is_canonical


def test_venue_roundtrip() -> None:
    v = Venue(
        code="binance", display_name="Binance",
        venue_type=VenueType.CRYPTO_CEX, adapter=AdapterKind.CCXT,
        ccxt_id="binance", supports_spot=True, supports_perp=True, priority=10,
    )
    dumped = v.model_dump(mode="json")
    assert dumped["venue_type"] == "crypto_cex"
    assert dumped["adapter"] == "ccxt"
    Venue.model_validate(dumped)  # must not raise


def test_venue_asset_row_total_cost() -> None:
    row = VenueAssetRow(
        asset_id=1, asset_symbol="BTC", asset_name="Bitcoin",
        asset_class=AssetClass.CRYPTO,
        venue_id=1, venue_code="binance", venue_name="Binance",
        venue_type=VenueType.CRYPTO_CEX, adapter=AdapterKind.CCXT,
        venue_priority=10,
        instrument_id=42, venue_symbol="BTC/USDT",
        contract_multiplier=Decimal("1"),
        spread_pct=Decimal("0.01"),
        taker_fee_pct=Decimal("0.1"),
    )
    assert row.total_cost_one_side_pct == Decimal("0.11")


def test_venue_asset_row_total_cost_zero_when_missing() -> None:
    row = VenueAssetRow(
        asset_id=1, asset_symbol="BTC", asset_name="Bitcoin",
        asset_class=AssetClass.CRYPTO,
        venue_id=1, venue_code="binance", venue_name="Binance",
        venue_type=VenueType.CRYPTO_CEX, adapter=AdapterKind.CCXT,
        venue_priority=10,
        instrument_id=42, venue_symbol="BTC/USDT",
        contract_multiplier=Decimal("1"),
    )
    assert row.total_cost_one_side_pct == Decimal("0")


# ---------------------------------------------------------------------------
# 2. AssetCatalogService with FakePool
# ---------------------------------------------------------------------------

class FakePool:
    """Records queries and returns canned rows. No SQL parser — tests only
    check that the service issues the right shape of request and returns the
    right pydantic model."""

    def __init__(self, rows: Dict[str, List[Dict[str, Any]]]) -> None:
        self._rows = rows
        self.queries: List[Tuple[str, Optional[Tuple[Any, ...]]]] = []

    def _match_key(self, sql: str) -> str:
        lower = sql.lower()
        if "from v_asset_venues" in lower:
            return "venue_asset"
        if "from instrument_aliases" in lower:
            return "alias_match"
        if "from instruments" in lower:
            return "instruments"
        if "from venues" in lower:
            return "venues"
        if "from assets" in lower:
            return "assets"
        return "stats"

    async def fetch_all(self, sql: str, params: Optional[Sequence[Any]] = None) -> List[Dict[str, Any]]:
        self.queries.append((sql, tuple(params) if params else None))
        return list(self._rows.get(self._match_key(sql), []))

    async def fetch_one(self, sql: str, params: Optional[Sequence[Any]] = None) -> Optional[Dict[str, Any]]:
        self.queries.append((sql, tuple(params) if params else None))
        rows = self._rows.get(self._match_key(sql), [])
        return rows[0] if rows else None


def _venue_row(code: str = "binance") -> Dict[str, Any]:
    return {
        "id": 1, "code": code, "display_name": code.title(),
        "venue_type": "crypto_cex", "adapter": "ccxt", "ccxt_id": code,
        "supports_spot": True, "supports_perp": True, "supports_margin": True,
        "api_base_url": None, "ws_base_url": None, "priority": 10,
        "is_active": True, "notes": None,
        "created_at": None, "updated_at": None,
    }


def _asset_row(symbol: str = "BTC") -> Dict[str, Any]:
    return {
        "id": 1, "symbol": symbol, "display_name": symbol,
        "asset_class": "crypto", "alias_of_id": None,
        "auto_seeded": True, "curation_notes": None, "metadata": None,
        "created_at": None, "updated_at": None,
    }


def _instrument_row() -> Dict[str, Any]:
    return {
        "id": 42, "symbol": "BTC/USDT", "exchange": "binance",
        "asset_class": "crypto", "base_currency": "BTC", "quote_currency": "USDT",
        "asset_id": 1, "venue_id": 1,
        "min_size": Decimal("0.0001"), "size_increment": Decimal("0.00001"),
        "contract_multiplier": Decimal("1"),
        "spread_pct": None, "maker_fee_pct": Decimal("0.01"), "taker_fee_pct": Decimal("0.1"),
        "overnight_funding_long_pct": None, "overnight_funding_short_pct": None,
        "max_leverage": 20, "is_active": True,
    }


def _venue_asset_row() -> Dict[str, Any]:
    return {
        "asset_id": 1, "asset_symbol": "BTC", "asset_name": "Bitcoin",
        "asset_class": "crypto",
        "venue_id": 1, "venue_code": "binance", "venue_name": "Binance",
        "venue_type": "crypto_cex", "adapter": "ccxt", "venue_priority": 10,
        "instrument_id": 42, "venue_symbol": "BTC/USDT",
        "min_size": None, "size_increment": None,
        "contract_multiplier": Decimal("1"),
        "spread_pct": Decimal("0.01"), "maker_fee_pct": Decimal("0.01"),
        "taker_fee_pct": Decimal("0.1"),
        "overnight_funding_long_pct": None, "overnight_funding_short_pct": None,
        "max_leverage": 20,
    }


def test_service_list_venues() -> None:
    pool = FakePool({"venues": [_venue_row(), _venue_row("bybit")]})
    svc = AssetCatalogService(pool)
    venues = asyncio.run(svc.list_venues())
    assert len(venues) == 2
    assert {v.code for v in venues} == {"binance", "bybit"}


def test_service_venue_by_code() -> None:
    pool = FakePool({"venues": [_venue_row()]})
    svc = AssetCatalogService(pool)
    v = asyncio.run(svc.venue_by_code("binance"))
    assert v is not None
    assert v.adapter == AdapterKind.CCXT


def test_service_list_assets_filters() -> None:
    pool = FakePool({"assets": [_asset_row("BTC"), _asset_row("ETH")]})
    svc = AssetCatalogService(pool)
    assets = asyncio.run(svc.list_assets(asset_class=AssetClass.CRYPTO))
    assert [a.symbol for a in assets] == ["BTC", "ETH"]
    # fake pool doesn't enforce the WHERE, but the SQL must include the class filter
    sent_sql = pool.queries[-1][0]
    assert "asset_class=" in sent_sql


def test_service_asset_by_symbol_uppercases() -> None:
    pool = FakePool({"assets": [_asset_row("BTC")]})
    svc = AssetCatalogService(pool)
    asyncio.run(svc.asset_by_symbol("btc"))
    _, params = pool.queries[-1]
    assert params == ("BTC",)


def test_service_resolve_symbol_direct_hit() -> None:
    pool = FakePool({"instruments": [_instrument_row()]})
    svc = AssetCatalogService(pool)
    ref = asyncio.run(svc.resolve_symbol("BTC/USDT", venue_code="binance"))
    assert ref is not None
    assert isinstance(ref, InstrumentRef)
    assert ref.id == 42


def test_service_resolve_symbol_alias_fallback() -> None:
    pool = FakePool({
        "instruments": [],             # no direct hit
        "alias_match": [_instrument_row()],  # alias hit
    })
    svc = AssetCatalogService(pool)
    ref = asyncio.run(svc.resolve_symbol("BTCUSDT"))
    assert ref is not None


def test_service_spread_snapshot_sort_and_delta() -> None:
    cheap = _venue_asset_row()
    cheap["spread_pct"] = Decimal("0.01")
    cheap["taker_fee_pct"] = Decimal("0.05")
    expensive = _venue_asset_row()
    expensive["venue_code"] = "capital"
    expensive["venue_priority"] = 70
    expensive["spread_pct"] = Decimal("0.2")
    expensive["taker_fee_pct"] = Decimal("0.1")

    pool = FakePool({"venue_asset": [cheap, expensive]})
    svc = AssetCatalogService(pool)
    snap = asyncio.run(svc.spread_snapshot("BTC"))
    assert snap["asset"] == "BTC"
    assert snap["cheapest_total_cost_pct"] == Decimal("0.06")
    assert snap["spread_between_cheapest_and_most_expensive_pct"] == Decimal("0.24")


# ---------------------------------------------------------------------------
# 3. Loader helpers + adapters
# ---------------------------------------------------------------------------

def test_d_handles_none_and_bad_inputs() -> None:
    assert _d(None) is None
    assert _d("") is None
    assert _d("abc") is None
    assert _d("0.5") == Decimal("0.5")
    assert _d(0.25) == Decimal("0.25")


def test_pct_converts_ratio_to_percent() -> None:
    assert _pct(0.001) == Decimal("0.1")
    assert _pct(1) == Decimal("100")
    assert _pct(15) == Decimal("15")  # already percent, left alone
    assert _pct(None) is None


def test_i_handles_bad_inputs() -> None:
    assert _i(None) is None
    assert _i("") is None
    assert _i("abc") is None
    assert _i("20") == 20
    assert _i(5.7) == 5


def test_capital_class_mapping() -> None:
    assert _capital_class("shares") == "stock"
    assert _capital_class("currencies") == "forex"
    assert _capital_class("commodities") == "commodity"
    assert _capital_class("indices") == "index"
    assert _capital_class("unknown") == "cfd"


def test_yfinance_adapter_returns_curated_set() -> None:
    adapter = YFinanceAdapter()
    rows = asyncio.run(adapter.fetch())
    symbols = {r.asset_symbol for r in rows}
    assert "GOLD" in symbols
    assert "SP500" in symbols
    assert "EURUSD" in symbols
    for r in rows:
        assert r.venue_code == "yfinance"
        assert r.asset_class in {"commodity", "index", "forex"}
        assert "venue_native" in r.aliases


def test_loader_instrument_defaults() -> None:
    inst = LoaderInstrument(
        venue_code="binance", venue_symbol="BTC/USDT",
        asset_symbol="BTC", asset_class="crypto",
    )
    assert inst.contract_multiplier == Decimal("1")
    assert inst.is_active is True
    assert inst.aliases == {}


def test_ccxt_adapter_handles_missing_package(monkeypatch: pytest.MonkeyPatch) -> None:
    """If ccxt is not installed, the adapter must return [] and log — never crash."""
    import builtins
    real_import = builtins.__import__

    def fake_import(name: str, *a: Any, **kw: Any) -> Any:
        if name == "ccxt.async_support":
            raise ImportError("simulated missing ccxt")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    adapter = CcxtAdapter("binance")
    rows = asyncio.run(adapter.fetch())
    assert rows == []
