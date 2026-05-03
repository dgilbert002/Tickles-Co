ignore claude.md, its out of date. ENV is here /opt/tickles/.env

review @/shared/docs/TICKLES_INFRASTRUCTURE_ATLAS.md  and review a proposed plan modelled against the @/shared/docs/TICKLES_INFRASTRUCTURE_ATLAS.md  document here: "# Unified Plan — Tickles Intelligence, Audit, Post-Mortem & Learning Architecture

You already have ~80% of what you need. The right move is **not to rebuild** — it is to make the existing pipeline **visible, configurable, unified, and causal**. Below is the consolidated plan, ordered by what unblocks you fastest, with every decision made explicitly so your VS Code agent has no ambiguity.

---

## PART A — IMMEDIATE VISIBILITY (Days 1–2)

You cannot improve what you cannot see. Everything else waits until you can answer: *"What did we interpret today, who posted it, which model ran it, at what cost, and does the chart actually look like what the model said?"*

### A1. `.env` — Signal Interpretation Control Block

Append this to `.env` and `.env.template`. **Keys are referenced by env-var name, not duplicated**, so there's one source of truth for `REQUESTY_API_KEY` / `OPENROUTER_API_KEY`.

```bash
# =============================================================================
# SIGNAL INTERPRETATION — Provider / Model / Gateway / Temperature
# =============================================================================

SIGNAL_INTERPRETATION_ENABLED=true
SIGNAL_INTERPRETATION_BATCH_SIZE=20
SIGNAL_INTERPRETATION_DEDUP_HOURS=24
SIGNAL_INTERPRETATION_PRICE_DEDUP_PCT=2.0

# Prompt (versioned, hashed per call)
SIGNAL_CHART_PROMPT_PATH=shared/intelligence/prompts/chart_analysis.json
SIGNAL_CHART_PROMPT_VERSION=chart_analysis_v1

# ----- Pre-filter: cheap binary "is this a chart?" -----
SIGNAL_PREFILTER_ENABLED=true
SIGNAL_PREFILTER_PROVIDER=requesty
SIGNAL_PREFILTER_GATEWAY_URL=https://router.requesty.ai/v1
SIGNAL_PREFILTER_API_KEY_ENV=REQUESTY_API_KEY     # indirection — no key duplication
SIGNAL_PREFILTER_MODEL=google/gemini-2.5-flash
SIGNAL_PREFILTER_TEMPERATURE=0.0
SIGNAL_PREFILTER_MIN_CONFIDENCE=0.60

# ----- Full vision analysis -----
SIGNAL_VISION_PROVIDER=requesty
SIGNAL_VISION_GATEWAY_URL=https://router.requesty.ai/v1
SIGNAL_VISION_API_KEY_ENV=REQUESTY_API_KEY
SIGNAL_VISION_MODEL=custom/chart-hacker-waterfall  # YOUR Requesty custom waterfall (Gemini 3.1 → Sonnet 4.6)
SIGNAL_VISION_TEMPERATURE=0.1                      # 0.1 = strict/anti-hallucination; raise for exploration runs
SIGNAL_VISION_MAX_TOKENS=2048
SIGNAL_VISION_TIMEOUT_SECONDS=90

# Optional fallback if Requesty errors out (disabled by default — explicit is better)
SIGNAL_VISION_FALLBACK_ENABLED=true
SIGNAL_VISION_FALLBACK_PROVIDER=openrouter
SIGNAL_VISION_FALLBACK_API_KEY_ENV=OPENROUTER_API_KEY
SIGNAL_VISION_FALLBACK_MODEL=anthropic/claude-sonnet-4

# ----- Text-only signal extraction -----
SIGNAL_TEXT_PROVIDER=requesty
SIGNAL_TEXT_API_KEY_ENV=REQUESTY_API_KEY
SIGNAL_TEXT_MODEL=google/gemini-2.5-flash
SIGNAL_TEXT_TEMPERATURE=0.0

# ----- Post-mortem / causal analysis -----
SIGNAL_POSTMORTEM_PROVIDER=requesty
SIGNAL_POSTMORTEM_API_KEY_ENV=REQUESTY_API_KEY
SIGNAL_POSTMORTEM_MODEL=custom/chart-hacker-waterfall
SIGNAL_POSTMORTEM_TEMPERATURE=0.2

# ----- Optional tool-assisted reasoning budget -----
SIGNAL_DEEPEN_MAX_TOOL_CALLS=3           # cap MCP tool calls when LLM infers trader logic

# ----- Observability -----
SIGNAL_LOG_PROVIDER_CALLS=true           # every call → api_cost_log with provider/model/temp
SIGNAL_INTERPRETATION_STORE_RAW=true     # save full prompt/response to disk (not DB)
SIGNAL_INTERPRETATION_RAW_DIR=shared/reports/signal_payloads

# ----- Review report paths -----
SIGNAL_REVIEW_REPORT_DIR=shared/reports/signal_review
SIGNAL_REVIEW_PUBLIC_BASE_URL=http://charts-goblin.ts.net:8080/opticals/signal_review
```

Wire-up: extend `shared/intelligence/gateway_config.py` so `GatewayConfig.for_service("signal_prefilter" | "signal_vision" | "signal_text" | "signal_postmortem")` returns provider/model/temperature/api-key resolved from env. **Every call must log `provider=..., model=..., temp=...` to `api_cost_log`.**

Add a read-only diagnostic command that does **not** print keys:

```bash
python -m shared.intelligence.gateway_config --show signal_vision
# → Provider: requesty | Gateway: https://router.requesty.ai/v1 | Key: REQUESTY_API_KEY (present: yes)
#   Model: custom/chart-hacker-waterfall | Temp: 0.1 | Fallback: openrouter/claude-sonnet-4
```

### A2. Metadata Persistence Migration

Additive migration — safe to run anytime:

```sql
ALTER TABLE public.signal_interpretations
  ADD COLUMN IF NOT EXISTS prefilter_provider TEXT,
  ADD COLUMN IF NOT EXISTS prefilter_model TEXT,
  ADD COLUMN IF NOT EXISTS prefilter_temperature NUMERIC(6,3),
  ADD COLUMN IF NOT EXISTS prefilter_result JSONB,         -- {is_chart, confidence, label}
  ADD COLUMN IF NOT EXISTS prefilter_cost_usd NUMERIC(10,6),

  ADD COLUMN IF NOT EXISTS vision_provider TEXT,
  ADD COLUMN IF NOT EXISTS vision_model TEXT,
  ADD COLUMN IF NOT EXISTS vision_temperature NUMERIC(6,3),
  ADD COLUMN IF NOT EXISTS vision_cost_usd NUMERIC(10,6),
  ADD COLUMN IF NOT EXISTS prompt_version TEXT,
  ADD COLUMN IF NOT EXISTS prompt_hash TEXT,

  -- Raw I/O paths (NOT the payloads themselves — keep Postgres lean)
  ADD COLUMN IF NOT EXISTS llm_raw_request_path TEXT,
  ADD COLUMN IF NOT EXISTS llm_raw_response_path TEXT,

  -- Dual-logic capture
  ADD COLUMN IF NOT EXISTS trader_stated_thesis TEXT,      -- verbatim from message
  ADD COLUMN IF NOT EXISTS llm_inferred_thesis TEXT,       -- what LLM thinks (can use MCP tools)
  ADD COLUMN IF NOT EXISTS reason_agreement_score NUMERIC(4,2),  -- cosine(embed(trader), embed(llm)); NULL if one side silent

  -- Structured taxonomy for cross-trader pattern learning
  ADD COLUMN IF NOT EXISTS pattern_tags JSONB DEFAULT '[]'::jsonb,   -- [{tag:'ascending_triangle', confidence:0.82}]
  ADD COLUMN IF NOT EXISTS setup_tags JSONB DEFAULT '[]'::jsonb,     -- breakout, liquidity_sweep, mean_reversion
  ADD COLUMN IF NOT EXISTS regime_tags JSONB DEFAULT '[]'::jsonb,    -- high_vol, trending, ranging
  ADD COLUMN IF NOT EXISTS session_tags JSONB DEFAULT '[]'::jsonb;   -- london_open, asia, ny
```

**Decision — raw payload storage:** store to **files on disk** (`shared/reports/signal_payloads/{interpretation_id}_request.json` and `_response.json`), with only the path stored in Postgres. Rationale: raw vision prompts are large, base64-image payloads are huge, and Postgres shouldn't be a blob store. The HTML report renders from these files via `<details>`.

### A3. `signals.report` — CSV + HTML Export (The Thing You Actually Asked For)

New script `shared/intelligence/signal_review_export.py` + MCP tool `intelligence.signals_report`.

```bash
python -m shared.intelligence.signal_review_export --limit 100 --format both
python -m shared.intelligence.signal_review_export --since "2026-04-28 00:00:00" --trader TraderJ --format html
python -m shared.intelligence.signal_review_export --diagnose     # health check
```

Produces:

```
shared/reports/signal_review/latest.csv
shared/reports/signal_review/latest.html
shared/reports/signal_review/signals_YYYYMMDD_HHMMSS.{csv,html}
```

**CSV columns** (at minimum):

```
interpretation_id, created_at, tracked_position_id, position_status,
source, channel_id, channel_name, message_id, message_url,
author_id, author_handle, trader_profile_id, trader_handle,
instrument_symbol, timeframe, consensus_direction, confidence, consensus_method,
llm_entry, llm_sl, llm_tp, quant_entry, quant_sl, quant_tp,
trader_stated_thesis, llm_inferred_thesis, reason_agreement_score,
pattern_tags, setup_tags,
prefilter_provider, prefilter_model, prefilter_result,
vision_provider, vision_model, vision_temperature,
prefilter_cost_usd, vision_cost_usd,
image_url, image_local_path, image_http_url, discord_message_url,
raw_request_path, raw_response_path
```

**HTML report** — self-contained, renders at `charts-goblin.ts.net:8080/opticals/signal_review/latest.html`:

- Inline `<img>` thumbnails (click → full-size)
- Hyperlink back to the Discord message (`https://discord.com/channels/{guild}/{channel}/{message}`)
- Color-coded: green=long, red=short, grey=neutral, amber=low-confidence
- Collapsible `<details>` per row: full LLM prompt, raw response, quant output, tools called
- Filter chips (trader / channel / direction / pass-dedup)
- Auto-refresh meta tag (30s) so leaving it open in VS Code gives you a live view

**Serve path:** the existing static server maps `/opticals` somewhere — either have the script write directly into that folder or create a symlink:

```bash
ln -sfn /opt/tickles/shared/reports/signal_review /opt/tickles/opticals/signal_review
```

**Diagnostic output** (`--diagnose`):

```
=== Last 24h Signal Interpretation Health ===
media_items received:        312
  status=pending (backlog):   14   ⚠ investigate if >10
  status=analyzed:            287
  status=discarded:            11   (pre-filter: not a chart)
signal_interpretations:      276
  long:        91   short:  78   neutral/unclear: 107
tracked_positions opened:    169   (long+short only)
agent_opinions created:       64
Provider breakdown:
  prefilter: requesty/google/gemini-2.5-flash   298 calls   $0.032
  vision:    requesty/custom/chart-hacker-waterfall  287 calls   $3.14
  ⚠ WARNING: 3 calls used model != SIGNAL_VISION_MODEL — investigate
Estimated monthly run-rate at current volume: ~$94/mo
```

**Before coding, run the audit** — these psql queries answer "is anything actually happening?":

```sql
-- Inspect actual schema first (column names may differ from assumptions)
\d+ signal_interpretations
\d+ media_items
\d+ tracked_positions

-- Today's signals with trader + model
SELECT si.id, si.created_at, si.instrument_symbol, si.consensus_direction,
       si.confidence, si.vision_model, tp.handle AS trader
FROM signal_interpretations si
LEFT JOIN trader_profiles tp ON tp.id = si.trader_profile_id
WHERE si.created_at::date = CURRENT_DATE
ORDER BY si.created_at DESC LIMIT 100;

-- Provider reality check
SELECT provider, model, COUNT(*), SUM(cost_usd)
FROM api_cost_log
WHERE created_at::date = CURRENT_DATE
GROUP BY provider, model;
```

If Requesty does not appear in `api_cost_log`, the interpretation service is either not firing or still hardcoded to OpenRouter — fix before proceeding.

### A4. Fix + Extend `manage_sources.py`

**Fix option 6 (hierarchy crash)** — almost certainly a null JOIN on `collector_catalog`. Add null-guards and missing-row tolerance.

**Add Option 7 — Recent Interpreted Chart Signals**:

```
Recent Interpreted Signals (last 100)
───────────────────────────────────────────────────────────────────────────────
ID    Time   Trader      Channel        Symbol    Dir    Entry   SL      TP     Conf  Model
1452  14:02  TraderJ     trading-zone   BTCUSDT   long   65200  64100  68100  0.82  requesty/custom-waterfall
1451  13:48  CryptoApe   vip-signals    ETHUSDT   short   3230   3310   3050  0.77  requesty/custom-waterfall

HTML: shared/reports/signal_review/latest.html
CSV:  shared/reports/signal_review/latest.csv
```

This option reuses the export script under the hood and also prints the terminal table.

**Add Option 8 — Live Tracked Positions**:

```
Open Tracked Positions
─────────────────────────────────────────────────────────────────────────────────────────
ID  Trader     Symbol    Dir   Entry   Current  PnL%   SL     DistSL%  TP     DistTP%  Age     CH Agrees?
22  TraderJ    BTCUSDT   long  65200   66120    +1.41  64100  3.05     68100  2.99     2h14m   YES
23  CryptoApe  ETHUSDT   short 3230    3188     +1.30  3310   3.83     3050   4.33     48m     NO (CH: skip)
```

Support `--watch` for 30-second refresh. Color-coded: green if in profit, red near SL, amber if stalled.

---

## PART B — UNIFIED SCHEMA (Days 3–7)

The right answer to *"don't create `surgeon3_*`, `surgeon4_*` per agent"* is: **one universal position/performance/postmortem spine**, works for watched traders, agents, humans, copy-bots, crypto, CFDs.

### B1. Extend `tracked_positions` — Universal Position Ledger

```sql
ALTER TABLE public.tracked_positions
  -- WHO made this trade/signal
  ADD COLUMN IF NOT EXISTS actor_type TEXT NOT NULL DEFAULT 'watched_trader'
    CHECK (actor_type IN ('watched_trader','agent','human','copy_bot','strategy')),
  ADD COLUMN IF NOT EXISTS actor_id TEXT,              -- trader_profile_id | agent_name | user_id | strategy_id
  ADD COLUMN IF NOT EXISTS company_id TEXT,
  ADD COLUMN IF NOT EXISTS department TEXT,            -- 'trading_desk_a', 'research', 'personal'

  -- Position kind (observed vs paper vs live vs counterfactual)
  ADD COLUMN IF NOT EXISTS position_kind TEXT NOT NULL DEFAULT 'observed'
    CHECK (position_kind IN ('observed','paper','live','shadow','copy_trade','counterfactual')),
  ADD COLUMN IF NOT EXISTS is_paper BOOLEAN DEFAULT TRUE,

  -- Market + venue (crypto + CFD in one table)
  ADD COLUMN IF NOT EXISTS asset_class TEXT DEFAULT 'crypto'
    CHECK (asset_class IN ('crypto','cfd','stock','fx','commodity')),
  ADD COLUMN IF NOT EXISTS market_type TEXT,           -- 'spot','perp','cfd','futures'
  ADD COLUMN IF NOT EXISTS venue TEXT,                 -- 'binance','capital_com','bybit'
  ADD COLUMN IF NOT EXISTS epic_code TEXT,             -- Capital.com CFDs
  ADD COLUMN IF NOT EXISTS instrument_id BIGINT REFERENCES instruments(id),

  -- Multi-leg support (TP1/TP2/TP3, partial closes, SL-to-BE)
  ADD COLUMN IF NOT EXISTS legs JSONB DEFAULT '[]'::jsonb,  -- [{leg_id, tp_price, size_frac, hit_at, filled_qty}]
  ADD COLUMN IF NOT EXISTS sl_history JSONB DEFAULT '[]'::jsonb,  -- [{ts, old_sl, new_sl, reason}]
  ADD COLUMN IF NOT EXISTS partial_closes JSONB DEFAULT '[]'::jsonb,

  -- Dual reasoning (per your exact requirement)
  ADD COLUMN IF NOT EXISTS entry_reason_trader TEXT,   -- verbatim
  ADD COLUMN IF NOT EXISTS entry_reason_llm TEXT,      -- LLM inference (may use MCP tools)
  ADD COLUMN IF NOT EXISTS entry_reason_agent TEXT,    -- agent SOUL logic (Surgeon entry criteria)
  ADD COLUMN IF NOT EXISTS exit_reason_trader TEXT,    -- scraped from later trader messages
  ADD COLUMN IF NOT EXISTS exit_reason_llm TEXT,       -- post-mortem inference
  ADD COLUMN IF NOT EXISTS exit_reason_system TEXT,    -- sl_hit | tp_hit | time_stop | manual | inferred_stale

  -- Outcome
  ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS exit_price NUMERIC(20,8),
  ADD COLUMN IF NOT EXISTS realized_pnl_usd NUMERIC(20,8),
  ADD COLUMN IF NOT EXISTS outcome TEXT CHECK (outcome IN ('win','loss','breakeven','expired','abandoned')),
  ADD COLUMN IF NOT EXISTS postmortem_status TEXT DEFAULT 'pending'
    CHECK (postmortem_status IN ('pending','in_progress','complete','skipped'));

CREATE INDEX IF NOT EXISTS idx_tp_actor ON tracked_positions(actor_type, actor_id);
CREATE INDEX IF NOT EXISTS idx_tp_company_dept ON tracked_positions(company_id, department);
CREATE INDEX IF NOT EXISTS idx_tp_status_pm ON tracked_positions(status, postmortem_status);
```

### B2. `actor_performance` — One Leaderboard For Everything

Rather than creating yet another table, **generalize `trader_performance`** by adding the same actor columns. To preserve backward compatibility, keep `trader_performance` as the physical table and create a **view** for the unified read path:

```sql
ALTER TABLE trader_performance
  ADD COLUMN IF NOT EXISTS actor_type TEXT DEFAULT 'watched_trader',
  ADD COLUMN IF NOT EXISTS actor_id TEXT,
  ADD COLUMN IF NOT EXISTS company_id TEXT,
  ADD COLUMN IF NOT EXISTS department TEXT,
  ADD COLUMN IF NOT EXISTS asset_class TEXT,
  ADD COLUMN IF NOT EXISTS venue TEXT,
  ADD COLUMN IF NOT EXISTS reason_match_rate NUMERIC(5,2),   -- how often LLM inferred the trader's logic correctly
  ADD COLUMN IF NOT EXISTS pattern_affinity JSONB;           -- rolling tags this actor favors

CREATE OR REPLACE VIEW actor_leaderboard AS
SELECT actor_type, actor_id, company_id, department, asset_class,
       period, total_trades, win_rate, avg_rr, profit_factor, sharpe,
       total_pnl_usd, reason_match_rate, pattern_affinity, computed_at
FROM trader_performance;
```

One query now ranks Surgeon1, Surgeon2, TraderJ from ChartHackers Discord, your manual trades, copy-bots — **all in the same leaderboard, across crypto and CFDs**.

### B3. Surgeon2 Migration (Day 7)

Do this **after** visibility (Part A) is working:

| Old | New |
|---|---|
| `surgeon2_state` | `agent_state.state_data` (JSONB: balance, realized_pnl, cycle_counter) |
| `surgeon2_positions` | `tracked_positions` (actor_type='agent', actor_id='surgeon2', company_id='rubicon', position_kind='paper') |
| `surgeon2_trade_log` | `trades` + `trade_cost_entries` + `agent_decisions` |
| `tp1_done`, `tp2_done`, `remaining_frac` | `tracked_positions.legs` JSONB (which *does* model multi-leg properly) |

Keep old tables read-only for 1 week, then drop. Surgeon2 now participates in `actor_leaderboard` automatically.

### B4. Structured Pattern Taxonomy

The `pattern_tags` / `setup_tags` / `regime_tags` / `session_tags` JSONB columns on `signal_interpretations` let you aggregate trader behavior without a rigid normalized schema. The vision prompt must require them:

```json
{
  "pattern_tags": [{"tag":"ascending_triangle","confidence":0.82}],
  "setup_tags":   [{"tag":"breakout","confidence":0.80}],
  "regime_tags":  [{"tag":"high_volatility","confidence":0.71}],
  "session_tags": [{"tag":"london_open","confidence":0.69}]
}
```

Don't build a formal `setup_catalog` table yet — wait until you have 1000+ signals and can see which tags converge. Then normalize.

---

## PART C — DUAL-LOGIC REASONING (Days 3–5)

Your most important epistemic feature: **what the trader said** vs **what the LLM inferred** — and protecting against the LLM rationalizing after the fact.

### C1. At Entry — Capture Both Perspectives

When `InterpretationService` processes a chart:

**1. Trader-stated thesis** (`entry_reason_trader`): extract verbatim via `TextSignalExtractor`:
- Keywords: "because", "setup is", "RSI", "VWAP", "London open", "liquidity grab", "FVG", "SMC", "divergence"
- Also scan the **prior 10 messages** from the same user in the same channel (the "I'm about to enter BTC" context)
- If nothing found → `NULL`. Never fabricate.

**2. LLM-inferred thesis** (`entry_reason_llm`): the vision prompt requires a structured `reasoning` block plus pattern tags. The LLM produces its *own* theory about why the setup works.

**3. Optional tool-assisted deepening**: if `confidence < 0.7` OR `entry_reason_trader IS NULL`, the interpreter may make up to `SIGNAL_DEEPEN_MAX_TOOL_CALLS=3` MCP calls (`md.candles`, `indicator.compute_preview`, `md.quote`) to refine its hypothesis. **This is budget-capped to bound cost.**

**4. Agreement score** (`reason_agreement_score`): compute cosine similarity between sentence-embeddings of `entry_reason_trader` and `entry_reason_llm`. **Store `NULL` if either side is empty** — no false competition when the trader was silent.

### C2. Hindsight-Bias Protection — Non-Negotiable

The LLM's entry interpretation must be **frozen at entry time** and never rewritten after outcome is known. The post-mortem service *reads* the original `entry_reason_llm` but writes its analysis into separate `exit_reason_llm` / `self_critique` fields. This is how we measure whether the LLM is genuinely learning or just rationalizing.

Enforce with a DB trigger or code-review rule: `entry_reason_*` columns are **immutable** after `status != 'pending'`.

### C3. Learning Metric Over Time

Populate `actor_performance.reason_match_rate` = fraction of trades (where both sides exist) with `reason_agreement_score > 0.7`. Over months, this tells you: *does the LLM actually understand TraderJ's style, or is it guessing?*

---

## PART D — CENTRALIZED POST-MORTEM SERVICE (Days 8–12)

**Decision: one centralized `PostMortemService` daemon, not per-agent post-mortem agents.** Runs on existing `ServiceDaemon` infrastructure (supervision, heartbeat, backoff all free). Agents stay lean — they trade; the service reflects. One prompt template, one learning loop, consistent schema.

### D1. New Service

**File:** `shared/intelligence/postmortem_service.py`
**Register:** `ServiceRegistry` with `enabled_on_vps=true`, tick = 60s
**Systemd:** `systemctl start tickles-service@postmortem-service`

### D2. `position_postmortems` — Dedicated Table

**Decision: dedicated table, not JSONB on `tracked_positions`.** Rationale: versionable prompts, multiple postmortems per position (e.g., quick + deep), separate audit trail, cleaner queries.

```sql
CREATE TABLE IF NOT EXISTS public.position_postmortems (
  id BIGSERIAL PRIMARY KEY,
  tracked_position_id BIGINT REFERENCES tracked_positions(id),
  actor_type TEXT, actor_id TEXT,
  postmortem_version TEXT NOT NULL DEFAULT 'v1',
  postmortem_type TEXT NOT NULL DEFAULT 'standard',    -- 'standard','deep','quick'

  created_at TIMESTAMPTZ DEFAULT now(),
  provider TEXT, model TEXT, temperature NUMERIC(6,3),
  prompt_version TEXT, prompt_hash TEXT,

  -- Deterministic metrics (no LLM required — computed first)
  deterministic_metrics JSONB DEFAULT '{}'::jsonb,
  -- Includes: mae_pct, mfe_pct, time_to_outcome_hours, near_miss_tp (bool),
  --          stop_too_tight (bool), rr_achieved, rr_planned, slippage_bps,
  --          did_sl_hit_first, peak_unrealized_pnl_pct, bars_in_position

  -- Evidence collected
  evidence JSONB DEFAULT '{}'::jsonb,
  -- Includes: candle_path_summary, nearby_trader_messages, duplicate_charts,
  --          position_updates_digest, agent_opinions_ids, related_trades

  -- Dual reasoning (frozen entry + post-close)
  trader_stated_entry_reason TEXT,
  llm_inferred_entry_reason TEXT,
  trader_stated_exit_reason TEXT,
  llm_inferred_exit_reason TEXT,
  exit_reason_system TEXT,

  -- Classification + learning
  outcome TEXT,                                  -- win|loss|breakeven|expired
  outcome_classification TEXT,                   -- sl_hit_on_liquidity_grab, tp_clean, stopped_on_news, drift_stop
  original_thesis_held BOOLEAN,
  lesson_learned TEXT,
  what_went_right TEXT,
  what_went_wrong TEXT,
  pattern_confirmed TEXT,

  -- Self-critique (the LLM grades its own entry reasoning in hindsight)
  llm_self_score NUMERIC(4,2),                   -- 0..1, how well did my entry thesis predict the outcome
  trader_logic_match_score NUMERIC(4,2),         -- 0..1, how well did I infer what the trader meant
  hindsight_bias_warning BOOLEAN DEFAULT false,  -- flagged if exit reasoning mentions info not available at entry

  tools_called JSONB,
  llm_cost_usd NUMERIC(10,6),
  raw_request_path TEXT, raw_response_path TEXT
);
```

### D3. Service Loop

```
every 60s:
  SELECT tracked_positions WHERE status IN ('closed','stopped','tp_hit','expired')
                             AND postmortem_status = 'pending'
  for each position:
    1. Gather evidence (no LLM):
       - candle path from entry_ts to close_ts
       - MAE / MFE / near-miss-TP / stop-tightness metrics
       - trader messages in [entry_ts, close_ts + 2h] about same symbol
       - duplicate chart updates
       - position_updates digest
       - agent_opinions
    2. Deterministic classification (sl_hit_first? tp_clean? time_stop?)
    3. LLM analysis (SIGNAL_POSTMORTEM_MODEL, temp=0.2):
       - Read FROZEN entry_reason_llm (no rewriting)
       - Compare to what actually happened
       - Produce exit_reason_llm, lesson, classification, self_score
       - May use MCP tools (indicator.compute_preview at exit bar, regime check)
    4. Write position_postmortems row
    5. Update tracked_positions (closed_at, realized_pnl_usd, outcome, exit_reason_*)
    6. If lesson is cross-company useful → memu.broadcast (Tier 3)
       Else → memory.add (Tier 2, per company)
    7. Update actor_performance rolling windows
```

### D4. Works For Everyone

Because `tracked_positions.actor_type` spans all actor kinds, **the same post-mortem service handles**: Surgeon1/2 closed trades, ChartHackers Discord traders, your manual entries, future copy-bots, strategy paper-forward trades. **One table. One service. One learning loop.**

Expose `postmortem.run_position {position_id}` as an MCP tool so agents can request on-demand postmortems.

---

## PART E — CHARTHACKER ROLE SPLIT

**Decision: split responsibilities cleanly, share the data spine.**

| Component | Role | Writes To |
|---|---|---|
| `InterpretationService` *(exists)* | Detect trader's signal from charts | `signal_interpretations`, `tracked_positions` (actor=watched_trader) |
| `ChartHackerGuru` *(exists)* | Cross-trader reports, hourly status, leaderboards | `shared/reports/`, Mem0 |
| `ChartHackerOpinionService` *(new, lightweight)* | Form independent opinion on every new signal | `agent_opinions` only |
| `ChartHackerCompetitor` *(optional, deferred)* | Paper-trade its own opinions to compete on leaderboard | `tracked_positions` (actor=agent, position_kind=paper) |

**Decision on the competitor question:** start with **agent_opinions only** (simpler, no data-model sprawl, keeps `tracked_positions` semantically clean). Agent opinions already track `would_take_trade`, `agent_entry/sl/tp`, `agent_pnl_pct`, `performance_delta` — that *is* the competition scoring.

**Only** graduate to `ChartHackerCompetitor` writing its own `tracked_positions` rows once you've seen enough agent-opinion data to know whether competitive paper-trading adds signal. This is a Phase 2 decision, not day-1.

### ChartHackerOpinionService Loop

For every new `tracked_positions` row (actor_type='watched_trader'):
1. Independent vision call with a prompt that **hides the trader's call** ("Ignore what anyone said. Looking only at this chart, what would YOU do?").
2. Write `agent_opinions` row with `would_take_trade`, `agent_direction`, `agent_entry/sl/tp`, `reasoning`, `confidence`.
3. `PositionMonitor` already updates `agent_pnl_pct` and `performance_delta` as the position evolves.

---

## PART F — TRADING-ZONE MONITORING

**Decision: filtered collection with context window.** The 3,000-member `trading-zone` is mostly noise. Don't ingest everything.

Collector config:
```yaml
channel: trading-zone
mode: filtered
filter: author_id IN (SELECT user_id FROM watched_users) 
     OR mentions_user_id IN watched_users
     OR reply_to_message_id IN (watched_users' recent messages)
context_window: ±10 messages around each match, stored in news_items.context_refs JSONB
```

This captures the "I'm about to enter BTC" banter AND the "got stopped out" follow-ups, without flooding the DB. The post-mortem service reads `context_refs` when looking for exit reasons.

**Open consideration:** how do we handle `tracked_positions` for observed watched-trader trades when the trader **never explicitly closes**? Two-stage rule: (1) if real-candle SL/TP would have triggered within 72h, mark `exit_reason_system='sl_hit'` or `'tp_hit'` at trigger price; (2) if neither fires within the trader's typical hold time (computed from their history), mark `exit_reason_system='inferred_stale'` and close at current price with `outcome='abandoned'`.

**Open consideration:** when multiple traders post contradictory signals on the same symbol within the dedup window, **both are tracked separately** (each under their own `trader_profile_id`). Dedup is per-trader, not per-symbol. This lets the leaderboard reveal which trader was right.

---

## PART G — MCP TOOLS (ADD / EXTEND, DON'T REBUILD)

### Extend existing
- `signals.recent` — add `include_images`, `include_raw`, `include_positions`, `since` params
- `positions.open` — ensure includes trader/actor, message/image links, CH agreement
- `traders.leaderboard` → rename call-through to `actor.leaderboard` (keep old name as alias)

### Add new (small, focused)
| Tool | Purpose |
|---|---|
| `intelligence.signals_report` | Part A3 report tool (CSV+HTML) |
| `intelligence.signals_diagnose` | Part A3 health check |
| `intelligence.postmortem_run` | Trigger post-mortem on a specific position |
| `intelligence.pattern_stats` | Query aggregated pattern performance per actor |
| `positions.context` | Given position_id → original message, image, nearby chats, updates, duplicates |
| `validation.verdict` | Expose existing `ValidationEngine` verdicts (Gap #12.6) |
| `validation.autopsy` | Expose existing Rule 1 autopsy (Gap #12.6) |
| `coach.propose` / `coach.rollback` | Prompt versioning (see Part I) |

---

## PART H — SINGLE-WRITER POLICY (PREVENT CHAOS)

As agents multiply, table ownership must be explicit. Enforce via code review:

| Table | Sole Writer |
|---|---|
| `news_items`, `media_items` | collectors |
| `signal_interpretations` | InterpretationService |
| `tracked_positions` (create) | InterpretationService, ExecutionRouter, human-intent service |
| `tracked_positions.status/outcome` | PositionMonitor + PostMortemService |
| `position_updates` | PositionMonitor |
| `agent_opinions` | ChartHackerOpinionService, PositionMonitor (updates) |
| `position_postmortems` | PostMortemService |
| `trader_performance` | PerformanceScorer |
| `trades`, `orders` | ExecutionRouter |
| `api_cost_log` | LLM gateway wrapper |
| `agent_decisions` | agents/services making the decision |

Agents access everything else via MCP tools, not direct writes.

---

## PART I — GENUINE GAPS (Days 15–30)

These are the Atlas-identified gaps. Map them to this plan:

### I.1 edge_score (3 days)
Composite sizing multiplier in `Treasury.evaluate()`:
```
edge_score = w1*signal_confidence + w2*trader_score + w3*backtest_expectancy 
           + w4*forward_test_health + w5*regime_fit + w6*risk_reward_ratio
           - w7*reason_agreement_penalty(if LLM inference contradicts trader)
```
Version it (`edge_score_v1`, `v2`) for A/B testing.

### I.2 CoachService (5 days) — Prompt Versioning & A/B Testing
`agent_prompts` table exists. Needs:
- `coach.propose {agent, prompt_name, new_content}` — queues for human approval
- `coach.deploy` / `coach.rollback` — one-click swap
- Auto A/B: run old + new prompts in parallel on last 7 days of `signal_interpretations` replays, compare `reason_agreement_score` and downstream `position_postmortem.llm_self_score`
- Proposal queue: auto-generate edits from Mem0 patterns ("LLM keeps missing liquidity-sweep setups — propose prompt update to add LSG keyword")

### I.3 MemoryLibrarian (3 days)
Daily daemon:
- Dedup memories with cosine similarity > 0.95
- Promote episodic → semantic when ≥3 similar events
- Prune memories contradicted by newer evidence
- Demote semantic memories whose associated win rate decayed below threshold

### I.4 Correlation-Aware Sizing (4 days)
Rolling correlation matrix across open `tracked_positions`. BTC + ETH longs ≈ 90% correlated → count as one bet. Treasury caps aggregate directional exposure per asset cluster.

### I.5 Research Agent (deferred — Phase 8)
Only after ≥500 tagged post-mortems exist. Queries `trader_patterns` + `position_postmortems` for high-win-rate setups → proposes strategies → calls existing `backtest.compose` + `backtest.plan_sweep` → runs sweeps → forward-test → Rule 1 → promote winners. **Zero new infrastructure needed** — the backtester already supports this.

### I.6 ClickHouse `agent_events` Unification
Keep ClickHouse for high-volume append-only event stream; surface key events into Postgres `agent_decisions` for the unified observability story. Treat ClickHouse as the firehose, Postgres as the curated timeline.

---

## PART J — OPEN CONSIDERATIONS (Blind Spots)

Acknowledge these explicitly; address where feasible now, defer where not.

1. **Privacy / ToS for Discord data** — scraping user messages and images at scale may violate Discord ToS and regional privacy laws (GDPR/CCPA). Mitigations: (a) only collect from channels you're authorized on via your own Discord account, (b) store user handles but hash raw user IDs after 30 days, (c) add a data-retention job that deletes media older than 90 days unless linked to an open/learning-valuable post-mortem, (d) never republish trader content outside your system.

2. **Cost projections** — at ~300 images/day with ~$0.0001 prefilter + ~$0.02 vision + ~$0.005 post-mortem → **~$7/day ≈ $210/month** at current volume. Monitor `api_cost_log` weekly; set a `SIGNAL_DAILY_COST_CAP_USD` guardrail that throttles the pipeline if exceeded.

3. **Prompt versioning & replay** — the `prompt_version` + `prompt_hash` columns let you re-run historical interpretations with a new prompt on cached images and compare outcomes. CoachService (I.2) formalizes this.

4. **Multi-leg / scaled-entry trades** — surgeon2 already supports TP1/TP2/TP3 with `remaining_frac`. The new `tracked_positions.legs` JSONB and `sl_history` JSONB columns model this properly; Surgeon2 migration (B3) is the first consumer.

5. **Time-zone / session normalization** — store all timestamps in UTC; derive `session_tags` (london_open, asia, ny) in the interpretation step using trader-configurable session definitions. Not perfect for weekend CFD traders — flag as open.

6. **Report authentication** — `charts-goblin.ts.net:8080/opticals` currently exposes signal data without auth. Minimum step: put it behind Tailscale ACL (which you're already using). Long-term: add basic-auth to the static server or route through the Phase 36 Dashboard's Telegram-OTP auth.

7. **Contradictory same-symbol signals** — handled by tracking each trader's signal separately (see Part F). The leaderboard resolves "who was right."

8. **Unclosed watched-trader positions** — handled with the two-stage rule in Part F.

---

## PART K — EXECUTION ROADMAP (30 Days)

| Day | Task | Unblocks |
|---|---|---|
| 1 | Run psql audit queries; confirm Requesty actually being called in `api_cost_log` | Sanity |
| 1 | `.env` signal block + `GatewayConfig.for_service()` + per-call logging | Provider visibility |
| 1 | Schema migration A2 (signal_interpretations metadata columns) | Audit trail |
| 2 | `signal_review_export.py` + MCP `intelligence.signals_report` (CSV + HTML) | **You can finally see what's happening** |
| 2 | Fix `manage_sources.py` #6, add #7 (signals), add #8 (positions) | Daily operations |
| 3 | Additional migrations: `tracked_positions` actor columns (B1), `trader_performance` extension (B2), `position_postmortems` (D2) | Unified foundation |
| 3–5 | Dual-reason extraction in InterpretationService (trader + LLM), agreement score, pattern tags | Learning foundation |
| 6 | ChartHackerOpinionService (writes to existing `agent_opinions`) | CH competition scoring |
| 7 | Surgeon2 migration to unified tables; drop `surgeon2_*` after 1 week read-only | Cleanup |
| 8–12 | `PostMortemService` daemon + deterministic metrics + LLM analysis + Mem0/MemU learning | Closing the loop |
| 13–14 | Trading-zone filtered collection + context window | Close-reason capture |
| 15 | `validation.verdict` / `validation.autopsy` / `positions.context` / `postmortem.run_position` MCP tools | Agent accessibility |
| 16–18 | Pattern aggregator: roll `pattern_tags` into `trader_performance.pattern_affinity` | Behavioral learning |
| 19–21 | `actor_leaderboard` view + unified leaderboard HTML report | Management visibility |
| 22–25 | edge_score v1 + wire into Treasury | Sizing intelligence |
| 26–28 | CoachService: prompt A/B + rollback | Self-improvement |
| 29–30 | MemoryLibrarian daemon: dedup, promotion, pruning | Memory hygiene |

---

## KEY DECISIONS — FOR YOUR SIGN-OFF

1. ✅ **One unified `tracked_positions`** — all actors (watched_trader/agent/human/copy_bot/strategy), crypto + CFD, via `actor_type` + `position_kind` columns. No per-agent tables.
2. ✅ **Centralized `PostMortemService`** — one daemon via existing ServiceDaemon, not per-agent post-mortems.
3. ✅ **Dedicated `position_postmortems` table** — not JSONB on tracked_positions (versionable, auditable, multiple postmortems possible).
4. ✅ **ChartHacker split**: InterpretationService + Guru + lightweight OpinionService writing only to `agent_opinions`. **No** Competitor-with-own-positions until data proves it's needed.
5. ✅ **Dual-reason capture** with agreement score NULL when one side is silent (no false competition). **Entry reasoning frozen** after position opens — hindsight-bias protection is non-negotiable.
6. ✅ **Requesty primary**, Gemini 2.5 Flash pre-filter, temperature 0.1 (raiseable for experimentation), all model/provider/temperature in `.env` with `*_API_KEY_ENV` indirection (no key duplication).
7. ✅ **Raw LLM I/O to disk** (`shared/reports/signal_payloads/`), path stored in DB. Postgres stays lean.
8. ✅ **HTML + CSV report** with image thumbnails, Discord links, collapsible LLM I/O, color-coded, auto-refresh — served at `charts-goblin.ts.net:8080/opticals/signal_review/`.
9. ✅ **Trading-zone = filtered ingestion** (watched users + ±10 context window).
10. ✅ **Pattern/setup/regime/session tags** as structured JSONB; normalize to a catalog table only after 1000+ signals show convergence.
11. ✅ **Single-writer policy** — explicit table ownership matrix, enforced at code review.
12. ✅ **Research agent deferred** — use existing backtester MCP tools when sufficient post-mortem data accumulates.

---

## THE PHILOSOPHY (Why This Works)

> The LLM may **propose**. The backtester **proves**. The forward-test **validates**. Treasury **sizes**. CrashProtection **protects**.

Every actor — human, Discord trader, agent, copy-bot — flows through the same data spine. Every trade produces a frozen entry thesis, monitored updates, deterministic outcome metrics, and a hindsight-protected post-mortem. Every useful lesson compounds into memory. Every promising pattern becomes a testable strategy.

**The system is a Trading Hive Mind** — humans and AI participating in the same scoring system, where the AI continuously observes, hypothesizes, scores itself against humans, and — when the data justifies it — writes new backtestable strategies based on what actually works.

---

## WHAT TO HAND YOUR VS CODE AGENT

Give it this plan plus:

> Begin with the Day-1 psql audit (Part A, bottom of A3). Report back what you find in `api_cost_log`, `signal_interpretations`, `tracked_positions` for the last 24h. Then execute Days 1–2 (env block, gateway refactor, metadata migration, export script, manage_sources fixes). Do NOT start Part D (post-mortem service) until Part A visibility is green. Acknowledge the plan and propose any clarifications before writing code.

**Reply with:**
- ✅ **Approve all** → hand to VS Code agent as the execution brief
- 🟡 **Change X** → specify which decisions/sections to alter
- ❓ **Deep-dive** → I can produce the exact SQL migration file, the post-mortem prompt template, the HTML report template, or the ChartHackerOpinionService SOUL.md" Now.. Create a roadmap of how we can impliment the plan in phases, and dont make assumptions that you KNOW the schema or the contents of our files. Review everything you need to to make sure we get a robust plan. Once done, there might be sections or parts to consider, for example the edge score - sometimes agents dont have a trader score or history, so they'd score less than a trade where the traders score from discord counted in the edge. What if i'm watching traders in telegram, or what if its just me trading personally, i'd be penalised cos i dont have what a discord trader has. the edge score needs to be platform agnostic yet good / tight enough so that any/all traders, bots, agents, humans have the same scoring. the .env files need to maintain all 100% content, but need reorganisation, and the keys/providers need to be structured as a if this then that, rather than manual entry per parameter. it should store both providers choises, keys, configs, models, etc, and a simple choise above both at top of a section is provider=requesty #[type requesty/openrouter and it'll know the settings from below cos both are configured]. it would be nice to have a dashboard served from the tailscale url and a folder, instead of reports, but where it shows me leaderboard, trades, positions, llm interpretations and links to the discord charts so i can validate and then something to show the queue or live trades that the traders have given or the agents have taken and the logic etc. we can build that later, the dashboard, but add it as the last step in the plan, "to plan a gui" but give it some of what we build, things to consider or your proposals on sections and content for the sections that i can see, and an easy to use page that has statistics dashboard and then some tabs with the details. non negotiable is the traders trades/predictions, etc... i want to filter per trader and then per trade if we can, where we can see all the details, including loading the chart in the screen side by side so i can compare the chart, and what messages surrounded it, the llm understandings and opinions and trends and patters, like i want overkill of fields and information so i can test the quality of the llm, its patterns it detected, its opinions, logic, scorings, wiethings, lpost mortums, and eeryhing thing that we're implimenting in the plan. for paterns in the chart, i dont want fixed categories or patterns, the LLM when interpreting the chart can reply with what it thinks the patterns or whatever it thinks, but it needs to be structured in such away that other obeservations on parterns from other coins/stocks etc, might return the same or simliar, and we can tidy or commonise/normalise later, but no hiting or suggesting or static patterns or trends or styles - the llm must detect. the py config file for sources and leader board and number 7 and 8 etc - lets make that a served html file actually, very basic where i can manage sources, but that i can also see statitics from the traders, trades, actually whatever i said above, where i get a html served in tailscale, iat the end of this implimentation i should be able to see whats happening with the llms and the traders and profits and losses and leaderboards and llm inferences and opinions etc, and measure the accuracy and start fine tuning. so it'll need that last in the phases. remember structured pattern taxonomny must be llm driven, not tags we give it, dynamic, system needs to learn/identify itself. dual logic resoning is good, love it. gaps are also really good. theres a @/shared/docs/NEW_TRADING_AGENT_HOWTO.md  on how to create agents for openclaw AND so that paperclip sees them, use it to create the agents. ensuer the soul.md etc are aligned with the file locations that openclaw needs, so i can see them in the interface. thememories are good, but remember the memU is for company/crosscompany wide. Mem0 for agents and experience, i think, you'll need to validate it with our current setup, but i want the principles of the plan enabled for memory anyway. open considerations section, ignore dataprivacy, its mine/my data and i consented, the traders are aware and consented and are part of the project.  if possible, lets keep tabs on all api calls and costs too, i know openrouter is meant to, but as part of this plan, find anything api related, and lets wire in a log/table that has explicit detail about the llm, provider, llm, agent that used it, or service, date/time, tokens, sent, received, cost up and/down or total, if all available, temparature, and all those things we might want to track with costs so its extremely granular. report auth, ignore that,