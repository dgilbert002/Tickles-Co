"""
Module: opinion_budget
Purpose: [AY] Cross-trader rate budget for OpinionService.
Location: /opt/tickles/shared/intelligence/opinion_budget.py
"""

import asyncio
import logging
import os
import time
from collections import defaultdict
from typing import Tuple

logger = logging.getLogger(__name__)

# Constants from environment
_PER_HOUR = int(os.environ.get("OPINION_GLOBAL_BUDGET_PER_HOUR", "120"))
_USD_PER_DAY = float(os.environ.get("OPINION_GLOBAL_BUDGET_USD_PER_DAY", "10.0"))
_PER_POS_PER_HOUR = int(os.environ.get("OPINION_PER_POSITION_PER_HOUR", "2"))


class OpinionBudget:
    """
    Three orthogonal limits prevent any one of them from being a single point of failure:
    global hour cap, per-position hour cap, daily USD cap.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._global_calls: list[float] = []  # timestamps within last hour
        self._per_pos: dict[int, list[float]] = defaultdict(list)
        self._usd_today: float = 0.0
        self._usd_day_start: float = time.time()

    async def try_acquire(self, position_id: int) -> Tuple[bool, str]:
        """
        Attempt to acquire budget for a single opinion call.
        Returns (success, reason).
        """
        now = time.time()
        async with self._lock:
            try:
                # roll global hour window
                self._global_calls = [t for t in self._global_calls if now - t < 3600]
                if len(self._global_calls) >= _PER_HOUR:
                    return False, f"global_budget_exhausted: {_PER_HOUR}/hour"

                # roll per-position hour window
                posts = [t for t in self._per_pos[position_id] if now - t < 3600]
                self._per_pos[position_id] = posts
                if len(posts) >= _PER_POS_PER_HOUR:
                    return False, f"per_position_budget_exhausted: {_PER_POS_PER_HOUR}/hour"

                # roll USD day window
                if now - self._usd_day_start > 86400:
                    self._usd_today = 0.0
                    self._usd_day_start = now
                if self._usd_today >= _USD_PER_DAY:
                    return False, f"usd_budget_exhausted: ${_USD_PER_DAY}/day"

                self._global_calls.append(now)
                self._per_pos[position_id].append(now)
                return True, "ok"
            except Exception as e:
                logger.exception("Failed to check opinion budget: %s", e)
                return False, f"error: {str(e)}"

    async def record_cost(self, usd: float) -> None:
        """Record the actual USD cost of an LLM call."""
        async with self._lock:
            try:
                self._usd_today += usd
            except Exception as e:
                logger.exception("Failed to record opinion cost: %s", e)


_BUDGET = OpinionBudget()


def get_opinion_budget() -> OpinionBudget:
    """Return the process-global OpinionBudget instance."""
    return _BUDGET
