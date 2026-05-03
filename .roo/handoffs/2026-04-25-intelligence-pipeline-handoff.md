# Handoff: Intelligence Pipeline Architecture — 2026-04-25

**Date:** 2026-04-25  
**Topic:** Intelligence Pipeline (Phase 3B+) — Design & Planning  
**Status:** Design complete. Ready for implementation.  
**Session type:** Architect mode (planning, no code changes made)

---

## 1. What We've Completed

### Design Documents Created
- [`shared/docs/INTELLIGENCE_PIPELINE_DESIGN.md`](shared/docs/INTELLIGENCE_PIPELINE_DESIGN.md) — Full technical architecture with SQL schemas, service definitions, data flow diagrams, implementation order.
- [`shared/docs/INTELLIGENCE_PIPELINE_PLAIN_ENGLISH.md`](shared/docs/INTELLIGENCE_PIPELINE_PLAIN_ENGLISH.md) — Non-technical narrative explaining the five problems and how the pipeline solves them.

### Key Design Decisions Finalized
1. **Three new SQL tables** designed (not yet created in DB):
   - `public.trader_profiles` — shared catalog of known traders (Discord/Telegram handles mapped to canonical IDs)
   - `public.signal_interpretations` — per-company dual-track interpretation records (LLM track + Quant track + consensus)
   - `public.trader_performance` — per-company rolling analytics (win rate, Sharpe, streaks, last 30 days)

2. **ChartHacker agent** — OpenClaw-native, NOT standalone Python script. This was a critical correction.
   - Workspace: `/root/.openclaw/workspace/chart_hacker/`
   - Registered via: `openclaw agents add chart_hacker --workspace ... --model openrouter/anthropic/claude-sonnet-4`
   - Cron: `openclaw cron add --agent chart_hacker --cron '*/5 * * * *' --tools read,write,exec`
   - Reads chart images from `public.media_items`, outputs structured JSON to `public.signal_interpretations`

3. **InterpretationService** — systemd daemon at `shared/intelligence/interpretation_service.py`
   - Polls `news_items` for uninterpreted signals
   - Writes trigger rows to queue table
   - ChartHacker (OpenClaw cron) reads from queue, processes chart images
   - PerformanceScorer (systemd daemon) reads completed interpretations, updates `trader_performance`

4. **Multi-tenancy rules** — `trader_profiles` and `media_items` are shared; `signal_interpretations` and `trader_performance` are per-company via `company_id`.

5. **Rule 1 (Backtest ≡ Live)** — Every interpretation captures `param_hash` and `model_version` for full reproducibility.

6. **TradingView MCP fallback** — If MCP server available, use it; else fall back to vision LLM. Both logged for accuracy comparison.

---

## 2. What's In Progress / Where We Left Off

**Nothing in progress.** This was a pure design session. No code was written, no migrations applied, no services started.

The design is complete and approved by user. Next step is **implementation** (see Section 8).

---

## 3. Key Decisions Made and Why

| Decision | Rationale |
|----------|-----------|
| ChartHacker is OpenClaw-native, not standalone | User explicitly rejected "rogue agents." All agents must be created through Paperclip/OpenClaw with workspace + SOUL.md + cron. Standalone scripts violate governance. |
| Dual-track interpretation (LLM + Quant) | User wants both "what the trader meant" (LLM) and "what the market actually did" (quant). Conflict between tracks is a feature — flagged for human review. |
| `trader_profiles` is shared across companies | A trader is the same person regardless of which company is analyzing them. Avoids duplicate profile records. |
| `signal_interpretations` and `trader_performance` are per-company | Each company has its own view of whether a signal was good or bad. One company's "buy" is another's "hold." |
| Chart images stored in `public.media_items` (existing table) | Reuses existing collector infrastructure. ChartHacker reads from this table, not from disk directly. |
| `--tools read,write,exec` mandatory for OpenClaw cron | Without this flag, LLM drowns in 50+ MCP tools and returns empty responses (`livenessState: abandoned`). Documented in [`shared/docs/NEW_TRADING_AGENT_HOWTO.md`](shared/docs/NEW_TRADING_AGENT_HOWTO.md). |

---

## 4. Pending TODOs / Unresolved Questions

### P0 (Critical — do first)
1. **Create SQL migration** for three new tables (`trader_profiles`, `signal_interpretations`, `trader_performance`)
2. **Create `shared/intelligence/` directory** and stub `interpretation_service.py`
3. **Register ChartHacker agent in OpenClaw** — run the `openclaw agents add` and `openclaw cron add` commands
4. **Create ChartHacker workspace files** — SOUL.md, TRADE_STATE.md, TRADE_LOG.md at `/root/.openclaw/workspace/chart_hacker/`

### P1 (Important — do after P0)
5. **Implement PerformanceScorer** daemon at `shared/intelligence/performance_scorer.py`
6. **Wire InterpretationService into systemd** — create `systemd/tickles-intelligence.service`
7. **Add tests** for interpretation consensus logic and performance scoring

### P2 (Nice to have)
8. **TradingView MCP integration** — conditional, only if MCP server is available
9. **Video frame extraction placeholder** — for future video signal support

### Unresolved Questions
- **Q1:** What is the exact OpenClaw model string for ChartHacker? User mentioned "openrouter/anthropic/claude-sonnet-4" but we should confirm the exact version.
- **Q2:** Should ChartHacker run every 5 minutes (same as trading agents) or less frequently (e.g., every 15 minutes) since chart analysis is more expensive?
- **Q3:** Do we need a separate queue table for ChartHacker jobs, or can it poll `media_items` directly? Design doc suggests a queue table for better observability.
- **Q4:** What is the fallback if ChartHacker (vision LLM) is unavailable? Design says "skip and retry next cycle" but user may want a circuit breaker.

---

## 5. Important Architecture Context

### ChartHacker is Part of Paperclip OpenClaw — NOT Standalone
This is the single most important context item. The user explicitly said:
> "but the agent is created throup paperclip and part of the company please. i cant have rogue agents."

**Correct pattern:**
```bash
openclaw agents add chart_hacker --workspace /root/.openclaw/workspace/chart_hacker \
  --model openrouter/anthropic/claude-sonnet-4 --non-interactive --json

openclaw cron add --agent chart_hacker --name chart_hacker_cycle \
  --cron '*/5 * * * *' --tz UTC --session isolated \
  --tools read,write,exec --thinking low --timeout-seconds 180 \
  --no-deliver --message 'Read pending chart images from media_items queue, analyze with vision LLM, write structured output to signal_interpretations.'
```

**Wrong pattern (DO NOT DO THIS):**
```bash
# NEVER create a standalone Python script like shared/agents/chart_hacker.py
# and trigger it from InterpretationService. This is what the user rejected.
```

### Existing Trading Agents (for reference)
- `rubicon_surgeon` — workspace at `/root/.openclaw/workspace/rubicon_surgeon/`
- `rubicon_surgeon2` — workspace at `/root/.openclaw/workspace/rubicon_surgeon2/`
- Both use the same pattern: `openclaw agents add` + `openclaw cron add --tools read,write,exec`

### Collector Infrastructure (already exists)
- [`shared/collectors/base.py`](shared/collectors/base.py) — `BaseCollector` ABC, `NewsItem` dataclass, `write_to_db()` persists to Postgres
- [`shared/collectors/discord/discord_collector.py`](shared/collectors/discord/discord_collector.py) — Full Discord collector with high-water marks, media download, TradingView chart URL extraction
- [`shared/collectors/telegram/telegram_collector.py`](shared/collectors/telegram/telegram_collector.py) — Telegram collector with Telethon MTProto, album grouping, media download
- [`shared/collectors/media_extractor.py`](shared/collectors/media_extractor.py) — Unified media extraction service
- [`shared/enrichment/news_enricher.py`](shared/enrichment/news_enricher.py) — Enrichment pipeline (sentiment, relevance, symbol resolution)

### Database Context
- Postgres: `tickles_shared` (shared data) + `tickles_[company]` (per-company data)
- Existing table: `public.news_items` — stores collected messages with `enrichment` jsonb column
- Existing table: `public.media_items` — stores downloaded media (images, videos, files)

---

## 6. Files Edited in This Session

| File | Action | Notes |
|------|--------|-------|
| [`shared/docs/INTELLIGENCE_PIPELINE_DESIGN.md`](shared/docs/INTELLIGENCE_PIPELINE_DESIGN.md) | Created | Full technical design. Initially had ChartHacker as standalone script — corrected to OpenClaw-native after user feedback. |
| [`shared/docs/INTELLIGENCE_PIPELINE_PLAIN_ENGLISH.md`](shared/docs/INTELLIGENCE_PIPELINE_PLAIN_ENGLISH.md) | Created | Non-technical narrative. Updated to clarify ChartHacker is OpenClaw-managed. |
| [`shared/docs/NEW_TRADING_AGENT_HOWTO.md`](shared/docs/NEW_TRADING_AGENT_HOWTO.md) | Read (not modified) | Canonical reference for OpenClaw-native agent creation. Key line: `--tools read,write,exec` is mandatory. |
| [`shared/collectors/base.py`](shared/collectors/base.py) | Read (not modified) | Understood `BaseCollector` interface for design compatibility. |
| [`shared/collectors/discord/discord_collector.py`](shared/collectors/discord/discord_collector.py) | Read (not modified) | Understood Discord collector capabilities (media download, TradingView URL extraction). |
| [`shared/collectors/telegram/telegram_collector.py`](shared/collectors/telegram/telegram_collector.py) | Read (not modified) | Understood Telegram collector capabilities (album grouping, media download). |
| [`shared/enrichment/schema.py`](shared/enrichment/schema.py) | Read (not modified) | Understood `EnrichmentResult`, `EnrichmentStage` for design compatibility. |
| [`shared/enrichment/migrations/2026_04_19_phase23_enrichment.sql`](shared/enrichment/migrations/2026_04_19_phase23_enrichment.sql) | Read (not modified) | Understood existing enrichment schema. |
| [`.roo/rules/global.md`](.roo/rules/global.md) | Modified | Added Session Handoff Rule and Mem0 Auto-Save Rule. |

---

## 7. Current State of Running Services

**No new services were started in this session.** This was a design-only session.

Existing services (from CLAUDE.md and prior knowledge):
- PostgreSQL — running, contains `tickles_shared` and per-company databases
- Redis — running, used by Gateway
- Market Data Gateway — running
- `rubicon_surgeon` and `rubicon_surgeon2` — OpenClaw-managed trading agents, running on 5-minute cron
- Discord collector — likely running as systemd service
- Telegram collector — likely running as systemd service

**No changes to any running services were made.**

---

## 8. Implementation Order (from Design Doc)

1. **P0: Schema migration** — Create `shared/intelligence/migrations/2026_04_25_intelligence.sql` with three tables
2. **P0: Directory structure** — Create `shared/intelligence/__init__.py`, `shared/intelligence/interpretation_service.py`
3. **P0: ChartHacker OpenClaw registration** — Run `openclaw agents add` + `openclaw cron add` commands
4. **P0: ChartHacker workspace** — Create SOUL.md, TRADE_STATE.md, TRADE_LOG.md
5. **P1: PerformanceScorer daemon** — Implement `shared/intelligence/performance_scorer.py`
6. **P1: Systemd service** — Create `systemd/tickles-intelligence.service`
7. **P1: Tests** — Add `shared/tests/test_intelligence.py`
8. **P2: TradingView MCP** — Conditional integration if MCP server available

---

## 9. Resume Command for Next Session

```
Read /opt/tickles/.roo/handoffs/2026-04-25-intelligence-pipeline-handoff.md and confirm understanding. We are ready to begin implementation of the intelligence pipeline. Start with P0: create the SQL migration for the three new tables (trader_profiles, signal_interpretations, trader_performance) in shared/intelligence/migrations/. Then create the shared/intelligence/ directory structure and stub the InterpretationService. Do NOT create ChartHacker as a standalone Python script — it must be registered through OpenClaw with a workspace at /root/.openclaw/workspace/chart_hacker/.
```
