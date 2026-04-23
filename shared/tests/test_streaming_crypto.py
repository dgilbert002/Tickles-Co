"""
Module: test_streaming_crypto
Purpose: Test WebSocket streaming from crypto exchanges using ccxt.pro.
Location: /opt/tickles/shared/tests/test_streaming_crypto.py
"""

import asyncio
import logging
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

# Ensure project root on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.utils.config import load_env
load_env()

import ccxt.pro as ccxtpro

async def test_exchange_streaming(exchange_id: str, symbol: str):
    print(f"\n>>> Testing streaming for {exchange_id} ({symbol})...")
    
    # Get credentials
    api_key = os.environ.get(f"{exchange_id.upper()}_API_KEY") or os.environ.get(f"{exchange_id.upper()}_DEMO_API_KEY")
    secret = os.environ.get(f"{exchange_id.upper()}_SECRET") or os.environ.get(f"{exchange_id.upper()}_DEMO_API_SECRET")
    
    exchange_class = getattr(ccxtpro, exchange_id)
    conf = {
        "enableRateLimit": True,
    }
    if api_key and secret:
        conf["apiKey"] = api_key
        conf["secret"] = secret
    
    # Special handling for sandbox
    if exchange_id == "bybit":
        conf["options"] = {"defaultType": "swap"}
        # Bybit sandbox often has issues with Pro, but let's try
        # conf["urls"] = {"api": exchange_class().urls["test"]}
    
    exchange = exchange_class(conf)
    
    try:
        print(f"Connecting to {exchange_id}...")
        # Try to watch ticker for 15 seconds
        start_time = time.time()
        count = 0
        while time.time() - start_time < 15:
            try:
                ticker = await exchange.watch_ticker(symbol)
                count += 1
                print(f"[{exchange_id}] Ticker update {count}: {ticker['symbol']} {ticker['last']}")
                if count >= 3:
                    break
            except Exception as e:
                print(f"[{exchange_id}] Error watching ticker: {e}")
                break
        
        if count > 0:
            print(f"✅ {exchange_id} streaming OK")
        else:
            print(f"❌ {exchange_id} streaming FAILED (no messages received)")
            
    finally:
        await exchange.close()

import time

async def main():
    exchanges = [
        ("bybit", "BTC/USDT:USDT"),
        ("binance", "BTC/USDT"),
        ("bitget", "BTC/USDT"),
    ]
    
    for eid, sym in exchanges:
        try:
            await test_exchange_streaming(eid, sym)
        except Exception as e:
            print(f"💥 {eid} crashed: {e}")

if __name__ == "__main__":
    asyncio.run(main())
