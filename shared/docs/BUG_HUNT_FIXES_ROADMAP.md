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

---

## 2. Functions & Code Changes

| File | Status | Action | Functions Added / Edited / Removed |
|------|--------|--------|-------------------------------------|
| `shared/intelligence/trade_dedup.py` | Ready | Edit | `find_duplicate_position`: Change `'partial_hit'` to `'partial_exit'`. |
| `shared/collectors/discord/discord_collector.py` | Ready | Edit | `_poll_and_ingest` / HWM loop: Save the HWM only after filtering and successfully inserting messages. |
| `shared/intelligence/interpretation_service.py` | Ready | Edit | `_process_text_one`: Write a placeholder in `signal_interpretations` when a message is flagged as a duplicate. |
| `shared/intelligence/position_monitor.py` | Ready | Edit | `_find_sl_tp_wick_candle`: Include the activation timestamp candle itself (`>=` instead of `>`) and limit historical candle lookup size. |

---

## 3. How to Roll Back
If you ever need to undo these changes, you can look at the Git history or run:
```bash
git checkout HEAD -- shared/intelligence/trade_dedup.py
git checkout HEAD -- shared/collectors/discord/discord_collector.py
git checkout HEAD -- shared/intelligence/interpretation_service.py
git checkout HEAD -- shared/intelligence/position_monitor.py
```
And then restart the services:
```bash
sudo systemctl restart tickles-discord-collector.service tickles-interpretation.service
```
