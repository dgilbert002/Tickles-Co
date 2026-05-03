# Phase X.0 — Position Pipeline Diagnosis: RESULTS
**Date:** 2026-05-02
**Mode:** Debug
**Source plan:** [`shared/docs/PHASE_X0_POSITION_PIPELINE_DIAGNOSIS.md`](shared/docs/PHASE_X0_POSITION_PIPELINE_DIAGNOSIS.md:1)
**Status:** Diagnosis complete. Multi-failure stack confirmed. Ready for Code mode.

---

## TL;DR — What's broken (one paragraph)

The position-tracking pipeline has been silently dead since 2026-05-01 11:06 UTC. **77 open `tracked_positions` exist, all with `entry_price=NULL`, `position_size=NULL`, `take_profit_1=NULL`, `epic_code=NULL`, `expiry_at=NULL`**, and **all hardcoded to `BTCUSDT/bybit/long`** because [`resolve_instrument_symbol()`](shared/intelligence/interpretation_service.py:871-894) defaults to that triple when the chart yields no symbol. The `instruments` table actually stores **`BTC/USDT`** (with slash), so [`fetch_latest_price()`](shared/intelligence/position_monitor.py:158-171) — which does exact match `WHERE symbol = $1 AND exchange = $2` — finds zero rows for every position. PositionMonitor returns `no_price_data` 77 times per cycle, writes **zero** `position_updates`, and is silent because it has no log file (running as a manual root process, not under systemd). The 9 `position_postmortems` rows are smoke-test stubs (19-char `what_happened`, `cost_usd=0`, no real LLM output) for non-existent position_ids 1001–1005 and 218516.

---

## Database connection used

```
host=127.0.0.1 port=5432 user=admin db=tickles_shared
```
All `tracked_positions / position_updates / position_postmortems / signal_interpretations / candles / instruments` live in **`tickles_shared.public`**. Per-company DBs (`tickles_jarvais`, `tickles_rubicon`, `tickles_testcorp`, `tickles_tradelab`) exist but the position pipeline does not use them.

---

## §1. Diagnostic Query Results

### Q1 — open positions snapshot
```
total=77   open=77   partial=0   closed=0
postmortem_pending=77   earliest=2026-04-30 21:25   latest=2026-05-01 11:06
```
**No new positions created in the last 30 hours.** No closes, ever.

### Q2 — sample 15 most-recent open positions
| Field | Value across ALL 15 |
|---|---|
| `instrument_symbol` | **`BTCUSDT`** (every row) |
| `instrument_exchange` | **`bybit`** (every row) |
| `direction` | **`long`** (every row) |
| `entry_price` | **NULL** (every row) |
| `stop_loss` | NULL (most), 64000 (a few) |
| `take_profit_1` | **NULL** (every row) |
| `position_size` | **NULL** (every row) |
| `leverage` | **NULL** (every row) |
| `epic_code` | **NULL** (every row) |
| `expiry_at` | **NULL** (every row) |
| `matched_instrument_id` (JOIN test) | **NULL** — no `instruments` row matches `BTCUSDT/bybit` |
| `latest_candle` | **NULL** — JOIN fails so price lookup fails |

### Q3 / Q4 — table totals & candle freshness
```
media_items            1293
news_items             2938
signal_interpretations  430   (latest 2026-05-02 18:55 — STILL flowing)
tracked_positions        77   (frozen since 2026-05-01 11:06)
position_updates          0   ← PositionMonitor has NEVER written a single row
position_postmortems      9   (all stubs — see Q8)
instruments              50
trader_profiles          15
```
```
candles for instrument_id=3 (BTC/USDT bybit): 136,475 rows, latest=2026-05-02 19:00
```
**Candles are fresh — H1 (no candles) is REJECTED.** The data is there; the JOIN can't reach it.

### Q5 — `instruments` rows for BTC
```
 id |  symbol  | exchange | asset_class | base | quote | active
----+----------+----------+-------------+------+-------+--------
  2 | BTC/USDT | binance  | crypto      | BTC  | USDT  | t
  3 | BTC/USDT | bybit    | crypto      | BTC  | USDT  | t
```
**Only TWO BTC rows exist, both with the slash form `BTC/USDT`.** Code writes the no-slash form `BTCUSDT`. That is the entire reason the pipeline is dead.

### Q6 — `position_updates` activity
```
total_updates=0   latest_update=NULL
```

### Q7 — signal_interpretations diversity & levels
```
total=430   long=41   short=41   unclear=348
distinct_symbols=1   distinct_exchanges=1
with_llm_levels=43   has_entry=43   has_sl=43   has_tp=43
```
Only **10% of interpretations carry LLM-detected levels.** The other 90% have empty `{}` — when those become tracked_positions, `entry_price`/`stop_loss`/`take_profit_1` are all NULL by construction.

### Q8 — `position_postmortems` truth check
```
  id | position_id | model | what_happened_len | lessons_len | cost_usd | created_at
   1 |  1001       | gpt-4 | 19  | 0 | 0.00 | 2026-04-30 21:25
   3 |  1002       | gpt-4 | 19  | 0 | 0.00 | 2026-04-30 21:25
   4 |  1002       | gpt-4 | 19  | 0 | 0.00 | 2026-04-30 21:25
   5 |  1003       | gpt-4 | 19  | 0 | 0.00 | 2026-04-30 21:25
   6 |  1004       | gpt-4 | 19  | 0 | 0.00 | 2026-04-30 21:25
   7 |  1005       | gpt-4 | 19  | 0 | 0.00 | 2026-04-30 21:25
   8 |  1005       | gpt-4 | 19  | 0 | 0.00 | 2026-04-30 21:25
 123 |  218516     | gpt-4 | 19  | 0 | 0.00 | 2026-05-01 11:06
 124 |  218516     | gpt-4 | 19  | 0 | 0.00 | 2026-05-01 11:06
```
None of those `position_id` values exist in `tracked_positions`. **All 9 postmortems are orphaned smoke-test stubs.** The PostMortemService has never produced a real LLM output. Confirms architect's smoking-gun finding [`shared/docs/PHASE_X0_POSITION_PIPELINE_DIAGNOSIS.md:1`](shared/docs/PHASE_X0_POSITION_PIPELINE_DIAGNOSIS.md:1).

### Q9 — process & systemd inventory
| Daemon | Systemd? | Process? | PID | Log file | Notes |
|---|---|---|---|---|---|
| candle_daemon | ✅ | ✅ | — | `/var/log/tickles/candle_daemon.log` | OK |
| catalog | ✅ | ✅ | — | `/var/log/tickles/catalog.log` | OK |
| backtest_workers | ✅ | ✅ | — | `/var/log/tickles/backtest_workers.log` | OK |
| resample | ✅ | ✅ | — | `/var/log/tickles/resample.log` | OK |
| funding_collector | ✅ | ✅ | — | journal | OK |
| md_gateway | ✅ | ✅ | — | journal | OK |
| cost_shipper | ✅ | ✅ | — | journal | OK |
| mcpd | ✅ | ✅ | — | journal | OK |
| rubicon_surgeon, rubicon_surgeon2 | ✅ | ✅ | — | journal | OK |
| **interpretation_service** | ❌ | ✅ | 4167917 | **none** | manual root process since Apr 27 |
| **position_monitor** | ❌ | ✅ | 4167936 | **none** | manual root process since Apr 27 |
| **postmortem_service** | ❌ | ❌ | — | — | not running anywhere |
| **chart_hacker_opinion_service** | ❌ | ❌ | — | — | not running |
| **edge_scorer_service** | ❌ | ❌ | — | — | not running |
| **coach_service** | ❌ | ❌ | — | — | not running |
| **memu_listener** | ❌ | ❌ | — | — | not running |
| discord_collector | unit exists | ❌ | — | — | unit file present, not started |

---

## §2. Hypothesis Verdicts

| ID | Hypothesis | Verdict | Evidence |
|---|---|---|---|
| **H1** | "No candles available" | **REJECTED** | 136,475 fresh BTC/USDT bybit candles in DB |
| **H2** | "PositionMonitor not running / not under systemd" | **PARTIALLY CONFIRMED** | Process IS running (PID 4167936), but NOT under systemd; no log file; no journal entries; `position_updates` count = 0 confirms it has NEVER successfully processed a position |
| **H3** | "Off-by-one candle / timezone" | **N/A** | Pipeline never reaches snapshot logic |
| **H4** | "Postmortem service is a stub" | **CONFIRMED** | 9 orphan stubs with 19-char placeholder text; no real LLM output ever; service not running |
| **H5** | "SL/TP detection broken" | **CONFIRMED & WORSE** | Not just SL/TP — `entry_price`, `position_size`, `take_profit_1`, `epic_code`, `expiry_at`, `leverage` are ALL NULL across all 77 positions |

## §3. NEW BUGS DISCOVERED (beyond original 5 hypotheses)

### Bug A — Wrong column name in PositionMonitor (CRITICAL)
- **File:** [`shared/intelligence/position_monitor.py:254`](shared/intelligence/position_monitor.py:254)
- **Code:** `tp = position.get("take_profit")`
- **Reality:** SQL at [`shared/intelligence/position_monitor.py:128`](shared/intelligence/position_monitor.py:128) selects `take_profit_1`, and the DB column is `take_profit_1`. There is no `take_profit` key in the row dict.
- **Impact:** `tp` is always `None` → `tp_val` always `None` → `compute_distance_to_sl_tp` and `tp_hit` always falsy. **No position can ever close via take-profit, even if everything else is fixed.**

### Bug B — `_build_snapshot` crashes on NULL `entry_price` / `position_size`
- **File:** [`shared/intelligence/position_monitor.py:250-251`](shared/intelligence/position_monitor.py:250)
- **Code:** `entry = float(position["entry_price"])` and `qty = float(position["position_size"])`
- **Reality:** 77/77 open positions have BOTH columns NULL. `float(None)` → `TypeError`. Caught by the wrapping `try/except` at [`shared/intelligence/position_monitor.py:539-548`](shared/intelligence/position_monitor.py:539). `logger.exception()` writes to stderr, but stderr → /dev/null because no log file is configured.
- **Impact:** Each cycle (60s) silently fails on every position. Invisible. **(Latent — currently masked by Bug C short-circuiting first.)**

### Bug C — Symbol-format mismatch defeats the JOIN (PRIMARY BLOCKER)
- **Where:** [`shared/intelligence/interpretation_service.py:881,889,894`](shared/intelligence/interpretation_service.py:881)
- **Code:** `resolve_instrument_symbol()` defaults to `("BTCUSDT", "bybit")` and stores raw `first.get("symbol")` unchanged into `tracked_positions.instrument_symbol`.
- **Mismatch:** `instruments.symbol = 'BTC/USDT'` (with slash), but `tracked_positions.instrument_symbol = 'BTCUSDT'` (no slash). [`fetch_latest_price()`](shared/intelligence/position_monitor.py:158-171) does exact match `WHERE symbol = $1 AND exchange = $2 AND is_active = TRUE`. **Zero matches → price=None → returns `no_price_data` → no row written, no error.**
- **Why it short-circuits before Bug B:** The price lookup fails before `_build_snapshot()` is called, so the NULL-entry crash is hidden behind this.
- **Note:** [`shared/utils/instrument_normaliser.py:23`](shared/utils/instrument_normaliser.py:23) `normalise_instrument()` exists and IS being called for `instrument_symbol_normalised`, but the raw `instrument_symbol` is the column the JOIN uses.

### Bug D — Hardcoded fallback `("BTCUSDT","bybit")` masks all real signals
- **File:** [`shared/intelligence/interpretation_service.py:871-894`](shared/intelligence/interpretation_service.py:871)
- **Code:** `resolve_instrument_symbol()` returns `("BTCUSDT", "bybit")` whenever the news_items.instruments JSONB is empty/null/malformed.
- **Reality:** ALL 430 signal_interpretations show `instrument_symbol='BTCUSDT'` and `exchange='bybit'`. Zero diversity. The "instruments" JSONB on news_items is empty for ~all 1293 media items, so the fallback fires every time.
- **Impact:** Even if Bug C were fixed by aliasing, the platform would only ever track BTCUSDT — losing all other tickers in the chart corpus.

### Bug E — PositionMonitor & InterpretationService run unsupervised, output to /dev/null
- **Verified:** PIDs 4167917 and 4167936 running since Apr 27 with no `/var/log/tickles/*.log` entries, no systemd unit, no journal. Errors invisible.
- **Impact:** Bug B's `TypeError` storm has been happening 60s/cycle for 5 days, undetected.

### Bug F — `write_signal_interpretation()` SQL writes columns that don't exist
- **File:** [`shared/intelligence/interpretation_service.py:978-1006`](shared/intelligence/interpretation_service.py:978)
- **INSERT lists:** `prefilter_provider`, `instrument_resolved_from`
- **Schema reality:** `signal_interpretations` has neither column.
- **But:** 430 rows have been inserted, so the INSERT is succeeding. **This means the deployed code is NOT the version on disk** — either the INSERT was edited recently (post-deployment) and the manual root processes are stale, OR the schema migration that adds these columns hasn't run yet.
- **Action required:** confirm before Code-mode fix — `git log -p shared/intelligence/interpretation_service.py | head -100` to compare on-disk vs running.

### Bug G — Postmortem stubs with hardcoded `position_id` values 1001-1005, 218516
- **DB evidence:** 9 rows in `position_postmortems` reference position_ids that do not exist in `tracked_positions`.
- **Source:** Architect-pass already identified [`shared/intelligence/postmortem_service.py:117-136`](shared/intelligence/postmortem_service.py:117) `_run_llm_postmortem()` returns hardcoded stub fixtures.
- **Impact:** The 9 stubs are smoke-test detritus. PostMortemService has never produced a real LLM output. Even if it were running.

---

## §4. Root-cause causal chain

```
Discord/Telegram chart arrives
        │
        ▼
news_items.instruments JSONB populated POORLY (90% empty)
        │
        ▼
resolve_instrument_symbol()  ── fallback ──►  ("BTCUSDT","bybit")     [Bug D]
        │
        ▼
LLM track returns (often) empty levels{}                              [data quality]
        │
        ▼
write_signal_interpretation INSERT ───►  430 rows, all BTCUSDT       [Bug F lurks]
        │
        ▼
create_tracked_position_from_interpretation() ──► tracked_positions
   instrument_symbol='BTCUSDT'  (no slash)                             [Bug C source]
   entry_price/stop_loss/take_profit_1/position_size = NULL  (90%)     [Bug B source]
        │
        ▼
PositionMonitor cycle (60s):
   fetch_latest_price('BTCUSDT','bybit')
        │ WHERE symbol = 'BTCUSDT' AND exchange = 'bybit'
        │ instruments has only 'BTC/USDT'/'bybit'
        ▼ 0 rows → price = None
   return {"status": "no_price_data"}                                  [Bug C blocks]
   ─────────────────────────────────── never reaches Bug A or Bug B ─────
   logged via logger.warning, but no log file → /dev/null              [Bug E]
        │
        ▼
0 position_updates rows ever written
0 positions ever close
0 real postmortems ever generated (stubs only)                         [Bug G]
0 evidence of pipeline failure visible to operators
```

---

## §5. Required Fixes (revised F1–F10)

Architect's original F1–F5, plus five new fixes (F6–F10) — F9 and F10 added after user's D6/D7/D8 directives (see §8):

| # | Fix | File(s) | Severity |
|---|---|---|---|
| **F1** | Symbol normalisation at JOIN — adopt `BTC/USDT` slash form as the SINGLE canonical form (per D6). Update [`normalise_instrument()`](shared/utils/instrument_normaliser.py:23) to return slash form. Fix [`fetch_latest_price()`](shared/intelligence/position_monitor.py:158) to call `normalise_instrument()` on the input symbol BEFORE the SQL lookup. Also seed `instrument_aliases` with `BTCUSDT → BTC/USDT`, `BTC-USDT → BTC/USDT`, etc., for backwards compatibility | [`shared/intelligence/position_monitor.py:158`](shared/intelligence/position_monitor.py:158), [`shared/utils/instrument_normaliser.py:23`](shared/utils/instrument_normaliser.py:23) | CRITICAL |
| **F2** | Expiry-based closer — implement an "auto-close when `now() > expiry_at`" branch in `_process_one()` (currently `expiry_at` column exists but no code uses it). On expiry-close, MUST also deduct fees per F10 | [`shared/intelligence/position_monitor.py:442`](shared/intelligence/position_monitor.py:442) | HIGH |
| **F3** | Wire real LLM postmortem — replace stub `_run_llm_postmortem` with `call_vision_llm` using payload_store; delete the 9 orphan stubs | [`shared/intelligence/postmortem_service.py:117`](shared/intelligence/postmortem_service.py:117) | HIGH |
| **F4** | Systemd units for: `position_monitor`, `interpretation_service`, `postmortem_service`, `chart_hacker_opinion_service`, `edge_scorer_service`, `coach_service`, `memu_listener`. Each with `StandardOutput=append:/var/log/tickles/<name>.log` | `systemd/` | CRITICAL |
| **F5** | SL/TP sanity guard — refuse to create `tracked_position` rows when `entry_price IS NULL` UNLESS the F9 backfill path supplies a candle-time fallback. Log and `update_media_status='skipped_no_price'` for non-recoverable cases | [`shared/intelligence/interpretation_service.py:1543`](shared/intelligence/interpretation_service.py:1543) | HIGH |
| **F6** | Fix `take_profit` → `take_profit_1` typo so TP-hit can ever fire | [`shared/intelligence/position_monitor.py:254`](shared/intelligence/position_monitor.py:254) | CRITICAL |
| **F7** | Guard `_build_snapshot` against NULL entry/size — return `{"status":"missing_levels"}` early instead of crashing into try/except | [`shared/intelligence/position_monitor.py:248`](shared/intelligence/position_monitor.py:248) | HIGH |
| **F8** | Remove `("BTCUSDT","bybit")` hardcoded fallback in [`resolve_instrument_symbol()`](shared/intelligence/interpretation_service.py:871). Return `(None, None)` and let the caller skip the signal. Also: improve upstream so news_items.instruments is populated by the InstrumentExtractor / ChartHacker (currently empty for 90%+ of items) | [`shared/intelligence/interpretation_service.py:871`](shared/intelligence/interpretation_service.py:871) | CRITICAL |
| **F9** | **(NEW — D8)** P&L backfill job for the 77 NULL-entry orphan positions. Algorithm per row: (1) fetch `signal_interpretations.raw_signal_text` and parse for entry/SL/TP via regex (e.g. `entry[:\s]*(\d+\.?\d*)`); (2) if no levels parsed, set `entry_price = candles.close` at `signal_interpretations.created_at` for the resolved instrument; (3) walk forward through candles until SL or TP hit, OR until `expiry_at`, OR until `now()`; (4) compute realized P&L per F10; (5) write `status_reason='backfilled_retrospective'`, `closed_at`, `realized_pnl_usd_final`, `entry_reason_frozen_at=signal_interpretations.created_at`. New script: `shared/scripts/backfill_orphan_positions.py` | new file | HIGH |
| **F10** | **(NEW — D7)** Fee-accurate close — replicate real exchange/broker behavior on every close (SL, TP, expiry, manual, backfill). Formula: `realized_pnl_usd_final = (exit_price - entry_price) × size × dir − (entry_notional × taker_fee_pct) − (exit_notional × taker_fee_pct) − (notional × overnight_funding_pct × hours_held / 24)`. Read `taker_fee_pct`, `maker_fee_pct`, `overnight_funding_long_pct`, `overnight_funding_short_pct` from [`instruments`](shared/migration/tickles_shared_pg.sql:1) row joined on `instrument_id`. Add helper `shared/intelligence/fee_calc.py` with `compute_realized_pnl(...)` | new file `shared/intelligence/fee_calc.py`, [`shared/intelligence/position_monitor.py:350`](shared/intelligence/position_monitor.py:350) (`update_position_outcome`) | HIGH |

**Pre-fix audit step:** Verify on-disk vs running code (Bug F) — `git log` and confirm the running PIDs are using the deployed version. If not, restart after F1–F10 land.

**Priority order:** F6 (1-line typo) → F1 (symbol JOIN, slash-form canonical) → F7 (NULL guard) → F8 (kill BTCUSDT fallback) → F10 (fee-calc helper, needed by F2/F9) → F5 (SL/TP sanity) → F2 (expiry closer) → F9 (backfill 77 orphans) → F3 (real LLM postmortem) → F4 (systemd units, last).

---

## §6. Files touched / artifacts

- SQL tempfiles created during diagnosis: `/tmp/q2.sql`, `/tmp/q6.sql`, `/tmp/q_inst2.sql`, `/tmp/q_si.sql`, `/tmp/q_levels.sql`, `/tmp/q_misc.sql`, `/tmp/q_si_schema.sql`, `/tmp/q_pm.sql`, `/tmp/q_pm2.sql`, `/tmp/q_tp_full.sql`, `/tmp/q_tp_nulls.sql`
- This document: `.roo/handoffs/2026-05-02-position-pipeline-results.md`
- Code/SQL files read but **NOT modified**: [`shared/intelligence/position_monitor.py`](shared/intelligence/position_monitor.py:1), [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:1), [`shared/utils/instrument_normaliser.py`](shared/utils/instrument_normaliser.py:1)

---

## §7. Resume command for Code mode

> Apply fixes F1–F10 in [`.roo/handoffs/2026-05-02-position-pipeline-results.md`](.roo/handoffs/2026-05-02-position-pipeline-results.md:1) §5 in the priority order specified there. **F6 (1-line typo) and F1 (symbol JOIN, `BTC/USDT` slash-form canonical) first** — they unblock the daemon. Then F7 → F8 → F10 (fee helper) → F5 → F2 (expiry closer, uses F10) → F9 (backfill 77 orphans, uses F10) → F3 → F4. Pre-flight: audit on-disk vs running PID (Bug F), `git log` and `pmap`. After every fix: restart the affected daemon, watch `/var/log/tickles/position_monitor.log` for at least 60 seconds, and verify with `SELECT count(*) FROM position_updates WHERE created_at > now() - interval '5 minutes'` that rows are now being written. Final acceptance: at least one of the 77 orphan positions has `realized_pnl_usd_final IS NOT NULL` and a non-empty `postmortem.what_happened`.

---

## §8. Binding directives from user (2026-05-02 — answers to open questions)

User's verbatim reply:
> "i need whatever is the best. signals or coins or stock should not be ambiguos. fee policy, repliacte what exchanges or brokers do, 77 LL positions, if they're the traders, then try resolve or figure out P&L"

Decoded into three binding directives:

### D6 — Symbol canonical form: pick the BEST, NO AMBIGUITY
- **Decision:** Adopt **`BTC/USDT` (slash form)** as the single canonical form across the entire pipeline.
- **Rationale:** It already matches what's in the [`instruments`](shared/migration/tickles_shared_pg.sql:1) table (CCXT/exchange convention), it round-trips losslessly through `ccxt`, it's unambiguous (slash separates base from quote), and standardising on it requires no schema migration.
- **Implementation:** F1 — update [`normalise_instrument()`](shared/utils/instrument_normaliser.py:23) to return slash form. Seed `instrument_aliases` so legacy `BTCUSDT` and `BTC-USDT` rows still resolve. EVERY new write of a symbol — `tracked_positions.instrument_symbol`, `signal_interpretations.instrument_symbol`, `news_items.instruments[].symbol`, `candles` lookups — MUST go through `normalise_instrument()` first. Add a CI gate in `shared/scripts/writer_registry_grep.py` that fails on raw symbol concatenation.

### D7 — Fee policy: replicate real exchange/broker behavior
- **Decision:** On every close (SL, TP, expiry, backfill, manual), deduct fees and overnight funding per the real instrument's fee schedule.
- **Source of truth:** the existing `instruments` columns `taker_fee_pct`, `maker_fee_pct`, `overnight_funding_long_pct`, `overnight_funding_short_pct`, `contract_multiplier`, `spread_pct`. NO hardcoded fees.
- **Implementation:** F10 — new [`shared/intelligence/fee_calc.py`](shared/intelligence/fee_calc.py:1) (to be created) with `compute_realized_pnl(entry, exit, size, direction, instrument_row, hours_held) -> Decimal`. Use `Decimal`, never `float`. Include both legs (entry-side taker fee + exit-side taker fee), spread cost (`spread_pct × notional`), and overnight funding accrual (`funding_pct × notional × hours_held / 24`). Wire into `update_position_outcome()`, the F2 expiry closer, and the F9 backfill loop.
- **Future hook:** the `mcp--ccxt` server is available — a periodic job can refresh `instruments.taker_fee_pct` from the live exchange schedule. Out of scope for this round; document only.

### D8 — 77 NULL-entry positions: try to resolve P&L from the trader signals
- **Decision:** DO NOT discard. These represent real trader signals from the corpus — backfill them.
- **Approach (F9):**
  1. **Parse `raw_signal_text`** of the linked `signal_interpretations` row for entry/SL/TP via regex (`entry[:\s]*([0-9]+(?:\.[0-9]+)?)`, `sl[:\s]*…`, `tp[:\s]*…`). If parse succeeds AND values are sane (within ±20% of candle-close at signal time) → use them.
  2. **Else, fallback to candle-time entry:** set `entry_price = candles.close` at `signal_interpretations.created_at` for the resolved instrument. Mark `entry_reason_trader='backfilled_from_candle'`.
  3. **Walk forward** through 1m candles from `signal_interpretations.created_at`. Close at the first of: SL hit, TP hit, `expiry_at` reached, or `now()`.
  4. **Compute P&L via F10** — full fee-accurate calculation.
  5. **Persist:** `status='closed'` (or `'open_backfilled'` if walk reached `now()`), `closed_at`, `realized_pnl_usd_final`, `status_reason='backfilled_retrospective'`, freeze `entry_reason_frozen_at=signal_interpretations.created_at`. Trigger postmortem service downstream.
- **Implementation:** new script [`shared/scripts/backfill_orphan_positions.py`](shared/scripts/backfill_orphan_positions.py:1). Idempotent — only touches rows where `realized_pnl_usd_final IS NULL`. Dry-run flag mandatory.
- **Acceptance:** ≥ 50 of 77 orphans get a non-NULL `realized_pnl_usd_final`. The remaining ≤ 27 (where parse fails AND candle data is missing) get `status='invalidated'`, `status_reason='unrecoverable_no_levels_no_candles'`.

These three directives are now baked into the F1–F10 fix table above. No further open questions.
