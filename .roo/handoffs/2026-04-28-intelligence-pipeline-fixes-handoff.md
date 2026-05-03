# Handoff: 2026-04-28 — Intelligence Pipeline Enhancements & Bug Fixes

## Completed Work (Today)

### 1. TUI Hierarchy Bug — FIXED
- **Issue**: `get_hierarchy()` in `shared/catalogue/db.py` was querying a non-existent `description` column.
- **Fix**: Updated the SQL query to use `notes AS description`.

### 2. Intelligence Pipeline Bug Hunt — COMPLETED
- **Action**: Performed a deep audit of the intelligence pipeline using the `bug-hunter` skill.
- **Result**: Identified 25 bugs (5 Critical, 5 High, 8 Medium, 5 Low).
- **Report**: Saved to `.roo/bug-reports/2026-04-27-intelligence-pipeline.md` (Note: filename reflects the session start date).

### 3. Critical Bug Fixes — IMPLEMENTED
- **BUG 3 (Temp File Leak)**: Fixed in `shared/intelligence/interpretation_service.py`. Changed `NamedTemporaryFile(delete=False)` to `tempfile.mkstemp()` with proper file descriptor cleanup and `os.unlink()` in a `finally` block.
- **BUG 4 (OOM Risk)**: Fixed in `shared/intelligence/interpretation_service.py`. Added `MAX_IMAGE_BYTES = 10 * 1024 * 1024` (10MB) validation to `_encode_image_b64()` before reading files into memory.
- **BUG 2 (Rate Limiter Bypass)**: Fixed in `shared/intelligence/llm_rate_limiter.py`. Added a `RuntimeError` raise if a token is still unavailable after the cooldown sleep, preventing silent rate limit violations.

## Verbatim Request (2026-04-28)

> "fix all bugs, re run bug hunter and code analyser again when done. then update the .env files with those keys where the tickles vision part of looking at charts or graphs will be from requesty not oprnrouter and using the key specified in the .env file. update all the sergeon agents to check every 15 min instead of 5min (heartbeat) and tell me more about them, their trades, wins, losses, their memory and what they're doing to improve themselves and if its working. update the prompt to not have halucinations for the chart viewer, and have a pre-fileter step using gemini flash to check first if the chart is image and is actually a chart (filtering out gifs and others), the model is in th e.env at the bottom of the file. also the requesty model is also at the bottom for the main chart analyser, note it has a custom name cos i'm using requesty's custom load balancer and fallback. update interpretation to use these. success is where you've run smoke tests, where the prompt is accurate and fetching entry tp and sl. heres a discussion with Claude, ignore the models, but impliment the pre-check and the prompt where it talks abot anti halicination. Pre-filter charts before sending to vision LLM. Not every "media item" is a tradeable chart. Some are: Memes, Profile pictures, Random screenshots, Text screenshots (no chart). Run a cheap classifier first ($0.0005 per image) using a smaller model: quick_classify(image) → "is_chart: true/false, type: candlestick|line|other". Only send is_chart: true candles to the expensive vision pass. Cuts your real load by 30-60% depending on source quality. You can use Gemini 2.5 Flash Lite for this — $0.10/M tokens, 380 TPS, perfect for this filter step. Adds <$2/month and saves $5-10/month in skipped vision calls. Net win. Tell the vision LLM explicitly to record what it CAN'T identify. Don't let it guess at indicators it can't actually see: If you see lines or overlays you cannot identify as a specific indicator, record them in `unknown_overlays` array with description but DO NOT label them as VWAP, EMA, BB, etc. unless you can see explicit labels or legend. If a TP/SL/Entry zone is drawn but no exact price label is visible at that zone, record `levels_drawn_without_labels: true` and set `pattern_confidence` below 0.6. This stops hallucinated indicators and protects the integrity of trader scoring."

## Pending Tasks

### 1. Bug Fixes (Remaining)
- [ ] **BUG 1**: Fix `find_duplicate_signal` SQL schema mismatch in `shared/intelligence/trade_dedup.py`.
- [ ] **BUG 5**: Fix cross-database JOIN in `shared/intelligence/trade_dedup.py`.
- [ ] **BUG 6**: Standardize SQL placeholders to `$1` in `shared/intelligence/interpretation_service.py`.
- [ ] **BUG 7**: Add hardcoded fallback for empty system prompt in `shared/intelligence/interpretation_service.py`.
- [ ] **BUG 8**: Remove redundant system prompt from user message in `shared/intelligence/text_signal_extractor.py`.
- [ ] **BUG 9**: Add None symbol guard in `run_quant_track` in `shared/intelligence/interpretation_service.py`.
- [ ] **BUG 10**: Add retry logic to `get_or_create_trader_profile` in `shared/intelligence/interpretation_service.py`.
- [ ] **BUG 11-25**: Address remaining medium/low severity bugs.

### 2. Vision Pipeline Upgrade
- [ ] Refactor `shared/intelligence/gateway_config.py` to use Requesty URL and `tickles-vision` model from `.env`.
- [ ] Update `shared/intelligence/prompts/chart_analysis.json` with anti-hallucination rules.
- [ ] Implement Gemini 2.5 Flash pre-filter in `shared/intelligence/interpretation_service.py`.

### 3. Surgeon Agent Updates
- [ ] Update `shared/daemons/surgeon_scanner.py`, `surgeon_trader.py`, and `surgeon2_trader.py` heartbeats to 15 minutes.
- [ ] Analyze surgeon performance (trades, wins, losses, memory) and report back.

### 4. Verification
- [ ] Re-run bug-hunter to verify all fixes.
- [ ] Run `integration_smoke_test.py`.
- [ ] Run full test suite `pytest shared/tests/`.

## Key Files
- `shared/intelligence/interpretation_service.py` — Main processing loop.
- `shared/intelligence/gateway_config.py` — LLM gateway configuration.
- `shared/intelligence/trade_dedup.py` — Deduplication logic.
- `shared/intelligence/prompts/chart_analysis.json` — Vision prompts.
- `.env` — API keys and model names.

## Resume Command
"Continue fixing the remaining bugs from the bug-hunter report, then proceed with the vision pipeline upgrade (Requesty + Gemini pre-filter) and surgeon agent heartbeat updates as per the verbatim request."
