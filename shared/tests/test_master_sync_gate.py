"""
Module: test_master_sync_gate
Purpose: Smoke tests for master-sync CI gate
Location: /opt/tickles/shared/tests/test_master_sync_gate.py
"""

import unittest
from unittest.mock import patch, MagicMock
from shared.scripts.master_sync import gate_fresh

class TestMasterSyncGate(unittest.TestCase):
    @patch("subprocess.run")
    def test_gate_fresh_calls_psql(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        
        gate_fresh("postgresql://user:pass@localhost/db")
        
        # Should call psql twice (shared + company)
        self.assertEqual(mock_run.call_count, 2)
        args1 = mock_run.call_args_list[0][0][0]
        self.assertIn("psql", args1)
        self.assertIn("shared_gate.sql", args1[-1])

        args2 = mock_run.call_args_list[1][0][0]
        self.assertIn("psql", args2)
        self.assertIn("company_gate.sql", args2[-1])

if __name__ == "__main__":
    unittest.main()
