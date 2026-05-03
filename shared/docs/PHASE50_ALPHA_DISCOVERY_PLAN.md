# Alpha Discovery & Chart Analysis — Comprehensive Architecture Plan v2

> **Phase:** 50 (post-bug-hunt, pre-Docker)  
> **Status:** Architecture plan — ready for implementation  
> **Target:** ChartHackers Discord + Telegram traders  
> **Storage:** Postgres (tickles_shared) — OLTP workload, JSONB flexibility  
> **TradingView MCP:** Researched, conditionally integrated (plan works with or without it)

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Target Users & Channels](#2-target-users--channels)
3. [Unified Ingestion Pipeline](#3-unified-ingestion-pipeline)
4. [Database Schema](#4-database-schema)
5. [Media Processing](#5-media-processing)
6. [Chart Analysis (Vision LLM)](#6-chart-analysis-vision-llm)
7. [TradingView MCP Integration (Conditional)](#7-tradingview-mcp-integration-conditional)
8. [Voice & Audio](#8-voice--audio)
9. [Video Placeholder](#9-video-placeholder)
10. [Deduplication & Trade Grouping](#10-deduplication--trade-grouping)
11. [Trade Stage Tracking](#11-trade-stage-tracking)
12. [Market Movement Analytics](#12-market-movement-analytics)
13. [Dual Interpretation (Trader vs LLM)](#13-dual-interpretation-trader-vs-llm)
14. [Reply & Thread Context](#14-reply--thread-context)
15. [ChartHacker Agent](#15-charthacker-agent)
16. [Trader Performance Analytics](#16-trader-performance-analytics)
17. [Global Knowledge Sharing](#17-global-knowledge-sharing)
18. [MCP Tools](#18-mcp-tools)
19. [Systemd Services](#19-systemd-services)
20. [Implementation Roadmap](#20-implementation-roadmap)
21. [Cost Estimate](#21-cost-estimate)
22. [What Could Go Wrong](#22-what-could-go-wrong)
23. [Self-Check](#23-self-check)

---

## 1. Executive Summary

We are building a unified alpha discovery system that monitors trading communities across Discord and Telegram (extensible to any platform). The system:

- **Ingests** messages, images, voice notes, links, and (future) videos
- **Analyzes** chart screenshots via vision LLM for technical setups
- **Transcribes** voice notes to text
- **Tracks** trade ideas from post through to outcome (hit entry, stopped out, profit taken)
- **Measures** trader performance (win rate, direction accuracy, market movement analysis)
- **Provides** dual interpretation: what the trader explicitly stated vs what the LLM independently sees
- **Shares** all intelligence as queryable knowledge for all trading agents
- **Deploys** a dedicated ChartHacker agent that watches traders and validates their setups via backtest/forward-test

**Key principle:** The plan works with or without TradingView MCP integration. If TV MCP is available, it enhances chart analysis with multi-timeframe context and custom indicators. If not, vision-only analysis is sufficient.

---

## 2. Target Users & Channels

### 2.1 Users to Watch (Dynamic — On/Off Switchable)

| Username | Platform | Priority | Notes |
|----------|----------|----------|-------|
| `thelordofentry` (Dylan) | Discord | High | Multiple groups, daily updates |
| `emutrading` (EMU/Trader J) | Discord | High | Skills Traders group |
| `degendavidd` (Degen David) | Discord | High | Charts and setups |
| `chaos` | Discord | High | Charts and setups |
| `panda` | Discord | High | Skills Traders group |
| `thenagel` (The Nagel) | Discord | High | Skills Traders group |

**Dynamic control:** Each user has an `is_monitored` flag in `alpha_user_discovery`. Can be toggled via MCP tool or system_config.

### 2.2 Channels to Watch (Explicit List + Auto-Discovery)

**Lord of Entry Groups:**
- Daily Market Updates
- Old Coin Trading Setups
- Stonks Setups (CFDs)
- Metals & Commodities
- When Entry (entry timing — high priority)

**Other Groups:**
- Chaos: Charts and Setups
- Degen David: Charts and Setups
- Skills Traders: Panda Trades, Trader J Trades, Nagel Trades
- Charts Prime: Charts and Setups
- Media & Studio: Live Show Charts, Zoom Charts
- Trading Zone: General chat (stopouts, trade mentions, replies)

**Channel monitoring is dynamic:** Channels can be added/removed via `alpha_user_discovery` table. The `monitor_reason` field distinguishes `manual_add` from `auto_discovered`.

### 2.3 Discovery Strategy

```
Phase 1: Seed known channels (from explicit list above)
Phase 2: Auto-discover new channels where target users post
Phase 3: Cross-reference — if a channel has >3 target users, auto-monitor even if not explicitly listed
```

---

## 3. Unified Ingestion Pipeline

### 3.1 Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    Unified Alpha Ingestion Pipeline                            │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  Discord ──▶ ┌─────────────┐    Telegram ──▶ ┌─────────────┐              │
│  Self-Bot    │  Discord      │    Bot/Client    │  Telegram     │              │
│              │  Collector    │                  │  Collector    │              │
│              │  (existing)   │                  │  (existing)   │              │
│              └──────┬──────┘                  └──────┬──────┘              │
│                     │                                 │                      │
│                     └─────────────┬───────────────────┘                      │
│                                   ▼                                          │
│                          ┌─────────────────┐                               │
│                          │  Message Router   │                               │
│                          │  (NEW)            │                               │
│                          │                   │                               │
│                          │ • Normalize       │                               │
│                          │ • Classify media  │                               │
│                          │ • Route to        │                               │
│                          │   processors      │                               │
│                          └────────┬────────┘                               │
│                                   │                                        │
│         ┌────────────────────────┼────────────────────────┐              │
│         ▼                        ▼                        ▼              │
│  ┌─────────────┐        ┌─────────────┐        ┌─────────────────┐      │
│  │ Text Only   │        │ Has Media   │        │ Has Links       │      │
│  │             │        │             │        │                 │      │
│  │ → Direct    │        │ → Media     │        │ → Link Resolver │      │
│  │   to DB     │        │   Processor │        │   & Filter      │      │
│  └─────────────┘        └─────────────┘        └─────────────────┘      │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 3.2 Message Classification

Every incoming message is classified by the router:

```python
class MessageType(Enum):
    TEXT_ONLY = "text_only"           # Plain text, no media, no links
    CHART_IMAGE = "chart_image"       # Image attachment or embed (likely chart)
    PHOTO = "photo"                   # Image but not a chart (meme, selfie, etc.)
    VOICE_NOTE = "voice_note"         # Audio/voice attachment
    VIDEO_ATTACHMENT = "video_attachment"  # Video file (Discord CDN)
    VIDEO_LINK = "video_link"         # YouTube, TikTok, etc. link
    TRADINGVIEW_LINK = "tradingview_link"  # tradingview.com/x/... or /chart/...
    OTHER_LINK = "other_link"         # Any other URL
    MIXED = "mixed"                   # Multiple types in one message
```

**Classification logic:**
- If message has image attachment → `CHART_IMAGE` if filename contains chart keywords or if text mentions trading terms; else `PHOTO`
- If message has audio attachment → `VOICE_NOTE`
- If message has video attachment → `VIDEO_ATTACHMENT` (placeholder — stored but not processed yet)
- If message contains URLs → classify each URL separately
- If multiple types → `MIXED` — split into sub-messages for processing

### 3.3 Link Resolution & Filtering

```python
def resolve_links(text: str) -> LinkResolution:
    """Extract and classify all URLs in message text."""
    urls = re.findall(r'https?://\S+', text)
    
    result = {
        "tradingview": [],      # Allowed — fetch chart snapshot
        "discord_cdn": [],      # Allowed — media download
        "youtube": [],          # Ignored for now (video placeholder)
        "tiktok": [],           # Ignored for now
        "twitter_x": [],        # Ignored for now
        "other": [],            # Logged but not processed
    }
    
    for url in urls:
        domain = extract_domain(url)
        if domain in ("tradingview.com", "www.tradingview.com"):
            result["tradingview"].append(url)
        elif domain in ("cdn.discordapp.com", "media.discordapp.net"):
            result["discord_cdn"].append(url)
        elif domain in ("youtube.com", "youtu.be", "tiktok.com", "twitter.com", "x.com"):
            result[domain.split(".")[0]].append(url)
        else:
            result["other"].append(url)
            logger.info("[LinkFilter] Unclassified link from %s: %s", author, url)
    
    return result
```

**TradingView links:** Extract the chart ID, fetch a snapshot via TradingView's public image API (or MCP server if available), treat as `CHART_IMAGE`.

**Other links:** Logged to `alpha_link_log` table for manual review. Not processed.

---

## 4. Database Schema

### 4.1 New Tables Overview

| Table | Purpose | Rows/Day (est.) |
|-------|---------|-----------------|
| `alpha_ingestion_log` | Every message ingested, with classification | ~500 |
| `alpha_analyses` | LLM analysis results (chart, voice, text) | ~150 |
| `alpha_dedup_signals` | Trade deduplication anchors | ~30 |
| `alpha_trade_groups` | Many-to-many: analyses ↔ signals | ~200 |
| `alpha_stage_tracker` | Trade stage snapshots over time | ~2,400 |
| `alpha_user_discovery` | User-to-channel mapping | ~50 (mostly static) |
| `alpha_link_log` | Unclassified links for review | ~20 |
| `alpha_trader_stats` | Aggregated trader performance | ~5 (one per trader) |
| `alpha_market_movement` | Price movement away from entry | ~500 |
| `alpha_reply_context` | Reply/thread relationships | ~100 |

### 4.2 `alpha_ingestion_log` — Unified ingestion record

```sql
-- ============================================================================
-- alpha_ingestion_log — Every message ingested from any platform
-- ============================================================================
-- One row per message. This is the "source of truth" for all incoming alpha.
-- Links to news_items (existing) for backward compatibility.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.alpha_ingestion_log (
    id                  BIGSERIAL PRIMARY KEY,
    
    -- Source platform
    platform            VARCHAR(32) NOT NULL
        CHECK (platform IN ('discord', 'telegram', 'rss', 'api', 'webhook')),
    platform_message_id VARCHAR(128) NOT NULL,  -- Discord message ID or Telegram message_id
    
    -- Author
    author_username     VARCHAR(255) NOT NULL,
    author_id           VARCHAR(128),           -- Discord snowflake or Telegram user_id
    author_display_name VARCHAR(255),           -- Nickname if different from username
    
    -- Channel/Group
    channel_id          VARCHAR(128) NOT NULL,
    channel_name        VARCHAR(255),
    server_id           VARCHAR(128),           -- Discord guild ID
    server_name         VARCHAR(255),
    
    -- Message content
    message_type        VARCHAR(32) NOT NULL
        CHECK (message_type IN (
            'text_only', 'chart_image', 'photo', 'voice_note',
            'video_attachment', 'video_link', 'tradingview_link',
            'other_link', 'mixed'
        )),
    raw_text            TEXT,                   -- Original message text
    cleaned_text        TEXT,                   -- Text after mention/link resolution
    
    -- Media references (link to media_items)
    has_media           BOOLEAN NOT NULL DEFAULT FALSE,
    media_count         SMALLINT NOT NULL DEFAULT 0,
    
    -- Links found in message
    links_found         JSONB,                  -- {tradingview: [], youtube: [], other: []}
    
    -- Reply/thread context
    reply_to_message_id VARCHAR(128),             -- If this is a reply
    reply_to_author     VARCHAR(255),             -- Who they're replying to
    thread_id           VARCHAR(128),             -- Discord thread ID
    
    -- Timestamps
    posted_at           TIMESTAMPTZ(3) NOT NULL,
    collected_at        TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    -- Processing status
    processing_status   VARCHAR(32) NOT NULL DEFAULT 'pending'
        CHECK (processing_status IN (
            'pending', 'text_enriched', 'media_processing',
            'analyzed', 'deduped', 'staged', 'completed', 'failed', 'ignored'
        )),
    processing_error    TEXT,
    
    -- Link to existing news_items (for backward compatibility)
    news_item_id        BIGINT
        REFERENCES public.news_items(id) ON DELETE SET NULL,
    
    -- Metadata
    metadata            JSONB,                  -- Platform-specific extras
    
    created_at          TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    -- Unique per platform message
    CONSTRAINT uq_ingestion_platform_msg UNIQUE (platform, platform_message_id)
);

CREATE INDEX IF NOT EXISTS idx_ingestion_author ON public.alpha_ingestion_log (author_username);
CREATE INDEX IF NOT EXISTS idx_ingestion_channel ON public.alpha_ingestion_log (channel_id, posted_at);
CREATE INDEX IF NOT EXISTS idx_ingestion_type ON public.alpha_ingestion_log (message_type);
CREATE INDEX IF NOT EXISTS idx_ingestion_status ON public.alpha_ingestion_log (processing_status);
CREATE INDEX IF NOT EXISTS idx_ingestion_posted ON public.alpha_ingestion_log (posted_at);
CREATE INDEX IF NOT EXISTS idx_ingestion_reply ON public.alpha_ingestion_log (reply_to_message_id);
CREATE INDEX IF NOT EXISTS idx_ingestion_news ON public.alpha_ingestion_log (news_item_id);

DROP TRIGGER IF EXISTS trg_alpha_ingestion_updated ON public.alpha_ingestion_log;
CREATE TRIGGER trg_alpha_ingestion_updated
    BEFORE UPDATE ON public.alpha_ingestion_log
    FOR EACH ROW EXECUTE FUNCTION public.trg_set_updated_at();
```

### 4.3 `alpha_analyses` — LLM analysis results (expanded)

```sql
-- ============================================================================
-- alpha_analyses — Chart/image/voice/text analysis results from LLM
-- ============================================================================
-- Expanded from v1 with dual interpretation fields and confidence scores.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.alpha_analyses (
    id                  BIGSERIAL PRIMARY KEY,
    ingestion_log_id    BIGINT NOT NULL
        REFERENCES public.alpha_ingestion_log(id) ON DELETE CASCADE,
    media_item_id       BIGINT
        REFERENCES public.media_items(id) ON DELETE SET NULL,

    analysis_type       VARCHAR(32) NOT NULL
        CHECK (analysis_type IN (
            'chart_image', 'voice_transcription', 'text_context',
            'link_preview', 'tradingview_snapshot'
        )),

    -- LLM call metadata (Rule #1: reproducibility)
    model_id            VARCHAR(100) NOT NULL,
    provider            VARCHAR(50) NOT NULL,
    prompt_version_hash CHAR(64) NOT NULL,
    prompt_params       JSONB,

    -- Raw LLM output
    raw_response        JSONB,

    -- =========================================================================
    -- DUAL INTERPRETATION — The core of the analysis
    -- =========================================================================
    -- 
    -- trader_stated: What the trader EXPLICITLY said or showed on the chart.
    -- This is extracted from text, voice transcription, or chart annotations.
    -- If the trader didn't state something (e.g., no take profit shown), it's null.
    --
    -- llm_interpreted: What the LLM INDEPENDENTLY sees and interprets.
    -- This is the LLM's own analysis of the chart/image.
    -- It may differ from trader_stated — that's valuable signal.
    --
    -- Both are stored as JSONB with the same schema structure.
    -- =========================================================================

    trader_stated       JSONB,  -- Trader's explicit statements
    trader_confidence   NUMERIC(3,2),  -- 0.0-1.0: how confident we are this is what the trader said

    llm_interpreted     JSONB,  -- LLM's independent interpretation
    llm_confidence      NUMERIC(3,2),  -- 0.0-1.0: LLM's confidence in its interpretation

    -- Structured extraction (merged/resolved view)
    extracted           JSONB,  -- See schema below

    -- Processing metadata
    processing_status   VARCHAR(32) NOT NULL DEFAULT 'pending'
        CHECK (processing_status IN ('pending', 'running', 'completed', 'failed', 'discarded')),
    processing_error    TEXT,
    processing_duration_ms INT,

    -- Cost tracking
    tokens_in           INT DEFAULT 0,
    tokens_out          INT DEFAULT 0,
    cost_usd            NUMERIC(10,6) DEFAULT 0,

    -- Deduplication linkage
    dedup_signal_id     BIGINT
        REFERENCES public.alpha_dedup_signals(id) ON DELETE SET NULL,

    created_at          TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_alpha_analyses_ingestion ON public.alpha_analyses (ingestion_log_id);
CREATE INDEX IF NOT EXISTS idx_alpha_analyses_media ON public.alpha_analyses (media_item_id);
CREATE INDEX IF NOT EXISTS idx_alpha_analyses_type ON public.alpha_analyses (analysis_type);
CREATE INDEX IF NOT EXISTS idx_alpha_analyses_status ON public.alpha_analyses (processing_status);
CREATE INDEX IF NOT EXISTS idx_alpha_analyses_dedup ON public.alpha_analyses (dedup_signal_id);
CREATE INDEX IF NOT EXISTS idx_alpha_analyses_created ON public.alpha_analyses (created_at);
CREATE INDEX IF NOT EXISTS idx_alpha_analyses_extracted_gin ON public.alpha_analyses USING gin (extracted);
CREATE INDEX IF NOT EXISTS idx_alpha_analyses_trader_gin ON public.alpha_analyses USING gin (trader_stated);
CREATE INDEX IF NOT EXISTS idx_alpha_analyses_llm_gin ON public.alpha_analyses USING gin (llm_interpreted);

DROP TRIGGER IF EXISTS trg_alpha_analyses_updated ON public.alpha_analyses;
CREATE TRIGGER trg_alpha_analyses_updated
    BEFORE UPDATE ON public.alpha_analyses
    FOR EACH ROW EXECUTE FUNCTION public.trg_set_updated_at();
```

**`trader_stated` / `llm_interpreted` / `extracted` JSONB schema:**

```json
{
  "symbol": "BTC/USDT",
  "direction": "long",
  "timeframe": "4h",
  "regime": "trending_bullish",
  
  "entry": {
    "price": 65000.00,
    "type": "limit",
    "confidence": 0.85,
    "source": "trader_stated"  // or "llm_interpreted"
  },
  "stop_loss": {
    "price": 64000.00,
    "type": "fixed",
    "confidence": 0.90,
    "source": "trader_stated"
  },
  "take_profits": [
    {"price": 67000.00, "label": "TP1", "confidence": 0.75, "source": "llm_interpreted"}
  ],
  
  "technicals": {
    "order_blocks": [{"type": "bullish", "price_range": [64800, 65200]}],
    "support_resistance": [{"level": 64000, "type": "support", "strength": "strong"}],
    "fib_levels": [0.382, 0.5, 0.618],
    "anchored_vwap": {"price": 65500, "anchor_date": "2026-04-20"},
    "trend_lines": [{"slope": "up", "from": [64000, "2026-04-15"], "to": [65000, "2026-04-24"]}]
  },
  
  "trade_stage": "waiting_for_entry",
  "chart_timestamp": "2026-04-24T14:30:00Z",
  
  "current_price_context": {
    "price_at_post_time": 65100.00,
    "distance_to_entry_pct": 0.15,
    "price_moving_toward_entry": true
  },
  
  "notes": "Price consolidating at support, expecting breakout",
  "llm_reasoning": "The LLM sees a bullish order block at 64800-65200 with price currently testing support. The trader has marked entry at 65000 which is within the order block. However, the LLM notes that volume is declining and suggests waiting for a confirmed breakout above 65200 before entering."
}
```

### 4.4 `alpha_dedup_signals` — Trade deduplication anchor

```sql
-- Same as v1 (see PHASE50_ALPHA_DISCOVERY_PLAN.md section 4.2)
-- Added: trader_performance_id for linking to stats

ALTER TABLE public.alpha_dedup_signals
    ADD COLUMN IF NOT EXISTS trader_performance_id BIGINT
        REFERENCES public.alpha_trader_stats(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_dedup_performance ON public.alpha_dedup_signals (trader_performance_id);
```

### 4.5 `alpha_stage_tracker` — Trade stage evolution

```sql
-- Same as v1 (see PHASE50_ALPHA_DISCOVERY_PLAN.md section 4.4)
-- Added: market_movement tracking fields

ALTER TABLE public.alpha_stage_tracker
    ADD COLUMN IF NOT EXISTS distance_from_entry_pct NUMERIC(10,4),
    ADD COLUMN IF NOT EXISTS distance_from_sl_pct NUMERIC(10,4),
    ADD COLUMN IF NOT EXISTS distance_from_tp_pct NUMERIC(10,4),
    ADD COLUMN IF NOT EXISTS candles_since_post INT,
    ADD COLUMN IF NOT EXISTS market_direction_vs_trade VARCHAR(10)
        CHECK (market_direction_vs_trade IN ('toward_entry', 'away_from_entry', 'sideways', 'unknown'));
```

### 4.6 `alpha_market_movement` — Price movement away from entry over time

```sql
-- ============================================================================
-- alpha_market_movement — Track how far price moved from entry over time
-- ============================================================================
-- This answers: "The trader posted an entry at 65K. How far did price move
-- away from it? For how long? Was it a gradual 10% over 2 weeks or a sudden
-- 100% pump in 3 days?"
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.alpha_market_movement (
    id                  BIGSERIAL PRIMARY KEY,
    dedup_signal_id     BIGINT NOT NULL
        REFERENCES public.alpha_dedup_signals(id) ON DELETE CASCADE,
    
    -- Snapshot timing
    snapshot_at         TIMESTAMPTZ(3) NOT NULL,
    hours_since_post    NUMERIC(10,2) NOT NULL,  -- Fractional hours
    
    -- Price data at snapshot
    current_price       NUMERIC(20,8) NOT NULL,
    entry_price         NUMERIC(20,8),
    stop_loss_price     NUMERIC(20,8),
    take_profit_price   NUMERIC(20,8),
    
    -- Movement metrics
    distance_from_entry_pct NUMERIC(10,4),  -- (current - entry) / entry * 100
    distance_from_sl_pct    NUMERIC(10,4),  -- (current - sl) / sl * 100
    distance_from_tp_pct    NUMERIC(10,4),  -- (current - tp) / tp * 100
    
    -- Direction analysis
    market_direction    VARCHAR(10)
        CHECK (market_direction IN ('toward_entry', 'away_from_entry', 'sideways', 'hit_entry', 'hit_sl', 'hit_tp')),
    
    -- Regime at snapshot (from our regime classifier)
    detected_regime     VARCHAR(50),
    regime_confidence   NUMERIC(3,2),
    
    -- Candle data summary
    high_since_post     NUMERIC(20,8),
    low_since_post      NUMERIC(20,8),
    volume_avg_since_post NUMERIC(30,8),
    
    created_at          TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_market_movement_signal ON public.alpha_market_movement (dedup_signal_id);
CREATE INDEX IF NOT EXISTS idx_market_movement_snapshot ON public.alpha_market_movement (snapshot_at);
CREATE INDEX IF NOT EXISTS idx_market_movement_hours ON public.alpha_market_movement (hours_since_post);
```

### 4.7 `alpha_trader_stats` — Aggregated trader performance

```sql
-- ============================================================================
-- alpha_trader_stats — Per-trader performance analytics
-- ============================================================================
-- Updated continuously by the ChartHacker agent.
-- Answers: "Is this trader profitable? What's their win rate? How often
-- does the market move in their predicted direction?"
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.alpha_trader_stats (
    id                  BIGSERIAL PRIMARY KEY,
    
    -- Trader identity
    trader_username     VARCHAR(255) NOT NULL,
    trader_id           VARCHAR(128),
    platform            VARCHAR(32) NOT NULL,
    
    -- Time window
    period_from         DATE NOT NULL,
    period_to           DATE NOT NULL,
    
    -- Trade counts
    total_signals       INT NOT NULL DEFAULT 0,
    trades_taken        INT NOT NULL DEFAULT 0,      -- Entry was hit
    trades_not_taken    INT NOT NULL DEFAULT 0,      -- Entry never hit (expired)
    stopped_out         INT NOT NULL DEFAULT 0,
    profit_taken_full   INT NOT NULL DEFAULT 0,
    profit_taken_partial INT NOT NULL DEFAULT 0,
    still_open          INT NOT NULL DEFAULT 0,
    
    -- Win/loss metrics
    win_rate_pct        NUMERIC(5,2),  -- profit_taken / (stopped_out + profit_taken)
    loss_rate_pct       NUMERIC(5,2),
    
    -- Direction accuracy (did market move toward or away from entry?)
    direction_correct   INT NOT NULL DEFAULT 0,      -- Market moved toward entry
    direction_wrong     INT NOT NULL DEFAULT 0,      -- Market moved away from entry
    direction_accuracy_pct NUMERIC(5,2),
    
    -- Market movement analysis
    avg_distance_to_entry_pct NUMERIC(10,4),  -- Average % price moved from entry
    max_distance_away_pct NUMERIC(10,4),      -- Maximum % price moved away
    avg_time_to_hit_entry_hours NUMERIC(10,2),
    avg_time_to_stopout_hours NUMERIC(10,2),
    avg_time_to_profit_hours NUMERIC(10,2),
    
    -- Risk/Reward
    avg_risk_reward_ratio NUMERIC(5,2),
    avg_sl_distance_pct NUMERIC(10,4),
    avg_tp_distance_pct NUMERIC(10,4),
    
    -- LLM vs Trader accuracy
    llm_agrees_with_trader INT NOT NULL DEFAULT 0,
    llm_disagrees_with_trader INT NOT NULL DEFAULT 0,
    llm_accuracy_pct NUMERIC(5,2),  -- When LLM disagreed, was LLM right?
    
    -- Conviction score (0-100)
    conviction_score    NUMERIC(5,2),  -- Composite: direction_accuracy * win_rate * frequency
    
    -- Metadata
    last_updated_at     TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    metadata            JSONB,
    
    created_at          TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    CONSTRAINT uq_trader_stats_period UNIQUE (trader_username, platform, period_from, period_to)
);

CREATE INDEX IF NOT EXISTS idx_trader_stats_trader ON public.alpha_trader_stats (trader_username);
CREATE INDEX IF NOT EXISTS idx_trader_stats_period ON public.alpha_trader_stats (period_from, period_to);
CREATE INDEX IF NOT EXISTS idx_trader_stats_conviction ON public.alpha_trader_stats (conviction_score);
```

### 4.8 `alpha_reply_context` — Reply and thread relationships

```sql
-- ============================================================================
-- alpha_reply_context — Track replies and thread conversations
-- ============================================================================
-- When a trader says "My BTC trade got stopped out", we need to know WHICH
-- trade they're referring to. This table links replies to their parent messages
-- and resolves the trade context.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.alpha_reply_context (
    id                  BIGSERIAL PRIMARY KEY,
    
    -- The reply message
    reply_ingestion_id  BIGINT NOT NULL
        REFERENCES public.alpha_ingestion_log(id) ON DELETE CASCADE,
    
    -- The parent message being replied to
    parent_ingestion_id BIGINT
        REFERENCES public.alpha_ingestion_log(id) ON DELETE SET NULL,
    
    -- Resolved trade context (if the reply refers to a trade)
    referenced_signal_id BIGINT
        REFERENCES public.alpha_dedup_signals(id) ON DELETE SET NULL,
    
    -- LLM resolution
    resolution_method   VARCHAR(32) NOT NULL DEFAULT 'unresolved'
        CHECK (resolution_method IN (
            'unresolved', 'explicit_reference', 'symbol_match',
            'time_proximity', 'llm_inference', 'manual'
        )),
    resolution_confidence NUMERIC(3,2),
    
    -- Context summary (LLM-generated)
    context_summary     TEXT,  -- "Reply to Dylan's BTC long posted 2 hours ago. Confirms stopout at 64K."
    
    created_at          TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_reply_context_reply ON public.alpha_reply_context (reply_ingestion_id);
CREATE INDEX IF NOT EXISTS idx_reply_context_parent ON public.alpha_reply_context (parent_ingestion_id);
CREATE INDEX IF NOT EXISTS idx_reply_context_signal ON public.alpha_reply_context (referenced_signal_id);
```

### 4.9 `alpha_link_log` — Unclassified links for manual review

```sql
-- ============================================================================
-- alpha_link_log — Log links we don't know how to handle
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.alpha_link_log (
    id                  BIGSERIAL PRIMARY KEY,
    ingestion_log_id    BIGINT NOT NULL
        REFERENCES public.alpha_ingestion_log(id) ON DELETE CASCADE,
    url                 TEXT NOT NULL,
    domain              VARCHAR(255),
    link_type           VARCHAR(32) NOT NULL DEFAULT 'unknown',
    action_taken        VARCHAR(32) NOT NULL DEFAULT 'logged'
        CHECK (action_taken IN ('logged', 'ignored', 'processed', 'reviewed')),
    reviewed_by         VARCHAR(255),
    reviewed_at         TIMESTAMPTZ(3),
    notes               TEXT,
    created_at          TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_link_log_ingestion ON public.alpha_link_log (ingestion_log_id);
CREATE INDEX IF NOT EXISTS idx_link_log_domain ON public.alpha_link_log (domain);
```

---

## 5. Media Processing

### 5.1 Media Classification

```python
def classify_media(message: IngestionMessage) -> List[MediaItem]:
    """Classify and route media for processing."""
    media_items = []
    
    for attachment in message.attachments:
        item = MediaItem(
            url=attachment.url,
            filename=attachment.filename,
            content_type=attachment.content_type,
            size_bytes=attachment.size,
        )
        
        # Classify by MIME type and filename
        if attachment.is_image:
            if is_likely_chart(attachment.filename, message.text):
                item.media_type = "chart_image"
                item.processing_queue = "vision_llm"
            else:
                item.media_type = "photo"
                item.processing_queue = "vision_llm_low_priority"
                
        elif attachment.is_voice or attachment.is_audio:
            item.media_type = "voice_note"
            item.processing_queue = "whisper"
            
        elif attachment.is_video:
            item.media_type = "video"
            item.processing_queue = "video_placeholder"  # See section 9
            
        else:
            item.media_type = "document"
            item.processing_queue = "ignore"
            
        media_items.append(item)
    
    return media_items
```

### 5.2 Chart Image Detection

```python
def is_likely_chart(filename: str, message_text: str) -> bool:
    """Heuristic: is this image likely a trading chart?"""
    chart_keywords = [
        "chart", "setup", "trade", "entry", "stop", "tp", "target",
        "long", "short", "buy", "sell", "fib", "vwap", "support",
        "resistance", "order block", "ob", "liquidity", "bos", "choch"
    ]
    
    # Filename hints
    fname_lower = filename.lower()
    if any(kw in fname_lower for kw in ["chart", "setup", "trade", "analysis"]):
        return True
    
    # Text context hints
    text_lower = message_text.lower()
    if any(kw in text_lower for kw in chart_keywords):
        return True
    
    # Default: analyze anyway (vision LLM will figure it out)
    return True  # Conservative: analyze all images
```

---

## 6. Chart Analysis (Vision LLM)

### 6.1 Dual Interpretation Prompt

The prompt is designed to extract BOTH what the trader explicitly stated AND what the LLM independently sees:

```
You are analyzing a trading chart screenshot shared by a trader. Your task is to extract TWO separate interpretations:

## PART 1: TRADER'S EXPLICIT STATEMENTS
Extract ONLY what the trader has explicitly shown or stated on the chart:
- Entry price (if marked with a line, label, or text)
- Stop loss price (if marked)
- Take profit levels (if marked)
- Direction (Long/Short — only if explicitly indicated)
- Symbol/Pair (if visible on chart)
- Timeframe (if visible)

If something is NOT explicitly shown, set it to null. Do NOT infer or guess.

## PART 2: LLM INDEPENDENT INTERPRETATION
Now, as an independent technical analyst, analyze what YOU see on the chart:
- Current price and where it sits relative to key levels
- Visible technical patterns (order blocks, support/resistance, fib levels, VWAP, trend lines)
- What YOU think the trader might be trying to do (your hypothesis)
- Where YOU would place entry, stop loss, and take profits based on what you see
- Your assessment of whether price is moving toward or away from any visible entry level
- Your assessment of the market regime (trending, ranging, breakout, etc.)

## PART 3: CURRENT MARKET CONTEXT
- What is the current price shown on the chart?
- If there's an entry level visible, how far is current price from it (in %)?
- Is price moving toward the entry or away from it?
- What is the overall structure you observe?

## OUTPUT FORMAT
Return ONLY valid JSON with this exact structure:

{
  "trader_stated": {
    "symbol": "... or null",
    "direction": "... or null",
    "timeframe": "... or null",
    "entry": {"price": ..., "type": "...", "confidence": 0.0-1.0},
    "stop_loss": {"price": ..., "type": "...", "confidence": 0.0-1.0},
    "take_profits": [...],
    "notes": "What the trader explicitly wrote on the chart"
  },
  "llm_interpreted": {
    "symbol": "...",
    "direction": "...",
    "timeframe": "...",
    "entry": {"price": ..., "type": "...", "confidence": 0.0-1.0, "reasoning": "..."},
    "stop_loss": {"price": ..., "type": "...", "confidence": 0.0-1.0, "reasoning": "..."},
    "take_profits": [...],
    "technicals": {
      "order_blocks": [...],
      "support_resistance": [...],
      "fib_levels": [...],
      "anchored_vwap": {...},
      "trend_lines": [...]
    },
    "current_price_context": {
      "price_at_analysis": ...,
      "distance_to_entry_pct": ...,
      "price_moving_toward_entry": true/false,
      "market_structure": "..."
    },
    "notes": "LLM's independent analysis",
    "reasoning": "Detailed reasoning for the interpretation"
  }
}

IMPORTANT:
- If the trader did NOT show something, use null. Do not make it up.
- The LLM interpretation is YOUR analysis, independent of what the trader showed.
- Confidence scores: 1.0 = absolutely certain, 0.0 = pure guess
- Include reasoning for all LLM interpretations
```

### 6.2 Model Selection

| Tier | Model | Cost/Image | Use Case |
|------|-------|-----------|----------|
| Primary | `google/gemini-2.5-pro` | ~$0.003 | Standard chart analysis |
| Fallback | `anthropic/claude-sonnet-4` | ~$0.015 | Complex multi-timeframe |
| Emergency | `openai/gpt-4.1-mini` | ~$0.002 | Low-confidence fallback |

---

## 7. TradingView MCP Integration (Conditional)

### 7.1 Research Findings

Two TradingView MCP servers were researched:

**Option A: `atilaahmettaner/tradingview-mcp`** (Benchmark: 64.6)
- Provides: Comprehensive coin analysis, candle pattern detection, volume analysis
- Tools: `comprehensive_coin_analysis`, `advanced_candle_patterns`, `volume_confirmation`
- **Limitation:** Crypto-focused, limited to supported exchanges (KuCoin, etc.)
- **No chart screenshot capability**

**Option B: `tradesdontlie/tradingview-mcp`** (Benchmark: 83.2)
- Provides: Chart screenshots via Chrome DevTools Protocol, Pine Script indicator data
- Tools: `screenshot`, `data_get_pine_lines`, `data_get_pine_labels`, `data_get_pine_tables`
- **Requires:** TradingView Desktop running locally
- **Can read:** Custom Pine Script indicators (lines, labels, tables, boxes)
- **Can capture:** Full chart screenshots

### 7.2 Integration Strategy (If/Then)

```
IF TradingView MCP (tradesdontlie) is available AND running:
    THEN:
        1. For each chart analysis, ALSO fetch 2h and 4h timeframe context
        2. Submit trader's chart + 2h context + 4h context to LLM
        3. LLM gets "big picture" context
        4. If user has paid indicators, read Pine Script output (lines, labels)
        5. Include indicator data in prompt
        
    ELSE IF only atilaahmettaner MCP is available:
        1. Use for indicator values (RSI, MACD, Bollinger, etc.)
        2. Include indicator values in prompt as text context
        3. No screenshot capability — vision-only for charts
        
    ELSE:
        1. Vision-only analysis (current plan)
        2. No multi-timeframe context
        3. No custom indicator data
        4. Plan works fully without any MCP
```

### 7.3 Enhanced Prompt (With TradingView MCP)

```
## ADDITIONAL CONTEXT (from TradingView MCP)

The trader posted a chart for {symbol} on the {timeframe} timeframe.
Here is the current market context from TradingView:

2-Hour Timeframe Context:
- Current price: {price_2h}
- Trend: {trend_2h}
- Key levels: {levels_2h}

4-Hour Timeframe Context:
- Current price: {price_4h}
- Trend: {trend_4h}
- Key levels: {levels_4h}

Indicator Values (if available):
- RSI: {rsi}
- MACD: {macd}
- Bollinger Bands: {bb}

Use this context to better understand the trader's setup in the broader market structure.
```

### 7.4 Fallback (Without TradingView MCP)

The plan works completely without TradingView MCP. The vision LLM analyzes the chart image directly. The prompt is simpler but still effective:

```
Analyze this chart screenshot. Extract the trader's explicit statements and provide your independent technical analysis.
```

---

## 8. Voice & Audio

### 8.1 Whisper Transcription

- **Model:** `openai/whisper-1` via OpenRouter (or direct OpenAI)
- **Format:** `verbose_json` with timestamps
- **Cost:** ~$0.006/minute
- **Output:** Text + segments with timestamps

### 8.2 Voice-Chart Correlation

When a message has BOTH voice note AND chart image:

```python
def correlate_voice_and_chart(voice_transcription: str, chart_analysis: dict) -> dict:
    """Correlate what the trader said with what the chart shows."""
    
    # Send both to LLM for correlation analysis
    prompt = f"""
    The trader posted a voice note and a chart screenshot.
    
    Voice transcription: {voice_transcription}
    Chart analysis: {json.dumps(chart_analysis)}
    
    Correlate the two:
    1. Does the voice mention the same trade as the chart shows?
    2. Does the voice add context not visible on the chart?
    3. Are there any contradictions between what was said and what was shown?
    4. What is the COMPLETE trade setup combining both sources?
    
    Return JSON with merged interpretation.
    """
    
    return call_llm(prompt)
```

---

## 9. Video Placeholder

### 9.1 Design (Well-Commented, Future-Ready)

```python
# shared/daemons/alpha_video_processor.py
"""
VIDEO PROCESSING PLACEHOLDER — Phase 50+ Future Enhancement
=============================================================

This module is a SCAFFOLD for future video analysis capabilities.
It is NOT active in Phase 50. All functions return "not_implemented"
or store videos with status='pending' for future processing.

FUTURE IMPLEMENTATION PLAN:
---------------------------
When video analysis is enabled (system_config: alpha_video_enabled=true):

1. VIDEO DOWNLOAD
   - Download Discord CDN videos (mp4, webm, mov)
   - Store in media_items with media_type='video'
   - Max file size: 100MB (configurable)

2. FRAME EXTRACTION
   - Extract screenshot every N seconds (default: 10s, configurable)
   - Use ffmpeg: ffmpeg -i input.mp4 -vf "fps=1/10" output_%03d.png
   - Store frames as separate media_items with media_type='video_frame'
   - Link frames to parent video via metadata.parent_video_id

3. SCENE DETECTION (Cheap AI)
   - Use a cheap vision model to classify each frame:
     * "face" — trader talking to camera (ignore for trading)
     * "chart" — trading chart visible (PRIORITY)
     * "screen_share" — screen sharing, may contain charts
     * "other" — irrelevant content
   - Model: google/gemini-2.5-flash or local model (e.g., CLIP)
   - Cost target: <$0.001 per frame
   - Only keep "chart" and "screen_share" frames

4. TRANSCRIPTION
   - Extract audio track from video
   - Send to Whisper for transcription
   - Include timestamps (verbose_json format)
   - Cost: same as voice notes (~$0.006/min)

5. TEMPORAL CORRELATION
   - Match transcription timestamps to frame timestamps
   - Create "scene segments":
     * 00:00-00:15: "Trader discussing Bitcoin setup" + frame_001.png (chart)
     * 00:15-00:30: "Trader showing entry level" + frame_002.png (chart zoomed)
   - Each segment becomes an analysis unit

6. DEDUPLICATION WITHIN VIDEO
   - Same 2% entry zone rule applies
   - If trader shows the same chart for 60 seconds, don't create 6 signals
   - Use frame similarity (image hash) to detect duplicate frames
   - Only analyze UNIQUE chart views

7. LLM ANALYSIS PER SEGMENT
   - Send frame + transcription segment to vision LLM
   - Prompt includes temporal context: "At 00:15, the trader said..."
   - Extract trade setup from each unique chart view

8. TRADE TRACKING
   - Same dedup_signals + stage_tracker pipeline
   - Video-derived signals are tagged: source='video_frame'

CONFIGURATION (system_config):
------------------------------
- alpha_video_enabled: false (default)
- alpha_video_max_size_mb: 100
- alpha_video_frame_interval_sec: 10
- alpha_video_scene_detection_model: "google/gemini-2.5-flash"
- alpha_video_max_frames_per_video: 30
- alpha_video_cost_cap_usd_day: 5.00

CURRENT BEHAVIOR:
----------------
- Videos are detected and logged in alpha_ingestion_log as message_type='video_attachment'
- Videos are stored in media_items with processing_status='pending'
- A placeholder row is created in alpha_analyses with status='discarded' and note='Video processing not yet enabled'
- No LLM calls are made for videos
"""

class VideoProcessor:
    """Placeholder video processor. All methods are no-ops or log warnings."""
    
    async def process(self, media_item: MediaItem) -> AnalysisResult:
        logger.info("[VideoProcessor] Video processing is not yet enabled. "
                   "Media item %s stored with status='pending' for future processing. "
                   "Set system_config.alpha_video_enabled=true to activate.",
                   media_item.id)
        
        return AnalysisResult(
            status="discarded",
            note="Video processing not yet enabled. See alpha_video_processor.py for future implementation plan.",
            cost_usd=0,
        )
```

---

## 10. Deduplication & Trade Grouping

### 10.1 Matching Algorithm (Enhanced)

```python
def match_to_signal(analysis: AlphaAnalysis) -> Optional[int]:
    """Find an existing dedup_signal that this analysis matches."""
    extracted = analysis.extracted
    if not extracted or not extracted.get("symbol"):
        return None

    symbol = extracted["symbol"]
    direction = extracted.get("direction", "neutral")
    entry = extracted.get("entry", {})
    entry_price = entry.get("price")
    
    # Get trader's stated entry first, fallback to LLM interpreted
    if not entry_price:
        trader_entry = analysis.trader_stated.get("entry", {})
        entry_price = trader_entry.get("price")
    if not entry_price:
        llm_entry = analysis.llm_interpreted.get("entry", {})
        entry_price = llm_entry.get("price")

    # Look for active signals in the last 24 hours from the same author
    sql = """
        SELECT id, entry_price_min, entry_price_max, stop_loss_price, author
        FROM alpha_dedup_signals
        WHERE symbol = $1
          AND direction = $2
          AND signal_status = 'active'
          AND first_seen_at > NOW() - INTERVAL '24 hours'
          AND author = $3
        ORDER BY last_seen_at DESC
    """

    for signal in db.fetch(sql, symbol, direction, analysis.author):
        # Price match: within 2% of entry zone (configurable)
        match_threshold = float(get_config("alpha_dedup_match_threshold_pct", 2.0))
        
        if entry_price and signal.entry_price_min and signal.entry_price_max:
            if signal.entry_price_min * (1 - match_threshold/100) <= entry_price <= signal.entry_price_max * (1 + match_threshold/100):
                return signal.id

        # Fallback: same symbol + direction + SL within 2%
        sl = extracted.get("stop_loss", {}).get("price")
        if not sl:
            sl = analysis.trader_stated.get("stop_loss", {}).get("price")
        if not sl:
            sl = analysis.llm_interpreted.get("stop_loss", {}).get("price")
            
        if sl and signal.stop_loss_price:
            if abs(sl - signal.stop_loss_price) / sl < match_threshold/100:
                return signal.id

    return None
```

### 10.2 Configurable Threshold

```sql
INSERT INTO system_config (namespace, config_key, config_value) VALUES
  ('alpha_dedup', 'match_threshold_pct', '2.0'),
  ('alpha_dedup', 'time_window_hours', '24'),
  ('alpha_dedup', 'max_signals_per_trader_day', '50');
```

---

## 11. Trade Stage Tracking

### 11.1 Stage Determination (Enhanced)

```python
def determine_stage(signal: DedupSignal, candles: List[Candle]) -> StageResult:
    """Determine trade stage with market movement analysis."""
    
    if not signal.entry_price_min or not signal.stop_loss_price:
        return StageResult(stage="waiting_for_entry", reason="Insufficient data")

    latest = candles[-1] if candles else None
    if not latest:
        return StageResult(stage="waiting_for_entry", reason="No candle data")

    entry = (signal.entry_price_min + signal.entry_price_max) / 2 if signal.entry_price_max else signal.entry_price_min
    sl = signal.stop_loss_price
    tp = signal.take_profit_price  # May be null
    
    # Calculate distances
    distance_from_entry = (latest.close - entry) / entry * 100
    distance_from_sl = (latest.close - sl) / sl * 100 if sl else None
    
    # Determine market direction relative to trade
    if signal.direction == "long":
        if latest.close > entry * 1.001:
            market_direction = "toward_entry" if latest.close > entry else "away_from_entry"
        else:
            market_direction = "toward_entry" if latest.close > latest.open else "sideways"
    else:  # short
        if latest.close < entry * 0.999:
            market_direction = "toward_entry"
        else:
            market_direction = "away_from_entry" if latest.close > latest.open else "sideways"
    
    # Check if entry was hit
    entry_triggered = any(
        (signal.direction == "long" and c.low <= entry <= c.high) or
        (signal.direction == "short" and c.high >= entry >= c.low)
        for c in candles[-10:]
    )
    
    if not entry_triggered:
        return StageResult(
            stage="waiting_for_entry",
            reason=f"Price at {latest.close}, entry at {entry}, distance: {distance_from_entry:.2f}%",
            distance_from_entry_pct=distance_from_entry,
            market_direction=market_direction,
        )
    
    # Check if stopped out
    if signal.direction == "long":
        stopped = any(c.low <= sl for c in candles)
        if stopped:
            return StageResult(stage="stopped_out", reason=f"Low {min(c.low for c in candles)} hit SL {sl}")
    else:
        stopped = any(c.high >= sl for c in candles)
        if stopped:
            return StageResult(stage="stopped_out", reason=f"High {max(c.high for c in candles)} hit SL {sl}")
    
    # Check TP (if available)
    if tp:
        if signal.direction == "long" and any(c.high >= tp for c in candles):
            return StageResult(stage="profit_taken_full", reason=f"High hit TP {tp}")
        elif signal.direction == "short" and any(c.low <= tp for c in candles):
            return StageResult(stage="profit_taken_full", reason=f"Low hit TP {tp}")
    
    # In trade
    return StageResult(
        stage="in_trade",
        reason=f"Entry triggered at {entry}, current price {latest.close}, P&L: {distance_from_entry:.2f}%",
        distance_from_entry_pct=distance_from_entry,
        market_direction=market_direction,
    )
```

---

## 12. Market Movement Analytics

### 12.1 Continuous Tracking

Every 15 minutes, for every active signal:

```python
async def record_market_movement(signal: DedupSignal):
    """Record how far price has moved from entry over time."""
    
    candles = await fetch_candles(signal.symbol, signal.timeframe, since=signal.first_seen_at)
    if not candles:
        return
    
    latest = candles[-1]
    entry = signal.entry_price
    
    hours_since = (latest.timestamp - signal.first_seen_at).total_seconds() / 3600
    distance_pct = (latest.close - entry) / entry * 100
    
    # Determine direction
    if signal.direction == "long":
        if latest.close >= entry:
            direction = "toward_entry" if latest.close > entry else "hit_entry"
        else:
            direction = "away_from_entry"
    else:
        if latest.close <= entry:
            direction = "toward_entry"
        else:
            direction = "away_from_entry"
    
    # Insert movement record
    await db.execute("""
        INSERT INTO alpha_market_movement (
            dedup_signal_id, snapshot_at, hours_since_post,
            current_price, entry_price, stop_loss_price, take_profit_price,
            distance_from_entry_pct, market_direction,
            high_since_post, low_since_post
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
    """, signal.id, latest.timestamp, hours_since,
        latest.close, entry, signal.stop_loss_price, signal.take_profit_price,
        distance_pct, direction,
        max(c.high for c in candles), min(c.low for c in candles))
```

### 12.2 Analytics Queries

```sql
-- How often does the market move in the trader's predicted direction?
SELECT 
    author,
    COUNT(*) as total_signals,
    SUM(CASE WHEN market_direction IN ('toward_entry', 'hit_entry') THEN 1 ELSE 0 END) as direction_correct,
    SUM(CASE WHEN market_direction = 'away_from_entry' THEN 1 ELSE 0 END) as direction_wrong,
    ROUND(100.0 * SUM(CASE WHEN market_direction IN ('toward_entry', 'hit_entry') THEN 1 ELSE 0 END) / COUNT(*), 2) as direction_accuracy_pct
FROM alpha_dedup_signals s
JOIN alpha_market_movement m ON s.id = m.dedup_signal_id
WHERE m.hours_since_post BETWEEN 0 AND 24  -- First 24 hours
GROUP BY author;

-- Average distance from entry over time
SELECT 
    author,
    AVG(ABS(distance_from_entry_pct)) as avg_distance_pct,
    MAX(ABS(distance_from_entry_pct)) as max_distance_pct,
    AVG(hours_since_post) as avg_hours_to_max
FROM alpha_dedup_signals s
JOIN alpha_market_movement m ON s.id = m.dedup_signal_id
GROUP BY author;
```

---

## 13. Dual Interpretation (Trader vs LLM)

### 13.1 Schema Recap

Every analysis has three JSONB fields:
- `trader_stated` — What the trader explicitly showed/said
- `llm_interpreted` — What the LLM independently sees
- `extracted` — Merged/resolved view (used by downstream systems)

### 13.2 Disagreement Detection

```python
def detect_disagreement(analysis: AlphaAnalysis) -> DisagreementReport:
    """Detect where LLM disagrees with trader."""
    
    trader = analysis.trader_stated or {}
    llm = analysis.llm_interpreted or {}
    
    disagreements = []
    
    # Direction disagreement
    if trader.get("direction") and llm.get("direction"):
        if trader["direction"] != llm["direction"]:
            disagreements.append({
                "field": "direction",
                "trader": trader["direction"],
                "llm": llm["direction"],
                "severity": "high",
            })
    
    # Entry price disagreement (>5% difference)
    t_entry = trader.get("entry", {}).get("price")
    l_entry = llm.get("entry", {}).get("price")
    if t_entry and l_entry:
        diff_pct = abs(t_entry - l_entry) / t_entry * 100
        if diff_pct > 5:
            disagreements.append({
                "field": "entry_price",
                "trader": t_entry,
                "llm": l_entry,
                "diff_pct": diff_pct,
                "severity": "medium" if diff_pct < 10 else "high",
            })
    
    # Stop loss disagreement
    t_sl = trader.get("stop_loss", {}).get("price")
    l_sl = llm.get("stop_loss", {}).get("price")
    if t_sl and l_sl:
        diff_pct = abs(t_sl - l_sl) / t_sl * 100
        if diff_pct > 5:
            disagreements.append({
                "field": "stop_loss",
                "trader": t_sl,
                "llm": l_sl,
                "diff_pct": diff_pct,
                "severity": "medium",
            })
    
    return DisagreementReport(
        has_disagreement=len(disagreements) > 0,
        disagreements=disagreements,
        recommendation="review" if any(d["severity"] == "high" for d in disagreements) else "ok",
    )
```

### 13.3 ChartHacker Agent Uses This

The ChartHacker agent tracks:
- When LLM disagrees with trader, who was right?
- If LLM consistently gets it wrong → maybe change the LLM model
- If trader consistently gets it wrong → lower conviction score for that trader

---

## 14. Reply & Thread Context

### 14.1 Reply Resolution

```python
async def resolve_reply_context(reply: IngestionMessage) -> ReplyContext:
    """Resolve what trade a reply is referring to."""
    
    # Method 1: Explicit reference
    # "My BTC trade from earlier" → match BTC + author + recent signal
    explicit_symbol = extract_symbol(reply.cleaned_text)
    if explicit_symbol:
        signal = await find_recent_signal(
            author=reply.author_username,
            symbol=explicit_symbol,
            within_hours=24
        )
        if signal:
            return ReplyContext(
                method="explicit_reference",
                confidence=0.9,
                signal_id=signal.id,
            )
    
    # Method 2: Reply to specific message
    if reply.reply_to_message_id:
        parent = await get_ingestion_by_platform_id(reply.reply_to_message_id)
        if parent and parent.dedup_signal_id:
            return ReplyContext(
                method="direct_reply",
                confidence=0.95,
                signal_id=parent.dedup_signal_id,
            )
    
    # Method 3: Time proximity + symbol match
    # If reply is within 5 min of a signal and mentions same symbol
    recent_signals = await get_recent_signals(
        channel_id=reply.channel_id,
        within_minutes=30
    )
    for signal in recent_signals:
        if symbol_in_text(signal.symbol, reply.cleaned_text):
            return ReplyContext(
                method="time_proximity",
                confidence=0.7,
                signal_id=signal.id,
            )
    
    # Method 4: LLM inference
    # "Got stopped out" → LLM looks at recent signals and infers which one
    context = await build_context(reply.channel_id, within_hours=6)
    prompt = f"""
    This message was posted in reply to a recent trade discussion:
    "{reply.cleaned_text}"
    
    Recent trade signals in this channel:
    {context}
    
    Which trade is this message referring to? Return the signal_id or null if unclear.
    """
    
    result = await call_llm(prompt)
    if result.get("signal_id"):
        return ReplyContext(
            method="llm_inference",
            confidence=result.get("confidence", 0.6),
            signal_id=result["signal_id"],
        )
    
    return ReplyContext(method="unresolved", confidence=0.0)
```

---

## 15. ChartHacker Agent

### 15.1 Agent Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         ChartHacker Agent                                      │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌─────────────────┐    ┌─────────────────┐    ┌─────────────────────────┐    │
│  │ Watch Daemon    │───▶│ Trade Validator │───▶│ Performance Tracker     │    │
│  │                 │    │                 │    │                         │    │
│  │ • Poll active   │    │ • Run backtest  │    │ • Update trader stats   │    │
│  │   signals       │    │   on setup      │    │ • Record outcomes       │    │
│  │ • Check stage   │    │ • Run forward   │    │ • Calculate conviction  │    │
│  │   changes       │    │   test          │    │ • Alert on anomalies    │    │
│  │ • Fetch candles │    │ • Compare vs    │    │                         │    │
│  │                 │    │   live market   │    │                         │    │
│  └─────────────────┘    └─────────────────┘    └─────────────────────────┘    │
│           │                      │                      │                    │
│           ▼                      ▼                      ▼                    │
│  ┌─────────────────────────────────────────────────────────────────────┐      │
│  │                         MemU / Knowledge Base                        │      │
│  │  • "Dylan's BTC long at 65K — hit entry, currently +2.3%"           │      │
│  │  • "EMU's ETH short — stopped out, market moved 5% against"         │      │
│  │  • "Panda's conviction score: 78% (high)"                             │      │
│  └─────────────────────────────────────────────────────────────────────┘      │
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐      │
│  │                         MCP Tools                                      │      │
│  │  • charthacker.status — current watch list + active signals           │      │
│  │  • charthacker.trader_report — full report on a trader                │      │
│  │  • charthacker.signal_detail — full history of a trade signal         │      │
│  │  • charthacker.compare_traders — side-by-side trader comparison       │      │
│  │  • charthacker.backtest_setup — run backtest on a trader's setup      │      │
│  └─────────────────────────────────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 15.2 Agent Deployment

The ChartHacker agent is deployed via the Paperclip web interface:

```python
# MCP tool: agent.create
{
    "name": "charthacker",
    "type": "daemon",
    "description": "Watches Discord/Telegram trader signals, validates setups via backtest/forward-test, tracks trader performance",
    "capabilities": [
        "read:alpha_analyses",
        "read:alpha_dedup_signals",
        "read:alpha_stage_tracker",
        "read:candles",
        "write:alpha_trader_stats",
        "write:memu",
    ],
    "schedule": "continuous",  # Polls every 5 minutes
    "config": {
        "watch_traders": ["thelordofentry", "emutrading", "degendavidd", "chaos", "panda", "thenagel"],
        "backtest_lookback_days": 30,
        "forward_test_enabled": True,
        "alert_on_new_signal": True,
        "alert_on_stage_change": True,
    }
}
```

### 15.3 Backtest Validation

When a new signal is detected:

```python
async def validate_signal_via_backtest(signal: DedupSignal) -> BacktestResult:
    """Run a quick backtest to validate the trader's setup."""
    
    # Build strategy from signal parameters
    strategy_config = {
        "symbol": signal.symbol,
        "timeframe": signal.timeframe,
        "direction": signal.direction,
        "entry_price": signal.entry_price,
        "stop_loss": signal.stop_loss_price,
        "take_profit": signal.take_profit_price,
        "lookback_days": 30,  # How much history to test
    }
    
    # Run backtest using existing backtest engine
    result = await run_backtest(strategy_config)
    
    # Compare backtest results to trader's claim
    return BacktestResult(
        signal_id=signal.id,
        backtest_win_rate=result.win_rate,
        backtest_profit_factor=result.profit_factor,
        backtest_max_drawdown=result.max_drawdown,
        historical_similar_setups=result.similar_trades,
        recommendation="valid" if result.win_rate > 0.5 else "questionable",
    )
```

---

## 16. Trader Performance Analytics

### 16.1 Metrics Computed

| Metric | Description | Update Frequency |
|--------|-------------|-----------------|
| Win Rate | % of signals that hit TP before SL | Per signal close |
| Direction Accuracy | % of times market moved toward entry | Every 6 hours |
| Avg Distance to Entry | Average % price moved from entry | Every 6 hours |
| Max Distance Away | Maximum % price moved against position | Per signal close |
| Avg Time to Hit Entry | Hours from post to entry hit | Per signal entry |
| Avg Time to Stopout | Hours from entry to SL hit | Per signal stopout |
| Avg Time to Profit | Hours from entry to TP hit | Per signal profit |
| Risk/Reward Ratio | Average TP distance / SL distance | Per signal close |
| LLM Agreement Rate | % of times LLM agreed with trader | Per analysis |
| LLM Accuracy | When LLM disagreed, was LLM right? | Per signal close |
| Conviction Score | Composite: direction_acc × win_rate × frequency | Daily |

### 16.2 Conviction Score Formula

```python
def calculate_conviction_score(trader_stats: TraderStats) -> float:
    """Calculate a 0-100 conviction score for a trader."""
    
    # Components (each 0-100)
    direction_accuracy = trader_stats.direction_accuracy_pct or 0
    win_rate = trader_stats.win_rate_pct or 0
    
    # Frequency bonus (traders who post more get slight boost)
    signals_per_week = trader_stats.total_signals / 7 * 7  # normalize
    frequency_bonus = min(signals_per_week * 2, 20)  # max 20 points
    
    # Consistency bonus (low variance in performance)
    consistency = max(0, 100 - trader_stats.performance_variance)
    
    # Weighted composite
    score = (
        direction_accuracy * 0.35 +  # Most important: do they predict direction?
        win_rate * 0.35 +             # Second: do they make money?
        frequency_bonus * 0.15 +    # Third: are they active?
        consistency * 0.15            # Fourth: are they consistent?
    )
    
    return min(score, 100)
```

---

## 17. Global Knowledge Sharing

### 17.1 Knowledge Schema

All alpha intelligence is stored in Postgres and accessible to all agents:

```sql
-- View: alpha_knowledge (for agent queries)
CREATE OR REPLACE VIEW public.alpha_knowledge AS
SELECT 
    s.id as signal_id,
    s.symbol,
    s.direction,
    s.author as trader,
    s.first_seen_at,
    s.signal_status,
    a.trader_stated,
    a.llm_interpreted,
    a.extracted,
    st.stage,
    st.check_at as last_checked,
    st.current_price,
    st.price_vs_entry_pct,
    ts.win_rate_pct,
    ts.direction_accuracy_pct,
    ts.conviction_score,
    rc.context_summary as reply_context
FROM alpha_dedup_signals s
LEFT JOIN alpha_analyses a ON a.dedup_signal_id = s.id AND a.is_anchor = TRUE
LEFT JOIN LATERAL (
    SELECT * FROM alpha_stage_tracker 
    WHERE dedup_signal_id = s.id 
    ORDER BY check_at DESC LIMIT 1
) st ON true
LEFT JOIN alpha_trader_stats ts ON ts.trader_username = s.author 
    AND ts.period_from <= CURRENT_DATE AND ts.period_to >= CURRENT_DATE
LEFT JOIN alpha_reply_context rc ON rc.referenced_signal_id = s.id
WHERE s.signal_status = 'active' OR s.last_seen_at > NOW() - INTERVAL '7 days';
```

### 17.2 Agent Access

Any agent can query:

```python
# Example: Surgeon2 trader wants to know what Dylan thinks about BTC
async def query_alpha_knowledge(symbol: str, trader: str = None) -> List[KnowledgeItem]:
    sql = """
        SELECT * FROM alpha_knowledge 
        WHERE symbol = $1 
          AND (trader = $2 OR $2 IS NULL)
        ORDER BY first_seen_at DESC
        LIMIT 10
    """
    return await db.fetch(sql, symbol, trader)

# Example: Get all active signals with high conviction traders
async def get_high_conviction_signals(min_conviction: float = 70.0) -> List[KnowledgeItem]:
    sql = """
        SELECT * FROM alpha_knowledge 
        WHERE conviction_score >= $1 
          AND signal_status = 'active'
        ORDER BY conviction_score DESC
    """
    return await db.fetch(sql, min_conviction)
```

---

## 18. MCP Tools

### 18.1 Alpha Discovery Tools

| Tool | Purpose |
|------|---------|
| `alpha.discover_users` | Trigger user discovery scan |
| `alpha.list_channels` | List monitored channels per user |
| `alpha.add_channel` | Manually add a channel to monitor |
| `alpha.remove_channel` | Remove a channel from monitoring |
| `alpha.set_user_monitor` | Enable/disable monitoring for a user |

### 18.2 Analysis Query Tools

| Tool | Purpose |
|------|---------|
| `alpha.query_analyses` | Query chart analyses with filters |
| `alpha.get_trade_group` | Get full trade group with stage history |
| `alpha.get_trader_stats` | Get trader performance summary |
| `alpha.compare_traders` | Side-by-side trader comparison |
| `alpha.get_market_movement` | Price movement history for a signal |

### 18.3 ChartHacker Tools

| Tool | Purpose |
|------|---------|
| `charthacker.status` | Current watch list + active signals |
| `charthacker.trader_report` | Full report on a trader |
| `charthacker.signal_detail` | Full history of a trade signal |
| `charthacker.backtest_setup` | Run backtest on a trader's setup |
| `charthacker.force_check` | Force immediate stage check on a signal |

---

## 19. Systemd Services

### 19.1 Service List

| Service | File | Interval | Purpose |
|---------|------|----------|---------|
| `tickles-alpha-ingestion` | `shared/daemons/alpha_ingestion.py` | Continuous | Unified message ingestion router |
| `tickles-alpha-discovery` | `shared/daemons/alpha_discovery.py` | 6 hours | User/channel discovery |
| `tickles-alpha-media` | `shared/daemons/alpha_media_processor.py` | 2 min | Media processing (vision, whisper) |
| `tickles-alpha-dedup` | `shared/daemons/alpha_dedup.py` | 5 min | Deduplication + signal grouping |
| `tickles-alpha-stage` | `shared/daemons/alpha_stage_tracker.py` | 15 min | Trade stage tracking |
| `tickles-alpha-movement` | `shared/daemons/alpha_movement_tracker.py` | 15 min | Market movement analytics |
| `tickles-alpha-reply` | `shared/daemons/alpha_reply_resolver.py` | 5 min | Reply context resolution |
| `tickles-charthacker` | `shared/agents/charthacker.py` | 5 min | ChartHacker agent |

### 19.2 Service Dependencies

```
tickles-alpha-ingestion
    └─▶ writes alpha_ingestion_log
    └─▶ writes news_items (backward compat)
    └─▶ writes media_items

tickles-alpha-discovery
    └─▶ writes alpha_user_discovery
    └─▶ writes collector_sources

tickles-alpha-media
    └─▶ reads media_items (status='downloaded')
    └─▶ calls vision LLM / whisper
    └─▶ writes alpha_analyses

tickles-alpha-dedup
    └─▶ reads alpha_analyses (status='completed')
    └─▶ writes alpha_dedup_signals
    └─▶ writes alpha_trade_groups

tickles-alpha-stage
    └─▶ reads alpha_dedup_signals (status='active')
    └─▶ reads candles
    └─▶ writes alpha_stage_tracker
    └─▶ updates alpha_dedup_signals.signal_status

tickles-alpha-movement
    └─▶ reads alpha_dedup_signals
    └─▶ reads candles
    └─▶ writes alpha_market_movement

tickles-alpha-reply
    └─▶ reads alpha_ingestion_log (replies)
    └─▶ reads alpha_dedup_signals
    └─▶ writes alpha_reply_context

tickles-charthacker
    └─▶ reads all alpha_* tables
    └─▶ reads candles
    └─▶ runs backtest engine
    └─▶ writes alpha_trader_stats
    └─▶ writes MemU
```

---

## 20. Implementation Roadmap

### Phase 50A: Foundation (Days 1-2)
1. Write migration SQL (all tables)
2. Apply migration + smoke tests
3. Implement `alpha_ingestion_log` + unified router
4. Wire Discord collector to new ingestion pipeline
5. Test with sample messages

### Phase 50B: Discovery & Channels (Days 2-3)
6. Implement `alpha_discovery.py` daemon
7. Seed known channels from explicit list
8. Test auto-discovery
9. Implement channel add/remove MCP tools

### Phase 50C: Media Processing (Days 3-5)
10. Implement chart image analysis (vision LLM)
11. Implement voice transcription (whisper)
12. Implement dual interpretation prompt
13. Test with real chart screenshots
14. Implement TradingView link resolution

### Phase 50D: Deduplication (Days 5-6)
15. Implement dedup matching algorithm
16. Implement signal creation/linking
17. Test with simulated repeated posts
18. Add configurable threshold

### Phase 50E: Stage Tracking (Days 6-7)
19. Implement stage tracker daemon
20. Implement market movement tracker
21. Test stage transitions with historical data
22. Add Freshness Guard integration

### Phase 50F: Reply Context (Days 7-8)
23. Implement reply resolution
24. Test with "got stopped out" scenarios
25. Add Trading Zone channel monitoring

### Phase 50G: ChartHacker Agent (Days 8-10)
26. Implement ChartHacker daemon
27. Implement backtest validation
28. Implement trader stats aggregation
29. Deploy via Paperclip agent.create
30. Test end-to-end

### Phase 50H: MCP Tools & Integration (Days 10-12)
31. Implement all MCP tools
32. Register in MCP daemon
33. Test each tool
34. Add monitoring/alerting

### Phase 50I: Hardening & Testing (Days 12-14)
35. Write tests (target: 30+ new tests)
36. Run bug hunt
37. Fix issues
38. Update CLAUDE.md
39. Deploy to production

### Phase 50J: Live Validation (Days 15-21)
40. Enable for all target users/channels
41. Monitor for 1 week
42. Review data quality
43. Tune prompts, thresholds, models
44. Generate first trader performance reports

---

## 21. Cost Estimate

| Component | Daily Volume | Unit Cost | Daily Cost |
|-----------|-------------|-----------|------------|
| Chart analysis (Gemini 2.5 Pro) | 30 images | $0.003 | $0.09 |
| Chart analysis fallback (Claude) | 5 images | $0.015 | $0.075 |
| Voice transcription (Whisper) | 10 min | $0.006/min | $0.06 |
| Reply resolution (LLM) | 20 replies | $0.01 | $0.20 |
| Stage tracker LLM review | 5 reviews | $0.08 | $0.40 |
| ChartHacker backtest | 10 backtests | $0.02 | $0.20 |
| **Total** | | | **~$1.05/day** |

Monthly: ~$32. Well within budget.

**Video processing (future):**
- Frame extraction: free (ffmpeg)
- Scene detection: ~$0.001/frame × 30 frames/video × 5 videos/day = $0.15/day
- Transcription: same as voice ($0.006/min)
- Total video (when enabled): ~$0.50/day

---

## 22. What Could Go Wrong

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Discord rate-limits discovery | Medium | Discovery delayed | Exponential backoff; scan fewer channels |
| Vision LLM returns garbage JSON | Medium | Analysis unparseable | JSON validation; retry with fallback model |
| Whisper transcription inaccurate | Medium | Missed context | Store raw; flag low-confidence segments |
| Target user changes username | Low | Discovery misses | Track by user ID; update username cache |
| Deduplication false-positive | Medium | Signals merged | Tighten threshold; manual review UI |
| Deduplication false-negative | Medium | Duplicate signals | Fuzzy matching on SL; time window expansion |
| Stage tracker uses stale candles | Low | Wrong stage | Freshness Guard; skip if stale |
| LLM costs exceed budget | Medium | Degradation | Daily cost cap; fallback to cheaper model |
| Discord self-bot token revoked | Low | Complete outage | Monitor auth health; backup token |
| Reply resolution wrong | Medium | Context lost | Multiple methods; confidence scoring |
| Trader performance skewed by small sample | Medium | Wrong conviction | Minimum sample size (n=10) before scoring |
| TradingView MCP unavailable | Low | No multi-timeframe | Plan works without it; vision-only is sufficient |
| Video processing too expensive | Low | Budget overrun | Disabled by default; cost cap; frame limit |
| Postgres JSONB queries slow | Low | Dashboard lag | GIN indexes; materialized views |

---

## 23. Self-Check (Devil's Advocate)

> *"What would a senior engineer critique about this design?"*

1. **Q: Why not ClickHouse for stage_tracker time-series?**  
   A: At 2.4K rows/day, Postgres is fine. If we grow 100x, migrate last 30 days to ClickHouse, keep metadata in Postgres.

2. **Q: What if the vision LLM hallucinates prices?**  
   A: Dual interpretation separates trader_stated (ground truth) from llm_interpreted (hypothesis). We trust trader_stated for trade tracking; llm_interpreted is advisory only.

3. **Q: What if a user posts in 500 channels?**  
   A: `max_channels_per_scan=500`. If exceeded, prioritize by recency. Collector has `max_messages_per_channel=200`.

4. **Q: What if dedup matches across different symbols?**  
   A: Matching requires exact symbol match. No cross-symbol dedup.

5. **Q: What about privacy?**  
   A: Only public channels. No DMs. Target users are public figures posting publicly.

6. **Q: What if Discord changes their API?**  
   A: We use `discord.py-self` which follows user API. If broken, fall back to webhook-based collection.

7. **Q: Why not use existing enrichment pipeline for everything?**  
   A: Enrichment pipeline is text-only and synchronous. Vision LLM calls are async, slow, expensive. They need a separate daemon with queuing, retries, cost tracking.

8. **Q: What if TradingView MCP is never available?**  
   A: Plan works completely without it. The conditional integration is additive, not required. Vision-only analysis is the primary path.

9. **Q: What if a trader posts 50 signals per day?**  
   A: `max_signals_per_trader_day=50` (configurable). Excess signals are logged but not analyzed to control costs.

10. **Q: What if the ChartHacker agent disagrees with the trader?**  
    A: That's the point. The agent records the disagreement and tracks who was right over time. This becomes valuable signal for other agents.

---

*End of plan. Living document — update when service boundaries change.*
