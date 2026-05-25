"""
Module: opinion_budget
Purpose: [AY] Cross-trader rate budget for OpinionService.
Location: /opt/tickles/shared/intelligence/opinion_budget.py
"""

import asyncio
import itertools
import logging
import os
import time
from collections import defaultdict
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Constants from environment
_PER_HOUR = int(os.environ.get("OPINION_GLOBAL_BUDGET_PER_HOUR", "120"))
_USD_PER_DAY = float(os.environ.get("OPINION_GLOBAL_BUDGET_USD_PER_DAY", "10.0"))
_PER_POS_PER_HOUR = int(os.environ.get("OPINION_PER_POSITION_PER_HOUR", "2"))


class OpinionBudget:
    """
    Three orthogonal limits prevent any one of them from being a single point
    of failure: global hour cap, per-position hour cap, daily USD cap.

    Round-6 sweep (BH2 #3, CA1 #3 follow-up): replaced LIFO ``pop()``-based
    refund with a token-keyed slot model. ``try_acquire`` returns an opaque
    integer token that uniquely identifies the slot it consumed; ``release``
    accepts that token and removes exactly the matching slot. This makes the
    budget safe under interleaved acquires (which is theoretically possible
    today only if anyone removes the chart-hacker advisory lock or runs
    ``OpinionBudget`` from a parallel asyncio context — but it's a free
    correctness upgrade with no behaviour change in the sequential case).
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # Round-6: tokens live in the deques alongside the timestamp so we
        # can locate-and-remove by token.
        self._global_calls: list[Tuple[int, float]] = []
        self._per_pos: dict[int, list[Tuple[int, float]]] = defaultdict(list)
        self._usd_today: float = 0.0
        self._usd_day_start: float = time.time()
        self._token_counter = itertools.count(1)

    async def try_acquire(self, position_id: int) -> Tuple[bool, str, Optional[int]]:
        """Attempt to acquire budget for a single opinion call.

        Returns ``(success, reason, token)``. ``token`` is non-None on success
        and is required by ``release(token)`` to undo this exact slot.
        """
        now = time.time()
        async with self._lock:
            try:
                # roll global hour window
                self._global_calls = [
                    (tok, t) for tok, t in self._global_calls if now - t < 3600
                ]
                if len(self._global_calls) >= _PER_HOUR:
                    return False, f"global_budget_exhausted: {_PER_HOUR}/hour", None

                # roll per-position hour window
                posts = [
                    (tok, t) for tok, t in self._per_pos[position_id] if now - t < 3600
                ]
                self._per_pos[position_id] = posts
                if len(posts) >= _PER_POS_PER_HOUR:
                    return False, f"per_position_budget_exhausted: {_PER_POS_PER_HOUR}/hour", None

                # roll USD day window
                if now - self._usd_day_start > 86400:
                    self._usd_today = 0.0
                    self._usd_day_start = now
                if self._usd_today >= _USD_PER_DAY:
                    return False, f"usd_budget_exhausted: ${_USD_PER_DAY}/day", None

                token = next(self._token_counter)
                self._global_calls.append((token, now))
                self._per_pos[position_id].append((token, now))
                return True, "ok", token
            except Exception as e:
                logger.exception("Failed to check opinion budget: %s", e)
                return False, f"error: {str(e)}", None

    async def record_cost(self, usd: float) -> None:
        """Record the actual USD cost of an LLM call."""
        async with self._lock:
            try:
                self._usd_today += usd
            except Exception as e:
                logger.exception("Failed to record opinion cost: %s", e)

    async def release(self, position_id: int, token: Optional[int] = None) -> bool:
        """Undo a previous ``try_acquire`` slot consumption.

        Round-6 sweep (BH2 #3, CA1 #3): now token-keyed. Pass the token
        returned by ``try_acquire``. The slot with that exact token is
        removed from both the global and per-position deques. Returns
        ``True`` if a slot was removed, ``False`` if no matching token was
        found (e.g. the slot was already evicted by the rolling window).

        Backward-compat: if ``token`` is None, falls back to the legacy
        LIFO ``pop()`` behaviour for callers that haven't been updated.

        Why this matters: the chart-hacker opinion service calls
        ``try_acquire`` BEFORE the LLM call, but later rejects the response
        (empty memo, JSON parse failure, post-LLM exception). Without a
        refund the slot is permanently lost and a stream of garbage LLM
        responses can exhaust the 120/h global cap and the 2/h per-position
        cap with zero rows written to ``agent_opinions``.

        Daily USD is NOT refunded — that's tracked only via
        ``record_cost`` which the rejection paths skip anyway, so an LLM
        call that returned bad JSON only affects USD if the gateway
        populated ``cost_usd`` in the response.
        """
        async with self._lock:
            try:
                if token is None:
                    # Legacy fallback — last-in-first-out.
                    removed = False
                    if self._global_calls:
                        self._global_calls.pop()
                        removed = True
                    pos_window = self._per_pos.get(position_id)
                    if pos_window:
                        pos_window.pop()
                        removed = True
                    return removed

                # Token-keyed removal. Each list is short (≤ cap), so linear
                # scan is fine.
                removed_global = False
                for i, (tok, _ts) in enumerate(self._global_calls):
                    if tok == token:
                        del self._global_calls[i]
                        removed_global = True
                        break

                removed_pos = False
                pos_window = self._per_pos.get(position_id)
                if pos_window:
                    for i, (tok, _ts) in enumerate(pos_window):
                        if tok == token:
                            del pos_window[i]
                            removed_pos = True
                            break

                return removed_global or removed_pos
            except Exception as e:
                logger.exception("Failed to release opinion budget slot: %s", e)
                return False


_BUDGET = OpinionBudget()


def get_opinion_budget() -> OpinionBudget:
    """Return the process-global OpinionBudget instance."""
    return _BUDGET
