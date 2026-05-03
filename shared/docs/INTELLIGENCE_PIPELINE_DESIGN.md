# Intelligence Pipeline Architecture — Phase 3B+ Design

> **Status:** Draft — pending review before implementation  
> **Scope:** Unified ingestion, enrichment, interpretation, and action routing for all signal sources (Discord, Telegram, RSS, TradingView, manual).  
> **Companion docs:** `shared/ARCHITECTURE.md` (platform topology), `shared/docs/RULE1_END_TO_END.md` (backtest≡live flow)

---

## 1. Problem Statement

The current collector layer (`shared/collectors/`) ingests raw messages into `public.news_items` and `public.media_items`. The enrichment pipeline (`shared/enrichment/`) adds sentiment, relevance, and symbol detection. **What is missing:**

1. **No structured interpretation layer** — enrichment produces labels, but no "what does this mean for our positions?"
2. **No trader performance tracking** — we ingest trader calls but never score their accuracy
3. **No market-movement context** — a bullish call at 3am is different from one at NY open
4. **No dual-interpretation** — LLM sees text, but a quantitative model might see the same chart differently
5. **No global knowledge sharing** — insights from `#signals` in Discord server A never reach Telegram channel B
6. **No video/chart analysis** — screenshots and TradingView links are stored as media but never processed

This design closes those gaps with **three new tables**, **two new agents**, and **one new service**.

---

## 2. Assumptions & Constraints

| # | Assumption | Rationale |
|---|-----------|-----------|
| A1 | All signal sources write to `public.news_items` | Already true — Discord, Telegram, RSS, TradingView stubs all use `BaseCollector.write_to_db()` |
| A2 | `public.media_items` stores references, not processed data | Media processing (OCR, chart parsing) is async and may fail — we keep the raw reference |
| A3 | Multi-tenancy is per-company, but signal ingestion is shared | `tickles_shared.news_items` is global; interpretation is per-company via `company_id` in downstream tables |
| A4 | LLM calls are expensive — batch where possible | Interpretation runs on a cron, not per-message, to reduce API cost |
| A5 | TradingView MCP is conditional | If no MCP server is available, we fall back to screenshot + OCR pipeline |
| A6 | Position sizing never exceeds available balance | Inherited from `trading/sizer.py` — this design does not change that |

| # | Constraint | Impact |
|---|-----------|--------|
| C1 | 48 GB RAM VPS, 50 DB connections pooled | Batch enrichment, no per-message LLM calls |
| C2 | No Kubernetes — systemd only | New services are Python scripts with systemd units |
| C3 | Archive never delete | Superseded designs go to `_archive/` with git history |
| C4 | Backtest ≡ live (Rule 1) | Any new "signal → trade" path must have a shadow/validation twin |

---

## 3. Schema Extensions

### 3.1 `public.trader_profiles` — Shared Catalog

Tracks every human or bot trader whose calls we ingest. Lives in `tickles_shared` because trader identity is global.

```sql
CREATE TABLE public.trader_profiles (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    platform        varchar(32) NOT NULL,           -- 'discord', 'telegram', 'tradingview', 'manual'
    platform_user_id varchar(128) NOT NULL,           -- Discord user ID, Telegram username, etc.
    display_name    varchar(256) NOT NULL,
    avatar_url      text,
    bio             text,
    is_verified     boolean NOT NULL DEFAULT FALSE,  -- platform-verified (e.g. TV Pro)
    is_bot          boolean NOT NULL DEFAULT FALSE,   -- known bot account
    first_seen_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz NOT NULL DEFAULT now(),
    metadata        jsonb DEFAULT '{}',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    -- Composite unique: one row per platform+user
    CONSTRAINT uq_trader_platform_user UNIQUE (platform, platform_user_id)
);

CREATE INDEX idx_trader_profiles_platform ON public.trader_profiles (platform);
CREATE INDEX idx_trader_profiles_verified ON public.trader_profiles (is_verified) WHERE is_verified = TRUE;

COMMENT ON TABLE public.trader_profiles IS
    'Global catalog of signal sources. Populated by collectors on first sight. '
    'Used by performance analytics and reputation scoring.';
```

**Why this table:**
- Without it, every performance query is a `GROUP BY metadata->>'sender_id'` — slow and fragile
- Enables cross-platform identity resolution (same trader on Discord + Telegram)
- `is_bot` flag prevents us from benchmarking bots against humans unfairly

---

### 3.2 `public.signal_interpretations` — Per-Company

The bridge between raw news_items and trade intents. One row per news_item × company × interpretation run.

```sql
CREATE TABLE public.signal_interpretations (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    news_item_id        bigint NOT NULL REFERENCES public.news_items(id) ON DELETE CASCADE,
    company_id          bigint NOT NULL,              -- references tickles_shared.companies(id)
    run_id              varchar(64) NOT NULL,          -- interpretation batch run (e.g. 'interp-2026-04-25T07-00-00')

    -- Dual interpretation
    llm_raw_response    text,                        -- full LLM output (for audit)
    llm_direction       varchar(8),                   -- 'long' | 'short' | 'neutral' | 'unclear'
    llm_confidence      decimal(3,2),                 -- 0.00 .. 1.00
    llm_symbols         jsonb DEFAULT '[]',          -- [{symbol, exchange, confidence}]
    llm_rationale       text,                         -- summarized reasoning

    quant_direction     varchar(8),                   -- 'long' | 'short' | 'neutral' | 'unclear'
    quant_confidence    decimal(3,2),                 -- 0.00 .. 1.00
    quant_signals       jsonb DEFAULT '[]',          -- [{indicator, value, threshold, fired}]
    quant_rationale     text,                        -- e.g. "RSI14=72.3 > 70, bearish divergence"

    -- Consensus
    consensus_direction varchar(8),                   -- 'long' | 'short' | 'neutral' | 'conflict' | 'unclear'
    consensus_score     decimal(3,2),                 -- weighted blend of llm + quant
    action_recommended  varchar(32),                  -- 'open_long', 'close_short', 'hold', 'reduce', ...

    -- Market context at time of interpretation
    market_snapshot     jsonb DEFAULT '{}',           -- {btc_price, eth_price, funding_rate, vix_approx}
    session_tag         varchar(32),                   -- 'london_open', 'ny_am', 'asia', 'weekend'

    -- Rule-1: reproducibility
    param_hash          varchar(64) NOT NULL,         -- hash of interpretation parameters
    model_version       varchar(32) NOT NULL,         -- e.g. 'gpt-4o-2026-04-01'
    strategy_version_hash varchar(64),                 -- if this interpretation triggered a strategy run

    -- Freshness guard
    interpreted_at      timestamptz NOT NULL DEFAULT now(),
    market_data_lag_ms  int,                          -- how stale was the market snapshot?

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    -- Prevent duplicate interpretation of same item with same params
    CONSTRAINT uq_signal_interp_item_company_run UNIQUE (news_item_id, company_id, run_id)
);

CREATE INDEX idx_signal_interp_news_item ON public.signal_interpretations (news_item_id);
CREATE INDEX idx_signal_interp_company ON public.signal_interpretations (company_id);
CREATE INDEX idx_signal_interp_consensus ON public.signal_interpretations (consensus_direction, consensus_score);
CREATE INDEX idx_signal_interp_interpreted ON public.signal_interpretations (interpreted_at);
CREATE INDEX idx_signal_interp_action ON public.signal_interpretations (action_recommended) WHERE action_recommended IS NOT NULL;

COMMENT ON TABLE public.signal_interpretations IS
    'Per-company interpretation of a news_item. Dual-track: LLM reads text/chart, '
    'quant model reads market data. Consensus drives downstream action routing. '
    'One row per (news_item, company, run_id) — allows re-interpretation with new models.';
```

**Why dual interpretation:**
- LLMs hallucinate chart patterns; quant models miss narrative context
- `consensus='conflict'` is a signal in itself — means high uncertainty, reduce size or skip
- `param_hash` + `model_version` = full reproducibility for Rule-1 validation

---

### 3.3 `public.trader_performance` — Per-Company

Scores every trader whose calls we've tracked. Updated asynchronously after market moves.

```sql
CREATE TABLE public.trader_performance (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trader_profile_id   bigint NOT NULL REFERENCES public.trader_profiles(id) ON DELETE CASCADE,
    company_id          bigint NOT NULL,

    -- Time window
    window_start        date NOT NULL,
    window_end          date NOT NULL,
    window_type         varchar(16) NOT NULL DEFAULT 'rolling_30d', -- 'rolling_7d', 'rolling_30d', 'rolling_90d', 'all_time'

    -- Call statistics
    total_calls         int NOT NULL DEFAULT 0,
    directional_calls   int NOT NULL DEFAULT 0,      -- calls with clear long/short direction
    correct_direction   int NOT NULL DEFAULT 0,      -- direction matched subsequent 24h move
    correct_timing      int NOT NULL DEFAULT 0,      -- direction + entry within 2h of optimal

    -- P&L attribution (if we followed the calls)
    simulated_pnl       decimal(20,8),                -- paper-trading result of blindly following
    simulated_max_dd    decimal(20,8),                -- max drawdown during window
    simulated_sharpe    decimal(10,6),                -- Sharpe of simulated returns

    -- Risk metrics
    avg_leverage_called decimal(10,4),               -- average leverage recommended
    max_leverage_called decimal(10,4),               -- highest leverage seen
    stop_hit_rate       decimal(5,4),                -- fraction of calls where SL would have hit

    -- Quality scores
    accuracy_score      decimal(5,4),                -- correct_direction / directional_calls
    timing_score        decimal(5,4),                -- correct_timing / directional_calls
    risk_adjusted_score decimal(10,6),                -- accuracy / (1 + avg_leverage)
    composite_score     decimal(10,6),                -- weighted blend of all above

    -- Metadata
    last_call_at        timestamptz,
    param_hash          varchar(64) NOT NULL,         -- hash of scoring parameters
    calculated_at       timestamptz NOT NULL DEFAULT now(),

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    -- One row per (trader, company, window)
    CONSTRAINT uq_trader_perf_trader_company_window UNIQUE (trader_profile_id, company_id, window_type, window_start)
);

CREATE INDEX idx_trader_perf_trader ON public.trader_performance (trader_profile_id);
CREATE INDEX idx_trader_perf_company ON public.trader_performance (company_id);
CREATE INDEX idx_trader_perf_composite ON public.trader_performance (composite_score DESC) WHERE composite_score IS NOT NULL;
CREATE INDEX idx_trader_perf_window ON public.trader_performance (window_type, window_end);

COMMENT ON TABLE public.trader_performance IS
    'Per-company performance analytics for tracked traders. Updated by a background '
    'scoring job that compares historical calls against actual market movement. '
    'composite_score drives signal weighting in the position sizer.';
```

**Why this table:**
- Without performance tracking, every signal has equal weight — a 30% accuracy trader gets the same size as a 70% trader
- `simulated_pnl` is paper-only; never uses live balance
- `param_hash` ensures scoring methodology is versioned and reproducible

---

## 4. New Services & Agents

### 4.1 `InterpretationService` — `shared/intelligence/interpretation_service.py`

**Type:** Background daemon (systemd unit: `tickles-interpretation.service`)  
**Frequency:** Every 5 minutes (configurable)  
**Input:** `news_items` where `enriched_at IS NOT NULL` and no `signal_interpretations` row exists for this company  
**Output:** `signal_interpretations` rows

**Pipeline:**

```
news_items (enriched)
    │
    ▼
┌─────────────────┐
│ Batch Fetcher   │  ← grabs N items, joins media_items for chart URLs
│ (last 5 min)    │
└────────┬────────┘
         │
    ┌────┴────┐
    ▼         ▼
┌────────┐  ┌─────────────┐
│ LLM    │  │ Quant Model │  ← runs in parallel
│ Track  │  │ Track       │
└───┬────┘  └──────┬──────┘
    │               │
    ▼               ▼
┌─────────────────────────┐
│ Consensus Engine        │  ← weighted blend; conflict detection
│ (direction, score,      │
│  action_recommended)    │
└───────────┬─────────────┘
            │
            ▼
    signal_interpretations
```

**LLM Track:**
- Prompt includes: headline, content, any chart images (base64), market snapshot (BTC/ETH price, funding)
- Model: configurable via `INTERPRETATION_LLM_MODEL` env var (default: `gpt-4o-mini`)
- Output structured JSON: `{direction, confidence, symbols[], rationale}`

**Quant Track:**
- Reads latest candles for symbols detected by enrichment
- Runs a lightweight rule set: RSI threshold, EMA cross, funding rate extreme
- Output: `{direction, confidence, signals[], rationale}`

**Consensus Engine:**
- If LLM and quant agree → `consensus_direction = agreed`, `consensus_score = weighted_avg`
- If they disagree → `consensus_direction = 'conflict'`, `consensus_score = min(llm_confidence, quant_confidence)`
- `action_recommended` mapped via config table `interpretation_action_map`

---

### 4.2 `ChartHacker` — OpenClaw-Native Agent

**Type:** OpenClaw-registered agent with workspace + cron (NOT a standalone Python script)
**Registration:** `openclaw agents add chart_hacker --workspace /root/.openclaw/workspace/chart_hacker --model openrouter/google/gemini-2.0-flash`
**Cron:** `openclaw cron add --agent chart_hacker --cron '* * * * *' --tools read,write,exec --session isolated`

**Purpose:** Extract structured data from chart screenshots and TradingView links

**Workspace files:**
- `SOUL.md` — ChartHacker strategy: how to read chart images, what to extract, output schema
- `TRADE_STATE.md` — Last-run status, pending queue cursor
- `TRADE_LOG.md` — Append-only log of analyses performed

**Pipeline:**

```
media_items (pending image)
    │
    ▼
┌─────────────────────────┐
│ InterpretationService │  ← detects pending chart images, writes trigger row
│ (systemd daemon)        │    to public.chart_analysis_queue
└────────┬────────────────┘
         │
         ▼
┌─────────────────────────┐
│ ChartHacker cron        │  ← every minute, reads queue, fetches image
│ (OpenClaw-managed)      │
└────────┬────────────────┘
         │
         ▼
┌─────────────────────────┐
│ URL Resolver            │  ← TradingView /x/ URLs → direct S3 PNG
│                         │  ← Discord CDN URLs → download to local
└────────┬────────────────┘
         │
         ▼
┌─────────────────────────┐
│ Vision LLM (OpenClaw)   │  ← gemini-2.0-flash via OpenRouter
│                         │  ← Extract: symbol, timeframe, indicators, drawn levels
└────────┬────────────────┘
         │
         ▼
┌─────────────────────────┐
│ Structured Output         │  ← Parse vision output into JSON schema
│                         │  ← {symbol, timeframe, indicators, levels}
└────────┬────────────────┘
         │
         ▼
    media_items.metadata.chart_hacker_result
    chart_analysis_queue.status = 'completed'
    TRADE_LOG.md (appended)
```

**Why an OpenClaw agent, not a standalone script:**
- All agents must be created through Paperclip/OpenClaw — no rogue agents
- Vision LLM calls are ~10× more expensive than text LLM calls; the cron scoping (`--tools read,write,exec`) prevents tool-menu bloat
- The agent reads its strategy from `SOUL.md` — same pattern as trading agents
- Can be disabled by `openclaw cron disable` without touching code
- Per-company: each company can have its own ChartHacker workspace with different `SOUL.md` rules

**TradingView MCP Fallback:**
- If `TRADINGVIEW_MCP_URL` env var is set, the InterpretationService calls MCP first
- If MCP unavailable or returns error, the ChartHacker agent falls back to vision LLM
- Both approaches are logged in `TRADE_LOG.md` for accuracy comparison over time

---

### 4.3 `PerformanceScorer` — `shared/intelligence/performance_scorer.py`

**Type:** Background daemon (systemd unit: `tickles-performance-scorer.service`)  
**Frequency:** Daily at 00:00 UTC  
**Input:** `news_items` from last N days with `source IN ('discord', 'telegram', 'tradingview')`  
**Output:** `trader_performance` rows

**Scoring Logic:**

```python
def score_call(news_item, market_data_24h_later) -> dict:
    """
    1. Extract direction from news_item (via enrichment sentiment + content keywords)
    2. Compare to actual 24h price movement of mentioned symbols
    3. Return: {correct_direction, pnl_if_followed, optimal_entry, optimal_exit}
    """
```

**Why daily batch:**
- 24h forward-looking window means we need to wait for market to move
- Batch scoring at midnight avoids intra-day noise
- `param_hash` captures the exact scoring parameters for reproducibility

---

## 5. Data Flow — End to End

```
┌─────────────┐   ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
│  Discord    │   │  Telegram   │   │    RSS      │   │ TradingView │
│  Collector  │   │  Collector  │   │  Collector  │   │   Monitor   │
└──────┬──────┘   └──────┬──────┘   └──────┬──────┘   └──────┬──────┘
       │                 │                 │                 │
       └─────────────────┴─────────────────┴─────────────────┘
                           │
                           ▼
                   ┌───────────────┐
                   │  news_items   │  ← shared, deduplicated by hash_key
                   │  media_items  │  ← pending processing
                   └───────┬───────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
       ┌──────────┐ ┌──────────┐ ┌──────────┐
       │Enrichment│ │ ChartHacker│ │Performance│
       │ Pipeline │ │  Agent    │ │  Scorer   │
       │(sentiment│ │ (charts)  │ │ (daily)   │
       │symbols)  │ │           │ │           │
       └────┬─────┘ └────┬─────┘ └────┬─────┘
            │            │            │
            ▼            ▼            ▼
   ┌─────────────────────────────────────────┐
   │      InterpretationService              │
   │  (LLM track + Quant track + Consensus)  │
   └──────────────────┬──────────────────────┘
                      │
                      ▼
           ┌─────────────────────┐
           │ signal_interpretations│
           └──────────┬──────────┘
                      │
         ┌────────────┼────────────┐
         ▼            ▼            ▼
   ┌──────────┐ ┌──────────┐ ┌──────────┐
   │  MemU    │ │  OMS     │ │  Alert   │
   │ (memory) │ │ (trade)  │ │ (notify) │
   │          │ │ intent   │ │ CEO      │
   └──────────┘ └──────────┘ └──────────┘
```

---

## 6. Multi-Tenancy Rules

| Layer | Shared vs Per-Company | Mechanism |
|-------|----------------------|-----------|
| `news_items` | **Shared** | All collectors write here; no company_id |
| `media_items` | **Shared** | Linked to news_items; no company_id |
| `trader_profiles` | **Shared** | Global catalog; populated on first sight |
| `signal_interpretations` | **Per-Company** | `company_id` column; each company gets its own interpretation |
| `trader_performance` | **Per-Company** | `company_id` column; scoring parameters may differ per company |
| `ChartHacker` | **Per-Company** | OpenClaw agent per company workspace; capability-granted |
| `InterpretationService` | **Per-Company batch** | One run per active company |

**Cross-company knowledge sharing:**
- `trader_profiles` is global — if a trader is accurate for Company A, Company B sees the same profile
- `trader_performance` is per-company — Company A may weight timing more than Company B
- A `shared_insights` MemU namespace (container=`shared`) allows agents to publish anonymized patterns

---

## 7. What Could Go Wrong

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| LLM API rate limit | Medium | Interpretation backlog | Exponential backoff; fallback to quant-only |
| Vision LLM cost explosion | Medium | Budget overrun | Hard daily token limit; disable ChartHacker per-company |
| TradingView MCP unavailable | Low | Chart parsing fails | Fallback to vision LLM; both logged for comparison |
| Trader profile spam (fake accounts) | Medium | Performance pollution | `is_verified` gate; minimum 10 calls before scoring |
| Consensus always = 'conflict' | Low | No actionable signals | Tune quant model weights; add human review queue |
| 24h scoring window too noisy | Medium | Accuracy scores meaningless | Make window configurable (48h, 1w); A/B test |
| Media download fills disk | Medium | VPS out of space | Retention: 30 days local, then archive to cold storage |
| Duplicate interpretations | Low | DB bloat | `uq_signal_interp_item_company_run` constraint |

---

## 8. Implementation Order

| Priority | Task | File / Location | Effort |
|----------|------|-----------------|--------|
| **P0** | Create `trader_profiles` table + seed from existing news_items metadata | `shared/migration/2026_04_25_phase3b_trader_profiles.sql` | 2h |
| **P0** | Create `signal_interpretations` table | `shared/migration/2026_04_25_phase3b_signal_interpretations.sql` | 2h |
| **P0** | Create `trader_performance` table | `shared/migration/2026_04_25_phase3b_trader_performance.sql` | 2h |
| **P1** | Build `InterpretationService` skeleton (batch fetcher + consensus engine) | `shared/intelligence/interpretation_service.py` | 1d |
| **P1** | Implement LLM track (prompt template + structured output parser) | `shared/intelligence/llm_track.py` | 1d |
| **P1** | Implement Quant track (lightweight indicator rules) | `shared/intelligence/quant_track.py` | 1d |
| **P2** | Create ChartHacker OpenClaw agent workspace + SOUL.md + cron | `/root/.openclaw/workspace/chart_hacker/` | 1d |
| **P2** | Build `PerformanceScorer` (daily batch job) | `shared/intelligence/performance_scorer.py` | 1d |
| **P2** | Wire collectors to upsert `trader_profiles` on first sight | `shared/collectors/base.py` + per-collector | 4h |
| **P3** | Add systemd units for new services | `systemd/tickles-interpretation.service` | 2h |
| **P3** | Smoke tests + Rule-1 validation pairing | `shared/tests/test_intelligence_pipeline.py` | 1d |
| **P4** | Update `CLAUDE.md` with new components | `CLAUDE.md` | 1h |

---

## 9. Self-Critique (Devil's Advocate)

> *"What would a senior engineer critique about this design?"*

1. **Three new tables is a lot.** Could we collapse `signal_interpretations` into `news_items.metadata`?  
   → No — `news_items` is shared; interpretations are per-company. JSONB would explode with N companies.

2. **Why not just use MemU for everything instead of new SQL tables?**  
   → MemU is for agent memory (unstructured, vector-searchable). These tables are for structured analytics, joins, and time-series queries — SQL is correct.

3. **The quant track is underspecified.** What indicators? What thresholds?  
   → Intentionally lightweight for P1. The `quant_signals` JSONB allows iterative refinement without schema changes. A registry table `quant_signal_definitions` can be added later.

4. **Daily scoring is too slow.** What if a trader goes on a hot streak?  
   → `rolling_7d` window exists. We can add intra-day scoring for high-frequency traders without schema changes.

5. **ChartHacker is an OpenClaw agent — why not a standalone Python script?**
   → All agents must be created through Paperclip/OpenClaw to prevent rogue agents. The OpenClaw cron model (`--tools read,write,exec`) provides tool scoping, session isolation, and disable/enable without code changes. Chart parsing is judgement (vision LLM), so it belongs in an agent, but it must be an OpenClaw-native agent following the same workspace + SOUL.md + cron pattern as trading agents.

6. **No mention of GDPR / data retention for trader profiles.**  
   → `trader_profiles` only stores public data (display names from public Discord/Telegram). No PII. Retention: profiles are kept but `is_active` can be set FALSE if a trader requests removal.

---

*End of design document. Ready for review before implementation.*
