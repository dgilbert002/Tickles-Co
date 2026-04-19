"""
shared.enrichment — Phase 23 enrichment pipeline.

What it does
------------
Raw news_items (Discord / Telegram / RSS / TradingView) come in
with ``headline`` + ``content`` + minimal metadata. Strategies,
agents, and the optimiser all want the *enriched* view:

  * Which trading instruments does this message refer to?
    (e.g. ``"BTC pumping to 100k"`` → ``BTC/USDT``, ``BTC-PERP``)
  * What's the sentiment — bullish / bearish / neutral?
  * Is this message actually actionable, or is it shitposting?
  * What language was it in?

Phase 23 wires a deterministic, lightweight enrichment pipeline
into the existing :mod:`shared.collectors` zoo:

  * Pure-Python stages with a shared :class:`EnrichmentStage`
    interface.
  * Stateless — stages are re-entrant and can run in workers.
  * Pluggable — LLM-backed stages can replace heuristics later
    without touching the orchestration.
  * DB-backed worker (:class:`NewsEnricher`) that reads pending
    news_items, applies the pipeline, writes the result back
    into existing ``sentiment`` + ``instruments`` columns plus
    the new ``enrichment`` jsonb column added by the Phase 23
    migration.

Public surface:

* :class:`Pipeline`, :func:`build_default_pipeline`
* :class:`EnrichmentStage`, :class:`EnrichmentResult`
* Concrete stages: :class:`SymbolResolver`, :class:`SentimentScorer`,
  :class:`LanguageDetector`, :class:`RelevanceScorer`.
* :class:`NewsEnricher` worker + :class:`EnricherConfig`.
"""

from shared.enrichment.schema import (
    EnrichmentResult,
    EnrichmentStage,
    SymbolMatch,
)
from shared.enrichment.pipeline import (
    Pipeline,
    build_default_pipeline,
)
from shared.enrichment.stages.symbol_resolver import SymbolResolver
from shared.enrichment.stages.sentiment import SentimentScorer
from shared.enrichment.stages.language import LanguageDetector
from shared.enrichment.stages.relevance import RelevanceScorer

__all__ = [
    "EnrichmentResult",
    "EnrichmentStage",
    "SymbolMatch",
    "Pipeline",
    "build_default_pipeline",
    "SymbolResolver",
    "SentimentScorer",
    "LanguageDetector",
    "RelevanceScorer",
]
