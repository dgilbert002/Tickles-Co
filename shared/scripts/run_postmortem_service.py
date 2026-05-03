"""
Module: run_postmortem_service
Purpose: CLI entry point for PostMortemService
Location: /opt/tickles/shared/scripts/run_postmortem_service.py
"""

import asyncio
import logging
import argparse
from shared.intelligence.postmortem_service import main

if __name__ == "__main__":
    asyncio.run(main())
