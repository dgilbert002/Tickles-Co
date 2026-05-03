"""
Module: run_edge_scorer_service
Purpose: CLI entry point for EdgeScorerService
Location: /opt/tickles/shared/scripts/run_edge_scorer_service.py
"""

import asyncio
import logging
from shared.intelligence.edge_scorer_service import main

if __name__ == "__main__":
    asyncio.run(main())
