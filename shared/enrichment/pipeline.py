"""Pipeline runner and default pipeline construction."""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable, List, Optional

from shared.enrichment.schema import EnrichmentResult, EnrichmentStage

logger = logging.getLogger("tickles.enrichment")


class Pipeline:
    """Sequential, in-process enrichment pipeline.

    Every stage gets the same :class:`EnrichmentResult` object and
    mutates it. If a stage raises, the error is captured in
    ``result.stage_errors[stage.name]`` and the pipeline continues —
    we'd rather finish the run with a best-effort enrichment than
    lose the whole message to one flaky detector.
    """

    def __init__(self, stages: Iterable[EnrichmentStage]) -> None:
        self.stages: List[EnrichmentStage] = list(stages)
        self._seen_names: set[str] = set()
        for stage in self.stages:
            if stage.name in self._seen_names:
                raise ValueError(f"duplicate stage name: {stage.name}")
            self._seen_names.add(stage.name)

    def run(
        self,
        headline: str,
        content: str,
        news_item_id: Optional[int] = None,
    ) -> EnrichmentResult:
        result = EnrichmentResult(
            news_item_id=news_item_id,
            headline=headline or "",
            content=content or "",
        )
        for stage in self.stages:
            start = time.perf_counter()
            try:
                stage.process(result)
            except Exception as exc:  # noqa: BLE001
                result.stage_errors[stage.name] = repr(exc)
                logger.warning(
                    "enrichment stage %r failed on item %s: %s",
                    stage.name,
                    news_item_id,
                    exc,
                )
            finally:
                result.stage_timings_ms[stage.name] = (
                    (time.perf_counter() - start) * 1000.0
                )
        return result


def build_default_pipeline(
    instruments_loader: Optional[Any] = None,
) -> Pipeline:
    """Construct the default pipeline.

    Stage order matters:

      1. :class:`LanguageDetector` — sets ``result.language``.
      2. :class:`SymbolResolver`   — fills ``result.symbols``.
         (Downstream stages can use the symbol count as a signal.)
      3. :class:`SentimentScorer`  — sets label + score.
      4. :class:`RelevanceScorer`  — uses everything the previous
         stages produced.

    ``instruments_loader`` is any object with a
    ``load()`` method returning a list of instrument dicts (see
    :class:`SymbolResolver`). If None, the resolver falls back to
    a compiled-in list of liquid majors, which is enough for unit
    tests and for boxes without a populated ``instruments`` table.
    """
    from shared.enrichment.stages.language import LanguageDetector
    from shared.enrichment.stages.relevance import RelevanceScorer
    from shared.enrichment.stages.sentiment import SentimentScorer
    from shared.enrichment.stages.symbol_resolver import SymbolResolver

    return Pipeline(
        [
            LanguageDetector(),
            SymbolResolver(instruments_loader=instruments_loader),
            SentimentScorer(),
            RelevanceScorer(),
        ]
    )
