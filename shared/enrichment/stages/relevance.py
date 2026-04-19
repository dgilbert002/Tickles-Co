"""
Relevance scoring — "is this message worth looking at?"

Inputs:

  * Number of symbol matches (more matches → more relevant).
  * Presence of action verbs (long/short/buy/sell/TP/SL).
  * Presence of numbers (price levels, targets).
  * Message length (very short shitposts score lower).

Output: ``result.relevance_score`` in ``[0.0, 1.0]``.
"""

from __future__ import annotations

import re

from shared.enrichment.schema import EnrichmentResult, EnrichmentStage

_ACTION_RE = re.compile(
    r"\b(long|short|buy|sell|target|tp|sl|stop\s*loss|take\s*profit|entry)\b",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"\b\d{2,7}(?:[.,]\d+)?\b")


class RelevanceScorer(EnrichmentStage):
    name_ = "relevance"

    @property
    def name(self) -> str:
        return self.name_

    def process(self, result: EnrichmentResult) -> None:
        text = f"{result.headline} {result.content}"
        if not text.strip():
            result.relevance_score = 0.0
            return

        score = 0.0

        # symbols
        n_syms = len(result.symbols)
        if n_syms == 1:
            score += 0.35
        elif n_syms == 2:
            score += 0.5
        elif n_syms >= 3:
            score += 0.55

        if _ACTION_RE.search(text):
            score += 0.25
        if _NUMBER_RE.search(text):
            score += 0.15

        length = len(text.strip())
        if length >= 200:
            score += 0.1
        elif length >= 80:
            score += 0.05
        elif length < 20:
            score = max(0.0, score - 0.1)

        if result.language == "en":
            score += 0.05
        elif result.language == "non_en":
            score = max(0.0, score - 0.05)

        if result.sentiment_label in ("positive", "negative"):
            score += 0.05

        result.relevance_score = float(max(0.0, min(score, 1.0)))
