"""
Module: shared.scripts.audit_capital_accounts
Purpose: Audit all Capital.com accounts and report balances, equity, and positions.
Location: /opt/tickles/shared/scripts/audit_capital_accounts.py
"""

import asyncio
import logging
import os
from typing import Dict, Any, List

from shared.connectors.capital_adapter import CapitalAdapter
from shared.utils.credentials import Credentials
from shared.utils.config import load_env

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

async def audit_environment(env: str):
    """Audit all accounts in a specific environment (live/demo)."""
    logger.info(f"Starting audit for {env} environment...")
    
    # Get main credentials to authenticate
    creds = Credentials.get("capital", "main")
    adapter = CapitalAdapter(environment=env)
    
    try:
        # 1. Authenticate
        success = await adapter.authenticate(
            creds["email"],
            creds["password"],
            creds["apiKey"]
        )
        if not success:
            logger.error(f"Failed to authenticate in {env}")
            return

        # 2. Fetch all accounts
        balance_data = await adapter.fetch_balance()
        accounts = balance_data.get("info", {}).get("accounts", [])
        
        if not accounts:
            logger.warning(f"No accounts found in {env}")
            return

        print(f"\n{'='*100}")
        print(f" CAPITAL.COM {env.upper()} ACCOUNTS AUDIT")
        print(f"{'='*100}")
        print(f"{'Account Name':<20} | {'Account ID':<20} | {'Balance':<12} | {'Equity':<12} | {'Assets Held'}")
        print(f"{'-'*100}")

        for acc in accounts:
            acc_id = str(acc.get("accountId"))
            acc_name = acc.get("accountName", "Unknown")
            
            logger.info(f"Auditing account: {acc_name} ({acc_id})")
            
            # 3. Switch to this account to get positions
            await adapter.switch_account(acc_id)
            
            # 4. Fetch positions
            positions = await adapter.fetch_positions()
            assets = [p["symbol"] for p in positions]
            assets_str = ", ".join(assets) if assets else "None"
            
            # 5. Get balance/equity
            bal_info = acc.get("balance", {})
            balance = float(bal_info.get("balance", 0))
            equity = float(bal_info.get("equity", 0))
            currency = acc.get("currency", "USD")
            
            print(f"{acc_name:<20} | {acc_id:<20} | {balance:>9.2f} {currency} | {equity:>9.2f} {currency} | {assets_str}", flush=True)

        print(f"{'='*100}\n")

    except Exception as e:
        logger.error(f"Error auditing {env}: {e}")
    finally:
        await adapter.close()

async def main():
    load_env()
    await audit_environment("live")
    await audit_environment("demo")

if __name__ == "__main__":
    asyncio.run(main())
