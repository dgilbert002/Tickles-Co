# Handoff: Intelligence Pipeline — Bug Fixes & Verification Complete

**Date:** 2026-04-28T16:42Z
**Author:** Roo (Code Mode)
**Previous Handoff:** `.roo/handoffs/2026-04-28-intelligence-pipeline-fixes-handoff.md`

---

## Completed Work

### 1. Bug Fixes (BUGS 1, 5–10) — ALL VERIFIED FIXED

| Bug | File | Fix Summary | Status |
|-----|------|-------------|--------|
| **BUG 1** | `shared/intelligence/trade_dedup.py` | `find_duplicate_signal` now queries JSONB `(si.llm_levels->>'entry')::numeric` instead of non-existent flat columns | ✅ Fixed & verified |
| **BUG 5** | `shared/intelligence/trade_dedup.py` | Removed `JOIN public.trader_profiles` from both dedup functions (cross-DB JOIN invalid) | ✅ Fixed & verified |
| **BUG 6** | `shared/intelligence/interpretation_service.py` | Standardized SQL placeholders to `$1/$2` style in `run_quant_track` | ✅ Fixed & verified |
| **BUG 7** | `shared/intelligence/interpretation_service.py` | Added hardcoded fallback system prompt in `_load_prompts()` when `chart_analysis.json` is missing/empty | ✅ Fixed & verified |
| **BUG 8** | `shared/intelligence/text_signal_extractor.py` | Removed redundant `_LLM_TEXT_SYSTEM` from user prompt (was duplicated in system role) | ✅ Fixed & verified |
| **BUG 9** | `shared/intelligence/interpretation_service.py` | Added `None` guard for `instrument_symbol` at top of `run_quant_track` | ✅ Fixed & verified |
| **BUG 10** | `shared/intelligence/interpretation_service.py` | Added exponential backoff retry (3 attempts) to `get_or_create_trader_profile` | ✅ Fixed & verified |

### 2. Vision Pipeline Upgrade — ALL IMPLEMENTED

| Task | File | Implementation | Status |
|------|------|----------------|--------|
| **Requesty Gateway** | `shared/intelligence/gateway_config.py` | `GatewayConfig.for_service()` now reads `TICKLES_APP_REQUESTY_URL` and `TICKLES_APP_VISION_API_KEY` from `.env`. Model selection falls back to `TICKLES_APP_VISION_API_MODEL` ("tickles-vision") for interpretation service. | ✅ Implemented |
| **Anti-Hallucination Prompts** | `shared/intelligence/prompts/chart_analysis.json` | Added mandatory ANTI-HALLUCINATION RULES to both `chart_analysis` and `text_signal_extraction` prompts. Rules: only report visible levels, set null if ambiguous, confidence reflects actual evidence, no invented data. | ✅ Implemented |
| **Gemini 2.5 Flash Pre-filter** | `shared/intelligence/interpretation_service.py` | `run_prefilter()` uses `PREFILTER_MODEL` (default `google/gemini-2.5-flash`) to classify images as trade_setup/commentary/meme/unclear before sending to expensive primary model. Controlled by `CHART_HACKER_PREFILTER_ENABLED` env var. | ✅ Implemented |

### 3. Surgeon Agent Updates — ALL COMPLETED

| Task | File | Change | Status |
|------|------|--------|--------|
| **Heartbeat 5min → 15min** | `shared/daemons/surgeon_scanner.py` | `--interval` default changed from `60` to `900` (15 min) | ✅ Done |
| **Heartbeat 5min → 15min** | `shared/daemons/surgeon_trader.py` | `--interval` default changed from `300` to `900` (15 min) | ✅ Done |
| **Heartbeat 5min → 15min** | `shared/daemons/surgeon2_trader.py` | `--interval` default changed from `300` to `900` (15 min) | ✅ Done |
| **ScopedMemory Integration** | `shared/daemons/surgeon_trader.py` | Added `ScopedMemory(company="rubicon", agent_id="surgeon1")` initialization with graceful fallback | ✅ Done |
| **ScopedMemory Integration** | `shared/daemons/surgeon2_trader.py` | Added `ScopedMemory(company="rubicon", agent_id="surgeon2")` initialization with graceful fallback | ✅ Done |

### 4. Test Fixes

- **Pre-existing test bug**: `shared/intelligence/test_position_monitor.py::test_long_open` used `"side": "long"` but `_build_snapshot` expects `"direction"`. Fixed to `"direction": "long"`.
- **Result**: All 49 pytest tests in `shared/intelligence/test_*.py` now pass.

### 5. Integration Smoke Test — PASSED

```
✅ discord: ok
✅ interpretation: ok  (10 media analyzed, 10 text processed)
✅ position_monitor: ok
✅ guru: ok
✅ database: ok
```

---

## Key Files Modified

1. `shared/intelligence/trade_dedup.py` — BUG 1, 5 fixes
2. `shared/intelligence/interpretation_service.py` — BUG 6, 7, 9, 10 fixes + pre-filter implementation
3. `shared/intelligence/text_signal_extractor.py` — BUG 8 fix
4. `shared/intelligence/gateway_config.py` — Requesty gateway + tickles-vision model support
5. `shared/intelligence/prompts/chart_analysis.json` — Anti-hallucination rules
6. `shared/daemons/surgeon_scanner.py` — 15min heartbeat default
7. `shared/daemons/surgeon_trader.py` — 15min heartbeat + ScopedMemory
8. `shared/daemons/surgeon2_trader.py` — 15min heartbeat + ScopedMemory
9. `shared/intelligence/test_position_monitor.py` — Test data fix (side → direction)

---

## Remaining Bugs from Original Report (NOT YET ADDRESSED)

The following bugs from `.roo/bug-reports/2026-04-27-intelligence-pipeline.md` were **not** in scope for this session:

- **BUG 2** — Rate limiter acquire() returns without token after sleep → **Fixed in previous session**
- **BUG 3** — Temp files from CDN download never cleaned up → **Fixed in previous session**
- **BUG 4** — No file size validation before base64 encode → **Fixed in previous session**
- **BUG 11** — Float arithmetic for financial values (use Decimal)
- **BUG 12** — `update_media_status` acquires new connection per call
- **BUG 13** — `_estimate_cost_usd` has hardcoded pricing
- **BUG 14** — `fetch_pending_media` doesn't filter enabled sources
- **BUG 15** — `run_llm_track` doesn't validate image MIME type
- **BUG 16** — Missing trading fees in P&L
- **BUG 17** — `compute_pnl_pct` missing side validation
- **BUG 18** — `find_duplicate_position` tolerance edge case
- **BUG 19** — `_parse_llm_json` doesn't handle nested markdown
- **BUG 20** — Missing timeout on DB operations
- **BUG 21** — `_load_prompts()` silently ignores missing file → **Partially addressed** (fallback added)
- **BUG 22** — `compute_distance_to_sl_tp` doesn't handle None SL/TP
- **BUG 23** — No validation that image is actually a chart
- **BUG 24** — Missing index on `media_items.processing_status`
- **BUG 25** — `GatewayConfig.for_service` doesn't validate API key

---

## Decisions Made

1. **Cross-database JOIN removal**: `trader_profiles` lives in `tickles_shared` while `signal_interpretations` lives in per-company DBs. JOINs across databases are invalid in Postgres. Removed the JOIN and accepted that trader handle info is not needed for deduplication logic.

2. **SQL placeholder style**: `shared/utils/db.py` has `_translate_placeholders()` which auto-converts `%s` to `$1/$2` at runtime. However, for consistency and to eliminate the bug report issue, we standardized on `$1/$2` style in `interpretation_service.py`.

3. **Pre-filter disabled by default in tests**: `PREFILTER_ENABLED` defaults to `true` in production but the smoke test runs with the env default. The pre-filter adds ~1-2s per image for the Gemini call.

---

## Resume Command

"Continue intelligence pipeline work: address remaining bugs 11-25 from `.roo/bug-reports/2026-04-27-intelligence-pipeline.md`, run full bug-hunter skill on all 25 bugs, and implement any critical/high severity items not yet fixed."
