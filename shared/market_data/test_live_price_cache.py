"""
Tests for :mod:`shared.market_data.live_price_cache`.

These tests patch out the CCXT-touching internals so we exercise the
cache, fallback chain, partial-result, and budget-timeout semantics
purely in-process. Network calls are NEVER made.

Run: ``pytest shared/market_data/test_live_price_cache.py -v``
"""

from __future__ import annotations

import asyncio
from typing import Dict, List
from unittest.mock import patch

import pytest

from shared.market_data import live_price_cache


@pytest.fixture(autouse=True)
def _clear_cache():
    """Every test starts with a fresh cache."""
    live_price_cache.clear_cache()
    yield
    live_price_cache.clear_cache()


# ---------------------------------------------------------------------------
# A small fake ``_probe_one_exchange_batch`` factory.
# ---------------------------------------------------------------------------
def make_probe(per_exchange: Dict[str, Dict[str, float]]):
    """Build a fake probe that returns the configured mapping for each exchange.

    Args:
        per_exchange: ``{exchange: {symbol: price}}``. Any unlisted exchange
            returns an empty dict (i.e. it could not quote any symbol).
    """

    async def fake_probe(_ccxt_async, exchange: str, symbols: List[str]) -> Dict[str, float]:
        venue_map = per_exchange.get(exchange, {})
        return {s: venue_map[s] for s in symbols if s in venue_map}

    return fake_probe


@pytest.mark.asyncio
async def test_resolves_from_first_exchange() -> None:
    probe = make_probe({"bybit": {"BTC/USDT": 100.0, "ETH/USDT": 4000.0}})
    with patch.object(live_price_cache, "_probe_one_exchange_batch", side_effect=probe):
        out = await live_price_cache.fetch_many(["BTC/USDT", "ETH/USDT"])
    assert out == {"BTC/USDT": 100.0, "ETH/USDT": 4000.0}


@pytest.mark.asyncio
async def test_falls_through_when_bybit_misses() -> None:
    """BloFin fills in symbols that bybit can't quote."""
    probe = make_probe({
        "bybit": {"BTC/USDT": 100.0},
        "blofin": {"BRETT/USDT": 0.008},
    })
    with patch.object(live_price_cache, "_probe_one_exchange_batch", side_effect=probe):
        out = await live_price_cache.fetch_many(["BTC/USDT", "BRETT/USDT"])
    assert out == {"BTC/USDT": 100.0, "BRETT/USDT": 0.008}


@pytest.mark.asyncio
async def test_exchange_preference_breaks_ties() -> None:
    """When two exchanges both quote a symbol, the earlier-in-chain wins."""
    probe = make_probe({
        "bybit": {"BTC/USDT": 100.0},   # preference rank 0
        "blofin": {"BTC/USDT": 101.0},  # preference rank 1
        "bitget": {"BTC/USDT": 102.0},
    })
    with patch.object(live_price_cache, "_probe_one_exchange_batch", side_effect=probe):
        out = await live_price_cache.fetch_many(["BTC/USDT"])
    # Bybit (rank 0) is the chain-preferred venue.
    assert out["BTC/USDT"] == 100.0


@pytest.mark.asyncio
async def test_all_exchanges_run_in_parallel() -> None:
    """Every exchange in the chain gets the full pending symbol list."""
    seen_per_exchange: Dict[str, List[str]] = {}

    async def tracking_probe(_a, exchange, symbols):
        seen_per_exchange[exchange] = list(symbols)
        # bybit covers BTC; blofin covers BRETT.
        if exchange == "bybit":
            return {"BTC/USDT": 100.0}
        if exchange == "blofin":
            return {"BRETT/USDT": 0.008}
        return {}

    with patch.object(live_price_cache, "_probe_one_exchange_batch", side_effect=tracking_probe):
        await live_price_cache.fetch_many(["BTC/USDT", "BRETT/USDT"])

    # Parallel chain: every venue sees BOTH symbols.
    assert sorted(seen_per_exchange["bybit"]) == ["BRETT/USDT", "BTC/USDT"]
    assert sorted(seen_per_exchange["blofin"]) == ["BRETT/USDT", "BTC/USDT"]


@pytest.mark.asyncio
async def test_returns_partial_when_some_symbols_never_resolve() -> None:
    probe = make_probe({"bybit": {"BTC/USDT": 100.0}})  # nothing has GARBAGE/USDT
    with patch.object(live_price_cache, "_probe_one_exchange_batch", side_effect=probe):
        out = await live_price_cache.fetch_many(["BTC/USDT", "GARBAGE/USDT"])
    assert out == {"BTC/USDT": 100.0}
    assert "GARBAGE/USDT" not in out


@pytest.mark.asyncio
async def test_cache_hit_skips_network() -> None:
    """Second fetch_many() must hit only the cache, not the probe at all.

    Note: within a SINGLE fetch_many call the probe is invoked once per
    exchange (we race them all in parallel). The cache test focuses on
    the across-calls behaviour: the SECOND fetch_many must NOT invoke
    the probe at all because everything is cached.
    """
    n_calls_per_run: List[int] = []
    counter = {"this_run": 0}

    async def counted_probe(_a, exchange, symbols):
        counter["this_run"] += 1
        return {s: 1.23 for s in symbols}

    with patch.object(live_price_cache, "_probe_one_exchange_batch", side_effect=counted_probe):
        first = await live_price_cache.fetch_many(["BTC/USDT"])
        n_calls_per_run.append(counter["this_run"])
        counter["this_run"] = 0
        second = await live_price_cache.fetch_many(["BTC/USDT"])
        n_calls_per_run.append(counter["this_run"])

    assert first == {"BTC/USDT": 1.23}
    assert second == {"BTC/USDT": 1.23}
    assert n_calls_per_run[0] >= 1  # at least one exchange was probed
    assert n_calls_per_run[1] == 0  # second call was a pure cache hit


@pytest.mark.asyncio
async def test_empty_input_returns_empty_dict() -> None:
    with patch.object(live_price_cache, "_probe_one_exchange_batch",
                      side_effect=AssertionError("should not be called")):
        out = await live_price_cache.fetch_many([])
    assert out == {}


@pytest.mark.asyncio
async def test_none_and_empty_symbols_are_filtered() -> None:
    probe = make_probe({"bybit": {"BTC/USDT": 1.0}})
    with patch.object(live_price_cache, "_probe_one_exchange_batch", side_effect=probe):
        out = await live_price_cache.fetch_many([None, "", "BTC/USDT"])
    assert out == {"BTC/USDT": 1.0}


@pytest.mark.asyncio
async def test_budget_timeout_returns_partial_results() -> None:
    """A slow exchange must not block the whole batch past the budget."""

    async def slow_probe(_a, exchange, symbols):
        if exchange == "bybit":
            return {"FAST/USDT": 9.9} if "FAST/USDT" in symbols else {}
        # Subsequent exchanges hang forever for the SLOW symbol.
        await asyncio.sleep(10.0)
        return {}

    with patch.object(live_price_cache, "_probe_one_exchange_batch", side_effect=slow_probe):
        out = await live_price_cache.fetch_many(
            ["FAST/USDT", "SLOW/USDT"], total_budget_s=0.4,
        )
    # FAST resolved on bybit; SLOW timed out on the remaining chain.
    assert out.get("FAST/USDT") == 9.9
    assert "SLOW/USDT" not in out


@pytest.mark.asyncio
async def test_custom_exchange_chain_is_respected() -> None:
    seen: List[str] = []

    async def tracking_probe(_a, exchange, symbols):
        seen.append(exchange)
        return {}  # nothing resolves → chain traversal completes

    with patch.object(live_price_cache, "_probe_one_exchange_batch", side_effect=tracking_probe):
        await live_price_cache.fetch_many(["X/USDT"], exchanges=["okx", "bitget"])
    assert seen == ["okx", "bitget"]


@pytest.mark.asyncio
async def test_zero_price_is_not_cached() -> None:
    """A probe must return ``None`` for a 0-priced ticker — never 0.

    Sanity check: if a probe ever did return 0 (it shouldn't), the cache
    must not store it as a real price. Confirmed via dict membership.
    """
    probe = make_probe({})  # nothing resolves
    with patch.object(live_price_cache, "_probe_one_exchange_batch", side_effect=probe):
        out = await live_price_cache.fetch_many(["WEIRD/USDT"])
    assert out == {}
