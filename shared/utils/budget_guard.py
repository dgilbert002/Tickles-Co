"""
Module: budget_guard
Purpose: Soft budget circuit-breaker for LLM calls — warns on anomaly, blocks on loop.
Location: /opt/tickles/shared/utils/budget_guard.py

Design:
  * Replaces hard daily USD caps with behavioral anomaly detection (loop + burst).
  * Provides the BudgetExceededError exception and check_budget() interface
    expected by gateway_config.py and other callers.
  * Delegates to LoopDetector for runaway-loop detection.
  * Delegates to llm_spend_tracker for visibility (dashboard-facing only).
  * All thresholds are env-driven; changing them requires a restart ([AC]).
"""

import logging
import os
from typing import Optional

from shared.intelligence.loop_detector import LoopDetectedError, LoopDetector
from shared.utils.llm_spend_tracker import get_spend_summary

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables (read at import — restart required to change)
# ---------------------------------------------------------------------------
_BUDGET_SOFT_USD_DAILY = float(os.environ.get("LLM_BUDGET_USD_DAILY_SOFT", "50.0"))
_BUDGET_HARD_USD_DAILY = float(os.environ.get("LLM_BUDGET_USD_DAILY_HARD", "200.0"))


class BudgetExceededError(Exception):
    """Raised when a call breaches the hard daily budget or loop threshold.

    Callers (e.g. gateway_config.py) should catch this and:
      1. Log a row to api_cost_log with success=FALSE, http_status=429.
      2. Return a cached / error response instead of burning an API call.
    """
    pass


class BudgetGuard:
    """Stateful guard that combines loop detection with soft spend warnings.

    One instance per process is typical; the loop detector is in-memory only.
    """

    def __init__(self) -> None:
        self._loop_detector = LoopDetector()

    async def check_budget(
        self,
        *,
        role: str,
        company_id: Optional[str] = None,
        model: Optional[str] = None,
        prompt_hash: Optional[str] = None,
        image_hash: Optional[str] = None,
    ) -> None:
        """Pre-call budget / anomaly check.

        Raises BudgetExceededError if the call should be blocked.
        Logs a warning if spend is above the soft threshold but below hard.

        Args:
            role: Logical role of the call (e.g. 'vision', 'text_extract').
            company_id: Company scope for the call.
            model: Model identifier (for loop fingerprint).
            prompt_hash: SHA-256 of prompt text (for loop fingerprint).
            image_hash: Perceptual hash of image (for loop fingerprint).

        Raises:
            BudgetExceededError: When hard budget or loop threshold is breached.
        """
        # 1. Loop / burst detection (behavioral anomaly)
        if prompt_hash is not None:
            from shared.intelligence.loop_detector import CallFingerprint

            fp = CallFingerprint(
                role=role,
                model=model or "unknown",
                prompt_hash=prompt_hash,
                image_hash=image_hash,
                company_id=company_id,
            )
            try:
                is_anomaly, reason = self._loop_detector.record(fp)
                if is_anomaly:
                    logger.warning(
                        "BudgetGuard: anomaly detected for %s/%s — %s",
                        role, company_id, reason,
                    )
            except LoopDetectedError as exc:
                logger.error("BudgetGuard: loop detected — blocking call: %s", exc)
                raise BudgetExceededError(str(exc)) from exc

        # 2. Soft spend warning (dashboard-facing, non-blocking)
        try:
            summary = await get_spend_summary(period="day", company_id=company_id)
            if summary.total_usd >= _BUDGET_HARD_USD_DAILY:
                msg = (
                    f"Hard daily budget exceeded: ${summary.total_usd:.2f} "
                    f"(limit ${_BUDGET_HARD_USD_DAILY:.2f}) for {company_id or 'all'}"
                )
                logger.error(msg)
                raise BudgetExceededError(msg)
            if summary.total_usd >= _BUDGET_SOFT_USD_DAILY:
                logger.warning(
                    "Soft daily budget at %.0f%%: $%.2f / $%.2f for %s",
                    (summary.total_usd / _BUDGET_SOFT_USD_DAILY) * 100,
                    summary.total_usd,
                    _BUDGET_SOFT_USD_DAILY,
                    company_id or "all",
                )
        except Exception as exc:
            # Never block on spend-tracker failure — log and continue
            logger.warning("BudgetGuard: spend tracker query failed: %s", exc)


# Process-global singleton (stateless across restarts by design)
_default_guard: Optional[BudgetGuard] = None


def get_budget_guard() -> BudgetGuard:
    """Return the process-global BudgetGuard instance."""
    global _default_guard
    if _default_guard is None:
        _default_guard = BudgetGuard()
    return _default_guard


async def check_budget(
    *,
    role: str,
    company_id: Optional[str] = None,
    model: Optional[str] = None,
    prompt_hash: Optional[str] = None,
    image_hash: Optional[str] = None,
) -> None:
    """Convenience wrapper using the global BudgetGuard.

    Args:
        role: Logical role of the call.
        company_id: Company scope.
        model: Model identifier.
        prompt_hash: SHA-256 of prompt text.
        image_hash: Perceptual hash of image.

    Raises:
        BudgetExceededError: When hard budget or loop threshold is breached.
    """
    guard = get_budget_guard()
    await guard.check_budget(
        role=role,
        company_id=company_id,
        model=model,
        prompt_hash=prompt_hash,
        image_hash=image_hash,
    )
