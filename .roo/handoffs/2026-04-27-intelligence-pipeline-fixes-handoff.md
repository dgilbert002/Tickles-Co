# Handoff: 2026-04-27 — Critical Intelligence Pipeline Fixes

## Completed Work

### 1. CRITICAL: Media Items Stuck in 'pending' — FIXED
**Root cause**: `BaseCollector.write_to_db()` inserted ALL CDN-hosted URLs (TradingView charts) with `processing_status='pending'`. But `interpretation_service.fetch_pending_media()` only queried `processing_status='downloaded'`.

**Fix #1** (`shared/collectors/base.py` lines 297-316):
- Detect direct images (photo type, image extensions, tradingview.com URLs)
- Mark them as `'downloaded'` instead of `'pending'`

**Fix #2** (`shared/intelligence/interpretation_service.py` lines 580-620):
- Expanded `fetch_pending_media()` to also query `processing_status='pending' AND source_url IS NOT NULL`
- Added `source_url` to SELECT columns
- Added on-the-fly CDN download logic in `_process_one()` to fetch TradingView chart images to temp files before vision LLM analysis

**Result**: 293 previously stuck chart images are now being processed by the vision LLM.

### 2. CRITICAL: Text Signal Extraction — INVESTIGATED
**Finding**: The 10 text items analyzed per cycle all returned `no_signal_detected`. This is CORRECT behavior — they were commentary/meme content, not trade setups. The regex + LLM fallback pipeline is working as designed.

**Verification**: `classify_message_type()` correctly filters memes and short commentary before extraction.

### 3. TUI Escape Bug — FIXED
**Root cause**: `IntPrompt.ask("Channel ID")` traps Ctrl+C and non-integer input in an infinite validation loop.

**Fix** (`shared/catalogue/tui_manager.py` lines 153-167, 228-247):
- Replaced `IntPrompt.ask()` with `Prompt.ask()` + manual validation
- Allows 'x'/'q'/'cancel' to exit gracefully
- Shows clear error message on invalid input

### 4. XNimrod Added to Trader Profiles
**Action**: Direct SQL INSERT into `public.trader_profiles` with `display_name='XNimrod'`, `handle_normalized='xnimrod'`, `platform='discord'`.

**Verification**: Confirmed in DB — 9 traders now tracked.

### 5. Discord Channel Auto-Discovery — IMPLEMENTED
**New file**: `shared/collectors/discord/discover_channels.py`
- Connects to Discord, enumerates ALL visible text channels across all servers
- Preserves existing config (marks as [KEEP])
- New channels default to `enabled: false` so user can choose via TUI
- Outputs JSON ready to merge into `discord_config.json`

**Merged config**: 116 Chart Hackers channels total
- 29 existing (preserved with their enabled flags)
- 87 new (default `enabled: false`)

**Key missing channels now present**:
- `ask-chaoss` (1311027807815467131) — disabled
- `ask-david` (1310592511122739282) — disabled
- `ask-chartprime` (1310593964910317648) — disabled
- `panda-trades` (1458485148713488529) — enabled
- `trader-j-trades` (1458485218477342720) — enabled
- `nagel-trades` (1458571915819221125) — enabled
- `wen-n-tree` (1410985210475974789) — enabled
- `trading-xcelerator` (1305520061217374299) — disabled

### 6. Smoke Test Results
All 5 services pass:
- ✅ discord: ok
- ✅ interpretation: ok (9 media analyzed, 1 skipped due to vision error)
- ✅ position_monitor: ok
- ✅ guru: ok
- ✅ database: ok

Signal interpretations grew from 56 → 65 rows during the test.
Two charts detected as `short` (0.63 confidence) and `long` (0.63 confidence).

### 7. Test Suite
627 tests passed. 10 failures are pre-existing (feature store import errors, Capital gateway streaming, Redis dedup — unrelated to today's changes).

## Files Changed
- `shared/collectors/base.py` — CDN image status fix
- `shared/intelligence/interpretation_service.py` — pending+source_url query, on-the-fly CDN download
- `shared/catalogue/tui_manager.py` — escape bug fix
- `shared/collectors/discord/discover_channels.py` — NEW auto-discovery script
- `shared/collectors/discord/data/discord_config.json` — merged 116 channels

## Known Issues / Next Steps
1. **OpenRouter vision 400 errors**: Some CDN images fail with "Could not process image" / "Provided image is not valid." This is a provider-side issue with certain image formats. The retry logic handles it gracefully (3 retries with backoff, then skip).
2. **Wen-n-tree zero news_items**: Channel IS in config and enabled, but DB shows zero collected messages. May be a high-water-mark issue or the channel may have no recent messages. Monitor after restart.
3. **User action needed**: Run `python3 -m shared.collectors.discord.discover_channels` periodically or on-demand to discover new channels. Use `manage_sources.py` TUI to enable/disable channels.

## Resume Command
"Restart the Discord collector and interpretation service with the updated config, then monitor for trade detections from the newly enabled channels."
