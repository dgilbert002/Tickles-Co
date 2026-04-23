import os
import unittest
from unittest.mock import patch, MagicMock, call
from shared.utils.credentials import Credentials
from shared.execution.ccxt_adapter import CcxtExecutionAdapter

class TestAccountSwitching(unittest.TestCase):
    def setUp(self):
        # Setup environment variables for testing
        os.environ["BYBIT_MAIN_API_KEY"] = "main_key"
        os.environ["BYBIT_SCALP_API_KEY"] = "scalp_key"
        os.environ["BYBIT_SCALP_SANDBOX"] = "true"
        
    def test_credentials_lookup(self):
        # Test main account
        creds_main = Credentials.get("bybit", "main")
        self.assertEqual(creds_main["apiKey"], "main_key")
        
        # Test scalp account
        creds_scalp = Credentials.get("bybit", "scalp")
        self.assertEqual(creds_scalp["apiKey"], "scalp_key")
        
        # Test fallback
        os.environ["BINANCE_API_KEY"] = "binance_key"
        creds_binance = Credentials.get("binance", "main")
        self.assertEqual(creds_binance["apiKey"], "binance_key")

    @patch("ccxt.bybit")
    def test_adapter_caching(self, mock_bybit):
        # Configure mock to return different objects for each call
        mock_bybit.side_effect = [MagicMock(), MagicMock(), MagicMock()]
        
        adapter = CcxtExecutionAdapter(sandbox=False)
        
        # Get main client
        client_main = adapter._get_client("bybit", "main")
        self.assertEqual(len(adapter._clients), 1)
        self.assertIn("bybit:main", adapter._clients)
        
        # Get scalp client
        client_scalp = adapter._get_client("bybit", "scalp")
        self.assertEqual(len(adapter._clients), 2)
        self.assertIn("bybit:scalp", adapter._clients)
        
        # Verify they are different instances
        self.assertNotEqual(client_main, client_scalp)
        
        # Verify call arguments
        # First call (main)
        args_main = mock_bybit.call_args_list[0][0][0]
        self.assertEqual(args_main["apiKey"], "main_key")
        
        # Second call (scalp)
        args_scalp = mock_bybit.call_args_list[1][0][0]
        self.assertEqual(args_scalp["apiKey"], "scalp_key")

if __name__ == "__main__":
    unittest.main()
