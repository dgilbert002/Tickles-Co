# Phase X.0 — Position Pipeline Diagnosis (PRIORITY ZERO)

**Status:** Architect trace complete (2026-05-02). Awaiting Debug mode for live SQL/systemd verification.
**Binding directive (user, 2026-05-02):** *"yes investigate the positions, there should by now at leaast be SOME trades that failed. if not, check the system or candle or tester thatvalidates the traders trades. until you can do that, you've not accomplished anything. we have 1000+ charts meaning we have trades. fix everything."*

---

## 1. Problem Statement

Charts have been collected and interpreted (46 SVGs confirmed in [`opticals/charts/`](opticals/charts:1), numbered 376–425 — implies at least 425 [`signal_interpretations`](shared/intelligence/interpretation_service.py:944) rows have been generated since the pipeline went live 2026-05-01 09:06 UTC). The user expects: **some of those trades MUST have hit SL/TP by now → there must be `closed` positions → there must be post-mortem rows in [`position_postmortems`](shared/intelligence/postmortem_service.py:138)**.

Observed reality: **post-mortem queue appears silent**. No closed positions are surfacing, no learnings are being produced, the position-detector daemon is silent.

> "Until you can do that, you've not accomplished anything."

The Phase Y Learning Dashboard plan is blocked until this is unblocked — a learning dashboard with no learning data is theatre.

---

## 2. Architectural Trace (Verified by Architect 2026-05-02)

The position pipeline has **exactly one INSERT path** and **exactly one CLOSE path**. There are no alternate writers.

```
chart SVG  ─►  media_items (status=downloaded)
              │
              ▼
         InterpretationService._process_one()      shared/intelligence/interpretation_service.py:1310
              │
              ▼
       signal_interpretations  (per-company)
              │
              ▼
    create_tracked_position_from_interpretation()  shared/intelligence/interpretation_service.py:1056
       └─► INSERT INTO public.tracked_positions (status='open')
              │
              ▼
    PositionMonitor.run_forever()  ◄── poll every 60s
       │  shared/intelligence/position_monitor.py:557
       │
       ├─► fetch_open_positions()                    line 111  — selects status IN ('open','partial_close')
       │
       ├─► fetch_latest_price()                      line 140  — joins candles ⨝ instruments BY symbol+exchange
       │   fetch_latest_price_by_epic()              line 199  — joins candles ⨝ instruments BY metadata->>'epic'
       │
       ├─► _build_snapshot()                         line 234  — computes sl_hit / tp_hit
       │
       ├─► write_position_update()                   line 310  — INSERT position_updates (always)
       │
       └─► IF snapshot.sl_hit OR snapshot.tp_hit:    line 489
             └─► update_position_outcome()           line 350
                  └─► UPDATE tracked_positions SET status='closed', outcome='take_profit'|'stop_loss'
                         │
                         ▼
                  PostMortemService.tick()           shared/intelligence/postmortem_service.py:180  — poll every 60s
                         │
                         ├─► _fetch_pending()        line 97  — WHERE status='closed' AND postmortem_status='pending'
                         │
                         ├─► _run_llm_postmortem()   line 117  ◄── ★★★ STUB — RETURNS PLACEHOLDER ★★★
                         │
                         └─► _write_postmortem()     line 138  — INSERT position_postmortems
```

### 2.1 Confirmed Facts

| Fact | Evidence |
|------|----------|
| Only INSERT into `tracked_positions` is from interpretation_service | [`interpretation_service.py:1056`](shared/intelligence/interpretation_service.py:1056) — sole `INSERT INTO public.tracked_positions` |
| Only CLOSE writer is `update_position_outcome()` | [`position_monitor.py:350`](shared/intelligence/position_monitor.py:350) — sole `UPDATE...SET status` for tracked_positions |
| Close fires ONLY on candle-driven SL/TP hit | [`position_monitor.py:489-505`](shared/intelligence/position_monitor.py:489) — `if snapshot.sl_hit or snapshot.tp_hit` is the only trigger |
| **NO timeout/expiry/manual-close fallback exists** | grep finds outcome literals `'manual_close'`, `'expired'` only in the docstring at [`position_monitor.py:365`](shared/intelligence/position_monitor.py:365) — no code emits them |
| **Post-mortem LLM is a STUB returning canned text** | [`postmortem_service.py:117-136`](shared/intelligence/postmortem_service.py:117) — *"Awaiting full LLM wiring in Phase 7 Day 2"*, `cost_usd=0.0` placeholder |
| Candle JOIN requires `instruments.symbol = tp.instrument_symbol AND instruments.exchange = tp.instrument_exchange AND is_active = TRUE` | [`position_monitor.py:163-167`](shared/intelligence/position_monitor.py:163) |
| Fallback: epic JOIN by `instruments.metadata->>'epic'` | [`position_monitor.py:218-222`](shared/intelligence/position_monitor.py:218) |
| Default poll interval: 60s, batch size: 50 | [`position_monitor.py:68-71`](shared/intelligence/position_monitor.py:68) |

### 2.2 Implications

- **If candles are missing/stale for the traded `instrument_symbol`** → `fetch_latest_price` returns `None` → `_process_one()` logs `no_price_data` at [`position_monitor.py:471`](shared/intelligence/position_monitor.py:471) and skips → position stays `open` forever.
- **If `tracked_positions.instrument_symbol` doesn't match any `instruments.symbol`** (case, separator, or capitalisation drift) → same outcome.
- **If PositionMonitor isn't running** (no systemd unit, advisory-lock contention, crash) → nothing closes.
- **Even if a position DOES close**, the LLM post-mortem is a stub → the Memory Feed gets canned text, not learnings.

---

## 3. Failure Hypotheses (Ranked by Likelihood)

| # | Hypothesis | Why likely | How to falsify |
|---|------------|-----------|----------------|
| **H1** | `tracked_positions` rows exist but are all `status='open'` because PositionMonitor cannot find candles for the traded symbols | Most positions are likely on CFD instruments (XAUUSD, GBPUSD, indices) collected by the Capital.com epic_resolver pipeline, but the candle collectors target crypto exchanges. Symbol/exchange mismatch is the textbook failure. | Q1, Q2 below |
| **H2** | PositionMonitor daemon is not running (no systemd unit registered, crashed, or never started) | The 2026-05-02 sweep showed only 8 of ~28 services have systemd units. PositionMonitor may have been written but never deployed. | Q5, Q6 below |
| **H3** | `tracked_positions` is empty — `create_tracked_position_from_interpretation` is never called because `signal_interpretations.consensus_signal` is always 'no_trade' / 'wait' / has zero confidence | If chart_hacker/LLM track returns ambiguous signals, no position is opened in the first place. The 425 signals may all be filtered out at consensus stage. | Q3, Q4 below |
| **H4** | PositionMonitor IS running and IS closing positions, but the post-mortem service is dead OR the post-mortem rows ARE being written but contain placeholder text only — so the user sees "nothing useful" rather than "nothing at all" | The LLM stub is confirmed-existing code. If close path works, `position_postmortems` has rows with `lessons='Awaiting full LLM wiring...'`. | Q7, Q8 below |
| **H5** | SL/TP values are being persisted incorrectly (e.g., `stop_loss = NULL`, or values in wrong direction relative to entry) so `check_sl_tp_hit` never returns true | LLM may return SL/TP in unexpected formats; no validation at INSERT time. | Q9 below |

---

## 4. Diagnostic Queries (For Debug Mode)

Run these in order against the `tickles_shared` database. Each query targets one hypothesis. Stop at the first one that produces non-empty surprising results.

### Q1 — Are there any tracked_positions at all? (H3 vs H1/H2)

```sql
SELECT
  status,
  outcome,
  count(*) AS n,
  min(created_at) AS earliest,
  max(created_at) AS latest
FROM public.tracked_positions
GROUP BY status, outcome
ORDER BY status, outcome;
```

**Decision tree:**
- 0 rows → **H3 confirmed**. Skip to Q3.
- All rows `status='open', outcome=NULL` → **H1 or H2 confirmed**. Continue Q2.
- Some `status='closed'` exist → **H4 partial**. Skip to Q7.

### Q2 — For open positions, do their instruments have ANY candles? (H1)

```sql
SELECT
  tp.id                     AS position_id,
  tp.instrument_symbol,
  tp.instrument_exchange,
  tp.metadata->>'epic'      AS epic,
  tp.created_at,
  i.id                      AS matched_instrument_id,
  i.is_active,
  (SELECT max("timestamp")
     FROM public.candles c
     WHERE c.instrument_id = i.id
       AND c.timeframe = '1m')   AS latest_candle_ts,
  EXTRACT(EPOCH FROM (NOW() - (
     SELECT max("timestamp") FROM public.candles c
     WHERE c.instrument_id = i.id AND c.timeframe = '1m'
  ))) / 60.0                AS candle_age_minutes
FROM public.tracked_positions tp
LEFT JOIN public.instruments i
  ON i.symbol = tp.instrument_symbol
 AND i.exchange = tp.instrument_exchange
 AND i.is_active = TRUE
WHERE tp.status IN ('open','partial_close')
ORDER BY tp.created_at DESC
LIMIT 30;
```

**Diagnose:**
- `matched_instrument_id IS NULL` everywhere → symbol/exchange mismatch. Go to Q2b.
- `matched_instrument_id` populated but `latest_candle_ts IS NULL` → instrument exists, no candle collector running for it. **H1 confirmed.**
- `candle_age_minutes > 60` → candle collector stale/dead. **H1 confirmed.**
- Candles fresh, but positions still open → SL/TP never triggered. Go to Q9.

### Q2b — Symbol/exchange mismatches (H1 sub-case)

```sql
-- What symbols are tracked_positions using?
SELECT DISTINCT tp.instrument_symbol, tp.instrument_exchange, count(*) AS n
FROM public.tracked_positions tp
WHERE tp.status IN ('open','partial_close')
GROUP BY tp.instrument_symbol, tp.instrument_exchange
ORDER BY n DESC;

-- What symbols/exchanges does the instruments table actually have, with candles?
SELECT i.symbol, i.exchange, count(c.id) AS candle_count, max(c."timestamp") AS latest
FROM public.instruments i
LEFT JOIN public.candles c ON c.instrument_id = i.id AND c.timeframe = '1m'
WHERE i.is_active = TRUE
GROUP BY i.symbol, i.exchange
ORDER BY candle_count DESC
LIMIT 50;
```

Cross-reference: any symbol in the first result missing from the second = stranded position.

### Q3 — Are signals being generated but filtered at consensus? (H3)

```sql
-- Per-company. Replace 'jarvais' with active company name.
SELECT
  consensus_signal,
  count(*) AS n,
  avg(consensus_confidence) AS avg_conf,
  min(created_at) AS earliest,
  max(created_at) AS latest
FROM jarvais.signal_interpretations
GROUP BY consensus_signal
ORDER BY n DESC;
```

**Diagnose:**
- All `'no_trade'` / `'wait'` → LLM is too cautious. **H3 confirmed.**
- Mostly `'long'`/`'short'` → signals are being made; problem is downstream. Continue to Q4.

### Q4 — Of the actionable signals, how many produced tracked_positions? (H3 sub-case)

```sql
SELECT
  count(*) FILTER (WHERE si.consensus_signal IN ('long','short'))  AS actionable_signals,
  count(DISTINCT tp.id)                                            AS positions_created,
  count(*) FILTER (WHERE si.consensus_signal IN ('long','short'))
    - count(DISTINCT tp.id)                                        AS lost_in_translation
FROM jarvais.signal_interpretations si
LEFT JOIN public.tracked_positions tp
  ON tp.metadata->>'signal_interpretation_id' = si.id::text
WHERE si.created_at >= NOW() - INTERVAL '7 days';
```

If `lost_in_translation > 0` → bug in [`create_tracked_position_from_interpretation`](shared/intelligence/interpretation_service.py:1056). Investigate that function's filters/guards.

### Q5 — Is PositionMonitor running? (H2, requires shell)

```bash
# Is there a systemd unit?
systemctl list-units --all | grep -i 'position'
ls -la /etc/systemd/system/tickles-position*.service 2>/dev/null

# Is the process running anywhere?
ps auxww | grep -i 'position_monitor' | grep -v grep

# Is anything bound to the advisory lock?
psql -d tickles_shared -c "
  SELECT pid, locktype, mode, granted, query_start, query
  FROM pg_locks
  JOIN pg_stat_activity USING (pid)
  WHERE locktype = 'advisory';"
```

### Q6 — When did PositionMonitor last write a position_updates row? (H2 evidence)

```sql
SELECT
  count(*) AS total_updates,
  max("timestamp") AS most_recent_update,
  EXTRACT(EPOCH FROM (NOW() - max("timestamp"))) / 60.0 AS minutes_since_last_update
FROM public.position_updates;
```

**Diagnose:**
- 0 rows ever → PositionMonitor has never run. **H2 confirmed.**
- `minutes_since_last_update > 5` and there are open positions → daemon is dead/stuck. **H2 confirmed.**
- Recent updates → daemon IS alive; problem is in close logic or candles.

### Q7 — Are there closed positions? Are post-mortems being processed? (H4)

```sql
-- All companies (run per company in a loop)
SELECT
  status,
  postmortem_status,
  count(*) AS n,
  min(closed_at) AS earliest_close,
  max(closed_at) AS latest_close
FROM public.tracked_positions
WHERE status = 'closed'
GROUP BY status, postmortem_status;
```

**Diagnose:**
- 0 closed → close pipeline is broken. Return to H1/H2.
- Closed exist + `postmortem_status='pending'` accumulating → PostMortemService daemon is dead.
- Closed exist + `postmortem_status='done'` → check Q8 to see if those post-mortems are placeholders.

### Q8 — Are the post-mortems real or placeholder? (H4 sub-case — confirms the STUB)

```sql
SELECT
  count(*)                                                            AS total,
  count(*) FILTER (WHERE lessons LIKE 'Awaiting full LLM wiring%')    AS placeholders,
  count(*) FILTER (WHERE (analysis_json->>'placeholder')::bool)       AS marked_placeholder,
  count(*) FILTER (WHERE cost_usd > 0)                                AS real_llm_calls
FROM public.position_postmortems;
```

**Expected with current code:** `placeholders == total`, `real_llm_calls == 0`.
**This is the documented STUB at [`postmortem_service.py:117-136`](shared/intelligence/postmortem_service.py:117).** Even when the close path works, the learning value is zero. **This is one of the user's "5 issues to fix" (D4).**

### Q9 — Are SL/TP values plausible? (H5)

```sql
SELECT
  count(*)                                                AS total_open,
  count(*) FILTER (WHERE stop_loss IS NULL)               AS no_sl,
  count(*) FILTER (WHERE take_profit IS NULL)             AS no_tp,
  count(*) FILTER (
    WHERE direction = 'long'
      AND stop_loss IS NOT NULL
      AND stop_loss >= entry_price)                       AS long_sl_above_entry,  -- nonsensical
  count(*) FILTER (
    WHERE direction = 'short'
      AND stop_loss IS NOT NULL
      AND stop_loss <= entry_price)                       AS short_sl_below_entry, -- nonsensical
  count(*) FILTER (
    WHERE direction = 'long'
      AND take_profit IS NOT NULL
      AND take_profit <= entry_price)                     AS long_tp_below_entry,  -- nonsensical
  count(*) FILTER (
    WHERE direction = 'short'
      AND take_profit IS NOT NULL
      AND take_profit >= entry_price)                     AS short_tp_above_entry  -- nonsensical
FROM public.tracked_positions
WHERE status IN ('open','partial_close');
```

Any non-zero in the four "nonsensical" columns → SL/TP set the wrong way → those positions can never close via candle hits. **H5 confirmed.**

---

## 5. Required Fixes (Once Hypothesis Confirmed)

These are **architecturally pre-approved**; Code mode can implement once Debug confirms which one applies.

### Fix F1 — Symbol normalisation at INSERT time (if H1 confirmed)

In [`interpretation_service.py:1056`](shared/intelligence/interpretation_service.py:1056) `create_tracked_position_from_interpretation`, before INSERT:
1. Look up `instruments` table; **fail loudly** if no match.
2. Persist the `instrument_id` (not just the symbol) onto `tracked_positions` (additive column `instrument_id BIGINT REFERENCES instruments(id)`).
3. Make `position_monitor.fetch_latest_price` JOIN by `instrument_id` directly. Eliminates symbol-drift entirely.

### Fix F2 — Stalled-position fallback closer (if H1 / H2 lasting > 24h)

Add a fallback to [`position_monitor.py`](shared/intelligence/position_monitor.py:1):
- If `created_at < NOW() - INTERVAL '7 days'` AND no candles available → `update_position_outcome(status='closed', outcome='expired')`. Logs warning, fires post-mortem with regime='unknown_no_data'. Prevents unbounded queue growth.

### Fix F3 — Wire the post-mortem LLM (D4 demand, kills H4)

Replace the stub at [`postmortem_service.py:117-136`](shared/intelligence/postmortem_service.py:117) with a real OpenRouter call:
- Use `os.getenv("SIGNAL_POSTMORTEM_MODEL", "openrouter/openai/gpt-4.1")` (already declared, never used).
- Build prompt from `entry_reason_trader`, `entry_reason_llm`, `exit_reason`, `outcome`, `time_in_trade_minutes`, `max_drawdown_pct`, `max_profit_pct`, plus a snapshot of last 100 1m candles around entry/exit.
- Parse structured JSON via `_extract_json_block` (already exists in [`shared/mcp/tools/intelligence.py:228`](shared/mcp/tools/intelligence.py:228)).
- Persist real `cost_usd` via `api_cost_log` writer (G5 contract).
- The lessons string MUST be ≥ 1 actionable sentence — feed Tier-2 memory (mem0 ScopedMemory for the agent that opened the trade).

### Fix F4 — Systemd unit for PositionMonitor + PostMortemService (if H2 confirmed)

Two new files:
- `systemd/tickles-position-monitor.service`
- `systemd/tickles-postmortem.service`

Both as `Type=simple, Restart=on-failure, RestartSec=10, EnvironmentFile=/opt/tickles/.env`. Both invoked via `python3 -m shared.intelligence.position_monitor` / `python3 -m shared.intelligence.postmortem_service`. Register both in [`shared/services/registry.py`](shared/services/registry.py:1) `_seed_known_services` (currently only some daemons are seeded — verify both are present).

### Fix F5 — INSERT-time SL/TP sanity check (if H5 confirmed)

In [`interpretation_service.py:1056`](shared/intelligence/interpretation_service.py:1056), reject inserts where:
- `direction='long'` and `stop_loss >= entry_price OR take_profit <= entry_price`
- `direction='short'` and `stop_loss <= entry_price OR take_profit >= entry_price`

Either reject the signal entirely (mark interpretation `consensus_signal='invalid_levels'`) or auto-flip and log a warning.

---

## 6. What Could Go Wrong (Self-Critique)

1. **Diagnostic queries fail because schema drift.** The `tracked_positions.metadata->>'signal_interpretation_id'` link in Q4 is assumed; if the actual link uses a different key, Q4 returns garbage. **Mitigation:** Debug mode should `\d public.tracked_positions` first and confirm column existence before running each query.
2. **Per-company tables.** `signal_interpretations` is per-company. Q3 and Q4 must be run per company. The active companies are listed in [`tickles_shared.companies`](shared/migration/tickles_shared_pg.sql:1) — query that first.
3. **Partial fixes mask root cause.** If we apply F2 (expiry closer) before identifying H1's true cause, we'll hide a candle-collector outage behind a stream of `expired` positions — losing all those potential learnings. **Mitigation:** F2 should ALWAYS log to a high-priority alert channel; never silent.
4. **F3 LLM costs balloon.** Wiring the real LLM means $X per close × N positions in the backlog. **Mitigation:** Add a `--catch-up-limit 10` flag for first run; check `api_cost_log` daily total before each tick.
5. **Phase Y dashboard depends on this data.** No closed positions → no Memory Feed content for Tier-2. We can mock/seed for v1 dashboard, but the demo will look empty until F1–F4 are deployed.

---

## 7. Implementation Order

**For Debug mode (immediate):**
1. Run Q1 → Q2 → Q5 → Q6 → Q7 → Q8 in sequence. Stop at first decisive signal.
2. Capture full output of all 9 queries in a single results doc (e.g., `.roo/handoffs/2026-05-02-position-pipeline-results.md`).
3. Post `journalctl -u tickles-position-monitor --since "2026-05-01" | tail -200` if the unit exists.
4. Identify which hypothesis (H1–H5) is confirmed.

**For Code mode (after Debug confirms):**
1. Apply the matching fix(es) F1–F5 from §5.
2. Re-run Q1, Q7 within 10 minutes — confirm new closed positions appear.
3. Smoke-test post-mortem real LLM via [`scripts/run_postmortem_service.py --once`](shared/scripts/run_postmortem_service.py:1).
4. Update [`CLAUDE.md`](CLAUDE.md:370) §"Daemons" table with both daemons + their systemd units.
5. Unblock Phase Y v2.

---

## 8. Resume Command

```
Run Phase X.0 diagnostic queries from shared/docs/PHASE_X0_POSITION_PIPELINE_DIAGNOSIS.md §4 (Q1–Q9 in order). Capture all outputs to .roo/handoffs/2026-05-02-position-pipeline-results.md. Identify which hypothesis (H1–H5) is confirmed and report. Do NOT apply fixes yet — Code mode will follow once root cause is confirmed.
```
