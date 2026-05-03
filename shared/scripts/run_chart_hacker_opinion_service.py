"""
Module: run_chart_hacker_opinion_service
Purpose: CLI entry point for ChartHackerOpinionService
Location: /opt/tickles/shared/scripts/run_chart_hacker_opinion_service.py
"""

import asyncio
import logging
from shared.intelligence.chart_hacker_opinion_service import main

if __name__ == "__main__":
    asyncio.run(main())
