"""
Module: test_price_routes
Purpose: Unit + integration tests for :mod:`shared.dashboard.price_routes`.
         Exercises symbol/exchange validation, the in-memory TTL cache, the
         per-bucket rate limiter, and the error mapping for the
         ``GET /api/price`` endpoint.
Location: /opt/tickles/shared/tests/test_price_routes.py

Design notes:
  * The route delegates to
    :func:`shared.market_data.live_price.fetch_live_price`, which is patched
    out per-test so no real network calls are made.
  * Module-level cache and rate-limit state are reset by an autouse fixture
    via the test-only :func:`shared.dashboard.price_routes._reset_state`.
  * The aiohttp app is created fresh per test via
    :class:`aiohttp.test_utils.TestServer` + :class:`TestClient`.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Callable, Optional

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from shared.dashboard import price_routes as pr
from shared.market_data import live_price as lp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_module_state() -> None:
    """Wipe cache + rate-limit buckets between tests."""
    pr._reset_state()
    yield
    pr._reset_state()


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[TestClient]:
    """Yield a :class:`TestClient` with the price route mounted at ``/api/price``."""
    app = web.Application()
    pr.attach_routes(app, prefix="")
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    try:
        yield test_client
    finally:
        await test_client.close()


def _patch_fetch(
    monkeypatch: pytest.MonkeyPatch,
    factory: Callable[[str, str], Any],
) -> None:
    """Patch :func:`fetch_live_price` in :mod:`price_routes` with ``factory``.

    Args:
        monkeypatch: pytest patcher.
        factory: callable taking ``(symbol, exchange)`` and either returning
            a :class:`LivePriceResult` or raising.
    """
    async def _fake(symbol: str, exchange: str = "binance", *, timeout_s: float = 5.0):
        return factory(symbol, exchange)

    monkeypatch.setattr(pr, "fetch_live_price", _fake)


# ---------------------------------------------------------------------------
# _normalise_symbol / _normalise_exchange (pure)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("BTC/USDT", "BTC/USDT"),
    ("btcusdt", "BTCUSDT"),
    ("BTC/USDT:USDT", "BTC/USDT:USDT"),
    ("TAOUSDT.P", "TAOUSDT.P"),
    ("  eth/usdt  ", "ETH/USDT"),
])
def test_normalise_symbol_accepts(raw: str, expected: str) -> None:
    """Common valid forms normalise to upper-case stripped strings."""
    assert pr._normalise_symbol(raw) == expected


@pytest.mark.parametrize("raw", [
    "",
    "   ",
    None,
    "ABC; DROP TABLE",
    "BTC USDT",
    "<script>",
    "A",
    "X" * 40,
    "BTC/USDT/EXTRA",
])
def test_normalise_symbol_rejects(raw: Optional[str]) -> None:
    """Garbage / SQL-ish / over-long inputs return ``None``."""
    assert pr._normalise_symbol(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("binance", "binance"),
    ("BYBIT", "bybit"),
    ("  okx  ", "okx"),
])
def test_normalise_exchange_accepts(raw: str, expected: str) -> None:
    """Supported exchanges are case-insensitive."""
    assert pr._normalise_exchange(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "wibble", "ftx", "DEX"])
def test_normalise_exchange_rejects(raw: Optional[str]) -> None:
    """Unsupported / empty exchange ids return ``None``."""
    assert pr._normalise_exchange(raw) is None


# ---------------------------------------------------------------------------
# Route: validation
# ---------------------------------------------------------------------------
async def test_handle_price_rejects_missing_symbol(client: TestClient) -> None:
    """No ``symbol`` query param -> 400."""
    resp = await client.get("/api/price")
    assert resp.status == 400
    body = await resp.json()
    assert "symbol" in body["error"].lower()


async def test_handle_price_rejects_invalid_symbol(client: TestClient) -> None:
    """A symbol that fails the regex -> 400."""
    resp = await client.get("/api/price", params={"symbol": "ABC; DROP"})
    assert resp.status == 400


async def test_handle_price_rejects_unsupported_exchange(client: TestClient) -> None:
    """Unsupported ``exchange`` -> 400 with a descriptive error."""
    resp = await client.get(
        "/api/price",
        params={"symbol": "BTC/USDT", "exchange": "wibble"},
    )
    assert resp.status == 400
    body = await resp.json()
    assert "wibble" in body["error"]


# ---------------------------------------------------------------------------
# Route: happy path + cache
# ---------------------------------------------------------------------------
async def test_handle_price_happy_path(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful probe returns 200 and a JSON body with ``cached: false``."""
    def _fac(sym: str, exch: str) -> lp.LivePriceResult:
        return lp.LivePriceResult(
            symbol=sym, price=50000.0, exchange=exch, ts_ms=1700000000000,
        )
    _patch_fetch(monkeypatch, _fac)

    resp = await client.get(
        "/api/price",
        params={"symbol": "BTC/USDT", "exchange": "binance"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["symbol"] == "BTC/USDT"
    assert body["price"] == 50000.0
    assert body["exchange"] == "binance"
    assert body["ts"] == 1700000000000
    assert body["cached"] is False


async def test_handle_price_cache_hit_on_second_call(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second call within TTL is served from cache and ``fetch_live_price`` is not called."""
    call_count = {"n": 0}

    def _fac(sym: str, exch: str) -> lp.LivePriceResult:
        call_count["n"] += 1
        return lp.LivePriceResult(
            symbol=sym, price=42.0, exchange=exch, ts_ms=0,
        )
    _patch_fetch(monkeypatch, _fac)

    r1 = await client.get("/api/price", params={"symbol": "ETH/USDT"})
    r2 = await client.get("/api/price", params={"symbol": "ETH/USDT"})
    assert r1.status == 200
    assert r2.status == 200
    b1 = await r1.json()
    b2 = await r2.json()
    assert b1["cached"] is False
    assert b2["cached"] is True
    assert call_count["n"] == 1


async def test_handle_price_default_exchange_is_bybit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``exchange`` is omitted, the route should default to ``bybit``."""
    seen: dict = {}

    def _fac(sym: str, exch: str) -> lp.LivePriceResult:
        seen["exch"] = exch
        return lp.LivePriceResult(symbol=sym, price=1.0, exchange=exch, ts_ms=0)
    _patch_fetch(monkeypatch, _fac)

    resp = await client.get("/api/price", params={"symbol": "BTC/USDT"})
    assert resp.status == 200
    assert seen["exch"] == "bybit"


# ---------------------------------------------------------------------------
# Route: error mapping
# ---------------------------------------------------------------------------
async def test_handle_price_maps_live_price_error_to_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A :class:`LivePriceError` from CCXT becomes HTTP 502."""
    def _fac(sym: str, exch: str) -> lp.LivePriceResult:
        raise lp.LivePriceError("upstream broken")
    _patch_fetch(monkeypatch, _fac)

    resp = await client.get("/api/price", params={"symbol": "BTC/USDT"})
    assert resp.status == 502


async def test_handle_price_maps_timeout_to_504(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An :class:`asyncio.TimeoutError` becomes HTTP 504."""
    def _fac(sym: str, exch: str) -> lp.LivePriceResult:
        raise asyncio.TimeoutError()
    _patch_fetch(monkeypatch, _fac)

    resp = await client.get("/api/price", params={"symbol": "BTC/USDT"})
    assert resp.status == 504


async def test_handle_price_maps_unsupported_exchange_to_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An :class:`UnsupportedExchangeError` from the helper becomes HTTP 400."""
    def _fac(sym: str, exch: str) -> lp.LivePriceResult:
        raise lp.UnsupportedExchangeError("nope")
    _patch_fetch(monkeypatch, _fac)

    resp = await client.get(
        "/api/price",
        params={"symbol": "BTC/USDT", "exchange": "binance"},
    )
    assert resp.status == 400


async def test_handle_price_maps_unexpected_exception_to_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any other exception becomes HTTP 502."""
    def _fac(sym: str, exch: str) -> lp.LivePriceResult:
        raise RuntimeError("unexpected")
    _patch_fetch(monkeypatch, _fac)

    resp = await client.get("/api/price", params={"symbol": "BTC/USDT"})
    assert resp.status == 502


# ---------------------------------------------------------------------------
# Route: rate limiting
# ---------------------------------------------------------------------------
async def test_handle_price_rate_limit_returns_429(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the bucket is full, subsequent calls return 429 with Retry-After."""
    def _fac(sym: str, exch: str) -> lp.LivePriceResult:
        return lp.LivePriceResult(symbol=sym, price=1.0, exchange=exch, ts_ms=0)
    _patch_fetch(monkeypatch, _fac)

    # Burn the bucket. The cache would mask repeats, so vary the symbol.
    quotes = ["USDT", "USDC", "BUSD", "USD", "EUR", "BTC", "ETH"]
    bases = ["BTC", "ETH", "SOL", "XRP", "ADA", "AVAX", "BNB", "MATIC", "DOT", "LINK"]
    symbols = [f"{b}/{q}" for b in bases for q in quotes][: pr._RATE_LIMIT_MAX_REQUESTS + 5]

    statuses = []
    for sym in symbols:
        resp = await client.get("/api/price", params={"symbol": sym})
        statuses.append(resp.status)
        if resp.status == 429:
            assert resp.headers.get("Retry-After") is not None
            await resp.read()
            break
        await resp.read()

    assert 429 in statuses
    # The first N must succeed.
    assert statuses[: pr._RATE_LIMIT_MAX_REQUESTS] == [200] * pr._RATE_LIMIT_MAX_REQUESTS


# ---------------------------------------------------------------------------
# attach_routes mounts at /api/price
# ---------------------------------------------------------------------------
def test_attach_routes_registers_get_price() -> None:
    """``attach_routes`` adds a GET handler at ``/api/price``."""
    app = web.Application()
    pr.attach_routes(app, prefix="")
    paths = {(r.method, str(r.url_for())) for r in app.router.routes()}
    assert ("GET", "/api/price") in paths
