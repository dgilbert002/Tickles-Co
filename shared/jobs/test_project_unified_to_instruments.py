"""
Tests for ``project_unified_to_instruments``.

Focus is on the pure-Python helpers (_slash_symbol, _exchange_rank,
_map_asset_class, _decide_active_per_symbol) — DB access is exercised
manually via the CLI and via the diagnostic script
(``/opt/tickles/.cursor/debug_signals_delta_v2.py``).

Run: ``pytest shared/jobs/test_project_unified_to_instruments.py -v``
"""

from __future__ import annotations

from shared.jobs.project_unified_to_instruments import (
    _EXCHANGE_PREFERENCE,
    _decide_active_per_symbol,
    _exchange_rank,
    _map_asset_class,
    _slash_symbol,
)


class TestSlashSymbol:
    def test_basic(self) -> None:
        assert _slash_symbol("BTC", "USDT") == "BTC/USDT"

    def test_lowercase_input_uppers_output(self) -> None:
        assert _slash_symbol("btc", "usdt") == "BTC/USDT"

    def test_whitespace_trimmed(self) -> None:
        assert _slash_symbol(" BTC ", "USDT ") == "BTC/USDT"

    def test_missing_base_returns_none(self) -> None:
        assert _slash_symbol(None, "USDT") is None
        assert _slash_symbol("", "USDT") is None

    def test_missing_quote_returns_none(self) -> None:
        assert _slash_symbol("BTC", None) is None
        assert _slash_symbol("BTC", "") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert _slash_symbol("   ", "USDT") is None
        assert _slash_symbol("BTC", "   ") is None


class TestExchangeRank:
    def test_bybit_is_most_preferred(self) -> None:
        # Bybit comes first in _EXCHANGE_PREFERENCE.
        assert _exchange_rank("bybit") == 0

    def test_blofin_second(self) -> None:
        assert _exchange_rank("blofin") == 1

    def test_unknown_exchange_sorts_last(self) -> None:
        assert _exchange_rank("nonsense") > _exchange_rank("capital.com")

    def test_ordering_is_consistent(self) -> None:
        # Pairwise: every venue earlier in the list ranks lower than
        # every venue later in the list.
        ranks = [_exchange_rank(v) for v in _EXCHANGE_PREFERENCE]
        assert ranks == sorted(ranks)


class TestMapAssetClass:
    def test_known_types(self) -> None:
        assert _map_asset_class("crypto") == "crypto"
        assert _map_asset_class("forex") == "forex"
        assert _map_asset_class("commodity") == "commodity"
        assert _map_asset_class("index") == "index"
        assert _map_asset_class("stock") == "stock"

    def test_case_insensitive(self) -> None:
        assert _map_asset_class("CRYPTO") == "crypto"
        assert _map_asset_class("Forex") == "forex"

    def test_unknown_defaults_to_crypto(self) -> None:
        # Unknown types fall back to crypto, which is the safe default
        # for downstream candle-daemon filtering (daemon selects
        # asset_class='crypto' rows for CCXT collection).
        assert _map_asset_class("blah") == "crypto"

    def test_empty_defaults_to_crypto(self) -> None:
        assert _map_asset_class(None) == "crypto"
        assert _map_asset_class("") == "crypto"


class TestDecideActivePerSymbol:
    def test_picks_preferred_exchange_per_symbol(self) -> None:
        projected = [
            {"symbol": "BRETT/USDT", "exchange": "bitget"},
            {"symbol": "BRETT/USDT", "exchange": "blofin"},
            {"symbol": "BRETT/USDT", "exchange": "bybit"},
            {"symbol": "BRETT/USDT", "exchange": "binance"},
        ]
        feed = {"BRETT/USDT"}
        active = _decide_active_per_symbol(projected, feed)
        # Bybit wins (rank 0).
        assert active == {("BRETT/USDT", "bybit")}

    def test_falls_through_when_preferred_missing(self) -> None:
        projected = [
            {"symbol": "RUNE/USDT", "exchange": "blofin"},
            {"symbol": "RUNE/USDT", "exchange": "binance"},
        ]
        feed = {"RUNE/USDT"}
        active = _decide_active_per_symbol(projected, feed)
        # No bybit row → blofin (rank 1) wins.
        assert active == {("RUNE/USDT", "blofin")}

    def test_symbol_not_in_feed_is_skipped(self) -> None:
        projected = [
            {"symbol": "OBSCURE/USDT", "exchange": "bybit"},
        ]
        feed: set = set()
        active = _decide_active_per_symbol(projected, feed)
        assert active == set()

    def test_feed_match_is_case_insensitive_via_upper(self) -> None:
        # Feed contains upper-cased symbols. Projected uses original case.
        projected = [
            {"symbol": "btc/usdt", "exchange": "bybit"},
        ]
        feed = {"BTC/USDT"}  # upper
        active = _decide_active_per_symbol(projected, feed)
        # Implementation uses sym.upper() against the feed set, so this works.
        assert active == {("btc/usdt", "bybit")}

    def test_multiple_symbols_independent(self) -> None:
        projected = [
            {"symbol": "BTC/USDT", "exchange": "bybit"},
            {"symbol": "BTC/USDT", "exchange": "binance"},
            {"symbol": "EUR/USD", "exchange": "capital.com"},
            {"symbol": "EUR/USD", "exchange": "bybit"},  # synthetic
        ]
        feed = {"BTC/USDT", "EUR/USD"}
        active = _decide_active_per_symbol(projected, feed)
        # Both feed symbols should resolve. Bybit wins for both.
        assert active == {("BTC/USDT", "bybit"), ("EUR/USD", "bybit")}
