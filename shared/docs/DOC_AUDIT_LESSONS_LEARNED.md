# Document Audit — Lessons Learned (2026-04-21)

> This is the result of reading every .md file in the Tickles-Co project, sorted by date.
> The goal: identify what's outdated, what's still useful, and what lessons we must carry forward.

---

## 1. Document Inventory (sorted by age, oldest first)

### ANCIENT (April 12-14) — Pre-platform, mostly JarvAIs V2 context

| File | Date | Status | Key Takeaway |
|------|------|--------|-------------|
| `CONTEXT_V3.md` (88KB) | Apr 12 | **OUTDATED but historically valuable** | The original "JarvAIs V2.0 Build Blueprint." Contains the three non-negotiable rules (Rule 1: backtest ≡ live 99.9%, Rule 2: execution accuracy, Rule 3: bounded memory). These rules are STILL VALID and must be enforced. The specific code references and file paths are stale (references MySQL, old folder structure). The CEO metaphor and vision statement are timeless. |
| `STEP1_RECONCILIATION_PLAN.md` | Apr 12 | **DONE** | Naming reconciliation is complete. Keep as history only. |
| `STEP2_SCHEMA_PLAN.md` | Apr 12 | **DONE** | Database schema is implemented. Keep as history only. |
| `STEP4_DATA_COLLECTION_PLAN.md` | Apr 13 | **PARTIALLY DONE** | Candle service + gap detector + collectors are built. BUT: MySQL references are stale (we purged MySQL). CFD-specific features (fake-close candle) are not yet wired. The plan for "smart candle architecture" is still relevant — the candle daemon is silent and needs diagnosis. |
| `Context_roadmap.md` (49KB) | Apr 14 | **OUTDATED but has critical bug reports** | Found 6 bugs in `candle_service.py`, `gap_detector.py`, `retention.py` (CRITICAL: `_validate_partition_name()` never defined — crashes at runtime). These bugs may still exist on the VPS. The "Smart Candle Architecture" discussion is still relevant. |

### OLD (April 17-18) — Platform architecture phase

| File | Date | Status | Key Takeaway |
|------|------|--------|-------------|
| `SOUL_CEO.md` | Apr 17 | **PARTIALLY VALID** | CEO agent soul. The LCM (Lossless Context Management) references are valid. The voice note rules are valid. The "ElevenLabs TTS" and "Groq STT" references need checking — may not be working. Mem0 scoping rules are correct but reference `z-ai/glm-5-turbo` as default model (we now use `gemini-2.0-flash-001`). |
| `TOOLS_CEO.md` | Apr 17 | **MOSTLY OUTDATED** | References MySQL directly (`mysql -u root -e ...`). We purged MySQL. The database list is wrong. Python package list is partially wrong (no `mysql-connector-python` needed). The LCM tools reference is valid. The Qdrant commands are valid. |
| `Collector_STATUS_UPDATE_INTELLIGENCE_PIPELINE.md` (30KB) | Apr 18 | **STILL RELEVANT** | Found 10 consistency issues in the collector pipeline. 3 are critical: `write_to_db()` discards author/media metadata, only first media item recorded, zero Qdrant/vector integration. These are EXACTLY what the Social Alpha Mining (Swarm Pillar 1) needs to fix. The 6-day Phase 3A.2-3A.5 plan is actionable. |
| `ARCHITECTURE.md` (24KB) | Apr 18 | **MOSTLY VALID** | The physical topology diagram is correct (Postgres + ClickHouse + Redis). The "deterministic maths in services; qualitative judgement in agents" principle is gold. The service list is partially outdated. Multi-company constraint is still valid. |
| `CORE_FILES.md` | Apr 18 | **PARTIALLY VALID** | The Janitor allowlist. References MySQL paths that no longer exist. The `trading/**` entry says "pending_cutover" but trading is now live via MCP. The `agents/**` entry says "pending_cutover" but agents are now active. |
| `CompanyIdeas.md` | Apr 18 | **OUTDATED priorities** | Lists JarvAIs as pilot tenant (we now use Rubicon). Capital CFD Co is #2 priority (still valid). The company template is useful. The priority order needs updating. |
| `MEMORY.md` | Apr 18 | **PARTIALLY VALID** | The locked decisions are valid (VPS is canonical, naming rules, file-structure policy). The "Discord Collector Remediation" section documents real bugs that were fixed. The "Morning Brief for Dean" sections are stale news. |
| `SECURITY_ROTATE_CHECKLIST.md` | Apr 18 | **STILL ACTIONABLE** | Telegram bot token, ElevenLabs API key, and Felo API key still need rotation. These are PENDING security items. |

### RECENT (April 19-20) — MCP and Memory phase

| File | Date | Status | Key Takeaway |
|------|------|--------|-------------|
| `SERVICES.md` | Apr 19 | **OUTDATED** | Lists 23 services but the M0 audit found only 8 actually running. The "17 staged services" claim was wrong. |
| `PHASE_38_VALIDATION.md` | Apr 19 | **DONE** | Validation engine phase is complete. Keep as history. |
| `PHASE_39_DRILL.md` | Apr 19 | **DONE** | Drill test phase is complete. Keep as history. |
| `NEW_CHAPTER_AUDIT_REPORT.md` (28KB) | Apr 19 | **STILL VERY RELEVANT** | The definitive asset/resource audit. Section 3 (verdict on every referenced asset) is still the best guide for what to build next. Section 7 (The "New Chapter" Plan with 3 themes) maps directly to our Swarm Roadmap. The architecture diagram (Section 5, Hybrid model) is still correct. The GDELT/Pizzint/Tokenomist data sources are still un-wired. |
| `COMPANIES_AS_IDEAS_PLAN.md` (24KB) | Apr 19 | **STILL VERY RELEVANT** | The "Rolls-Royce-simple company" vision. The 8 glue gaps are partially addressed (MCP tools wired, MemU live) but several remain (Gap 3: OpenRouter cost tracking per agent, Gap 5: Shareholder dashboard, Gap 7: Agent curiosity/skill discovery). The company = idea = office metaphor is still the right model. |
| `ROADMAP_V3.md` (205KB!) | Apr 19 | **PARTIALLY OUTDATED, partially gold** | Massive document (4400+ lines). Phases 0 and 1 are done. Phases 1B-12 are "pending" but the phase structure is still useful. The locked decisions (Section 1) are still valid. The execution truth layer (Phase 2) and forward-test engine (Phase 1D) are still needed. |
| `RUBICON_STATUS_REPORT.md` | Apr 20 | **OUTDATED** | Written when surgeons used Claude Sonnet 4.6 and GPT-4.1. Now both on Gemini Flash. References MySQL. The Postgres queries for surgeon2 are still valid. |
| `NEW_TRADING_AGENT_HOWTO.md` | Apr 20 | **MOSTLY VALID** | The 5-step recipe for spawning agents is still correct. The `--tools read,write,exec` flag is critical knowledge. Cost expectations need updating (Gemini Flash is much cheaper). The model list needs updating. |
| `PHASE_M0_BASELINE.md` | Apr 20 | **VALID** | The VPS audit is accurate. The candle data gap is still the biggest blocker. The venue credential map is still correct. |
| `MCP_AND_MEMORY_PLAN.md` (56KB) | Apr 20 | **CURRENT** | The active plan. M0-M4 done. M1 (candles) is the next priority. |
| `SWARM_ROADMAP.md` | Apr 21 | **CURRENT** | Just written. The 10-pillar vision for the hive mind. |

---

## 2. LESSONS LEARNED — What to implement

### From CONTEXT_V3.md (Apr 12)
- **Rule 1 (backtest ≡ live 99.9%)** — This is the #1 non-negotiable rule. Every strategy that goes live must have its backtest running alongside, and deviations must be explained. We haven't implemented the continuous parity checker yet.
- **Rule 2 (execution accuracy tracked per-entry)** — Every fill should be compared to its expected fill. Slippage, latency, and partial fills must be logged.
- **Rule 3 (bounded memory + partitioned data)** — The 48GB VPS has finite resources. Memory must be hot/warm/cold, DB connections pooled, caches TTL'd.
- **The CEO metaphor** — "You inherited a failing company. Fix the business model, right-size the workforce, cut costs, deliver profit." This is exactly the swarm vision.

### From Context_roadmap.md (Apr 14)
- **`retention.py` has a CRITICAL bug** — `_validate_partition_name()` is called but never defined. This means partition management (creating/dropping monthly candle partitions) crashes at runtime. We need to verify if this bug still exists on the VPS.
- **Smart Candle Architecture** — The concept of candles that "know" their market hours, gap thresholds, and backfill status is still the right design. We should build this as MCP tools (`candles.coverage`, `candles.backfill`).

### From Collector_STATUS_UPDATE_INTELLIGENCE_PIPELINE.md (Apr 18)
- **`write_to_db()` discards rich metadata** — Author, channel, media_type, media_urls are collected but thrown away before DB write. This is exactly what Pillar 1 (Social Alpha Mining) needs to fix.
- **Zero vector integration in collectors** — No Qdrant embeddings for any news/social content. The 6-day Phase 3A.2-3A.5 plan would fix this.
- **The "intelligence pipeline" concept** — An agent should be able to answer "what bullish BTC setups did professional traders post in the last 24 hours?" We're still not there yet.

### From COMPANIES_AS_IDEAS_PLAN.md (Apr 19)
- **Gap 3: OpenRouter cost tracking per agent** — We had a cost crisis ($200 in a few days). This gap is now CRITICAL, not just "nice to have." We need `finance_events` per agent per call.
- **Gap 5: Shareholder dashboard** — Still needed. The competition layer (Swarm Pillar 3) needs a leaderboard view.
- **Gap 7: Agent curiosity** — Agents should browse the MCP tool catalog and request new tools. This maps to `tools.catalogue` and `tools.suggest` from M6.

### From NEW_CHAPTER_AUDIT_REPORT.md (Apr 19)
- **GDELT** — The geopolitics oracle. Free API, 100+ languages, 15-min updates. Still not wired into our alt-data pipeline.
- **Pizzint** — Pentagon Pizza Index OSINT feed. Still not wired.
- **Tokenomist** — Unlock schedules for the Sylvan short strategy. Still not wired.
- **Twilly's 13-exchange scanner** — 700 lines of working Python that adds dYdX, Drift, Lighter, MEXC, Gate.io, KuCoin to our coverage. Still not adopted.
- **The 3-Prompt Analysis Framework** — Autopsy / Post-Mortem / Feedback-Loop. This is now partially implemented via `learning.py` MCP tools but not yet enforced as mandatory post-trade routines.

---

## 3. LESSONS LEARNED — What NOT to implement

### From CLAUDE.md
- **MySQL is dead.** Every reference to `mysql -u root`, `tickles_shared` MySQL database, `aiomysql`, and MySQL partitions is OUTDATED. We moved to Postgres + ClickHouse. Do NOT re-introduce MySQL.
- **The `news/` folder structure is wrong** — The `CLAUDE.md` references `shared/news/` but the actual collectors live in `shared/collectors/`. Don't recreate the wrong structure.

### From SOUL_CEO.md and TOOLS_CEO.md
- **Don't give agents direct DB access.** The CEO agent's `TOOLS.md` shows raw `mysql` commands. The right approach is MCP tools (`md.candles`, `banker.positions`, etc.) — not direct SQL. Agents should never run `SELECT` directly.
- **Don't hardcode model names.** The CEO SOUL references specific models (`z-ai/glm-5-turbo`). Models change frequently. Use config-driven model selection.

### From ROADMAP_V3.md
- **Don't build a custom orchestrator.** Paperclip already has `routines`, `heartbeat_runs`, and `workspace_operations`. OpenClaw has `agents`, `cron`, and `sessions`. Don't reinvent scheduling.
- **Don't build a custom finance ledger.** Paperclip has `finance_events`, `cost_events`, and `budget_policies`. Mirror into them, don't replace them.
- **Don't enable Risk Agent by default.** Dean explicitly decided Risk Agent (LLM judgment layer) is OFF by default. Only enable per-company when desired.

### From the cost crisis (April 20)
- **Never give agents access to all tools.** The `--tools` whitelist is mandatory. Without it, the LLM sees 50+ tool schemas and silently returns empty responses OR makes expensive calls.
- **Never use expensive models for tactical agents.** Gemini Flash at $0.01/cycle is the right choice for 5-minute trading cycles. Claude Sonnet at $0.72/cycle burned $200 in days.
- **Always set OpenRouter spending limits.** The $200 cap saved us from worse damage. Set $50 limits per key.

### From the Discord collector incident (April 18)
- **Don't store plaintext tokens in world-readable JSON files.** The Discord bot token was in a `discord_config.json` readable by anyone. Tokens must be in `/etc/tickles/*.env` with mode 600.
- **Don't trust silent failures.** The MySQL→Postgres migration broke `write_to_db()` silently (wrong schema prefix). Every DB write should log success/failure.

---

## 4. Action Items from this audit

| Priority | Item | Source |
|----------|------|--------|
| **CRITICAL** | Fix `retention.py` crash (undefined `_validate_partition_name`) | Context_roadmap.md |
| **CRITICAL** | Implement per-agent OpenRouter cost tracking → `finance_events` | COMPANIES_AS_IDEAS_PLAN Gap 3 |
| **HIGH** | Wire GDELT + Pizzint + Tokenomist into alt-data pipeline | NEW_CHAPTER_AUDIT_REPORT |
| **HIGH** | Fix `write_to_db()` to preserve author/channel/media metadata | Collector_STATUS_UPDATE |
| **HIGH** | Implement Qdrant embeddings for news/social content | Collector_STATUS_UPDATE |
| **MEDIUM** | Build shareholder/competition dashboard | COMPANIES_AS_IDEAS_PLAN Gap 5 |
| **MEDIUM** | Adopt Twilly's 13-exchange scanner as MCP tool | NEW_CHAPTER_AUDIT_REPORT |
| **MEDIUM** | Rotate Telegram bot token + ElevenLabs API key + Felo API key | SECURITY_ROTATE_CHECKLIST |
| **LOW** | Update CLAUDE.md (remove MySQL references, update model list) | CLAUDE.md |
| **LOW** | Update TOOLS_CEO.md (remove MySQL, add Postgres/MCP tools) | TOOLS_CEO.md |
| **LOW** | Update CompanyIdeas.md priority order (Rubicon first, not JarvAIs) | CompanyIdeas.md |

---

*End of document audit. This should be referenced when planning the next implementation phase.*
