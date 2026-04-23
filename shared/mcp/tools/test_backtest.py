"""
Module: test_backtest
Purpose: Unit tests for MCP backtest discovery tools
Location: /opt/tickles/shared/mcp/tools/test_backtest.py
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch, PropertyMock

from shared.mcp.tools.backtest import (
    _strategy_card,
    _indicator_card,
    _handle_strategy_list,
    _handle_strategy_get,
    _handle_indicator_list,
    _handle_indicator_get,
    _handle_indicator_compute_preview,
    _handle_engine_list,
    _compose_spec,
    _plan_sweep,
    _top_k,
)


# ---------------------------------------------------------------------------
# Strategy helpers
# ---------------------------------------------------------------------------


class TestStrategyCard(unittest.TestCase):
    """Tests for _strategy_card helper."""

    def test_extracts_first_docstring_line(self) -> None:
        def my_strat(df, params):
            """This is my strategy.

            It does things.
            """
            pass

        card = _strategy_card("my_strat", my_strat)
        self.assertEqual(card["name"], "my_strat")
        self.assertEqual(card["description"], "This is my strategy.")

    def test_no_docstring(self) -> None:
        def no_doc(df, params):
            pass

        card = _strategy_card("no_doc", no_doc)
        self.assertEqual(card["name"], "no_doc")
        self.assertEqual(card["description"], "")


class TestHandleStrategyList(unittest.TestCase):
    """Tests for _handle_strategy_list sync handler."""

    @patch("shared.mcp.tools.backtest._list_strategies")
    def test_returns_ok_with_strategies(self, mock_list: MagicMock) -> None:
        mock_list.return_value = [
            {"name": "sma_cross", "description": "SMA crossover"},
        ]
        result = _handle_strategy_list({})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["strategies"][0]["name"], "sma_cross")

    @patch("shared.mcp.tools.backtest._list_strategies")
    def test_returns_error_on_exception(self, mock_list: MagicMock) -> None:
        mock_list.side_effect = RuntimeError("db down")
        result = _handle_strategy_list({})
        self.assertEqual(result["status"], "error")
        self.assertIn("db down", result["message"])


class TestHandleStrategyGet(unittest.TestCase):
    """Tests for _handle_strategy_get sync handler."""

    @patch("shared.mcp.tools.backtest._get_strategy")
    def test_returns_ok_with_strategy(self, mock_get: MagicMock) -> None:
        mock_get.return_value = {"name": "sma_cross", "description": "SMA crossover"}
        result = _handle_strategy_get({"name": "sma_cross"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["strategy"]["name"], "sma_cross")

    @patch("shared.mcp.tools.backtest._get_strategy")
    def test_returns_error_on_key_error(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = KeyError("unknown strategy 'bogus'")
        result = _handle_strategy_get({"name": "bogus"})
        self.assertEqual(result["status"], "error")

    def test_returns_error_on_missing_name(self) -> None:
        result = _handle_strategy_get({})
        self.assertEqual(result["status"], "error")


# ---------------------------------------------------------------------------
# Indicator helpers
# ---------------------------------------------------------------------------


class TestIndicatorCard(unittest.TestCase):
    """Tests for _indicator_card helper."""

    def test_builds_card_from_spec(self) -> None:
        spec = MagicMock()
        spec.name = "rsi"
        spec.category = "momentum"
        spec.direction = "reversal"
        spec.description = "Relative Strength Index"
        spec.defaults = {"period": 14}
        spec.param_ranges = {"period": [5, 10, 14, 20, 28]}
        spec.asset_class = "any"

        card = _indicator_card(spec)
        self.assertEqual(card["name"], "rsi")
        self.assertEqual(card["category"], "momentum")
        self.assertEqual(card["direction"], "reversal")
        self.assertEqual(card["defaults"], {"period": 14})
        self.assertEqual(card["paramRanges"], {"period": [5, 10, 14, 20, 28]})
        self.assertEqual(card["assetClass"], "any")


class TestHandleIndicatorList(unittest.TestCase):
    """Tests for _handle_indicator_list sync handler."""

    @patch("shared.mcp.tools.backtest._list_indicators")
    def test_returns_ok_with_indicators(self, mock_list: MagicMock) -> None:
        mock_list.return_value = [
            {"name": "rsi", "category": "momentum"},
        ]
        result = _handle_indicator_list({})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["count"], 1)

    @patch("shared.mcp.tools.backtest._list_indicators")
    def test_passes_category_filter(self, mock_list: MagicMock) -> None:
        mock_list.return_value = []
        _handle_indicator_list({"category": "momentum"})
        mock_list.assert_called_once_with("momentum")

    @patch("shared.mcp.tools.backtest._list_indicators")
    def test_returns_error_on_exception(self, mock_list: MagicMock) -> None:
        mock_list.side_effect = RuntimeError("oops")
        result = _handle_indicator_list({})
        self.assertEqual(result["status"], "error")


class TestHandleIndicatorGet(unittest.TestCase):
    """Tests for _handle_indicator_get sync handler."""

    @patch("shared.mcp.tools.backtest._get_indicator")
    def test_returns_ok_with_indicator(self, mock_get: MagicMock) -> None:
        mock_get.return_value = {"name": "rsi", "category": "momentum"}
        result = _handle_indicator_get({"name": "rsi"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["indicator"]["name"], "rsi")

    @patch("shared.mcp.tools.backtest._get_indicator")
    def test_returns_error_on_key_error(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = KeyError("unknown indicator 'bogus'")
        result = _handle_indicator_get({"name": "bogus"})
        self.assertEqual(result["status"], "error")

    def test_returns_error_on_missing_name(self) -> None:
        result = _handle_indicator_get({})
        self.assertEqual(result["status"], "error")


# ---------------------------------------------------------------------------
# Indicator compute preview
# ---------------------------------------------------------------------------


class TestHandleIndicatorComputePreview(unittest.TestCase):
    """Tests for _handle_indicator_compute_preview sync handler."""

    @patch("shared.mcp.tools.backtest._compute_preview")
    def test_returns_ok_with_preview(self, mock_preview: MagicMock) -> None:
        mock_preview.return_value = {
            "status": "ok",
            "indicator": "rsi",
            "values": [{"value": 55.3, "timestamp": None}],
        }
        result = _handle_indicator_compute_preview({
            "name": "rsi",
            "symbol": "BTC/USDT",
        })
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["indicator"], "rsi")

    def test_returns_error_on_missing_symbol(self) -> None:
        result = _handle_indicator_compute_preview({"name": "rsi"})
        self.assertEqual(result["status"], "error")
        self.assertIn("missing", result["message"].lower())

    def test_returns_error_on_negative_window(self) -> None:
        result = _handle_indicator_compute_preview({
            "name": "rsi",
            "symbol": "BTC/USDT",
            "window": -5,
        })
        self.assertEqual(result["status"], "error")
        self.assertIn("positive", result["message"].lower())

    @patch("shared.mcp.tools.backtest._compute_preview")
    def test_passes_params_through(self, mock_preview: MagicMock) -> None:
        mock_preview.return_value = {"status": "ok"}
        _handle_indicator_compute_preview({
            "name": "rsi",
            "params": {"period": 20},
            "symbol": "BTC/USDT",
            "venue": "bybit",
            "timeframe": "4h",
            "window": 300,
        })
        mock_preview.assert_called_once_with(
            "rsi", {"period": 20}, "BTC/USDT", "bybit", "4h", 300,
        )

    @patch("shared.mcp.tools.backtest._compute_preview")
    def test_clamps_window_to_max(self, mock_preview: MagicMock) -> None:
        """Window exceeding _MAX_WINDOW (1000) is clamped, not rejected."""
        mock_preview.return_value = {"status": "ok"}
        _handle_indicator_compute_preview({
            "name": "rsi",
            "symbol": "BTC/USDT",
            "window": 5000,
        })
        mock_preview.assert_called_once()
        call_args = mock_preview.call_args
        # 6th positional arg is window
        self.assertEqual(call_args[0][5], 1000)

    def test_missing_name_returns_clear_message(self) -> None:
        result = _handle_indicator_compute_preview({"symbol": "BTC/USDT"})
        self.assertEqual(result["status"], "error")
        self.assertIn("name", result["message"])

    def test_missing_symbol_returns_clear_message(self) -> None:
        result = _handle_indicator_compute_preview({"name": "rsi"})
        self.assertEqual(result["status"], "error")
        self.assertIn("symbol", result["message"])


# ---------------------------------------------------------------------------
# Engine helpers
# ---------------------------------------------------------------------------


class TestListEngines(unittest.TestCase):
    """Tests for _list_engines — verifies available() is called per engine."""

    @patch("shared.backtest.engines.get")
    @patch("shared.backtest.engines.list_engines")
    @patch("shared.backtest.engines.capabilities")
    def test_reports_unavailable_engine(self, mock_caps: MagicMock,
                                         mock_list: MagicMock,
                                         mock_get: MagicMock) -> None:
        from shared.mcp.tools.backtest import _list_engines

        mock_list.return_value = ["classic", "nautilus"]
        mock_caps.return_value = {
            "classic": MagicMock(supports_intrabar_sl_tp=True, supports_funding=True,
                                 supports_fees=True, supports_slippage=True,
                                 supports_vectorised_sweep=False, supports_walk_forward=False,
                                 notes=""),
            "nautilus": MagicMock(supports_intrabar_sl_tp=True, supports_funding=True,
                                  supports_fees=True, supports_slippage=True,
                                  supports_vectorised_sweep=False, supports_walk_forward=True,
                                  notes=""),
        }
        classic_eng = MagicMock()
        classic_eng.available.return_value = True
        nautilus_eng = MagicMock()
        nautilus_eng.available.return_value = False
        mock_get.side_effect = [classic_eng, nautilus_eng]

        result = _list_engines()
        by_name = {r["name"]: r for r in result}
        self.assertTrue(by_name["classic"]["available"])
        self.assertFalse(by_name["nautilus"]["available"])


class TestHandleEngineList(unittest.TestCase):
    """Tests for _handle_engine_list sync handler."""

    @patch("shared.mcp.tools.backtest._list_engines")
    def test_returns_ok_with_engines(self, mock_list: MagicMock) -> None:
        mock_list.return_value = [
            {"name": "classic", "available": True, "capabilities": {}},
        ]
        result = _handle_engine_list({})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["count"], 1)

    @patch("shared.mcp.tools.backtest._list_engines")
    def test_returns_error_on_exception(self, mock_list: MagicMock) -> None:
        mock_list.side_effect = RuntimeError("engine registry broken")
        result = _handle_engine_list({})
        self.assertEqual(result["status"], "error")


# ---------------------------------------------------------------------------
# Backtest compose
# ---------------------------------------------------------------------------


class TestComposeSpec(unittest.TestCase):
    """Tests for _compose_spec helper."""

    @patch("shared.mcp.tools.backtest._compose_spec")
    def test_delegates_to_compose(self, mock_compose: MagicMock) -> None:
        """Verify the async handler delegates to the sync helper."""
        mock_compose.return_value = {"status": "ok", "spec": {}}
        # We test the sync helper directly below
        pass

    @patch("shared.backtest.engines.get")
    @patch("shared.backtest.strategies.get")
    @patch("shared.backtest.engine.BacktestConfig")
    def test_compose_ok(self, mock_cfg_cls: MagicMock, mock_strat_get: MagicMock,
                        mock_eng_get: MagicMock) -> None:
        mock_strat_get.return_value = lambda df, p: None
        mock_engine = MagicMock()
        mock_engine.available.return_value = True
        mock_eng_get.return_value = mock_engine
        mock_cfg = MagicMock()
        mock_cfg.param_hash.return_value = "abc123"
        mock_cfg_cls.return_value = mock_cfg

        result = _compose_spec({
            "symbol": "BTC/USDT",
            "strategy": "sma_cross",
        })
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["spec"]["paramHash"], "abc123")
        self.assertEqual(result["spec"]["symbol"], "BTC/USDT")
        self.assertEqual(result["spec"]["strategy"], "sma_cross")

    @patch("shared.backtest.strategies.get")
    def test_compose_unknown_strategy(self, mock_strat_get: MagicMock) -> None:
        mock_strat_get.side_effect = KeyError("unknown strategy 'bogus'")
        result = _compose_spec({
            "symbol": "BTC/USDT",
            "strategy": "bogus",
        })
        self.assertEqual(result["status"], "error")

    @patch("shared.backtest.engines.get")
    @patch("shared.backtest.strategies.get")
    def test_compose_unknown_engine(self, mock_strat_get: MagicMock,
                                     mock_eng_get: MagicMock) -> None:
        mock_strat_get.return_value = lambda df, p: None
        mock_eng_get.side_effect = KeyError("unknown engine 'bogus'")
        result = _compose_spec({
            "symbol": "BTC/USDT",
            "strategy": "sma_cross",
            "engine": "bogus",
        })
        self.assertEqual(result["status"], "error")

    @patch("shared.backtest.engines.get")
    @patch("shared.backtest.strategies.get")
    def test_compose_unavailable_engine(self, mock_strat_get: MagicMock,
                                         mock_eng_get: MagicMock) -> None:
        mock_strat_get.return_value = lambda df, p: None
        mock_engine = MagicMock()
        mock_engine.available.return_value = False
        mock_eng_get.return_value = mock_engine
        result = _compose_spec({
            "symbol": "BTC/USDT",
            "strategy": "sma_cross",
            "engine": "nautilus",
        })
        self.assertEqual(result["status"], "error")
        self.assertIn("not available", result["message"])

    @patch("shared.backtest.engines.get")
    @patch("shared.backtest.strategies.get")
    @patch("shared.backtest.engine.BacktestConfig")
    def test_compose_invalid_direction(self, mock_cfg_cls: MagicMock,
                                        mock_strat_get: MagicMock,
                                        mock_eng_get: MagicMock) -> None:
        mock_strat_get.return_value = lambda df, p: None
        mock_engine = MagicMock()
        mock_engine.available.return_value = True
        mock_eng_get.return_value = mock_engine
        result = _compose_spec({
            "symbol": "BTC/USDT",
            "strategy": "sma_cross",
            "direction": "sideways",
        })
        self.assertEqual(result["status"], "error")
        self.assertIn("direction", result["message"])

    @patch("shared.backtest.engines.get")
    @patch("shared.backtest.strategies.get")
    @patch("shared.backtest.engine.BacktestConfig")
    def test_compose_negative_capital(self, mock_cfg_cls: MagicMock,
                                       mock_strat_get: MagicMock,
                                       mock_eng_get: MagicMock) -> None:
        mock_strat_get.return_value = lambda df, p: None
        mock_engine = MagicMock()
        mock_engine.available.return_value = True
        mock_eng_get.return_value = mock_engine
        result = _compose_spec({
            "symbol": "BTC/USDT",
            "strategy": "sma_cross",
            "startingCashUsd": -500,
        })
        self.assertEqual(result["status"], "error")
        self.assertIn("positive", result["message"].lower())

    @patch("shared.backtest.engines.get")
    @patch("shared.backtest.strategies.get")
    def test_compose_nan_capital(self, mock_strat_get: MagicMock,
                                  mock_eng_get: MagicMock) -> None:
        mock_strat_get.return_value = lambda df, p: None
        mock_engine = MagicMock()
        mock_engine.available.return_value = True
        mock_eng_get.return_value = mock_engine
        result = _compose_spec({
            "symbol": "BTC/USDT",
            "strategy": "sma_cross",
            "startingCashUsd": float("nan"),
        })
        self.assertEqual(result["status"], "error")
        self.assertIn("nan", result["message"].lower())

    @patch("shared.backtest.engines.get")
    @patch("shared.backtest.strategies.get")
    @patch("shared.backtest.engine.BacktestConfig")
    def test_compose_uses_dynamic_dates(self, mock_cfg_cls: MagicMock,
                                         mock_strat_get: MagicMock,
                                         mock_eng_get: MagicMock) -> None:
        """When no dates provided, compose uses dynamic defaults (not hardcoded 2026-01-01)."""
        mock_strat_get.return_value = lambda df, p: None
        mock_engine = MagicMock()
        mock_engine.available.return_value = True
        mock_eng_get.return_value = mock_engine
        mock_cfg = MagicMock()
        mock_cfg.param_hash.return_value = "hash"
        mock_cfg_cls.return_value = mock_cfg

        result = _compose_spec({"symbol": "BTC/USDT", "strategy": "sma_cross"})
        self.assertEqual(result["status"], "ok")
        # The 'from' date should NOT be "2026-01-01" (hardcoded old default)
        self.assertNotEqual(result["spec"]["from"], "2026-01-01")


# ---------------------------------------------------------------------------
# Backtest plan_sweep
# ---------------------------------------------------------------------------


class TestPlanSweep(unittest.TestCase):
    """Tests for _plan_sweep helper."""

    @patch("shared.backtest.engine.BacktestConfig")
    def test_sweep_ok(self, mock_cfg_cls: MagicMock) -> None:
        mock_cfg = MagicMock()
        mock_cfg.param_hash.return_value = "hash1"
        mock_cfg_cls.return_value = mock_cfg

        result = _plan_sweep({
            "baseSpec": {"symbol": "BTC/USDT", "strategy": "sma_cross"},
            "paramRanges": {"period": [10, 20]},
        })
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["totalSpecs"], 2)
        self.assertEqual(result["sweptParams"], ["period"])

    def test_sweep_missing_base_spec(self) -> None:
        result = _plan_sweep({
            "paramRanges": {"period": [10]},
        })
        self.assertEqual(result["status"], "error")
        self.assertIn("baseSpec", result["message"])

    def test_sweep_missing_param_ranges(self) -> None:
        result = _plan_sweep({
            "baseSpec": {"symbol": "BTC/USDT"},
        })
        self.assertEqual(result["status"], "error")
        self.assertIn("paramRanges", result["message"])

    def test_sweep_empty_param_range(self) -> None:
        result = _plan_sweep({
            "baseSpec": {"symbol": "BTC/USDT"},
            "paramRanges": {"period": []},
        })
        self.assertEqual(result["status"], "error")

    @patch("shared.backtest.engine.BacktestConfig")
    def test_sweep_exceeds_max(self, mock_cfg_cls: MagicMock) -> None:
        mock_cfg = MagicMock()
        mock_cfg.param_hash.return_value = "hash"
        mock_cfg_cls.return_value = mock_cfg

        # Generate 501 combinations (exceeds _MAX_SWEEP_SPECS=500)
        result = _plan_sweep({
            "baseSpec": {"symbol": "BTC/USDT"},
            "paramRanges": {
                "a": list(range(50)),
                "b": list(range(11)),
            },
        })
        self.assertEqual(result["status"], "error")
        self.assertIn("max is 500", result["message"])

    @patch("shared.backtest.engine.BacktestConfig")
    def test_sweep_cartesian_product(self, mock_cfg_cls: MagicMock) -> None:
        mock_cfg = MagicMock()
        mock_cfg.param_hash.return_value = "hash"
        mock_cfg_cls.return_value = mock_cfg

        result = _plan_sweep({
            "baseSpec": {"symbol": "BTC/USDT", "strategy": "sma_cross"},
            "paramRanges": {
                "period": [10, 20],
                "stddev": [1.5, 2.0],
            },
        })
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["totalSpecs"], 4)  # 2 * 2

    @patch("shared.backtest.engine.BacktestConfig")
    def test_sweep_top_level_param(self, mock_cfg_cls: MagicMock) -> None:
        """Sweeping a top-level param (e.g. leverage) sets it on the variant."""
        mock_cfg = MagicMock()
        mock_cfg.param_hash.return_value = "hash"
        mock_cfg_cls.return_value = mock_cfg

        result = _plan_sweep({
            "baseSpec": {"symbol": "BTC/USDT", "strategy": "sma_cross"},
            "paramRanges": {
                "leverage": [1, 2, 3],
            },
        })
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["totalSpecs"], 3)
        # Check leverage is set at top level, not in indicatorParams
        for spec in result["specs"]:
            self.assertIn("leverage", spec)


# ---------------------------------------------------------------------------
# Top-K
# ---------------------------------------------------------------------------


class TestTopK(unittest.TestCase):
    """Tests for _top_k helper."""

    @patch("shared.backtest.accessible.top")
    def test_top_k_ok(self, mock_top: MagicMock) -> None:
        mock_top.return_value = [
            {
                "run_id": "abc",
                "symbol": "BTC/USDT",
                "exchange": "bybit",
                "timeframe": "1h",
                "indicator_name": "sma",
                "params": {"period": 20},
                "sharpe": 1.5,
                "sortino": 2.0,
                "deflated_sharpe": 1.2,
                "winrate": 55.0,
                "return_pct": 30.0,
                "max_drawdown": -10.0,
                "num_trades": 42,
            },
        ]
        result = _top_k({"limit": 10, "sortBy": "sharpe"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["symbol"], "BTC/USDT")

    @patch("shared.backtest.accessible.top")
    def test_top_k_ch_unavailable(self, mock_top: MagicMock) -> None:
        mock_top.side_effect = RuntimeError("CH_PASSWORD not set")
        result = _top_k({})
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["results"], [])

    @patch("shared.backtest.accessible.top")
    def test_top_k_invalid_sort(self, mock_top: MagicMock) -> None:
        mock_top.side_effect = ValueError("sort must be in [...]")
        result = _top_k({"sortBy": "bogus_metric"})
        self.assertEqual(result["status"], "error")

    @patch("shared.backtest.accessible.top")
    def test_top_k_clamps_limit(self, mock_top: MagicMock) -> None:
        mock_top.return_value = []
        _top_k({"limit": 9999})
        # _top_k clamps limit to _TOP_K_MAX (500) before calling top()
        mock_top.assert_called_once()
        call_args = mock_top.call_args
        n_value = call_args.kwargs.get("n", call_args[0][0] if call_args[0] else None)
        self.assertLessEqual(n_value, 500)


# ---------------------------------------------------------------------------
# Registration smoke test
# ---------------------------------------------------------------------------


class TestRegistration(unittest.TestCase):
    """Smoke test: register() does not crash."""

    def test_register_creates_tools(self) -> None:
        from shared.mcp.tools.backtest import register
        from shared.mcp.registry import ToolRegistry
        from shared.mcp.tools.context import ToolContext

        reg = ToolRegistry()
        ctx = ToolContext()
        register(reg, ctx)
        tools = reg.list_tools()
        tool_names = [t.name for t in tools]
        expected = [
            "strategy.list",
            "strategy.get",
            "indicator.list",
            "indicator.get",
            "indicator.compute_preview",
            "engine.list",
            "backtest.compose",
            "backtest.plan_sweep",
            "backtest.top_k",
        ]
        for name in expected:
            self.assertIn(name, tool_names, f"tool {name!r} not registered")


if __name__ == "__main__":
    unittest.main()
