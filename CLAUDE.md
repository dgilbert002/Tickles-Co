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
| MySQL | localhost:3306 | User: admin |
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
│       ├── db.py            # Async MySQL connection pool (aiomysql)
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
  - `shared/utils/db.py` — Async MySQL connection pool
  - `shared/utils/config.py` — Configuration loader
  - `shared/migration/seed_instruments.py` — Bybit instrument seeder
- Current migration step: Step 5 (Indicator Engine)

## Mem0 Scoping Rules — MANDATORY

**Never call `Memory` directly. Always use `get_memory(company, agent)`.**

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
    ├── market-data/
    ├── migration/          ← V2 build blueprint and DDL
    │   ├── CONTEXT_V3.md   ← definitive build blueprint (1400+ lines)
    │   ├── tickles_shared.sql
    │   ├── tickles_company.sql
    │   └── V2_Build_Bundle.zip
    ├── news/
    ├── reference/          ← extracted legacy reference files (131 files)
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
