# VPS: vmi3220412 — Tickles Infrastructure

## Tailscale
- **Hostname:** vmi3220412.trout-goblin.ts.net
- **Tailscale IP:** 100.71.74.12

## Services

| Service | Address | Notes |
|---------|---------|-------|
| OpenClaw | ws://127.0.0.1:18789 | WebSocket server; exposed via Tailscale at https://vmi3220412.trout-goblin.ts.net:8443/ |
| MemClaw | (OpenClaw skill) | Felo LiveDoc integration; workspace: "V2 Migration"; skill at `~/.openclaw/workspace/skills/memclaw/` |
| Paperclip | http://127.0.0.1:3100 | Web app; exposed via Tailscale at https://vmi3220412.trout-goblin.ts.net/ |
| Postgres | localhost:5432 | User: admin |
| Qdrant (mem0) | localhost:6333 | Docker container (restart:always), data at /opt/qdrant_data |
| VS Code Server | http://127.0.0.1:8080 | code-server@root.service; exposed via Tailscale at https://vmi3220412.trout-goblin.ts.net:8080/ |

## Tailscale Serve Config
- `https://vmi3220412.trout-goblin.ts.net/` → `http://127.0.0.1:3100` (Paperclip)
- `https://vmi3220412.trout-goblin.ts.net:8080/` → `http://127.0.0.1:8080` (VS Code Server)
- `https://vmi3220412.trout-goblin.ts.net:8443/` → `http://127.0.0.1:18789` (OpenClaw)

## Mem0 Configuration
- **Vector store:** Qdrant at localhost:6333
- **LLM provider:** OpenRouter (https://openrouter.ai/api/v1)
- **Model:** z-ai/glm-5-turbo
- **API key env var:** `OPENROUTER_API_KEY`
- **Config file:** `/opt/tickles/shared/utils/mem0_config.py`
- **Test script:** `/opt/tickles/shared/utils/mem0_test.py`

## V2 Project Structure

### Canonical File Locations
- Database schemas: `shared/migration/`
  - `tickles_shared.sql` - Shared database schema
  - `tickles_company.sql` - Company database template
- Shared utilities: `shared/utils/`

### Directory Structure
```
/opt/tickles/
├── projects/
│   ├── [company]/          # Per-company project directory
│   │   ├── config/         # Configuration files
│   │   ├── logs/           # Log files
│   │   └── strategies/     # Strategy implementations
├── shared/
│   ├── backtesting/        # Backtest engine (Step 6)
│   ├── connectors/         # Exchange adapters
│   │   ├── base.py          # BaseExchangeAdapter ABC + Candle dataclass
│   │   └── ccxt_adapter.py  # CCXT adapter (Bybit, BloFin, Bitget)
│   ├── market-data/        # Candle collection + timing
│   │   ├── candle_service.py    # Main candle collection orchestrator
│   │   ├── gap_detector.py      # Gap detection and backfill
│   │   ├── retention.py         # Partition management + retention
│   │   └── timing_service.py    # Adaptive market hours timing
│   ├── migration/          # Database schema definitions
│   ├── news/               # News/social collectors
│   │   ├── base.py              # BaseCollector ABC + NewsItem dataclass
│   │   ├── rss_collector.py     # RSS news collector (fully implemented)
│   │   ├── telegram_collector.py # Telegram collector (stub)
│   │   ├── discord_collector.py # Discord collector (stub)
│   │   └── tradingview_monitor.py # TradingView monitor (stub)
│   └── utils/              # Shared utility libraries
│       ├── db.py            # Async Postgres connection pool (asyncpg)
│       ├── config.py        # Configuration loader (env vars)
│       ├── mem0_config.py   # Mem0 memory integration
│       └── mem0_test.py     # Mem0 smoke test
```

### Database Naming
- Shared database: `tickles_shared`
- Company databases: `tickles_[company]` (e.g. `tickles_jarvais`)

### V2 Migration Status
- `shared/reference/v2_build/` has been REMOVED (superseded by `shared/migration/`)
- Step 1: Reconcile Naming — ✅ Complete
- Step 2: Database Schema (DDL) — ✅ Complete (14 shared tables, 10 company tables, 226 indicators)
- Step 3: VPS Infrastructure — ✅ Complete (Git repo, services verified)
- Step 4: Data Collection Services — ✅ Complete (architecture + code)
  - `shared/connectors/` — BaseExchangeAdapter + CCXTAdapter
  - `shared/market-data/` — CandleService + GapDetector + RetentionManager + TimingService
  - `shared/news/` — BaseCollector + RSSCollector + stubs (Telegram, Discord, TradingView)
  - `shared/utils/db.py` — Async Postgres connection pool
  - `shared/utils/config.py` — Configuration loader
  - `shared/migration/seed_instruments.py` — Bybit instrument seeder
- Current migration step: Step 5 (Indicator Engine)

## Mem0 Scoping Rules — MANDATORY

**Never call `Memory` directly. Always use `get_memory(company, agent)`.**

### D1 — No agent-output `.md` writes (enforced 2026-05-03)

Agent output (trade state snapshots, trade logs, autopsies, decisions, etc.) MUST be persisted to mem0, NOT to `.md` files. The legacy [`TRADE_STATE.md`](shared/daemons/surgeon_trader.py:1) and [`TRADE_LOG.md`](shared/daemons/surgeon_trader.py:1) overlays were retired during the `.md` → mem0 migration (see [`.roo/handoffs/2026-05-03-md-vs-mem0-audit.md`](.roo/handoffs/2026-05-03-md-vs-mem0-audit.md:1)).

The only `.md` files agents may still write are the provisioning overlays:
`AGENT.md`, `SOUL.md`, `IDENTITY.md`, `TOOLS.md`, `USER.md`, `HEARTBEAT.md`, `BOOTSTRAP.md`, `MEMORY.md`.

This rule is enforced by [`shared/tests/test_grep_guard_md_writes.py`](shared/tests/test_grep_guard_md_writes.py:1) which runs as part of the standard pytest collection. Any new `.md` writer outside the allowlist (or outside `shared/tests/`, `shared/scripts/`, `shared/jobs/`, `shared/provisioning/`, `shared/templates/`) will fail CI with a clear remediation message.

To migrate historical `.md` content, use [`shared/scripts/migrate_md_to_mem0.py`](shared/scripts/migrate_md_to_mem0.py:1) (`--dry-run` first; never `--apply` without an audit of the manifest).

```python
from shared.utils.mem0_config import get_memory

memory, agent_id = get_memory("jarvais", "cody")
memory.add("Found new table positions", user_id="jarvais", agent_id=agent_id)
memory.search("what tables exist", user_id="jarvais", agent_id=agent_id)
```

### Model fallback chain

The `ScopedMemory` wrapper automatically falls back through models if the primary one fails:
1. **MEM0_MODEL** (env var, defaults to `z-ai/glm-5-turbo`) — primary choice
2. **google/gemini-2.0-flash-001** (~$0.10/M input) — cheap & fast
3. **deepseek/deepseek-chat** (~$0.14/M input) — alternative
4. **openrouter/auto** — last resort, OpenRouter picks the cheapest available

All are extremely cheap for Mem0's simple extraction tasks. Set `MEM0_MODEL=google/gemini-2.0-flash-001` to prefer Gemini's lower cost, or leave blank to stick with GLM-5-Turbo.

### Why scoping matters

Each call creates a fully isolated memory scope via two independent barriers:

| Barrier | Mechanism | Effect |
|---------|-----------|--------|
| Collection | `collection_name = tickles_{company}` | Separate Qdrant collection per company |
| Identity | `user_id={company}`, `agent_id={company}_{agent}` | Separate vector namespace within collection |

### Naming conventions

| company arg | agent arg | Qdrant collection | agent_id tag |
|-------------|-----------|-------------------|--------------|
| `jarvais` | `cody` | `tickles_jarvais` | `jarvais_cody` |
| `jarvais` | `schemy` | `tickles_jarvais` | `jarvais_schemy` |
| `jarvais` | `audrey` | `tickles_jarvais` | `jarvais_audrey` |
| `crypto` | `cody` | `tickles_crypto` | `crypto_cody` |

A future `tickles_crypto` project gets its own collection automatically — zero config needed, zero contamination possible.

## Memory Isolation — Verified (2026-04-26)

Dev and trading memories are isolated at **five independent layers**, all verified by [`shared/tests/test_mem0_isolation.py`](shared/tests/test_mem0_isolation.py):

| Layer | Mechanism | Verified |
|-------|-----------|----------|
| 1. Qdrant collection | `tickles_dev` vs `tickles_rubicon` | ✓ separate collections |
| 2. Vector store object | Fresh `Memory.from_config()` per call | ✓ separate client instances |
| 3. Config | `collection_name` baked into config dict | ✓ different configs |
| 4. user_id | `"dev"` vs `"rubicon"` in payload | ✓ payload-level filter |
| 5. agent_id | `"dev_{agent}"` vs `"rubicon_{agent}"` | ✓ payload-level filter |

### Active namespaces

| Collection | Purpose | Writable? |
|------------|---------|-----------|
| `tickles_dev` | Build environment (Roo, Claude Code, Hermes) | ✓ yes — use `get_dev_memory(agent=...)` |
| `tickles_shared` | Cross-company institutional memory (MemU) | ✓ yes — via MemU/pgvector |
| `tickles_rubicon` | Test/sandbox trading company | ✓ yes — via `get_memory("rubicon", ...)` |
| `tickles_jarvais` | Frozen legacy V1 | ✗ **NO new writes** — read-only reference |
| `tickles_tradelab`, `tickles_testcorp` | Other test companies | ✓ yes |

### Sparse-collection gotcha

Semantic search in a **sparse collection** (few points) returns the closest matches available even when none are semantically relevant. A search for "X" in a collection containing only "Y" memories will return "Y" results — this is **NOT** an isolation leak. It is normal vector-search behavior in low-cardinality collections.

**Correct verification:** check the `user_id` field on returned payloads, **not** the count of returned results. See [`test_mem0_isolation.py`](shared/tests/test_mem0_isolation.py) for the canonical pattern.

### Dev memory helper

```python
from shared.utils.mem0_config import get_dev_memory
mem, agent_id = get_dev_memory(agent="roo")
mem.add("Decision: ...", user_id="dev", agent_id=agent_id)
```

**Never** use `get_memory(company="tickles", ...)` — this creates a garbage `tickles_tickles` collection.

## MemClaw Configuration
- **Skill status:** `✓ ready` (openclaw-workspace source)
- **Workspace name:** V2 Migration
- **Purpose:** Felo LiveDoc project management — create/open/switch projects, save artifacts, query history, manage tasks
- **API key env var:** `FELO_API_KEY`
- **Installed at:** `~/.openclaw/workspace/skills/memclaw/`

## V2 Migration
- **Blueprint:** `/opt/tickles/shared/migration/CONTEXT_V3.md` — definitive build document (1400+ lines, merges V2 + Gemini architectural review + normalized schemas + implementation plan)
- **Shared DDL:** `/opt/tickles/shared/migration/tickles_shared.sql` — 14 tables
- **Company DDL:** `/opt/tickles/shared/migration/tickles_company.sql` — 10 tables (replace COMPANY_NAME)
- **Reference bundle:** `/opt/tickles/shared/reference/` — 131 files from both legacy systems (extracted from V2_Build_Bundle.zip)

## Environment Variables
Set in `/root/.bashrc` and `/home/paperclip/.bashrc`:
- `OPENROUTER_API_KEY` — OpenRouter API key
- `FELO_API_KEY` — Felo API key (used by MemClaw)
- `MEM0_MODEL` — (optional) LLM model for Mem0 memory operations. Defaults to `z-ai/glm-5-turbo`. Fallback chain: Gemini Flash → DeepSeek → OpenRouter auto (cheapest). Set if you want a different primary model (e.g., `MEM0_MODEL=google/gemini-2.0-flash-001`)

## Folder Structure

```
/opt/tickles/
├── CLAUDE.md               ← this file
├── new-project.sh          ← creates a new project directory
├── delete-project.sh       ← removes a project directory
├── projects/               ← individual trading/AI projects
│   ├── btc-mean-reversion/
│   ├── crypto-ai-learner/
│   └── gold-scalping/
└── shared/                 ← shared libraries/utils
    ├── backtesting/
    ├── connectors/
    ├── intelligence/       ← Phase 3B+ intelligence pipeline
    │   ├── interpretation_service.py   ← LLM+quant interpretation daemon
    │   ├── performance_scorer.py       ← Trader accuracy scoring daemon
    │   └── migrations/
    │       ├── 2026_04_26_phase3b_intelligence.sql
    │       └── 2026_04_26_phase3b_intelligence_ROLLBACK.sql
    ├── market-data/
    ├── migration/          ← V2 build blueprint and DDL
    │   ├── CONTEXT_V3.md   ← definitive build blueprint (1400+ lines)
    │   ├── tickles_shared.sql
    │   ├── tickles_company.sql
    │   └── V2_Build_Bundle.zip
    ├── mcp/                ← MCP tool registry and transports
    │   ├── bin/
    │   │   ├── tickles_mcpd.py         ← HTTP daemon (:7777)
    │   │   └── tickles_mcp_stdio.py    ← Stdio entry (OpenClaw Desktop)
    │   └── tools/
    │       ├── intelligence.py         ← 6 intelligence MCP tools
    │       └── test_intelligence.py    ← Smoke tests
    ├── news/
    ├── reference/          ← extracted legacy reference files (131 files)
    ├── templates/
    │   ├── chart_hacker/   ← ChartHacker OpenClaw-native agent templates
    │   │   ├── SOUL.template.md
    │   │   ├── config.template.json
    │   │   └── spawn_chart_hacker.sh
    │   └── trading_agent/  ← Twilly-faithful surgeon agent templates
    └── utils/
        ├── mem0_config.py  ← mem0 Memory config (Qdrant + OpenRouter)
        └── mem0_test.py    ← mem0 smoke test script
```

## V2 Migration Status

### Database Creation
- `tickles_shared` database: created, 14 tables, 18 config rows
- `tickles_jarvais` database: created, 10 tables, 9 config rows
- `indicator_catalog`: seeded with 226 indicators (215 from Capital 2.0 + 11 JarvAIs V1)
- Candle partitions: 2024-01 through 2026-12 + future
- Note: partition maintenance job needed before 2027-01

## Intelligence Pipeline (Phase 3B+)

> **⚠️ CANONICAL ROADMAP — READ THIS FIRST**
>
> The source-of-truth plan for everything below (and the entire intelligence/learning/dashboard rebuild) lives in:
>
> [`shared/docs/INTELLIGENCE_UNIFIED_PLAN.md`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:1)
>
> - **Status:** active master plan (supersedes [`shared/docs/prompt.md`](shared/docs/prompt.md:1) and [`shared/docs/INTELLIGENCE_PIPELINE_DESIGN.md`](shared/docs/INTELLIGENCE_PIPELINE_DESIGN.md:1))
> - **Effort:** ~31 days of focused engineering
> - **Phases (14):** `0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11 → L → R`
>   - **0** Document Contract & pre-flight — resolved: ClickHouse scope confirmed, writer-registry 30-day grace, loop detection replaces budget caps, BLOCK duplicate images (0.5d)
>   - **1** Universal LLM gateway + cost log + loop detection / frequency analysis (1.5d)
>   - **2** Schema unification — `signal_interpretations`, `tracked_positions`, `position_postmortems`, reason-freeze trigger (1.5d)
>   - **3** Per-company DBs + payload-retention sweeper (1d)
>   - **4** `signals.report` CSV/HTML export with thumbnail size cap (1.5d)
>   - **5** Served HTML manage panel + CSRF + rate-limit + audit trail (G2) (2.5d)
>   - **6** Tracked-position writer + pgvector reason-similarity + prompt registry + master-sync seed (2.0d)
>   - **7** PostmortemService + `memu_outbox` + listener + OpenClaw shell (3.0d)
>   - **8** ChartHackerOpinionService + opinion budget (2.5d)
>   - **9** Trading-zone monitor + collector rate-limits + image pHash dedup (2.0d)
>   - **10** Single-writer policy — `actor_instance` + writer-domain registry [BC] with 30-day grace (2.5d)
>   - **11** `edge_score` + CoachService + `trader_performance` reconciliation (4.0d)
>   - **L** Dashboard read-only rebuild — services/intelligence/leaderboard/positions/audit + chart renderer + anchor-link contract (5.0d)
>   - **R** CI gates — schema-diff [BL], writer-registry grep [BC], master-sync [BN], cron heartbeats [BM] (2.0d)
> - **Embedded findings:** all **20 original A–T patches** + all **42 AA–BP devil's-advocate findings** are inlined in the relevant phase.
> - **Uniform phase contract — every phase 0–R contains exactly 8 sub-sections, in this order:**
>   1. **Purpose** — why this phase exists
>   2. **What** — the deliverables, numbered
>   3. **Where** — file paths (NEW / EXTEND), tables, services
>   4. **When** — day-by-day breakdown
>   5. **Why** — rationale & links to upstream findings
>   6. **How** — the actual code/SQL/config to ship
>   7. **Risks & mitigations** — failure modes + counter-measures
>   8. **Benchmark checklist** — verifiable acceptance criteria
> - **Resume command for the next session:**
>   *"Read [`shared/docs/INTELLIGENCE_UNIFIED_PLAN.md`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:1), find the next unchecked benchmark in the latest phase, and execute it. Do not start a new phase until the previous phase's Benchmark checklist is fully ticked."*
> - **Resumability index:** the [Mini-Memory section](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:4141) at the bottom of the plan contains the full phase order, hard rules, files-most-likely-modified list, after-each-phase ritual, and the uniform phase contract — read it first when resuming cold.
>
> ### Phase 3 Implementation Status (2026-04-30)
>
> **Status:** ✅ COMPLETE — Payload persistence layer shipped.
>
> | Component | File | Purpose |
> |-----------|------|---------|
> | Payload Store | [`shared/intelligence/payload_store.py`](shared/intelligence/payload_store.py:1) | Atomic JSON writer for LLM request/response payloads with secret redaction (API keys, tokens stripped before disk write) |
> | Payload Retention | [`shared/jobs/payload_retention.py`](shared/jobs/payload_retention.py:1) | Daily cron: compress JSON >30 days old into `.tar.zst`, delete archives >365 days old unless `.retention_locked` marker present |
> | Prompt Versioning | [`shared/intelligence/prompts/chart_analysis.json`](shared/intelligence/prompts/chart_analysis.json:1) | Added `"version": "2026.04.30-chart-analysis-v1"` semver field |
> | LlmResult Extension | [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:110) | 5 new fields: `model_resolved`, `request_path`, `response_path`, `prompt_version`, `prompt_hash` |
> | INSERT Wiring | [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:940) | `write_signal_interpretation()` writes all Phase 2 columns including payload paths and correlation_id |
> | Tests | [`shared/intelligence/test_payload_store.py`](shared/intelligence/test_payload_store.py:1) (23 tests) + [`shared/jobs/test_payload_retention.py`](shared/jobs/test_payload_retention.py:1) (18 tests) | Secret redaction, atomic writes, compression, deletion, dry-run mode, disk budget alerts |
>
> **Tests:** 144 passed, 2 skipped (trio backend) — all Phase 0–3 components verified.
>
> ### Phase 4 Implementation Status (2026-04-30)
>
> **Status:** ✅ COMPLETE — Signal-Review Export + Opticals static serve shipped.
>
> | Component | File | Purpose |
> |-----------|------|---------|
> | Signal Review Exporter | [`shared/intelligence/signal_review_export.py`](shared/intelligence/signal_review_export.py:1) | Daemon that exports `signal_interpretations` to CSV + HTML every 60s. JOINs `news_items`, `media_items`, `trader_profiles`. |
> | HTML Template | [`shared/intelligence/templates/signal_review.html.jinja2`](shared/intelligence/templates/signal_review.html.jinja2:1) | Self-contained dark-theme HTML with inline CSS, color-coded directions, collapsible LLM I/O panels, 30s auto-refresh |
> | Thumbnail Policy | [`shared/intelligence/signal_review_export.py`](shared/intelligence/signal_review_export.py:58) | 3-tier: <200 KB inline base64, 200 KB–5 MB lazy `file://` link, >5 MB oversized placeholder |
> | Atomic File Swap | [`shared/intelligence/signal_review_export.py`](shared/intelligence/signal_review_export.py:246) | `tmpfile → os.replace` for both CSV and HTML; `latest.csv` / `latest.html` symlinks |
> | Symlink Integrity | [`shared/intelligence/signal_review_export.py`](shared/intelligence/signal_review_export.py:145) | Daemon refuses to run if `/opt/tickles/opticals/signal_review` is not a symlink to `shared/reports/signal_review` |
> | CSV Column Contract | [`shared/intelligence/signal_review_export.py`](shared/intelligence/signal_review_export.py:27) | 25 frozen columns; downstream Phase L re-reads this |
> | XSS Prevention | [`shared/intelligence/templates/signal_review.html.jinja2`](shared/intelligence/templates/signal_review.html.jinja2:1) | `select_autoescape` on all string interpolations; only `render_thumb()` uses `\|safe` |
> | Registry Entry | [`shared/services/registry.py`](shared/services/registry.py:1) | `signal-review-exporter` registered as `kind="worker"` |
> | Env Vars | [`.env.template`](.env.template:1) | `SIGNAL_REVIEW_LOOKBACK_H`, `SIGNAL_REVIEW_REPORT_DIR`, `SIGNAL_REVIEW_PUBLIC_BASE_URL` |
>
> **Tailscale Serve Mapping:**
> ```bash
> # Run once (idempotent):
> tailscale serve --bg --set-path /opticals/ /opt/tickles/opticals/
> # Verify:
> tailscale serve status
> ```
>
> **Tests:** 27 passed (signal_review_export + xss) — all Phase 4 components verified.
>
> ### Phase 5 Implementation Status (2026-04-30)
>
> **Status:** ✅ COMPLETE — Served HTML Manage Panel shipped.
>
> | Component | File | Purpose |
> |-----------|------|---------|
> | CSRF Tokens | [`shared/dashboard/csrf.py`](shared/dashboard/csrf.py:1) | `__Host-csrf` cookie (Secure, SameSite=Strict, httponly=False so JS can read). `X-CSRF-Token` header required on POST/PUT/DELETE. `secrets.compare_digest()` constant-time comparison. |
> | Rate Limiter | [`shared/dashboard/rate_limit.py`](shared/dashboard/rate_limit.py:1) | Token-bucket per `(session_token, "read"|"write")`. Read cap 600/min, Write cap 30/min. Tunable via `MANAGE_RATE_READ` / `MANAGE_RATE_WRITE` env vars. |
> | Default-Deny Auth | [`shared/dashboard/server.py`](shared/dashboard/server.py:53) | `ALLOWED_PUBLIC` explicit allow-list. `__Host-session` cookie on OTP verify (Secure, HttpOnly, SameSite=Strict, 12h). Auth middleware checks bearer → query string → cookie. |
> | DB Views | [`shared/intelligence/manage_panel/db_views.py`](shared/intelligence/manage_panel/db_views.py:1) | Re-uses `shared.catalogue.db` for sources/channels/users/hierarchy/leaderboard. New: `list_recent_signals`, `list_open_positions`, `list_closed_positions`, `get_trader_signals`, `get_trader_positions`, `get_trader_profile`. |
> | Route Handlers | [`shared/intelligence/manage_panel/server_routes.py`](shared/intelligence/manage_panel/server_routes.py:1) | Jinja2 Environment with `select_autoescape`. 6 read-only views + 5 mutating API endpoints. `attach_routes(app)` mounts on existing dashboard app. |
> | HTML Templates | [`shared/intelligence/manage_panel/templates/`](shared/intelligence/manage_panel/templates/) | `base.html.jinja2` (nav + error banner), `sources.html.jinja2`, `signals.html.jinja2`, `positions.html.jinja2`, `leaderboard.html.jinja2`, `trader_drill.html.jinja2` |
> | Static Assets | [`shared/intelligence/manage_panel/static/`](shared/intelligence/manage_panel/static/) | `manage.css` (dark theme, mobile-first, CSS variables, responsive tables), `manage.js` (`tickFetch()` hardened wrapper, CSRF injection, error banners) |
> | TUI Read-Only Gate | [`shared/catalogue/tui_manager.py`](shared/catalogue/tui_manager.py:36) | `TICKLES_TUI_READONLY=1` (default). `_readonly_guard()` blocks source/channel/user toggles and collection/tracking flag changes. Read-only views (dashboard, hierarchy, leaderboard) still work over SSH. |
> | Launcher Redirect | [`manage_sources.py`](manage_sources.py:1) | Default: prints web panel URL. `--tui` flag for legacy SSH mode. `MANAGE_PANEL_PUBLIC_BASE_URL` env var. |
> | Registry Entry | [`shared/services/registry.py`](shared/services/registry.py:440) | `manage-panel` registered as `kind="api"` |
> | Env Vars | [`.env.template`](.env.template:1) | `TICKLES_TUI_READONLY=1`, `MANAGE_PANEL_PUBLIC_BASE_URL`, `MANAGE_RATE_READ=600`, `MANAGE_RATE_WRITE=30` |
>
> **Tailscale Serve Mapping:**
> ```bash
> # The /manage/* routes are served by the dashboard aiohttp app.
> # Add to tailscale serve (idempotent):
> tailscale serve --bg --set-path /manage/ http://127.0.0.1:3100/manage/
> # Or if dashboard runs on a different port, adjust accordingly.
> ```
>
> **Tests:** 41 new tests (auth_default_deny + csrf + rate_limit + panel_routes + tui_readonly) — all Phase 5 components verified.
>
> ### Phases 6-11 Implementation Status (2026-05-01)
>
> **Status:** ✅ COMPLETE — Tracked-position writer, postmortem, ChartHacker opinion, zone monitor, single-writer policy, edge scorer + coach all shipped per the unified plan.
>
> | Phase | Component | File | Status |
> |-------|-----------|------|--------|
> | **6** | InterpretationService → tracked_positions writer | [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:1079) `create_tracked_position_from_interpretation` | ✅ |
> | **6** | pgvector reason-similarity helper | [`shared/intelligence/reason_similarity.py`](shared/intelligence/reason_similarity.py:1) | ✅ |
> | **6** | Prompt-versions registry | [`shared/intelligence/prompt_registry.py`](shared/intelligence/prompt_registry.py:1) | ✅ |
> | **6** | Instrument normaliser (D6 canonical `BTC/USDT`) | [`shared/utils/instrument_normaliser.py`](shared/utils/instrument_normaliser.py:1) | ✅ |
> | **6** | Master-schema sync | [`shared/scripts/master_sync.py`](shared/scripts/master_sync.py:1) | ✅ |
> | **7** | PostMortemService | [`shared/intelligence/postmortem_service.py`](shared/intelligence/postmortem_service.py:1) | ✅ |
> | **7** | `memu_outbox` + listener | [`shared/memu/listener_service.py`](shared/memu/listener_service.py:1) + [`shared/memu/broadcast_payload.py`](shared/memu/broadcast_payload.py:1) | ✅ |
> | **8** | ChartHackerOpinionService | [`shared/intelligence/chart_hacker_opinion_service.py`](shared/intelligence/chart_hacker_opinion_service.py:1) | ✅ |
> | **8** | Opinion budget gate | [`shared/intelligence/opinion_budget.py`](shared/intelligence/opinion_budget.py:1) | ✅ |
> | **9** | Trading-zone monitor + image pHash dedup | [`shared/intelligence/zone_filter.py`](shared/intelligence/zone_filter.py:1) + [`shared/intelligence/image_phash.py`](shared/intelligence/image_phash.py:1) | ✅ |
> | **10** | Writer-domain registry | [`shared/intelligence/writer_registry.py`](shared/intelligence/writer_registry.py:1) | ✅ |
> | **11** | Edge scorer + CoachService | [`shared/intelligence/edge_scorer.py`](shared/intelligence/edge_scorer.py:1) + [`shared/intelligence/coach_service.py`](shared/intelligence/coach_service.py:1) + [`shared/intelligence/edge_scorer_service.py`](shared/intelligence/edge_scorer_service.py:1) | ✅ |
> | **11** | ChartHacker Guru (cross-trader analysis) | [`shared/intelligence/chart_hacker_guru.py`](shared/intelligence/chart_hacker_guru.py:1) | ✅ |
>
> ### Phase L Implementation Status (2026-05-04)
>
> **Status:** ✅ COMPLETE — Live dashboard at `https://vmi3220412.trout-goblin.ts.net/` with 8 visible tabs + hidden Trader Drill. Visual redesign (D3) and Smart Refresh (D4) shipped.
>
> | Component | File | Notes |
> |-----------|------|-------|
> | Per-company DB pool cache | [`shared/dashboard/db_pools.py`](shared/dashboard/db_pools.py:1) | LRU-cached `get_company_pool()` |
> | Snapshot aggregator | [`shared/dashboard/snapshot.py`](shared/dashboard/snapshot.py:1) | 10s TTL snapshot cache, fan-out across active companies |
> | Anchor-link contract | [`shared/dashboard/anchors.py`](shared/dashboard/anchors.py:1) | `#sig-/pos-/opn-/pm-/trade-/interp-` deep-link fragments |
> | Cached chart renderer | [`shared/dashboard/chart_renderer.py`](shared/dashboard/chart_renderer.py:1) | Idempotent matplotlib SVG, cache-key includes prompt_version + sr_hash |
> | WebSocket | [`shared/dashboard/ws.py`](shared/dashboard/ws.py:1) | `/ws/queue` 5s tick |
> | Routes | [`shared/dashboard/server.py`](shared/dashboard/server.py:1) | `/api/snapshot, /services, /leaderboard, /signals, /positions, /interpretations, /trader-drill, /charts/{id}, /media/{id}` |
> | Tabs (live in `web/index.html`) | [`shared/dashboard/web/index.html`](shared/dashboard/web/index.html:52) | overview, leaderboard, signals, positions, interpretations, queue, news, learning, config (+ hidden trader-drill) |
> | Auth + CSRF + rate-limit | [`shared/dashboard/auth.py`](shared/dashboard/auth.py:1) + [`shared/dashboard/csrf.py`](shared/dashboard/csrf.py:1) + [`shared/dashboard/rate_limit.py`](shared/dashboard/rate_limit.py:1) | Telegram-OTP, default-deny, `__Host-session` cookie |
> | Visual Redesign (D3) | [`shared/dashboard/static/app.css`](shared/dashboard/static/app.css:1) | Dark glassmorphism theme (#0a0e14), KPI grid, modern sidebar |
> | Smart Refresh (D4) | [`shared/dashboard/static/app.js`](shared/dashboard/static/app.js:1) | `REFRESH_REGISTRY` with per-tab cadences, relative time helpers |
>
> **Anchor-link convention:** every card on every tab carries `id="kind-N"` where `kind ∈ {sig, pos, opn, pm, trade, interp}` and `N` is the canonical primary-key id. Pasting `https://vmi3220412.trout-goblin.ts.net/positions#pos-12345` scrolls + highlights the card.
>
> ### Phase R Implementation Status (2026-05-04)
>
> **Status:** ✅ COMPLETE — CI Gates & Operational Canaries fully implemented.
>
> | Component | File | Status |
> |-----------|------|--------|
> | Schema-drift detector | [`shared/scripts/schema_diff.py`](shared/scripts/schema_diff.py:1) | ✅ shipped |
> | Writer-domain enforcer | [`shared/scripts/writer_registry_grep.py`](shared/scripts/writer_registry_grep.py:1) | ✅ shipped |
> | Master-schema sync gate | [`shared/scripts/master_sync.py`](shared/scripts/master_sync.py:1) | ✅ shipped |
> | Cron heartbeat helper | [`shared/intelligence/heartbeat.py`](shared/intelligence/heartbeat.py:1) | ✅ shipped |
> | Cron canary watchdog | [`shared/intelligence/cron_canary.py`](shared/intelligence/cron_canary.py:1) | ✅ shipped |
> | `cron_heartbeats` table | [`shared/intelligence/migrations/2026_05_04_phase_r_cron_heartbeats.sql`](shared/intelligence/migrations/2026_05_04_phase_r_cron_heartbeats.sql:1) | ✅ shipped |
> | Dashboard staleness query | [`shared/dashboard/server.py:313`](shared/dashboard/server.py:313) `handle_services` | ✅ wired |
> | GitHub Actions workflow | [`.github/workflows/schema-and-writer-gates.yml`](.github/workflows/schema-and-writer-gates.yml:1) | ✅ shipped |
> | Tests | [`shared/tests/test_schema_diff.py`](shared/tests/test_schema_diff.py:1), [`test_writer_registry_grep.py`](shared/tests/test_writer_registry_grep.py:1), [`test_master_sync_gate.py`](shared/tests/test_master_sync_gate.py:1), [`test_cron_canary.py`](shared/tests/test_cron_canary.py:1) | ✅ shipped |
> | Systemd units | [`systemd/tickles-schema-drift.timer`](systemd/tickles-schema-drift.timer:1), [`systemd/tickles-schema-drift.service`](systemd/tickles-schema-drift.service:1), [`systemd/tickles-cron-canary.service`](systemd/tickles-cron-canary.service:1) | ✅ shipped |
> | Schema snapshots | [`shared/scripts/snapshots/tickles_shared.snapshot.sql`](shared/scripts/snapshots/tickles_shared.snapshot.sql:1), [`tickles_company.snapshot.sql`](shared/scripts/snapshots/tickles_company.snapshot.sql:1) | ✅ shipped |
> | Heartbeat wire-in | postmortem / edge_scorer / coach / chart_hacker_opinion service ticks | ✅ wired |
> | Makefile targets | [`Makefile`](Makefile:1) (`refresh-snapshots`, `gate-local`, `test-all`) | ✅ shipped |
>
> ### Phase X Implementation Status (2026-05-04)
>
> **Plan:** [`shared/docs/PHASE_X_DASHBOARD_AND_PIPELINE_PLAN.md`](shared/docs/PHASE_X_DASHBOARD_AND_PIPELINE_PLAN.md:1) + [`shared/docs/PHASE_X0_POSITION_PIPELINE_DIAGNOSIS.md`](shared/docs/PHASE_X0_POSITION_PIPELINE_DIAGNOSIS.md:1)
>
> **Status:** ✅ COMPLETE — Pipeline triage and all dashboard sub-phases shipped.
>
> | Sub-phase | Description | Status |
> |-----------|-------------|--------|
> | **X.0 / F1** | Symbol normalisation at INSERT — D6 canonical `BTC/USDT` | ✅ shipped (commit `a9e44d5` family, [`shared/utils/instrument_normaliser.py`](shared/utils/instrument_normaliser.py:1)) |
> | **X.0 / F2** | Expiry-based stalled-position closer | ✅ shipped |
> | **X.0 / F3** | Real LLM postmortem (kills hardcoded stubs) | ✅ shipped ([`shared/intelligence/postmortem_service.py`](shared/intelligence/postmortem_service.py:1)) |
> | **X.0 / F4** | Systemd units for PositionMonitor + PostMortemService | ✅ shipped (11 units in [`systemd/`](systemd/:1)) |
> | **X.0 / F5** | INSERT-time SL/TP sanity guard | ✅ shipped |
> | **X.0 / F9** | P&L backfill for 77 orphans (D8) | ✅ shipped ([`shared/scripts/backfill_orphan_positions.py`](shared/scripts/backfill_orphan_positions.py:1)) |
> | **X.0 / F10** | Fee-accurate close (D7) | ✅ shipped ([`shared/intelligence/fee_calc.py`](shared/intelligence/fee_calc.py:1)) |
> | **X.0 / F11** | `PositionSnapshot` field-name alignment | ✅ shipped (commit `a9e44d5`) |
> | **X.0 / F12** | Cosmetic `BTCUSDT → BTC/USDT` historical UPDATE | ✅ shipped (2026-05-03 07:14 UTC) |
> | **X.1** | PositionMonitor → PostgreSQL wiring | ✅ shipped ([`shared/intelligence/position_monitor.py`](shared/intelligence/position_monitor.py:1)) |
> | **X.2** | actor_instance + writer-registry runtime wiring | ✅ shipped |
> | **X.3** | Interpretation enrichment | ✅ shipped |
> | **X.4** | News Feed tab ("Chat Server") | ✅ shipped ([`shared/dashboard/news_routes.py`](shared/dashboard/news_routes.py:1)) |
> | **X.5** | Cross-Tab Interpretation Drawer | ✅ shipped ([`shared/dashboard/interpretation_drawer_routes.py`](shared/dashboard/interpretation_drawer_routes.py:1)) |
> | **X.6** | Config tab | ✅ shipped ([`shared/dashboard/config_routes.py`](shared/dashboard/config_routes.py:1)) |
> | **X.7** | E2E smoke test | ✅ verified 2026-05-04 |
>
> ### Phase Y Implementation Status (2026-05-04)
>
> **Plan:** [`shared/docs/PHASE_Y_LEARNING_DASHBOARD_PLAN.md`](shared/docs/PHASE_Y_LEARNING_DASHBOARD_PLAN.md:1) — v2 design.
>
> **Status:** ✅ COMPLETE — Learning dashboard v2 shipped.
>
> | Sub-phase | What | Status |
> |-----------|------|--------|
> | Y.0 | MemU enum extension (`scope_kind ENUM` add `'learning'`) | ✅ shipped |
> | Y.1 | Three migrations: `mem0_recall_log`, skill_score views, learning enum | ✅ shipped |
> | Y.2 | Python skill_scorer + edge_scorer integration | ✅ shipped |
> | Y.3 | Snapshot providers for 7d/14d/30d Memory Feed | ✅ shipped |
> | Y.4 | UI: Learning tab, skill-score sparkline, memory-feed widget | ✅ shipped |
> | Y.5 | Sidebar wiring: skill-vs-luck headline on Overview tab | ✅ shipped |
>
> ### Dashboard v2 (May 2026) — Drawer UX, Live Price, Position Tracking, Feed Hygiene
>
> **Status:** ✅ COMPLETE — Four-slice dashboard improvement project shipped on top of Phase L. Source-of-truth diagnosis & fixes live in handoffs and this section.
>
> **Pre-slice bug fixes:**
>
> | Bug | Description | Fix |
> |-----|-------------|-----|
> | **A — Vision-LLM symbol guessing** | LLM was inferring `BTC/USDT` from chart-only posts that had no visible ticker | Patched [`shared/intelligence/prompts/chart_analysis.json`](shared/intelligence/prompts/chart_analysis.json:1) to version `2026.05.04-no-symbol-guessing-v2` (forces `UNKNOWN` when ticker not visible); fixed `instrument_resolved_from` labelling in [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:2218) so `inferred` vs `actor_text` vs `chart_ocr` is honest |
> | **B — Stuck media rows** | 120 image rows stuck in `skipped_vision_unavailable` from prior outage | Re-queued via state-machine reset back to `downloaded` |
>
> **Slice 1 — Drawer UX Overhaul:**
>
> | Component | File | Notes |
> |-----------|------|-------|
> | Wider drawer | [`shared/dashboard/static/app.css`](shared/dashboard/static/app.css:1) | `clamp(720px, 80vw, 1600px)` so the drawer breathes on wide monitors but never overflows on tablets |
> | **Image proxy** (NEW) | [`shared/dashboard/media_proxy.py`](shared/dashboard/media_proxy.py:1) | `/api/media/proxy` — SSRF-guarded image proxy (allowlisted hosts, blocked private IP ranges, max-bytes cap, per-session rate limit). Avoids CSP/CORS pain when rendering Discord/Twitter media in-drawer |
> | Multi-media gallery | [`shared/dashboard/news_routes.py`](shared/dashboard/news_routes.py:1) `fetch_media_for_news_item()` | Provider method returns ordered media list; drawer renders first frame + thumbnails |
> | `chart_hacker_trades` table formatter | [`shared/dashboard/static/app.js`](shared/dashboard/static/app.js:1) | Renders the trades JSONB as a real table instead of raw JSON dump |
> | Empty-drawer gallery branch | [`shared/dashboard/static/app.js`](shared/dashboard/static/app.js:1) | When no interpretation exists yet, drawer still shows media gallery + raw text |
> | Tests | [`shared/tests/test_media_proxy.py`](shared/tests/test_media_proxy.py:1) | SSRF guard, host allowlist, rate-limit, oversize-body cases |
>
> **Slice 2 — Always-on Quant with Live Price:**
>
> | Component | File | Notes |
> |-----------|------|-------|
> | Resilient quant track | [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:1) `run_quant_track()` | Now falls back to CCXT live ticker when local 1m candles are stale or missing — quant track never silently skips |
> | Live-price stamp | `quant_indicators` JSONB | New key `current_price_at_interp` captured at interpretation time (decimal serialised), so post-mortems can compute slippage vs. signal |
> | **`/api/price` route** (NEW) | [`shared/dashboard/price_routes.py`](shared/dashboard/price_routes.py:1) | 30-second in-process cache; symbol+exchange keyed; default-deny auth, rate-limited |
> | **Live-price helper** (NEW shared lib) | [`shared/market_data/live_price.py`](shared/market_data/live_price.py:1) | Single source of truth for "what is BTC trading at right now" — used by both `run_quant_track()` and `/api/price` so they can never disagree |
> | Mem0 recall in drawer | [`shared/dashboard/static/app.js`](shared/dashboard/static/app.js:1) | Drawer queries dev/company mem0 for prior context on the same symbol |
> | Tests | [`shared/tests/test_live_price.py`](shared/tests/test_live_price.py:1), [`shared/tests/test_price_routes.py`](shared/tests/test_price_routes.py:1) | Cache TTL, fallback chain, auth, rate-limit |
>
> **Slice 3 — Position Tracking (Trader signals → `tracked_positions` + Surgeon aggregation):**
>
> | Component | File | Notes |
> |-----------|------|-------|
> | Schema migration | `tracked_positions` | Added `entry_price_source ENUM('actor_explicit','chart_levels','live_price','last_candle_close')`, `metadata JSONB`, partial index on `WHERE status='open'` for fast dashboard reads |
> | Entry-price resolver | [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:1) `_resolve_entry_price()` | 4-tier fallback: actor explicit → chart-detected entry levels → live price → last candle close. Each tier stamps `entry_price_source` for transparency |
> | Dashboard read union | [`shared/dashboard/server.py`](shared/dashboard/server.py:1) `aggregate_open_positions()` | Now reads BOTH `positions_current` (real broker fills) AND `tracked_positions` (signal-derived shadow positions). UI tags each row with its origin |
> | **Surgeon position bridge** (NEW) | [`shared/intelligence/surgeon_position_bridge.py`](shared/intelligence/surgeon_position_bridge.py:1) | Translates trader-signal `tracked_positions` into Surgeon-readable position objects so Surgeon-1 / Surgeon-2 can aggregate signal exposure with real broker exposure |
> | **Surgeon position reconciler** (NEW) | [`shared/intelligence/surgeon_position_reconciler.py`](shared/intelligence/surgeon_position_reconciler.py:1) | Periodic reconciler: matches signal-derived positions to real fills (when they happen), closes stale shadow positions, deduplicates. **Runs as its own systemd unit** — see operational notes below |
> | Modified | [`shared/daemons/surgeon_trader.py`](shared/daemons/surgeon_trader.py:1), [`shared/daemons/surgeon2_trader.py`](shared/daemons/surgeon2_trader.py:1) | Now consume the bridge so risk math sees both real + shadow exposure |
> | Tests | [`shared/tests/test_surgeon_position_reconciler.py`](shared/tests/test_surgeon_position_reconciler.py:1) | Match logic, stale-close behaviour, idempotency |
>
> **Slice 4 — Feed Hygiene:**
>
> | Change | Where | Why |
> |--------|-------|-----|
> | Prefilter rejection → `skipped_not_chart` | [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:1) | Stops wasting vision-LLM tokens on memes / screenshots / non-chart images |
> | Video filtering | vision pipeline | Videos are skipped at the media-extractor stage; status tagged `skipped_unsupported_media` |
> | Stale text-only news cleanup | enrichment pipeline | News items with no media and no actionable enrichment after 6 h are auto-marked `skipped_no_content` |
> | Status enum expansion | `media_items.processing_status`, `news_items.processing_status` | New values: `skipped_not_chart`, `skipped_unsupported_media`, `skipped_no_content` |
> | Tests | [`shared/tests/test_feed_hygiene.py`](shared/tests/test_feed_hygiene.py:1) | Each rejection path produces the correct enum value and never blocks the queue |
>
> **New modules introduced (cheat sheet):**
>
> | Module | Path | Role |
> |--------|------|------|
> | `media_proxy` | [`shared/dashboard/media_proxy.py`](shared/dashboard/media_proxy.py:1) | SSRF-safe image proxy for the dashboard drawer |
> | `price_routes` | [`shared/dashboard/price_routes.py`](shared/dashboard/price_routes.py:1) | `/api/price` HTTP route with 30 s cache |
> | `live_price` | [`shared/market_data/live_price.py`](shared/market_data/live_price.py:1) | Shared live-price helper (CCXT + cache) |
> | `surgeon_position_bridge` | [`shared/intelligence/surgeon_position_bridge.py`](shared/intelligence/surgeon_position_bridge.py:1) | Trader-signal → Surgeon position adapter |
> | `surgeon_position_reconciler` | [`shared/intelligence/surgeon_position_reconciler.py`](shared/intelligence/surgeon_position_reconciler.py:1) | Periodic shadow-vs-real reconciliation daemon |
>
> **New HTTP routes:**
>
> | Route | Method | Auth | Purpose |
> |-------|--------|------|---------|
> | `/api/media/proxy` | `GET` | session | Fetches an external image through the SSRF-guarded proxy. Query: `?url=<encoded>` |
> | `/api/price` | `GET` | session | Returns latest cached spot price. Query: `?symbol=BTC/USDT&exchange=bybit` |
>
> **Schema changes (`tracked_positions`):**
>
> | Column | Type | Notes |
> |--------|------|-------|
> | `entry_price_source` | `ENUM('actor_explicit','chart_levels','live_price','last_candle_close')` | Records which tier of `_resolve_entry_price()` was used |
> | `metadata` | `JSONB` | Free-form per-position metadata (resolver intermediate values, ChartHacker opinion id, etc.) |
> | partial index | `(status, instrument)` `WHERE status='open'` | Powers `aggregate_open_positions()` without a full-table scan |
>
> **Media status enum expansion:**
>
> Both `media_items.processing_status` and `news_items.processing_status` gained:
> - `skipped_not_chart` — Gemini Flash prefilter said this image is not a trading chart
> - `skipped_unsupported_media` — video / audio / unsupported MIME
> - `skipped_no_content` — text-only news item with no actionable enrichment after 6 h
>
> These are **terminal** states. They are NOT retried by the daemon; only manual re-queue (or a deliberate prompt-version bump) clears them.
>
> **Operational notes — systemd:**
>
> The Slice 3 reconciler ([`shared/intelligence/surgeon_position_reconciler.py`](shared/intelligence/surgeon_position_reconciler.py:1)) needs its own systemd unit (template: `tickles-surgeon-position-reconciler.service`). Recommended interval: 60 s. Heartbeat wires into the existing `cron_heartbeats` table from Phase R so the cron-canary watchdog flags it if it stalls.
>
> The new `/api/price` and `/api/media/proxy` routes are mounted by the existing dashboard aiohttp app — **no new systemd unit is required for them**.
>
> **Caveats / things to remember:**
>
> 1. The image proxy is the ONLY allowed path for external image fetches from the browser. Do not bypass it — the SSRF guard, host allowlist, and byte cap are non-negotiable.
> 2. `current_price_at_interp` is stamped at interpretation time, NOT trade time. Distance-from-entry on the dashboard uses live price minus this stamp; do not retro-edit it.
> 3. Shadow positions in `tracked_positions` are **NOT** real fills. The Surgeon reconciler is responsible for matching them to real fills when they appear; until then they are advisory only and must never be used to compute realised P&L.
> 4. `skipped_*` states are terminal. If you are debugging "why is this media item not interpreted?", check the status first — re-queue is a deliberate operator action.
>
> ---
>
> The block below describes the **legacy Phase 3B** (already shipped). Anything new — schema changes, new daemons, dashboard work, CI gates — MUST come from the unified plan, not from this legacy block.

---

### Legacy Phase 3B (already shipped — do not duplicate)

Three new tables, two daemons, one OpenClaw-native vision agent, and six MCP tools.

### New Database Tables

| Table | Database | Purpose |
|-------|----------|---------|
| `public.trader_profiles` | `tickles_shared` | Shared catalog of known traders (Discord/Telegram/Twitter handles) with accuracy scoring |
| `public.signal_interpretations` | `tickles_[company]` | Per-company dual-track interpretations (LLM + quant consensus) of media items |
| `public.trader_performance` | `tickles_[company]` | Per-company scored performance metrics (accuracy, Sharpe, drawdown) per trader per period |

Migration: `shared/intelligence/migrations/2026_04_26_phase3b_intelligence.sql`
Rollback: `shared/intelligence/migrations/2026_04_26_phase3b_intelligence_ROLLBACK.sql`

### Daemons

| Daemon | File | Purpose | Interval |
|--------|------|---------|----------|
| `InterpretationService` | `shared/intelligence/interpretation_service.py` | Polls `media_items` for `downloaded` rows, runs LLM vision + quant track, writes consensus to `signal_interpretations` | 5 minutes (env: `INTERPRETATION_POLL_INTERVAL_S`) |
| `PerformanceScorer` | `shared/intelligence/performance_scorer.py` | Compares old signals to actual price movement, scores trader accuracy, writes `trader_performance` | 1 hour (env: `SCORER_POLL_INTERVAL_S`) |

### ChartHacker — OpenClaw-Native Vision Agent

- **Template:** `shared/templates/chart_hacker/SOUL.template.md`
- **Config:** `shared/templates/chart_hacker/config.template.json`
- **Spawn script:** `shared/templates/chart_hacker/spawn_chart_hacker.sh`
- **Model:** `anthropic/claude-sonnet-4` primary, `google/gemini-2.0-flash-001` fallback
- **Cron:** `*/5 * * * *` (every 5 minutes)
- **Budget:** $2.00/day
- **Pattern:** OpenClaw-native — registered via `openclaw agents add` + `openclaw cron add --tools read,write,exec`

### MCP Intelligence Tools

Registered in both `tickles_mcpd` (HTTP :7777) and `tickles_mcp_stdio` (OpenClaw Desktop).

| Tool | Purpose |
|------|---------|
| `intelligence.chart_analyze` | Direct vision LLM analysis of a chart image path |
| `intelligence.interpret` | Trigger one cycle of InterpretationService |
| `intelligence.trader_profile` | Get or create a trader profile in shared catalog |
| `intelligence.trader_score` | Get latest performance score for a trader |
| `intelligence.signals.recent` | List recent signal interpretations with filters |
| `intelligence.signals.pending` | Count media items awaiting interpretation |

### Key Design Decisions

- **No separate queue table** — uses `media_items.processing_status` state machine (`downloaded` → `analyzing` → `analyzed` / `skipped_vision_unavailable`)
- **Dual-track interpretation** — LLM track (reads chart image) + Quant track (reads last 100 1m candles, computes RSI/EMA/ATR/Bollinger) → consensus engine
- **Freshness Guard** — market data reads validated against `market_data_fresh` threshold
- **Rule 1 (Backtest ≡ Live)** — every interpretation captures `param_hash` and `model_version` for reproducibility
- **Multi-tenancy** — `trader_profiles` is shared; `signal_interpretations` and `trader_performance` are per-company
- **Vision LLM down** — marks `skipped_vision_unavailable`, retries next cycle (no hard failure)
- **No Dashboard Auth** — Authentication, CSRF, and Rate Limiting are disabled for the dashboard as it runs on a private server.
- **CI Gates** — Schema drift and writer-registry checks are enforced in CI to prevent regressions.

### How to Run

```bash
# Run InterpretationService (foreground, one cycle then exit)
python3 -m shared.intelligence.interpretation_service --once

# Run PerformanceScorer (foreground, one cycle then exit)
python3 -m shared.intelligence.performance_scorer --once

# Run as continuous daemons
python3 -m shared.intelligence.interpretation_service
python3 -m shared.intelligence.performance_scorer

# Spawn ChartHacker
sudo bash shared/templates/chart_hacker/spawn_chart_hacker.sh jarvais chart_hacker

# Run smoke tests
python3 -m pytest shared/mcp/tools/test_intelligence.py -v
python3 -m pytest shared/tests/test_schema_diff.py
python3 -m pytest shared/tests/test_writer_registry_grep.py
python3 -m pytest shared/tests/test_master_sync_gate.py
python3 -m pytest shared/tests/test_cron_canary.py
```

## Installed Software
- **code-server** 4.115.0 — VS Code in browser (systemd service: `code-server@root`)
- **Docker** — runs Qdrant container
- **mem0ai** Python package — AI memory layer
- **qdrant-client** Python package — Qdrant vector DB client
- **tailscale** — VPN + serve proxy
- **memclaw** — OpenClaw skill for Felo LiveDoc project management

## VS Code Extensions
- Roo Code (RooVeterinaryInc.roo-cline) — AI coding assistant

## Useful Commands

```bash
# Check all services
systemctl status code-server@root
docker ps
tailscale serve status

# Restart services
systemctl restart code-server@root
docker restart qdrant

# Run mem0 test
OPENROUTER_API_KEY="..." python3 /opt/tickles/shared/utils/mem0_test.py

# Project management
/opt/tickles/new-project.sh <name>
/opt/tickles/delete-project.sh <name>
```
