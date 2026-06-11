# Heal + Backfill — Single Source of Truth (2026-05-29)

> Owner-approved big-fix. Goal: make the signal pipeline correct again AND
> backfill every chart since it broke, so the system is a single source of
> truth with 100% completeness and 100% outcome-accuracy wherever we have
> price data.
>
> **Status:** IN PROGRESS. This doc is the canonical plan + rollback. Update it
> at the end of every phase (explain what/why/how-to-rollback in plain English).

---

## 0. What broke, exactly (root cause — confirmed by forensics)

On **2026-05-25 12:05 UTC** two new `chart_analysis` prompts were written to the
DB (`prompt_versions`): `2026.05.24-DIscord` and `2024-05-25=telegram-rose-v1`.
From that moment extraction collapsed:

| Date | signals | conf>0 | box extracted |
|------|--------:|-------:|--------------:|
| 05-24 | 35 | 35 | 35 |
| 05-25 | 84 | 8 | 8 |
| 05-26 | 74 | 4 | 4 |
| 05-27 → now | 123 | **0** | **0** |

Last fully-good signal: **2026-05-26 07:12:46 UTC**.

### The bug (one sentence)
Each new prompt's `system` half and `body` (user-template) half are **two
contradictory prompts mashed together**, and they also disagree with the parser:

- **`system`** = rigid *"THE ONE RULE"* — a trade may only be extracted if the
  model sees a literal green/red (Discord) or blue/yellow (Rose) **position-box
  pair**; anything else → empty arrays. It also asks for per-trade
  `trader_confidence` / `explicit_evidence`.
- **`body`** = a *different, sane* "identify levels by semantic role, don't rely
  on fixed colours" prompt, whose **output schema has NO per-trade confidence**
  and puts `reasoning` at the **top level**.

The model obeys the strict `system` (→ refuses every chart that isn't a textbook
position box: Fib zones, S/R lines, arrows, teal/orange boxes, etc.) **and**
emits the `body` schema (→ no per-trade confidence, top-level reasoning).

### The three downstream symptoms (all from that one contradiction)
1. **Refusals.** Real setups (BTC Fib-zone longs, gold boxes, trendline breaks)
   → `trader_trades:[]`, `chart_hacker_trades:[]`.
2. **Zero confidence.** When a trade *is* extracted, `trader_trades` carries no
   `confidence` → parser reads `0.0` → the arming gate
   (`create_tracked_position_from_interpretation`, `confidence < 0.4 → return
   None`) kills it. (Verified: `trader_trade.confidence` is `None` in *every*
   signal, armed or not.)
3. **Blank reasoning.** Model writes `reasoning` at top level; parser only reads
   per-trade `rationale` (line ~1313) → DB stores `''`.

### Bonus defects found
- **Junk fallback:** the legacy fallback (`interpretation_service.py` ~line 4539)
  arms a position from a bare `consensus.direction` with **no SL/TP at live
  price**. Result: the only 11 positions armed in 24h are all `SL=None TP=None`
  garbage, while every properly-leveled setup was dropped.
- **Three prompt sources:** `_load_prompts_async` falls back DB → `system_config`
  (still populated: `telegram/rose`, `default`, …) → file
  (`prompts/chart_analysis.json`, a duplicate). Nobody knows which ran.
- **Messy versioning:** `2026.05.24-DIscord` (typo), `2024-05-25=telegram-rose-v1`
  (typo year + `=`); recorded `prompt_version` is inconsistent (`db:…` vs file
  internal version).

---

## 1. Locked decisions (owner, 2026-05-29)

1. Reprocess **everything since the prompt swap (2026-05-25 12:05)** — 259 signals.
2. **Fix the extraction contract first, then backfill.** **Test on a small
   sample before the full 259** so we don't redo 259 and then find mistakes.
3. **Learn as we go — improve the prompt iteratively** during the sample tests.
4. **TradFi tail** (QQQ, US30, DOW, NVDA, S&P 500, WTI/oil, NAS100, AUD/NZD, MNQ):
   try to resolve price via **MCP capital.com** or **exchange equivalents**; if
   genuinely impossible, extract+show the signal but mark outcome untracked.
5. **Re-fire the vision LLM** on the stored images (costs API money — fine).
   **If any LLM/API call returns insufficient-funds / rate-limit → PAUSE, tell
   the owner, wait for balance top-up + "continue".**
6. **Point-in-time** quant + outcomes: candles *as of* each signal's timestamp,
   walk forward; intrabar SL/TP tie-break = **worst-case (SL first)**.
7. **Recompute** the 12 paper agents' balances from the break point (not append).
8. **Delete/supersede** the 11 junk positions (+ any junk competition_trades).
9. Backfilled postmortems + mem0/MemU writes are **tagged as backfilled**.
10. Backfill engine is **idempotent / re-runnable** (keyed on correlation/news/media id).

### Honest limits on "100%"
- LLM re-read = fresh best-effort, not a byte-identical replay of a live call.
- Outcomes/P&L are exact only where we have candle data (crypto + XAU/USDT today;
  TradFi pending decision 4).

---

## 2. Build order (phases)

- **P1 — FIX (must precede backfill):**
  - P1a Rewrite Discord + Rose prompts so `system` + `body` agree: semantic-role
    extraction from ANY drawing style; output schema carries per-trade
    `confidence`, `reasoning`, `evidence_source`. Store as new DB versions.
  - P1b Parser: read per-trade `confidence`; `reasoning` per-trade else top-level;
    apply to BOTH `trader_trades` and `chart_hacker_trades`.
  - P1c Gate: arm on `direction + entry + (SL or TP)`; confidence ranks/sizes, it
    does not hard-kill a fully-leveled setup. Remove the bare-direction
    live-price junk fallback.
  - P1d Single prompt source: drop `system_config` + file fallbacks from
    `_load_prompts_async`; fail-loud if no DB prompt for a source.
  - **P1-TEST:** re-fire vision on ~8 broken charts across styles/sources; verify
    real extraction + would-arm; iterate the prompt. **Gate to P2 = owner OK.**
- **P2 — BACKFILL ENGINE (idempotent):**
  re-interpret the 259 → re-arm open / close resolved → point-in-time outcomes →
  TradFi resolution → replay 12 agents + recompute balances → junk cleanup →
  retrospective postmortems → mem0/MemU. Pause on funds/limit.
- **P3 — VERIFY/RECONCILE:** radar (approaching), positions tab, leaderboard,
  interpretations all coherent + reconciled to the 259.
- **P4 — GUARDRAILS:** prompt↔parser contract test + daily extraction-rate canary
  (would have caught 05-25 in hours, not 4 days).

---

## 3. Rollback

- **Prompts:** originals backed up at
  `shared/reports/heal_backfill_2026_05_29/prompt_backup/*.json`. To revert,
  re-insert those `system`/`body` values for versions `2026.05.24-DIscord` and
  `2024-05-25=telegram-rose-v1`.
- **Code (parser/gate/loader):** changes will be commented-out-old-then-new with
  clear `HEAL-2026-05-29` markers; `git checkout` of `interpretation_service.py`
  reverts. (Exact line ranges recorded per-phase below as they land.)
- **Backfill data:** every backfilled row is tagged (correlation_id +
  `backfilled_at`), so a single `DELETE WHERE backfilled_at IS NOT NULL` (per
  table) fully reverses the data backfill without touching live rows.
- **Junk positions:** the 11 ids are snapshotted before deletion (see
  `shared/reports/heal_backfill_2026_05_29/`).

---

## 3a. Prompt evolution (batch-tuning gauntlet, 2026-05-29)

Tuned the draft prompts against the real broken corpus in batches of 20
(`batch_tune.py`, read-only re-fire). Model switched mid-way: owner set
`OPENROUTER_DEFAULT_MODEL=anthropic/claude-sonnet-4.6` — all tuning from batch 2
onward uses 4.6 (batches 0–1 were sonnet-4).

| ver | learning baked in |
|-----|-------------------|
| v1  | coherent semantic-role extraction (all chart styles) + per-trade confidence/reasoning/evidence |
| v2  | stop chart_hacker hedging both directions; symbol↔price consistency check; levels discipline (no round-number guesses) |
| v3  | confidence calibration (reserve ≥0.6 for real trigger+invalidation, speculative <0.6) so the inferred-arm policy self-filters |
| v4  | trader-track LEVEL COMPLETION — when a human frames an explicit entry with drawn profit/stop zones or labelled fibs, capture the full trio into trader_trade (fixes GOLD-style entry-only misses) |
| v5  | (brevity caps — REVERTED at owner request) |
| v6  | DRAWN DIRECTIONAL HINTS vocabulary (arch/dome+down-arrow=short, bowl/U+up-arrow=long, yellow circle on S/R, lightning, Bull/Bearzone) for INFERRED reads ONLY; comprehensive learning-grade reasoning (patterns/trend/SMC/why) — NOT truncated; raised interpretation max_tokens 2048→8192 (gateway_config); enriched trader-assessment fields (trader_market_view / chart_hacker_market_view / ai_comment_on_trader) |

Also fixed: `shared/intelligence/gateway_config.py` `for_service()` now resolves
`max_tokens` (env-overridable `LLM_MAX_TOKENS_<SERVICE>` / `LLM_MAX_TOKENS_DEFAULT`;
interpretation defaults 8192, others 2048). Root cause of 2 lost charts
(JTO #2714, LDO #2715) was 2048-token truncation → invalid JSON.

Batch results (would-arm, honest UNKNOWN-symbol rejection applied):
  batch 0 (sonnet-4, v2): 19 fired, 19 arm — flags UNKNOWN×2, NO_LEVELS×2
  batch 1 (sonnet-4, v3): 19 fired, 18 arm — flags UNKNOWN×1
  batch 2 (4.6, v4):      20 fired, 16 arm — flags INVARIANT×1, UNKNOWN×2, NO_LEVELS×1, +2 JSON-truncation (fixed in v6)

## 4. Phase log (append as we go)

- _2026-05-29:_ Forensics complete; root cause confirmed (system/body prompt
  contradiction + confidence plumbing + junk fallback). Prompts backed up.
  Roadmap created.
- _2026-05-29 (P1a draft + P1-TEST):_ Wrote coherent semantic-role prompts
  (`shared/reports/heal_backfill_2026_05_29/new_prompts.py`) emitting the exact
  parser contract (per-trade confidence/reasoning/evidence). Built read-only
  re-fire harness (`refire_test.py`) using the production `call_vision_llm` +
  real `_parse_llm_json`. **Result: v1 re-fired 7 sample charts (Discord +
  Rose + NAS100 + AUD/NZD) — all 7 would arm vs ~0 live.** v2 fixed 3 quality
  issues found in v1 (chart_hacker hedging both directions; round-number
  "inferred" levels; symbol/price inconsistency e.g. BTC@266). Confidence now
  honest, reasoning populated, all chart styles extract. Parser confirmed to
  already read per-trade `confidence` (line 1296) — so only a small top-level
  `reasoning` fallback is needed, not a rewrite.
  **OPEN DECISION before install + 259 run:** policy for chart_hacker "inferred"
  reads on commentary charts (where the human committed nothing) — arm always /
  only when anchored to drawn levels / inferred only if confidence ≥ threshold.
  Next: lock that policy, install v2 prompts as new DB versions (single source),
  apply parser reasoning-fallback + gate relaxation + remove junk fallback, then
  build the idempotent backfill engine and run in tested batches.
- _2026-05-29 (policy locked + integration de-risked):_ Owner chose **arm
  anchored chart_hacker reads always; arm pure `inferred` only if confidence
  ≥ 0.6 (tagged); explicit trader setups always arm.** Verified the Round 9
  trader gate (`_is_explicit_trader_setup`, line 2688) bypasses for our new
  prompts: it only enforces A1–A4 when `_TRADER_GATE_PROMPT_TAG`
  (`"trader-explicit-only"`) is in the prompt version; our versions
  (`2026.05.29-discord-semantic-v2`, `rose-semantic-v2`) omit it → gate returns
  `(True,"legacy_prompt")` → accepts every trader_trade. **No gate edit needed.**
  Final P1 change-list (all integration risks resolved):
    1. Install v2 prompts as new DB `prompt_versions` rows (single source) +
       point the source→prompt mapping at them.
    2. Parser (`interpretation_service.py` ~1313-1324): add top-level `reasoning`
       fallback for per-trade rationale (per-trade first, else top-level).
    3. Gate (`create_tracked_position…` ~2974): relax the `_conf < 0.4` hard
       floor (trader trades are already explicit-by-prompt; chart_hacker policy
       is enforced at the Pass-2 call site). Keep entry/symbol/min-price guards.
    4. Pass-2 chart_hacker call site (~4520-4534): enforce arming policy using the
       trade's `evidence` field (anchored→arm; inferred→arm iff conf≥0.6).
    5. Remove the junk bare-direction live-price legacy fallback (~4536-4574) —
       comment out with HEAL-2026-05-29 marker (this is what armed the 53 junk
       rows with null symbol/TP).
    6. Single prompt source (`_load_prompts_async` ~843-963): drop `system_config`
       + file fallbacks; fail-loud if no DB prompt for a source.
  All six are confirmed safe to land together; then install prompts + restart as
  the activation step, then build the idempotent backfill engine.
