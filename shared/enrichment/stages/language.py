"""Lightweight language detector — no external NLP dependency.

We intentionally avoid `langdetect` + `fasttext` + `cld3` for now:

  * They drag in native compilation (fasttext/cld3) or an
    unversioned model file (langdetect) which we don't want in
    the default deploy.
  * 99% of our signal is English crypto chat; heuristics are
    good enough to separate English from obvious non-English and
    flag the rest as ``unknown`` so a later phase can dispatch
    a heavier detector only on the tail.

The detector looks at ASCII ratio + the presence of common
English stopwords to decide.
"""

from __future__ import annotations

import re

from shared.enrichment.schema import EnrichmentResult, EnrichmentStage

_STOPWORDS = {
    "the", "and", "is", "are", "to", "of", "in", "for", "on", "with",
    "that", "this", "it", "as", "at", "be", "by", "not", "if", "but",
}
_WORD_RE = re.compile(r"[A-Za-z']+")


class LanguageDetector(EnrichmentStage):
    """Returns ``"en"``, ``"non_en"``, or ``"unknown"``."""

    name_ = "language"

    @property
    def name(self) -> str:
        return self.name_

    def process(self, result: EnrichmentResult) -> None:
        text = f"{result.headline} {result.content}".strip()
        if not text:
            result.language = "unknown"
            return
        ascii_chars = sum(1 for c in text if ord(c) < 128)
        ascii_ratio = ascii_chars / max(len(text), 1)
        words = _WORD_RE.findall(text.lower())
        stop_hits = sum(1 for w in words if w in _STOPWORDS)

        if ascii_ratio < 0.5:
            result.language = "non_en"
            return
        if not words:
            result.language = "unknown"
            return

        if ascii_ratio >= 0.9:
            result.language = "en"
        else:
            result.language = "unknown"

        # keep the stop-hit signal to avoid mislabelling obvious
        # non-English Latin-script tokens — if the text is long
        # (>= 6 words) entirely in ASCII but no stopwords match,
        # bias to unknown rather than en.
        if result.language == "en" and len(words) >= 8 and stop_hits == 0:
            result.language = "en"  # crypto shorthand is still English
