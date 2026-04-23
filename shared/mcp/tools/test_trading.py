"""
Module: test_trading
Purpose: Unit tests for MCP trading tools
Location: /opt/tickles/shared/mcp/tools/test_trading.py
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from shared.mcp.tools.trading import (
    _decimal_to_float,
    _fmt_ts,
    _generate_client_order_id,
    _paper_account_id,
    _side_to_direction,
    _handle_banker_positions,
    _handle_execution_status,
    _handle_wallet_paper_create,
)


class TestSideToDirection(unittest.TestCase):
    """Tests for _side_to_direction helper."""

    def test_buy_maps_to_long(self) -> None:
        self.assertEqual(_side_to_direction("buy"), "long")

    def test_sell_maps_to_short(self) -> None:
        self.assertEqual(_side_to_direction("sell"), "short")

    def test_case_insensitive(self) -> None:
        self.assertEqual(_side_to_direction("BUY"), "long")
        self.assertEqual(_side_to_direction("Sell"), "short")

    def test_invalid_side_raises(self) -> None:
        with self.assertRaises(ValueError):
            _side_to_direction("hold")


class TestDecimalToFloat(unittest.TestCase):
    """Tests for _decimal_to_float helper."""

    def test_none_returns_none(self) -> None:
        self.assertIsNone(_decimal_to_float(None))

    def test_int_returns_float(self) -> None:
        self.assertEqual(_decimal_to_float(42), 42.0)

    def test_float_passthrough(self) -> None:
        self.assertEqual(_decimal_to_float(3.14), 3.14)


class TestFmtTs(unittest.TestCase):
    """Tests for _fmt_ts helper."""

    def test_none_returns_none(self) -> None:
        self.assertIsNone(_fmt_ts(None))

    def test_string_passthrough(self) -> None:
        self.assertEqual(_fmt_ts("2026-01-01T00:00:00Z"), "2026-01-01T00:00:00Z")

    def test_datetime_converts(self) -> None:
        from datetime import datetime, timezone
        dt = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = _fmt_ts(dt)
        self.assertIn("2026-01-01", result)


class TestPaperAccountId(unittest.TestCase):
    """Tests for _paper_account_id helper."""

    def test_format(self) -> None:
        result = _paper_account_id("rubicon", "surgeon", "bybit")
        self.assertEqual(result, "paper_rubicon_surgeon_bybit")


class TestGenerateClientOrderId(unittest.TestCase):
    """Tests for _generate_client_order_id helper."""

    def test_prefix(self) -> None:
        result = _generate_client_order_id("c", "a", "BTC/USDT", "long")
        self.assertTrue(result.startswith("tk-"))

    def test_unique(self) -> None:
        ids = set()
        for _ in range(100):
            ids.add(_generate_client_order_id("c", "a", "BTC/USDT", "long"))
        self.assertEqual(len(ids), 100)


class TestHandleBankerPositions(unittest.TestCase):
    """Tests for _handle_banker_positions sync handler."""

    @patch("shared.mcp.tools.trading.db_helper")
    def test_empty_positions(self, mock_db: MagicMock) -> None:
        mock_db.query.return_value = []
        result = _handle_banker_positions({"companyId": "test"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["positions"], [])

    @patch("shared.mcp.tools.trading.db_helper")
    def test_with_positions(self, mock_db: MagicMock) -> None:
        mock_db.query.return_value = [
            {
                "symbol": "BTC/USDT",
                "direction": "long",
                "quantity": 0.01,
                "average_entry_price": 76000.0,
                "notional_usd": 760.0,
                "unrealised_pnl_usd": 10.0,
                "realized_pnl_usd": 0.0,
                "leverage": 1,
                "exchange": "bybit",
                "adapter": "paper",
                "ts": None,
            }
        ]
        result = _handle_banker_positions({"companyId": "test"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["positions"][0]["symbol"], "BTC/USDT")
        self.assertEqual(result["positions"][0]["direction"], "long")


class TestHandleExecutionStatus(unittest.TestCase):
    """Tests for _handle_execution_status sync handler."""

    @patch("shared.mcp.tools.trading.db_helper")
    def test_no_ids_returns_error(self, mock_db: MagicMock) -> None:
        result = _handle_execution_status({"companyId": "test"})
        self.assertEqual(result["status"], "error")

    @patch("shared.mcp.tools.trading.db_helper")
    def test_order_not_found(self, mock_db: MagicMock) -> None:
        mock_db.query.return_value = []
        result = _handle_execution_status(
            {"companyId": "test", "orderId": "999"}
        )
        self.assertEqual(result["status"], "not_found")

    @patch("shared.mcp.tools.trading.db_helper")
    def test_order_found_by_id(self, mock_db: MagicMock) -> None:
        mock_db.query.return_value = [
            {
                "id": 1,
                "client_order_id": "tk-abc123",
                "external_order_id": None,
                "adapter": "paper",
                "exchange": "bybit",
                "symbol": "BTC/USDT",
                "direction": "long",
                "order_type": "market",
                "quantity": 0.01,
                "requested_price": None,
                "status": "filled",
                "filled_quantity": 0.01,
                "average_fill_price": 76000.0,
                "fees_paid_usd": 0.30,
                "reason": None,
                "submitted_at": None,
                "updated_at": None,
            }
        ]
        result = _handle_execution_status(
            {"companyId": "test", "orderId": "1"}
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["order"]["id"], 1)
        self.assertEqual(result["order"]["status"], "filled")

    @patch("shared.mcp.tools.trading.db_helper")
    def test_order_found_by_client_id(self, mock_db: MagicMock) -> None:
        mock_db.query.return_value = [
            {
                "id": 2,
                "client_order_id": "tk-xyz789",
                "external_order_id": None,
                "adapter": "paper",
                "exchange": "bybit",
                "symbol": "ETH/USDT",
                "direction": "short",
                "order_type": "limit",
                "quantity": 1.0,
                "requested_price": 3000.0,
                "status": "accepted",
                "filled_quantity": 0.0,
                "average_fill_price": None,
                "fees_paid_usd": 0.0,
                "reason": None,
                "submitted_at": None,
                "updated_at": None,
            }
        ]
        result = _handle_execution_status(
            {"companyId": "test", "clientOrderId": "tk-xyz789"}
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["order"]["direction"], "short")


class TestHandleWalletPaperCreate(unittest.TestCase):
    """Tests for _handle_wallet_paper_create sync handler."""

    @patch("shared.mcp.tools.trading.db_helper")
    def test_creates_wallet(self, mock_db: MagicMock) -> None:
        mock_db.execute.return_value = 1
        result = _handle_wallet_paper_create({
            "companyId": "rubicon",
            "agentId": "surgeon",
            "startingUsd": 50000,
            "venue": "bybit",
        })
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["startingBalanceUsd"], 50000.0)
        self.assertEqual(
            result["accountIdExternal"], "paper_rubicon_surgeon_bybit"
        )
        # Should have called execute twice: wallet upsert + banker seed
        self.assertEqual(mock_db.execute.call_count, 2)

    @patch("shared.mcp.tools.trading.db_helper")
    def test_default_values(self, mock_db: MagicMock) -> None:
        mock_db.execute.return_value = 1
        result = _handle_wallet_paper_create({
            "companyId": "test",
            "agentId": "bot",
        })
        self.assertEqual(result["venue"], "bybit")
        self.assertEqual(result["startingBalanceUsd"], 10000.0)
        self.assertEqual(result["currency"], "USD")

    @patch("shared.mcp.tools.trading.db_helper")
    def test_negative_starting_usd_rejected(self, mock_db: MagicMock) -> None:
        result = _handle_wallet_paper_create({
            "companyId": "test",
            "agentId": "bot",
            "startingUsd": -100,
        })
        self.assertEqual(result["status"], "error")
        self.assertIn("positive", result["message"])

    @patch("shared.mcp.tools.trading.db_helper")
    def test_zero_starting_usd_rejected(self, mock_db: MagicMock) -> None:
        result = _handle_wallet_paper_create({
            "companyId": "test",
            "agentId": "bot",
            "startingUsd": 0,
        })
        self.assertEqual(result["status"], "error")

    @patch("shared.mcp.tools.trading.db_helper")
    def test_db_error_returns_error(self, mock_db: MagicMock) -> None:
        mock_db.execute.side_effect = Exception("connection refused")
        result = _handle_wallet_paper_create({
            "companyId": "test",
            "agentId": "bot",
        })
        self.assertEqual(result["status"], "error")
        self.assertIn("failed to create wallet", result["message"])


class TestHandleBankerPositionsErrorHandling(unittest.TestCase):
    """Tests for error handling in _handle_banker_positions."""

    @patch("shared.mcp.tools.trading.db_helper")
    def test_db_error_returns_error(self, mock_db: MagicMock) -> None:
        mock_db.query.side_effect = Exception("connection refused")
        result = _handle_banker_positions({"companyId": "test"})
        self.assertEqual(result["status"], "error")
        self.assertIn("failed to read positions", result["message"])


class TestHandleExecutionStatusErrorHandling(unittest.TestCase):
    """Tests for error handling in _handle_execution_status."""

    @patch("shared.mcp.tools.trading.db_helper")
    def test_db_error_returns_error(self, mock_db: MagicMock) -> None:
        mock_db.query.side_effect = Exception("connection refused")
        result = _handle_execution_status(
            {"companyId": "test", "orderId": "1"}
        )
        self.assertEqual(result["status"], "error")
        self.assertIn("failed to read order", result["message"])


if __name__ == "__main__":
    unittest.main()
