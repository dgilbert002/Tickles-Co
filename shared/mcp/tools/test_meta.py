"""
Module: test_meta
Purpose: Unit tests for shared/mcp/tools/meta.py (M6 curious-agent loop).
Location: /opt/tickles/shared/mcp/tools/test_meta.py
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from shared.mcp.protocol import McpTool
from shared.mcp.registry import ToolRegistry
from shared.mcp.tools.context import ToolContext
from shared.mcp.tools.meta import (
    _content_hash,
    _fmt_dt,
    _group_tools,
    _match_suggestions,
    _parse_since,
    register,
)


def _make_tool(
    name: str = "test.tool",
    group: str = "test",
    status: str | None = None,
    enabled: bool = True,
    destructive: bool = False,
) -> McpTool:
    """Create a minimal McpTool for testing."""
    tags: dict = {"phase": "6", "group": group}
    if status:
        tags["status"] = status
    if destructive:
        tags["destructive"] = True
    return McpTool(
        name=name,
        description=f"Tool {name}",
        version="1",
        input_schema={"type": "object", "properties": {}},
        read_only=True,
        enabled=enabled,
        tags=tags,
    )


class TestGroupTools(unittest.TestCase):
    """Tests for _group_tools helper."""

    def test_groups_by_tag(self) -> None:
        tools = [
            _make_tool("a.x", group="alpha"),
            _make_tool("a.y", group="alpha"),
            _make_tool("b.x", group="beta"),
        ]
        result = _group_tools(tools)
        self.assertIn("alpha", result)
        self.assertIn("beta", result)
        self.assertEqual(len(result["alpha"]), 2)
        self.assertEqual(len(result["beta"]), 1)

    def test_uncategorised_when_no_group(self) -> None:
        tool = McpTool(
            name="orphan",
            description="no group",
            version="1",
            input_schema={"type": "object", "properties": {}},
            read_only=True,
            enabled=True,
            tags={},
        )
        result = _group_tools([tool])
        self.assertIn("uncategorised", result)

    def test_excludes_stubs(self) -> None:
        tools = [
            _make_tool("live.tool", group="g", status="live"),
            _make_tool("stub.tool", group="g", status="stub"),
        ]
        result = _group_tools(tools, include_stubs=False)
        names = [t["name"] for t in result["g"]]
        self.assertIn("live.tool", names)
        self.assertNotIn("stub.tool", names)

    def test_includes_stubs_by_default(self) -> None:
        tools = [
            _make_tool("stub.tool", group="g", status="stub"),
        ]
        result = _group_tools(tools, include_stubs=True)
        self.assertEqual(len(result["g"]), 1)

    def test_summary_contains_status(self) -> None:
        tools = [_make_tool("x", group="g", status="live")]
        result = _group_tools(tools)
        self.assertEqual(result["g"][0]["status"], "live")

    def test_summary_contains_destructive(self) -> None:
        tools = [_make_tool("x", group="g", destructive=True)]
        result = _group_tools(tools)
        self.assertTrue(result["g"][0]["destructive"])

    def test_empty_list(self) -> None:
        result = _group_tools([])
        self.assertEqual(result, {})


class TestMatchSuggestions(unittest.TestCase):
    """Tests for _match_suggestions helper."""

    def _registry_with(self, *names: str) -> ToolRegistry:
        """Build a registry with named tools enabled."""
        reg = ToolRegistry()
        for name in names:
            reg.register(
                McpTool(
                    name=name,
                    description=f"Tool {name}",
                    version="1",
                    input_schema={"type": "object", "properties": {}},
                    read_only=True,
                    enabled=True,
                    tags={"phase": "6", "group": "test"},
                ),
                lambda p, _n=name: {"status": "ok"},
            )
        return reg

    def test_candle_phrase_matches(self) -> None:
        reg = self._registry_with("md.candles", "md.quote")
        result = _match_suggestions("I need candle data for BTC", reg)
        names = [s["tool"] for s in result]
        self.assertIn("md.candles", names)

    def test_backtest_phrase_matches(self) -> None:
        reg = self._registry_with("backtest.compose", "strategy.list", "engine.list")
        result = _match_suggestions("run a backtest on sma_cross", reg)
        names = [s["tool"] for s in result]
        self.assertIn("backtest.compose", names)

    def test_disabled_tool_excluded(self) -> None:
        reg = ToolRegistry()
        tool = McpTool(
            name="md.candles",
            description="candles",
            version="1",
            input_schema={"type": "object", "properties": {}},
            read_only=True,
            enabled=False,
            tags={"phase": "6", "group": "data"},
        )
        reg.register(tool, lambda p: {"status": "ok"})
        result = _match_suggestions("show me candle data", reg)
        self.assertEqual(result, [])

    def test_unregistered_tool_skipped(self) -> None:
        reg = self._registry_with("md.candles")
        # "candle" rule also suggests md.quote, but it's not registered
        result = _match_suggestions("show me candle data", reg)
        names = [s["tool"] for s in result]
        self.assertIn("md.candles", names)
        self.assertNotIn("md.quote", names)

    def test_max_suggestions_cap(self) -> None:
        """Even with many matching rules, result is capped."""
        reg = self._registry_with(
            "md.candles", "md.quote", "backtest.compose", "strategy.list",
            "engine.list", "indicator.list", "indicator.compute_preview",
            "execution.submit", "execution.status", "banker.positions",
            "banker.snapshot", "wallet.paper.create", "memory.add",
            "autopsy.run",
        )
        # Use a description that matches many rules
        result = _match_suggestions(
            "candle backtest indicator order memory autopsy", reg
        )
        self.assertLessEqual(len(result), 12)

    def test_no_match_returns_empty(self) -> None:
        reg = self._registry_with("md.candles")
        result = _match_suggestions("something completely unrelated xyz", reg)
        self.assertEqual(result, [])

    def test_divergence_phrase(self) -> None:
        reg = self._registry_with(
            "indicator.compute_preview", "md.candles", "strategy.list"
        )
        result = _match_suggestions("find divergence on RSI", reg)
        names = [s["tool"] for s in result]
        self.assertIn("indicator.compute_preview", names)

    def test_dedup_across_rules(self) -> None:
        """Same tool suggested by multiple rules appears only once."""
        reg = self._registry_with("md.candles", "md.quote")
        # Both "candle" and "coverage" rules could match md.candles
        result = _match_suggestions("candle coverage gap", reg)
        names = [s["tool"] for s in result]
        self.assertEqual(names.count("md.candles"), 1)


class TestContentHash(unittest.TestCase):
    """Tests for _content_hash helper."""

    def test_deterministic(self) -> None:
        h1 = _content_hash("foo", "bar")
        h2 = _content_hash("foo", "bar")
        self.assertEqual(h1, h2)

    def test_different_inputs(self) -> None:
        h1 = _content_hash("foo", "bar")
        h2 = _content_hash("foo", "baz")
        self.assertNotEqual(h1, h2)

    def test_returns_hex(self) -> None:
        h = _content_hash("x", "y")
        self.assertTrue(all(c in "0123456789abcdef" for c in h))


class TestParseSince(unittest.TestCase):
    """Tests for _parse_since helper."""

    def test_default_is_30_days_ago(self) -> None:
        result = _parse_since(None)
        expected = datetime.now(tz=timezone.utc) - timedelta(days=30)
        delta = abs((result - expected).total_seconds())
        self.assertLess(delta, 5)

    def test_iso_string_parsed(self) -> None:
        result = _parse_since("2026-01-01T00:00:00+00:00")
        self.assertEqual(result.year, 2026)
        self.assertEqual(result.month, 1)

    def test_naive_datetime_gets_utc(self) -> None:
        result = _parse_since("2026-01-01T00:00:00")
        self.assertEqual(result.tzinfo, timezone.utc)

    def test_invalid_string_falls_back(self) -> None:
        result = _parse_since("not-a-date")
        expected = datetime.now(tz=timezone.utc) - timedelta(days=30)
        delta = abs((result - expected).total_seconds())
        self.assertLess(delta, 5)


class TestFmtDt(unittest.TestCase):
    """Tests for _fmt_dt helper."""

    def test_datetime_to_iso(self) -> None:
        dt = datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)
        result = _fmt_dt(dt)
        self.assertIn("2026-04-21", result)

    def test_none_returns_none(self) -> None:
        self.assertIsNone(_fmt_dt(None))

    def test_string_passthrough(self) -> None:
        self.assertEqual(_fmt_dt("already-string"), "already-string")


class TestHandleCatalogue(unittest.TestCase):
    """Tests for tools.catalogue handler."""

    def setUp(self) -> None:
        self.registry = ToolRegistry()
        self.ctx = ToolContext()
        # Register some test tools
        for name, group, status in [
            ("md.candles", "data", "live"),
            ("md.quote", "data", "live"),
            ("altdata.search", "data", "stub"),
            ("execution.submit", "trading", "live"),
        ]:
            self.registry.register(
                _make_tool(name, group=group, status=status),
                lambda p, _n=name: {"status": "ok"},
            )
        # Register meta tools
        register(self.registry, self.ctx)

    def test_returns_all_groups(self) -> None:
        handler = self.registry._handlers["tools.catalogue"]
        result = handler({})
        self.assertEqual(result["status"], "ok")
        self.assertIn("data", result["groups"])
        self.assertIn("trading", result["groups"])

    def test_filter_by_group(self) -> None:
        handler = self.registry._handlers["tools.catalogue"]
        result = handler({"group": "data"})
        self.assertIn("data", result["groups"])
        self.assertNotIn("trading", result["groups"])

    def test_exclude_stubs(self) -> None:
        handler = self.registry._handlers["tools.catalogue"]
        result = handler({"includeStubs": False})
        data_tools = result["groups"].get("data", [])
        names = [t["name"] for t in data_tools]
        self.assertNotIn("altdata.search", names)

    def test_include_disabled(self) -> None:
        # Add a disabled tool
        self.registry.register(
            _make_tool("disabled.tool", group="test", enabled=False),
            lambda p: {"status": "ok"},
        )
        handler = self.registry._handlers["tools.catalogue"]
        result = handler({"includeDisabled": True})
        all_names = []
        for group_tools in result["groups"].values():
            all_names.extend(t["name"] for t in group_tools)
        self.assertIn("disabled.tool", all_names)

    def test_total_tools_count(self) -> None:
        handler = self.registry._handlers["tools.catalogue"]
        result = handler({})
        self.assertGreater(result["total_tools"], 0)


class TestHandleSuggest(unittest.TestCase):
    """Tests for tools.suggest handler."""

    def setUp(self) -> None:
        self.registry = ToolRegistry()
        self.ctx = ToolContext()
        # Register tools that suggestion rules reference
        for name in [
            "md.candles", "md.quote", "backtest.compose",
            "strategy.list", "engine.list", "indicator.list",
            "indicator.compute_preview", "execution.submit",
            "execution.status", "banker.positions", "banker.snapshot",
            "memory.add", "memory.search", "autopsy.run",
            "candles.coverage", "candles.backfill",
        ]:
            self.registry.register(
                _make_tool(name, group="test"),
                lambda p, _n=name: {"status": "ok"},
            )
        register(self.registry, self.ctx)

    def test_suggests_candle_tools(self) -> None:
        handler = self.registry._handlers["tools.suggest"]
        result = handler({"taskDescription": "I need candle data"})
        self.assertEqual(result["status"], "ok")
        self.assertGreater(result["match_count"], 0)

    def test_missing_description_returns_error(self) -> None:
        handler = self.registry._handlers["tools.suggest"]
        result = handler({})
        self.assertEqual(result["status"], "error")
        self.assertIn("taskDescription", result["message"])

    def test_empty_description_returns_error(self) -> None:
        handler = self.registry._handlers["tools.suggest"]
        result = handler({"taskDescription": ""})
        self.assertEqual(result["status"], "error")

    def test_no_match_returns_empty_suggestions(self) -> None:
        handler = self.registry._handlers["tools.suggest"]
        result = handler({"taskDescription": "xyzzy nothing matches"})
        self.assertEqual(result["match_count"], 0)


class TestHandleRequestNew(unittest.TestCase):
    """Tests for tools.request_new handler."""

    def setUp(self) -> None:
        self.registry = ToolRegistry()
        self.ctx = ToolContext()
        register(self.registry, self.ctx)

    @patch("shared.mcp.tools.meta.db_helper")
    def test_creates_request(self, mock_db: MagicMock) -> None:
        mock_db.query.side_effect = [
            [],  # no existing request (dedup check)
            [{"id": 42}],  # fetch inserted id
        ]
        mock_db.execute.return_value = 1

        handler = self.registry._handlers["tools.request_new"]
        result = handler({
            "name": "onchain.whale_entries",
            "rationale": "Need to track whale wallets",
            "requestedBy": "agent-1",
        })
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["request_id"], 42)
        self.assertIn("CEO review", result["message"])

    @patch("shared.mcp.tools.meta.db_helper")
    def test_dedup_existing_request(self, mock_db: MagicMock) -> None:
        mock_db.query.return_value = [{"id": 7, "status": "open"}]

        handler = self.registry._handlers["tools.request_new"]
        result = handler({
            "name": "onchain.whale_entries",
            "rationale": "Need to track whale wallets",
        })
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["dedup"])
        self.assertEqual(result["request_id"], 7)

    def test_missing_name_returns_error(self) -> None:
        handler = self.registry._handlers["tools.request_new"]
        result = handler({"rationale": "some reason"})
        self.assertEqual(result["status"], "error")
        self.assertIn("name", result["message"])

    def test_missing_rationale_returns_error(self) -> None:
        handler = self.registry._handlers["tools.request_new"]
        result = handler({"name": "some.tool"})
        self.assertEqual(result["status"], "error")
        self.assertIn("rationale", result["message"])

    @patch("shared.mcp.tools.meta.db_helper")
    def test_db_error_returns_error(self, mock_db: MagicMock) -> None:
        mock_db.query.side_effect = RuntimeError("connection failed")

        handler = self.registry._handlers["tools.request_new"]
        result = handler({
            "name": "some.tool",
            "rationale": "some reason",
        })
        self.assertEqual(result["status"], "error")
        self.assertIn("database unavailable", result["message"])

    @patch("shared.mcp.tools.meta.db_helper")
    def test_stores_example_input_output(self, mock_db: MagicMock) -> None:
        mock_db.query.side_effect = [
            [],  # no existing
            [{"id": 1}],  # fetch id
        ]
        mock_db.execute.return_value = 1

        handler = self.registry._handlers["tools.request_new"]
        handler({
            "name": "test.tool",
            "rationale": "test",
            "exampleInput": {"symbol": "BTC/USDT"},
            "exampleOutput": {"price": 42000},
        })
        # Verify execute was called with JSON-encoded examples
        call_args = mock_db.execute.call_args
        params = call_args[0][1]  # second positional arg is the params tuple
        self.assertIsNotNone(params[2])  # example_input (JSON string)
        self.assertIsNotNone(params[3])  # example_output (JSON string)


class TestHandleUsageStats(unittest.TestCase):
    """Tests for tools.usage_stats handler."""

    def setUp(self) -> None:
        self.registry = ToolRegistry()
        self.ctx = ToolContext()
        register(self.registry, self.ctx)

    @patch("shared.mcp.tools.meta.db_helper")
    def test_returns_stats(self, mock_db: MagicMock) -> None:
        mock_db.query.return_value = [
            {
                "tool_name": "md.candles",
                "call_count": 150,
                "error_count": 3,
                "avg_latency_ms": 45.2,
                "last_called_at": datetime(2026, 4, 21, tzinfo=timezone.utc),
            },
        ]

        handler = self.registry._handlers["tools.usage_stats"]
        result = handler({})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["total_calls"], 150)
        self.assertEqual(len(result["tools"]), 1)
        self.assertEqual(result["tools"][0]["tool_name"], "md.candles")

    @patch("shared.mcp.tools.meta.db_helper")
    def test_passes_since_param(self, mock_db: MagicMock) -> None:
        mock_db.query.return_value = []

        handler = self.registry._handlers["tools.usage_stats"]
        handler({"since": "2026-01-01T00:00:00+00:00"})
        call_args = mock_db.query.call_args
        params = call_args[0][1]
        self.assertEqual(params[0].year, 2026)

    @patch("shared.mcp.tools.meta.db_helper")
    def test_empty_result(self, mock_db: MagicMock) -> None:
        mock_db.query.return_value = []

        handler = self.registry._handlers["tools.usage_stats"]
        result = handler({})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["total_calls"], 0)
        self.assertEqual(result["unique_tools"], 0)

    @patch("shared.mcp.tools.meta.db_helper")
    def test_db_error_returns_error(self, mock_db: MagicMock) -> None:
        mock_db.query.side_effect = RuntimeError("connection failed")

        handler = self.registry._handlers["tools.usage_stats"]
        result = handler({})
        self.assertEqual(result["status"], "error")
        self.assertIn("database unavailable", result["message"])


class TestRegistration(unittest.TestCase):
    """Smoke test: register() does not crash."""

    def test_register_creates_tools(self) -> None:
        reg = ToolRegistry()
        ctx = ToolContext()
        register(reg, ctx)
        tool_names = [t.name for t in reg.list_tools()]
        self.assertIn("tools.catalogue", tool_names)
        self.assertIn("tools.suggest", tool_names)
        self.assertIn("tools.request_new", tool_names)
        self.assertIn("tools.usage_stats", tool_names)

    def test_all_meta_tools_have_handlers(self) -> None:
        reg = ToolRegistry()
        ctx = ToolContext()
        register(reg, ctx)
        for tool in reg.list_tools():
            if tool.name.startswith("tools."):
                self.assertIn(tool.name, reg._handlers)


if __name__ == "__main__":
    unittest.main()
