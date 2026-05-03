# Bug Hunter Report — Intelligence Pipeline
## Generated: 2026-04-27T21:54Z

---

## CRITICAL (Fix Immediately)

### BUG 1: find_duplicate_signal queries non-existent columns
**File:** `shared/intelligence/trade_dedup.py`  
**Lines:** 168-170  
**Severity:** Critical  
**What happens:** The function queries `si.entry_price`, `si.stop_loss`, `si.take_profit_1` from `signal_interpretations`, but the table schema (verified from `write_signal_interpretation` INSERT at lines 722-740) stores these inside `llm_levels` JSONB. The query crashes with `column si.entry_price does not exist` every time it's called.  
**Root cause:** Schema mismatch — signal_interpretations was designed with JSONB levels, not flat columns.  
**Fix:** Query `llm_levels->>'entry'` etc. from JSONB, or add flat columns to the schema.

### BUG 2: Rate limiter acquire() returns without token after sleep
**File:** `shared/intelligence/llm_rate_limiter.py`  
**Lines:** 146-149  
**Severity:** Critical  
**What happens:** If token is still unavailable after the sleep, the function returns silently. The caller (`_process_one`) proceeds to call the LLM anyway, violating rate limits and getting 429s.  
**Root cause:** Missing `raise RuntimeError("Rate limit exceeded")` or recursive retry.  
**Fix:** After second failed acquire, raise RuntimeError or enqueue the request.

### BUG 3: Temp files from CDN download never cleaned up
**File:** `shared/intelligence/interpretation_service.py`  
**Lines:** 905-909  
**Severity:** Critical  
**What happens:** `tempfile.NamedTemporaryFile(delete=False)` creates files that persist forever. With 293+ media items, /tmp fills up.  
**Root cause:** `delete=False` and no cleanup code.  
**Fix:** Use `delete=True` (default) and keep file handle open, or wrap in try/finally with `os.unlink()`.

### BUG 4: No file size validation before base64 encode
**File:** `shared/intelligence/interpretation_service.py`  
**Lines:** 161-164  
**Severity:** Critical  
**What happens:** `_encode_image_b64()` reads the entire file into memory. A 100MB image OOMs the process.  
**Root cause:** No size check before `f.read()`.  
**Fix:** Check `os.path.getsize(path) > MAX_IMAGE_BYTES` (e.g., 10MB) and skip/resize.

### BUG 5: Cross-database JOIN in dedup functions
**File:** `shared/intelligence/trade_dedup.py`  
**Lines:** 74-75, 172-173  
**Severity:** Critical  
**What happens:** `find_duplicate_position` JOINs `public.trader_profiles` with `tracked_positions`. If called with a company pool, `public.trader_profiles` doesn't exist. If called with shared pool, `tracked_positions` doesn't exist.  
**Root cause:** Assumes both tables are in the same database.  
**Fix:** Pass trader_profile_id as parameter instead of JOINing, or query separately.

---

## HIGH (Fix Today)

### BUG 6: SQL placeholder inconsistency
**File:** `shared/intelligence/interpretation_service.py`  
**Lines:** 346, 356-361, 618-619  
**Severity:** High  
**What happens:** `fetch_pending_media` uses `$1`/`$2` (asyncpg style), but `run_quant_track` uses `%s` (psycopg2 style). If the same pool type is used for both, one crashes.  
**Root cause:** Mixed SQL placeholder styles across the codebase.  
**Fix:** Standardize on `$1`/`$2` for all asyncpg queries.

### BUG 7: Empty system prompt fallback
**File:** `shared/intelligence/interpretation_service.py`  
**Lines:** 258-260  
**Severity:** High  
**What happens:** If `chart_analysis.json` is missing or the key is absent, `system_prompt` becomes empty string. The LLM has no instructions and hallucinates random outputs.  
**Root cause:** `.get("system_prompt", "")` returns empty string as fallback.  
**Fix:** Use a hardcoded safe fallback prompt, or raise an error if prompts can't be loaded.

### BUG 8: text_signal_extractor sends system prompt twice
**File:** `shared/intelligence/text_signal_extractor.py`  
**Lines:** 294-299  
**Severity:** High  
**What happens:** The system prompt `_LLM_TEXT_SYSTEM` is included in both the system message AND concatenated into the user message. Wastes ~30% tokens and may confuse the model.  
**Root cause:** `user_prompt = _LLM_TEXT_SYSTEM + "\n\nMessage:\n" + text`.  
**Fix:** Remove system prompt from user message — it's already in the system role.

### BUG 9: run_quant_track doesn't handle None symbol
**File:** `shared/intelligence/interpretation_service.py`  
**Lines:** 344-351  
**Severity:** High  
**What happens:** If `instrument_symbol` is None (happens when `instruments_jsonb` is empty), the SQL query `WHERE symbol = %s` crashes with `None` parameter.  
**Root cause:** No guard clause for None symbol.  
**Fix:** Return early with `QuantResult(direction="unclear", confidence=0.0)` if symbol is None.

### BUG 10: Missing retry on DB operations
**File:** `shared/intelligence/interpretation_service.py`  
**Lines:** 694-698  
**Severity:** High  
**What happens:** `get_or_create_trader_profile` has no retry logic. A transient DB connection failure causes the entire interpretation cycle to fail for that item.  
**Root cause:** No `try/except/retry` wrapper.  
**Fix:** Add exponential backoff retry (3 attempts) around the DB call.

---

## MEDIUM (Fix This Week)

### BUG 11: Float arithmetic for financial values
**File:** `shared/intelligence/position_quant.py`  
**Lines:** 14-41  
**Severity:** Medium  
**What happens:** P&L calculations use `float`, which accumulates rounding errors on large numbers.  
**Root cause:** Using float instead of Decimal for monetary values.  
**Fix:** Use `decimal.Decimal` for all price/quantity calculations.

### BUG 12: update_media_status acquires new connection per call
**File:** `shared/intelligence/interpretation_service.py`  
**Lines:** 784-785, 793-794  
**Severity:** Medium  
**What happens:** Each media status update opens a new DB connection. With batch processing, this creates connection churn.  
**Root cause:** `async with shared_pool.acquire()` inside the function.  
**Fix:** Accept a connection parameter, or batch updates.

### BUG 13: _estimate_cost_usd has hardcoded pricing
**File:** `shared/intelligence/interpretation_service.py`  
**Lines:** 214-219  
**Severity:** Medium  
**What happens:** Only 2 models have pricing. New models (GPT-4o, Gemini 2.5 Flash) default to expensive Claude pricing.  
**Root cause:** Hardcoded dict with 2 entries.  
**Fix:** Load pricing from config or API, or add common models.

### BUG 14: fetch_pending_media doesn't filter enabled sources
**File:** `shared/intelligence/interpretation_service.py`  
**Lines:** 596-619  
**Severity:** Medium  
**What happens:** Processes media from disabled channels/sources because there's no `enabled = TRUE` filter.  
**Root cause:** Missing JOIN to sources/channels with enabled filter.  
**Fix:** JOIN `news_sources` and filter `enabled = TRUE`.

### BUG 15: run_llm_track doesn't validate image MIME type
**File:** `shared/intelligence/interpretation_service.py`  
**Lines:** 255-256  
**Severity:** Medium  
**What happens:** If a non-image file (PDF, text) is passed, `_image_mime_type` returns a generic type and the vision API rejects it with a confusing error.  
**Root cause:** No validation that the file is actually an image.  
**Fix:** Check MIME type is in allowed image types before calling vision LLM.

### BUG 16: Missing trading fees in P&L
**File:** `shared/intelligence/position_quant.py`  
**Lines:** 14-41  
**Severity:** Medium  
**What happens:** `compute_pnl()` returns gross P&L without subtracting trading fees. Reported profitability is overstated.  
**Root cause:** No fee parameter.  
**Fix:** Add `maker_fee` and `taker_fee` parameters, default to 0.1%.

### BUG 17: compute_pnl_pct missing side validation
**File:** `shared/intelligence/position_quant.py`  
**Lines:** 44-62  
**Severity:** Medium  
**What happens:** If `side` is not "long" or "short", the function silently returns wrong result (direction = 1.0 for anything except "long").  
**Root cause:** No validation like `compute_pnl` has.  
**Fix:** Add `if side not in ("long", "short"): raise ValueError`.

### BUG 18: find_duplicate_position tolerance edge case
**File:** `shared/intelligence/trade_dedup.py`  
**Lines:** 99  
**Severity:** Medium  
**What happens:** Division by zero if `existing_entry` is 0 (though guarded by line 96, float precision could make it ~0).  
**Root cause:** `abs(entry - existing_entry) / existing_entry` — no epsilon check.  
**Fix:** Add `if existing_entry < 1e-9: continue`.

### BUG 19: _parse_llm_json doesn't handle nested markdown
**File:** `shared/intelligence/interpretation_service.py`  
**Lines:** 222-241  
**Severity:** Medium  
**What happens:** If LLM returns JSON inside nested markdown (e.g., explanation text then ```json), the regex may capture the wrong block.  
**Root cause:** Greedy `.*` in regex.  
**Fix:** Use non-greedy `.*?` and validate the parsed JSON has expected keys.

### BUG 20: Missing timeout on DB operations
**File:** `shared/intelligence/interpretation_service.py`  
**Lines:** 618-619  
**Severity:** Medium  
**What happens:** `conn.fetch()` has no timeout. A slow query blocks the entire interpretation cycle indefinitely.  
**Root cause:** No `timeout` parameter on DB calls.  
**Fix:** Add `timeout=30` to all DB operations.

---

## LOW (Nice to Have)

### BUG 21: _load_prompts() silently ignores missing file
**File:** `shared/intelligence/interpretation_service.py`  
**Lines:** 183-208  
**Severity:** Low  
**What happens:** If `chart_analysis.json` is missing, returns empty dict. No warning logged.  
**Fix:** Log a warning when falling back to empty prompts.

### BUG 22: compute_distance_to_sl_tp doesn't handle None SL/TP
**File:** `shared/intelligence/position_quant.py`  
**Lines:** 65-100  
**Severity:** Low  
**What happens:** If both SL and TP are None, returns distances of 0.0 which is misleading.  
**Fix:** Return `None` for missing distances.

### BUG 23: No validation that image is actually a chart
**File:** `shared/intelligence/interpretation_service.py`  
**Lines:** 244-316  
**Severity:** Low  
**What happens:** Memes, selfies, and random images are sent to the vision LLM, wasting API cost.  
**Fix:** Add a lightweight pre-filter (Gemini 2.5 Flash) to check if image contains a chart.

### BUG 24: Missing index on media_items.processing_status
**File:** Database schema  
**Severity:** Low  
**What happens:** `fetch_pending_media` does a sequential scan on `media_items` filtering by `processing_status`. With thousands of rows, this is slow.  
**Fix:** Add `CREATE INDEX idx_media_status ON media_items(processing_status, collected_at)`.

### BUG 25: GatewayConfig.for_service doesn't validate API key
**File:** `shared/intelligence/gateway_config.py`  
**Lines:** 46-95  
**Severity:** Low  
**What happens:** Returns a config with empty API key. The error only surfaces at call time.  
**Fix:** Log a warning if API key is empty at construction time.

---

## Summary

| Severity | Count | Key Issues |
|----------|-------|------------|
| Critical | 5 | Schema mismatch, rate limit bypass, temp file leak, OOM risk, cross-DB JOIN |
| High | 5 | SQL inconsistency, empty prompt, double prompt, None symbol, no DB retry |
| Medium | 8 | Float P&L, connection churn, hardcoded pricing, no enabled filter, etc. |
| Low | 5 | Missing validations, missing indexes, silent failures |

**Recommended fix order:** 3 → 4 → 2 → 1 → 5 → 9 → 7 → 8 → 6 → 10
