"""
Module: shared.utils.credentials
Purpose: Centralized credential management for multiple exchange accounts.
Location: /opt/tickles/shared/utils/credentials.py
"""

import os
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

class Credentials:
    """
    Helper to load exchange credentials from environment variables.
    Supports multiple accounts per exchange using the format:
    EXCHANGE_ACCOUNTNAME_API_KEY
    EXCHANGE_ACCOUNTNAME_API_SECRET
    
    If ACCOUNTNAME is omitted (e.g., BYBIT_API_KEY), it defaults to 'main'.
    """

    @staticmethod
    def get(exchange: str, account_name: str = "main") -> Dict[str, Any]:
        """
        Get credentials for a specific exchange and account.
        
        Args:
            exchange: Exchange ID (e.g., 'bybit', 'binance', 'capital')
            account_name: Account identifier (default: 'main')
            
        Returns:
            Dict with apiKey, secret, and other exchange-specific fields.
        """
        exchange = exchange.upper()
        account = account_name.upper()
        
        # Try specific account first: BYBIT_SCALP_API_KEY
        api_key = os.environ.get(f"{exchange}_{account}_API_KEY")
        api_secret = os.environ.get(f"{exchange}_{account}_API_SECRET")
        api_passphrase = os.environ.get(f"{exchange}_{account}_API_PASSPHRASE")
        
        # Fallback to default if account is 'main': BYBIT_API_KEY
        if not api_key and account == "MAIN":
            api_key = os.environ.get(f"{exchange}_API_KEY")
            api_secret = os.environ.get(f"{exchange}_API_SECRET")
            api_passphrase = os.environ.get(f"{exchange}_API_PASSPHRASE")

        # Special handling for Capital.com
        if exchange == "CAPITAL":
            email = os.environ.get(f"CAPITAL_{account}_EMAIL") or os.environ.get("CAPITAL_EMAIL")
            password = os.environ.get(f"CAPITAL_{account}_PASSWORD") or os.environ.get("CAPITAL_PASSWORD")
            account_id = os.environ.get(f"CAPITAL_{account}_ACCOUNT_ID")
            env = os.environ.get(f"CAPITAL_{account}_ENV") or os.environ.get("CAPITAL_ENV", "demo")
            
            if not api_key:
                api_key = os.environ.get("CAPITAL_API_KEY")
            
            return {
                "email": email,
                "password": password,
                "apiKey": api_key,
                "accountId": account_id,
                "environment": env
            }

        return {
            "apiKey": api_key,
            "secret": api_secret,
            "password": api_passphrase, # CCXT uses 'password' for passphrase
        }

    @staticmethod
    def is_paper(exchange: str, account_name: str = "main") -> bool:
        """Check if the account is configured for sandbox/testnet."""
        exchange = exchange.upper()
        account = account_name.upper()
        
        val = os.environ.get(f"{exchange}_{account}_SANDBOX")
        if val is None and account == "MAIN":
            val = os.environ.get(f"{exchange}_SANDBOX")
            
        return str(val).lower() in ("true", "1", "yes")
