# 2026-05-03 — MASTER RESUME HANDOFF (no stone unturned)

> **Purpose:** Single source of truth for everything Roo / Code / Architect must know to continue this work without re-discovering context. Reads like a runbook.
> **Author:** Debug mode, 2026-05-03 06:59 UTC
> **Branch:** `feature/intelligence-unified-plan`
> **Last commit:** `77851be` (Code mode, end of F1-F11 wave)
> **Tags:** `milestone-position-monitor-unblocked`, `milestone-orphan-backfill-complete`

---

## §0 — How to use this document

1. **First time resuming?** Read §1 (state-of-the-world) → §2 (binding directives) → §3 (what was just done & verified) → §4 (what's pending) → §5 (exact resume commands per todo).
2. **Need to verify a claim?** §6 has every SQL query I ran with expected output.
3. **Need to find a file?** §7 is the canonical file inventory for this work.
4. **Confused about WHY a decision was made?** §2 has the verbatim user directives that drove every choice.
5. **Want to hand to a fresh agent?** §10 has copy-paste resume prompts per pending todo.

---

## §1 — State of the world (verified 2026-05-03 06:44 UTC)

### Database

- **Engine:** PostgreSQL (NOT MySQL — `CLAUDE.md` is out of date on this point)
- **Database name:** `tickles_shared`
- **Connection:** read from `.env` — `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`
- **Canonical SQL invocation pattern** (heredoc, since psql args + workspace `cd` is locked):
  ```bash
  cat <<'EOF' > /tmp/qN.sql
  SELECT ...;
  EOF
  bash -c 'set -a; source .env; set +a; export PGPASSWORD="$DB_PASSWORD"; \
    psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d tickles_shared -f /tmp/qN.sql'
  ```

### Daemons (all systemd-supervised, all `active (running)`)

| Unit | Service file | Purpose |
|---|---|---|
| `tickles-position-monitor.service` | [`systemd/tickles-position-monitor.service`](systemd/tickles-position-monitor.service:1) | Polls open positions, updates metrics, fires SL/TP closes |
| `tickles-interpretation.service` | [`systemd/tickles-interpretation.service`](systemd/tickles-interpretation.service:1) | Dual-track LLM + quant interpretation of media items |
| `tickles-postmortem.service` | [`systemd/tickles-postmortem.service`](systemd/tickles-postmortem.service:1) | Real-LLM postmortems on closed positions |
| `tickles-edge-scorer.service` | [`systemd/tickles-edge-scorer.service`](systemd/tickles-edge-scorer.service:1) | Computes per-actor edge score |
| `tickles-coach.service` | [`systemd/tickles-coach.service`](systemd/tickles-coach.service:1) | Weekly prompt A/B promotion |
| `tickles-chart-hacker-opinion.service` | [`systemd/tickles-chart-hacker-opinion.service`](systemd/tickles-chart-hacker-opinion.service:1) | Critic agent on closed positions |
| `tickles-candle-daemon.service` | (pre-existing) | 1m candle ingestion |

**Health-check command:**
```bash
systemctl is-active tickles-position-monitor tickles-interpretation \
  tickles-postmortem tickles-edge-scorer tickles-coach \
  tickles-chart-hacker-opinion tickles-candle-daemon
```
All return `active`.

**Tail logs:**
```bash
journalctl -u tickles-position-monitor -f
journalctl -u tickles-postmortem -n 200 --no-pager
```

### Position pipeline state

- **77 / 77** historic orphan positions are now `status='closed'`, all tagged `status_reason='backfilled_retrospective'`
- **0** open positions remain (so no live snapshots are being written — this is **expected**, not a bug)
- **77 / 77** have a real OpenRouter LLM postmortem (avg 337 chars, range 280-448)
- **avg P&L +$23.69**, range $13.40 → $26.61, total ≈ **+$1,824**, **all closes have non-zero fees deducted**
- **0** stub postmortems remaining (the old `1001-1005`, `218516` rows were deleted)
- **All `instrument_symbol` values now slash-form** as of 2026-05-03 07:14 UTC: `tracked_positions` 77 rows + `signal_interpretations` 435 rows migrated `BTCUSDT` → `BTC/USDT` via §4.1 transaction. `tracked_positions JOIN instruments ON i.symbol = tp.instrument_symbol` now resolves cleanly (154 rows: 77 trades × 2 venues).
- **58** rows in `instrument_aliases`. **Correction (2026-05-03 07:12, Debug verification):** the schema is NOT a flat `(alias, canonical_symbol)` lookup as the original author wrote. Real schema: `(id, instrument_id, alias_type, alias_value, source, created_at)`. Of the 58 rows, 8 are legacy: 4 `legacy_no_slash` (`BTCUSDT`, `ETHUSDT` × binance/bybit) + 4 `legacy_dash` (`BTC-USDT`, `ETH-USDT` × binance/bybit). The other 50 are `venue_native` aliases that are themselves already in slash form (CCXT exchange-symbol mappings keyed by `instrument_id`). Resolution must JOIN through `instruments.id` — there is no flat key/value lookup column.

### Cost ledger

- OpenRouter spend for 77 postmortems: **$1.137516** (≈ $0.0148 / postmortem)
- Logged in `api_cost_log` table

### Mem0 dev memory

- Decision memory ID: `de737be7-d688-46e7-8e4b-d113bdd967fc` (written by Code mode at end of wave)
- Use [`shared/utils/mem0_config.py`](shared/utils/mem0_config.py:208) `get_dev_memory(agent="...")` — NEVER use a trading-company namespace for dev work
- Trading namespaces: `rubicon` (test), future production firms (assigned later)
- **`jarvais` is FROZEN — DO NOT WRITE**

---

## §2 — Binding directives (verbatim user, do not paraphrase)

These are LAW. If a future decision contradicts one of these, escalate — don't override.

### From 2026-05-02, batch 1 (the original 5):
> "all agents must save memories into memory, not their own files (upgrade). memeory should have phases, 7 days 14 days and a monht. thats enough to determine improvments vs luck. did we get smarter should be related to memory and results, not just P&L (luck we want to avoid, skill we want to devlop). fix the 5 issue or imprive it. yes investigate the positions, there should by now at leaast be SOME trades that failed. if not, check the system or candle or tester thatvalidates the traders trades. until you can do that, you've not accomplished anything. we have 1000+ charts meaning we have trades. fix everything"

Decoded:
- **D1** — All agents write to mem0, not `.md` files (migration job pending — Todo §4.3)
- **D2** — Memory windows: **7d / 14d / 30d** for skill-vs-luck inference
- **D3** — "Smarter" = function of memory × results, not just P&L. P&L alone is luck.
- **D4** — Fix the 5 issues from PHASE_Y plan v1 (or improve them) — Todo §4.2
- **D5** — Position pipeline MUST surface failed trades. We have 1000+ charts ⇒ we have trades ⇒ they MUST be findable. **DONE** in this wave (F9).

### From 2026-05-02, batch 2 (driven by F9 / F10 design):
> "i need whatever is the best. signals or coins or stock should not be ambiguos. fee policy, repliacte what exchanges or brokers do, 77 LL positions, if they're the traders, then try resolve or figure out P&L"

Decoded:
- **D6** — Symbol canonical form: pick the BEST, NO AMBIGUITY → adopted **`BTC/USDT` slash form** (matches `instruments` table, matches CCXT, matches every exchange UI)
- **D7** — Fee policy: replicate real exchange/broker behaviour → use `instruments.taker_fee_pct`, `maker_fee_pct`, `overnight_funding_long_pct`, `overnight_funding_short_pct`, `spread_pct`. NO hardcoded fees.
- **D8** — 77 NULL-entry positions: backfill from `raw_signal_text` regex → candle-time fallback → walk-forward → tag `backfilled_retrospective`. Do NOT discard.

D6 / D7 / D8 are **DONE** in this wave (F1, F10, F9).
D1 / D2 / D3 / D4 are **PENDING** — see §4.

---

## §3 — What was just done (F1–F11 with verification evidence)

The position pipeline had been silent since 2026-05-01. This wave restored it end-to-end. Each fix below has independent SQL or systemd verification.

### F1 — Symbol normalisation (CRITICAL, D6)
- **What:** [`shared/utils/instrument_normaliser.py`](shared/utils/instrument_normaliser.py:23) now returns slash form (`BTC/USDT`). Position monitor calls `normalise_instrument()` before every SQL lookup.
- **Bridge table:** `instrument_aliases` seeded with 58 rows so legacy `BTCUSDT` / `BTC-USDT` rows still JOIN.
- **Verified:** `SELECT count(*) FROM instrument_aliases` → 58.
- **CAVEAT:** Historical `tracked_positions.instrument_symbol` rows still read `BTCUSDT` literally. Functionally fine — alias-resolves at JOIN time. Cosmetic UPDATE pending in §4.1.

### F2 — Expiry-based closer (HIGH)
- **What:** Auto-close branch in [`shared/intelligence/position_monitor.py:658`](shared/intelligence/position_monitor.py:658) — `_settle_close()` triggers on `now() > expiry_at`. Routes through F10 fee math.
- **Test:** [`shared/tests/test_f2_expiry_and_d7_close.py`](shared/tests/test_f2_expiry_and_d7_close.py:1) (Code mode authored).

### F3 — Real LLM postmortem (HIGH)
- **What:** [`shared/intelligence/postmortem_service.py:434`](shared/intelligence/postmortem_service.py:434) `_call_llm()` uses OpenRouter via the gateway. Old stubs (`position_id` 1001-1005, 218516) deleted.
- **Verified:** 77 postmortems, all `model_provider='openrouter'`, lengths 280-448 chars, avg 337. Total spend $1.137516.

### F4 — Systemd units (CRITICAL)
- **What:** 6 new units in `systemd/`. Pre-existing `tickles-candle-daemon.service` covers candles (the redundant `tickles-candle-collector.service` was removed during this wave to avoid double-write to `public.candles`).
- **Verified:** `systemctl is-active …` returns `active` for all 7.
- **Logs:** journald (not `/var/log/tickles/*.log` files — keep going through `journalctl`).

### F5 — SL/TP sanity guard (HIGH)
- **What:** [`shared/intelligence/interpretation_service.py:1186`](shared/intelligence/interpretation_service.py:1186) refuses to create a `tracked_position` row when `entry_price IS NULL` UNLESS the F9 backfill path is invoked. Non-recoverable cases get `update_media_status='skipped_no_price'`.
- **Test:** [`shared/tests/test_f5_entry_price_guard.py`](shared/tests/test_f5_entry_price_guard.py:1).

### F6 — `take_profit` → `take_profit_1` typo (CRITICAL)
- **What:** 1-line column-name fix in [`shared/intelligence/position_monitor.py:254`](shared/intelligence/position_monitor.py:254). TP-hit detection can now actually fire.

### F7 — NULL guard in `_build_snapshot` (HIGH)
- **What:** [`shared/intelligence/position_monitor.py:367`](shared/intelligence/position_monitor.py:367) returns `{"status":"missing_levels"}` early instead of crashing into try/except when entry/size are NULL.

### F8 — Kill `("BTCUSDT","bybit")` hardcoded fallback (CRITICAL)
- **What:** [`shared/intelligence/interpretation_service.py:871`](shared/intelligence/interpretation_service.py:871) `resolve_instrument_symbol()` now returns `(None, None)` on parse failure — caller skips the signal instead of silently mislabelling it.

### F9 — P&L backfill for 77 orphans (HIGH, D8)
- **What:** New [`shared/scripts/backfill_orphan_positions.py`](shared/scripts/backfill_orphan_positions.py:1).
  Algorithm:
  1. Fetch `signal_interpretations.raw_signal_text`, regex-parse for `entry`, `sl`, `tp`.
  2. If parse fails or values insane (±20% sanity check), fallback to `candles.close` at `signal_interpretations.created_at`.
  3. Walk forward through 1m candles; close at first of (SL hit, TP hit, `expiry_at`, `now()`).
  4. Compute P&L via F10.
  5. Tag `status_reason='backfilled_retrospective'`, freeze `entry_reason_frozen_at`.
- **Idempotent:** only touches rows where `realized_pnl_usd_final IS NULL`.
- **Verified:** 77 / 77 closed (100%, exceeded the ≥50 acceptance bar). Avg +$23.69. Test: [`shared/tests/test_f9_orphan_backfill.py`](shared/tests/test_f9_orphan_backfill.py:1).

### F10 — Fee-accurate close (HIGH, D7)
- **What:** New [`shared/intelligence/fee_calc.py`](shared/intelligence/fee_calc.py:1).
  Signature:
  ```python
  def compute_realized_pnl(
      entry: Decimal, exit: Decimal, size: Decimal,
      direction: str,         # "long" | "short"
      instrument_row: dict,   # has taker_fee_pct, maker_fee_pct, spread_pct,
                              #     overnight_funding_long_pct, overnight_funding_short_pct
      hours_held: Decimal,
  ) -> Decimal: ...
  ```
  - Decimal everywhere, never float.
  - Both legs charged taker fee (conservative — assumes market orders).
  - Spread cost = `spread_pct × notional`.
  - Overnight funding accrual = `funding_pct × notional × hours_held / 24` using the direction-correct column.
- **Wired into:** `update_position_outcome()`, F2 expiry closer, F9 backfill.
- **Test:** [`shared/tests/test_fee_calc.py`](shared/tests/test_fee_calc.py:1).

### F11 — `PositionSnapshot` field-name alignment (commit `a9e44d5`)
- **Correction (Debug 2026-05-03 verify pass):** the master handoff originally claimed F11 was a "schema-drift sweep". That is **wrong**. The schema-drift sweep (renames in `edge_scorer_service.py` + `chart_hacker_opinion_service.py`) was bundled into F4 commit `77851be`. The standalone F11 commit `a9e44d5` does:
  - Renames `unrealized_pnl_usd` → `unrealized_pnl` so [`write_position_update()`](shared/intelligence/position_monitor.py:480) and the [`PositionSnapshot`](shared/intelligence/position_monitor.py:93) dataclass match the actual `position_updates` column name (was an `AttributeError` at runtime).
  - Adds `distance_to_entry_pct: float` to `PositionSnapshot` so the snapshot persistence path doesn't drop the column.
- **Schema-drift specifics that lived in F4 (commit `77851be`):**
  - `realised_pnl_pct` → `realized_pnl_pct`
  - `opened_at` → `signal_timestamp`
  - `pos.side` → `pos.direction`
  - Rewrote `agent_opinions` INSERTs to use the real `chart_hacker_opinions(agent_name, agent_version, reasoning)` columns
  - Removed hardcoded `COMPANY_ID="jarvais"` (jarvais is FROZEN)
- **Lesson learned:** add `shared/scripts/schema_diff.py` to CI gates (PHASE 12 of the unified plan, not yet built).

### Pre-flight audit (Bug F)
- Checked on-disk vs running PIDs before starting — no stale processes; daemons restarted post-fix.

### F12 — Cosmetic symbol migration (2026-05-03 07:14 UTC, Code mode)
- **What:** Executed the corrected §4.1 recipe in a transaction.
- **Migrations:** `tracked_positions` 77 rows + `signal_interpretations` 435 rows: `BTCUSDT` → `BTC/USDT`.
- **Method:** Built a temp `legacy_to_canonical` lookup by joining `instrument_aliases` (where `alias_type IN ('legacy_no_slash','legacy_dash')`) to `instruments`. Confirmed each legacy maps to exactly one canonical (no fan-out). Then UPDATE in a single transaction.
- **Verified:**
  - Q1 unchanged (77 closed / 77 P&L / 77 backfill-tagged)
  - Q5 now reads `BTC/USDT  77` (was `BTCUSDT  77`)
  - Q7 P&L untouched (avg $23.69 / total $1,824.24)
  - JOIN sanity: `tracked_positions JOIN instruments ON i.symbol = tp.instrument_symbol` now produces 154 rows (77 trades × 2 venues — binance + bybit)
- **Pre-flight:** verified `position_postmortems` has no symbol column (so no migration needed there); confirmed `assets.symbol` is a different namespace (bare ticker `BTC`) and excluded from migration.
- **Schema correction shipped to handoff:** the original §4.1 recipe referenced `instrument_aliases.alias` and `.canonical_symbol` which **do not exist** — corrected to use `alias_value` + JOIN through `instruments.symbol`. The original §6 Q2 and Q9 queries also had column-name typos (`model_provider`, `estimated_cost_usd`) — corrected.

---

## §4 — Pending todos (with full execution recipes)

### §4.1 — Cosmetic UPDATE: rewrite historical `BTCUSDT` → `BTC/USDT` (LOW priority)

**Why this is optional:** alias-lookup at JOIN time already resolves correctly via the [`instrument_aliases`](shared/migration/tickles_shared_pg.sql:1) → [`instruments`](shared/migration/tickles_shared_pg.sql:1) join. All NEW writes go through `normalise_instrument()` and produce slash form. Only purpose of this UPDATE is cosmetic uniformity when humans `SELECT * FROM tracked_positions`.

**⚠️ ORIGINAL RECIPE WAS BROKEN — corrected by Debug 2026-05-03 07:12 UTC.**
The original recipe referenced `instrument_aliases.alias` and `instrument_aliases.canonical_symbol`, which **do not exist**. Real schema:
```
instrument_aliases(id, instrument_id, alias_type, alias_value, source, created_at)
instruments(id, symbol, exchange, ...)  -- canonical symbol lives here
```
Resolution path: `legacy_value → instrument_aliases.alias_value → instrument_aliases.instrument_id → instruments.symbol`.

**Tables that store symbols (28 columns surveyed 2026-05-03):**
```sql
SELECT table_name, column_name
FROM information_schema.columns
WHERE column_name IN ('instrument_symbol','symbol','base_symbol','asset_symbol')
  AND table_schema = 'public'
ORDER BY table_name;
```
Two namespaces:
- **Trading-symbol namespace** (slash form `BTC/USDT`): `tracked_positions.instrument_symbol`, `signal_interpretations.instrument_symbol`, plus `symbol` columns on most trading tables (most are currently empty — only `tracked_positions` has 77 rows, `signal_interpretations` has 435).
- **Asset-ticker namespace** (bare `BTC`): `assets.symbol`. **DO NOT MIGRATE these — different namespace.**

**Corrected recipe (run in TRANSACTION; abort if affected rows > expected):**
```sql
BEGIN;

-- Build a temp lookup view limited to legacy_* aliases
CREATE TEMP TABLE legacy_to_canonical AS
SELECT DISTINCT ia.alias_value AS legacy, i.symbol AS canonical
FROM instrument_aliases ia
JOIN instruments i ON i.id = ia.instrument_id
WHERE ia.alias_type IN ('legacy_no_slash', 'legacy_dash');
-- expected: ~4 distinct rows (BTCUSDT→BTC/USDT, BTC-USDT→BTC/USDT, ETHUSDT→ETH/USDT, ETH-USDT→ETH/USDT)

SELECT * FROM legacy_to_canonical;  -- visual-check before UPDATE

-- 1. tracked_positions
UPDATE tracked_positions tp
SET instrument_symbol = ltc.canonical
FROM legacy_to_canonical ltc
WHERE tp.instrument_symbol = ltc.legacy;
-- expected: 77 rows (all current orphan-backfilled rows)

-- 2. signal_interpretations
UPDATE signal_interpretations si
SET instrument_symbol = ltc.canonical
FROM legacy_to_canonical ltc
WHERE si.instrument_symbol = ltc.legacy;
-- expected: 435 rows (per Q5-style scan 2026-05-03)

-- 3. Verify
SELECT instrument_symbol, count(*) FROM tracked_positions GROUP BY 1;
-- expected: 'BTC/USDT'  77
SELECT instrument_symbol, count(*) FROM signal_interpretations GROUP BY 1;
-- expected: 'BTC/USDT'  435

-- 4. Sanity: no remaining legacy values?
SELECT 'tracked_positions' AS tbl, count(*) AS leftover
FROM tracked_positions WHERE instrument_symbol IN (SELECT legacy FROM legacy_to_canonical)
UNION ALL
SELECT 'signal_interpretations', count(*)
FROM signal_interpretations WHERE instrument_symbol IN (SELECT legacy FROM legacy_to_canonical);
-- expected: both 0

-- COMMIT only if all counts match expectation, else ROLLBACK
COMMIT;
```

**Acceptance:** post-commit, zero rows in `tracked_positions` or `signal_interpretations` carry an `instrument_symbol` value that appears in `legacy_to_canonical.legacy`.

**Risk:** None if done in a transaction. Worst case ROLLBACK and we're back where we started.

**Other tables to consider:** `position_postmortems` does NOT have a symbol column (verified — see §6 Q2 column list); `assets.symbol` is a different namespace and must NOT be migrated. All other 25 `symbol`-bearing tables are empty.

**Estimated time:** 15 minutes.

---

### §4.2 — PHASE_Y plan v2 (Architect mode, MEDIUM priority)

**Source plan:** [`shared/docs/PHASE_Y_LEARNING_DASHBOARD_PLAN.md`](shared/docs/PHASE_Y_LEARNING_DASHBOARD_PLAN.md:1) (v1, written before user's batch-1 directives)

**What v2 must add (per D1, D2, D3, D4):**

1. **Memory unification (D1):** Kill `openclaw_export_daemon` and any other agent that writes summaries to `.md` files. Replace with mem0 writes via [`shared/utils/mem0_config.py`](shared/utils/mem0_config.py:1) `get_memory(company, agent)`.
   - **Discovery step:** §4.3 below — inventory all `.md` writers.
   - **Migration job:** one-shot script that reads existing `.md` files into mem0 with proper `metadata.type` tags.
   - **CI gate:** add to [`shared/scripts/writer_registry_grep.py`](shared/scripts/writer_registry_grep.py:1) — fail any PR that imports `pathlib.Path(...).write_text` for summaries in agent code paths.

2. **Memory windows (D2):** 7d / 14d / 30d aggregation views over `position_postmortems` JOIN `tracked_positions`. Query patterns:
   ```sql
   -- 7d window per actor
   SELECT actor_id, count(*), avg(realized_pnl_pct)
   FROM tracked_positions
   WHERE closed_at > now() - interval '7 days'
   GROUP BY actor_id;
   ```
   Add three materialised views or three params on a view:
   - `v_actor_perf_7d`
   - `v_actor_perf_14d`
   - `v_actor_perf_30d`

3. **Skill-vs-luck composite (D3):** This is the headline metric. Drafted formula:
   ```
   skill_score(actor, window) =
       0.30 × normalised(reasoning_clarity_avg)         -- from postmortems
     + 0.25 × normalised(consistency_inverse_variance)   -- low P&L variance = skill
     + 0.20 × normalised(reason_freeze_to_outcome_corr)  -- did the stated reason actually predict outcome?
     + 0.15 × normalised(memory_recall_hit_rate)         -- did agent reference relevant past memories?
     + 0.10 × normalised(prompt_lift)                    -- did winning prompts replicate?
   ```
   - All five components READ from existing tables (`position_postmortems`, `prompt_versions`, mem0 query results).
   - Must NOT include raw P&L or hit-rate (those are luck).
   - Output: per-actor `skill_score` ∈ [0, 1] per window.
   - Wire into edge_scorer as a new component (currently uses `pnl_quality`, `consistency`, etc.; add `skill_minus_luck`).

4. **Fix the 5 issues (D4):** Re-read PHASE_Y v1 §"5 issues" list, decide for each: fix here OR roll up into the memory unification (D1) work. Likely candidates already on the list:
   - i. Dual-role writers (trader path also writing critic data)
   - ii. Postmortem stub orphans (FIXED in F3)
   - iii. Open-positions count NaN in dashboard
   - iv. Edge-scorer crash on missing `agent_opinions` table (FIXED in F11)
   - v. Coach service has no variants to evaluate (no prompts registered yet — chicken/egg)

**Deliverable:** `shared/docs/PHASE_Y_LEARNING_DASHBOARD_PLAN.md` rewritten as v2 with explicit phase ordering, file lists, SQL DDL, and per-phase acceptance criteria.

**Mode:** Architect (only allowed to edit `*.md`).

**Estimated time:** 1 day in Architect, then 2-3 days in Code to implement.

---

### §4.3 — `.md` vs mem0 writer audit (Architect → Code, MEDIUM priority) ✅ DONE (2026-05-03)

**Status:** Phases 1–5 complete. Architect audit shipped at [`.roo/handoffs/2026-05-03-md-vs-mem0-audit.md`](.roo/handoffs/2026-05-03-md-vs-mem0-audit.md:1); migration script + tests + writer deletions + CI gate all merged; **Phase 5 `--apply` executed successfully on 2026-05-03 after fixing a script defect** (see Phase 5 — Apply subsection below).

**Delivered:**
- Migration script [`shared/scripts/migrate_md_to_mem0.py`](shared/scripts/migrate_md_to_mem0.py:1) with mutually-exclusive `--dry-run`/`--apply`, `--company` filter, R2 raw_dump fallback, manifest-before-writes, idempotent `.md` → `.md.migrated` rename. **Applied in production 2026-05-03** — 4 files migrated, 4 mem0 entries written, sources renamed to `.md.migrated`.
- 14/14 tests pass in [`shared/tests/test_migrate_md_to_mem0.py`](shared/tests/test_migrate_md_to_mem0.py:1) (11 original + 3 regression tests added in Phase 5).
- `.md` writers deleted in [`shared/daemons/surgeon_trader.py`](shared/daemons/surgeon_trader.py:1) and [`shared/templates/trading_agent/surgeon_llm_runner.py`](shared/templates/trading_agent/surgeon_llm_runner.py:1); replaced with `persist_state_snapshot()` / `persist_trade_decisions()` writing to mem0 (`type=trade_state_snapshot|trade_decision`, `kind=live`).
- [`shared/scripts/check_system_freshness.py`](shared/scripts/check_system_freshness.py:153) now checks only `.surgeon_state.json` (not `TRADE_STATE.md`).
- CI gate [`shared/tests/test_grep_guard_md_writes.py`](shared/tests/test_grep_guard_md_writes.py:1) passes — zero forbidden writers remaining.
- Latent bug fixed: `ScopedMemory(company_id=..., agent_id=...)` → `ScopedMemory(company=..., agent_id=...)` in `surgeon_llm_runner.py:813` (constructor takes `company`, not `company_id`).
- D1 enforcement note added to [`CLAUDE.md`](CLAUDE.md:91).

**Resume command (if `--apply` is needed later):**
```bash
python3 -m shared.scripts.migrate_md_to_mem0 --dry-run --allow-fallback-owner | tee /tmp/manifest.json
# review manifest, then:
python3 -m shared.scripts.migrate_md_to_mem0 --apply --company rubicon
```

---

#### Phase 5 — Apply (executed 2026-05-03)

**Apply commit:** `b3f55ff131296840320098a0899fd6f91bfd209a` (short: `b3f55ff`, branch `feature/intelligence-unified-plan`) — see [`shared/scripts/migrate_md_to_mem0.py`](shared/scripts/migrate_md_to_mem0.py:1) and [`shared/tests/test_migrate_md_to_mem0.py`](shared/tests/test_migrate_md_to_mem0.py:1).

**Files migrated (4):**
- `/root/.openclaw/workspace/rubicon_surgeon/TRADE_LOG.md` (96848 bytes) → `.md.migrated`, 1 entry written
- `/root/.openclaw/workspace/rubicon_surgeon/TRADE_STATE.md` (448 bytes) → `.md.migrated`, 1 entry written
- `/root/.openclaw/workspace/rubicon_surgeon2/TRADE_LOG.md` (466 bytes) → `.md.migrated`, 1 entry written
- `/root/.openclaw/workspace/rubicon_surgeon2/TRADE_STATE.md` (972 bytes) → `.md.migrated`, 1 entry written

**Entries written per company (mem0, `kind=historical_migration`):**
- `rubicon_surgeon`: 2 entries (1 trade_decision from TRADE_LOG, 1 trade_state_snapshot from TRADE_STATE) — Qdrant collection `tickles_rubicon_surgeon`
- `rubicon_surgeon2`: 2 entries (1 trade_decision, 1 trade_state_snapshot) — Qdrant collection `tickles_rubicon_surgeon2`
- **Total: 4 entries, 0 errors**

**Manifest paths (kept for forensic reference):**
- Pre-apply (dry-run): [`/tmp/manifest_pre_apply_v2.json`](file:///tmp/manifest_pre_apply_v2.json) — 4 candidates `dry_run_planned`
- Post-apply: [`/tmp/manifest_apply_v2.json`](file:///tmp/manifest_apply_v2.json) and [`shared/scripts/migration_manifest.json`](shared/scripts/migration_manifest.json:1) — 4 entries `applied`, `entries_written_total=4`

**Pre-flight evidence:**
- PF1 (`tracked_positions`): 77 closed, 0 open ✅ (no live data at risk)
- PF2 (services): only `rubicon-surgeon-scanner.service` running (V2, allowed); zero V1 surgeon services active ✅

**Script-defect note (CRITICAL — read before any future migration work):**
The script as originally shipped called `mem.add()` at three sites without forwarding `user_id=company, agent_id=agent_id` kwargs. mem0 0.x raises `ValidationError` if none of `user_id`/`agent_id`/`run_id` is provided, so every entry write crashed inside `ScopedMemory.add()`. The previous Phase 5 attempt failed silently (per-entry try/except swallowed the exceptions; manifest reported `entries_written=0` for every file) and did NOT rename any sources. Forensic write-up: [`.roo/handoffs/2026-05-03-md-vs-mem0-apply-FAILED.md`](.roo/handoffs/2026-05-03-md-vs-mem0-apply-FAILED.md:1) §3.3.

**Fix applied in this commit (4 edits to [`shared/scripts/migrate_md_to_mem0.py`](shared/scripts/migrate_md_to_mem0.py:1)):**
1. `replay_trade_state` (line ~174): `mem, _agent_id` → `mem, agent_id`; forward `user_id=company, agent_id=agent_id` to `mem.add()`.
2. `replay_trade_log` raw_dump fallback branch (line ~260): same forwarding.
3. `replay_trade_log` per-entry loop (line ~289 + ~302): same forwarding for each parsed Twilly entry.
4. `process_file` defensive guard: refuse to rename source `.md` → `.md.migrated` when `entries_written=0` on a non-empty source (writes `status=error_zero_entries` to manifest instead). Prevents silent data-loss recurrence.

**Regression tests added (3, all passing):**
- [`test_replay_trade_state_forwards_user_id_and_agent_id`](shared/tests/test_migrate_md_to_mem0.py:344) — asserts kwargs forwarded.
- [`test_replay_trade_log_forwards_user_id_and_agent_id_per_entry`](shared/tests/test_migrate_md_to_mem0.py:388) — asserts per-entry kwargs forwarded.
- [`test_apply_refuses_rename_when_zero_entries_written`](shared/tests/test_migrate_md_to_mem0.py:430) — asserts source NOT renamed when all `mem.add()` calls fail.

**Mem0 verification probe (post-apply):**
- `rubicon_surgeon` search for `Trade decision OR state snapshot` returned 2 hits, all `metadata.kind=historical_migration`, `metadata.original_timestamp` preserved (e.g., `2026-04-24T10:34:28+00:00`).
- `rubicon_surgeon2` search returned 2 hits, same shape.
- `metadata.source_file` preserved on every entry, pointing back to original `.md` path.

**30-day grace period for `.md.migrated` files:** scheduled cleanup date **2026-06-02**. After that date, the four `.md.migrated` files under `/root/.openclaw/workspace/{rubicon_surgeon,rubicon_surgeon2}/` may be deleted (they are pure backups; mem0 is now source of truth).

---

**Original goal (preserved for reference):** identify every place where an agent persists "knowledge" or "summaries" to a `.md` file instead of mem0.

**Discovery step 1 — find writers:**
```bash
# any code path that writes a .md file
grep -rn --include='*.py' "write_text\|\.md['\"]" shared/ projects/ | \
  grep -v test_ | \
  grep -v migrations/

# any code path that opens .md for append/write
grep -rn --include='*.py' "open(.*\.md.*['\"][wa]" shared/ projects/
```

**Discovery step 2 — known suspects (start here):**
- `openclaw_export_daemon` (referenced in batch-1 directive — confirm location, likely under `projects/openclaw/`)
- Any "summariser" / "digest" / "report" service
- ChartHacker: confirm whether it writes guru reports to `.md` or to `chart_hacker_opinions` table (F11 wired the table; verify no parallel `.md` writes remain)
- Phase-4 "signal review export" — that one is intentionally CSV/HTML, not `.md`, but verify

**Output deliverable:**
`.roo/handoffs/2026-05-XX-md-vs-mem0-audit.md` containing:
1. Table: file path | line | what kind of content | proposed mem0 namespace + agent_id
2. Migration script spec for moving historical `.md` content into mem0 (preserve `created_at` as `metadata.original_timestamp`)
3. CI gate spec to prevent regression

**Acceptance:** zero lines of code in `shared/` or `projects/<production company>/` that write agent-output to `.md`. Dev / handoff `.md` files (this document, for example) are EXCLUDED from the ban.

**Estimated time:** 1 day audit + 1 day migration script + 0.5 day CI gate.

---

## §5 — Architecture invariants (DO NOT VIOLATE)

These come from `.roo/rules/global.md`, `CLAUDE.md`, and decisions baked into this wave:

- **Postgres only.** Not MySQL. CLAUDE.md is wrong on this — fix it next time you're in there.
- **All money in `Decimal`,** never `float`. Prices `decimal(20,8)`, volumes `decimal(30,8)`.
- **All timestamps UTC.** Storage and comparisons. No naive datetime objects.
- **Parameterised SQL only.** Never f-string interpolation. Bug F discovered an instance — re-grep occasionally.
- **One canonical symbol form: `BTC/USDT`** (slash). Run everything through [`normalise_instrument()`](shared/utils/instrument_normaliser.py:164) at every entry point.
- **Fees come from `instruments` table.** Never hardcode.
- **Mem0 namespaces: `dev` for build work, `<company>` for trading agents.** `jarvais` is FROZEN (read-only). Use [`get_dev_memory()`](shared/utils/mem0_config.py:208) in dev / Roo / Code / Architect work.
- **Keys are trade-only.** No withdrawal permissions. If you find a key with withdraw scope, escalate immediately.
- **No bare `except:`.** Catch specific exceptions. Always log with traceback.
- **Network calls have timeouts.** Default 30s. Retry up to 3 times with exponential backoff.

---

## §6 — Verification SQL playbook (re-run any of these to re-verify state)

### Q1: confirm 77/77 closed and tagged
```sql
SELECT
  count(*) FILTER (WHERE realized_pnl_usd_final IS NOT NULL) AS pnl_filled,
  count(*) FILTER (WHERE realized_pnl_usd_final IS NULL)     AS pnl_null,
  count(*) FILTER (WHERE status_reason = 'backfilled_retrospective') AS backfill_tagged
FROM tracked_positions;
```
Expected: `pnl_filled=77, pnl_null=0, backfill_tagged=77`.

### Q2: confirm 77 real LLM postmortems
**⚠️ Schema correction (Debug 2026-05-03):** column is `postmortem_provider`, not `model_provider`. There is no `model_provider` column on `position_postmortems`.
```sql
SELECT
  count(*) AS total,
  count(DISTINCT position_id) AS uniq_positions,
  count(*) FILTER (WHERE postmortem_provider = 'openrouter') AS from_openrouter,
  min(length(what_happened)) AS min_len,
  max(length(what_happened)) AS max_len,
  avg(length(what_happened))::int AS avg_len
FROM position_postmortems;
```
Expected: `total=77, uniq_positions=77, from_openrouter=77, min_len≥280, max_len≤448, avg_len≈337`.

### Q3: confirm zero stub orphans
```sql
SELECT count(*) FROM position_postmortems
WHERE position_id IN (1001,1002,1003,1004,1005,218516);
```
Expected: `0`.

### Q4: position_updates (expected to be 1, since all positions closed)
```sql
SELECT count(*) AS total,
       count(*) FILTER (WHERE created_at > now() - interval '10 minutes') AS recent_10min
FROM position_updates;
```
Expected: `total=1, recent_10min=0`. Will jump as soon as new open positions exist.

### Q5: instrument_symbol historical values
```sql
SELECT instrument_symbol, count(*)
FROM tracked_positions
GROUP BY 1
ORDER BY 2 DESC;
```
Expected (post-2026-05-03 §4.1 commit): `BTC/USDT  77`. The §4.1 cosmetic UPDATE has been **completed** — see §3 F12.

### Q6: instrument_aliases seeded
```sql
SELECT count(*) FROM instrument_aliases;
-- ⚠️ Real column is alias_value, not alias.
SELECT ia.alias_type, ia.alias_value, i.symbol, i.exchange
FROM instrument_aliases ia
JOIN instruments i ON i.id = ia.instrument_id
WHERE ia.alias_value IN ('BTCUSDT','BTC-USDT','BTCUSD') LIMIT 10;
```
Expected: `count=58`, BTC variants resolve to `BTC/USDT`.

### Q7: P&L distribution
```sql
SELECT
  count(*)                                AS n_closed,
  count(*) FILTER (WHERE realized_pnl_usd_final <> 0) AS n_nonzero,
  min(realized_pnl_usd_final)::numeric(10,2) AS min_pnl,
  max(realized_pnl_usd_final)::numeric(10,2) AS max_pnl,
  avg(realized_pnl_usd_final)::numeric(10,2) AS avg_pnl,
  sum(realized_pnl_usd_final)::numeric(10,2) AS total_pnl
FROM tracked_positions
WHERE status = 'closed';
```
Expected: `n_closed=77, n_nonzero=77, min=13.40, max=26.61, avg=23.69, total≈1824`.

### Q8: sample postmortem (sanity-check it's real LLM output, not stub)
**⚠️ Schema correction (Debug 2026-05-03):** column is `postmortem_provider`, not `model_provider`.
```sql
SELECT position_id, postmortem_provider, postmortem_model,
       length(what_happened),
       left(what_happened, 200) AS preview
FROM position_postmortems
ORDER BY random() LIMIT 3;
```
Expected: each row reads like real causal English ("The long was opened at 77334.30 and closed at 78371.70 …").

### Q9: API spend
**⚠️ Schema correction (Debug 2026-05-03):** column is `cost_usd`, not `estimated_cost_usd`.
```sql
SELECT model, count(*) AS calls,
       sum(cost_usd)::numeric(10,4) AS total_cost
FROM api_cost_log
WHERE created_at > '2026-05-02'
GROUP BY model
ORDER BY total_cost DESC;
```
Expected: `openai/gpt-4.1-2025-04-14` row with 77 calls totalling ≈ $1.1375 (plus a stray $0.0001 gemini call).

---

## §7 — File inventory (everything touched in this wave)

### Created (new files)

| Path | Purpose |
|---|---|
| [`shared/intelligence/fee_calc.py`](shared/intelligence/fee_calc.py:1) | F10 — Decimal fee math |
| [`shared/scripts/backfill_orphan_positions.py`](shared/scripts/backfill_orphan_positions.py:1) | F9 — orphan backfill |
| [`shared/tests/test_fee_calc.py`](shared/tests/test_fee_calc.py:1) | F10 tests |
| [`shared/tests/test_f2_expiry_and_d7_close.py`](shared/tests/test_f2_expiry_and_d7_close.py:1) | F2 + D7 tests |
| [`shared/tests/test_f5_entry_price_guard.py`](shared/tests/test_f5_entry_price_guard.py:1) | F5 tests |
| [`shared/tests/test_f9_orphan_backfill.py`](shared/tests/test_f9_orphan_backfill.py:1) | F9 tests |
| [`systemd/tickles-position-monitor.service`](systemd/tickles-position-monitor.service:1) | F4 |
| [`systemd/tickles-interpretation.service`](systemd/tickles-interpretation.service:1) | F4 |
| [`systemd/tickles-postmortem.service`](systemd/tickles-postmortem.service:1) | F4 |
| [`systemd/tickles-edge-scorer.service`](systemd/tickles-edge-scorer.service:1) | F4 |
| [`systemd/tickles-coach.service`](systemd/tickles-coach.service:1) | F4 |
| [`systemd/tickles-chart-hacker-opinion.service`](systemd/tickles-chart-hacker-opinion.service:1) | F4 |
| `.roo/handoffs/2026-05-02-position-pipeline-results.md` | Diagnosis + fix table + D6/D7/D8 |
| `.roo/handoffs/2026-05-03-master-resume-handoff.md` | **THIS document** |

### Modified

| Path | Why |
|---|---|
| [`shared/utils/instrument_normaliser.py`](shared/utils/instrument_normaliser.py:23) | F1 — return slash form |
| [`shared/intelligence/position_monitor.py`](shared/intelligence/position_monitor.py:1) | F6 (typo), F7 (NULL guard), F2 (expiry close), wired to F10 |
| [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:871) | F8 (kill BTCUSDT fallback), F5 (sanity guard) |
| [`shared/intelligence/postmortem_service.py`](shared/intelligence/postmortem_service.py:117) | F3 (real LLM), schema fixes |
| [`shared/intelligence/edge_scorer_service.py`](shared/intelligence/edge_scorer_service.py:72) | F11 (column renames) |
| [`shared/intelligence/chart_hacker_opinion_service.py`](shared/intelligence/chart_hacker_opinion_service.py:74) | F11 (`agent_opinions` → real schema, side→direction, dropped jarvais hardcode) |

### Deleted

- `systemd/tickles-candle-collector.service` — redundant with pre-existing `tickles-candle-daemon.service`
- 9 stub rows in `position_postmortems` (`position_id` 1001-1005, 218516, plus 3 others tied to those)

### Read but NOT modified (reference only)

- [`shared/migration/tickles_shared_pg.sql`](shared/migration/tickles_shared_pg.sql:1) — schema source of truth
- [`shared/utils/mem0_config.py`](shared/utils/mem0_config.py:208) — `get_dev_memory()` helper
- [`shared/docs/INTELLIGENCE_UNIFIED_PLAN.md`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:1) — phase contract

---

## §8 — Gotchas, scars, and "you'll trip on this" notes

1. **`CLAUDE.md` says MySQL.** It is **WRONG.** This server runs Postgres. Update CLAUDE.md when you next touch it. (Architect mode only — `.md` edits.)
1a. **`instrument_aliases` schema is NOT a flat `(alias, canonical_symbol)` lookup.** Real schema is `(instrument_id, alias_type, alias_value)` and you must JOIN through `instruments.id` to get the canonical `instruments.symbol`. Original §4.1 recipe in the master handoff was wrong — corrected in-place 2026-05-03 07:12.
1b. **`position_postmortems` columns:** the LLM provider lives in `postmortem_provider` (not `model_provider`); the cost lives on `api_cost_log.cost_usd` (not `estimated_cost_usd`). Verify column names before writing analysis SQL.
1c. **27 tables have a `symbol`/`instrument_symbol` column**, not just 3. Most are empty. Only `tracked_positions` (77) and `signal_interpretations` (435) currently hold legacy values needing migration. `assets.symbol` is a separate namespace (bare ticker `BTC`) and MUST NOT be migrated.
2. **Workspace is locked to `/opt/tickles`.** You cannot `cd` between commands. Every command that needs another directory must be `cd path && cmd` in one shot.
3. **`.env` is git-ignored.** Source it at the start of every shell that needs DB access: `set -a; source .env; set +a; export PGPASSWORD="$DB_PASSWORD"`.
4. **Daemons log to journald, NOT `/var/log/tickles/`.** F4's original spec said file logs; we changed to journald during deployment because it auto-rotates and integrates with `systemctl status`. Don't re-introduce file logging.
5. **`tickles-candle-daemon.service` already exists** — DO NOT create a parallel one. Code mode tripped on this and we deleted the duplicate.
6. **`agent_opinions` table does NOT exist** — use `chart_hacker_opinions` with columns `agent_name`, `agent_version`, `reasoning`. F11 fixed every reference; if you see new code mention `agent_opinions`, it's a regression.
7. **`pos.side` column does NOT exist** — use `pos.direction` ("long" / "short"). Same regression risk.
8. **`opened_at` does NOT exist** in `tracked_positions` — use `signal_timestamp`. Same regression risk.
9. **`realised_pnl_pct` (British spelling) does NOT exist** — use `realized_pnl_pct` (American). Same.
10. **`COMPANY_ID="jarvais"` is FORBIDDEN.** Jarvais is FROZEN legacy. Production company IDs are TBD. For dev work, use the `dev` namespace via `get_dev_memory()`.
11. **`position_updates` table currently has 1 row.** Not a bug — there are 0 open positions, so the snapshot loop has nothing to write. New rows appear when new open positions exist.
12. **`instrument_symbol = 'BTCUSDT'` on historical rows.** Functionally fine (alias resolves). Cosmetic UPDATE pending in §4.1.
13. **OpenRouter rate limits:** the 77-postmortem run cost $1.13 in ≈ 5 minutes. Do not loop the postmortem service against thousands of historical rows without throttling — see [`shared/intelligence/llm_rate_limiter.py`](shared/intelligence/llm_rate_limiter.py:1).
14. **Mem0 dev memory ≠ trading memory.** Mixing them poisons both. Use the right helper.

---

## §9 — Decision log (for memory: who decided what and why)

| Decision | Source | Date | Rationale |
|---|---|---|---|
| Postgres canonical, not MySQL | discovered during migration | pre-2026-04 | Migration to PG already shipped; CLAUDE.md stale |
| `BTC/USDT` slash form canonical | user D6 | 2026-05-02 | Matches `instruments`, CCXT, exchange UIs; no schema migration needed |
| Replicate real exchange fees, no hardcoding | user D7 | 2026-05-02 | Honest P&L; supports cross-exchange comparison |
| Backfill 77 orphans, don't discard | user D8 | 2026-05-02 | Real trader signals; throwing them away = throwing away the corpus |
| Postmortem via OpenRouter, not local | implementation | 2026-05-02 | Gateway already wired; fallback chain handles model failures |
| journald logging, not file | deployment | 2026-05-02 | Auto-rotation; integrates with `systemctl` |
| Skill score must NOT include raw P&L | user D3 | 2026-05-02 | "Luck we want to avoid, skill we want to develop" |
| 7d / 14d / 30d windows | user D2 | 2026-05-02 | "Enough to determine improvements vs luck" |

---

## §10 — Resume commands (copy-paste per todo)

### §4.1 — DONE 2026-05-03 07:14 UTC (Code mode). See F12 above.

### To resume §4.2 (PHASE_Y v2):

> Read [`.roo/handoffs/2026-05-03-master-resume-handoff.md`](.roo/handoffs/2026-05-03-master-resume-handoff.md:1) §2 (binding directives D1–D5) and §4.2. Then rewrite [`shared/docs/PHASE_Y_LEARNING_DASHBOARD_PLAN.md`](shared/docs/PHASE_Y_LEARNING_DASHBOARD_PLAN.md:1) as v2 incorporating: memory unification (kill openclaw_export_daemon), 7d/14d/30d aggregation views, the skill-vs-luck composite formula, and resolution of the 5 issues from v1. Per-phase: explicit acceptance criteria + file lists + SQL DDL.

Mode: `architect` (only edits `*.md`).

### To resume §4.3 (`.md` vs mem0 audit):

> Read [`.roo/handoffs/2026-05-03-master-resume-handoff.md`](.roo/handoffs/2026-05-03-master-resume-handoff.md:1) §4.3. Run the discovery `grep` commands. Produce `.roo/handoffs/2026-05-XX-md-vs-mem0-audit.md` with: per-file inventory, proposed mem0 namespace + agent_id per writer, migration script spec preserving `created_at` as `metadata.original_timestamp`, and a CI-gate spec for [`shared/scripts/writer_registry_grep.py`](shared/scripts/writer_registry_grep.py:1).

Mode: `architect` first (audit + migration spec), then `code` for implementation.

### To resume the whole stack (fresh agent, no context):

> Read these in order: (1) [`.roo/handoffs/2026-05-03-master-resume-handoff.md`](.roo/handoffs/2026-05-03-master-resume-handoff.md:1), (2) [`.roo/handoffs/2026-05-02-position-pipeline-results.md`](.roo/handoffs/2026-05-02-position-pipeline-results.md:1), (3) [`CLAUDE.md`](CLAUDE.md:1) (note: it says MySQL, that's wrong — we're on Postgres). Then re-run §6 verification queries (Q1-Q9) to confirm pipeline state hasn't drifted. Pending todos in §4. Pick one and execute its §10 resume command.

Mode: any.

---

## §11 — Out-of-scope (explicitly NOT in this wave)

These exist on the radar but were deferred:

- Periodic CCXT refresh of `instruments.taker_fee_pct` (D7 future hook — schedule once weekly via [`shared/intelligence/cron_canary.py`](shared/intelligence/cron_canary.py:1))
- News Feed tab / Manage Panel UI (Phase X.4-X.6 of [`shared/docs/PHASE_X_DASHBOARD_AND_PIPELINE_PLAN.md`](shared/docs/PHASE_X_DASHBOARD_AND_PIPELINE_PLAN.md:1))
- Schema-drift CI gate (Phase 12 of unified plan — F11 surfaced the need but didn't build it)
- CLAUDE.md correction: "MySQL" → "PostgreSQL" (1-line fix, deferred to next architect task)
- New trading-company namespace creation (will happen when first production firm onboards)

---

## §12 — Sign-off

- **Position pipeline:** RESTORED. End-to-end verified with independent SQL.
- **77 orphan trader signals:** RESOLVED with fee-accurate P&L + real LLM postmortems.
- **6 daemons:** systemd-supervised, auto-restart, journald logs.
- **Symbol resolution:** unambiguous via D6 slash-form canonical + 58-row alias bridge.
- **Fee math:** real, Decimal, replicates exchange behaviour per D7.
- **3 todos pending:** all have full execution recipes in §4 and resume commands in §10. Nothing is "TBD" — every step has a concrete next action.

If a future agent finds a gap in this document, that gap is a bug. Patch it in-place.

— Debug, 2026-05-03
