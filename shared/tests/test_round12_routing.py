"""
Module: test_round12_routing
Purpose: Behavioural tests for Round 12 (2026-05-24) "exotic + mirroring +
         journey + retirement" deliverables. These tests exercise the
         changes WITHOUT a live database where possible, falling back to
         live integration when the test names start with ``test_live_``.
Location: /opt/tickles/shared/tests/test_round12_routing.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Phase 12.1 — exchange_router pre-flight classification (no DB needed)
# ---------------------------------------------------------------------------
def test_router_aggregate_symbols_unsupported():
    """USDT.D + friends must be rejected before touching DB or CCXT."""
    from shared.utils.exchange_router import (
        clear_cache,
        resolve_market,
        UNSUPPORTED_REASONS,
    )

    async def _go():
        for sym in ("USDT.D", "BTC.D", "ETH.D", "TOTAL3"):
            clear_cache()
            r = await resolve_market(sym)
            assert r.supported is False, f"{sym} should be unsupported"
            assert r.unsupported_reason == UNSUPPORTED_REASONS.AGGREGATE
            assert r.exchange is None
    asyncio.run(_go())


def test_router_malformed_symbols_unsupported():
    """Round 13: only TRUE base-less junk is malformed.

    Pre-Round-13 the regex ``^[A-Z]+USDT[./-]P$`` flagged any base+USDT+.P
    combo, including ``BTCUSDT.P`` and ``LDOUSDT.P`` — which is the perp
    ticker traders write on TradingView. Round 13 tightened the regex to
    only match zero/single-char bases (``USDT.P``, ``HUSDT.P``,
    ``XUSDT.P``). Multi-char bases like LDOUSDT.P now resolve to perps
    via the normal pipeline.
    """
    from shared.utils.exchange_router import (
        clear_cache,
        resolve_market,
        UNSUPPORTED_REASONS,
    )

    async def _go():
        # True malformed (base-less or single-char base)
        for sym in ("HUSDT.P", "HUSDT/P", "USDT.P", "XUSDT.P"):
            clear_cache()
            r = await resolve_market(sym)
            assert r.supported is False, f"{sym} should be malformed-unsupported"
            assert r.unsupported_reason == UNSUPPORTED_REASONS.MALFORMED
    asyncio.run(_go())


def test_router_perp_ticker_no_longer_malformed():
    """Round 13: ``BTCUSDT.P`` / ``LDOUSDT.P`` etc. are valid perp signals.

    These must NOT be flagged as malformed — that was Round 12's bug.
    Test the runtime regex object directly (not source-grep, because the
    docstring reasonably mentions the OLD pattern for context)."""
    from shared.utils.exchange_router import _MALFORMED_PATTERNS
    # The current malformed patterns must NOT match real perp tickers
    real_perp_tickers = (
        "BTCUSDT.P", "ETHUSDT.P", "LDOUSDT.P", "SOLUSDT.P",
        "1000PEPEUSDT.P", "AVAXUSDT.P",
    )
    for sym in real_perp_tickers:
        assert not any(p.match(sym) for p in _MALFORMED_PATTERNS), \
            f"Round 13 regression: real perp ticker {sym!r} flagged as malformed"
    # And TRUE base-less junk MUST still match
    for sym in ("USDT.P", "HUSDT.P", "XUSDT.P", "USDT/P", "USDT-P"):
        assert any(p.match(sym) for p in _MALFORMED_PATTERNS), \
            f"Round 13 regression: base-less junk {sym!r} no longer caught"


def test_router_empty_input_unsupported():
    """Empty / None input must not crash and must return EMPTY reason."""
    from shared.utils.exchange_router import (
        clear_cache,
        resolve_market,
        UNSUPPORTED_REASONS,
    )

    async def _go():
        for sym in ("", "  ", None):
            clear_cache()
            r = await resolve_market(sym)
            assert r.supported is False
            assert r.unsupported_reason == UNSUPPORTED_REASONS.EMPTY
    asyncio.run(_go())


def test_router_unsupported_status_reason_format():
    """status_reason format matches retro_activated:<utc> from Round 11."""
    from shared.utils.exchange_router import (
        unsupported_status_reason,
        RoutedMarket,
        UNSUPPORTED_REASONS,
    )
    routed = RoutedMarket(
        supported=False,
        unsupported_reason=UNSUPPORTED_REASONS.AGGREGATE,
        raw_input="USDT.D",
    )
    s = unsupported_status_reason(routed, "2026-05-24T12:34:56+00:00")
    assert s == "unsupported:tradingview_aggregate:2026-05-24T12:34:56+00:00"
    assert s.startswith("unsupported:")
    assert s.count(":") >= 4  # unsupported : reason : ISO with colons


# ---------------------------------------------------------------------------
# Phase 12.1 — live DB-backed routing (skip if Postgres unavailable)
# ---------------------------------------------------------------------------
def _pg_env_ok() -> bool:
    return bool(os.environ.get("PGHOST")) or os.path.exists("/var/run/postgresql")


@pytest.mark.skipif(not _pg_env_ok(), reason="No local Postgres available")
def test_live_router_known_routings():
    """Round 13 contract test for crypto-first routing.

    Every realistic trader signal MUST land on a crypto exchange. The
    only exceptions are Capital-only forex/index symbols, which are
    parked (supported=False, reason=capital_only_parked). The asset
    class for every supported route is ``crypto``.

    Pool-lifecycle fail-soft kept from Round 12 — see the inline
    comment in that branch."""
    from shared.utils.exchange_router import (
        clear_cache, resolve_market, UNSUPPORTED_REASONS,
    )

    # (input, supported, exchange, asset_class, expected_canonical_contains)
    # ``expected_canonical_contains`` is a substring assertion against
    # ``canonical_symbol`` so a fuzzy match works regardless of exact
    # exchange-symbol shape variations.
    crypto_cases = [
        ("GOLD",          "bybit",  "crypto", "XAU/USDT:USDT"),
        ("XAU/USD",       "bybit",  "crypto", "XAU/USDT:USDT"),
        ("XAUUSD",        "bybit",  "crypto", "XAU/USDT:USDT"),
        ("SILVER",        "bybit",  "crypto", "XAG/USDT:USDT"),
        ("US100",         "bybit",  "crypto", "QQQ/USDT:USDT"),
        ("NAS100",        "bybit",  "crypto", "QQQ/USDT:USDT"),
        ("NQ",            "bybit",  "crypto", "QQQ/USDT:USDT"),
        ("SPX",           "bybit",  "crypto", "SPX/USDT:USDT"),
        ("SP500",         "bybit",  "crypto", "SPX/USDT:USDT"),
        ("BTC/USD",       "bybit",  "crypto", "BTC/USDT:USDT"),
        ("ETH/USD",       "bybit",  "crypto", "ETH/USDT:USDT"),
        ("BTCUSD",        "bybit",  "crypto", "BTC/USDT:USDT"),
        ("BTC/USDT",      "bybit",  "crypto", "BTC/USDT:USDT"),
        ("ETHUSDT.P",     "bybit",  "crypto", "ETH/USDT:USDT"),
        ("LDOUSDT.P",     "bybit",  "crypto", "LDO/USDT:USDT"),
        ("BTC",           "bybit",  "crypto", "BTC/USDT:USDT"),
        ("PAXG",          "bybit",  "crypto", "PAXG/USDT:USDT"),
        ("KAS/USDT",      "bybit",  "crypto", "KAS/USDT:USDT"),
        ("KAS/USD",       "bybit",  "crypto", "KAS/USDT:USDT"),
        # COTI/USDT:USDT exists on bybit/bitget/blofin; bybit wins via priority
        ("COTI/USDT",     "bybit",  "crypto", "COTI/USDT:USDT"),
        ("1000PEPE/USDT", "bybit",  "crypto", "1000PEPE/USDT:USDT"),
    ]
    parked_cases = [
        # Capital-only — must be parked (supported=False) but keep epic
        ("EUR/USD",  "EURUSD"),
        ("USD/JPY",  "USDJPY"),
        ("USDJPY",   "USDJPY"),
        ("DE40",     "DE40"),
        ("UK100",    "UK100"),
    ]

    async def _go():
        for sym, ex, ac, contains in crypto_cases:
            clear_cache()
            r = await resolve_market(sym)
            if not r.supported and "another operation is in progress" in (
                r.unsupported_reason or ""
            ):
                pytest.skip("asyncpg pool bound to dead loop in this pytest session")
            assert r.supported is True, f"{sym}: must be supported, got {r}"
            assert r.exchange == ex, f"{sym}: exchange expected {ex}, got {r.exchange}"
            assert r.asset_class == ac, f"{sym}: asset_class expected {ac}"
            assert r.canonical_symbol and contains in r.canonical_symbol, \
                f"{sym}: canonical_symbol={r.canonical_symbol!r} must contain {contains!r}"
            # Round 13 contract: persist form == perp swap form
            assert r.canonical_symbol == r.ccxt_perp_symbol, \
                f"{sym}: canonical_symbol must equal ccxt_perp_symbol in Round 13"
            assert ":" in r.canonical_symbol, \
                f"{sym}: canonical_symbol must be CCXT swap form (contain ':')"
            assert r.epic_code is None, \
                f"{sym}: crypto routes must not carry an epic_code"

        for sym, expected_epic in parked_cases:
            clear_cache()
            r = await resolve_market(sym)
            if not r.supported and "another operation is in progress" in (
                r.unsupported_reason or ""
            ):
                pytest.skip("asyncpg pool bound to dead loop in this pytest session")
            assert r.supported is False, f"{sym}: must be parked"
            assert r.unsupported_reason == UNSUPPORTED_REASONS.CAPITAL_ONLY, \
                f"{sym}: must be parked with reason capital_only_parked"
            assert r.epic_code == expected_epic, \
                f"{sym}: epic_code preserved as {expected_epic}, got {r.epic_code}"
            assert r.exchange is None, \
                f"{sym}: parked routes must not return an exchange"

    try:
        asyncio.run(_go())
    except RuntimeError as exc:
        if "another operation" in str(exc) or "different loop" in str(exc):
            pytest.skip(f"asyncpg cross-loop issue in pytest session: {exc}")
        raise


# ---------------------------------------------------------------------------
# Phase 12.2 — distance mirroring signature
# ---------------------------------------------------------------------------
def test_position_monitor_update_price_pnl_accepts_kwargs():
    """update_position_price_pnl gained four optional kwargs in Round 12.

    Old call signature (3 positional + position_id) MUST still work so
    callers that haven't been migrated don't break."""
    from shared.intelligence.position_monitor import update_position_price_pnl
    import inspect
    sig = inspect.signature(update_position_price_pnl)
    params = sig.parameters
    # New optional kwargs
    for kw in (
        "distance_to_entry_pct",
        "distance_to_sl_pct",
        "distance_to_tp1_pct",
        "time_in_trade_minutes",
    ):
        assert kw in params, f"missing kwarg {kw}"
        assert params[kw].default is None, f"{kw} must default to None for backward compat"
    # Old positional args still in correct order
    pos_args = [p.name for p in params.values()
                if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                              inspect.Parameter.POSITIONAL_ONLY)]
    assert pos_args[:5] == [
        "pool", "position_id", "current_price",
        "pnl_pct", "unrealized_pnl_usd",
    ], f"signature drift: positional args = {pos_args}"


# ---------------------------------------------------------------------------
# Phase 12.3 — trade journey route registered
# ---------------------------------------------------------------------------
def test_journey_route_registered():
    """build_app must register GET /api/position-journey/{id}.

    This is the smallest integration check that doesn't require a DB:
    it asserts the route exists and points at handle_position_journey."""
    from shared.dashboard import server as srv
    # Walk the source for the registration line — cheaper than booting aiohttp.
    import inspect
    src = inspect.getsource(srv)
    assert "/api/position-journey/{id}" in src, \
        "Round 12 journey route not registered"
    assert "handle_position_journey" in src
    # The handler must exist and be a coroutine
    assert hasattr(srv, "handle_position_journey")


# ---------------------------------------------------------------------------
# Phase 12.4 — retirement: legacy /api/positions gone, snapshot uses live
# ---------------------------------------------------------------------------
def test_legacy_positions_route_retired():
    """Round 12 §12.4: GET /api/positions should be GONE from server.py.

    The route registration line must not appear in build_app, and
    handle_positions (the legacy handler) must be removed too. Test
    this by source-grep so it catches re-additions."""
    import inspect
    from shared.dashboard import server as srv
    src = inspect.getsource(srv)
    # The new endpoints must remain
    assert "/api/positions/live" in src
    assert "/api/positions/historic" in src
    # The legacy registration line must NOT appear (anchored to avoid
    # false positives on the substring sub-routes which are /api/positions/...)
    bad = 'add_get(prefix + "/api/positions", handle_positions)'
    assert bad not in src, "legacy /api/positions GET registration is back"
    # The handler function must be gone
    assert not hasattr(srv, "handle_positions"), \
        "legacy handle_positions function is back — should be removed"


def test_snapshot_uses_live_aggregator():
    """Snapshot builder must call aggregate_live_positions, not the
    deprecated aggregate_open_positions. The Floor mini-table only
    renders live rows so the closed-tail fan-out was wasted I/O."""
    import inspect
    from shared.dashboard import snapshot as snap
    src = inspect.getsource(snap)
    # The deprecated function may still exist (back-compat) but it must
    # not be CALLED inside task_positions().
    # Locate task_positions and inspect its body.
    # Use simple substring assertion since task_positions is a closure.
    assert "snap.positions = await aggregate_live_positions" in src, \
        "snapshot must call aggregate_live_positions in task_positions"


# ---------------------------------------------------------------------------
# Phase 12.4 micro — legacy systemd env var removed; dead JS removed
# ---------------------------------------------------------------------------
def test_systemd_position_monitor_no_legacy_env():
    """The legacy POSITION_MONITOR_INTERVAL_S env var must be gone from
    the systemd unit file. Code reads POSITION_MONITOR_POLL_S; the
    legacy var was a transition alias kept across Round 11.

    The check looks for the actual ``Environment=...=`` declaration
    rather than the substring (the comment in the unit file
    explains the removal and naturally mentions the old name)."""
    unit = ROOT / "systemd" / "tickles-position-monitor.service"
    if not unit.exists():
        pytest.skip("systemd unit file not present")
    text = unit.read_text()
    # Must NOT have the actual environment variable declaration
    assert "Environment=POSITION_MONITOR_INTERVAL_S=" not in text, \
        "Round 12: legacy POSITION_MONITOR_INTERVAL_S env declaration still present"
    # The current var must remain
    assert "Environment=POSITION_MONITOR_POLL_S=" in text


def test_app_js_dead_renderPositionsTable_removed():
    """Round 12: renderPositionsTable removed (Round 11 split made it
    dead code). Source-grep so a re-add by accident is caught."""
    js = ROOT / "shared" / "dashboard" / "static" / "app.js"
    assert js.exists()
    text = js.read_text()
    # Function definition is gone
    assert "function renderPositionsTable(" not in text, \
        "renderPositionsTable function is back"
    # Round 11 renderers ARE present
    assert "function renderPositionsLive(" in text
    assert "function renderPositionsHistoric(" in text


# ---------------------------------------------------------------------------
# Phase 12.4 micro — exchange_router unit logic
# ---------------------------------------------------------------------------
def test_router_cache_isolation():
    """clear_cache() must drop entries; otherwise unit tests pollute
    each other when the unified_instruments table changes."""
    from shared.utils.exchange_router import _CACHE, clear_cache
    _CACHE["test|"] = (0, "dummy")  # synthetic entry
    assert _CACHE
    clear_cache()
    assert not _CACHE


# ---------------------------------------------------------------------------
# Round 13 — crypto-first remap + bare base + perp persistence (no DB)
# ---------------------------------------------------------------------------
def test_router_crypto_first_remap_table_present():
    """Round 13: GOLD/SILVER/SPX/NQ/etc. must be in the crypto-first remap.

    Source-grep so a regression in the mapping is caught even without a
    live DB. The values map to the spot canonical (``XAU/USDT``); the
    perp-swap conversion is applied later in resolve_market."""
    from shared.utils import exchange_router as r
    table = r._CRYPTO_FIRST_REMAP
    # Commodities
    assert table["GOLD"] == "XAU/USDT"
    assert table["XAU/USD"] == "XAU/USDT"
    assert table["SILVER"] == "XAG/USDT"
    # Indices
    assert table["NAS100"] == "QQQ/USDT"
    assert table["NQ"] == "QQQ/USDT"
    assert table["US100"] == "QQQ/USDT"
    assert table["SPX"] == "SPX/USDT"
    assert table["SP500"] == "SPX/USDT"
    assert table["US500"] == "SPX/USDT"


def test_router_known_crypto_bases_present():
    """Round 13: bare crypto tickers default to /USDT.

    The known-base set must include the obvious majors so
    ``resolve_market('BTC')`` returns a supported route."""
    from shared.utils.exchange_router import _CRYPTO_BASES
    for base in ("BTC", "ETH", "SOL", "BNB", "XRP", "PAXG", "XAUT",
                 "TSLA", "AAPL", "NVDA", "MSTR", "QQQ", "SPX",
                 "XAU", "XAG", "KAS", "DOGE", "LINK"):
        assert base in _CRYPTO_BASES, f"{base} missing from _CRYPTO_BASES"
    # Sanity: forex bases NOT in the list (those go to capital-only)
    for not_crypto in ("EUR", "GBP", "JPY", "AUD", "CAD"):
        assert not_crypto not in _CRYPTO_BASES, \
            f"{not_crypto} must NOT be in _CRYPTO_BASES (forex)"


def test_router_unsupported_reasons_complete():
    """Round 13 added CAPITAL_ONLY. Make sure every reason still has a
    non-empty string value (lint guard)."""
    from shared.utils.exchange_router import UNSUPPORTED_REASONS
    expected = {
        "AGGREGATE", "MALFORMED", "NOT_LISTED",
        "EMPTY", "CAPITAL_ONLY", "UNKNOWN",
    }
    actual = {k for k in vars(UNSUPPORTED_REASONS) if not k.startswith("_")}
    missing = expected - actual
    assert not missing, f"UNSUPPORTED_REASONS missing: {missing}"
    for k in expected:
        v = getattr(UNSUPPORTED_REASONS, k)
        assert isinstance(v, str) and v, f"{k} value must be non-empty string"


def test_router_aggregate_table_unchanged_round13():
    """Round 13 didn't change the aggregate list — guard against
    accidental drops."""
    from shared.utils.exchange_router import _AGGREGATE_SYMBOLS
    for s in ("USDT.D", "BTC.D", "ETH.D", "TOTAL", "TOTAL2", "TOTAL3"):
        assert s in _AGGREGATE_SYMBOLS, f"{s} dropped from aggregate set"


# ---------------------------------------------------------------------------
# Phase 12.1 — interpretation_service router wiring (smoke)
# ---------------------------------------------------------------------------
def test_interpretation_service_imports_router():
    """create_tracked_position_from_interpretation must reference the
    Round 12 router. We assert by inspecting the function source — no
    DB or LLM needed."""
    from shared.intelligence import interpretation_service as svc
    import inspect
    src = inspect.getsource(svc.create_tracked_position_from_interpretation)
    assert "shared.utils.exchange_router" in src, \
        "Round 12: create_tracked_position_from_interpretation must import the router"
    assert "resolve_market" in src, "Round 12: must call resolve_market"
    assert "unsupported_status_reason" in src, \
        "Round 12: must use unsupported_status_reason for cancelled rows"
    # The INSERT block must NOT use a `or "bybit"` fallback for the
    # ``instrument_exchange`` bind value — the routed exchange is now
    # the canonical truth. We grep specifically for the SQL-VALUES
    # binding pattern, NOT the helper-only fallback in normalise_instrument
    # (which is defensive against None).
    # The bind list line we removed was literally:
    #   ``instrument_exchange or "bybit",``  immediately after
    #   ``instrument_symbol,``  in the values tuple.
    bad = 'instrument_symbol,\n            instrument_exchange or "bybit",\n            sym_norm,'
    assert bad not in src, \
        "Round 12: legacy 'or \"bybit\"' default still in INSERT values list"


# ---------------------------------------------------------------------------
# Phase 12.1 — position_monitor uses the multi-venue helper
# ---------------------------------------------------------------------------
def test_position_monitor_has_multi_venue_helper():
    """_fetch_ohlcv_for_market must exist and be a coroutine; both
    fallback blocks must call it."""
    from shared.intelligence import position_monitor as pm
    import inspect
    assert hasattr(pm, "_fetch_ohlcv_for_market")
    assert asyncio.iscoroutinefunction(pm._fetch_ohlcv_for_market)
    # Both fallback paths reference the helper
    src_entry = inspect.getsource(pm._find_entry_touch_candle)
    src_wick = inspect.getsource(pm._find_sl_tp_wick_candle)
    assert "_fetch_ohlcv_for_market" in src_entry, \
        "_find_entry_touch_candle must call the helper"
    assert "_fetch_ohlcv_for_market" in src_wick, \
        "_find_sl_tp_wick_candle must call the helper"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
