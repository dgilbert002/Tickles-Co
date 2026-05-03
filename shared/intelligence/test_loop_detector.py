"""Module: test_loop_detector
Purpose: Smoke tests for the LLM loop / burst detector.
Location: /opt/tickles/shared/intelligence/test_loop_detector.py
"""

import pytest

from shared.intelligence.loop_detector import (
    CallFingerprint,
    LoopDetectedError,
    LoopDetector,
)


class TestCallFingerprint:
    def test_identity_key_includes_all_fields(self):
        fp = CallFingerprint(
            role="vision",
            model="gpt-4o",
            prompt_hash="abc123",
            image_hash="phash456",
            company_id="rubicon",
        )
        key = fp.identity_key()
        assert "vision" in key
        assert "gpt-4o" in key
        assert "abc123" in key
        assert "phash456" in key
        assert "rubicon" in key

    def test_similarity_key_ignores_prompt(self):
        fp1 = CallFingerprint(
            role="vision", model="gpt-4o", prompt_hash="abc", image_hash=None, company_id="rubicon"
        )
        fp2 = CallFingerprint(
            role="vision", model="gpt-4o", prompt_hash="xyz", image_hash=None, company_id="rubicon"
        )
        assert fp1.similarity_key() == fp2.similarity_key()

    def test_similarity_key_differs_on_role(self):
        fp1 = CallFingerprint(
            role="vision", model="gpt-4o", prompt_hash="abc", image_hash=None, company_id="rubicon"
        )
        fp2 = CallFingerprint(
            role="text", model="gpt-4o", prompt_hash="abc", image_hash=None, company_id="rubicon"
        )
        assert fp1.similarity_key() != fp2.similarity_key()


class TestLoopDetector:
    def test_single_call_no_anomaly(self):
        det = LoopDetector()
        fp = CallFingerprint(
            role="vision", model="gpt-4o", prompt_hash="abc", image_hash=None, company_id="rubicon"
        )
        is_anomaly, reason = det.record(fp)
        assert is_anomaly is False
        assert reason is None

    def test_two_identical_calls_no_anomaly(self):
        det = LoopDetector()
        fp = CallFingerprint(
            role="vision", model="gpt-4o", prompt_hash="abc", image_hash=None, company_id="rubicon"
        )
        det.record(fp)
        is_anomaly, reason = det.record(fp)
        assert is_anomaly is False

    def test_three_identical_calls_raises_loop(self):
        det = LoopDetector()
        fp = CallFingerprint(
            role="vision", model="gpt-4o", prompt_hash="abc", image_hash=None, company_id="rubicon"
        )
        det.record(fp)
        det.record(fp)
        with pytest.raises(LoopDetectedError) as exc_info:
            det.record(fp)
        assert "LOOP DETECTED" in str(exc_info.value)
        assert "3 identical calls" in str(exc_info.value)

    def test_different_prompts_no_loop(self):
        det = LoopDetector()
        # 4 calls with different prompts — below similar threshold of 5
        for i in range(4):
            fp = CallFingerprint(
                role="vision",
                model="gpt-4o",
                prompt_hash=f"hash{i}",
                image_hash=None,
                company_id="rubicon",
            )
            is_anomaly, reason = det.record(fp)
            assert is_anomaly is False, f"Call {i} should not be anomaly"

    def test_similar_burst_detected(self):
        det = LoopDetector()
        # 5 calls with same role/model/company but different prompts
        for i in range(5):
            fp = CallFingerprint(
                role="vision",
                model="gpt-4o",
                prompt_hash=f"hash{i}",
                image_hash=None,
                company_id="rubicon",
            )
            is_anomaly, reason = det.record(fp)

        # 5th call triggers similar threshold (>= _LOOP_SIMILAR_THRESHOLD)
        assert is_anomaly is True
        assert "BURST DETECTED" in reason
        assert "5 similar calls" in reason

    def test_frequency_burst_per_minute(self):
        # Use a detector where frequency threshold (3) is LOWER than
        # similar threshold (100) so frequency fires first.
        det = LoopDetector(window_seconds=60)
        # Temporarily patch thresholds for this test
        import shared.intelligence.loop_detector as ld
        orig_similar = ld._LOOP_SIMILAR_THRESHOLD
        orig_freq = ld._FREQUENCY_BURST_THRESHOLD
        ld._LOOP_SIMILAR_THRESHOLD = 100  # never hit
        ld._FREQUENCY_BURST_THRESHOLD = 3  # hit on 3rd call
        try:
            for i in range(3):
                fp = CallFingerprint(
                    role="text",
                    model="gpt-4o",
                    prompt_hash=f"hash{i}",
                    image_hash=None,
                    company_id="rubicon",
                )
                is_anomaly, reason = det.record(fp)

            # 3rd call should trigger frequency burst
            assert is_anomaly is True
            assert "FREQUENCY BURST" in reason
            assert "3 calls/minute" in reason
        finally:
            ld._LOOP_SIMILAR_THRESHOLD = orig_similar
            ld._FREQUENCY_BURST_THRESHOLD = orig_freq

    def test_window_eviction(self):
        """Old calls outside the window should not count."""
        from datetime import datetime, timezone, timedelta

        det = LoopDetector(window_seconds=1)
        now = datetime.now(timezone.utc)

        # 3 identical calls, but first two are 2 seconds old
        fp = CallFingerprint(
            role="vision", model="gpt-4o", prompt_hash="abc", image_hash=None, company_id="rubicon"
        )
        fp_old = CallFingerprint(
            role="vision", model="gpt-4o", prompt_hash="abc", image_hash=None, company_id="rubicon",
            ts=now - timedelta(seconds=2),
        )

        det.record(fp_old)
        det.record(fp_old)
        # After eviction, only the old calls should be gone
        # But we need to trigger eviction by recording a new call
        is_anomaly, reason = det.record(fp)
        # Only 1 call in window now (the new one), so no loop
        assert is_anomaly is False

    def test_hash_prompt_truncates(self):
        long_prompt = "x" * 10000
        h = LoopDetector.hash_prompt(long_prompt)
        assert len(h) == 16
        # Should be deterministic
        assert LoopDetector.hash_prompt(long_prompt) == h

    def test_hash_prompt_short(self):
        prompt = "hello"
        h = LoopDetector.hash_prompt(prompt)
        assert len(h) == 16
        assert h != prompt
