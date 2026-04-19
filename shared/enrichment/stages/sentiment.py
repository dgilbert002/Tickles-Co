"""Heuristic, keyword-based sentiment scorer.

Deliberately simple: we want a baseline that runs without a model
download, zero dependencies, and deterministic output suitable
for tests. A later phase can plug in an LLM or transformer-based
scorer behind the same :class:`EnrichmentStage` contract.

Score output is in the range ``[-1.0, +1.0]``. Label is derived
from the score at ``±0.1`` thresholds.
"""

from __future__ import annotations

import re
from typing import Tuple

from shared.enrichment.schema import EnrichmentResult, EnrichmentStage

_BULLISH: Tuple[str, ...] = (
    "bullish", "bull", "moon", "pump", "pumping", "rally",
    "rallying", "breakout", "ath", "all time high", "all-time high",
    "long", "buy", "buying", "dip buy", "accumulate", "accumulating",
    "rocket", "send it", "strong", "green", "upgrade", "upside",
    "support holding", "reclaim", "reclaimed", "squeeze",
)
_BEARISH: Tuple[str, ...] = (
    "bearish", "bear", "dump", "dumping", "rekt", "liquidated",
    "capitulation", "crash", "crashed", "breakdown", "short",
    "sell", "selling", "downside", "red", "downgrade", "bleed",
    "bleeding", "puke", "flush", "panic", "liquidation",
)

# Simple negation handler: if one of these precedes a keyword
# inside a small window, invert the contribution.
_NEGATORS: Tuple[str, ...] = ("not", "no", "isn't", "ain't", "never")

_WORD_RE = re.compile(r"[a-z']+")


class SentimentScorer(EnrichmentStage):
    name_ = "sentiment"

    @property
    def name(self) -> str:
        return self.name_

    def _count(self, words: list[str]) -> Tuple[int, int]:
        bull_hits = 0
        bear_hits = 0
        for i, w in enumerate(words):
            prev = words[i - 1] if i > 0 else ""
            negated = prev in _NEGATORS
            if w in _BULLISH:
                if negated:
                    bear_hits += 1
                else:
                    bull_hits += 1
            elif w in _BEARISH:
                if negated:
                    bull_hits += 1
                else:
                    bear_hits += 1
        return bull_hits, bear_hits

    def process(self, result: EnrichmentResult) -> None:
        text = f"{result.headline} {result.content}".lower()
        if not text.strip():
            result.sentiment_label = None
            result.sentiment_score = None
            return

        words = _WORD_RE.findall(text)
        # also catch multi-word phrases
        bull_extra = sum(1 for phrase in _BULLISH if " " in phrase and phrase in text)
        bear_extra = sum(1 for phrase in _BEARISH if " " in phrase and phrase in text)
        bull_hits, bear_hits = self._count(words)
        bull_hits += bull_extra
        bear_hits += bear_extra

        total = bull_hits + bear_hits
        if total == 0:
            result.sentiment_label = "neutral"
            result.sentiment_score = 0.0
            return

        score = (bull_hits - bear_hits) / float(total)
        result.sentiment_score = float(score)
        if score > 0.1:
            result.sentiment_label = "positive"
        elif score < -0.1:
            result.sentiment_label = "negative"
        else:
            result.sentiment_label = "neutral"
