"""
Module: test_bug_hunt_2026_05_24
Purpose: Regression tests for the multi-round bug-hunt audit run between
         2026-05-22 (Bugs 8-12) and 2026-05-24 (Tier 0 / Tier 1 / Tier 2
         findings from the 5-agent + 4-agent validation rounds).

The previous round shipped fixes; this file ensures none of them regress.

Coverage matrix:

  BUG 8  — handle_media falls back to a sibling media_items row when the
           originally-linked row has NULL local_path.
  BUG 9  — _media_url and api/* routes are RELATIVE so sub-path proxies work.
  BUG 10 — _flatten_levels maps singular `take_profit` -> `take_profit_1`.
  BUG 11 — _timeframe_window applies a DESC+LIMIT slide guard.
  BUG A  — handle_entry_radar applies the same slide guard.
  BUG B  — _timeframe_window expands window for closed trades.
  BUG C  — symbol_resolver._strip_reply_prefix removes [Reply to @user] lines.
  BUG D  — extract_signal_from_text falls back to singular take_profit /
           entry_price keys.
  news_context — InterpretationService strips reply prefix before vision LLM.

Tier 0 / Tier 1 / Tier 2 (this round):

  C1 — `tracked_positions.deduped_at` column exists. (DB migration test.)
  C2 — _process_text_one marks news rows terminal so the LLM cost loop
       cannot re-pay for the same dead message every cycle. (Code check.)
  C3 — fetch_pending_media SQL includes FOR UPDATE SKIP LOCKED + UPDATE
       to processing_status='analyzing'.
  C3-companion — recover_stale_analyzing exists.
  C4 — _close_position SQL includes `AND status IN ('open','partial_exit')`.
  C5 — Phase-11 actor_performance / prompt_assignments tables exist in
       tickles_shared. (DB migration test.)
  H1 — fetch_open_positions ORDER BY price_updated_at NULLS FIRST.
  H2 — wick watermark advance picks the EARLIER of `now` and DB MAX.
  H3 — postmortem _fetch_candles uses ASC and clamps `end` to start +
       candle_limit minutes.
  H4 — chart_hacker_opinion: actor_type filter + JSON parse + correct
       gateway helper.
  H5 — trade_dedup canonicalises symbol before query.
  H6 — CCXT replay re-fetch references $6 / $7 (NOT $7 / $8).
  H7 — handle_media sibling fallback (covered by Bug 8 path).
  H8 — write_signal_interpretation INSERT includes pattern_tags / setup_tags.
  H9 — handle_candles applies slide guard when start+end are passed.
  H10 — handle_competition_agent canonicalises symbol before live-price join.
  H11 — Discord collector advances HWM only after filters.
  H12 — _find_sl_tp_wick_candle uses keyset pagination (max_iterations).
  H13 — pending activation UPDATE includes `AND status = 'pending'`.

  T2-reply — strip_reply_prefix applied in sentiment / relevance /
             language / news_provider / postmortem consumer / app.js.
  T2-tp     — find_duplicate_signal COALESCEs take_profit_1 + take_profit;
             legacy_levels / chart_hacker_trades fall back to singular.
  T2-misc  — text-only signal returns analyzed_position_create_failed
             when create_tracked_position_from_interpretation throws.
  T2-cache — _INSTRUMENT_ID_CACHE_MAX bound + OrderedDict eviction.
  T2-paths — manage panel uses relative redirects + relative anchor href.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import re
import unittest

logger = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[2]


def _read(p: str) -> str:
    return (REPO / p).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Bug 8 / H7 — handle_media sibling fallback
# ---------------------------------------------------------------------------
class TestBug8_HandleMediaSiblingFallback(unittest.TestCase):
    def test_handle_media_queries_news_item_id(self) -> None:
        src = _read("shared/dashboard/server.py")
        # The original row SELECT must include news_item_id so we can find
        # siblings.
        self.assertIn("SELECT local_path, mime_type, news_item_id", src)

    def test_handle_media_does_sibling_lookup(self) -> None:
        src = _read("shared/dashboard/server.py")
        self.assertIn("WHERE news_item_id = $1 AND id <> $2", src)
        self.assertIn("local_path IS NOT NULL", src)

    def test_handle_media_only_404s_after_sibling_check(self) -> None:
        src = _read("shared/dashboard/server.py")
        # Make sure the early "media file missing on disk" return is gone
        # (replaced by sibling fallback before final 404).
        self.assertNotIn(
            "if not row or not row.get(\"local_path\"):\n        return _err(404",
            src,
        )


# ---------------------------------------------------------------------------
# Bug 9 — relative API paths
# ---------------------------------------------------------------------------
class TestBug9_RelativeApiUrls(unittest.TestCase):
    def test_media_routes_emit_relative_paths(self) -> None:
        src = _read("shared/dashboard/market_routes.py")
        # `_media_url` should NOT emit absolute paths starting with `/api`.
        for m in re.finditer(r'def _media_url[\s\S]+?return ', src):
            chunk = m.group(0)
            self.assertNotIn('"/api/', chunk)

    def test_drawer_provider_emits_relative_paths(self) -> None:
        src = _read("shared/dashboard/interpretation_drawer_provider.py")
        # The drawer rebuild from Bug 9 emitted relative `api/...` paths.
        # We just check that no absolute `/api/media/` literal sneaks in.
        self.assertNotIn('"/api/media/{', src)


# ---------------------------------------------------------------------------
# Bug 10 / H8 — singular take_profit + pattern_tags / setup_tags
# ---------------------------------------------------------------------------
class TestBug10_FlattenLevelsAndPatternTags(unittest.TestCase):
    def test_flatten_levels_maps_singular_take_profit(self) -> None:
        src = _read("shared/intelligence/interpretation_service.py")
        # The fix populates `take_profit_1` from a singular `take_profit` key.
        # Either fallback flavour (chained get-or, or explicit if-in-dict) is
        # acceptable as long as the singular key is read.
        self.assertIn('levels.get("take_profit")', src)
        self.assertIn('out["take_profit_1"]', src)

    def test_pattern_tags_setup_tags_in_insert(self) -> None:
        src = _read("shared/intelligence/interpretation_service.py")
        # H8 — pattern_tags + setup_tags must appear in both column list
        # and VALUES list of write_signal_interpretation.
        self.assertIn("pattern_tags, setup_tags", src)
        self.assertIn("$46::jsonb, $47::jsonb", src)

    def test_legacy_levels_fallback_to_singular(self) -> None:
        src = _read("shared/intelligence/interpretation_service.py")
        # Bug Hunter 2 §10.2 — legacy_levels accepts singular take_profit.
        self.assertIn(
            'primary.get("tp1") or primary.get("take_profit")',
            src,
        )

    def test_chart_hacker_trades_fallback_to_singular(self) -> None:
        src = _read("shared/intelligence/interpretation_service.py")
        self.assertIn(
            'trade.get("tp1") or trade.get("take_profit")',
            src,
        )


# ---------------------------------------------------------------------------
# Bug 11 / Bug A / Bug B / H9 — DESC+LIMIT slide guard
# ---------------------------------------------------------------------------
class TestBug11_SlideGuards(unittest.TestCase):
    def test_market_routes_timeframe_window_has_slide_guard(self) -> None:
        src = _read("shared/dashboard/market_routes.py")
        # Either the legacy `_timeframe_window` or the public `handle_candles`
        # must clamp `end` to `start + limit * tf_seconds`.
        self.assertIn("max_seconds", src)

    def test_postmortem_fetch_candles_uses_asc_and_clamps_end(self) -> None:
        src = _read("shared/intelligence/postmortem_service.py")
        self.assertIn("ORDER BY timestamp ASC", src)
        # clamp end = start + candle_limit minutes
        self.assertIn("limit_seconds = float(self.candle_limit) * 60.0", src)

    def test_entry_radar_has_slide_guard(self) -> None:
        src = _read("shared/dashboard/market_routes.py")
        # Bug A — radar uses sec * radar_limit clamp
        self.assertTrue(
            "radar_end" in src or "radar_limit" in src,
            "handle_entry_radar slide guard missing",
        )


# ---------------------------------------------------------------------------
# Bug C — symbol_resolver reply-prefix
# ---------------------------------------------------------------------------
class TestBugC_SymbolResolverReplyStrip(unittest.TestCase):
    def test_symbol_resolver_imports_canonical_helper(self) -> None:
        src = _read("shared/enrichment/stages/symbol_resolver.py")
        self.assertIn(
            "from shared.utils.reply_prefix import strip_reply_prefix",
            src,
        )

    def test_symbol_resolver_calls_helper_in_process(self) -> None:
        src = _read("shared/enrichment/stages/symbol_resolver.py")
        self.assertIn("_strip_reply_prefix", src)
        self.assertIn("clean_headline", src)
        self.assertIn("clean_content", src)


# ---------------------------------------------------------------------------
# Bug D — text_signal_extractor singular fallback
# ---------------------------------------------------------------------------
class TestBugD_TextExtractorSingularFallback(unittest.TestCase):
    def test_extractor_falls_back_to_singular_take_profit(self) -> None:
        src = _read("shared/intelligence/text_signal_extractor.py")
        # Either the explicit fallback we added in Bug D or its eqv.
        self.assertTrue(
            'parsed.get("take_profit")' in src,
            "Bug D singular take_profit fallback missing",
        )

    def test_extractor_falls_back_to_entry_price(self) -> None:
        src = _read("shared/intelligence/text_signal_extractor.py")
        self.assertIn('parsed.get("entry_price")', src)


# ---------------------------------------------------------------------------
# news_context — strip reply prefix before vision LLM
# ---------------------------------------------------------------------------
class TestNewsContext_ReplyStrip(unittest.TestCase):
    def test_news_context_built_from_clean_headline_content(self) -> None:
        src = _read("shared/intelligence/interpretation_service.py")
        self.assertIn("clean_headline_for_ctx = strip_reply_prefix", src)
        self.assertIn("clean_content_for_ctx = strip_reply_prefix", src)
        self.assertIn(
            'news_context = f"{clean_headline_for_ctx}\\n{clean_content_for_ctx}"[:1000]',
            src,
        )


# ---------------------------------------------------------------------------
# C2 — text-only LLM cost-loop terminal placeholder
# ---------------------------------------------------------------------------
class TestC2_TextLoopTerminal(unittest.TestCase):
    def test_mark_news_terminal_helper_exists(self) -> None:
        src = _read("shared/intelligence/interpretation_service.py")
        self.assertIn("async def _mark_news_terminal", src)

    def test_skip_paths_call_mark_terminal(self) -> None:
        src = _read("shared/intelligence/interpretation_service.py")
        # All four skip paths in _process_text_one must call the helper.
        for status in ("skipped_no_content", "skipped_meme",
                       "skipped_commentary", "no_signal_detected"):
            self.assertIn(status, src)
        # Helper called at least once
        self.assertGreaterEqual(
            src.count("await self._mark_news_terminal("),
            4,
            "Expected _mark_news_terminal to be called at every skip return",
        )


# ---------------------------------------------------------------------------
# C3 — atomic media-queue claim
# ---------------------------------------------------------------------------
class TestC3_AtomicClaim(unittest.TestCase):
    def test_fetch_pending_media_uses_for_update_skip_locked(self) -> None:
        src = _read("shared/intelligence/interpretation_service.py")
        self.assertIn("FOR UPDATE OF m SKIP LOCKED", src)

    def test_claim_transitions_to_analyzing(self) -> None:
        src = _read("shared/intelligence/interpretation_service.py")
        self.assertIn("SET processing_status = 'analyzing'", src)

    def test_recover_stale_analyzing_exists(self) -> None:
        src = _read("shared/intelligence/interpretation_service.py")
        self.assertIn("async def recover_stale_analyzing", src)
        self.assertIn("WHERE processing_status = 'analyzing'", src)


# ---------------------------------------------------------------------------
# C4 — optimistic close lock
# ---------------------------------------------------------------------------
class TestC4_OptimisticCloseLock(unittest.TestCase):
    def test_close_update_has_status_guard(self) -> None:
        src = _read("shared/intelligence/position_monitor.py")
        # Both close-paths (with & without realized_pnl_final) must guard
        # status = open or partial_exit.
        guards = src.count("AND status IN ('open', 'partial_exit')")
        self.assertGreaterEqual(guards, 2)


# ---------------------------------------------------------------------------
# H1 — fetch_open_positions ordering
# ---------------------------------------------------------------------------
class TestH1_OpenPositionRotation(unittest.TestCase):
    def test_fetch_open_orders_by_price_updated_at_nulls_first(self) -> None:
        src = _read("shared/intelligence/position_monitor.py")
        self.assertIn(
            "ORDER BY price_updated_at ASC NULLS FIRST",
            src,
        )


# ---------------------------------------------------------------------------
# H2 — watermark advance never goes past `now`
# ---------------------------------------------------------------------------
class TestH2_WatermarkAdvance(unittest.TestCase):
    def test_watermark_picks_earlier_of_now_and_max_ts(self) -> None:
        src = _read("shared/intelligence/position_monitor.py")
        self.assertIn("if max_ts is not None and max_ts < now:", src)
        self.assertIn("watermark = max_ts", src)
        self.assertIn("watermark = now", src)


# ---------------------------------------------------------------------------
# H4 — chart_hacker_opinion fix
# ---------------------------------------------------------------------------
class TestH4_ChartHackerOpinion(unittest.TestCase):
    def test_actor_type_filter_uses_real_values(self) -> None:
        src = _read("shared/intelligence/chart_hacker_opinion_service.py")
        # Active SQL contains the corrected filter.
        self.assertIn(
            "AND p.actor_type IN ('trader_human', 'agent')",
            src,
        )

    def test_uses_chat_completion_not_call_vision_llm(self) -> None:
        src = _read("shared/intelligence/chart_hacker_opinion_service.py")
        self.assertIn("from shared.intelligence.gateway_config import GatewayConfig, chat_completion", src)
        self.assertNotIn("from shared.intelligence.gateway_config import call_vision_llm", src)

    def test_parses_json_from_content(self) -> None:
        src = _read("shared/intelligence/chart_hacker_opinion_service.py")
        # The new parser uses re.search on response['content'].
        self.assertIn('content_str = (response or {}).get("content"', src)
        self.assertIn("json.loads", src)


# ---------------------------------------------------------------------------
# H5 — dedup symbol normalization
# ---------------------------------------------------------------------------
class TestH5_DedupSymbolNorm(unittest.TestCase):
    def test_canonicalise_helper_exists(self) -> None:
        src = _read("shared/intelligence/trade_dedup.py")
        self.assertIn("def _canonicalise_symbol", src)

    def test_find_duplicate_position_canonicalises(self) -> None:
        src = _read("shared/intelligence/trade_dedup.py")
        # Ensure both find_duplicate_position and find_duplicate_signal
        # canonicalise their input.
        canon_calls = src.count("symbol = _canonicalise_symbol(symbol)")
        self.assertGreaterEqual(canon_calls, 2)

    def test_find_duplicate_signal_coalesces_take_profit(self) -> None:
        src = _read("shared/intelligence/trade_dedup.py")
        self.assertIn("(si.llm_levels->>'take_profit_1')::numeric", src)
        self.assertIn("(si.llm_levels->>'take_profit')::numeric", src)


# ---------------------------------------------------------------------------
# H6 — CCXT replay $6/$7 SQL fix
# ---------------------------------------------------------------------------
class TestH6_CcxtReplaySqlParams(unittest.TestCase):
    def test_re_fetch_uses_param_6_and_7(self) -> None:
        src = _read("shared/dashboard/market_routes.py")
        # The fixed version uses $6::text as symbol, $7::text as exchange.
        self.assertIn("$6::text as symbol", src)
        self.assertIn("$7::text as exchange", src)
        # The legacy `$7::text as symbol` SQL line must not still be active —
        # but we tolerate it inside the explanatory comment that documents
        # the H6 fix, so look for the SQL form (no leading `# `).
        for line in src.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            self.assertNotIn("$7::text as symbol", stripped)
            self.assertNotIn("$8::text as exchange", stripped)


# ---------------------------------------------------------------------------
# H10 — handle_competition_agent normalizes symbols
# ---------------------------------------------------------------------------
class TestH10_CompetitionAgentSymbolNorm(unittest.TestCase):
    def test_imports_canonicaliser(self) -> None:
        src = _read("shared/dashboard/market_routes.py")
        # Local-import inside handle_competition_agent.
        self.assertIn(
            "from shared.utils.instrument_normaliser import to_canonical_symbol",
            src,
        )

    def test_uses_raw_to_canon_map(self) -> None:
        src = _read("shared/dashboard/market_routes.py")
        self.assertIn("raw_to_canon", src)
        self.assertIn("live_prices_by_canon", src)


# ---------------------------------------------------------------------------
# H11 — Discord HWM advance after filter
# ---------------------------------------------------------------------------
class TestH11_DiscordHwmAfterFilter(unittest.TestCase):
    def test_hwm_advance_only_when_post_filter_or_user_filter(self) -> None:
        src = _read("shared/collectors/discord/discord_collector.py")
        # The fix introduces user_filter_applied / zone_filter_applied flags.
        self.assertIn("user_filter_applied", src)
        self.assertIn("zone_filter_applied", src)
        # And only sets _pending_hwm in the right branches.
        self.assertIn("if raw_messages:", src)


# ---------------------------------------------------------------------------
# H12 — wick scan keyset pagination
# ---------------------------------------------------------------------------
class TestH12_WickKeysetPagination(unittest.TestCase):
    def test_pagination_loop_present(self) -> None:
        src = _read("shared/intelligence/position_monitor.py")
        self.assertIn("while iterations < max_iterations:", src)
        self.assertIn("max_iterations = 12", src)


# ---------------------------------------------------------------------------
# H13 — pending activation status guard
# ---------------------------------------------------------------------------
class TestH13_PendingActivationStatusGuard(unittest.TestCase):
    def test_pending_activation_update_guards_status(self) -> None:
        src = _read("shared/intelligence/position_monitor.py")
        # The activation UPDATE must include `AND status = 'pending'`.
        idx = src.find("SET status = 'open'")
        self.assertNotEqual(idx, -1)
        # Look in a window around the UPDATE for the guard.
        window = src[idx: idx + 700]
        self.assertIn("AND status = 'pending'", window)


# ---------------------------------------------------------------------------
# T2-reply — strip applied in 7 sites
# ---------------------------------------------------------------------------
class TestT2Reply_AllSitesStrip(unittest.TestCase):
    REPLY_IMPORT_RE = re.compile(
        r"from\s+shared\.utils\.reply_prefix\s+import\s+strip_reply_prefix"
    )

    def test_canonical_helper_exists(self) -> None:
        src = _read("shared/utils/reply_prefix.py")
        self.assertIn("def strip_reply_prefix(text: str) -> str:", src)

    def test_sentiment_imports_helper(self) -> None:
        src = _read("shared/enrichment/stages/sentiment.py")
        self.assertRegex(src, self.REPLY_IMPORT_RE)

    def test_relevance_imports_helper(self) -> None:
        src = _read("shared/enrichment/stages/relevance.py")
        self.assertRegex(src, self.REPLY_IMPORT_RE)

    def test_language_imports_helper(self) -> None:
        src = _read("shared/enrichment/stages/language.py")
        self.assertRegex(src, self.REPLY_IMPORT_RE)

    def test_news_provider_imports_helper(self) -> None:
        src = _read("shared/dashboard/news_provider.py")
        self.assertRegex(src, self.REPLY_IMPORT_RE)

    def test_postmortem_imports_helper(self) -> None:
        src = _read("shared/intelligence/postmortem_service.py")
        # uses alias `_srp` from helper
        self.assertIn(
            "from shared.utils.reply_prefix import strip_reply_prefix as _srp",
            src,
        )

    def test_appjs_strips_reply_prefix(self) -> None:
        src = _read("shared/dashboard/static/app.js")
        self.assertIn("_stripReplyPrefixJS", src)


# ---------------------------------------------------------------------------
# T2-misc — text status_position_create_failed, INSTRUMENT_ID_CACHE bound,
#           manage panel relative paths
# ---------------------------------------------------------------------------
class TestT2Misc(unittest.TestCase):
    def test_text_signal_status_when_position_create_fails(self) -> None:
        src = _read("shared/intelligence/interpretation_service.py")
        self.assertIn("analyzed_position_create_failed", src)
        self.assertIn("text_position_failed = True", src)

    def test_instrument_id_cache_bounded(self) -> None:
        src = _read("shared/intelligence/position_monitor.py")
        self.assertIn("_INSTRUMENT_ID_CACHE_MAX", src)
        # Use OrderedDict for FIFO eviction.
        self.assertIn("OrderedDict", src)

    def test_manage_index_redirect_relative(self) -> None:
        # Bug I fix (2026-05-24 second-round audit): the redirect used to
        # be a bare relative `location="sources"`, which broke for
        # /manage (no trailing slash) and for sub-path mounts. The new
        # implementation builds the location off `request.path` so it
        # always lands under the same prefix the request came in on. We
        # now assert the request-path-based form, and explicitly assert
        # the old broken hard-coded forms are gone.
        src = _read("shared/intelligence/manage_panel/server_routes.py")
        self.assertIn("base = request.path.rstrip(\"/\")", src)
        self.assertIn('target = f"{base}/sources"', src)
        # Both the absolute-path form AND the bare relative form would be
        # regressions.
        self.assertNotIn('location="/manage/sources"', src)
        # The bare-relative regression: NO call to web.HTTPFound with the
        # plain `"sources"` literal.
        self.assertNotRegex(
            src,
            r"web\.HTTPFound\(location=\"sources\"\)",
        )

    def test_manage_leaderboard_link_relative(self) -> None:
        src = _read("shared/intelligence/manage_panel/templates/leaderboard.html.jinja2")
        self.assertIn('href="trader/{{', src)
        self.assertNotIn('href="/manage/trader/{{', src)


# ---------------------------------------------------------------------------
# Integration: reply-prefix helper behaves correctly
# ---------------------------------------------------------------------------
class TestReplyPrefixHelperBehaviour(unittest.TestCase):
    def test_helper_strips_reply_prefix(self) -> None:
        from shared.utils.reply_prefix import strip_reply_prefix
        self.assertEqual(strip_reply_prefix("[Reply to @bob]: parent\nactual"), "actual")

    def test_helper_returns_empty_for_reply_only(self) -> None:
        from shared.utils.reply_prefix import strip_reply_prefix
        self.assertEqual(strip_reply_prefix("[Reply to @bob]: parent"), "")

    def test_helper_preserves_normal_text(self) -> None:
        from shared.utils.reply_prefix import strip_reply_prefix
        self.assertEqual(strip_reply_prefix("hello world"), "hello world")

    def test_helper_handles_none_and_empty(self) -> None:
        from shared.utils.reply_prefix import strip_reply_prefix
        self.assertEqual(strip_reply_prefix(""), "")
        self.assertEqual(strip_reply_prefix(None), "")


if __name__ == "__main__":
    unittest.main()
