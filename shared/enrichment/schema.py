"""Schema primitives for the enrichment pipeline."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SymbolMatch:
    """One instrument candidate detected inside a message."""

    symbol: str
    exchange: str
    asset_class: str
    base: Optional[str] = None
    quote: Optional[str] = None
    match_text: str = ""
    confidence: float = 1.0
    instrument_id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "exchange": self.exchange,
            "asset_class": self.asset_class,
            "base": self.base,
            "quote": self.quote,
            "match_text": self.match_text,
            "confidence": self.confidence,
            "instrument_id": self.instrument_id,
        }


@dataclass
class EnrichmentResult:
    """Accumulator passed through every stage.

    Each stage mutates this object. The final result is written
    back to the DB as:

      * ``sentiment`` — textual label ("positive" | "negative" |
        "neutral" | None).
      * ``instruments`` — jsonb array of SymbolMatch dicts.
      * ``enrichment`` (new jsonb column) — stage-by-stage
        details: {"sentiment_score": float, "relevance_score":
        float, "language": str, "stage_timings_ms": {...}}.
    """

    news_item_id: Optional[int] = None
    headline: str = ""
    content: str = ""

    sentiment_label: Optional[str] = None
    sentiment_score: Optional[float] = None  # -1.0 .. +1.0
    relevance_score: Optional[float] = None  # 0.0 .. 1.0
    language: Optional[str] = None
    symbols: List[SymbolMatch] = field(default_factory=list)

    stage_timings_ms: Dict[str, float] = field(default_factory=dict)
    stage_errors: Dict[str, str] = field(default_factory=dict)

    def to_db_row(self) -> Dict[str, Any]:
        """Shape suitable for :class:`NewsEnricher` UPDATE path."""
        return {
            "sentiment": self.sentiment_label,
            "instruments": [m.to_dict() for m in self.symbols],
            "enrichment": {
                "sentiment_score": self.sentiment_score,
                "relevance_score": self.relevance_score,
                "language": self.language,
                "stage_timings_ms": dict(self.stage_timings_ms),
                "stage_errors": dict(self.stage_errors),
            },
        }

    def summary(self) -> Dict[str, Any]:
        return {
            "news_item_id": self.news_item_id,
            "sentiment_label": self.sentiment_label,
            "sentiment_score": self.sentiment_score,
            "relevance_score": self.relevance_score,
            "language": self.language,
            "n_symbols": len(self.symbols),
            "symbols": [m.symbol for m in self.symbols],
            "stage_timings_ms": dict(self.stage_timings_ms),
            "stage_errors": dict(self.stage_errors),
        }


class EnrichmentStage(ABC):
    """Abstract base for all pipeline stages.

    Stages must be pure (no global state) and re-entrant. All
    timing + error capture is done by :class:`Pipeline` — stages
    only care about :meth:`process`.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    def process(self, result: EnrichmentResult) -> None:
        """Mutate ``result`` in place."""
