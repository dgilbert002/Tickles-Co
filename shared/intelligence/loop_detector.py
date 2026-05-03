"""Module: loop_detector
Purpose: Behavioral anomaly detection for LLM calls — loop + burst detection.
Location: /opt/tickles/shared/intelligence/loop_detector.py
"""

import hashlib
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# --- tunables (env or config) ---
_LOOP_WINDOW_SECONDS = 300          # 5-minute lookback for loop detection
_LOOP_IDENTICAL_THRESHOLD = 3       # flag after 3 identical calls in window
_LOOP_SIMILAR_THRESHOLD = 5         # flag after 5 similar calls in window
_FREQUENCY_BURST_THRESHOLD = 10     # flag after 10 calls / minute for same role


@dataclass
class CallFingerprint:
    """Lightweight hash of an LLM call for deduplication / loop detection."""
    role: str
    model: str
    prompt_hash: str          # SHA-256 of the prompt text (first 4 KB)
    image_hash: Optional[str]  # perceptual hash if vision call
    company_id: Optional[str]
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def identity_key(self) -> str:
        """Exact-match key: same role + model + prompt + image + company."""
        return (
            f"{self.role}:{self.model}:{self.prompt_hash}:"
            f"{self.image_hash or ''}:{self.company_id or ''}"
        )

    def similarity_key(self) -> str:
        """Similarity key: same role + model + company (ignores prompt content)."""
        return f"{self.role}:{self.model}:{self.company_id or ''}"


class LoopDetectedError(Exception):
    """Raised when a loop or burst is detected. Caller should back off."""
    pass


class LoopDetector:
    """In-memory sliding-window tracker. Stateless across restarts —
    persistent audit trail lives in api_cost_log.
    """

    def __init__(self, window_seconds: int = _LOOP_WINDOW_SECONDS):
        self.window = timedelta(seconds=window_seconds)
        self._calls: List[CallFingerprint] = []          # chronological
        self._identical_counts: Dict[str, int] = defaultdict(int)
        self._similar_counts: Dict[str, int] = defaultdict(int)

    def record(self, fp: CallFingerprint) -> Tuple[bool, Optional[str]]:
        """Record a call and return (is_anomaly, reason_or_None).

        Raises LoopDetectedError if the call breaches the identical-call
        threshold (runaway loop).  Returns (True, reason) for burst /
        frequency anomalies that should be logged but not necessarily
        blocked.

        Args:
            fp: Fingerprint of the LLM call about to be made.

        Returns:
            (is_anomaly, reason_or_None).  If is_anomaly is True and
            reason is not None, a burst was detected but not blocked.

        Raises:
            LoopDetectedError: When identical calls exceed threshold
                within the window — indicates a runaway loop.
        """
        now = fp.ts
        cutoff = now - self.window

        # Evict old calls outside the window
        while self._calls and self._calls[0].ts < cutoff:
            old = self._calls.pop(0)
            self._identical_counts[old.identity_key()] -= 1
            self._similar_counts[old.similarity_key()] -= 1

        # Record new call
        self._calls.append(fp)
        self._identical_counts[fp.identity_key()] += 1
        self._similar_counts[fp.similarity_key()] += 1

        # Check thresholds
        identical_count = self._identical_counts[fp.identity_key()]
        similar_count = self._similar_counts[fp.similarity_key()]

        if identical_count >= _LOOP_IDENTICAL_THRESHOLD:
            msg = (
                f"LOOP DETECTED: {identical_count} identical calls to "
                f"{fp.identity_key()} in {_LOOP_WINDOW_SECONDS}s. "
                f"Threshold={_LOOP_IDENTICAL_THRESHOLD}. Backing off."
            )
            logger.error(msg)
            raise LoopDetectedError(msg)

        if similar_count >= _LOOP_SIMILAR_THRESHOLD:
            msg = (
                f"BURST DETECTED: {similar_count} similar calls for "
                f"{fp.similarity_key()} in {_LOOP_WINDOW_SECONDS}s. "
                f"Threshold={_LOOP_SIMILAR_THRESHOLD}."
            )
            logger.warning(msg)
            return True, msg

        # Frequency check: calls per minute for this similarity key
        recent = [
            c for c in self._calls
            if c.similarity_key() == fp.similarity_key()
            and c.ts > now - timedelta(minutes=1)
        ]
        if len(recent) >= _FREQUENCY_BURST_THRESHOLD:
            msg = (
                f"FREQUENCY BURST: {len(recent)} calls/minute for "
                f"{fp.similarity_key()}. Threshold={_FREQUENCY_BURST_THRESHOLD}."
            )
            logger.warning(msg)
            return True, msg

        return False, None

    @staticmethod
    def hash_prompt(prompt: str) -> str:
        """SHA-256 of first 4 KB of prompt text.

        Args:
            prompt: Raw prompt string.

        Returns:
            16-character hex digest prefix.
        """
        return hashlib.sha256(prompt[:4096].encode()).hexdigest()[:16]
