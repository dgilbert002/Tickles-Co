"""
Module: test_budget_guard
Purpose: Unit tests for budget_guard.py — loop detection + soft spend warnings.
Location: /opt/tickles/shared/tests/test_budget_guard.py
"""

import pytest

from shared.intelligence.loop_detector import CallFingerprint, LoopDetectedError, LoopDetector
from shared.utils.budget_guard import BudgetExceededError, BudgetGuard, check_budget


class TestLoopDetector:
    """LoopDetector behavioral anomaly tests."""

    def test_identical_calls_raise_loop(self) -> None:
        """3 identical calls within the window must raise LoopDetectedError."""
        detector = LoopDetector()
        fp = CallFingerprint(
            role="vision", model="gpt-4", prompt_hash="abc123", image_hash=None, company_id="rubicon"
        )
        detector.record(fp)
        detector.record(fp)
        with pytest.raises(LoopDetectedError):
            detector.record(fp)

    def test_similar_calls_warn(self) -> None:
        """5 similar calls (same role+model+company, different prompt) warn but do not raise."""
        detector = LoopDetector()
        for i in range(4):
            fp = CallFingerprint(
                role="vision", model="gpt-4", prompt_hash=f"hash{i}", image_hash=None, company_id="rubicon"
            )
            is_anomaly, reason = detector.record(fp)
            assert not is_anomaly, f"call {i} should not be an anomaly"

        fp5 = CallFingerprint(
            role="vision", model="gpt-4", prompt_hash="hash4", image_hash=None, company_id="rubicon"
        )
        is_anomaly, reason = detector.record(fp5)
        assert is_anomaly is True
        assert "BURST DETECTED" in reason or "burst_detected" in reason

    def test_similar_burst_before_frequency(self) -> None:
        """Similar-call threshold (5) fires before frequency threshold (10).

        This is expected behaviour: the loop detector flags repeated calls
        to the same role+model+company at 5 calls, which is earlier than the
        per-minute frequency cap of 10.  Both are logged as anomalies.
        """
        detector = LoopDetector()
        for i in range(4):
            fp = CallFingerprint(
                role="text_extract", model="gpt-4", prompt_hash=f"h{i}",
                image_hash=None, company_id="rubicon",
            )
            is_anomaly, _ = detector.record(fp)
            assert not is_anomaly, f"call {i} should not be an anomaly"

        fp5 = CallFingerprint(
            role="text_extract", model="gpt-4", prompt_hash="h4",
            image_hash=None, company_id="rubicon",
        )
        is_anomaly, reason = detector.record(fp5)
        assert is_anomaly is True
        assert "BURST DETECTED" in reason or "burst_detected" in reason


class TestBudgetGuard:
    """BudgetGuard integration with LoopDetector."""

    @pytest.mark.anyio
    async def test_loop_blocks_call(self) -> None:
        """BudgetGuard.check_budget must raise BudgetExceededError on loop."""
        guard = BudgetGuard()
        # Seed 2 identical calls
        for _ in range(2):
            await guard.check_budget(
                role="vision", company_id="rubicon", model="gpt-4", prompt_hash="same_hash"
            )
        # 3rd identical call should raise
        with pytest.raises(BudgetExceededError):
            await guard.check_budget(
                role="vision", company_id="rubicon", model="gpt-4", prompt_hash="same_hash"
            )

    @pytest.mark.anyio
    async def test_no_block_on_unique_calls(self) -> None:
        """Unique calls should never raise BudgetExceededError."""
        guard = BudgetGuard()
        for i in range(10):
            await guard.check_budget(
                role="vision", company_id="rubicon", model="gpt-4", prompt_hash=f"hash{i}"
            )
