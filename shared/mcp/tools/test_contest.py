"""
Module: test_contest
Purpose: Smoke tests for contest MCP tools
Location: /opt/tickles/shared/mcp/tools/test_contest.py
"""

import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone
from shared.mcp.tools.contest import (
    _handle_contest_create,
    _handle_contest_join,
    _handle_contest_leaderboard,
    _handle_contest_end
)

class TestContestTools(unittest.TestCase):

    @patch("shared.mcp.tools.db_helper.execute")
    def test_contest_create(self, mock_execute):
        params = {
            "name": "Test Contest",
            "companyId": "testcorp",
            "agentIds": ["agent1", "agent2"],
            "venues": ["bybit"],
            "durationDays": 1
        }
        res = _handle_contest_create(params)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["name"], "Test Contest")
        self.assertEqual(len(res["agentsProvisioned"]), 2)
        self.assertTrue(mock_execute.called)

    @patch("shared.mcp.tools.db_helper.query")
    @patch("shared.mcp.tools.db_helper.execute")
    def test_contest_join(self, mock_execute, mock_query):
        mock_query.return_value = [{"venues": ["bybit"], "starting_balance_usd": 10000.0}]
        params = {
            "contestId": "c123",
            "companyId": "testcorp",
            "agentId": "agent3"
        }
        res = _handle_contest_join(params)
        self.assertEqual(res["status"], "ok")
        self.assertTrue(mock_execute.called)

    @patch("shared.mcp.tools.db_helper.query")
    def test_contest_leaderboard(self, mock_query):
        # Mock contest info
        mock_query.side_effect = [
            [{"name": "Test Contest", "starting_balance_usd": 10000.0, "status": "active"}],
            [
                {"agent_id": "agent1", "company_id": "testcorp", "strategy_ref": "strat1", "total_equity": 11000.0},
                {"agent_id": "agent2", "company_id": "testcorp", "strategy_ref": "strat2", "total_equity": 9500.0}
            ]
        ]
        params = {"contestId": "c123"}
        res = _handle_contest_leaderboard(params)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(len(res["rankings"]), 2)
        self.assertEqual(res["rankings"][0]["agentId"], "agent1")
        self.assertEqual(res["rankings"][0]["pnlUsd"], 1000.0)
        self.assertEqual(res["rankings"][1]["pnlUsd"], -500.0)

    @patch("shared.mcp.tools.db_helper.execute")
    def test_contest_end(self, mock_execute):
        mock_execute.return_value = 1
        params = {"contestId": "c123"}
        res = _handle_contest_end(params)
        self.assertEqual(res["status"], "ok")
        self.assertTrue(mock_execute.called)

if __name__ == "__main__":
    unittest.main()
