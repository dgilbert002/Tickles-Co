# Phase J — ChartHacker Dual Analysis

**Status**: SHIPPED (producer side) 2026-05-03
**Author**: Claude (alongside user, while Roo finished Phase Y)
**Companion documents**:
- [`shared/docs/PHASE_Y_LEARNING_DASHBOARD_PLAN.md`](PHASE_Y_LEARNING_DASHBOARD_PLAN.md) — the consumer side that reads what Phase J writes
- [`shared/intelligence/migrations/2026_05_03_phase_j_chart_hacker_dual.sql`](../intelligence/migrations/2026_05_03_phase_j_chart_hacker_dual.sql)

---

## 1. Why this exists

The user's original prompt design ("Lens") was a **dual-extraction** chart analyser — for every chart it sees, it produces both:
1. The trader's interpretation (what the human marked / said / drew)
2. An independent AI interpretation (what the AI itself would trade based on TA)

The previous interpretation pipeline collapsed both into one signal. Phase J restores the dual extraction — but instead of inventing a separate "Jarvis" personality, it treats **`chart_hacker`** (the existing interpreter agent) as a first-class actor that produces its own trades alongside the human trader's, both sides flowing through the same downstream pipeline.

---

## 2. The core idea

`chart_hacker` becomes a registered actor on the leaderboard, with:
- Its own positions in `tracked_positions` (`signal_source='chart_hacker'`)
- Its own postmortems (the existing service runs on every closed position regardless of source)
- Its own mem0 namespace (`agent_id={company}_chart_hacker`)
- Its own skill score (Phase Y's `compute_skill_score()` works on any actor)

This means **chart_hacker competes on the same leaderboard as emutrading, thelordofentry, etc.** Without writing any new monitoring/scoring code, you instantly answer: *"chart_hacker vs emutrading vs thelordofentry over 30 days — who's actually winning?"*

---

## 3. The two memories chart_hacker builds

Both live in the **same mem0 namespace** (`agent_id={company}_chart_hacker`), differentiated by `metadata.about`:

| `metadata.about` | Source | Example memory |
|---|---|---|
| `self` | chart_hacker's own closed trade | "When I called LONG on 4h RSI overbought breakouts, lost 6 of 8 last month" |
| `trader:<handle>` | A human trader's closed trade | "emutrading hit TP1 on 6 of last 8 SOL/USDT longs during Asia session" |
| `company` | Company-wide lesson | "Major regime shift in BTC 2026-04-30; tighten stops by 30%" |

When the next chart comes in from `emutrading`, chart_hacker's pre-analysis recall pulls all three buckets and injects them into the prompt:

```
PAST MEMORIES (use these to inform your analysis...):

Your own past lessons:
- When I called LONG on 4h RSI overbought breakouts, lost 6 of 8 last month
- My RSI-divergence calls on BTC have a 71% hit rate

Your past observations about emutrading:
- emutrading hit TP1 on 6 of last 8 SOL/USDT longs during Asia session
```

The LLM is no longer stateless — it reasons with self-correction + trader-credibility + cross-company context.

---

## 4. Schema additions (J.1)

Migration: `2026_05_03_phase_j_chart_hacker_dual.sql`

### `tracked_positions`
| Column | Type | Purpose |
|---|---|---|
| `signal_source` | `VARCHAR(20)` CHECK in `('trader','chart_hacker')` | Which side of the dual extraction this row came from |
| `trade_type` | `VARCHAR(20)` | swing / scalp / degen / position |
| `timeframe` | `VARCHAR(8)` | 1m / 5m / 15m / 1h / 4h / 1d / 1w |
| `take_profit_4..6` | `DECIMAL(20,8)` | Lens prompt extracts up to 6 TP levels |

Indexes added:
- `idx_tracked_positions_signal_source`
- `idx_tracked_positions_actor_company`

### `signal_interpretations`
| Column | Type | Purpose |
|---|---|---|
| `timeframe` | `VARCHAR(8)` | Chart timeframe identified by the LLM |
| `chart_analysis` | `JSONB` | market_structure, candle_analysis, indicators, patterns, key_levels |
| `trader_trades` | `JSONB` array | Each setup the LLM thinks the trader marked |
| `chart_hacker_trades` | `JSONB` array | chart_hacker's own independent setups |
| `ai_agreement_score` | `DECIMAL(3,2)` | 0..1 — alignment between trader and chart_hacker |
| `ai_comment` | `TEXT` | chart_hacker's assessment of the trader's thesis |

### Actor seed
`trader_profiles` row:
```
platform='api', handle_normalized='chart_hacker',
display_name='ChartHacker (AI vision agent)', trader_type='bot'
```

---

## 5. Pipeline changes (J.2)

### J.2a — Lens-style prompt
File: `shared/intelligence/prompts/chart_analysis.json` — replaced wholesale with the dual-extraction prompt. New output schema includes `instrument`, `timeframe`, `trader_trades[]`, `chart_hacker_trades[]`, `chart_analysis{}`, `ai_agreement_with_trader`, `ai_comment_on_trader`, `trader_market_view`, `chart_hacker_market_view`.

### J.2b — Memory recall before LLM call
New helper `_recall_relevant_memories()` in `interpretation_service.py`:
- Pulls chart_hacker's self-memories (mem0)
- Pulls chart_hacker's accumulated observations about THIS trader (mem0)
- Returns formatted context injected into the prompt's `{recall_context}` slot
- Best-effort: any failure returns empty string — never blocks analysis

### J.2c — Price lookup helper
New helper `_lookup_current_price()`: when a trade has `direction + symbol` but no entry price, queries the latest 1m candle close as the entry. Solves the "long TIA, no price stated" case.

### J.2d — Dual write
`_process_one()` now writes TWO sets of `tracked_positions` rows from one chart:
- For each `trader_trades[]` element → row with `signal_source='trader'`, `actor_id=<company>_trader_<id>`, trader's `trader_profile_id`
- For each `chart_hacker_trades[]` element → row with `signal_source='chart_hacker'`, `actor_id=<company>_chart_hacker`, chart_hacker's seeded `trader_profile_id`

A legacy fallback preserves single-write behaviour when the LLM returns the old schema (e.g. fallback model on retry).

### J.2e — Postmortem → mem0 (closing the loop)
After `position_postmortems` row is written successfully, `_push_lessons_to_mem0()` writes:
- `lessons_for_actor` → mem0 with `metadata.about='self'` (chart_hacker's positions) or `metadata.about=trader:<handle>` (trader positions, observed by chart_hacker)
- `lessons_for_company` → mem0 with `metadata.about='company'`

Both go to chart_hacker's namespace so the next analysis can recall them.

---

## 6. What this gives you on the dashboard

### Phase Y dashboard works automatically
Phase Y was built to score per-`actor_id`. Once chart_hacker writes positions:
- **Skill-vs-luck card** — chart_hacker has its own 7d/14d/30d skill score
- **Agent Brain Card** — wins/breakeven/losses for chart_hacker, alongside human traders
- **Memory Feed** — pulls from `position_postmortems.lessons_for_actor` for both sides
- **Failed Trades surface** — flags chart_hacker's failures alongside humans'

### What's still pending (J.3 — dashboard drawer)
Held until Roo finishes Phase Y to avoid file conflicts. When you build it:
- New endpoint `/api/interpretations/<id>/full` returning the full `signal_interpretations` row
- Drawer renders: chart image, trader's read (left), chart_hacker's read (right), agreement score, ai_comment
- Click a chart anywhere on the dashboard → drawer opens

### Head-to-head metrics (J.4 — fall-out)
SQL queries that fall out of the schema for free:
- *"Of trades where chart_hacker and emutrading agreed, win rate = ?"*
- *"When they disagree, who's right more often?"*
- *"chart_hacker's skill score curve over 30 days — is it improving?"*

---

## 7. Files modified / created

```
shared/intelligence/migrations/2026_05_03_phase_j_chart_hacker_dual.sql           NEW
shared/intelligence/migrations/2026_05_03_phase_j_chart_hacker_dual_ROLLBACK.sql  NEW
shared/intelligence/prompts/chart_analysis.json                                   REWRITE
shared/intelligence/interpretation_service.py                                     EXTEND
shared/intelligence/postmortem_service.py                                         EXTEND
shared/docs/PHASE_J_CHART_HACKER_DUAL_ANALYSIS.md                                 NEW (this file)
```

Crucially, **none of Phase Y's files are touched**. The dashboard layer (`shared/dashboard/`) was deliberately left alone so Roo's in-progress Phase Y work can complete cleanly. The drawer UI (J.3) is the follow-on once Roo is done.

---

## 8. Operational notes

- Restart `tickles-interpretation.service` and `tickles-postmortem.service` to pick up code changes
- Migration is idempotent — safe to re-run
- Rollback file provided
- Memory recall is best-effort — never blocks analysis on mem0 failures
- Existing 77 historical positions remain untouched (NULL `actor_id`, `signal_source` defaults to 'trader')
- New positions going forward use the dual-write path
- The legacy fallback ensures backward compatibility if the LLM returns the old prompt format
