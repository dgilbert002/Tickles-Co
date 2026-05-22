"""
Module: test_live_price
Purpose: Unit tests for :mod:`shared.market_data.live_price`. Exercises the
         pure helpers (``_candidate_symbols``, ``_extract_price``,
         ``_ticker_ts_ms``) and the public :func:`fetch_live_price` entry
         point with a fake CCXT module so no network calls happen.
Location: /opt/tickles/shared/tests/test_live_price.py

Design notes:
  * The fake CCXT class is injected by patching
    ``ccxt.async_support`` and ``ccxt.base.errors`` *before* the call
    site imports them inside :func:`shared.market_data.live_price._probe`.
  * Each fake exchange records the order of ``fetch_ticker`` calls so we
    can assert candidate-symbol fall-through.
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any, Dict, List, Optional

import pytest

from shared.market_data import live_price as lp


# ---------------------------------------------------------------------------
# _candidate_symbols
# ---------------------------------------------------------------------------
def test_candidate_symbols_slash_form_adds_perp() -> None:
    """``BTC/USDT`` should yield itself and a perp form ``BTC/USDT:USDT``."""
    out = lp._candidate_symbols("BTC/USDT")
    assert out[0] == "BTC/USDT"
    assert "BTC/USDT:USDT" in out


def test_candidate_symbols_compact_form_expands() -> None:
    """``BTCUSDT`` should expand to slash + perp variants."""
    out = lp._candidate_symbols("BTCUSDT")
    assert out[0] == "BTCUSDT"
    assert "BTC/USDT" in out
    assert "BTC/USDT:USDT" in out


def test_candidate_symbols_strips_perp_suffix() -> None:
    """``TAOUSDT.P`` should produce a base form without the ``.P`` marker."""
    out = lp._candidate_symbols("TAOUSDT.P")
    assert out[0] == "TAOUSDT.P"
    assert "TAOUSDT" in out
    assert "TAO/USDT" in out
    assert "TAO/USDT:USDT" in out


def test_candidate_symbols_lowercase_input_is_uppercased() -> None:
    """Case-insensitive: lower-case input is normalised to upper-case."""
    out = lp._candidate_symbols("btc/usdt")
    assert out[0] == "BTC/USDT"


def test_candidate_symbols_empty_returns_empty() -> None:
    """Empty input yields an empty list."""
    assert lp._candidate_symbols("") == []


def test_candidate_symbols_dedupes() -> None:
    """No duplicate entries even when the input shape already contains a perp."""
    out = lp._candidate_symbols("BTC/USDT:USDT")
    assert out == list(dict.fromkeys(out))


# ---------------------------------------------------------------------------
# _extract_price
# ---------------------------------------------------------------------------
def test_extract_price_prefers_last() -> None:
    """``last`` wins over ``close`` and bid/ask."""
    assert lp._extract_price({"last": 50000, "close": 49000, "bid": 1, "ask": 2}) == 50000.0


def test_extract_price_falls_back_to_close() -> None:
    """``close`` is used when ``last`` is missing."""
    assert lp._extract_price({"last": None, "close": 49000}) == 49000.0


def test_extract_price_uses_bid_ask_midpoint() -> None:
    """Midpoint of bid and ask is the last resort."""
    assert lp._extract_price({"bid": 100, "ask": 102}) == 101.0


def test_extract_price_returns_none_for_invalid_shape() -> None:
    """Non-dict input returns ``None``."""
    assert lp._extract_price(None) is None
    assert lp._extract_price("garbage") is None
    assert lp._extract_price(42) is None


def test_extract_price_returns_none_when_all_missing() -> None:
    """Missing every price field returns ``None``."""
    assert lp._extract_price({"foo": "bar"}) is None


def test_extract_price_returns_none_for_zero_or_negative() -> None:
    """Zero / negative prices are rejected (treated as missing)."""
    assert lp._extract_price({"last": 0}) is None
    assert lp._extract_price({"last": -5}) is None


def test_extract_price_handles_string_numbers() -> None:
    """String numbers are coerced via ``float()``."""
    assert lp._extract_price({"last": "12345.6"}) == pytest.approx(12345.6)


def test_extract_price_skips_non_numeric_strings() -> None:
    """Non-numeric strings are skipped, falling through to next fields."""
    assert lp._extract_price({"last": "n/a", "close": 50.0}) == 50.0


# ---------------------------------------------------------------------------
# _ticker_ts_ms
# ---------------------------------------------------------------------------
def test_ticker_ts_ms_returns_int() -> None:
    """A valid ``timestamp`` is coerced to ``int``."""
    assert lp._ticker_ts_ms({"timestamp": 1700000000000}) == 1700000000000


def test_ticker_ts_ms_returns_zero_when_missing() -> None:
    """Missing or non-dict input returns ``0``."""
    assert lp._ticker_ts_ms({}) == 0
    assert lp._ticker_ts_ms(None) == 0
    assert lp._ticker_ts_ms({"timestamp": None}) == 0


def test_ticker_ts_ms_returns_zero_for_garbage() -> None:
    """Non-numeric timestamps are treated as ``0``."""
    assert lp._ticker_ts_ms({"timestamp": "not-a-number"}) == 0


# ---------------------------------------------------------------------------
# fetch_live_price — input validation
# ---------------------------------------------------------------------------
async def test_fetch_live_price_rejects_empty_symbol() -> None:
    """Empty symbol must raise :class:`ValueError`."""
    with pytest.raises(ValueError):
        await lp.fetch_live_price("", "binance")


async def test_fetch_live_price_rejects_unsupported_exchange() -> None:
    """Unknown exchange must raise :class:`UnsupportedExchangeError`."""
    with pytest.raises(lp.UnsupportedExchangeError):
        await lp.fetch_live_price("BTC/USDT", "wibble")


# ---------------------------------------------------------------------------
# fetch_live_price — fake CCXT integration
# ---------------------------------------------------------------------------
class _FakeBadSymbol(Exception):
    pass


class _FakeNetworkError(Exception):
    pass


class _FakeExchangeError(Exception):
    pass


def _install_fake_ccxt(
    monkeypatch: pytest.MonkeyPatch,
    exchange_id: str,
    behaviour: Dict[str, Any],
) -> List[str]:
    """Inject a fake ``ccxt.async_support`` class for ``exchange_id``.

    Args:
        monkeypatch: pytest patcher.
        exchange_id: e.g. ``"binance"`` (must be in
            :data:`SUPPORTED_EXCHANGES`).
        behaviour: dict mapping candidate symbol -> response. Each value is
            either a ticker dict (returned), an Exception instance (raised),
            or the special string ``"sleep"`` to trigger a long sleep
            (used for timeout tests).

    Returns:
        A list which records every ``fetch_ticker`` call symbol, in order.
    """
    calls: List[str] = []

    class _FakeClient:
        def __init__(self, _opts: Dict[str, Any]) -> None:
            self._closed = False

        async def fetch_ticker(self, symbol: str) -> Any:
            calls.append(symbol)
            response = behaviour.get(symbol, _FakeBadSymbol(f"no such symbol {symbol!r}"))
            if isinstance(response, BaseException):
                raise response
            if response == "sleep":
                await asyncio.sleep(60)
            return response

        async def close(self) -> None:
            self._closed = True

    fake_async = types.ModuleType("ccxt.async_support")
    setattr(fake_async, exchange_id, _FakeClient)

    fake_errors = types.ModuleType("ccxt.base.errors")
    fake_errors.BadSymbol = _FakeBadSymbol  # type: ignore[attr-defined]
    fake_errors.NetworkError = _FakeNetworkError  # type: ignore[attr-defined]
    fake_errors.ExchangeError = _FakeExchangeError  # type: ignore[attr-defined]

    fake_base = types.ModuleType("ccxt.base")
    fake_base.errors = fake_errors  # type: ignore[attr-defined]

    fake_root = types.ModuleType("ccxt")
    fake_root.async_support = fake_async  # type: ignore[attr-defined]
    fake_root.base = fake_base  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "ccxt", fake_root)
    monkeypatch.setitem(sys.modules, "ccxt.async_support", fake_async)
    monkeypatch.setitem(sys.modules, "ccxt.base", fake_base)
    monkeypatch.setitem(sys.modules, "ccxt.base.errors", fake_errors)
    return calls


async def test_fetch_live_price_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """First candidate hits → result is returned with that symbol form."""
    behaviour: Dict[str, Any] = {
        "BTC/USDT": {"last": 50000.0, "timestamp": 1700000000000},
    }
    _install_fake_ccxt(monkeypatch, "binance", behaviour)
    result = await lp.fetch_live_price("BTC/USDT", "binance")
    assert isinstance(result, lp.LivePriceResult)
    assert result.symbol == "BTC/USDT"
    assert result.price == 50000.0
    assert result.exchange == "binance"
    assert result.ts_ms == 1700000000000


async def test_fetch_live_price_falls_through_bad_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``BadSymbol`` on the first form should advance to the next candidate."""
    behaviour: Dict[str, Any] = {
        "BTCUSDT": _FakeBadSymbol("nope"),
        "BTC/USDT": {"last": 49000.0, "timestamp": 0},
    }
    calls = _install_fake_ccxt(monkeypatch, "binance", behaviour)
    result = await lp.fetch_live_price("BTCUSDT", "binance")
    assert result.symbol == "BTC/USDT"
    assert result.price == 49000.0
    assert calls[:2] == ["BTCUSDT", "BTC/USDT"]


async def test_fetch_live_price_raises_when_all_candidates_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When every candidate raises, :class:`LivePriceError` is propagated."""
    behaviour: Dict[str, Any] = {
        "BTC/USDT": _FakeNetworkError("net down"),
        "BTC/USDT:USDT": _FakeExchangeError("oops"),
    }
    _install_fake_ccxt(monkeypatch, "binance", behaviour)
    with pytest.raises(lp.LivePriceError):
        await lp.fetch_live_price("BTC/USDT", "binance")


async def test_fetch_live_price_raises_when_price_unusable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tickers without a usable price are skipped; final error is LivePriceError."""
    behaviour: Dict[str, Any] = {
        "BTC/USDT": {"last": None, "close": None, "bid": None, "ask": None},
        "BTC/USDT:USDT": {"foo": "bar"},
    }
    _install_fake_ccxt(monkeypatch, "binance", behaviour)
    with pytest.raises(lp.LivePriceError):
        await lp.fetch_live_price("BTC/USDT", "binance")


async def test_fetch_live_price_timeout_is_live_price_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slow probes that exceed ``timeout_s`` raise :class:`LivePriceError`."""
    behaviour: Dict[str, Any] = {
        "BTC/USDT": "sleep",
    }
    _install_fake_ccxt(monkeypatch, "binance", behaviour)
    with pytest.raises(lp.LivePriceError):
        await lp.fetch_live_price("BTC/USDT", "binance", timeout_s=0.05)


async def test_fetch_live_price_htx_aliases_to_huobi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``htx`` is mapped onto the CCXT ``huobi`` class."""
    behaviour: Dict[str, Any] = {
        "BTC/USDT": {"last": 1.0, "timestamp": 1},
    }
    _install_fake_ccxt(monkeypatch, "huobi", behaviour)
    result = await lp.fetch_live_price("BTC/USDT", "htx")
    # The user-facing ``exchange`` field echoes the caller's input id.
    assert result.exchange == "htx"


async def test_fetch_live_price_gate_aliases_to_gateio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``gate`` is mapped onto the CCXT ``gateio`` class."""
    behaviour: Dict[str, Any] = {
        "BTC/USDT": {"last": 2.0, "timestamp": 2},
    }
    _install_fake_ccxt(monkeypatch, "gateio", behaviour)
    result = await lp.fetch_live_price("BTC/USDT", "gate")
    assert result.exchange == "gate"
