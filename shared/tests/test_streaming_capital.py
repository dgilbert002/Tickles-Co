"""
Module: test_streaming_capital
Purpose: Test WebSocket streaming from Capital.com.
Location: /opt/tickles/shared/tests/test_streaming_capital.py
"""

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

import aiohttp

# Ensure project root on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.utils.config import load_env
load_env()

from shared.connectors.capital_adapter import CapitalAdapter

async def test_capital_streaming():
    print("\n>>> Testing Capital.com streaming...")
    
    email = os.environ.get("CAPITAL_EMAIL")
    password = os.environ.get("CAPITAL_PASSWORD")
    api_key = os.environ.get("CAPITAL_API_KEY")
    
    if not all([email, password, api_key]):
        print("❌ Missing Capital.com credentials")
        return

    adapter = CapitalAdapter(environment="demo")
    try:
        print("Authenticating via REST...")
        success = await adapter.authenticate(email, password, api_key)
        if not success:
            print("❌ REST Authentication failed")
            return
            
        cst = adapter._cst
        security_token = adapter._x_security_token
        
        ws_url = "wss://api-streaming-capital.backend-capital.com/connect"
        print(f"Connecting to WebSocket: {ws_url}")
        
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url) as ws:
                print("WebSocket connected")
                
                # Subscribe to BTCUSD
                sub_msg = {
                    "destination": "OHLCMarketData.subscribe",
                    "correlationId": f"sub_{int(time.time())}",
                    "cst": cst,
                    "securityToken": security_token,
                    "payload": {
                        "epics": ["BTCUSD"],
                        "resolutions": ["MINUTE"],
                        "type": "classic"
                    }
                }
                
                await ws.send_str(json.dumps(sub_msg))
                print("Subscription message sent")
                
                start_time = time.time()
                count = 0
                while time.time() - start_time < 30:
                    try:
                        msg = await ws.receive(timeout=5)
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            if data.get("destination") == "ohlc.event":
                                count += 1
                                payload = data.get("payload", {})
                                print(f"[Capital] Candle update {count}: {payload.get('epic')} {payload.get('c')}")
                                if count >= 3:
                                    break
                            elif data.get("status") == "ERROR":
                                print(f"❌ WebSocket Error: {data}")
                                break
                            else:
                                print(f"Received: {data.get('destination') or data}")
                        elif msg.type == aiohttp.WSMsgType.CLOSED:
                            print("WebSocket closed")
                            break
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            print("WebSocket error")
                            break
                    except asyncio.TimeoutError:
                        print("Waiting for messages...")
                        continue
                
                if count > 0:
                    print("✅ Capital.com streaming OK")
                else:
                    print("❌ Capital.com streaming FAILED (no messages received)")
                    
    finally:
        await adapter.close()

if __name__ == "__main__":
    asyncio.run(test_capital_streaming())
