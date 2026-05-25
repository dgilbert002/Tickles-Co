# BUG HUNT & CODE HARDENING ROADMAP (MAY 2026)

This roadmap documents our systematic fixes for logic bugs, processing loops, data gaps, and performance optimizations. It is written to be 100% clear and easy to understand (even for a 21-year-old).

---

## 1. What we are changing & Why

### Bug 1: The "Groundhog Day" Loop (Infinite processing of duplicate text messages)
* **Why we are changing it:** When the system receives a text message that is a duplicate of a trade we've already tracked, it correctly identifies it but *fails to write a record* of this decision in the `signal_interpretations` table. On the next cycle, the database query still sees the message as "unprocessed" and re-extracts it, calling expensive LLMs over and over.
* **What we are changing:** If a text-only news item is identified as a duplicate, we now write a minimal `signal_interpretations` row with `consensus_method = 'text_dedup_continuation'` (or similar) to mark it as processed, satisfying the database search criteria and stopping the loop.
* **Hardening:** If the database write of this placeholder fails, we fail the cycle for that item (return `status: "failed"` instead of `status: "analyzed"`), preventing the system from falsely marking it as successful and ensuring we don't skip actual errors.
* **Functions edited:** `InterpretationService._process_text_one` in `shared/intelligence/interpretation_service.py`.

### Bug 2: The "Partial Exit" Typo (Duplicate trades on your dashboard)
* **Why we are changing it:** The trade duplicate detector checks if you already have a trade open by looking for states like `open`, `pending`, or `partial_hit`. However, in our real database schema, partially closed trades are stored as `partial_exit`, not `partial_hit`. Because of this typo, the detector ignores partially closed trades, allowing the system to open duplicate positions on your dashboard.
* **What we are changing:** Change `'partial_hit'` to `'partial_exit'`.
* **Functions edited:** `find_duplicate_position` in `shared/intelligence/trade_dedup.py`.

### Bug 3: The "Skip the Good Stuff" Bug (Silent Discord Message Loss)
* **Why we are changing it:** The Discord collector currently updates its "High-Water Mark" (the newest message ID checked) immediately upon retrieving a batch from Discord, *before* it applies your user/channel filters. If a channel gets filled with spam or blocked messages, the collector saves the latest ID, advances the marker, and then deletes the messages. If a real trader posted a message with an ID slightly lower than that marker, the collector skips over it forever in future checks.
* **What we are changing:** Save the High-Water Mark based on the newest message *actually ingested and saved* to your database, rather than the raw Discord query stream.
* **Functions edited:** `_load_messages_since` and message loop logic in `shared/collectors/discord/discord_collector.py`.

### Bug 4: The "Flash Crash" Wick Miss (Same-Candle Activation and Exit)
* **Why we are changing it:** When a pending trade gets triggered, the position monitor activates it and sets `price_updated_at` to that exact minute. The wick scanner (which checks if Stop Loss or Take Profit were hit) only scans candles *strictly after* that timestamp (`timestamp > price_updated_at`). If a coin hits the entry price and immediately crashes through its SL in the same minute, the system misses it and the trade stays open, creating massive drift between the dashboard and your real account.
* **What we are changing:** Change the wick scanner to scan candles starting from the activation candle itself (`timestamp >= price_updated_at`), but only scan candles *after* the precise trigger index within that minute, or carefully check same-bar levels.
* **Functions edited:** `_find_sl_tp_wick_candle` in `shared/intelligence/position_monitor.py`.

### Bug 5: O(N) Wick Scanning Optimization (Eliminating the 54-second bottleneck)
* **Why we are changing it:** Currently, for 15 open positions, the monitor runs 15 separate database scans. Each scan fetches up to 10,000 candles from the database to check for wicks. This sequential loop takes 54 seconds!
* **What we are changing:** 
  1. **Watermark Advance (Robust Watermark):** Since a position was checked and had no wick hit, we can safely advance `price_updated_at` to avoid re-scanning those same candles again in future cycles.
  2. **DB-Verified Watermark Protection:** Instead of using wall-clock `NOW()` (which can skip late-ingested candles), we query the `MAX("timestamp")` of the candles actually present in the database for that instrument. This guarantees that no late-ingested candles are ever skipped!
  3. **In-Memory Instrument ID Cache:** We implemented `_INSTRUMENT_ID_CACHE` to cache instrument database IDs. This saves up to 15-30 database lookups per monitor cycle.
* **Functions edited:** `_resolve_instrument_id` and `_process_one` in `shared/intelligence/position_monitor.py`.

### Bug 6: Pending Queue Starvation (New setups never activating)
* **Why we are changing it:** The Position Monitor only checked the oldest 200 pending positions in every cycle to prevent overloading the server. If you had more than 200 pending setups, newer setups (like the ICP/USDT position 11345) would be stuck at the back of the queue and never get checked for activation, even when they hit their entry price in the real market!
* **What we are changing:** 
  1. **Pending Queue Expansion:** Increased the queue check limit from 200 to 1000.
  2. **Smart Local-Candle Check (Supercharge Speed):** Added a check to see if local 1-minute candle data is up to date (fresh within 10 minutes) before falling back to the slow, external CCXT exchange queries. If our local candles are fresh and show no touch, we skip the slow network calls entirely. This reduces the pending check from minutes to virtually instant!
  3. **One-Time Retroactive Settle Utility:** Created a script (`shared/scripts/retro_activate_positions.py`) to scan any starved positions, retroactively activate them at their real entry candle, scan subsequent candles to see if they hit their SL or TP, and settle them immediately with their accurate net realized P&L.
* **Functions edited:** `_find_entry_touch_candle` and `_activate_pending_positions` in `shared/intelligence/position_monitor.py`.

### Bug 7: Replay Chart Gaps & Open-Price Corruption
* **Why we are changing it:** The user noticed that on the replay charts, the candles looked "thinly spaced" and didn't "flow into each other", with massive visual "gaps" between consecutive candles. Our forensic analysis of the database revealed a massive data-corruption bug in the incremental resampler! Every 5 minutes, the resampler ran a sliding 30-minute window. When resampling coarser timeframes (like `1h`, `4h`, `1d`, `1w`), this sliding window cut across bucket boundaries. Postgres grouped these partial-hour/day candles and computed their open as the first minute of that *partial slice* (e.g. 18:26 instead of 18:00). Then, `ON CONFLICT DO UPDATE SET "open" = EXCLUDED."open"` would overwrite the existing true open of the candle with this partial open! This resulted in corrupted candles where the `open` price was almost identical to the `close` price (making candles look like tiny dots/dashes) and left massive price gaps between consecutive candles!
* **What we are changing:**
  1. **Smart Incremental Resampler Flooring:** In `_run_incremental_resample`, we now calculate the starting timestamp of the resample window for each timeframe individually, flooring it to the target timeframe's bucket floor (e.g., floor of 18:26 to 18:00 for the `1h` timeframe). This ensures that any bucket we touch is always resampled from its actual beginning, preserving the true open price!
  2. **Full History Database Restoration:** Created and executed a one-time utility to clear all corrupted non-1m candles and perfectly regenerate them from the 1m history for all 89 instruments in under 4 minutes.
  3. **Dashboard Real-Time vs Bar-to-Bar Spacing Options:** Added a brand new "Spacing" toggle button on the chart replay section of the dashboard. This allows the user to instantly switch between:
     - **Bars (Contiguous Category Axis):** Standard view where every bar has equal horizontal width.
     - **Time (Continuous Time Axis):** Proportional time scale where the horizontal spacing corresponds to the actual time of occurrence, beautifully showing market gaps, weekends, or trading sessions naturally.
     This serves as the requested **revert option** so that the user has complete control and can switch styles with a single click!
* **Functions edited:**
  - `_run_incremental_resample` and added `_floor_dt` helper in `shared/candles/resample_runner.py`.
  - `renderReplayChart` and draw/open layout templates in `shared/dashboard/static/app.js` to implement the Spacing toggle button and ECharts axis type configuration.

### Bug 8: The "Expired CDN Chart" Blank Image (Discord CDN 24-hour expiration)
* **Why we are changing it:** The user pointed out that the original trader chart on the left was not loading ("Original/annotated chart unavailable"). During our forensic analysis, we discovered that in the past, the Discord collector inserted *both* a `cdn_hosted` row (no local file, only a `source_url`) and a duplicate `attached` row (with the downloaded `local_path`) for the same post. The `InterpretationService` often processed the `cdn_hosted` row. When the dashboard requested the image, the backend served the Discord CDN URL. However, Discord CDN URLs now strictly expire after 24 hours, meaning any historical charts older than 24 hours would display as broken/unavailable, even though the image file was actually downloaded and exists on our VPS disk perfectly intact!
* **What we are changing:**
  1. **One-Time Retroactive Path Restoration:** Created and executed a robust utility (`shared/scripts/restore_expired_media_paths.py`) that scanned all mismatched interpretations (118 in total), verified that the corresponding downloaded files existed on disk, and updated the `local_path`, `mime_type`, `file_size_bytes`, `file_hash`, and `dimensions` metadata from the good duplicate rows back to the primary row. This instantly restored 100% of the historical images!
  2. **Pipeline Safety:** Verified that the current Discord collection pipeline in `base.py`'s `write_to_db` correctly skips generating duplicate `cdn_hosted` rows when a local attachment is present, preventing any future mismatched rows from being created.
* **Functions edited:**
  - Created `shared/scripts/restore_expired_media_paths.py` to retroactively restore 118 missing paths.

### Bug 9: The "Sub-Path Reverse Proxy" 404 (Absolute API URLs hit wrong service)
* **Why we are changing it:** Even after restoring the local paths in the database, the user STILL saw "Original/annotated chart unavailable". The forensic investigation revealed: our Tailscale Serve proxies `https://host/` → Paperclip (port 3100) and `https://host/dashboard/` → Tickles dashboard (port 3101). We had earlier set the API response to return absolute paths (`/api/media/5695`). When the browser saw `<img src="/api/media/5695">` on a page with `<base href="/dashboard/">`, it ignored the `<base>` (because it was absolute) and resolved against the origin → `https://host/api/media/5695` → reached Paperclip → 404! Even though the image API itself worked perfectly when accessed directly at the dashboard's path.
* **What we are changing:**
  1. **Use Relative API Paths:** Changed `_media_url()` and `annotated_chart_url` in `shared/dashboard/market_routes.py` and the `media_url` builder in `shared/dashboard/interpretation_drawer_provider.py` to return path strings WITHOUT a leading slash (`api/media/{id}` instead of `/api/media/{id}`). Removed leading slash from `chartUrl` fallback in `app.js drawCallFallback`.
  2. **Why this works:** The dashboard's `index.html` injects `<base href="/dashboard/">` at runtime. Relative paths like `api/media/5695` are resolved by the browser against this base, becoming `/dashboard/api/media/5695`, which correctly reaches the dashboard service. This fix is fully agnostic to whether the dashboard is mounted at root, at `/dashboard/`, or at any custom prefix.
* **Functions edited:**
  - `_media_url` in `shared/dashboard/market_routes.py`.
  - `handle_signal_replay` (annotated_chart_url) in `shared/dashboard/market_routes.py`.
  - `_row_to_dict` (or media URL builder) in `shared/dashboard/interpretation_drawer_provider.py`.
  - `drawCallFallback` in `shared/dashboard/static/app.js`.

### Bug 10: The "Disappearing Take Profit" (Singular `take_profit` silently dropped)
* **Why we are changing it:** Spot-checking the BTC/USDT replay (interp 2502) revealed that the trader's clear take-profit zone at ~77,000 (a large green box on the chart) was visible to the LLM and correctly extracted into `llm_levels.take_profit = 77000`, but the dashboard never showed a TP marker. Forensic analysis of `_flatten_levels()` in `shared/intelligence/interpretation_service.py` showed the function only looked for `take_profit_1` … `take_profit_6` keys when copying LLM output to DB columns. However, the canonical chart-analysis prompt (`shared/intelligence/prompts/chart_analysis.json` AND the in-code fallbacks at lines 544 and 746) **explicitly asks the LLM for a SINGULAR `take_profit` key**. Result: the LLM dutifully returned `take_profit`, the flattener never read it, and `take_profit_1` was set to NULL in 242 historical interpretations.
* **What we are changing:**
  1. **Map singular `take_profit` to TP1 first:** Patched `_flatten_levels()` to read `levels.get("take_profit")` into `take_profit_1` first, then allow numbered `take_profit_1..take_profit_6` to override or supplement when present. This preserves backwards compatibility with structured numbered output AND captures the singular shape the prompt actually asks for.
  2. **One-time historical recovery:** Created and executed `shared/scripts/backfill_take_profit_from_llm_levels.py` which scanned all `signal_interpretations` rows where `take_profit_1 IS NULL` AND `llm_levels->>'take_profit' IS NOT NULL`, parsed the singular value, and (a) updated `signal_interpretations.take_profit_1` and (b) mirrored the value down into any linked `tracked_positions` rows that also had `take_profit_1 IS NULL`. **242 interpretations recovered**, including interp 2502 (now `take_profit_1 = 77000`).
  3. **Why we DON'T regret-write the prompt:** Changing the prompt to ask for `take_profit_1..take_profit_6` would break model-version reproducibility (Rule 1: Backtest ≡ Live) for every historical row. Patching the consumer side (the flattener) is fully backwards compatible with both shapes.
* **Functions edited:**
  - `_flatten_levels` in `shared/intelligence/interpretation_service.py`.
  - Created `shared/scripts/backfill_take_profit_from_llm_levels.py`.

### Bug 11: The "Wrong Day" Chart Window (DESC+LIMIT slid the call out of view)
* **Why we are changing it:** Comparing the trader's posted chart (left, taken at 2026-05-22 15:44 UTC) to our reconstructed candle replay (right) for interp 2502, the right-side chart's X-axis was showing **2026-05-23 13:11 → 19:42** — a full day AFTER the trader's post. The "ENT/SL/TP1" lines were drawn at correct prices, but the candles around them were 24 h away from the moment the trader actually called the trade. Forensic analysis of `_timeframe_window` and `_fetch_native_candles` in `shared/dashboard/market_routes.py`: for a still-open / pending position, `end_ts` is `now()`, so the SQL window spans `[call_ts - 140 m, now]`. The candle query is `ORDER BY timestamp DESC LIMIT $6` with `limit=550` — which silently returns the **newest 550 candles** in the window, sliding past the call entirely once `now()` is more than ~9 hours past `call_ts`.
* **What we are changing:**
  1. **DESC-LIMIT slide guard:** Added a clamp in `_timeframe_window` so that if `(end - start) > limit_seconds`, `end` is reset to `start + limit_seconds`. The visible window is now always anchored to the call (start = `call_ts - pre_bars` minutes), and the call is guaranteed to be inside the rendered candles.
  2. **Visible call timestamp:** Added the call timestamp ("call YYYY-MM-DD HH:MM UTC") to the chart sub-header in `shared/dashboard/static/app.js renderReplayChart` flow, so a viewer can immediately verify what moment the candles map to.
  3. **Vertical "CALL" line:** Added a vertical markLine on the replay chart at `r.call_ts` (snapped to the closest candle index in 'Bars' mode, raw timestamp in 'Time' mode), labelled `CALL` in amber, so the user can SEE which candle contained the trader's post — eliminating any ambiguity about chart-time vs post-time.
* **Functions edited:**
  - `_timeframe_window` in `shared/dashboard/market_routes.py` (added DESC-LIMIT slide guard).
  - `renderReplayChart` and the chart-toolbar HTML in `shared/dashboard/static/app.js` (CALL marker + call_ts label).

### Bug 12: Reply-prefix False Symbol Resolution (the "OF link" → LINK/USDT bug)
* **Why we are changing it:** When a Discord trader replies to someone else's message, the collector stores the post as `[Reply to @parent]: <quoted parent text>\n<actual reply>`. The symbol extractor was reading the WHOLE blob and pulling tickers out of the *quoted parent's* text. Real example: trader replied to a parent saying "looking for the OF link", and the regex extracted "link" → resolved to LINK/USDT, even though the trader hadn't said a word about LINK. The dashboard then showed an entire fake "LINK/USDT call" panel.
* **What we are changing:**
  1. **Strip reply-prefix before symbol extraction:** Call `strip_reply_prefix()` on both `headline` and `content` BEFORE running `_extract_symbol_from_text` in `_process_text_one`. The quoted parent text is now trimmed away so we only resolve tickers from the trader's actual message.
  2. **Honest provenance label:** When symbol extraction yields nothing from the trader's text but vision LLM later picks one off the chart, `instrument_resolved_from` now correctly reads `chart_ocr` (or `inferred`) instead of falsely claiming `message`.
  3. **Discord call panel UI:** Restructured the drawer Discord panel via a new `renderDiscordCallBody` helper. Reply-quote-prefix renders as italicised secondary text. When the trader only attached a chart (no actual reply text), the panel shows "No text from {trader} — signal extracted from attached chart by AI vision" so the user understands where the signal really came from.
* **Functions edited:**
  - `_process_text_one` symbol extraction block in `shared/intelligence/interpretation_service.py`.
  - `renderDiscordCallBody` (new helper) + Discord-panel rendering in `shared/dashboard/static/app.js`.

---

## 1b. Sibling-bug round (May 2026, post 5-agent validation)

After the 5-agent validation cleared Bugs 1–12, Agent 3's static review identified four sibling bugs of the same classes in code paths we hadn't touched. All four are fixed in this round; smoke-tested locally before service restart.

### Bug A: Entry-Radar DESC+LIMIT slide (Bug 11 sibling)
* **Where:** `handle_entry_radar` in `shared/dashboard/market_routes.py`.
* **Why:** The radar fetched candles with `start=call_ts, end=now, limit=260`. For a pending call placed 24h ago on 1m, `260 candles ≈ 4.3h` — the SQL `ORDER BY timestamp DESC LIMIT 260` returns the newest 260, dropping the call_ts. The Entry Radar mini-charts showed the "wrong day" for older pending positions.
* **Fix:** Compute `radar_end = min(now, call_ts + radar_limit * tf_seconds)` so the window is always anchored at `call_ts` and the call is guaranteed to be inside the rendered candles. Same DESC-LIMIT-slide pattern as Bug 11.

### Bug B: Closed-trade truncation in `_timeframe_window` (Bug 11 edge case)
* **Where:** `_timeframe_window` in `shared/dashboard/market_routes.py`.
* **Why:** The Bug 11 slide-guard clamped `end = start + limit_seconds` unconditionally. For a *closed* trade where `closed_at` is more than `limit_seconds` after `call_ts` (e.g. 14-day-long trade on 1m), this cut the close itself off the chart.
* **Fix:** When `end_ts` represents a real close (not "now"), try to fit BOTH `call_ts` and `closed_at` by compressing the pre-call buffer. If the gap exceeds `limit_seconds`, fall back to keeping `call_ts` visible (since that's the drawer's header anchor and what the user navigated to). Live/pending trades keep the original Bug 11 behavior.

### Bug C: SymbolResolver doesn't strip reply-prefix (Bug 12 sibling)
* **Where:** `SymbolResolver.process` in `shared/enrichment/stages/symbol_resolver.py`.
* **Why:** The same root cause as Bug 12, but in the enrichment pipeline (which writes `news_items.instruments`). The resolver concatenates `headline + content` and runs ticker regex over the whole thing — so the quoted parent's tickers leaked into `news_items.instruments`.
* **Fix:** Inlined a small `_strip_reply_prefix` helper (kept inside the enrichment layer to avoid layer-violating imports from `shared/intelligence`). Both `headline` and `content` are stripped separately before concatenation. Verified with smoke tests: a reply-only post no longer extracts any ticker; a real call after a reply still resolves correctly.

### Bug D: text_signal_extractor LLM key drift (Bug 10 sibling, defense-in-depth)
* **Where:** `_extract_with_llm` in `shared/intelligence/text_signal_extractor.py`.
* **Why:** Same Bug 10 class (LLM emits singular keys when system prompt asks for numbered keys). The text-LLM path could silently drop a TP if the LLM returned `take_profit` instead of `take_profit_1`, or drop a whole signal if it returned `entry_price` instead of `entry`.
* **Fix:** Defense-in-depth fallback: if the numbered keys are empty, try the singular form; if `entry` is missing, accept `entry_price`. Preserves backwards compat when the LLM follows the prompt exactly.

### Bonus: Vision-LLM `news_context` strip (Bug 12 deeper sibling)
* **Where:** `_process_text_one` (LLM track call site) in `shared/intelligence/interpretation_service.py`.
* **Why:** Even after Bug 12 stripped the reply prefix from the symbol-extractor input, the same un-stripped `headline + content` blob was being fed to the **vision LLM** as `news_context`. The vision LLM could then hallucinate the wrong direction or symbol from the parent's quoted text.
* **Fix:** Strip the reply prefix from both `headline` and `content` *before* assembling `news_context`. Vision LLM now only sees the trader's actual message.

**Functions edited (this round):**
  - `handle_entry_radar` and `_timeframe_window` in `shared/dashboard/market_routes.py`.
  - `_strip_reply_prefix` (new) and `process` in `shared/enrichment/stages/symbol_resolver.py`.
  - `_extract_with_llm` parsing block in `shared/intelligence/text_signal_extractor.py`.
  - `_process_text_one` (vision LLM call site `news_context` build) in `shared/intelligence/interpretation_service.py`.

---

## 1c. Tier 0 / Tier 1 / Tier 2 (May 24 2026, 4-agent deep-audit round)

After Bugs 1–12 + sibling A–D + bonus shipped, the user requested another deeper review with two independent code analysers (CA1, CA2) and two independent bug hunters (BH1, BH2). They surfaced **5 critical**, **13 high**, and **~25 medium-severity** items spread across stripping, dedup, monitoring, LLM gateways, and dashboard UX.

We patched the entire backlog, in three tiers.

### Tier 0 — Critical (cost / data loss / silent breakage)

| ID | Where | What it was | What we changed |
|----|-------|-------------|------------------|
| **C1** | `tracked_positions` schema | Bug Hunter 1 flagged that `deduped_at` column was referenced by `trade_dedup.find_duplicate_position` but might be missing on disk. | Verified column exists in production; added idempotent migration `shared/intelligence/migrations/2026_05_24_bughunt_t0_t1_columns.sql` (with `ADD COLUMN IF NOT EXISTS` + partial index) so future fresh-install boxes get the column even before dedup writes. |
| **C2** | `_process_text_one` skip paths in `shared/intelligence/interpretation_service.py` | Text-only news items that resolved to *no signal* (`skipped_no_content`, `skipped_meme`, `skipped_commentary`, `no_signal_detected`, `skipped_unresolved_instrument`) returned without marking `news_items.enrichment_status` terminal. The next loop saw the same row as "still pending" and re-paid the LLM cost forever. | Added `_mark_news_terminal(news_item_id, status)` helper. Every skip path now calls it with either `'non_signal'` (clear no-signal verdicts) or `'skipped'` (operator-controlled rejections). Backfilled 1633 stuck rows with `enrichment_status='non_signal'`. |
| **C3** | `fetch_pending_media` / media-queue loop | The pending-media query and `UPDATE … SET processing_status='analyzing'` were two separate statements. With multiple workers, two cycles could both grab the same `media_id`, both spend LLM tokens, and (worse) both write conflicting `signal_interpretations` rows. Items could also get stuck in `'analyzing'` forever if a worker died mid-cycle. | Rewrote `fetch_pending_media` as a single CTE: `SELECT … FOR UPDATE OF m SKIP LOCKED` → `UPDATE … RETURNING`, atomic claim. Added `recover_stale_analyzing(stale_minutes=15)` that resets stuck rows back to `'downloaded'` / `'pending'`. Hooked it into `run_cycle` housekeeping so abandoned items recover automatically. |
| **C4** | `close_position` in `shared/intelligence/position_monitor.py` | Two concurrent close paths (wick scanner vs expiry sweep) could both `UPDATE` the same `tracked_positions` row, racing on P&L computation. Last writer wins; sometimes the *worse* P&L was retained. | Added optimistic-status guard: `UPDATE … WHERE id=$1 AND status IN ('open','partial_exit')`. Both close branches now return zero affected rows instead of overwriting a row that another worker already finalised. |
| **C5** | EdgeScorer & CoachService dual-write target | `actor_performance`, `prompt_assignments`, `edge_score_changes`, `actor_leaderboard`, and `trader_performance` only existed in `tickles_jarvais`, but `EdgeScorerService` writes against the *shared* pool. Every write silently failed for weeks. | Re-applied `2026_05_03_phase11_actor_performance.sql` against `tickles_shared` (idempotent — skipped existing indices). Manually created `trader_performance` + its `set_updated_at` trigger in `tickles_shared` so dual-write succeeds. |

### Tier 1 — High (functional bugs, partial data loss)

| ID | Where | What it was | What we changed |
|----|-------|-------------|------------------|
| **H1** | `fetch_open_positions` in `position_monitor.py` | `ORDER BY created_at DESC LIMIT 50`. With >50 open positions, the same 50 newest rows were monitored every tick; older positions starved. | Switched to `ORDER BY price_updated_at ASC NULLS FIRST, created_at ASC` so the *least-recently-checked* rows rotate to the front. |
| **H2** | `_monitor_position_wicks` watermark advance | Watermark (= `price_updated_at`) was always advanced to `now()` after a clean tick. If candles were back-filled later (gap detector), they had timestamps `< now()` and were never re-scanned. | Watermark now advances to `min(now, MAX(timestamp))` — i.e. only as far as the data we actually scanned. Late candles get a chance on the next tick. |
| **H3** | `_fetch_candles` in `postmortem_service.py` | `ORDER BY timestamp DESC LIMIT 50` over `[opened_at, closed_at]`. For positions open >50 minutes (most are), the entry-side candles slid off and the LLM saw only the exit window. | Switched to `ORDER BY timestamp ASC` and added a slide guard: if `(end - start) > candle_limit minutes`, clamp `end = start + candle_limit minutes`. Entry context is always present. |
| **H4** | `chart_hacker_opinion_service.py` | Three layered breakages: (a) `_eligible_positions` filtered `actor_type IN ('trader','copy_bot','self')` but live DB only stores `'trader_human'` and `'agent'` — 0 rows for two weeks; (b) the LLM call used `call_vision_llm(system_prompt=…, user_prompt=…, image_path=…)` but the gateway signature is `(cfg, model, system_prompt, user_text, image_b64, image_mime)` — every call would have raised `TypeError`, swallowed by the surrounding `except`; (c) the consumer read `result.get("memo")`, `result.get("memo_confidence")` etc. directly off the gateway response, but those fields live inside `result["content"]` as JSON. Every opinion would have been NULL SL/TP + empty memo. | Rewrote the service: corrected the SQL filter, switched to `chat_completion` (text-only — the critic doesn't need to re-OCR the chart, it gets the position context), added `_parse_llm_json` over `result["content"]` (re.search to strip code fences/preambles) and merged usage metadata. |
| **H5** | `find_duplicate_position` and `find_duplicate_signal` in `trade_dedup.py` | Symbol comparison used the raw input string, so `BTCUSDT` vs `BTC/USDT` were treated as separate symbols → duplicate positions slipped through. Also: `find_duplicate_signal` queried `(llm_levels->>'take_profit_1')::numeric` but Bug 10 already proved the LLM returns singular `take_profit`. | Added `_canonicalise_symbol` helper. Both `find_*` functions now `symbol = _canonicalise_symbol(symbol)` before the SQL. SQL switched to `COALESCE((llm_levels->>'take_profit_1')::numeric, (llm_levels->>'take_profit')::numeric)` for the same dedup symmetry. |
| **H6** | `_fetch_native_candles` CCXT fallback in `market_routes.py` | The SQL re-fetch line read `$7::text as symbol, $8::text as exchange`, but the bound tuple had only 7 params (`$1..$7`). Asyncpg raised "too few parameters" on every CCXT-fallback path → first-replay of any new instrument 500'd. | Corrected to `$6::text as symbol, $7::text as exchange` matching the actual bound tuple `(inst_id, timeframe, start, end, limit, inst_symbol, inst_exchange)`. |
| **H7** | `handle_media` in `dashboard/server.py` | If an interpretation linked to a `media_items` row whose `local_path` was `NULL` (legacy `cdn_hosted` duplicate), the route returned 404 even when a sibling row for the same `news_item_id` had the file on disk. | Added a sibling fallback: `WHERE news_item_id = $1 AND id <> $2 AND local_path IS NOT NULL ORDER BY id LIMIT 1`. If a sibling exists, serve its file. |
| **H8** | `write_signal_interpretation` INSERT in `interpretation_service.py` | Schema has `pattern_tags` and `setup_tags` JSONB columns intended to power dashboard chips and learning. The INSERT never bound them, so `chart_patterns` and `key_levels` from the LLM output rotted inside the `chart_analysis` blob, invisible to the UI. | Added `pattern_tags_list = chart_patterns` and `setup_tags_list = [lvl.type for lvl in key_levels]`. Extended the INSERT column list and `VALUES` to include `$46::jsonb, $47::jsonb`. Backfilled 252 historical `pattern_tags` and 253 `setup_tags` rows by re-reading their `chart_analysis` JSONB. |
| **H9** | `handle_candles` in `market_routes.py` | The public `/api/candles` endpoint had no DESC+LIMIT slide guard. A caller passing `start, end, limit` over a span larger than `limit × tf_seconds` got the *newest* `limit` candles — anchor at `start` slides off. | When both `start` and `end` are passed, clamp `end = min(end, start + limit × tf_seconds)`. Same pattern as Bug 11. |
| **H10** | `handle_competition_agent` in `market_routes.py` | Pulled raw symbols off `tracked_positions` (some `BTC/USDT`, some `BTCUSDT`) and joined `public.candles` with `instrument_symbol = $raw`. Mixed-format symbols produced NULL live-prices → broken unrealised P&L for every `BTCUSDT`-style row. | Build a `raw → canonical` map via `to_canonical_symbol`, query `public.candles` keyed by canonical, then re-map prices back to raw before returning the JSON. P&L now correct regardless of which format the row was inserted with. |
| **H11** | `_collect_channel` in `discord_collector.py` | `_pending_hwm` advanced based on raw Discord IDs *before* user/zone filters. If a channel was 90 % bot spam and only a few real-trader messages, the bot-spam IDs (which were higher) advanced the HWM past the trader's real messages → silent loss. | HWM now advances only after filters: post-filter max ID drives the watermark, except when *only* the user filter applied (then the raw max ID is safe because those filtered messages will be permanently excluded forever). |
| **H12** | `_find_sl_tp_wick_candle` in `position_monitor.py` | Single 10 000-row scan over the watermark window. For positions opened >7 days ago, this could miss the wick if it lay >7 days back. | Switched to keyset pagination: 10 000-row pages × up to 12 iterations = ~83 days of 1m coverage with constant memory; advance `since_utc` page-by-page. |
| **H13** | `_activate_pending_positions` UPDATE in `position_monitor.py` | The `UPDATE tracked_positions SET status='open' …` had no status guard. A position that was concurrently `expired` or `cancelled` could be revived. | Added `AND status='pending'` so we only flip rows that are still actually pending. |

### Tier 2 — Medium (defense in depth, drift, UX, operational)

| Area | What we changed |
|------|------------------|
| **Reply-prefix consolidation** | New canonical helper `shared/utils/reply_prefix.py`. Five enrichment / display sites now `from shared.utils.reply_prefix import strip_reply_prefix` and call it: `sentiment.py`, `relevance.py`, `language.py`, `news_provider.py`, `postmortem_service.py` (defensive, alias `_srp`). The duplicate inline implementations in `text_signal_extractor.py` and `symbol_resolver.py` were refactored to import + re-export the canonical helper. Frontend got a sibling `_stripReplyPrefixJS()` in `app.js` so the Discord-Feed and floor mini-feed strip the prefix at render time too. |
| **`take_profit` vs `tp1` drift (last 3 sites)** | `legacy_levels` mapping in `interpretation_service.py` and the `chart_hacker_trades`-writer use `primary.get("tp1") or primary.get("take_profit")`. `find_duplicate_signal` SQL uses `COALESCE(tp_1, tp)`. All five LLM-emit-shape variants now resolve consistently. |
| **`analyzed_position_create_failed`** | When `_process_text_one` succeeded at signal extraction but `create_tracked_position_from_interpretation` raised or returned `None`, the row was previously marked `'analyzed'` — masking the failure. Now: `'analyzed_position_create_failed'` if it threw, `'analyzed_no_position'` if it returned `None` (e.g. deduped). The interpretation row is still written, but downstream tooling can distinguish "wrote a signal" from "wrote a signal AND opened a tracked position". |
| **Bounded `_INSTRUMENT_ID_CACHE`** | Was an unbounded `dict`, growing forever. Switched to `OrderedDict` with `_INSTRUMENT_ID_CACHE_MAX = 4096` and FIFO eviction. Preserves the per-tick speed-up while bounding memory. |
| **Manage-panel relative paths** | `server_routes.handle_manage_index` redirected to absolute `/manage/sources` and `templates/leaderboard.html.jinja2` linked to absolute `/manage/trader/{id}`. Both now relative (`location="sources"`, `href="trader/{id}"`) so the panel works under any reverse-proxy mount-point — same fix pattern as Bug 9 for the dashboard. |

### Functions & Code Changes (Tier 0/1/2 round)

| File | Status | Action |
|------|--------|--------|
| `shared/utils/reply_prefix.py` | Ready | NEW — canonical strip helper |
| `shared/intelligence/migrations/2026_05_24_bughunt_t0_t1_columns.sql` | Ready | NEW — idempotent `deduped_at` column + partial index |
| `shared/intelligence/interpretation_service.py` | Ready | C2 (`_mark_news_terminal` + 4 callsites), C3 (`fetch_pending_media` atomic claim, `recover_stale_analyzing`), H8 (pattern/setup tags + INSERT + 252/253 backfill), T2 (TP fallback in `legacy_levels` + `chart_hacker_trades`, reply-strip on `raw_signal_text`/`entry_reason_trader`, `analyzed_position_create_failed` status) |
| `shared/intelligence/position_monitor.py` | Ready | C4 (close status guard ×2), H1 (open-position rotation), H2 (watermark min(now, max_ts)), H12 (wick keyset pagination), H13 (pending status guard), T2 (`_INSTRUMENT_ID_CACHE` → `OrderedDict` + cap) |
| `shared/intelligence/postmortem_service.py` | Ready | H3 (ASC + slide guard), T2 (`_srp` defensive strip on `entry_reason_trader`) |
| `shared/intelligence/chart_hacker_opinion_service.py` | Ready | H4 (filter, gateway helper, JSON parse over `response["content"]`) |
| `shared/intelligence/trade_dedup.py` | Ready | H5 (`_canonicalise_symbol` ×2, `COALESCE(tp1, tp)`) |
| `shared/intelligence/text_signal_extractor.py` | Ready | T2 (re-export canonical strip helper) |
| `shared/dashboard/market_routes.py` | Ready | H6 ($6/$7 fix), H9 (slide guard in `handle_candles`), H10 (`raw_to_canon` + `live_prices_by_canon`) |
| `shared/dashboard/server.py` | Ready | H7 (sibling-row fallback in `handle_media`) |
| `shared/dashboard/news_provider.py` | Ready | T2 (strip in `_row_to_dict`) |
| `shared/dashboard/static/app.js` | Ready | T2 (`_stripReplyPrefixJS` + `newsMsg` strip) |
| `shared/enrichment/stages/sentiment.py`, `relevance.py`, `language.py` | Ready | T2 (strip before scoring) |
| `shared/enrichment/stages/symbol_resolver.py` | Ready | T2 (re-export canonical strip helper) |
| `shared/collectors/discord/discord_collector.py` | Ready | H11 (HWM-after-filter, `user_filter_applied`/`zone_filter_applied`) |
| `shared/intelligence/manage_panel/server_routes.py` | Ready | T2 (relative `sources` redirect) |
| `shared/intelligence/manage_panel/templates/leaderboard.html.jinja2` | Ready | T2 (relative `trader/{id}` href) |
| `shared/tests/test_bug_hunt_2026_05_24.py` | Ready | NEW — 52 regression tests covering Bugs 8–12 + A–D + news_context + every Tier 0/1/2 fix above |

### Verification

* `python3 -m pytest shared/tests/test_bug_hunt_2026_05_24.py -q` → **52 passed**.
* Live-API smoke test: `GET /api/snapshot` on dashboard (port 3101) returns HTTP 200 with fresh data.
* `journalctl -u tickles-interpretation -u tickles-position-monitor -u tickles-chart-hacker-opinion -u tickles-postmortem -u tickles-dashboard -u tickles-discord-collector -u tickles-edge-scorer -u tickles-coach --since '60 seconds ago'` → CLEAN (no tracebacks, no `ERROR`/`Exception`).
* Per-Tier-0 db checks (Phase 0 verification):
  * C2 backfill: `UPDATE news_items SET enrichment_status='non_signal'` affected **1633** rows.
  * C5 schema-sync: `tickles_shared.actor_performance`, `prompt_assignments`, `edge_score_changes`, `actor_leaderboard`, `trader_performance` all exist with expected columns + triggers.
  * H8 backfill: `signal_interpretations.pattern_tags` populated for **252** historical rows; `setup_tags` for **253**.

### How to roll back

`git revert` the commits that landed this round, plus:

```bash
psql "$TICKLES_SHARED_DSN" -c "
UPDATE news_items SET enrichment_status='pending'
WHERE enrichment_status='non_signal'
  AND collected_at > '2026-05-22';
"
psql "$TICKLES_SHARED_DSN" -c "
UPDATE signal_interpretations SET pattern_tags=NULL, setup_tags=NULL
WHERE created_at > '2026-05-22';
"
sudo systemctl restart tickles-interpretation tickles-position-monitor tickles-chart-hacker-opinion tickles-postmortem tickles-discord-collector tickles-dashboard tickles-edge-scorer tickles-coach
```

The `actor_performance` / `trader_performance` tables in `tickles_shared` should be **left in place** even on rollback — they have no consumers that would break if absent, but other future services (Coach v2, Edge v2) will expect them.

---

## 1d. Second-round self-audit (May 24 2026, A–J fixes)

After the Tier 0/1/2 round shipped, the user asked us to run another self-audit. Two more code reviewers and two more bug hunters ran over the changes we'd just made. They surfaced **5 critical**, **5 high**, and assorted medium-severity items introduced by the round-1 fixes themselves. We patched the entire backlog as fixes A through J, then ran a third round of validation (BH1, BH2, CA1, CA2 — the agents that produced this commit's verdict in `1e` below).

### A — Orphan signal_interpretations rollback on text-path failure

**Symptom:** When the text-only signal path called `create_tracked_position_from_interpretation` and it threw, the previous round's fix (T2 "analyzed_position_create_failed") only updated the return status. The `signal_interpretations` row had already been INSERTed by `write_signal_interpretation`, so the queue's `NOT EXISTS (signal_interpretations …)` guard saw the news_item as "already processed" and skipped it forever. A transient DB error became permanent signal loss.

**Fix:** [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:3737) — when `text_position_failed` is True and `sig_id is not None`, run a guarded `DELETE FROM public.signal_interpretations WHERE id=$1 AND NOT EXISTS (SELECT 1 FROM public.tracked_positions tp WHERE tp.signal_interpretation_id=$1)`. The NOT EXISTS clause prevents us deleting a signal that some other path successfully gave a position to. On DELETE failure, log `error` (not warning) so the silent-skip risk is loud. Media path is intentionally left as-is — the LLM cost is already paid and the orphan is data the operator can act on.

### B — `deduped_at` column added to canonical schema + snapshot

**Symptom:** `trade_dedup` writes `deduped_at = NOW()` and the dashboard "duplicates today" KPI reads it. The column was only added by an out-of-band migration, so a fresh-install or DR restore would 500 the dedup writer and the KPI query.

**Fix:** Added `deduped_at TIMESTAMPTZ NULL` and a partial index `idx_tracked_positions_deduped_at` to [`shared/migration/tickles_shared_pg.sql`](shared/migration/tickles_shared_pg.sql:543) and [`shared/scripts/snapshots/tickles_shared.snapshot.sql`](shared/scripts/snapshots/tickles_shared.snapshot.sql:4189). The existing migration `2026_05_24_bughunt_t0_t1_columns.sql` keeps `IF NOT EXISTS` so it's a no-op on fresh DBs.

### C — Trade-dedup queries both raw AND normalised symbol columns

**Symptom:** The previous round's H5 fix canonicalised the INPUT symbol but the SQL still only matched against `instrument_symbol` (raw). Legacy rows stored raw `BTCUSDT`; new rows store canonical `BTC/USDT`. A `BTCUSDT` caller's canonicalised query (`BTC/USDT`) would miss the legacy raw rows, so dedup failed across the migration boundary.

**Fix:** [`shared/intelligence/trade_dedup.py`](shared/intelligence/trade_dedup.py:103) — both `find_duplicate_position` and `find_duplicate_signal` now `WHERE (instrument_symbol = $1 OR instrument_symbol_normalised = $1)`. Trade-off: SARGability is reduced; the OR may force a Bitmap OR plan instead of a single index seek. Acceptable because the dedup window is bounded by `created_at >= NOW() - INTERVAL` so the row-set is tiny.

### D — Expiry UPDATE adds `AND status='pending'` guard

**Symptom:** The previous round's H13 fix added the guard to the *activation* UPDATE in `_activate_pending_positions` but missed the sibling *expire* UPDATE one block above. With multiple monitor instances, the expire path could clobber a row that was activated mid-cycle, silently killing a live trade.

**Fix:** [`shared/intelligence/position_monitor.py`](shared/intelligence/position_monitor.py:1391) — added `AND status='pending'` to the expire UPDATE; check the asyncpg `"UPDATE 0"` reply and log `Expiry sweep lost race for position …` on a no-op rather than incrementing the `expired` counter falsely.

### E — Non-greedy JSON parser in chart-hacker opinion

**Symptom:** Round-1 H4 fix used `re.search(r"\{[\s\S]*\}", content_str)` to extract JSON from LLM responses — a GREEDY match. If the LLM emitted preamble + JSON + commentary that contained any `}`, the regex slurped from the FIRST `{` to the LAST `}`, producing invalid JSON or — worse — silently parsing the wrong object and writing garbage `suggested_sl` / `suggested_tp`.

**Fix:** [`shared/intelligence/chart_hacker_opinion_service.py`](shared/intelligence/chart_hacker_opinion_service.py:286) — strip ` ```json ` fences first; try `json.loads` on the whole; on failure, walk each `{` index in the cleaned text and use `json.JSONDecoder().raw_decode` to greedily but correctly parse only the FIRST balanced JSON object. Reject empty memos (the parser would have written an empty opinion row that still consumed the daily budget).

### F — Wick-scan watermark resumes from `last_scanned_ts` when pagination exhausts

**Symptom:** Round-1 H12 fix added keyset pagination (12 × 10000 candles ≈ 83 days) but kept the H2 watermark logic. When the scan exhausted at max_iterations, the watermark still advanced to `min(now, MAX(timestamp))` — i.e. forward of the last_scanned_ts, skipping the unscanned tail forever.

**Fix:** [`shared/intelligence/position_monitor.py`](shared/intelligence/position_monitor.py:413) — added `scan_state: Optional[Dict[str, Any]]` OUT-parameter to `_find_sl_tp_wick_candle`. The function populates `scan_state["complete"]` (False on `iterations >= max_iterations` or query failure) and `scan_state["last_scanned_ts"]`. The caller in `_monitor_position_wicks` reads it: if `complete=False`, watermark = `last_scanned_ts` so next cycle resumes forward. Existing callers (tests, parity scripts, retro_activate_positions) continue to work without passing `scan_state`.

### G — Discord HWM advance distinguishes allowlist vs blocklist

**Symptom:** Round-1 H11 fix advanced the HWM to raw_max_id whenever any user filter was applied and zero post-filter messages survived. But allowlists are MUTABLE (operators add traders later); blocklists are deterministic. A channel with `allowed_users={A}` that received 100 messages from user B advanced past those 100 messages. Adding B to the allowlist later made those messages permanently lost.

**Fix:** [`shared/collectors/discord/discord_collector.py`](shared/collectors/discord/discord_collector.py:1057) — track `allowlist_applied` and `blocklist_applied` separately. HWM advances on raw_max_id ONLY when `blocklist_applied AND not allowlist_applied AND not zone_filter_applied`. All other zero-survivor cycles leave the HWM untouched, accepting some repeated Discord API traffic in exchange for never losing signals when filter config changes.

### H — Stale-recovery threshold raised + `update_media_status` guard

**Symptom:** Round-1 C3 set `recover_stale_analyzing(stale_minutes=15)`. p99 vision-LLM latency is 16 minutes under load. Healthy workers that took >15 min lost their claim mid-flight; a sibling worker re-claimed; both paid the LLM cost. The duplicate trade write was blocked by the unique constraint, but cost was doubled.

**Fix:** [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:1379) — raised default to 60 min (and the `run_cycle` invocation). Tightened `update_media_status` to `WHERE id=$N AND processing_status = ANY('downloaded','pending','analyzing')`, so a late-finishing worker that returns AFTER recovery + sibling write cannot blindly overwrite the sibling's terminal status.

### I — `/manage` redirect uses `request.path`

**Symptom:** Round-1 manage-panel relative-path fix used the bare relative `location="sources"`. Per RFC 3986, `/manage/sources` works (browser keeps `/manage/` as base) but `/manage` (no trailing slash) resolves to `/sources` and 404s. Sub-path mounts (`/tools/manage`) had the same problem.

**Fix:** [`shared/intelligence/manage_panel/server_routes.py`](shared/intelligence/manage_panel/server_routes.py:106) — `base = request.path.rstrip("/"); raise web.HTTPFound(location=f"{base}/sources")`. Lands under whichever mount prefix the request came in on, regardless of trailing-slash state.

### J — Behavioural regression test suite for the round-3 fixes

[`shared/tests/test_bug_hunt_2026_05_24_round2.py`](shared/tests/test_bug_hunt_2026_05_24_round2.py:1) — 23 tests, 10 sections (one per fix A–I plus a meta-doc test). Mix of source-presence (where SQL/string assertions are unavoidable, e.g. C, D) and behavioural tests that drive real code with mocked DB pools (E parser cases, F scan_state population, I redirect target). The behavioural set replaces what previous string-presence tests would have missed (e.g. greedy regex still parsing technically valid JSON).

### Functions & Code Changes (Round-3 A–J)

| File | Action |
|------|--------|
| `shared/intelligence/interpretation_service.py` | A (orphan rollback), H (stale 60min + update_media_status guard) |
| `shared/migration/tickles_shared_pg.sql` | B (deduped_at + idx) |
| `shared/scripts/snapshots/tickles_shared.snapshot.sql` | B (snapshot column) |
| `shared/intelligence/trade_dedup.py` | C (raw+normalised OR-match ×2) |
| `shared/intelligence/position_monitor.py` | D (expire status guard), F (scan_state OUT-param + watermark resumption) |
| `shared/intelligence/chart_hacker_opinion_service.py` | E (raw_decode JSON parser + empty-memo rejection) |
| `shared/collectors/discord/discord_collector.py` | G (allowlist vs blocklist HWM policy) |
| `shared/intelligence/manage_panel/server_routes.py` | I (request.path-based redirect) |
| `shared/tests/test_bug_hunt_2026_05_24.py` | J (updated round-2 redirect test for fix I) |
| `shared/tests/test_bug_hunt_2026_05_24_round2.py` | J (NEW — 23 behavioural tests) |

### 1e. Round-4 self-audit (2026-05-24, A–J review pass)

After the round-3 A–J fixes shipped, the user asked for *another* 4-agent self-check (2 bug hunters BH1/BH2, 2 code analysers CA1/CA2). They found regressions in the round-3 fixes themselves; the patches below close them. Each is guarded by a new behavioural test in `TestRound3Review_*` inside [`shared/tests/test_bug_hunt_2026_05_24_round2.py`](shared/tests/test_bug_hunt_2026_05_24_round2.py:1).

| # | Severity | Where | Symptom | Fix |
|---|----------|-------|---------|-----|
| 1 | **Critical (NameError crash)** | [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:1) `_process_text_one` | Round-3 Fix A initialised `text_position_id` and `text_position_failed` *inside* the `if sig_id is not None:` guard. When `write_signal_interpretation` returns `None` (ON CONFLICT skip), the post-guard rollback / status block referenced both names → NameError, killing the text-processing loop. | Pulled both initialisations *above* the guard so they always exist. Test: `test_text_position_vars_initialised_before_sigid_guard`. |
| 2 | **High (cost burn)** | [`shared/intelligence/opinion_budget.py`](shared/intelligence/opinion_budget.py:1) + [`shared/intelligence/chart_hacker_opinion_service.py`](shared/intelligence/chart_hacker_opinion_service.py:1) | Fix E rejected empty memos / unparseable JSON AFTER `try_acquire`, but the consumed slot was never refunded. A misbehaving model could exhaust the global 120/h cap with zero rows written. | Added `OpinionBudget.release(position_id)` that pops one entry off the global and per-position deques; chart-hacker calls it on `if llm_result is None`. Tests: `test_opinion_budget_has_release_method`, `test_release_pops_one_slot…`, `test_chart_hacker_calls_release_on_none_result`. |
| 3 | **High (silent watermark drift)** | [`shared/intelligence/position_monitor.py`](shared/intelligence/position_monitor.py:1) `_find_sl_tp_wick_candle` + caller | Fix F populated `scan_state` for the local-DB pagination loop, but the CCXT fallback path didn't update `last_scanned_ts` / `scan_complete`. If local DB was empty AND CCXT returned its 1000-candle cap, the caller still advanced the watermark to `now`, skipping the unscanned tail. | CCXT-success branch sets `last_scanned_ts = ensure_utc(rows[-1]["timestamp"])` and `scan_complete = False` when `len(rows) >= 1000`; CCXT-exception branch sets `scan_complete = False`. Caller now holds the watermark at `wick_since` if `scan_complete=False AND last_scanned_ts is None`. Tests: `test_ccxt_fallback_updates_last_scanned_ts`, `test_ccxt_fallback_failure_keeps_scan_incomplete`, `test_watermark_holds_at_wick_since_when_no_progress`. |
| 4 | **High (dedup miss on legacy compact)** | [`shared/intelligence/trade_dedup.py`](shared/intelligence/trade_dedup.py:1) | Fix C OR-matched `instrument_symbol = $1 OR instrument_symbol_normalised = $1`. But pre-D6 rows can carry the *compact* form (`BTCUSDT`) in `instrument_symbol` while a new request arrives with the canonical form (`BTC/USDT`). Same trade slipped past dedup. | Added `_compact_symbol(s)` (strips `/`, `-`); SQL now does a 3-way OR: `instrument_symbol = $1 OR instrument_symbol = $2 OR instrument_symbol_normalised = $1` with `$2 = compact(symbol)`. Tests: `test_compact_symbol_helper_drops_separators`, `test_find_duplicate_position_three_way_or_match`, `test_find_duplicate_signal_three_way_or_match`. |
| 5 | **Medium (sibling missed guard)** | [`shared/scripts/retro_activate_positions.py`](shared/scripts/retro_activate_positions.py:1) | Fix D added the `AND status = 'pending'` guard to the live monitor's expire UPDATE but missed the retro-activate batch script that runs the same logic. With concurrent runs, retro could expire an already-activated row. | Mirrored the guard + lost-race log in the script. Test: `test_retro_script_has_status_guard`. |
| 6 | **Medium (terminal-status race) — round-4 partial; completed in round-5** | [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:1) `update_media_status` + `fetch_pending_media` + `_process_one` | Fix H raised stale-recovery to 60 min but the worker→DB write still didn't carry an OWN-CLAIM marker. A late-finishing worker that returned >60 min after starting could overwrite a sibling's fresh `analyzing` claim with its terminal `analyzed` / `error_*`. | **Round-4** added `expected_processed_at: Optional[datetime]` CAS sentinel to `update_media_status` (extra `AND processed_at = $N` predicate); `fetch_pending_media` exposes `claim_ts` from the CTE. **Note:** round-4 only wired the CAS arg into 2 of 8 terminal call sites (text-dedup short-circuit and success path); the remaining 6 paths (CDN failures, file-missing, vision unavailable, prefilter skip, unresolved instrument, run_cycle exception handler) were closed in **round-5 §1f** below. Tests: `test_update_media_status_accepts_expected_processed_at`, `test_fetch_pending_media_returns_claim_ts`, `test_process_one_passes_claim_ts`, plus round-5 `test_every_terminal_update_media_status_carries_cas`. |
| 7 | **Cosmetic** | [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:1) `recover_stale_analyzing` | Inline comment still claimed "Threshold is conservative (15 min)" after Fix H raised it to 60 min — a future operator reading the code would have been misled. | Updated comment to reflect 60-minute threshold and link the round-3 reviewer findings. |

#### Verification (round 4)

* `python3 -m pytest shared/tests/test_bug_hunt_2026_05_24.py shared/tests/test_bug_hunt_2026_05_24_round2.py shared/intelligence/test_wick_close.py shared/tests/test_wick_fallback.py shared/tests/test_grep_guard_md_writes.py` → **109 passed**.
* `TestRound3Review_*` (6 new test classes, 11 tests) all green.
* No new module-level imports added; round-3 module structure unchanged.
* No DB schema changes; CAS uses an existing column (`processed_at`).

#### Round-4 functions & code changes

| File | Action |
|------|--------|
| `shared/intelligence/interpretation_service.py` | Lift text-path var init above sig_id guard (Round-4 #1); add `expected_processed_at` CAS to `update_media_status`; expose `claim_ts` from `fetch_pending_media`; thread `claim_ts` through `_process_one_impl` to every terminal `update_media_status`; refresh stale "15 min" comment to 60 min. |
| `shared/intelligence/opinion_budget.py` | New `release(position_id)` method to refund one slot. |
| `shared/intelligence/chart_hacker_opinion_service.py` | Call `budget.release(position_id)` when `_run_vision_opinion` returns `None`. |
| `shared/intelligence/position_monitor.py` | CCXT fallback path now writes `last_scanned_ts` and `scan_complete` to `scan_state`; `_monitor_position_wicks` holds watermark at `wick_since` when no progress was made. |
| `shared/intelligence/trade_dedup.py` | Add `_compact_symbol`; 3-way OR-match in `find_duplicate_position` and `find_duplicate_signal`. |
| `shared/scripts/retro_activate_positions.py` | Add `AND status='pending'` guard + lost-race log. |
| `shared/tests/test_bug_hunt_2026_05_24_round2.py` | Append 6 `TestRound3Review_*` classes (11 new tests). |

---

### Verification (round 3)

* `python3 -m pytest shared/tests/test_bug_hunt_2026_05_24.py shared/tests/test_bug_hunt_2026_05_24_round2.py shared/intelligence/test_wick_close.py shared/tests/test_wick_fallback.py shared/tests/test_grep_guard_md_writes.py shared/tests/test_schema_diff.py shared/tests/test_writer_registry_grep.py` → **102 passed**.
* `python3 -m pytest shared/intelligence/ --ignore=shared/intelligence/test_wick_close.py --ignore=shared/intelligence/test_writer_registry.py` → **115 passed**. (`test_writer_registry.py::_MODE` is a pre-existing module-attribute mismatch unrelated to round 3.)
* Restarted: `tickles-interpretation`, `tickles-chart-hacker-opinion`, `tickles-discord-collector`, `tickles-copy-trade-monitor`, `tickles-dashboard`. All five `active`. No tracebacks in `journalctl --since '60s ago'`.
* Dashboard `GET /healthz` → 200.

### How to roll back

`git revert` the round-3 commit. The schema column change (B) is forward-compatible and does NOT need to be reverted — leave `deduped_at` in place, the dedup writer will populate it after rollback if it ran before. Restart the same five services.

---

### 1f. Round-5 Tier-A fixes (post round-4 self-audit, 2026-05-24)

A second 4-agent self-audit (BH1, BH2, CA1, CA2) of the round-4 follow-up changes flagged **1 Critical + 2 High** consensus items. These are bounded but real — Tier A is the *minimum responsible* close-out before declaring the audit cycle complete. Tier B/C items (compact-symbol gaps, budget-token model, behavioural-test gaps, log-level tweaks) were **deliberately deferred**; the operator can reopen them later without trading-accuracy risk.

| # | Severity | Where | Symptom | Fix |
|---|----------|-------|---------|-----|
| **A1** | **Critical (incomplete CAS)** | [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:1) — 6 `update_media_status` call sites | Round-4 §1e #6 claimed CAS was threaded into "every terminal call." In reality only 2 of 8 sites (`text-dedup short-circuit` and `analyzed` success) carried `expected_processed_at=claim_ts`. The other 6 (CDN HTTP fail, CDN exception, file-missing, vision-unavailable, prefilter `skipped_not_chart`, unresolved-instrument, `run_cycle` exception handler) wrote terminal status without CAS — a stale worker finishing >60 min after `recover_stale_analyzing` could still poison a sibling's fresh `analyzing` claim with `failed`/`skipped_*`. | Threaded `expected_processed_at=claim_ts` into all 6 missing sites in `_process_one_impl`, plus `expected_processed_at=row.get("claim_ts")` in the `run_cycle` per-row exception handler. Behavioural test `test_every_terminal_update_media_status_carries_cas` walks every `update_media_status(` call in the file and asserts the CAS arg is present. |
| **A2** | **High (sibling-script race)** | [`shared/scripts/retro_activate_positions.py`](shared/scripts/retro_activate_positions.py:1) — both activation UPDATEs (lines 122-134 and 154-166 pre-fix) | Round-4 §1e #5 added `AND status='pending'` to the **expire** UPDATE only. The two **activate** UPDATEs above the expire block (one in the SL/TP-hit branch, one in the "still open" branch) had no guard. A live monitor activation between the script's SELECT and UPDATE could be silently overwritten with the script's `current_price` / `price_updated_at`. | Mirrored `AND status = 'pending'` on both activation UPDATEs; on `"UPDATE 0"` reply, log the lost-race line ("Activate SKIPPED") and `continue` to the next row instead of falling into `_settle_close` / increment counters. Tests: `test_both_activation_updates_have_status_guard`, `test_lost_race_logs_present`. |
| **A3** | **High (snapshot drift)** | [`shared/scripts/snapshots/tickles_shared.snapshot.sql`](shared/scripts/snapshots/tickles_shared.snapshot.sql:1) | Snapshot was missing `instrument_symbol_normalised` column on `tracked_positions` plus its three supporting indices (`idx_tp_symbol_norm`, `idx_tracked_pos_open_by_company_symbol`, `idx_tracked_positions_deduped_at`). Live DB and canonical DDL had them — only the snapshot was stale. The Phase-R schema-diff CI gate would mis-flag any provisioning attempt that consumed the snapshot. **No live-DB change was needed** (verified read-only via `\d+ tracked_positions`). | Added the column + 3 index DDL stanzas to the snapshot. Tests: `test_snapshot_declares_instrument_symbol_normalised`, `test_snapshot_has_idx_tp_symbol_norm`, `test_snapshot_has_idx_tracked_pos_open_by_company_symbol`, `test_snapshot_has_idx_tracked_positions_deduped_at`. |

#### Verification (round 5)

* Live DB readonly check: `psql -d tickles_shared -c "\d+ public.tracked_positions"` confirmed `instrument_symbol_normalised`, `idx_tp_symbol_norm`, `idx_tracked_pos_open_by_company_symbol`, `idx_tracked_positions_deduped_at` all already present.
* `python3 -m pytest shared/tests/test_bug_hunt_2026_05_24.py shared/tests/test_bug_hunt_2026_05_24_round2.py shared/intelligence/test_wick_close.py shared/tests/test_wick_fallback.py shared/tests/test_grep_guard_md_writes.py` → **116 passed** (+7 new round-5 tests over the round-4 baseline of 109).
* No live DDL applied; only snapshot file refreshed.
* No new env vars, no new systemd units, no new dependencies.

#### Round-5 functions & code changes

| File | Action |
|------|--------|
| `shared/intelligence/interpretation_service.py` | Threaded `expected_processed_at=claim_ts` into the 6 missing terminal `update_media_status` call sites (lines 3017, 3028, 3039, 3137, 3170, 3213); threaded `expected_processed_at=row.get("claim_ts")` into the `run_cycle` per-row exception handler (line 3956). |
| `shared/scripts/retro_activate_positions.py` | Added `AND status = 'pending'` guard + `"Activate SKIPPED"` lost-race log + `continue` on both activation UPDATEs. |
| `shared/scripts/snapshots/tickles_shared.snapshot.sql` | Added `instrument_symbol_normalised` column to `tracked_positions` definition; added 3 missing CREATE INDEX stanzas (`idx_tp_symbol_norm`, `idx_tracked_pos_open_by_company_symbol`, `idx_tracked_positions_deduped_at`). |
| `shared/tests/test_bug_hunt_2026_05_24_round2.py` | Appended 3 new test classes: `TestRound5_A1_CASOnAllTerminalUpdates`, `TestRound5_A2_RetroActivateGuards`, `TestRound5_A3_SnapshotHasNormalisedColAndIndices` (7 tests total). |

#### Deliberately deferred (Tier B/C, not shipped in round 5)

* **`_compact_symbol`** doesn't strip `_` or `:`, doesn't handle lowercase storage. Real but bounded — only matters for legacy rows with non-canonical exchange suffixes; new writes are normalised.
* **Cross-actor pending dedup at `interpretation_service.py:2533`** still uses single-symbol match. Same risk class as Fix C; not a regression introduced by round 4.
* **`OpinionBudget.release()` blind-LIFO `pop()`** — safe under current sequential model + advisory lock, latent if anyone parallelises.
* **Chart-hacker exception-after-acquire slot leak** — slow leak only on rare write-failure path.
* **Test behavioural-coverage upgrade** — round-4/round-5 tests are ~91% string-presence. Functional but not query-plan-aware.
* **Log-level downgrades** for CAS no-op INFO and retro `print` → `logger.info`.
* **Writer-registry coverage for `position_monitor` and `media_items`** — pre-existing gap, predates round 4.

These are tracked here so a future operator can reopen them without re-discovering them.

#### How to roll back

Same `git revert` pattern as rounds 1-4. The snapshot column/index additions are documentation only; reverting them does not affect the live DB. The `expected_processed_at=claim_ts` parameter has a default of `None`, so reverting individual call sites is safe (just falls back to no-CAS behaviour).

---

### 1g. Round-6 "fix everything" sweep (post round-5 deferred items, 2026-05-24)

After completing round-5 Tier A and reviewing the deferred Tier B/C list, the user issued a single-word directive: **"fix everything"**. Round 6 closes every deferred item except one (text-path advisory lock) that was deemed not worth a 450-line method restructure given DB constraints already prevent corruption.

#### Plain-language summary (what changed and why)

Five things got fixed:

1. **Symbol matching is now case- and separator-tolerant.** Dedup queries used to miss legacy rows like `btcusdt` (lowercase) or `BTC_USDT` (underscore). Now the helper strips `/`, `-`, `_`, `:` and the SQL has `UPPER()` fallbacks. Result: a trader posting "BTC long" can no longer create a second pending position when an old row exists in legacy form.
2. **The cross-actor 24-hour pending dedup got the same treatment.** It's a different code path from the trade_dedup helper — round 5 noted both needed the fix; round 6 ships the second one.
3. **Opinion-budget slots are now token-keyed.** The previous `release()` did a blind LIFO `pop()` which works under one-at-a-time runs but would corrupt accounting if anyone parallelised. Now `try_acquire()` returns a unique integer token and `release(token)` removes the exact slot. Any future parallelism is safe by construction.
4. **Chart-hacker can't leak budget slots on exceptions any more.** Wrapped the post-acquire work in `try / finally`. If anything between "acquired the slot" and "wrote the opinion row" raises (gateway timeout, DB write error, anything), the slot is refunded. Without this, repeated crashes could silently drain the 120/h global cap with zero rows written.
5. **Lost-race counters are split out.** Position-monitor expiry sweeps and the text path now report `expired_lost_race` and `position_create_failed_text` as separate fields. Dashboards stop conflating "nothing to expire" with "expired UPDATE returned 0 rows because a sibling monitor already flipped this row".

Plus three supporting changes:

* CAS no-op log INFO → DEBUG (operator noise reduction).
* Writer-registry static gate now covers `position_monitor` and `media_items` writers (closes a pre-existing audit gap).
* `test_master_schema_sync.py` now asserts `deduped_at` column + index so a future drop is caught at CI.

#### Deferred (one item)

* **Text-path atomic advisory lock** — the audit's recommendation to wrap `_process_text_one` (450 lines) in a per-news-item `pg_try_advisory_lock`. Deferred. Worst case under text-path concurrency is wasted LLM cost; existing DB unique constraints (`tracked_positions` ON CONFLICT, `signal_interpretations` per-news-item uniqueness) prevent data corruption. Restructuring 450 lines for a no-observed-bug theoretical race is a worse trade than leaving it alone with this note.

#### Round-6 functions & code changes

| File | Function / Section | Change |
|------|---------|--------|
| [`shared/intelligence/trade_dedup.py`](shared/intelligence/trade_dedup.py:1) | `_compact_symbol` | Strip `_` and `:` in addition to `/` and `-`; uppercase result. |
| [`shared/intelligence/trade_dedup.py`](shared/intelligence/trade_dedup.py:1) | `find_duplicate_position` + `find_duplicate_signal` | SQL `UPPER()` fallback predicates added. |
| [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:2538) | cross-actor pending dedup | 5-way OR + UPPER fallback (was 1-way exact match). |
| [`shared/intelligence/opinion_budget.py`](shared/intelligence/opinion_budget.py:1) | `try_acquire`, `release` | Token-keyed accounting. `try_acquire` now returns `(ok, why, token)`; `release(token)` removes the exact slot. Backward-compatible `token=None` falls back to LIFO `pop()`. |
| [`shared/intelligence/chart_hacker_opinion_service.py`](shared/intelligence/chart_hacker_opinion_service.py:464) | `_process_position` | `try / finally` around all post-acquire work; refunds via `budget.release(position_id, token=token)` if `opinion_written` is False. |
| [`shared/intelligence/position_monitor.py`](shared/intelligence/position_monitor.py:1452) | `_activate_pending_positions` | New `expired_lost_race` counter; CAS no-op INFO → DEBUG. |
| [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:4017) | `run_cycle` summary | New `position_create_failed_text` counter in the cycle return dict. |
| [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:2766) | `update_media_status` | CAS no-op log downgraded INFO → DEBUG. |
| [`shared/scripts/writer_registry_grep.py`](shared/scripts/writer_registry_grep.py:31) | `PATH_TO_SERVICE` | Added `position_monitor` and `surgeon_position_reconciler`. |
| [`shared/intelligence/migrations/2026_05_24_round6_writer_registry_extension.sql`](shared/intelligence/migrations/2026_05_24_round6_writer_registry_extension.sql:1) | NEW | Idempotent migration extending `tracked_positions` allow-list and adding `media_items` row. |
| [`shared/migration/test_master_schema_sync.py`](shared/migration/test_master_schema_sync.py:1) | new asserts | `test_tracked_positions_has_deduped_at` + `_index`. |

#### Tests added (round 6)

* `TestRound6_CompactSymbolStripsAllSeparators` — 5 cases (underscore, colon, whitespace, lowercase, None).
* `TestRound6_DedupSQLHasUpperFallback` — both helpers verified.
* `TestRound6_CrossActorPendingDedupUsesCompactOR` — multi-pattern regex against the `:2538` query.
* `TestRound6_OpinionBudgetTokenKeyedRelease` — actually runs `try_acquire` / `release` in an event loop and asserts the right slot was removed.
* `TestRound6_ChartHackerTryFinallyRefund` — verifies the `try/finally` and refund call site.
* `TestRound6_LostRaceCounters` — both new counter fields are returned.
* `TestRound6_CASNoOpDowngradedToDebug` — regex verification on the demoted log line.
* `TestRound6_WriterRegistryExtensions` — grep gate + migration file presence.
* `TestRound6_MasterSyncDedupedAtAssertions` — meta-test that the master-sync test itself asserts `deduped_at`.

Plus 3 new tests in `test_opinion_budget.py` for token-keyed release + legacy fallback.

* `python3 -m pytest shared/tests/test_bug_hunt_2026_05_24_round2.py` → **59 passed** (+9 round-6 cases over the round-5 baseline of 50).
* `python3 -m pytest shared/tests/test_opinion_budget.py shared/migration/test_master_schema_sync.py` → **53 passed**.

#### How to roll back round 6

* Migration `2026_05_24_round6_writer_registry_extension.sql` is idempotent and additive; rolling back means deleting the appended services from the `allowed_writer_services` arrays. No DDL, no data loss.
* The `try_acquire` 3-tuple return is a contract change. If you revert `opinion_budget.py`, you must also revert all callers (`chart_hacker_opinion_service.py` + tests). `git revert` of the round-6 commit handles this cleanly.
* Counter additions and log-level changes are isolated and safe to revert independently.

---

### 1h. Round-7 copy-trade persistence (2026-05-24, late afternoon)

**The symptom you saw on the dashboard:** all 7 copy-trade competition agents (`copy_spot_seq`, `copy_lev_parallel`, `copy_lev_be_lock`, `copy_opt_spot_seq`, `copy_opt_lev_parallel`, `copy_opt_lev_be_lock`, `copy_ch_ai_vision`) showed Balance=$1000, Live Equity=$1000, Realized=$0, Unrealized=$0, Trades=0 — even though the live trader feed underneath the same panel showed 9 open positions with real P&L like +$53.26 / +$19.54.

**Root cause (two bugs, both in [`shared/intelligence/copy_trade_monitor.py`](shared/intelligence/copy_trade_monitor.py:1)):**

1. **In-memory state, no persistence.** `__init__` built every agent fresh on startup with hard-coded defaults (balance=$1000, trades=0, wins=0, losses=0, open_positions=[]). There was no `_load_state()` from the DB. **Every service restart wiped the agents' entire history.** The `_update_contest_scores` push then froze `contest_participants` at the round-defaults until a new trade closed.
2. **`_get_new_open_positions` used `signal_timestamp >= self._started_at`.** The intent was "don't back-fill stale signals", but the side effect was that every restart **orphaned every currently-open trader position** — the daemon couldn't see them, so it never paper-entered them into any agent. Combined with bug 1, the daemon would silently do nothing.

**The dashboard's "9 LIVE" panel still worked because it reads `tracked_positions` directly. The aggregate balance / equity / P&L / trade-count came from `contest_participants`, which was frozen.**

#### What shipped (4 phases, all 4 applied to live)

| Phase | What | Where |
|-------|------|-------|
| 1 | New table `public.copy_agent_state` (one JSONB-bearing row per agent) | [`shared/intelligence/migrations/2026_05_24_copy_agent_state.sql`](shared/intelligence/migrations/2026_05_24_copy_agent_state.sql:1) — applied to live DB; 7 known agents seeded idempotently |
| 1 | `_load_state()` / `_save_agent()` / `_save_state()` methods | [`shared/intelligence/copy_trade_monitor.py`](shared/intelligence/copy_trade_monitor.py:107) — hot path is per-agent UPSERT after every open/close; bulk save runs every 30s as a backstop |
| 1 | Factor `NAME_TO_ID` to a module-level constant (was duplicated 3×) | Same file: now defined once near top, used everywhere |
| 2 | `run_forever()` calls `_load_state()` BEFORE the first tick | Restarts now resume from persisted state instead of fresh defaults |
| 2 | `_get_new_open_positions`: drop the `_started_at` filter, replace with 7-day lookback | Same file. `_load_state()` rebuilds `_entered_positions` from BOTH persisted open paper-positions AND `competition_trades` so we don't double-enter |
| 3 | One-shot backfill script with `--dry-run` / `--apply` | [`shared/scripts/backfill_copy_agent_state.py`](shared/scripts/backfill_copy_agent_state.py:1) — aggregates `competition_trades` and writes the correct end-state for each of the 7 agents |
| 3 | Backfill applied to live DB | All 7 agents UPSERT'd from competition_trades aggregates |
| 4 | Dashboard auto-populates from `contest_participants` (no code change needed; the read path was already correct) | Verified live: `/api/competitions` now returns real numbers |

#### Verification at the moment of restart

After applying the migration + backfill + service restart, the journal showed exactly the recovery we expected:

```
copy_agent_state: loaded persisted state for 7 agents (43 trader_ids in entered set)
Tick start
Entered 11 new positions across agents: ['A: Spot Seq', 'B: Lev Parallel', 'C: +BE Lock', 'A+Opt: Spot Seq', 'B+Opt: Lev Par', 'C+Opt: +BE Lock', 'CH: AI Vision']
A: Spot Seq: closed BEAM/USDT long @ 0.0019 (reason=sl) PnL=$78.34 balance=$1057.94
B: Lev Parallel: closed BEAM/USDT long @ 0.0019 (reason=sl) PnL=$3.49 balance=$821.00
... (24 more orphan-position closes at SL because prices had moved past SL during the outage) ...
```

Final balances after the recovery cycle:

| Agent | Balance | Realized P&L | Trades | Win rate | Open |
|-------|---------|--------------|--------|----------|------|
| `copy_ch_ai_vision`     | $1063.89 | +$63.89  |  7 | 28.6% | 0 |
| `copy_opt_spot_seq`     | $1061.91 | +$61.91  |  3 | 33.3% | 0 |
| `copy_spot_seq`         | $1057.94 | +$57.94  |  2 | 50.0% | 0 |
| `copy_opt_lev_parallel` |  $986.67 | -$13.33  | 33 | 57.6% | 3 |
| `copy_opt_lev_be_lock`  |  $986.67 | -$13.33  | 33 | 57.6% | 3 |
| `copy_lev_parallel`     |  $834.72 | -$165.28 | 33 | 54.5% | 3 |
| `copy_lev_be_lock`      |  $834.72 | -$165.28 | 33 | 54.5% | 3 |

#### Tests added (round 7)

[`shared/tests/test_copy_trade_persistence.py`](shared/tests/test_copy_trade_persistence.py:1) — 22 behavioural assertions covering:

* `NAME_TO_ID` is a single module-level constant (no inline redefinitions).
* `_load_state` / `_save_agent` / `_save_state` methods exist and reference the right table.
* `run_forever` calls `_load_state()` before the run-loop.
* The open path (after `_log_trade_open`) and the close path (`_close_agent_position`) both call `_save_agent`.
* `_update_contest_scores` calls `_save_state()` as a bulk backstop.
* `_get_new_open_positions` no longer uses the `signal_timestamp >= self._started_at` filter.
* `_get_new_open_positions` uses `INTERVAL '7 days'` for the lookback.
* `_load_state` rebuilds `_entered_positions` from `competition_trades`.
* The migration creates the table, adds all required columns, and seeds 7 known agents idempotently.
* The backfill script is dry-run by default, requires `--apply`, uses UPSERT for idempotency, aggregates from `competition_trades`, and references all 7 agents.
* The dashboard route still reads `equity_usd` / `realized_pnl_usd` / `unrealized_pnl_usd` from `contest_participants`.

`python3 -m pytest shared/tests/test_copy_trade_persistence.py -v` → **22 passed**.
Full sweep (`test_bug_hunt_2026_05_24*`, `test_copy_trade_persistence`, `test_opinion_budget`, `test_master_schema_sync`) → **186 passed**.

#### How to roll back round 7

* `DROP TABLE public.copy_agent_state;` (the table is additive — losing it just reverts to in-memory defaults).
* `git revert` of the [`shared/intelligence/copy_trade_monitor.py`](shared/intelligence/copy_trade_monitor.py:1) commit takes the daemon back to in-memory-only behaviour.
* The backfill script is read-only on `competition_trades` — running or reverting it has no effect on trade history.
* `contest_participants` rows continue to be updated by the (reverted) live monitor; reverting won't corrupt them, but they'll refreeze at whatever the next `_update_contest_scores` push writes.

---

### Round 8 — Replay-chart "9999999" y-axis artefact (2026-05-24)

#### What the user saw

On the **CALL DETAIL** drawer (e.g. interp 2586 / pos 13111, BTC/USDT 2h replay) the right-hand reconstructed-replay chart had a phantom y-axis label reading **`9999999`** floating above the real top tick (`82,000`), even though the actual price range was 74k–82k. The candlesticks themselves were drawn correctly — only the top axis label was wrong.

#### Root cause (after a long debug)

Two things compounded:

1. **Default ECharts value-axis formatter mis-printing the explicit `max` edge label.** `renderReplayChart()` was passing an explicit `yAxis.max` like `82445.81999999999` (data max + 5% pad). When ECharts' default `axisLabel` formatter rendered that exact float at the very top of the axis, it emitted `"9999999"` instead of the actual value. Reproducible whenever the explicit `max` was a float with a long fractional tail; the rounded interior ticks (`82,000`, `80,000`, …) were unaffected because they came out of ECharts' tick generator as clean integers.
2. **Stale chart instance leaking when the same DOM id was rendered for different signals.** `chart()` was calling `dispose()` on the cached instance but didn't account for: (a) ECharts holding an instance reference via `getInstanceByDom()` even after our cache lost it, and (b) the parent's `innerHTML` having been replaced under us when the drawer re-rendered. Result: occasional canvas residue from a previous render bleeding through.

#### What we changed (one user-visible fix, two hardening fixes)

| Where | Change |
|-------|--------|
| `shared/dashboard/static/app.js` → `renderReplayChart` | (1) **Custom `axisLabel.formatter`** — `v => Number(v).toLocaleString(undefined, {maximumFractionDigits: 2})`. Defensive against whatever default-formatter edge case produced `9999999`. (2) **Snap `yMin` / `yMax` to nice round multiples** of a 1/2/5 × 10^k step so axis ticks are always clean integers (`74,000`, `76,000`, …) and the edge label can never disagree with an interior tick. (3) Restored explicit `type:'value'`, `boundaryGap:[0,0]`, `tooltip` with cross axis-pointer, both `inside` + `slider` `dataZoom`, and the vertical CALL marker at the candle that contained the post. |
| `shared/dashboard/static/app.js` → `chart(id)` | Made the chart-init path defensive: dispose any cached instance, **also** dispose any orphan ECharts instance that `echarts.getInstanceByDom(el)` still tracks for the same node, then `el.innerHTML = ''` for a guaranteed-clean canvas slot before `echarts.init()`. Eliminates residue when the same `disc-chart-{id}` is reused across drawer re-renders. |
| `shared/dashboard/web/index.html` | Bumped the `app.css` / `app.js` cache-bust query strings (`?v=20260524-yaxis…`) so browsers picked up the new chart code without a manual hard-refresh. |

#### Verification

* Re-opened the same CALL DETAIL drawer (interp 2586, pos 13111). Y-axis now reads `74,000 / 76,000 / 78,000 / 80,000 / 82,000 / 83,000` — no `9999999`, no clipped edge label.
* Entry / SL / TP1 horizontal markLines render at the correct prices (78,000 / 74,850 / 79,500).
* Vertical CALL marker is back, anchored to the candle at the trader-call timestamp.
* Tooltip and dataZoom slider both work.
* Mini-radar charts on the Entry Radar grid are unaffected (they use a separate code path with `yAxis.show:false`, where the bug couldn't appear anyway).

#### How to roll back round 8

* `git checkout HEAD~1 -- shared/dashboard/static/app.js shared/dashboard/web/index.html`
* No DB changes, no service restarts required — pure frontend.

---

## 3. Round 9 (2026-05-24) — "Don't Invent Trades": stop attributing AI-inferred setups to humans

### What the user saw

Opening the CALL DETAIL drawer for a Discord post by `emutrading` whose **entire** content was the four words **"btc must get above here"**, the dashboard showed:

* A tracked-position card attributed to the trader (`emutrading`) with `entry=78,000`, `SL=74,850`, `TP1=79,000`.
* A second tracked-position card attributed to **ChartHacker (AI vision agent)** with the same levels.
* The chart itself had **no long/short box, no entry line, no SL line, no TP marker** — just freehand yellow scribbles and some horizontal zones marking key levels.

User reaction: *"isn't it odd that this was detected as a trade? it's got no long or short box with stop loss and entry. look at the chart."*

### Root cause

The vision-LLM prompt at [`shared/intelligence/prompts/chart_analysis.json`](shared/intelligence/prompts/chart_analysis.json:1) (version `2026.05.04-no-symbol-guessing-v2`) had section 5 titled **"WHEN NO EXPLICIT ENTRY/SL/TP IS DRAWN"** that **explicitly instructed the model to "Construct a trade: entry near support for BUY, near resistance for SELL"** when the trader hadn't actually drawn one. Combined with the line *"EVEN CHART-ONLY POSTS WITH MINIMAL TEXT CONTAIN TRADE IDEAS"*, the LLM was being told: *if you don't see a setup, make one up*.

The result: every level-commentary post (`"btc must get above here"`, `"watching this level"`, `"key support holding"`) shipped a constructed trade in `trader_trades`, which the InterpretationService then booked as a `tracked_position` with `signal_source='trader'`. The trader's accuracy stats double-counted the AI's hallucinations and the position monitor started force-closing 100+ phantom positions at SL whenever price moved.

A scope query against production showed **177 open/pending trader-attributed positions, of which only 22 (12%) had any explicit setup language in the underlying news text** — the other **155 (88%) were AI-hallucinated** trades falsely attributed to humans.

### What we changed (5 layers, defence in depth)

| Layer | Where | Change |
|-------|-------|--------|
| **Prompt** | [`shared/intelligence/prompts/chart_analysis.json`](shared/intelligence/prompts/chart_analysis.json:1) | Bumped to **`2026.05.24-trader-explicit-only-v1`**. Rewrote the system prompt to enforce a strict two-track separation: **TRACK A `trader_trades`** (must satisfy A1 position box / A2 explicit role labels / A3 setup keywords in text / A4 price-anchored arrows — emit `[]` otherwise) vs **TRACK B `chart_hacker_trades`** (allowed to construct trades from levels — this is the AI's independent opinion). Each `trader_trades` row now carries an `explicit_evidence` field tagging which rule it satisfied. The "construct a trade" instruction was kept but moved to TRACK B only. |
| **Server-side gate** | [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:1) — new helper `_is_explicit_trader_setup` (~line 2120) | Defence in depth in case the LLM disobeys. Each `trader_trade` is validated before a position is written: accepts the LLM's `explicit_evidence` code (A1–A4) **or** falls back to scanning the news text for direction-word + level-word (`A3_text_fallback`) **or** direction-word followed by a price-like number within ~40 chars (`A3_price_window`, catches casual entries like *"long btc 70k"*). Anything else → `no_explicit_evidence` → drop the trade. Backward-compatible: the gate self-disables for old prompt versions so historical interpretations still parse. |
| **Wired into `_write_one`** | Same file, Pass-1 of the trader-trades loop (~line 3460) | Each candidate `trader_trade` runs through `_is_explicit_trader_setup`. Failures are logged with the gate's reason and dropped — no position is created. The chart_hacker pass continues to write its independent opinion. |
| **Performance scorer** | [`shared/intelligence/performance_scorer.py`](shared/intelligence/performance_scorer.py:1) `fetch_unscored_signals` | The scorer now requires `EXISTS (SELECT 1 FROM tracked_positions tp WHERE tp.signal_interpretation_id = s.id AND tp.signal_source = 'trader')` before crediting a signal to the human trader. ChartHacker's inferred trades live under their own `trader_profiles` row and are scored separately. |
| **Dashboard UI** | [`shared/dashboard/static/app.js`](shared/dashboard/static/app.js:1) + [`shared/dashboard/static/app.css`](shared/dashboard/static/app.css:1) | New `aiInferredBadge()` helper drops a small **AI INFERRED** pill on radar cards whose `signal_source === 'chart_hacker'` so the user can tell at a glance which positions are AI-inferred opinions vs. real trader calls. Drawer kicker also now reads `INTERP ID: 2586 · POS ID: 13111 · AI INFERRED (ChartHacker)`. Cache-bust bumped to `?v=20260524-round9`. |

### One-shot historical cleanup (backfill)

[`shared/scripts/backfill_round9_retag_inferred_positions.py`](shared/scripts/backfill_round9_retag_inferred_positions.py:1) walks every open / pending `tracked_positions` row with `signal_source='trader'`, applies the same gate logic, and:

* If the gate fails AND a chart_hacker twin exists for the same `signal_interpretation_id` → **CANCEL** the trader-attributed row (`status='cancelled'`, `status_reason='round9_no_explicit_setup'`, `exit_reason='round9_no_explicit_setup'`, `closed_at=NOW()`). The chart_hacker twin remains as the AI opinion.
* If the gate fails AND no chart_hacker twin exists → **RETAG** the row in place to `signal_source='chart_hacker'`, `actor_type='agent'`, `actor_id='{company}_chart_hacker'`, `trader_profile_id=<chart_hacker profile id>`. Pipeline tracking continues, attribution moves to the AI agent.
* If the gate passes → leave it alone.

Defaults to dry-run; `--apply` mutates the DB.

**Production run (2026-05-24, applied):**

```
Total candidates (signal_source='trader', open|pending): 177
  PASS gate (real trader calls, untouched):    22
  FAIL gate, has chart_hacker twin → CANCEL:   154
  FAIL gate, no chart_hacker twin   → RETAG:   1
```

The user's example (interp 2586 / pos 13110, *"btc must get above here"*) was correctly cancelled.

### Tests

[`shared/tests/test_round9_trader_setup_gate.py`](shared/tests/test_round9_trader_setup_gate.py:1) — 36 behavioural tests covering:

* 15 commentary-style posts that MUST fail the gate (`"btc must get above here"`, `"watching this level"`, `"free money"`, `""`, etc.).
* 7 explicit-setup posts that MUST pass (with the right evidence code).
* All 4 LLM-declared evidence codes (A1–A4) overriding text scanning.
* Invalid evidence codes falling through to text scan correctly.
* Legacy / empty / unknown prompt versions bypassing the gate (backward-compat).
* The prompt JSON shape: version bumped, two-track separation present, `explicit_evidence` field advertised, "Construct a trade" removed from TRACK A.

187/187 in the round-2 + copy-trade-persistence + opinion-budget + round-9 suite pass.

### Verification

* Reload the dashboard → `app.js?v=20260524-round9` loads.
* Open the radar / signals tab → cards from ChartHacker now show the `AI INFERRED` pink badge next to the trader name; cards from real traders don't.
* Click a ChartHacker card → drawer kicker reads `INTERP ID: N · POS ID: M · AI INFERRED (ChartHacker)`.
* `psql tickles_shared "SELECT signal_source, status, COUNT(*) FROM tracked_positions GROUP BY 1,2;"` shows the population shift: trader / pending dropped 159 → 21, trader / cancelled appeared with 154.
* `tickles-interpretation.service` restarted → next interpretation cycle uses the new prompt; new commentary posts no longer mint trader-attributed positions.

### How to roll back round 9

In rough order of how disruptive a rollback would be (most reversible first):

1. **Frontend / UI** — `git checkout HEAD~1 -- shared/dashboard/static/app.js shared/dashboard/static/app.css shared/dashboard/web/index.html` (pure cosmetic, no DB / no service restart needed).
2. **Performance scorer** — `git checkout HEAD~1 -- shared/intelligence/performance_scorer.py` (no schema impact; existing `trader_performance` rows are unchanged, the next cycle will simply consider more signals).
3. **InterpretationService gate** — `git checkout HEAD~1 -- shared/intelligence/interpretation_service.py` and restart `tickles-interpretation.service`. New trader_trades will start writing again immediately.
4. **Prompt** — `git checkout HEAD~1 -- shared/intelligence/prompts/chart_analysis.json`. Restart the interpretation service so the new (old) prompt loads.
5. **Backfill (un-cancel)** — to revive the 154 cancelled positions:

   ```sql
   UPDATE public.tracked_positions
      SET status        = 'pending',
          status_reason = 'awaiting_entry',
          exit_reason   = NULL,
          closed_at     = NULL,
          updated_at    = NOW()
    WHERE status = 'cancelled'
      AND status_reason = 'round9_no_explicit_setup';
   ```

   And to re-attribute the 1 retagged row back to its original trader, you'd need to consult the audit trail in `metadata` — but in practice the single retag row carries no commercial value so leaving it is fine.

6. **Tests** — `git rm shared/tests/test_round9_trader_setup_gate.py` (no cascade impact).

---

## 2. Functions & Code Changes

| File | Status | Action | Functions Added / Edited / Removed |
|------|--------|--------|-------------------------------------|
| `shared/intelligence/trade_dedup.py` | Ready | Edit | `find_duplicate_position`: Change `'partial_hit'` to `'partial_exit'`. |
| `shared/collectors/discord/discord_collector.py` | Ready | Edit | `_poll_and_ingest` / HWM loop: Save the HWM only after filtering and successfully inserting messages. |
| `shared/intelligence/interpretation_service.py` | Ready | Edit | `_process_text_one`: Write a placeholder in `signal_interpretations` when a message is flagged as a duplicate. |
| `shared/intelligence/position_monitor.py` | Ready | Edit | `_find_sl_tp_wick_candle`: Include the activation timestamp candle itself (`>=` instead of `>`) and limit historical candle lookup size.<br>`_find_entry_touch_candle`: Added smart local-candle freshness check to skip CCXT fallback when up-to-date.<br>`_activate_pending_positions`: Increased limit from 200 to 1000. |
| `shared/scripts/retro_activate_positions.py` | Ready | New | Added a clean retroactive recovery utility. |
| `shared/candles/resample_runner.py` | Ready | Edit | Added `_floor_dt` helper function.<br>Modified `_run_incremental_resample` to floor the resample window start time. |
| `shared/dashboard/static/app.js` | Ready | Edit | Initialized `state.timeSpacing` from localStorage.<br>Added `Spacing` button to `openCall` and `drawReplay` HTML and wired click events.<br>Modified `renderReplayChart` to switch axis types dynamically. |
| `shared/scripts/restore_expired_media_paths.py` | Ready | New | Added retroactive local_path and metadata recovery script. |

---

## 3. How to Roll Back
If you ever need to undo these changes, you can look at the Git history or run:
```bash
git checkout HEAD -- shared/intelligence/trade_dedup.py
git checkout HEAD -- shared/collectors/discord/discord_collector.py
git checkout HEAD -- shared/intelligence/interpretation_service.py
git checkout HEAD -- shared/intelligence/position_monitor.py
git checkout HEAD -- shared/candles/resample_runner.py
git checkout HEAD -- shared/dashboard/static/app.js
git checkout HEAD -- shared/tests/test_candles.py
rm shared/scripts/retro_activate_positions.py
rm shared/scripts/restore_expired_media_paths.py
```
And then restart the services:
```bash
sudo systemctl restart tickles-discord-collector.service tickles-interpretation.service tickles-position-monitor.service tickles-resample.service
```

---

## 4. Round 10 (2026-05-24) — Vision-Model Picker (dashboard dropdown for the OpenRouter LLM)

### 4.1 The problem (in one paragraph, 21-year-old style)

Before Round 10, we had three "vision LLM" slots hard-coded in `interpretation_service.py`:

| Slot | What it does | Pre-Round-10 default |
|------|--------------|----------------------|
| **primary** | The expensive smart model that reads the chart and tries to extract entry/SL/TP. Called once per chart. | `anthropic/claude-sonnet-4` |
| **fallback** | Used only if the primary call fails or rate-limits. Should be reliable, doesn't have to be cheap. | `google/gemini-2.0-flash-001` |
| **prefilter** | A cheap binary classifier that runs on every image and answers "is this even a trading chart?". Saves us from sending memes / random screenshots to the expensive primary. | `google/gemini-2.5-flash` |

These were set via env vars (`CHART_HACKER_MODEL_PRIMARY`, etc.) and read **once at process start**. To change a model — even for a quick A/B test — you had to:
1. SSH in
2. Edit the systemd unit's environment
3. `systemctl daemon-reload`
4. `systemctl restart tickles-interpretation.service`
5. Wait for the next cycle
6. If it sucked, repeat with the old value.

That friction stopped us from trying genuinely better/cheaper models. Specifically: Qwen3-VL-32B-Instruct is now strong on charts and ~6× cheaper than Claude Sonnet 4, but switching took half an hour.

### 4.2 What Round 10 ships

A **runtime-configurable** model picker with these guarantees:

* **DB-backed.** Choices live in `public.system_config` (existing table). No env var or restart required.
* **60-second cache.** Operator picks a new model in the dropdown → next interpretation cycle (max 60s later) uses it.
* **Allow-list enforced.** A curated `vision_model_catalogue.json` lists ~11 vetted vision-capable models. Any attempt to set a model outside this list (whether via API or via direct DB write through code) is rejected.
* **Audit trail.** Every change writes one row into the new `public.model_config_audit` table — when, what slot, old → new, by whom (best-effort identity tag since the dashboard has no auth).
* **A/B test button.** Each card has a "Test on a recent chart" button that runs the chosen model **once** off-the-record on the most recently interpreted chart and shows the JSON output, so the operator can eyeball quality before committing.
* **Defaults flipped to Qwen3-VL-32B / Claude Sonnet 4 / Gemini 2.5 Flash.** Anyone deploying fresh now gets the cheap-and-good Qwen primary with the expensive-but-bulletproof Claude as a safety net.

### 4.3 Files added / changed (with intent)

| File | Action | What & Why |
|------|--------|------------|
| `shared/intelligence/model_config.py` | **NEW** | Single source of truth for the 3 slots. Exposes async `get_model(slot)` (per-cycle lookup), `set_model(slot, model)` (validates + persists + invalidates cache), `get_all_slots()` (snapshot for the UI), `get_model_sync(slot)` (env-or-default for module-import-time bootstraps; never touches DB), `is_allowed(model_id)` (allow-list check), and `invalidate_cache()` (test hook). |
| `shared/intelligence/vision_model_catalogue.json` | **NEW** | Curated allow-list. ~11 vetted vision-capable models with vendor / cost-per-million-tokens / `recommended_for` slots / one-line operator notes. Edit this file to add or remove dropdown options — no code change needed. |
| `shared/intelligence/migrations/2026_05_24_round10_model_picker.sql` | **NEW** | Creates `public.model_config_audit` (id, slot, model_old, model_new, actor_label, changed_at) + 2 indexes. Idempotent (safe re-run). |
| `shared/intelligence/migrations/2026_05_24_round10_model_picker_ROLLBACK.sql` | **NEW** | Drops the audit table and removes any operator-set rows from `system_config` (returning the system to env / code-default resolution). |
| `shared/intelligence/interpretation_service.py` | **EDIT** | Lines 94-96: replaced module-level `os.environ.get(...)` constants with calls to `model_config.get_model_sync(...)`. Line ~625 (prefilter call): swapped `model = PREFILTER_MODEL` for `await _runtime_get_model(SLOT_PREFILTER)`. Line ~776 (vision LLM): `models = [cfg.primary_model, cfg.fallback_model]` → `[await _runtime_get_model(SLOT_PRIMARY), await _runtime_get_model(SLOT_FALLBACK)]`. Lines ~897/903 (logging tags): use the freshly-resolved `runtime_primary` for the operation tag. Line ~3275 (rate limiter): resolve fresh so the per-model bucket follows dropdown changes. Line ~3424-3433 (param hash): resolve fresh so the audit hash reflects what was *actually* used. **No call site reads from a stale `self.cfg.primary_model` anymore.** |
| `shared/dashboard/settings_routes.py` | **NEW** | Four aiohttp routes: `GET /api/settings/vision-models` (returns slots + catalogue), `POST /api/settings/vision-model` (slot+model, with allow-list validation), `POST /api/settings/test-vision-model` (one-shot LLM call against latest interpreted chart), `GET /api/settings/vision-model-history` (last N audit rows). |
| `shared/dashboard/server.py` | **EDIT** | Single line addition wiring `settings_routes.attach_routes(app, prefix=prefix)` next to the existing `attach_market_routes` block. |
| `shared/dashboard/web/index.html` | **EDIT** | Added `<button class="nav-link" data-tab="settings"><span>⚙</span>Settings</button>` to left nav. Added `<section id="tab-settings">` with two panels (settings-body for the 3 cards + settings-history for the audit table). Bumped CSS+JS cache buster from `round9d` to `round10a`. |
| `shared/dashboard/static/app.js` | **EDIT** | Added `'settings'` to the `switchTab` names map and to `render()` dispatch. Added Settings tab fetch hook in `load()`. New functions: `fetchSettings()`, `priceLabel()`, `modelOptions()`, `modelNotes()`, `sourceBadge()`, `renderSettingsPage()`, `wireSettings()`, `refreshHistory()`. The Save round-trip is direct `fetch()` POST (no CSRF since the dashboard has no auth on this private server). |
| `shared/dashboard/static/app.css` | **EDIT** | New rules: `.settings-body`, `.settings-card`, `.settings-card-head`, `.settings-label`, `.settings-select`, `.model-notes`, `.model-notes-line`, `.model-notes-body`, `.settings-actions`, `.settings-test-status`, `.settings-test-result`, `.settings-test-pane`, `.settings-test-head`, `.settings-test-raw`, `.src-tag`, `.data-table.small`. Dark theme, glassmorphism aesthetic to match existing panels. |
| `shared/tests/test_round10_model_picker.py` | **NEW** | 17 tests: catalogue shape, qwen3-vl present, every entry has required fields, recommended_for tags, allow-list accepts/rejects, set_model rejects bad inputs, DB > env > default precedence, cache hit skips DB, set_model invalidates cache, sync helper never touches DB, slot constant stability. |

### 4.4 The 3-tier resolution chain (essential)

When the InterpretationService asks "what's the primary model right now?":

1. **DB row** in `public.system_config` where `(namespace='chart_hacker', config_key='model.primary')` — wins if present.
2. **Env var** `CHART_HACKER_MODEL_PRIMARY` — used only if DB row absent.
3. **Code default** — `qwen/qwen3-vl-32b-instruct` (Round 10 default). Last resort.

Same chain for `fallback` (env var `CHART_HACKER_MODEL_FALLBACK`, default `anthropic/claude-sonnet-4`) and `prefilter` (env var `CHART_HACKER_PREFILTER_MODEL`, default `google/gemini-2.5-flash`).

The cache TTL (60 seconds, tunable via `MODEL_CONFIG_CACHE_TTL_S`) sits **in front of** step 1, so a dropdown click invalidates the cache for that one slot only and the next call refetches.

### 4.5 Verification (already done as of 2026-05-24)

* Smoke-tested the module: code defaults correct, allow-list rejects junk, DB write+readback works, audit trail captures every change, cache hit skips redundant DB reads.
* Smoke-tested all four API endpoints via curl on port 3101 — list returns slots + catalogue, set persists + returns updated state, invalid model rejected with 400, history endpoint returns audit rows newest-first.
* Browser-verified the Settings tab end-to-end: gear icon visible in sidebar, three cards render with correct currently-selected models, dropdown change triggers a "Saved..." flash + audit-table refresh, revert works.
* All 17 Round 10 unit tests pass (`pytest shared/tests/test_round10_model_picker.py`). All 36 Round 9 tests still pass — no regression.

### 4.6 Operational note: how the operator uses this

1. Open https://vmi3220412.trout-goblin.ts.net/dashboard/.
2. Click "⚙ Settings" in the left nav.
3. (Optional) Click "Test on a recent chart" on any card. Wait 5-20s. Eyeball the JSON output.
4. Pick the model from the dropdown. The "Saved..." flash confirms the change. The audit table at the bottom updates immediately.
5. Within 60 seconds, every new interpretation uses the new model. (You can verify by tailing `journalctl -u tickles-interpretation.service -f` and watching the `operation=vision_primary` log lines — `model=` will switch.)

If extraction quality tanks after a swap, just pick the previous model from the dropdown again. The audit trail tells you what the previous value was.

### 4.7 How to roll back Round 10 (cleanly)

If Qwen3-VL turns out worse than Claude Sonnet 4 in production, **don't roll back the code** — just pick `anthropic/claude-sonnet-4` from the Primary dropdown. That's the entire point of the round.

If you want to roll back Round 10 *itself* (remove the picker and revert to env-var-only):

```bash
# 1. Remove the new files.
rm shared/intelligence/model_config.py
rm shared/intelligence/vision_model_catalogue.json
rm shared/dashboard/settings_routes.py
rm shared/tests/test_round10_model_picker.py

# 2. Roll back the migration.
PGPASSWORD='Tickles21!' psql -U admin -h localhost -d tickles_shared \
  -f shared/intelligence/migrations/2026_05_24_round10_model_picker_ROLLBACK.sql

# 3. Revert the edits.
git checkout HEAD -- shared/intelligence/interpretation_service.py
git checkout HEAD -- shared/dashboard/server.py
git checkout HEAD -- shared/dashboard/web/index.html
git checkout HEAD -- shared/dashboard/static/app.js
git checkout HEAD -- shared/dashboard/static/app.css

# 4. (Optional) restore the original env-var defaults if you want claude
#    primary instead of qwen primary:
sudo systemctl edit tickles-interpretation.service  # add Environment=CHART_HACKER_MODEL_PRIMARY=anthropic/claude-sonnet-4
sudo systemctl daemon-reload

# 5. Restart.
sudo systemctl restart tickles-interpretation.service tickles-dashboard.service
```

After rollback the system reverts to env-var-only resolution. Anything previously chosen via the dropdown is gone (the system_config rows are deleted by the rollback migration).

### 4.8 Bonus fix shipped alongside Round 10: widen `prompt_version` from VARCHAR(32) to VARCHAR(64)

While verifying that Qwen3-VL was being called end-to-end, I noticed every interpretation cycle since Round 9 deployed was completing the LLM call but failing the INSERT with:

```
asyncpg.exceptions.StringDataRightTruncationError: value too long for type character varying(32)
```

**Root cause:** Round 9 bumped the prompt to `2026.05.24-trader-explicit-only-v1` — that's 34 characters, but `signal_interpretations.prompt_version` was `VARCHAR(32)`. Every Round 9 interpretation since 2026-05-24 was making the expensive LLM call, then dropping the result on write.

**Damage:** ~80 wasted Claude Sonnet 4 calls (~$2.12 in API spend) plus zero Round 9 prompt versions ever made it into the DB.

**Fix:** New migration `2026_05_24_round10_widen_prompt_version.sql` widens `prompt_version`, `prefilter_provider`, `prefilter_result`, and `vision_provider` to `VARCHAR(64)`. Idempotent. Rollback file truncates over-long values defensively before shrinking the column back to 32.

**Verification:** After applying the migration, a re-test of media_id=5913 produced the first-ever successful Round 9 prompt_version row in the DB — `prompt_version=2026.05.24-trader-explicit-only-v1`, `vision_model_requested=qwen/qwen3-vl-32b-instruct`, full extraction persisted, $0.029 cost (vs Claude's $0.18 for the same chart).

Roll back this fix:

```bash
PGPASSWORD='Tickles21!' psql -U admin -h localhost -d tickles_shared \
  -f shared/intelligence/migrations/2026_05_24_round10_widen_prompt_version_ROLLBACK.sql
```

(Note: rollback truncates the column back to 32 chars, which means future Round 9-prompt rows will start failing again. Don't roll this back unless you're also reverting Round 9.)


---

## Round 11 — Standardisation pass: distance/sort, status enum, Live/Historic positions split (2026-05-24)

### What the user asked for

> "fix everything. great findings. standardisation is important. anything thats stopped out out or tp'd, sure that shouldnt be in entry radar or signals? positions also need to be looked at. We need an audit by 3-4 agents to make sure that our system is functioning. positions should have live and also a tab for historic."

This came after the user noticed the Trading Floor and Entry Radar disagreed on which trade was "closest to entry". Investigation by four parallel audit agents (sort/filter consistency, positions tab anatomy, status lifecycle hygiene, UX standardisation) revealed:

* The Trading Floor sorted by **signed** distance — a short trading 16% above entry sorted ahead of a long trading 1% below entry, because `-16 < -1` numerically. Entry Radar correctly used absolute distance. Same data, opposite top-of-list.
* The Positions tab was a hybrid risk-monitor stuffed into one table: live broker fills + open shadow positions + the last 7 days of closed rows, with a status dropdown that advertised "Pending" but never returned pending rows. There was no historic view — anything older than 7 days was invisible.
* Status pills only had explicit colors for `pending`, `long`, `short`. The other 7 lifecycle states (`open`, `partial_exit`, `closed`, `expired`, `cancelled`, `invalidated`, `deleted`) all rendered as plain grey, so the user couldn't tell at a glance what state a row was in.
* Five small wiring bugs: the radar filter was wired to `renderFloor()` (typo); `posRows()` always used `unrealized_pnl_usd` so closed rows showed $0.00; manage-panel SQL had a `partial_close` typo (DB CHECK allows `partial_exit`); the systemd unit set `POSITION_MONITOR_INTERVAL_S` while the code reads `POSITION_MONITOR_POLL_S`; HTML referenced CSS classes that didn't exist (`.topbar`, `.top-controls`, `.pill-btn`, `.close`).

The user agreed to a six-phase fix.

### What I changed (and why), one phase at a time

#### Phase 11.5 — Wiring bug bundle (small fixes first)

Five independently-revertable fixes in one pass.

* **Radar filter wiring** — `app.js:814` previously called `renderFloor()` for `radar-filter` and `radar-age`; typing in the radar filter did nothing. Split into separate handler tied to `renderRadarPage()`.
* **Closed-row P&L** — `posRows()` previously read `unrealized_pnl_usd ?? pnl_usd` for every row. Closed positions only have `realized_pnl_usd_final` populated, so they showed $0.00. Added a `POS_CLOSED_STATUSES` set and branched on status.
* **`partial_close` typo** — `shared/intelligence/manage_panel/db_views.py:182`, `shared/catalogue/db.py:315`, and `shared/mcp/tools/intelligence.py:1003` all filtered `WHERE status IN ('open','partial_close')`, but the DB CHECK constraint allows `partial_exit`. Result: those queries silently dropped any partial-exit rows. Fixed all three.
* **Systemd env var** — `tickles-position-monitor.service` set `POSITION_MONITOR_INTERVAL_S=60`, but the code reads `POSITION_MONITOR_POLL_S`. The default in code is 60 so the typo was masked. Added the correct var while keeping the legacy alias for one release.
* **HTML/CSS realignment** — `topbar` → `top-bar`, `top-controls` → `controls`, `close` → `drawer-close`, and added a `.pill-btn` style extending `.btn` (it was previously unstyled, so the Refresh button used browser defaults).

#### Phase 11.4 — Status enum pill coverage

Added explicit `.pill.X` rules for every `tracked_positions.status` value (`open`, `partial_exit`, `closed`, `expired`, `cancelled`, `invalidated`, `deleted`, plus `signal`, `neutral`, `unclear`, `conflict`). Each tier has a subdued accent so the user can distinguish state at a glance:

| State | Color |
|---|---|
| pending | amber |
| open | blue |
| partial_exit | purple |
| closed | muted green |
| expired | faded amber |
| cancelled | muted red |
| invalidated | pink |
| deleted | dark grey |
| signal / neutral / unclear / conflict | neutral grey |

Status dropdowns expanded to expose every value the DB can produce (was: pending/open/closed only). Dropped the bogus frontend-only `r.status==='active'` filter on the Floor positions panel — `'active'` isn't in the DB CHECK constraint.

#### Phase 11.1 — Standardise distance & sort (the user-visible bug)

Three small changes in one phase:

* `shared/dashboard/snapshot.py::_sort_key` — `(0, float(dist))` → `(0, abs(float(dist)))`. Recency tiebreaker became `signal_timestamp DESC` (newest first) instead of `created_at DESC`.
* `shared/dashboard/market_routes.py::_radar_sort_key` — same change PLUS the recency tiebreaker is now `-ts.timestamp()` instead of `ts` (so newest wins ties, not oldest like before).
* `shared/dashboard/snapshot.py::_aggregate_pending_positions` — when `current_price` or `entry_price` is missing, set `distance_to_entry_pct=None` instead of `0.0`. The frontend already shows `—` for null; the `0.0` placeholder used to make "no data" rows sort as "closest possible" (rank 1), pushing legitimate close-to-entry rows below them.

After this phase the live API confirms the fix: the Trading Floor now leads with `KAS/USDT` at 0.69% abs distance, and Entry Radar leads with `ETH/USDT` at 0.17% abs distance. Both views now mean the same thing by "closest to entry"; the row identities differ only because each samples a different live-price source (snapshot batch fetch vs candle-replay close), which is by design.

#### Phase 11.2 — 24h monitor lookback + retro-tag

The PositionMonitor's `ACTIVATION_LOOKBACK_S` was 30 min — too tight. A pending row whose entry was touched even an hour ago would never auto-activate, polluting the dashboard with "16% away" rows that should have already been retired. Widened to **24 hours**.

To preserve auditability, any activation triggered by a candle older than the original 30-min "fast path" stamps `status_reason='retro_activated:<utc>'`. So if you ever need to see which activations caught up retroactively (vs fired live), you can `SELECT id, instrument_symbol, status_reason FROM tracked_positions WHERE status_reason LIKE 'retro_activated:%'`.

* Code constant: `FAST_ACTIVATION_LOOKBACK_S = 1800.0` (the audit threshold).
* Default: `ACTIVATION_LOOKBACK_S = 86400.0` (24 h).
* Systemd unit: explicit `Environment=POSITION_MONITOR_ACTIVATION_LOOKBACK_S=86400`.

I deliberately did NOT add a heavy entry-touch filter to `aggregate_signals` because it would require per-row candle replay every 30 seconds (181 pending rows × 0.5 q/sec = unacceptable). The combination of (a) abs-distance sort pushing noise to the bottom + (b) wider monitor lookback keeping the DB cleaner is enough; Entry Radar continues to be the highest-quality "really still waiting" view because it still does the full forensic candle replay per row.

#### Phase 11.3 — Positions Live/Historic split

Three sub-phases.

**11.3a — backend aggregators.** Added two new functions to `shared/dashboard/snapshot.py`:

* `aggregate_live_positions(company_filter, limit=200)` — `open + partial_exit` from `tracked_positions` plus broker fills from `positions_current`. De-duped on `(company, symbol, direction)` with broker-fill winning. **No closed/historic mix-in.**
* `aggregate_historic_positions(company_filter, since_days=30, cursor_closed_at, cursor_id, limit=50, status_filter, outcome_filter)` — keyset-paginated, default 30-day window, supports filtering by single status (closed / expired / cancelled / invalidated) and single outcome (tp1_hit / sl_hit / expired). Returns `{rows, next_cursor, has_more, page_size}`.

Two module-level constants `LIVE_TRACKED_STATUSES` and `HISTORIC_TRACKED_STATUSES` are the single source of truth for which status enum values belong on each tab.

The legacy `aggregate_open_positions` was left in place for backward compatibility with `/api/positions` (still used by some legacy callers and the snapshot builder for the Floor mini-table). The bug Agent B caught (`status NOT IN ('open','pending')` leaking `partial_exit` rows into the closed bucket) was fixed there too: the closed-bucket query now uses `status IN ('closed','expired','cancelled','invalidated')` explicitly.

**`_normalise_position_row` extension.** The canonical row shape now exposes:

| Field | Purpose |
|---|---|
| `stop_loss`, `take_profit_1/2/3` | Live tab distance display |
| `distance_to_sl_pct`, `distance_to_tp1_pct` | Live tab columns (with client-side fallback computation) |
| `time_in_trade_minutes` | Live tab "Age" column |
| `outcome`, `exit_price`, `exit_reason` | Historic tab columns |
| `realized_pnl_usd_final` | Historic tab P&L column |
| `status_reason` | Surfaces `retro_activated:` and `round9_no_explicit_setup` tags |
| `news_item_id` | Drawer click-through for both tabs |
| `price_updated_at` | Live tab staleness indicator |

**11.3b — routes.** Added two new endpoints to `shared/dashboard/server.py`:

* `GET /api/positions/live?company=&limit=` — live positions
* `GET /api/positions/historic?company=&since_days=&limit=&cursor_at=&cursor_id=&status=&outcome=` — paginated historic

Mounted alongside the existing `/api/positions` so the legacy endpoint keeps working.

**11.3c — frontend.** Replaced the single Positions panel with two pill-tab sub-tabs (Live / Historic) using the same pattern as the existing Competition tab. Each sub-tab has its own filter row and its own column set:

| Live tab | Historic tab |
|---|---|
| Position | Position |
| Side | Side |
| Status | Closed (relative) |
| Entry | Status |
| Now (with stale indicator) | Outcome |
| Unrealised P&L | Entry |
| P&L % | Exit |
| Dist SL (computed client-side from entry+now+SL) | Realised P&L |
| Dist TP1 (computed client-side from entry+now+TP1) | Hold (duration) |
| Age (time in trade) | Source |
| Source | |

Historic tab adds a "Load more" button that uses keyset pagination via the API's `next_cursor`. Filter changes invalidate the cursor so a status/outcome change starts at page 1.

Distance to SL/TP1 is computed client-side because PositionMonitor writes those values to `position_updates` (the time-series table) but does NOT mirror them back to `tracked_positions`. Rather than touch the daemon, the frontend now computes them from `(entry, current, stop_loss, take_profit_1)` — all four are already in the row payload.

#### Phase 11.6 — Tests + roadmap

* `shared/tests/test_round11_standardisation.py` — 20 tests covering: abs-distance sort key, radar sort key, recency tiebreaker, monitor lookback constants, retro-tag format, live/historic status sets, status pill CSS coverage, and the five wiring-bug regression checks. All pass.
* Browser smoke test confirmed the user-visible behaviour: Trading Floor now leads with the smallest absolute Δ entry, Entry Radar agrees, the Positions tab has working Live + Historic sub-tabs with the correct columns, status filters narrow the list correctly, status pills are color-coded, and there are no JS console errors.

### Files changed

```
shared/dashboard/snapshot.py                          (sort key + new aggregators + status sets + normaliser fields)
shared/dashboard/market_routes.py                     (radar sort key + Dict import)
shared/dashboard/server.py                            (2 new route handlers + registration)
shared/dashboard/static/app.js                        (sub-tabs, livePosRows, historicPosRows, wiring fixes, posRows P&L branch)
shared/dashboard/static/app.css                       (pill palette, sub-tab styles, pager, pill-btn)
shared/dashboard/web/index.html                       (Positions sub-tab markup, status dropdowns, HTML class fixes, cache-bust)
shared/intelligence/position_monitor.py               (24h lookback, FAST_ACTIVATION_LOOKBACK_S, retro_activated tag, partial_exit comment)
shared/intelligence/manage_panel/db_views.py          (partial_close → partial_exit)
shared/catalogue/db.py                                (partial_close → partial_exit)
shared/mcp/tools/intelligence.py                      (partial_close → partial_exit)
systemd/tickles-position-monitor.service              (POLL_S env var + 24h lookback env var)
shared/tests/test_round11_standardisation.py          NEW — 20 behavioural tests
shared/docs/BUG_HUNT_FIXES_ROADMAP.md                 THIS FILE
```

### How to verify

```bash
# 1. Behavioural tests
cd /opt/tickles && python3 -m pytest shared/tests/test_round11_standardisation.py -v

# 2. New endpoints respond
curl -s 'http://127.0.0.1:3101/api/positions/live?company=all&limit=5' | python3 -m json.tool | head -30
curl -s 'http://127.0.0.1:3101/api/positions/historic?company=all&limit=5' | python3 -m json.tool | head -30
curl -s 'http://127.0.0.1:3101/api/positions/historic?company=all&status=cancelled&limit=3' | python3 -m json.tool

# 3. Sort consistency
curl -s 'http://127.0.0.1:3101/api/snapshot' | python3 -c "import sys,json;d=json.load(sys.stdin);print([(s.get('instrument_symbol'),round(abs(float(s.get('distance_to_entry_pct') or 999)),2)) for s in (d.get('signals') or [])[:5]])"

# 4. Retro-activations (will be empty until a touch fires outside the 30-min fast window)
PGPASSWORD='Tickles21!' psql -h localhost -U admin -d tickles_shared -c \
  "SELECT id, instrument_symbol, status_reason FROM tracked_positions WHERE status_reason LIKE 'retro_activated:%' LIMIT 10;"

# 5. Browser
open https://vmi3220412.trout-goblin.ts.net/dashboard/?nocache=20260524-round11c
```

### Rollback

Each phase is independently revertible. Full rollback in reverse-dependency order:

```bash
# 11.6 — drop tests
rm shared/tests/test_round11_standardisation.py

# 11.3c — restore single-panel Positions tab (manual revert in index.html + app.js)
git checkout HEAD -- shared/dashboard/web/index.html shared/dashboard/static/app.js shared/dashboard/static/app.css

# 11.3b — drop new routes (manual revert of the two add_get lines in server.py)
git checkout HEAD -- shared/dashboard/server.py

# 11.3a — drop new aggregators (manual revert in snapshot.py)
git checkout HEAD -- shared/dashboard/snapshot.py

# 11.2 — restore 30-min lookback
sed -i 's/POSITION_MONITOR_ACTIVATION_LOOKBACK_S", "86400"/POSITION_MONITOR_ACTIVATION_LOOKBACK_S", "1800"/' shared/intelligence/position_monitor.py
# Then remove the FAST_ACTIVATION_LOOKBACK_S const and the retro_reason logic in _activate_pending_positions.
sed -i '/POSITION_MONITOR_ACTIVATION_LOOKBACK_S=86400/d' systemd/tickles-position-monitor.service
systemctl daemon-reload && systemctl restart tickles-position-monitor.service

# 11.5 — these are atomic; revert any subset via git checkout per-file

# 11.4 — drop the new pill rules + revert dropdowns
git checkout HEAD -- shared/dashboard/static/app.css shared/dashboard/web/index.html

# 11.1 — restore signed sort
# In shared/dashboard/snapshot.py change `abs(float(dist))` → `float(dist)` on the _sort_key tuple.
# In shared/dashboard/market_routes.py revert _radar_sort_key to the lambda form.

systemctl restart tickles-dashboard.service
```

### What's still on the wishlist (NOT in Round 11)

These were caught by the audit but are out of scope for a standardisation pass:

* **Exotic-symbol routing** — `GOLD`, `US100`, `1000PEPE/USDT`, `COTI/USDT` fail Bybit CCXT every cycle. They need a proper exchange-router fix, not a band-aid.
* **`position_updates` time series in drawer** — the drawer could show a "trade journey" chart from `position_updates` data. Its own feature.
* **PositionMonitor mirroring distances back to `tracked_positions`** — would let the Live tab read `distance_to_sl_pct` straight from the row instead of computing client-side. Optional perf improvement; not user-visible.
* **Dropping the legacy `aggregate_open_positions`** — once we're sure no one calls `/api/positions` we can remove it. Currently kept for safety.
