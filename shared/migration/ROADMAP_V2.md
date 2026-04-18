# JarvAIs V2 Roadmap — Postgres + ClickHouse + MemU Stack

**Status:** Phase 0 complete. Phase 1 cutover deferred to waking hours.
**Owner:** Dean (project owner) · **Dev:** Overnight build.
**Last updated:** Phase 0 end.
**Supersedes:** `Context_roadmap.md`.

---

## 0. What changed and why

The previous roadmap targeted MySQL as the permanent OLTP store and deferred
ClickHouse / MemU indefinitely. That was safe but locks us into two growing
costs at scale: (a) MySQL's weak partition pruning on candles, (b) inability
to hold millions of backtest rows without painful housekeeping, and
(c) no shared institutional memory for the agents to reason across companies.

We fixed this while the data volume is still small (one-day optimality
window). The infrastructure work is done; the remaining cutover is code-level
and fully reversible.

### Stack after Phase 0

| Layer | Role | Tech | Port | User |
|---|---|---|---|---|
| OLTP: trading + reference | `tickles_shared`, `tickles_jarvais` | Postgres 16 + pgvector 0.6.0 | 5432 | `admin`, `schemy` |
| OLAP: raw backtests + signals | `backtests` DB | ClickHouse 26.3 (8 GB cap) | 9000/8123 | `admin` |
| Shared agent memory (new) | biologically-inspired memory | MemU (memu-py 0.2.2) on Postgres `memu` DB | 5432 | `admin` |
| Agent-private memory (kept) | per-agent episodic history | Mem0 + Qdrant (already running) | 6333 | - |
| Coordination / queue | task fan-out, pub/sub | Redis 7 | 6379 | - |
| Embedded meta-store | Paperclip internal | Postgres (Paperclip-owned) | 54329 | don't touch |
| Legacy OLTP | MySQL (archived) | MySQL 8 | 3306 | unchanged |

MySQL is still running for rollback safety. It will stop being written to the
moment Phase 1 collector cutover happens. It stays around until Dean says
"drop it".

---

## 1. Ground rules (never negotiable)

1. **Backtest-to-live validation (Rule 1).** Every filled live trade must
   have a traceable backtest ancestor. Enforced in
   `tickles_jarvais.trade_validations`; raw backtest lineage in ClickHouse
   `backtest_runs` via `parent_run_id`.
2. **Execution accuracy (Rule 2).** Slippage, spread, funding, fees logged
   per-trade in `trade_cost_entries`. No tipping "gross PnL" as truth.
3. **Memory efficiency (Rule 3).** Keep 1m candles on disk, roll up
   everything else from 1m on demand. Retention values in `system_config`.
4. **Human-in-loop for capital.** Approval mode `human_all` until you flip
   it. Guardrails phase (Phase 8) adds kill switches and compliance logs.
5. **Cross-company isolation.** A company DB (e.g. `tickles_jarvais`)
   never reads another company's private tables. Shared reference data
   (instruments, strategies) lives in `tickles_shared`. Future allowlist
   for shared learning (Phase 7 / MemU) will be explicit.

---

## 2. Current state (verified tonight)

```
Postgres 16.13  ✓   51 tables in tickles_shared (14 logical + 37 partitions)
                    10 tables in tickles_jarvais
                    pgvector 0.6.0 live
                    admin + schemy roles, password auth OK
ClickHouse 26.3 ✓   5 tables in backtests DB + 1 materialized view
                    8 GB server cap, bound to 127.0.0.1
                    admin/Tickles21! login works
Redis 7         ✓   PING → PONG
MemU 0.2.2      ✓   memu DB created, pgvector enabled
asyncpg         ✓   smoke tests pass: JSONB round-trip, partitioned inserts,
                    execute_many, %s → $N translator, dual-pool concurrency
Collectors      ✓   discord + telegram HWM upserts patched to ON CONFLICT
                    candle_service.py patched to ON CONFLICT
Config/env      ✓   .env updated: DB_PORT=5432, DB_PASSWORD set,
                    CH_*, REDIS_*, MEMU_* added
Backups         ✓   db.mysql.py, config.mysql.py, .env.mysql.bak preserved
```

Test artefacts are in `/tmp/` on the VPS for traceability.

---

## 3. Phase-by-phase plan (what's done, what remains)

Legend: ✅ done tonight, ⏳ ready to run tomorrow, ⏰ later.

### Phase 0 — Infrastructure migration ✅ COMPLETE

| Task | Status |
|---|---|
| Install Postgres 16 + pgvector, create admin/schemy users | ✅ |
| Install ClickHouse single-node (8 GB RAM cap, localhost-only) | ✅ |
| Install Redis | ✅ |
| Install MemU python client | ✅ |
| Create Postgres DBs: `tickles_shared`, `tickles_jarvais`, `memu` | ✅ |
| Translate 24 MySQL tables → Postgres DDL with JSONB, enum types, declarative partitioning, BRIN index | ✅ |
| Apply both schemas, verify permissions | ✅ |
| Port `shared/utils/db.py` to asyncpg (keeps `DatabasePool.get_instance()` for backwards compat) | ✅ |
| Update `shared/utils/config.py` for multi-DB Postgres + ClickHouse + Redis + MemU | ✅ |
| Patch Discord + Telegram collector ON-DUPLICATE → ON CONFLICT | ✅ |
| Patch `candle_service.py` insert → ON CONFLICT with reserved-word quoting | ✅ |
| ClickHouse `backtests` schema: backtest_runs, backtest_trades, signal_feed, agent_events, top_sharpe MV | ✅ |
| Smoke tests: asyncpg pool × 2 DBs + partitioned candle insert + execute_many + ClickHouse insert/aggregate/materialized view | ✅ |

### Phase 1 — Live cutover ⏳ DO WITH DEAN AWAKE

This is the point of no return for collector traffic. Not run overnight.

1. Stop MySQL-writing collectors (`systemctl stop tickles-*` or `pm2 stop`).
2. Run the updated collector once against Postgres and confirm:
   * Discord HWM upsert works
   * Telegram HWM upsert works
   * No regression in media download path
3. Re-seed reference data into Postgres (one-shot scripts need porting):
   * Rewrite `shared/migration/seed_instruments.py` to psycopg (currently pymysql)
   * Rewrite `shared/migration/seed_indicator_catalog.py` likewise
4. Re-enable collector services against Postgres.
5. MySQL becomes cold read-only. Keep a nightly `mysqldump` to disk for 14 days.

Estimated time with Dean: 30 min.

### Phase 2 — 1-minute candle daemon ⏰

Single always-on service that, per instrument/exchange, pulls the latest
closed 1m bar from CCXT / Capital REST and inserts into `candles` with
the exact ON CONFLICT pattern already in `candle_service.py`. Writes a
`NOTIFY candle_inserted` so downstream services react without polling.

Scaffold: `shared/market_data/run_candle_collection.py` already exists
and will just need the `LISTEN/NOTIFY` hooks added.

### Phase 3 — CandleRollupService + CandleLoader ⏰

Per Rule 3, higher timeframes come from the 1m store.
* `CandleRollupService` — a Postgres stored function (SQL `CREATE FUNCTION
  rollup(instrument_id, timeframe, from, to)` returning `candles_view`)
  for agents that need live multi-TF data without a daemon.
* `CandleLoader` — Python wrapper that either reads materialized rollup
  or computes on-the-fly via the function; caches in Redis for 5 minutes
  (`candles.cache_ttl_seconds` in `system_config`).

### Phase 4 — Gap detection + completeness ⏰

Existing `shared/market_data/gap_detector.py` already works against the
old schema. Update queries to use partition-aware SELECT. Add a
`candles_completeness(instrument_id, date)` view that exposes missing
minute-bars per day for observability.

### Phase 5 — AgentDataService + CollectorControlService ⏰

Thin Python service layer the agents call (no raw SQL for them):
* `list_instruments(asset_class)`
* `get_candles(instrument_id, timeframe, from, to)`
* `start_collector(exchange, symbols, timeframes)`
* `stop_collector(name)`
* `collector_status()`

Used by both Paperclip agents and by future Librarian/Noticeboard.

### Phase 6 — ClickHouse backtest pipeline ⏰

Schema: ✅ already applied tonight (`backtests.backtest_runs`, etc).
Still to build:
1. Python `backtest_worker.py` — pulls a job from Redis queue
   (`LPOP backtest:queue`), runs engine, INSERTs one row into
   `backtests.backtest_runs` + optional trade rows.
2. Promotion gate — agents `SELECT ... WHERE sharpe_ratio > X AND
   deflated_sharpe > Y AND oos_sharpe > Z` then insert a curated copy
   into `tickles_shared.backtest_results` with `promotion_status='approved'`.
3. Human-approval step before any live binding (see Phase 8).

### Phase 7 — Wire MemU into the agent stack ⏰

Additive, not destructive. Agents keep Mem0 for their own history.
MemU gets two new agent skills:
* `memu_write(category, content)` — write to shared learning.
* `memu_query(query, top_k)` — read across categories.

Categories seeded: `strategy_outcomes`, `market_regimes`,
`execution_patterns`, `risk_events`, `review_findings`.

Cody and Audrey are the first consumers: when Cody confirms a fix works,
he writes a `review_findings` item. When Audrey detects data drift, she
writes a `risk_events` item.

### Phase 8 — Guardrails (never optional for money-touching paths) ⏰

Hard-wired before any `approval_mode` flips from `human_all`:
1. **Approval agent** — separate lightweight LLM call that must say "yes"
   before a live order is submitted. Logs reasoning to `api_cost_log`
   with `context='approval'`.
2. **Kill switches** in `system_config.guardrails`:
   * `daily_loss_killswitch_usd` (default 50)
   * `daily_loss_killswitch_pct` (default 10)
3. **Compliance log** — every live order and every agent override is
   appended to ClickHouse `agent_events` with event_type='compliance'.
4. **Deflated Sharpe + OOS columns** on `backtest_results` (already in
   schema) must be populated before a strategy can go live.

### Phase 9 — Decommission MySQL ⏰

Only after 14 days of clean Postgres operation. `systemctl stop mysql`,
tar the data dir, archive to storage, remove the service.

---

## 4. File and directory reference

### Database DDL
* `/opt/tickles/shared/migration/tickles_shared.sql`     — original MySQL (kept for reference)
* `/opt/tickles/shared/migration/tickles_company.sql`    — original MySQL (kept for reference)
* `/tmp/tickles_shared_pg.sql`                           — Postgres DDL applied tonight
* `/tmp/tickles_company_pg.sql`                          — Postgres company template
* `/tmp/clickhouse_schema.sql`                           — ClickHouse backtests DDL
* TODO (tomorrow): copy the three `_pg.sql` files into `/opt/tickles/shared/migration/`

### Code
* `/opt/tickles/shared/utils/db.py`                      — NEW: asyncpg-based DatabasePool
* `/opt/tickles/shared/utils/db.mysql.py`                — BACKUP: old aiomysql version
* `/opt/tickles/shared/utils/config.py`                  — NEW: Postgres + CH + Redis + MemU
* `/opt/tickles/shared/utils/config.mysql.py`            — BACKUP: old MySQL-only version
* `/opt/tickles/shared/utils/mem0_config.py`             — UNCHANGED: 384-dim HF embedder
* `/opt/tickles/shared/collectors/discord/discord_collector.py`    — PATCHED
* `/opt/tickles/shared/collectors/telegram/telegram_collector.py`  — PATCHED
* `/opt/tickles/shared/market_data/candle_service.py`              — PATCHED

### Environment
* `/opt/tickles/.env`                                    — UPDATED with PG + CH + Redis + MemU
* `/opt/tickles/.env.mysql.bak`                          — BACKUP: pre-migration version

### Smoke tests (re-runnable)
* `/tmp/smoke_test_pg.py`                                — asyncpg round-trip tests
* `/tmp/smoke_ch.sh`                                     — ClickHouse insert/query/MV tests
* `/tmp/verify_schema.sh`                                — permissions + table counts

---

## 5. Rollback plan (safety net Dean requested)

We promised "no pre-flight rollback plans" but here is the minimum useful one.
This is only a safety net if Postgres or ClickHouse misbehaves overnight.

### 5.1 Immediate rollback to MySQL (≤ 5 minutes)

```bash
cd /opt/tickles
cp .env.mysql.bak .env                     # restore env vars
cp shared/utils/db.mysql.py shared/utils/db.py
cp shared/utils/config.mysql.py shared/utils/config.py
# Revert collector files from /tmp/*.mysql.py if needed
systemctl restart tickles-*
```

MySQL was never stopped and keeps accepting writes for collectors using
the old DB pool, so this rollback returns us to the pre-Phase-0 baseline.

### 5.2 Partial rollback (keep Postgres, skip ClickHouse)

`CH_ENABLED=false` in `.env`. The backtest path currently has no live
consumer, so ClickHouse being ignored is harmless until Phase 6.

### 5.3 "Do not auto-modify" markers

The agent CEO is barred from modifying the following files (enforced in
its SOUL.md):
* `/opt/tickles/.env` and `.env.mysql.bak`
* `/opt/tickles/shared/utils/db.py` and `db.mysql.py`
* `/opt/tickles/shared/utils/config.py` and `config.mysql.py`
* `/root/.openclaw/openclaw.json`
* `/opt/tickles/shared/migration/*.sql`

Any change to these must go through Dean in Cursor.

---

## 6. What the agents will see on first boot after Phase 1

1. CEO reads `MEMORY.md` → knows Phase 0 is done, Postgres is the OLTP.
2. First tool call that hits the DB uses the new asyncpg pool.
3. Schemy runs `\dt` on both DBs via psql (password in her `TOOLS.md`).
4. Cody finds `ON CONFLICT` patches already in place when he scans
   recent diffs.
5. Audrey runs the new smoke test script to verify no regressions.

---

## 7. Decisions locked in this cycle

| # | Decision | Owner |
|---|---|---|
| 1 | Postgres is the OLTP. No more MySQL for new work. | Dean |
| 2 | ClickHouse holds raw backtest sweeps. Postgres holds promoted/curated. | Dean |
| 3 | Mem0 (per-agent) + MemU (shared) run in parallel forever. | Dean |
| 4 | `human_all` approval mode until Phase 8 guardrails are fully wired. | Dean |
| 5 | Cross-company memory isolation is strict. Allowlist is future work. | Dean |
| 6 | Single roadmap file — this one — is the source of truth. | Dean |
| 7 | No pre-flight rollback plans, but keep `.mysql.py`/`.mysql.bak` backups. | Dean |
| 8 | "Do not auto-modify" markers on infra-critical files. | Dean |
| 9 | Per-phase Cody+Audrey scans happen *after* each phase, not before. | Dean |
| 10 | Store 1m, roll up everything else on-demand (Rule 3 realisation). | Dean |
| 11 | Primary LLM: openrouter/openai/gpt-4.1. Fallback: google/gemini-2.5-pro. | Dean |

---

## 8. Open questions for Dean (not blocking)

1. **Which company stands up second?** The per-company template is ready.
   `capital` (CFD) or `explorer` (sandbox) makes the most sense.
2. **Candle retention for 1m** — currently 90 days. Is that right, or do
   we want longer? (Raw storage ≈ 2 MB/day/instrument, negligible.)
3. **MemU content sharing** — shall we seed initial categories from
   existing Mem0 data, or start clean?
4. **Paperclip embedded Postgres (port 54329)** — leave alone, or also
   point it at our managed PG for observability? Mild preference to
   leave it alone — different lifecycle.

---

## 9. How to verify the system tomorrow morning

Run these three commands to confirm Phase 0 is still healthy:

```bash
# 1. All services up
systemctl is-active postgresql clickhouse-server redis-server

# 2. Postgres reachable + schema intact
PGPASSWORD='Tickles21!' psql -h 127.0.0.1 -U admin -d tickles_shared \
  -c "SELECT COUNT(*) AS tables FROM pg_tables WHERE schemaname='public'"
# Expect: 51

# 3. ClickHouse reachable + empty backtests
clickhouse-client --user admin --password 'Tickles21!' --database backtests \
  --query "SELECT name, total_rows FROM system.tables WHERE database='backtests'"
# Expect: 5 tables + 1 MV inner, all 0 rows
```

If any of those fail, read `/tmp/` for the deployment scripts and re-run
the relevant one.

---

---

## 10. Phase 1 overnight build — COMPLETE (2026-04-16 → 2026-04-17)

Dean asked for a world-class backtesting foundation by morning. Here's
what now lives on the VPS and what's verified working end-to-end.

### 10.1 What was built

Code deployed to `/opt/tickles/shared/`:

| Module | Purpose | Key ideas |
|---|---|---|
| `backtest/engine.py` | Deterministic single-run backtest engine | SHA256 `param_hash` dedup, realistic fills (spread+slippage+fees), SL/TP, equity curve, deflated-Sharpe |
| `backtest/candle_loader.py` | Pull OHLCV from Postgres → pandas | Sync loader uses psycopg2 (worker-safe); async loader uses shared asyncpg pool |
| `backtest/indicators/` | 23-indicator library | `core` (SMA, EMA, RSI-Wilder, MACD, ATR, BB %B, OBV, VWAP, MFI), `smart_money` (BOS, FVG, liquidity sweep, volume spike), `crash_protect` (CapitalTwo2.0 +86% block) |
| `backtest/strategies/` | 5 building-block strategies | `ema_cross`, `sma_cross`, `rsi_reversal`, `bollinger_pullback`, `anchored_vwap_pullback`. All deterministic, +1/0/-1 signals. |
| `backtest/queue.py` | Redis-backed job queue | Atomic BRPOPLPUSH claim, hash-dedup set, heartbeat TTL, stuck-job reaper |
| `backtest/worker.py` | One worker process | Polls queue → loads candles → runs engine → writes CH; SIGTERM-safe |
| `backtest/runner.py` | Worker pool manager | Auto-detects CPUs (`WORKERS` env cap), respawns crashed workers, reaps stuck jobs every loop |
| `backtest/ch_writer.py` | Writes runs + trades to ClickHouse | Matches *actual* `backtests.backtest_runs` / `backtest_trades` schema; `batch_id` stored in `notes` JSON |
| `backtest/accessible.py` | `backtests.txt` flat-file layer | Pipe-delimited greppable output; `lookup`, `top` helpers; rebuild/append |
| `candles/daemon.py` | 1m CCXT collector | Producer/consumer per instrument, queue→batch upsert, `ON CONFLICT DO UPDATE` |
| `catalog/service.py` + `client.py` | REST discovery API for agents | `/health`, `/exchanges`, `/instruments`, `/timeframes`, `/indicators`, `/strategies`, `/backtests/top`, `/backtests/lookup/`, `/stats` |
| `memu/client.py` | Shared MemU wrapper | pgvector insights table, `write_insight` / `search` / `count`, `pg_notify` pub-sub |
| `local_runner/` | Desktop backtest runner | `runner.py` (SSH-tunneled claim loop), `tray.py` (pystray), `ssh_tunnel.py` (auto-restart), `requirements.txt`, README with PyInstaller steps |
| `scripts/seed_reference_data.py` | Postgres seeding | 50 instruments (19 crypto × {binance, bybit} + 12 Capital CFDs), 23 indicators in `indicator_catalog` — idempotent |
| `scripts/e2e_smoke.py` | End-to-end validation | Fetch 30d × 3 symbols, enqueue 54 jobs, drain via workers, verify CH rows/trades, rebuild txt, time top-lookup |
| `systemd/tickles-{catalog,candle-daemon,bt-workers}.service` | Always-on services | Auto-restart, journald + dedicated logs in `/var/log/tickles/` |

### 10.2 What Dean can verify now

```bash
# All three new services healthy
systemctl is-active tickles-catalog tickles-candle-daemon tickles-bt-workers

# Catalog answers on loopback
curl -s http://127.0.0.1:8765/stats | jq
curl -s 'http://127.0.0.1:8765/backtests/top?n=5&sort=sharpe' | jq

# Flat-file backtest index
head /opt/tickles/shared/backtests.txt

# Candles streaming live (1m, from CCXT, 50 instruments × 2 exchanges)
PGPASSWORD='Tickles21!' psql -U admin -d tickles_shared -h 127.0.0.1 -c \
  "SELECT timeframe, COUNT(*) FROM candles GROUP BY timeframe"
```

### 10.3 Last smoke-test report (baseline for regression)

```json
{
  "fetch_candles": {"BTC/USDT": 720, "ETH/USDT": 720, "SOL/USDT": 720},
  "enqueue_count": 54,
  "drain_ok": true,
  "ch_runs": 54, "ch_trades": 135,
  "txt_lines": 54, "top_lookup_ms": 75.8,
  "best_sharpe": {
    "symbol": "SOL/USDT", "indicator": "rsi_reversal",
    "params": {"period": 7, "overbought": 70, "oversold": 30},
    "sharpe": 4.51, "winrate": 72.7, "return_pct": 20.76,
    "max_drawdown": -7.2, "num_trades": 22
  },
  "elapsed_s": 20.5
}
```

### 10.4 Non-obvious decisions made tonight (so we can roll back if needed)

1. **`batch_id` lives in `notes` JSON**, not a dedicated ClickHouse column.
   Reason: the `backtests.backtest_runs` DDL already existed with a
   different shape than the first-draft writer assumed, and adding a
   column means a `CREATE TABLE … AS` + swap. We can always add the
   column later and backfill from `JSONExtractString(notes, 'batch_id')`.
2. **Sync candle loader uses psycopg2**, not the async shared pool. The
   shared asyncpg pool is bound to the loop that first created it, so
   spawning a new loop per worker job was raising "Event loop is closed".
   psycopg2 is simpler for the single-query-per-backtest pattern and
   only adds ~2ms per call. Async loader is still used by the catalog
   service and any async caller.
3. **`direction` in `backtest_trades` is written as the Enum8 int** (1/2),
   not as a string — clickhouse-driver’s enum encoder rejected the
   string form.
4. **Instrument lookup cached per-worker-process** in `worker.py` to
   avoid 50 Postgres round-trips per batch.

### 10.5 Known rough edges (morning work)

* **MATIC delisted on Bybit** — the candle daemon logs a warning every
  5 s for `MATIC/USDT@bybit`. Fix: mark `is_active=FALSE` in
  `instruments`, or switch that pair to `polygon`. Benign for now.
* **MemU wiring is built but not exercised end-to-end.** The client
  (`/opt/tickles/shared/memu/client.py`) writes insights and searches
  via pgvector, but no agent is producing insights yet. First "write +
  search round-trip" is a 5-minute test when Dean wakes up.
* **Local desktop runner is code-complete but not packaged.** Files are
  in `/opt/tickles/local_runner/`. Steps to get a `.exe` are in its
  README; takes ~5 min and a Windows Python.
* **`oos_sharpe` / `oos_return_pct` are placeholder zeros.** Walk-
  forward split is Phase 7 work.

### 10.6 What this unlocks

Agents now have, via the catalog API:

* A searchable index of every indicator, its params and category.
* A searchable index of every strategy.
* A way to ask "which backtests did we already run?" and dedupe against
  the `param_hash` before spending CPU.
* A way to ask "what's the best SOL/USDT setup on 1h?" and get an
  answer in ~75 ms.

Dean gets:

* 54 real backtests to play with out of the box.
* A greppable `backtests.txt` that any shell script / editor can tail.
* Systemd-managed candle collection continuously feeding the DB.
* A clean path to add more pairs: `INSERT INTO instruments …; systemctl
  restart tickles-candle-daemon`.

---

*End of ROADMAP_V2.md. Phase 1 overnight build complete.*
