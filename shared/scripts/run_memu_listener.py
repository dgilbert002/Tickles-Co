"""
Module: run_memu_listener
Purpose: CLI entry point for MemuListenerService
Location: /opt/tickles/shared/scripts/run_memu_listener.py
"""

import asyncio
import logging
from shared.memu.listener_service import main

if __name__ == "__main__":
    asyncio.run(main())
