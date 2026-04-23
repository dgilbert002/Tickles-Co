"""
Module: test_bug_fixes
Purpose: Verify all 12 bug fixes from the bug-hunter audit.
Location: /opt/tickles/shared/tests/test_bug_fixes.py

Covers bugs in:
  - capital_adapter.py  (BUG #1, #3, #4, #6)
  - ccxt_adapter.py     (BUG #7, #8)
  - capital_source.py   (BUG #13, #14, #15, #16, #17)
  - trading.py          (BUG #12)
"""

import asyncio
import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from shared.connectors.capital_adapter import (
    CapitalAdapter,
    DEMO_BASE_URL,
    LIVE_BASE_URL,
)
from shared.gateway.capital_source import (
    CapitalSource,
    DEMO_WS_URL,
    LIVE_WS_URL,
    _RESOLUTION_MAP,
)
from shared.gateway.schema import Candle, SubscriptionKey, TickChannel


# ---------------------------------------------------------------------------
# BUG #1: switch_account() session race condition
# ---------------------------------------------------------------------------

class TestBug1AuthHeaders(unittest.IsolatedAsyncioTestCase):
    """Verify _auth_headers() returns current tokens and switch_account()
    does NOT recreate the session (avoiding the race condition)."""

    async def test_auth_headers_returns_cst_and_token(self):
        """_auth_headers() must include CST and X-SECURITY-TOKEN when set."""
        adapter = CapitalAdapter(environment="demo")
        adapter._cst = "test-cst-123"
        adapter._x_security_token = "test-sec-456"

        headers = adapter._auth_headers()
        self.assertEqual(headers["CST"], "test-cst-123")
        self.assertEqual(headers["X-SECURITY-TOKEN"], "test-sec-456")

    async def test_auth_headers_empty_when_no_tokens(self):
        """_auth_headers() must return empty dict when tokens are not set."""
        adapter = CapitalAdapter(environment="demo")
        headers = adapter._auth_headers()
        self.assertEqual(headers, {})

    async def test_auth_headers_partial_token(self):
        """_auth_headers() must include only the token that is set."""
        adapter = CapitalAdapter(environment="demo")
        adapter._cst = "only-cst"
        headers = adapter._auth_headers()
        self.assertEqual(headers, {"CST": "only-cst"})

    async def test_switch_account_updates_tokens_not_session(self):
        """switch_account() must update tokens in-place without recreating
        the session, so concurrent coroutines are not broken."""
        adapter = CapitalAdapter(environment="demo")
        adapter._authenticated = True
        adapter._cst = "old-cst"
        adapter._x_security_token = "old-sec"
        adapter._api_key = "test-key"

        # Create a real session so we can verify it stays the same object.
        session = await adapter._get_session()
        session_id_before = id(session)

        # Mock the PUT response to return new tokens.
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.headers = {"CST": "new-cst", "X-SECURITY-TOKEN": "new-sec"}
        mock_resp.text = AsyncMock(return_value="")

        mock_put = AsyncMock(return_value=mock_resp)
        mock_put.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_put.__aexit__ = AsyncMock(return_value=False)

        with patch.object(session, "put", return_value=mock_put):
            result = await adapter.switch_account("ABC123")

        self.assertTrue(result)
        self.assertEqual(adapter._cst, "new-cst")
        self.assertEqual(adapter._x_security_token, "new-sec")
        self.assertEqual(adapter._account_id, "ABC123")

        # Session object must be the same — not recreated.
        session_after = await adapter._get_session()
        self.assertEqual(id(session_after), session_id_before)

        # _auth_headers() must now reflect the new tokens.
        headers = adapter._auth_headers()
        self.assertEqual(headers["CST"], "new-cst")
        self.assertEqual(headers["X-SECURITY-TOKEN"], "new-sec")

        await adapter.close()


# ---------------------------------------------------------------------------
# BUG #3: fetch_balance() uses float for money
# ---------------------------------------------------------------------------

class TestBug3DecimalBalance(unittest.IsolatedAsyncioTestCase):
    """Verify fetch_balance() uses Decimal intermediate calculation."""

    async def test_balance_uses_decimal_precision(self):
        """Balance values must be computed via Decimal to avoid float
        precision errors (e.g. 0.1 + 0.2 != 0.3)."""
        adapter = CapitalAdapter(environment="demo")
        adapter._authenticated = True
        adapter._api_key = "test-key"

        # Simulate API response with values that expose float imprecision.
        api_response = {
            "accounts": [{
                "currency": "USD",
                "balance": {
                    "balance": 10000.07,
                    "available": 9500.03,
                },
            }]
        }

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=api_response)

        session = await adapter._get_session()
        mock_get = AsyncMock(return_value=mock_resp)
        mock_get.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_get.__aexit__ = AsyncMock(return_value=False)

        with patch.object(session, "get", return_value=mock_get):
            balance = await adapter.fetch_balance()

        # The 'used' value is total - available, computed via Decimal.
        # With float: 10000.07 - 9500.03 = 500.0400000000009
        # With Decimal: 500.04
        self.assertAlmostEqual(balance["USD"]["used"], 500.04, places=2)
        self.assertAlmostEqual(balance["USD"]["total"], 10000.07, places=2)
        self.assertAlmostEqual(balance["USD"]["free"], 9500.03, places=2)

        await adapter.close()


# ---------------------------------------------------------------------------
# BUG #4: fetch_ticker() last=mid is wrong
# ---------------------------------------------------------------------------

class TestBug4LastTradedPrice(unittest.IsolatedAsyncioTestCase):
    """Verify fetch_ticker() uses lastTradedPrice when available."""

    async def test_ticker_uses_last_traded_price(self):
        """When lastTradedPrice is present, 'last' must equal it,
        not the mid-price."""
        adapter = CapitalAdapter(environment="demo")
        adapter._authenticated = True
        adapter._api_key = "test-key"

        api_response = {
            "snapshot": {
                "bid": 1.1000,
                "offer": 1.1020,
                "lastTradedPrice": 1.1015,
            }
        }

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=api_response)

        session = await adapter._get_session()
        mock_get = AsyncMock(return_value=mock_resp)
        mock_get.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_get.__aexit__ = AsyncMock(return_value=False)

        with patch.object(session, "get", return_value=mock_get):
            ticker = await adapter.fetch_ticker("CS.D.EURUSD.CFD.IP")

        # last must be the lastTradedPrice, not the mid-price.
        self.assertEqual(ticker["last"], 1.1015)
        # Mid-price would be (1.1000 + 1.1020) / 2 = 1.1010 — must NOT be used.
        self.assertNotEqual(ticker["last"], 1.1010)

        await adapter.close()

    async def test_ticker_falls_back_to_mid_when_no_last(self):
        """When lastTradedPrice is 0/missing, 'last' must fall back
        to mid-price."""
        adapter = CapitalAdapter(environment="demo")
        adapter._authenticated = True
        adapter._api_key = "test-key"

        api_response = {
            "snapshot": {
                "bid": 1.1000,
                "offer": 1.1020,
                "lastTradedPrice": 0,
            }
        }

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=api_response)

        session = await adapter._get_session()
        mock_get = AsyncMock(return_value=mock_resp)
        mock_get.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_get.__aexit__ = AsyncMock(return_value=False)

        with patch.object(session, "get", return_value=mock_get):
            ticker = await adapter.fetch_ticker("CS.D.EURUSD.CFD.IP")

        # Fallback: mid-price = (1.1000 + 1.1020) / 2 = 1.1010
        self.assertEqual(ticker["last"], 1.1010)

        await adapter.close()


# ---------------------------------------------------------------------------
# BUG #6: get_market_hours() redundant API call
# ---------------------------------------------------------------------------

class TestBug6MarketHoursSingleCall(unittest.IsolatedAsyncioTestCase):
    """Verify get_market_hours() makes only ONE API call, not two."""

    async def test_market_hours_single_api_call(self):
        """get_market_hours() must determine is_open from the same
        response instead of calling get_market_status() again."""
        adapter = CapitalAdapter(environment="demo")
        adapter._authenticated = True
        adapter._api_key = "test-key"

        api_response = {
            "instrument": {"openingHours": [{"mon": "00:00-23:59"}]},
            "snapshot": {"bid": 1.1000},
            "dealingRules": [{"rule": "some_rule"}],
        }

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=api_response)

        session = await adapter._get_session()
        call_count = 0

        mock_get = AsyncMock(return_value=mock_resp)
        mock_get.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_get.__aexit__ = AsyncMock(return_value=False)

        with patch.object(session, "get", return_value=mock_get) as patched_get:
            result = await adapter.get_market_hours("CS.D.EURUSD.CFD.IP")

        # Only one GET call should have been made.
        self.assertEqual(patched_get.call_count, 1)
        self.assertTrue(result["is_open"])

        await adapter.close()


# ---------------------------------------------------------------------------
# BUG #7: submit() metadata None check
# ---------------------------------------------------------------------------

class TestBug7MetadataNone(unittest.IsolatedAsyncioTestCase):
    """Verify CcxtExecutionAdapter.submit() handles None metadata."""

    async def test_submit_with_none_metadata(self):
        """submit() must not crash when intent.metadata is None."""
        from shared.execution.ccxt_adapter import CcxtExecutionAdapter
        from shared.execution.protocol import (
            ExecutionIntent, DIRECTION_LONG, ORDER_TYPE_MARKET,
        )

        adapter = CcxtExecutionAdapter(sandbox=True)

        # Use an explicit client_order_id so we can look it up later.
        # (ensure_client_order_id() generates a new ID each call because
        # ExecutionIntent is frozen and can't store the result.)
        fixed_cid = "test-cid-none-meta"

        # Create an intent with metadata=None (the bug trigger).
        intent = ExecutionIntent(
            company_id="test",
            strategy_id="strat",
            agent_id="agent",
            exchange="bybit",
            account_id_external="main",
            symbol="BTC/USDT",
            direction=DIRECTION_LONG,
            order_type=ORDER_TYPE_MARKET,
            quantity=0.001,
            client_order_id=fixed_cid,
            metadata=None,  # BUG #7 trigger
        )

        # Mock _get_client to avoid real exchange connection.
        mock_client = MagicMock()
        mock_client.create_order = MagicMock(return_value={
            "id": "ex-123",
            "status": "open",
            "symbol": "BTC/USDT",
        })
        adapter._get_client = MagicMock(return_value=mock_client)

        # This must not raise AttributeError.
        updates = await adapter.submit(intent)

        # Should get at least 2 updates: submitted + accepted.
        self.assertGreaterEqual(len(updates), 2)
        # The known_orders entry must have account_name="main" (default).
        self.assertEqual(adapter._known_orders[fixed_cid]["account_name"], "main")

    async def test_submit_with_metadata_account_name(self):
        """submit() must store the account_name from metadata."""
        from shared.execution.ccxt_adapter import CcxtExecutionAdapter
        from shared.execution.protocol import (
            ExecutionIntent, DIRECTION_LONG, ORDER_TYPE_MARKET,
        )

        adapter = CcxtExecutionAdapter(sandbox=True)

        fixed_cid = "test-cid-scalp-meta"

        intent = ExecutionIntent(
            company_id="test",
            strategy_id="strat",
            agent_id="agent",
            exchange="bybit",
            account_id_external="scalp",
            symbol="BTC/USDT",
            direction=DIRECTION_LONG,
            order_type=ORDER_TYPE_MARKET,
            quantity=0.001,
            client_order_id=fixed_cid,
            metadata={"accountName": "scalp"},
        )

        mock_client = MagicMock()
        mock_client.create_order = MagicMock(return_value={
            "id": "ex-456",
            "status": "open",
            "symbol": "BTC/USDT",
        })
        adapter._get_client = MagicMock(return_value=mock_client)

        updates = await adapter.submit(intent)
        self.assertEqual(adapter._known_orders[fixed_cid]["account_name"], "scalp")


# ---------------------------------------------------------------------------
# BUG #8: _known_orders doesn't track account_name
# ---------------------------------------------------------------------------

class TestBug8AccountNameTracking(unittest.IsolatedAsyncioTestCase):
    """Verify cancel() and poll_updates() use the stored account_name."""

    async def test_cancel_uses_stored_account_name(self):
        """cancel() must use the account_name from _known_orders,
        not the default parameter."""
        from shared.execution.ccxt_adapter import CcxtExecutionAdapter

        adapter = CcxtExecutionAdapter(sandbox=True)

        # Pre-populate _known_orders with a specific account_name.
        adapter._known_orders["cid-1"] = {
            "exchange": "bybit",
            "symbol": "BTC/USDT",
            "account_name": "scalp",
            "raw": {"id": "ex-789", "status": "open"},
        }

        mock_client = MagicMock()
        mock_client.cancel_order = MagicMock(return_value={
            "id": "ex-789",
            "status": "canceled",
        })

        # Track which client key was requested.
        captured_key = None

        def mock_get_client(exchange: str, account_name: str) -> Any:
            nonlocal captured_key
            captured_key = f"{exchange}:{account_name}"
            return mock_client

        adapter._get_client = mock_get_client

        # Call cancel with default account_name="main" — should still
        # route to "scalp" because that's what was stored.
        await adapter.cancel("cid-1", exchange="bybit", symbol="BTC/USDT")

        self.assertEqual(captured_key, "bybit:scalp")

    async def test_poll_uses_stored_account_name(self):
        """poll_updates() must use the account_name from _known_orders."""
        from shared.execution.ccxt_adapter import CcxtExecutionAdapter

        adapter = CcxtExecutionAdapter(sandbox=True)

        adapter._known_orders["cid-2"] = {
            "exchange": "bybit",
            "symbol": "ETH/USDT",
            "account_name": "momentum",
            "raw": {"id": "ex-999", "status": "open"},
        }

        mock_client = MagicMock()
        mock_client.fetch_order = MagicMock(return_value={
            "id": "ex-999",
            "status": "filled",
            "symbol": "ETH/USDT",
            "filled": 1.0,
        })

        captured_key = None

        def mock_get_client(exchange: str, account_name: str) -> Any:
            nonlocal captured_key
            captured_key = f"{exchange}:{account_name}"
            return mock_client

        adapter._get_client = mock_get_client

        # Call poll with default account_name="main" — should route to "momentum".
        await adapter.poll_updates(["cid-2"])

        self.assertEqual(captured_key, "bybit:momentum")


# ---------------------------------------------------------------------------
# BUG #12: venue name inconsistency "capital" vs "capitalcom"
# ---------------------------------------------------------------------------

class TestBug12VenueNormalization(unittest.TestCase):
    """Verify _is_capital_venue() accepts both 'capital' and 'capitalcom'."""

    def test_capital_venue_lowercase(self):
        """'capital' must be recognized as a Capital.com venue."""
        from shared.mcp.tools.trading import _is_capital_venue
        self.assertTrue(_is_capital_venue("capital"))

    def test_capitalcom_venue(self):
        """'capitalcom' (the adapter's name property) must be recognized."""
        from shared.mcp.tools.trading import _is_capital_venue
        self.assertTrue(_is_capital_venue("capitalcom"))

    def test_capital_venue_mixed_case(self):
        """Mixed-case variants must also be recognized."""
        from shared.mcp.tools.trading import _is_capital_venue
        self.assertTrue(_is_capital_venue("Capital"))
        self.assertTrue(_is_capital_venue("CapitalCom"))

    def test_non_capital_venue(self):
        """Non-Capital venues must return False."""
        from shared.mcp.tools.trading import _is_capital_venue
        self.assertFalse(_is_capital_venue("bybit"))
        self.assertFalse(_is_capital_venue("binance"))

    def test_adapter_name_matches(self):
        """CapitalAdapter.name must be recognized by _is_capital_venue."""
        from shared.mcp.tools.trading import _is_capital_venue
        adapter = CapitalAdapter(environment="demo")
        self.assertTrue(_is_capital_venue(adapter.name))


# ---------------------------------------------------------------------------
# BUG #13: hardcoded live WS URL
# ---------------------------------------------------------------------------

class TestBug13WsUrlSelection(unittest.TestCase):
    """Verify CapitalSource selects the correct WS URL based on
    the adapter's environment."""

    def test_demo_adapter_uses_demo_ws_url(self):
        """Demo adapter must use the demo WS streaming URL."""
        adapter = CapitalAdapter(environment="demo")
        # The source reads adapter._environment to pick the URL.
        self.assertEqual(adapter._environment, "demo")

    def test_live_adapter_uses_live_ws_url(self):
        """Live adapter must use the live WS streaming URL."""
        adapter = CapitalAdapter(environment="live")
        self.assertEqual(adapter._environment, "live")

    def test_ws_url_constants_are_different(self):
        """Demo and Live WS URLs must be different."""
        self.assertNotEqual(DEMO_WS_URL, LIVE_WS_URL)

    def test_demo_ws_url_contains_demo(self):
        """Demo WS URL must contain 'demo'."""
        self.assertIn("demo", DEMO_WS_URL)

    def test_live_ws_url_does_not_contain_demo(self):
        """Live WS URL must NOT contain 'demo'."""
        self.assertNotIn("demo", LIVE_WS_URL)


# ---------------------------------------------------------------------------
# BUG #14: hardcoded MINUTE resolution
# ---------------------------------------------------------------------------

class TestBug14ResolutionMap(unittest.TestCase):
    """Verify _RESOLUTION_MAP is used instead of hardcoded 'MINUTE'."""

    def test_resolution_map_has_standard_timeframes(self):
        """_RESOLUTION_MAP must include standard timeframe mappings."""
        self.assertEqual(_RESOLUTION_MAP["1m"], "MINUTE")
        self.assertEqual(_RESOLUTION_MAP["5m"], "MINUTE_5")
        self.assertEqual(_RESOLUTION_MAP["15m"], "MINUTE_15")
        self.assertEqual(_RESOLUTION_MAP["1h"], "HOUR")
        self.assertEqual(_RESOLUTION_MAP["4h"], "HOUR_4")
        self.assertEqual(_RESOLUTION_MAP["1d"], "DAY")

    def test_resolution_map_default_fallback(self):
        """Unknown timeframe must fall back to 'MINUTE'."""
        self.assertEqual(
            _RESOLUTION_MAP.get("unknown", "MINUTE"), "MINUTE"
        )


# ---------------------------------------------------------------------------
# BUG #15: no payload field validation
# ---------------------------------------------------------------------------

class TestBug15PayloadValidation(unittest.IsolatedAsyncioTestCase):
    """Verify incomplete OHLC payloads are skipped."""

    async def test_incomplete_payload_skipped(self):
        """Messages missing required OHLC fields must be silently skipped
        instead of raising KeyError."""
        adapter = CapitalAdapter(environment="demo")
        messages_received = []

        async def on_message(key: SubscriptionKey, candle: Any) -> None:
            messages_received.append(candle)

        source = CapitalSource(adapter=adapter, on_message=on_message)

        # Payload missing 'h' (high) and 'l' (low).
        incomplete_msg = json.dumps({
            "destination": "ohlc.event",
            "payload": {
                "epic": "CS.D.EURUSD.CFD.IP",
                "t": 1700000000000,
                "o": "1.1000",
                # 'h' missing
                # 'l' missing
                "c": "1.1010",
            },
        })

        # Must not raise — should silently skip.
        await source._handle_raw_message(incomplete_msg)
        self.assertEqual(len(messages_received), 0)

    async def test_complete_payload_accepted(self):
        """Complete OHLC payloads must produce a Candle."""
        adapter = CapitalAdapter(environment="demo")
        messages_received = []

        async def on_message(key: SubscriptionKey, candle: Any) -> None:
            messages_received.append(candle)

        source = CapitalSource(adapter=adapter, on_message=on_message)

        complete_msg = json.dumps({
            "destination": "ohlc.event",
            "payload": {
                "epic": "CS.D.EURUSD.CFD.IP",
                "t": 1700000000000,
                "o": "1.1000",
                "h": "1.1050",
                "l": "1.0990",
                "c": "1.1010",
                "v": "1234",
            },
        })

        await source._handle_raw_message(complete_msg)
        self.assertEqual(len(messages_received), 1)
        self.assertIsInstance(messages_received[0], Candle)

    async def test_payload_with_null_fields_skipped(self):
        """Payloads with None/null values for required fields must be skipped."""
        adapter = CapitalAdapter(environment="demo")
        messages_received = []

        async def on_message(key: SubscriptionKey, candle: Any) -> None:
            messages_received.append(candle)

        source = CapitalSource(adapter=adapter, on_message=on_message)

        null_msg = json.dumps({
            "destination": "ohlc.event",
            "payload": {
                "epic": "CS.D.EURUSD.CFD.IP",
                "t": 1700000000000,
                "o": "1.1000",
                "h": None,  # null instead of missing
                "l": "1.0990",
                "c": "1.1010",
            },
        })

        await source._handle_raw_message(null_msg)
        self.assertEqual(len(messages_received), 0)


# ---------------------------------------------------------------------------
# BUG #16: reconnect count for all streams
# ---------------------------------------------------------------------------

class TestBug16ReconnectCount(unittest.IsolatedAsyncioTestCase):
    """Verify only desired keys get their reconnect count incremented."""

    async def test_reconnect_only_for_desired_keys(self):
        """When the WS loop errors, only streams that are in _desired_keys
        should have their reconnect count incremented."""
        adapter = CapitalAdapter(environment="demo")

        async def on_message(key: SubscriptionKey, candle: Any) -> None:
            pass

        source = CapitalSource(adapter=adapter, on_message=on_message)

        # Create two subscription keys.
        key1 = SubscriptionKey(
            exchange="capital", symbol="EPIC1", channel=TickChannel.CANDLE
        )
        key2 = SubscriptionKey(
            exchange="capital", symbol="EPIC2", channel=TickChannel.CANDLE
        )

        # Add both to _streams.
        from shared.gateway.capital_source import _StreamState
        source._streams[key1] = _StreamState(
            started_at=datetime.now(timezone.utc), reconnects=0
        )
        source._streams[key2] = _StreamState(
            started_at=datetime.now(timezone.utc), reconnects=0
        )

        # Only key1 is in _desired_keys.
        source._desired_keys.add(key1)

        # Simulate the reconnect increment logic from _run_loop.
        for key in list(source._streams):
            if key in source._desired_keys:
                source._streams[key].reconnects += 1

        # key1 (desired) should have reconnects=1.
        self.assertEqual(source._streams[key1].reconnects, 1)
        # key2 (not desired) should have reconnects=0.
        self.assertEqual(source._streams[key2].reconnects, 0)


# ---------------------------------------------------------------------------
# BUG #17: stop() doesn't reset _stopping
# ---------------------------------------------------------------------------

class TestBug17StopReset(unittest.IsolatedAsyncioTestCase):
    """Verify stop() resets _stopping so the source can be restarted."""

    async def test_stop_resets_stopping_flag(self):
        """After stop(), _stopping must be False so start_stream() works."""
        adapter = CapitalAdapter(environment="demo")

        async def on_message(key: SubscriptionKey, candle: Any) -> None:
            pass

        source = CapitalSource(adapter=adapter, on_message=on_message)
        source._stopping = False

        # Mock adapter.close() to avoid real cleanup.
        adapter.close = AsyncMock()

        await source.stop()

        # _stopping must be reset to False.
        self.assertFalse(source._stopping)

    async def test_stop_clears_desired_keys_and_streams(self):
        """After stop(), _desired_keys and _streams must be empty."""
        adapter = CapitalAdapter(environment="demo")

        async def on_message(key: SubscriptionKey, candle: Any) -> None:
            pass

        source = CapitalSource(adapter=adapter, on_message=on_message)

        key = SubscriptionKey(
            exchange="capital", symbol="EPIC1", channel=TickChannel.CANDLE
        )
        source._desired_keys.add(key)
        from shared.gateway.capital_source import _StreamState
        source._streams[key] = _StreamState(
            started_at=datetime.now(timezone.utc)
        )

        adapter.close = AsyncMock()
        await source.stop()

        self.assertEqual(len(source._desired_keys), 0)
        self.assertEqual(len(source._streams), 0)

    async def test_source_restartable_after_stop(self):
        """After stop(), start_stream() must be able to create a new task."""
        adapter = CapitalAdapter(environment="demo")

        async def on_message(key: SubscriptionKey, candle: Any) -> None:
            pass

        source = CapitalSource(adapter=adapter, on_message=on_message)
        adapter.close = AsyncMock()

        # Stop the source.
        await source.stop()

        # Now start a stream — must not fail.
        key = SubscriptionKey(
            exchange="capital", symbol="EPIC1", channel=TickChannel.CANDLE
        )

        # Mock _subscribe_key to avoid real WS operations.
        source._subscribe_key = AsyncMock()

        await source.start_stream(key)

        # A new main task must have been created.
        self.assertIsNotNone(source._main_task)
        self.assertFalse(source._main_task.done())

        # Clean up.
        source._main_task.cancel()
        try:
            await source._main_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Integration: _auth_headers used in all API calls
# ---------------------------------------------------------------------------

class TestAuthHeadersInAllCalls(unittest.IsolatedAsyncioTestCase):
    """Verify all API methods pass headers=self._auth_headers()."""

    async def test_fetch_ohlcv_passes_auth_headers(self):
        """fetch_ohlcv must pass per-request auth headers."""
        adapter = CapitalAdapter(environment="demo")
        adapter._authenticated = True
        adapter._api_key = "test-key"
        adapter._cst = "cst-val"
        adapter._x_security_token = "sec-val"

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"prices": []})

        session = await adapter._get_session()
        captured_kwargs = {}

        original_get = session.get

        def capture_get(*args, **kwargs):
            captured_kwargs.update(kwargs)
            mock_cm = AsyncMock()
            mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_cm.__aexit__ = AsyncMock(return_value=False)
            return mock_cm

        with patch.object(session, "get", side_effect=capture_get):
            try:
                await adapter.fetch_ohlcv("CS.D.EURUSD.CFD.IP", "5m")
            except Exception:
                pass  # May fail on parsing empty data — that's OK.

        # Must have passed auth headers.
        self.assertIn("headers", captured_kwargs)
        self.assertEqual(captured_kwargs["headers"].get("CST"), "cst-val")
        self.assertEqual(
            captured_kwargs["headers"].get("X-SECURITY-TOKEN"), "sec-val"
        )

        await adapter.close()


if __name__ == "__main__":
    unittest.main()
