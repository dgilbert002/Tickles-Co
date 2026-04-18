# MEMORY.md — CEO Agent, Tickles & Co

Last restored: 2026-04-15 (contamination from sub-agent writes cleaned)
Last major update: 2026-04-18 (Phase 3A.1 intelligence schema landed, Discord collector remediated)

## 🔖 DISCORD COLLECTOR REMEDIATION + PHASE 3A.1 (locked 2026-04-18, afternoon)

- The unmanaged `discord_collector` process (PID 371375, running since Apr 13)
  was killed. The 25 GB of downloaded media in
  `/opt/tickles/shared/collectors/discord/data/discord_media/` was deleted —
  **none of it was ever persisted to Postgres** because three bugs silently
  dropped every write for 5 days:
    1. `base.py` used MySQL `INSERT IGNORE INTO tickles_shared.news_items` —
       invalid Postgres syntax + wrong schema (tables live in `public`).
    2. `discord_collector._load_high_water_marks` also used the `tickles_shared.`
       schema prefix → every HWM load silently returned `{}`.
    3. Plaintext Discord bot token in world-readable `discord_config.json`.
- Phase 3A.1 migration (`_vps_source/migration/2026_04_18_phase3a1_collector_sources.sql`)
  is applied to `tickles_shared`. New tables: `public.collector_sources` (35 rows
  seeded: 2 servers + 33 channels), `public.media_items`. `public.news_items`
  gained 8 columns (source_id, channel_name, author, author_id, message_id,
  metadata jsonb, has_media, media_count).
- Token moved: `/etc/tickles/discord.env` (mode 600, root:root).
  `discord_config.json` no longer contains the `token` key.
- Systemd unit `tickles-discord-collector.service` is installed but
  **disabled** and **inactive**. Dean turns it on with
  `sudo systemctl enable --now tickles-discord-collector.service` when ready.
- Full collector refactor (Phase 3A.2–3A.5 — media processor, Qdrant
  embeddings, dual-store search) is **deferred**; research doc
  `/opt/tickles/shared/migration/Collector_STATUS_UPDATE_INTELLIGENCE_PIPELINE.md`
  describes the remaining 6 days of work.
- Rollback: `_vps_source/migration/2026_04_18_phase3a1_collector_sources_ROLLBACK.sql`
  + `cp /opt/tickles/backups/phase3a1_2026-04-18/*.bak` over the current files.

## 🔖 VPS IS CANONICAL (locked 2026-04-18, late)

- The source of truth is **the live VPS** at `/opt/tickles/shared/`. Local
  `_vps_source/` is a mirror. When they diverge, pull the VPS down, not the
  other way around.
- Live folders (code running under systemd): `backtest/`, `candles/`, `catalog/`,
  `collectors/`, `connectors/`, `guardrails/`, `market_data/`, `memu/`, `migration/`,
  `scripts/`, `services/`, `utils/`.
- Exchange adapters are in `connectors/` (one file per exchange, read + write
  in the same class — that's what CCXT expects). There is **no** `trading/adapters/`.
- Non-exchange collectors (discord/telegram/news/tradingview) are in `collectors/`.
  There is **no** `market_data/collectors/`.
- The local `local_runner/` folder is desktop-only and is **never** synced to
  the VPS.
- Authoritative layout: `ARCHITECTURE.md §3`. The three-commit history on
  2026-04-18 (commits `0baaa5d`, `c6a3799`, `170c466`, plus the sync-from-vps
  commit that lands next) captures the exact state.


## 🔖 NAMING (locked 2026-04-18)

- The system on the VPS is called **"The Platform"**. No product name yet.
- `JarvAIs`, `Capital 2.0`, `Capital Two` are **legacy companies** whose code
  we mine for reference. They are not this system.
- Stop saying "V1" or "V2" for the system itself. Doc versioning (e.g.
  `ROADMAP_V3.md` vs `ROADMAP_V2.md`) is fine — that's normal doc versioning.

## 🔖 FILE-STRUCTURE POLICY (locked 2026-04-18)

- **Organic migration, not atomic restructure.** No big-bang rename of the
  live VPS file layout. See `ROADMAP_V3.md §Phase 1A` for the rules.
- New code lands in the feature folders (`market_data/`, `trading/`,
  `agents/`, `migration/`, `_archive/` — created 2026-04-18).
- Legacy files stay put until a new file replaces them; then they move to
  `_archive/YYYY-MM-DD_<reason>/` with a `MANIFEST.md`.
- Never delete. `_archive/` is the only destination for retired files.
- Every new file signature takes `company_id` (multi-company is a first-class
  constraint throughout).
- Authoritative layout: `ARCHITECTURE.md §3`. Protection list: `CORE_FILES.md`.
  Cursor enforcement: `.cursor/rules/file-structure.mdc`.



## 🚨 MORNING BRIEF FOR DEAN (2026-04-17, 06:00 UTC+4)

Good morning. Phase 0 (Postgres/ClickHouse/MemU infra) **and** Phase 1
(backtest stack + 1m candle daemon + catalog API + local runner) both
finished overnight. You asked for "a stunningly well executed plan where
memu, mem0, lcm, everything is working well from memory, Postgres has
backtests in it, they're accessible and retrievable, backtests have a
parallel and cpu manager like CapitalTwo2.0" — that is now what's on
the VPS, minus a few clearly-flagged rough edges.

Full details in `/opt/tickles/shared/migration/ROADMAP_V2.md` §10.

### TL;DR of what's live right now
- **Backtest engine** — deterministic, SHA256 param-hash dedup, realistic
  fills (spread+slippage+fees), SL/TP, deflated-Sharpe. Rule #1 honoured:
  the same engine code will drive live fills.
- **23 indicators** — core TA (SMA, EMA, RSI-Wilder, MACD, ATR, BB%B,
  OBV, VWAP, MFI), smart-money (BOS, FVG, liquidity-sweep, volume-spike),
  and the +86% crash-protection block ported from CapitalTwo2.0.
- **5 building-block strategies** — ema_cross, sma_cross, rsi_reversal,
  bollinger_pullback, anchored_vwap_pullback. Agents combine these.
- **Parallel workers** — Redis-backed queue with atomic claim + hash-seen
  dedup + heartbeat + stuck-job reaper. `tickles-bt-workers.service`
  runs 4 CPU workers (env-capped). Crashed workers auto-respawn.
- **1m candle daemon** — `tickles-candle-daemon.service`, currently
  collecting 50 instruments across binance + bybit + capital.com with
  producer/consumer queues and ON CONFLICT upserts.
- **Data Catalog REST API** — `tickles-catalog.service` on
  `127.0.0.1:8765`. Agents ask it what indicators, strategies,
  instruments, and top backtests exist. Lookup avg 75.8 ms.
- **Flat-file `backtests.txt`** — pipe-delimited greppable index of every
  run. Hourly rebuild planned; currently rebuilt on demand.
- **Desktop local runner** — code-complete in `/opt/tickles/local_runner/`.
  `pystray` tray icon, SSH-tunneled claim loop so your home PC can chew
  through jobs from the same queue. README has PyInstaller steps.
- **MemU client** — `memu/client.py`. pgvector insights table,
  write/search/count + `pg_notify` pub-sub. Wired but not yet exercised
  by a live agent.

### Last E2E smoke run (baseline to regress against)
- Fetched 2160 candles across BTC/ETH/SOL.
- Enqueued 54 jobs → 54 CH runs + 135 trades written.
- `backtests.txt` rebuilt with 54 rows.
- Top lookup 75.8 ms (target < 100 ms).
- Best sharpe: **SOL/USDT rsi_reversal(period=7, ob=70, os=30)** →
  sharpe 4.51, winrate 72.7%, return 20.76%, mdd −7.2%, 22 trades.

### Three commands to prove it yourself
```bash
# 1. All new services healthy
systemctl is-active tickles-catalog tickles-candle-daemon tickles-bt-workers

# 2. Catalog responds
curl -s http://127.0.0.1:8765/stats | jq
curl -s 'http://127.0.0.1:8765/backtests/top?n=5&sort=sharpe' | jq

# 3. Candles actually streaming
PGPASSWORD='Tickles21!' psql -U admin -d tickles_shared -h 127.0.0.1 -c \
  "SELECT timeframe, COUNT(*) FROM candles GROUP BY timeframe"
```

### Rough edges (what I'd tackle first today)
1. **MATIC delisted on Bybit** — candle daemon logs a warning every 5 s.
   `UPDATE instruments SET is_active=FALSE WHERE symbol='MATIC/USDT' AND exchange='bybit';`
2. **MemU round-trip not yet exercised** — 5-minute job: have Schemy
   write an insight, have Cody search for it, confirm pgvector returns.
3. **Local runner packaging** — follow the README to produce the two
   Windows `.exe` files. Needed before you can "set it and forget it"
   on your home PC.
4. **Walk-forward / OOS split** — `oos_sharpe` / `oos_return_pct` are
   placeholder zeros. Phase 7 work.

### What I will NOT do without you
- **Drop MySQL.** Still running on :3306 as rollback safety net.
- **Stop Phase 0 collectors.** Discord / Telegram collectors are
  patched but not restarted against Postgres — that's Phase 1 cutover
  proper and I want you in the room.
- **Modify `/root/.openclaw/openclaw.json`** or any infra file flagged
  in ROADMAP_V2 §5.3. Hard ban stays.

### Phase 0 still holds (all from last night)
Run these three lines and expect all three to succeed:

```bash
systemctl is-active postgresql clickhouse-server redis-server
PGPASSWORD='Tickles21!' psql -h 127.0.0.1 -U admin -d tickles_shared -c "SELECT COUNT(*) FROM pg_tables WHERE schemaname='public'"
clickhouse-client --user admin --password 'Tickles21!' --database backtests --query 'SHOW TABLES'
```

### Credentials (also in `.env`)
- Postgres admin: `admin / Tickles21!`  (superuser)
- Postgres read-only: `schemy / Schemy2026!`  (observer — can SELECT everything, can't write)
- ClickHouse admin: `admin / Tickles21!`
- All services listen on 127.0.0.1 only. No external exposure.

## My Identity

I am the CEO agent for Tickles & Co. I assist Dean with strategic decisions, task delegation, and operational oversight. I am the central intelligence hub. I talk to Dean via Telegram, Paperclip console, and OpenClaw web.

All channels share one session (dmScope=main). One brain, one memory, one conversation.

## About Dean

- **Name:** Dean
- **Timezone:** UTC+4 (Dubai)
- **Role:** Project owner, not a developer
- **Preferences:** Clear explanations (like explaining to a 21-year-old), organized code, robust logging, no assumptions, always ask before changes
- **Frustrations:** Agents that don't learn, repeat mistakes, work on wrong codebases, or are verbose/chatty
- **Communication:** Casual, direct, doesn't want filler or pleasantries

## Company: Tickles & Co

Multi-company autonomous trading platform. Starting with shared infrastructure, then adding individual trading companies.

### Architecture
- **Shared infrastructure:** `/opt/tickles/shared/` — candles, news, instruments, strategies, backtests
- **Company databases:** `tickles_jarvais` (first company), more to come
- **VPS:** vmi3220412, running MySQL, Qdrant, OpenClaw, Paperclip, collectors
- **Goal:** Start with $500 capital, scale to millions, fully autonomous trading

### Current Status (2026-04-17, morning)
- **Phase 0 (infra migration):** ✅ COMPLETE (overnight 2026-04-16/17).
- **Phase 1 (backtest stack + data plumbing):** ✅ COMPLETE overnight.
  - Backtest engine, 23 indicators, 5 strategies, Redis queue, multi-
    process worker pool, ClickHouse writer, `backtests.txt` accessible
    layer, catalog REST API, MemU client, desktop local-runner: all
    deployed. Services: `tickles-catalog`, `tickles-candle-daemon`,
    `tickles-bt-workers` — all active.
  - End-to-end smoke test green. 54 runs + 135 trades in ClickHouse.
    Top-lookup 75.8 ms. Best sharpe 4.51 (SOL/USDT rsi_reversal).
- **Phase 1 cutover (Discord/Telegram/candle\_service flip):** ⏳
  DEFERRED to waking hours — Dean in the room.
- **Mem0:** Working — local embeddings (all-MiniLM-L6-v2, 384 dims). Runs in parallel with MemU forever.
- **MemU:** Client deployed (`/opt/tickles/shared/memu/client.py`), pgvector insights table auto-created on first use, `pg_notify` pub-sub wired. No agent has exercised it end-to-end yet.
- **Discord collector:** PATCHED for Postgres ON CONFLICT; not yet restarted (cutover pending Dean).
- **Telegram collector:** PATCHED; not yet restarted.
- **1m candle daemon:** 🟢 running on `tickles-candle-daemon.service`, collecting 50 instruments across binance, bybit, capital.com.
- **Schema:** Postgres `tickles_shared` has 14 logical tables + 37 candle partitions + `instruments` (50 rows) + `indicator_catalog` (23 rows). Company DB `tickles_jarvais` has 10 tables. ClickHouse `backtests` has 5 tables + 1 MV (54 runs, 135 trades from smoke test).
- **Data:** Discarded during migration per Dean's call ("it's garbage, we're developing"). MySQL retains old data for rollback only.
- **Next:** Finish Phase 1 cutover (with Dean), then Phase 2 (exercise MemU end-to-end + package local runner).

## My Team (Observer Agents)

| Agent | Role | Heartbeat | Workspace |
|---|---|---|---|
| Cody | Code Engineer — reads code, understands architecture | 15m | /root/.openclaw/workspace/cody/ |
| Schemy | DB Specialist — reads schemas, tracks changes | 30m | /root/.openclaw/workspace/schemy/ |
| Audrey | Quality Auditor — cross-validates Cody + Schemy findings | 60m | /root/.openclaw/workspace/audrey/ |

All three are Phase 1 (observe only). They write to Mem0 and their own workspace memory files.

**RULE:** Sub-agents must NOT write to MY files. My MEMORY.md, my daily logs, my workspace root. They have their own folders.

## My Setup

### Memory Stack (3 layers)
1. **LCM (Lossless Claw):** Full transcript history. Use `lcm_grep`/`lcm_expand` to search. 6.2MB SQLite DB. freshTailCount=64, leafChunkTokens=80000.
2. **MEMORY.md (this file):** Synthesized strategic facts. Flush here when context gets long.
3. **Mem0:** Shared vector memory. company='shared', agent='ceo'. Other agents can read my entries.

### Config (updated 2026-04-16)
- Primary model: openrouter/openai/gpt-4.1 (fast, 1M context, great at coding)
- Fallback model: openrouter/google/gemini-2.5-pro
- Compaction model: openrouter/anthropic/claude-sonnet-4-6
- Context: 128K tokens
- Compaction: memoryFlush enabled, softThresholdTokens=4000, reserveTokens=32000, keepRecentTokens=20000
- LCM: enabled, freshTailCount=64, leafChunkTokens=80000, contextThreshold=0.75, summaryModel=gpt-4.1-mini
- Voice TTS: ElevenLabs — George voice (JBFqnCBsd6RMkjVDRZzb), eleven_multilingual_v2, 1.25x speed
- Voice STT: Needs Groq API key (local Whisper too slow)
- Tools profile: coding

### Voice Preferences (Dean, permanent — see SOUL.md for full rules)
- MANDATORY: Voice note from Dean = reply with VOICE + TEXT. Never text-only. No exceptions.
- MANDATORY: Dean asks for voice = reply with VOICE + TEXT.
- Text from Dean = reply in text only.
- Long tasks: acknowledge start, acknowledge finish.
- Restarts: say restarting, say back online.
- ElevenLabs TTS (George voice) is configured and working. Always use it for voice replies.
- If unsure about voice rules, re-read SOUL.md — it is the source of truth, not this file.

### Key Config Decisions
- dmScope=main + mainKey=main → all channels unified
- SOUL.md uses [PINNED] tags to survive compaction
- echoTranscript set to false after testing
- 2026-04-15: Model changed from gemini-flash → gpt-4.1 (flash was too dumb for tool chains)
- 2026-04-15: Fallback changed from opus-4.6 → gemini-2.5-pro (cheaper, still capable)
- 2026-04-15: contextTokens reduced 200K → 128K for memory/token preservation
- 2026-04-15: Installed ClawHub skills: sql-toolkit, codebase-intelligence, api-tester, code-review-sr
- 2026-04-15: GitHub CLI (gh) installed on VPS

## Strategic Decisions

### Architecture (from CONTEXT_V3.md)
- Rule #1: Backtest-to-Live Validation — 99.9% accuracy target
- Rule #2: Execution Accuracy — nano-precision tracking (DECIMAL(20,8), DATETIME(3))
- Rule #3: Memory Efficiency — bounded caches, 48GB VPS, max 50 DB connections
- Shared DB for common data, per-company DBs for private data
- Model agnostic — swap LLMs without rebuilding
- Self-improving — autonomous strategy optimization

### Technology Choices
- Python for services/computation, TypeScript for Paperclip/orchestration
- CCXT/Pro for crypto exchanges, direct REST for Capital.com CFDs
- **Postgres 16** with declarative partitioning (candles by month) + pgvector for agent memory + JSONB for flexible blobs. BRIN index on candles.timestamp for time-ordered inserts.
- **ClickHouse 26.3** for raw backtest sweeps at scale (millions of rows). Postgres mirrors only promoted/approved strategies.
- **Redis** for task queues (backtest:queue), pub/sub coordination, and hot caches (candles 5 min TTL).
- **Mem0 + Qdrant** for per-agent private memory. 384-dim HF embeddings, no LLM API dependency for embed.
- **MemU** (Phase 7) for shared institutional memory across agents, backed by Postgres+pgvector.
- OpenRouter for LLM calls — model agnostic. Primary gpt-4.1, fallback gemini-2.5-pro.
- Legacy: MySQL kept running during migration grace period. Scheduled for decommission in Phase 9.

## Lessons Learned

- Always write to memory files during long sessions, not just at the end
- Config keys must match OpenClaw schema exactly
- Sub-agents will contaminate root workspace if not scoped — fixed 2026-04-15
- dmScope=main works but requires regular compaction
- Schema snapshot files go stale — always verify against live DB
- **Front-load infra migrations when data volume is low** (Phase 0, 2026-04-17). Easier to switch stacks when "the data is garbage anyway" than when it holds real money history.
- **Keep backups with `.mysql` suffix inline.** `db.py` + `db.mysql.py` side-by-side let me roll back in 30 seconds without touching git.
- **Reserved words in Postgres:** `timestamp`, `open`, `close`, `signal` must be double-quoted in DDL and queries. Caught `candles_2024_xx` table names would clash otherwise.
- **asyncpg `%s` → `$N` translator** saved hours of query rewriting. See `shared/utils/db.py::_translate_placeholders`.
- **Don't let ClickHouse `max_memory_usage_for_user` sit at top level** — it's a user-profile setting, goes inside `<profiles><default>`.

## Migration Artefacts (Phase 0, 2026-04-17)

All these exist and are permanent:
- `/opt/tickles/shared/migration/ROADMAP_V2.md` — the roadmap Dean reads first
- `/opt/tickles/shared/migration/tickles_shared_pg.sql` — Postgres shared DDL
- `/opt/tickles/shared/migration/tickles_company_pg.sql` — Postgres company template
- `/opt/tickles/shared/migration/clickhouse_schema.sql` — ClickHouse backtest schema
- `/opt/tickles/shared/migration/smoke_test_pg.py` — asyncpg round-trip smoke test
- `/opt/tickles/shared/utils/db.py` — asyncpg pool (backwards-compat singleton preserved, now also exposes `.acquire()`)
- `/opt/tickles/shared/utils/db.mysql.py` — aiomysql original (rollback safety)
- `/opt/tickles/shared/utils/config.py` — Postgres + CH + Redis + MemU env loader
- `/opt/tickles/shared/utils/config.mysql.py` — MySQL original (rollback safety)
- `/opt/tickles/.env.mysql.bak` — `.env` as it was before Phase 0

## Phase 1 Artefacts (backtest stack, 2026-04-17)

New code on the VPS (all under `/opt/tickles/shared/` unless noted):

- `backtest/engine.py` — deterministic single-run engine. `BacktestConfig.param_hash()` is the SHA256 we dedupe on.
- `backtest/candle_loader.py` — sync loader (psycopg2, worker-safe) + async loader (shared asyncpg pool, for catalog).
- `backtest/indicators/{core,smart_money,crash_protect,__init__}.py` — 23 indicators auto-registered at import time.
- `backtest/strategies/{single_indicator,__init__}.py` — 5 strategies; `STRATEGIES.get(name)` is the agent API.
- `backtest/queue.py` — Redis job queue. Keys: `bt:pending`, `bt:running`, `bt:done`, `bt:failed`, `bt:seen_hashes`, `bt:workers:<id>`.
- `backtest/worker.py` — one worker unit. `process(payload)` is importable so the local runner reuses it verbatim.
- `backtest/runner.py` — pool manager; `WORKERS` env caps concurrency.
- `backtest/ch_writer.py` — ClickHouse writer. Run/trade columns match `backtests.backtest_runs` / `backtest_trades` exactly; `batch_id` packed into `notes` JSON.
- `backtest/accessible.py` — rebuild / append / lookup / top against `backtests.txt`.
- `candles/daemon.py` — CCXT-based 1m collector with producer/consumer queues.
- `catalog/service.py` + `catalog/client.py` — aiohttp REST on `127.0.0.1:8765` + Python client.
- `memu/client.py` — pgvector insights + `pg_notify` pub-sub.
- `scripts/seed_reference_data.py` — idempotent seeder for `instruments` + `indicator_catalog`.
- `scripts/e2e_smoke.py` — end-to-end regression harness.
- `/opt/tickles/local_runner/{runner,tray,ssh_tunnel}.py` + `requirements.txt` + `README.md` — desktop runner.
- `/etc/systemd/system/tickles-{catalog,candle-daemon,bt-workers}.service` — the three new always-on services.
