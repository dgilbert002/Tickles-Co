"""
Module: run_coach_service
Purpose: CLI entry point for CoachService
Location: /opt/tickles/shared/scripts/run_coach_service.py
"""

import asyncio
import logging
from shared.intelligence.coach_service import main

if __name__ == "__main__":
    asyncio.run(main())
