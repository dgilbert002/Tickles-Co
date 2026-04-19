"""Unit tests for Phase 23 shared.enrichment."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from shared.enrichment import (
    EnrichmentResult,
    EnrichmentStage,
    LanguageDetector,
    Pipeline,
    RelevanceScorer,
    SentimentScorer,
    SymbolMatch,
    SymbolResolver,
    build_default_pipeline,
)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_symbol_match_to_dict_roundtrips_through_json() -> None:
    m = SymbolMatch(
        symbol="BTC/USDT", exchange="binance", asset_class="crypto",
        base="BTC", quote="USDT", match_text="BTC", confidence=0.6,
    )
    d = m.to_dict()
    json.dumps(d)
    assert d["symbol"] == "BTC/USDT"


def test_enrichment_result_empty_summary() -> None:
    r = EnrichmentResult()
    s = r.summary()
    assert s["n_symbols"] == 0
    assert s["sentiment_label"] is None


def test_enrichment_result_to_db_row() -> None:
    r = EnrichmentResult(sentiment_label="positive", sentiment_score=0.5,
                         relevance_score=0.8, language="en")
    r.symbols.append(SymbolMatch(symbol="BTC/USDT", exchange="binance",
                                 asset_class="crypto"))
    row = r.to_db_row()
    assert row["sentiment"] == "positive"
    assert len(row["instruments"]) == 1
    assert row["enrichment"]["sentiment_score"] == 0.5


# ---------------------------------------------------------------------------
# Individual stages
# ---------------------------------------------------------------------------


def test_language_detector_english() -> None:
    stage = LanguageDetector()
    r = EnrichmentResult(headline="BTC is pumping to the moon", content="bulls are in control")
    stage.process(r)
    assert r.language == "en"


def test_language_detector_unknown_on_empty() -> None:
    stage = LanguageDetector()
    r = EnrichmentResult()
    stage.process(r)
    assert r.language == "unknown"


def test_language_detector_non_english() -> None:
    stage = LanguageDetector()
    r = EnrichmentResult(content="这个比特币会涨吗")
    stage.process(r)
    assert r.language == "non_en"


def test_symbol_resolver_pair_form() -> None:
    stage = SymbolResolver()
    r = EnrichmentResult(content="long BTC/USDT with tight SL")
    stage.process(r)
    symbols = [m.symbol for m in r.symbols]
    assert "BTC/USDT" in symbols


def test_symbol_resolver_ticker_form() -> None:
    stage = SymbolResolver()
    r = EnrichmentResult(content="bought $ETH at 4k")
    stage.process(r)
    assert any(m.symbol == "ETH/USDT" for m in r.symbols)


def test_symbol_resolver_bare_word_form() -> None:
    stage = SymbolResolver()
    r = EnrichmentResult(content="BTC pumping hard")
    stage.process(r)
    assert any(m.symbol == "BTC/USDT" for m in r.symbols)


def test_symbol_resolver_custom_instruments() -> None:
    custom = [
        {"symbol": "FOO/BAR", "exchange": "x", "asset_class": "custom",
         "base_currency": "FOO", "quote_currency": "BAR",
         "aliases": ["FOO"], "id": 42},
    ]
    stage = SymbolResolver(instruments=custom)
    r = EnrichmentResult(content="FOO is mooning!")
    stage.process(r)
    assert len(r.symbols) == 1
    assert r.symbols[0].instrument_id == 42


def test_symbol_resolver_no_false_positives_on_common_text() -> None:
    stage = SymbolResolver()
    r = EnrichmentResult(headline="Hello World", content="this is just chat")
    stage.process(r)
    assert r.symbols == []


def test_sentiment_scorer_bullish() -> None:
    stage = SentimentScorer()
    r = EnrichmentResult(content="BTC pumping to the moon, strong bull rally")
    stage.process(r)
    assert r.sentiment_label == "positive"
    assert r.sentiment_score is not None and r.sentiment_score > 0


def test_sentiment_scorer_bearish() -> None:
    stage = SentimentScorer()
    r = EnrichmentResult(content="bearish, market is crashing, panic selling")
    stage.process(r)
    assert r.sentiment_label == "negative"
    assert r.sentiment_score is not None and r.sentiment_score < 0


def test_sentiment_scorer_neutral_empty() -> None:
    stage = SentimentScorer()
    r = EnrichmentResult(content="the weather is nice today")
    stage.process(r)
    assert r.sentiment_label == "neutral"


def test_sentiment_scorer_negation_flips_sign() -> None:
    stage = SentimentScorer()
    r = EnrichmentResult(content="this is not bullish at all")
    stage.process(r)
    assert r.sentiment_label in ("negative", "neutral")


def test_relevance_scorer_action_and_symbol_boost() -> None:
    stage = RelevanceScorer()
    r = EnrichmentResult(content="long BTC entry 60000 TP 65000 SL 58000")
    r.symbols.append(SymbolMatch(symbol="BTC/USDT", exchange="binance", asset_class="crypto"))
    r.language = "en"
    stage.process(r)
    assert r.relevance_score is not None and r.relevance_score > 0.5


def test_relevance_scorer_low_on_shitpost() -> None:
    stage = RelevanceScorer()
    r = EnrichmentResult(content="lol")
    stage.process(r)
    assert r.relevance_score == 0.0


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def test_pipeline_rejects_duplicate_stage_names() -> None:
    class _Dup(EnrichmentStage):
        name_ = "same"

        @property
        def name(self) -> str:
            return self.name_

        def process(self, result: EnrichmentResult) -> None:
            pass

    with pytest.raises(ValueError):
        Pipeline([_Dup(), _Dup()])


def test_pipeline_runs_all_stages() -> None:
    pipe = build_default_pipeline()
    r = pipe.run(
        headline="BTC breakout",
        content="BTC/USDT long entry 65000 TP 70000 SL 62000. pumping hard.",
    )
    assert r.language == "en"
    assert any(m.symbol == "BTC/USDT" for m in r.symbols)
    assert r.sentiment_label in ("positive", "neutral")
    assert r.relevance_score is not None and r.relevance_score > 0.5
    for stage_name in ("language", "symbol_resolver", "sentiment", "relevance"):
        assert stage_name in r.stage_timings_ms


def test_pipeline_swallows_stage_exception() -> None:
    class _Bad(EnrichmentStage):
        name_ = "bad"

        @property
        def name(self) -> str:
            return self.name_

        def process(self, result: EnrichmentResult) -> None:
            raise RuntimeError("boom")

    class _Good(EnrichmentStage):
        name_ = "good"

        @property
        def name(self) -> str:
            return self.name_

        def process(self, result: EnrichmentResult) -> None:
            result.language = "en"

    pipe = Pipeline([_Bad(), _Good()])
    r = pipe.run(headline="h", content="c")
    assert r.language == "en"
    assert "bad" in r.stage_errors
    assert "boom" in r.stage_errors["bad"]


# ---------------------------------------------------------------------------
# Enricher convenience
# ---------------------------------------------------------------------------


def test_enrich_text_once_returns_summary() -> None:
    from shared.enrichment.news_enricher import enrich_text_once

    summary = enrich_text_once(
        headline="ETH breakout",
        content="$ETH pumping hard, long entry 4000",
    )
    assert summary["sentiment_label"] == "positive"
    assert "ETH/USDT" in summary["symbols"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _run_cli(args: list[str]) -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "shared.cli.enrichment_cli", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode in (0, 1), proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_enrichment_cli_stages() -> None:
    payload = _run_cli(["stages"])
    assert payload["ok"] is True
    assert payload["count"] == 4
    names = {s["name"] for s in payload["stages"]}
    assert names == {"language", "symbol_resolver", "sentiment", "relevance"}


def test_enrichment_cli_enrich_text() -> None:
    payload = _run_cli(
        ["enrich-text", "--headline", "BTC long", "--content", "BTC/USDT pumping"],
    )
    assert payload["ok"] is True
    assert "BTC/USDT" in payload["summary"]["symbols"]


def test_enrichment_cli_apply_migration() -> None:
    payload = _run_cli(["apply-migration"])
    assert payload["ok"] is True
    assert payload["migration_path"].endswith("2026_04_19_phase23_enrichment.sql")
