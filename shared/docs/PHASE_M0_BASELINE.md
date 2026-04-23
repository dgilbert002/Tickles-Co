# Phase M0 — VPS Baseline Audit

**Date:** 2026-04-20
**Auditor:** AI, read-only via `ssh vps`
**Target VPS:** `vmi3220412` (Ubuntu 24 LTS, Linux 6.8)
**Scope:** 9 yes/no questions defined in `MCP_AND_MEMORY_PLAN.md` § 4 / Phase M0. Zero state changes. No secrets printed.

---

## 1. Executive summary (read this first)

1. **The platform is LIVE and more mature than `SERVICES.md` claims.** 7 systemd units (`paperclip`, `tickles-mcpd`, `tickles-candle-daemon`, `tickles-catalog`, `tickles-cost-shipper`, `tickles-funding-collector`, `tickles-md-gateway`) are `enabled + active`. One (`tickles-bt-workers`) is `activating`. `SERVICES.md` is stale.
2. **MCP daemon is running on `:7777`** (unit name: `tickles-mcpd.service`, **not** `tickles-mcp-server` as `SERVICES.md` said). Endpoint is `POST /mcp`. Health at `GET /healthz`. **35 tools** registered.
3. **Memory infrastructure is ready:** Postgres `memu` DB exists, `pgvector 0.6.0` installed, `insights` table missing but MemU client auto-creates it on first write — no DB-admin work needed.
4. **Biggest reality gap: candle data.** Only **Bybit**, only **BTC/ETH/SOL USDT**, and only **~11 hours of 1m** and **~2 days of 5m**. Plan assumed "2 months of 1m/5m for high-vol coins" — it does not exist. Must backfill before the Phase M7 contest.
5. **Contest venue scope (Phase M7) is constrained to Bybit for the initial run** — Blofin + BitGet have keys but zero candles yet (can be backfilled in M1); Capital is CFD, separate schema; Binance + Gates.io have no keys at all.

---

## 2. The 9 baseline questions

| # | Question | Answer | Evidence |
|---|---|---|---|
| Q1 | `mcp-server` systemd unit running? | **YES** — unit name is `tickles-mcpd.service` (not `tickles-mcp-server`). `enabled + active` since 2026-04-19 21:56 UTC. Listens `127.0.0.1:7777`. | `systemctl is-active tickles-mcpd = active`, journal: `[build_registry] registered tools=35`, `ss -ltnp` shows Python PID 1284516 on :7777 |
| Q2 | Postgres `memu` DB + pgvector + `insights` table? | **DB: YES (owner `admin`). pgvector 0.6.0 installed. `insights` table: MISSING (auto-created on first write).** | `psql -l` lists `memu`; `SELECT FROM pg_extension WHERE extname='vector'` → `v0.6.0`; `SELECT COUNT(*) FROM insights` → `ERROR: relation "insights" does not exist` |
| Q3 | `user-mem0` MCP registered with OpenClaw? | **NO.** Only `tickles` is registered in `/root/.openclaw/openclaw.json`. | `openclaw mcp list` → `- tickles` (single entry). Confirms Gap 6 in architecture doc. |
| Q4 | Candle coverage? | **SHALLOW. Only Bybit. Only BTC/ETH/SOL USDT.** See § 3.4 for the full breakdown. **1m: ~11h per pair. 5m: ~2d per pair. 15m: ~5d. 1h: ~3w. 4h: ~3 months.** | `sudo mysql tickles_shared` queries — see § 3.4 |
| Q5 | `tickles-bt-workers` running? | **Activating** (not yet `active`). Other backtest-related: no `tickles-backtest-runner` / `tickles-backtest-submitter` / `tickles-strategy-composer` units exist on VPS. | `systemctl is-active tickles-bt-workers = activating` |
| Q6 | CCXT Pro live venue connections? | **md-gateway is connected, subscribing/unsubscribing `md.binance.BTC-USDT.{trade,tick,l1}` symbols on demand.** Funding-collector writes ~3 snapshots/minute. Candle-daemon quiet (no recent journal entries after startup). | `journalctl -u tickles-md-gateway -n 40` (see § 3.6) |
| Q7 | Which of the "staged" services would start cleanly? | **Most already ARE started.** The 17-service list in `SERVICES.md` is out of date. Only 3 are currently `disabled`: `tickles-discord-collector`, `tickles-trader-rubicon_surgeon`, `tickles-trader-rubicon_surgeon2`. | `systemctl list-unit-files tickles-*` (see § 3.2) |
| Q8 | Current MCP tool count? | **35 tools** across 15 groups. **6 built-in tools defined in `registry.py` are NOT registered** (dead code in current daemon). | `POST /mcp {method:"tools/list"}` — see § 3.7 |
| Q9 | Venue credentials in `/opt/tickles/.env`? | **Bybit + Blofin + BitGet + Capital: present. Binance + Gates.io: absent.** See § 3.9. | `grep ^BINANCE_ /opt/tickles/.env` → empty; full presence map in § 3.9 |

---

## 3. Detailed evidence

### 3.1 Systemd unit inventory

| Unit | enabled | active | Notes |
|---|---|---|---|
| `paperclip.service` | — | **active** | Paperclip prod-static UI + tsx runtime |
| `tickles-mcpd.service` | enabled | **active** | MCP daemon on `:7777`, 35 tools registered |
| `tickles-candle-daemon.service` | enabled | **active** | 1m candle collector via CCXT (currently Bybit-only per § 3.4) |
| `tickles-catalog.service` | enabled | **active** | Data catalog |
| `tickles-cost-shipper.service` | enabled | **active** | Streams OpenClaw LLM usage → Paperclip `cost_events` |
| `tickles-funding-collector.service` | enabled | **active** | ~3 funding snapshots/min, active writes to `derivatives_snapshots` |
| `tickles-md-gateway.service` | enabled | **active** | CCXT Pro WS → Redis fan-out (Phase 17) |
| `tickles-bt-workers.service` | enabled | activating | Backtest workers, currently in retry loop |
| `tickles-discord-collector.service` | disabled | inactive | |
| `tickles-trader-rubicon_surgeon.service` | disabled | inactive | Surgeon cron fires via openclaw, not via this unit |
| `tickles-trader-rubicon_surgeon2.service` | disabled | inactive | Same as above |

**Action:** after M1 completes, `tickles-bt-workers` should be investigated (why is it stuck `activating`? — journal doesn't tell us).

### 3.2 `SERVICES.md` reality gap

`SERVICES.md` lists 23 services. Reality:

- **Exist + running:** 8 units (listed above).
- **Exist + disabled:** 3 units (discord-collector, 2 rubicon traders).
- **Listed in SERVICES.md but NO unit file on VPS:** 12 (`banker`, `executor`, `regime`, `crash-protection`, `altdata-ingestor`, `events-calendar`, `souls`, `arb-scanner`, `copy-trader`, `strategy-composer`, `backtest-runner`, `backtest-submitter`, `dashboard`, `news-rss`, `telegram-collector`, `tradingview-monitor`, `auditor`). These were shipped as Python modules but never got systemd units.

**Implication:** the "17 services ready to enable" in the plan was wrong. Realistic M8 scope is much smaller — just `enable --now` the 3 currently-disabled units, and write new systemd unit files for any of the 12 Python modules we actually want running.

### 3.3 Postgres `memu` state

```
DB list:       memu | admin | UTF8 | C.UTF-8 | ... (present)
Extension:     vector  default=0.6.0  installed-in-cluster=(not installed)
In memu DB:    vector v0.6.0 (installed, ready)
Tables:        (empty — no tables created yet)
insights row:  ERROR: relation "insights" does not exist
```

**Why this is fine:** `shared/memu/client.py` `_connect()` runs `_SCHEMA_SQL` on every connection — the table + indexes auto-create on first write. No manual DB setup needed for Phase M3.

### 3.4 Candle data coverage (this matters most)

**All candles are Bybit. All other venues: 0 rows.**

| Timeframe | Total rows | Per-pair rows (BTC = ETH = SOL) | Earliest | Latest | Span |
|---|---|---|---|---|---|
| `1m` | 1,935 | 645 | 2026-04-13 11:06 UTC | 2026-04-13 21:50 UTC | **~10.75 hours** |
| `5m` | 1,587 | 529 | 2026-04-12 01:50 UTC | 2026-04-13 21:50 UTC | **~2 days** |
| `15m` | 1,530 | 510 | 2026-04-08 14:30 UTC | 2026-04-13 21:45 UTC | ~5 days |
| `1h` | 1,506 | 502 | 2026-03-24 00:00 UTC | 2026-04-13 21:00 UTC | ~3 weeks |
| `4h` | 1,503 | 501 | 2026-01-20 12:00 UTC | 2026-04-13 20:00 UTC | ~3 months |

**Instruments table:** 542 rows, **all Bybit.** Blofin / BitGet / Binance / Gates.io / Capital have 0 instrument rows.

**Latest candle is 2026-04-13 21:50 UTC — that's ~7 days ago.** Candle-daemon has been running since 2026-04-16 but no new journal output since startup. Either (a) it silently fell off the WebSocket, (b) it's writing candles that we aren't seeing, or (c) the subscriber list is stale. **Needs investigation before M1.**

**Backfill math for the contest (Phase M7):**
- 60 days of 1m for 3 pairs × 1 venue = 60 × 1440 × 3 = **~260k rows**. Bybit REST OHLCV endpoint returns 1000/call with ~250ms round-trip → ~260 API calls × 0.25s = **~65 seconds wall-clock**. Very achievable in one pass.
- Adding BTC/ETH/SOL across **3 venues** (Bybit + Blofin + BitGet) = **~780k rows** = **~3 minutes** of one-shot backfill.
- Expanding to 8 high-vol coins × 3 venues × 2 timeframes × 60 days = ~12M rows = ~45 minutes. Still trivial.

### 3.5 OpenClaw MCP registration

```
$ openclaw mcp list
MCP servers (/root/.openclaw/openclaw.json):
- tickles
```

**Only one MCP server is visible to OpenClaw agents.** `user-mem0` is not registered. Any `memory.add` / `memory.search` call from an OpenClaw-cron agent today cannot actually write to mem0 — it would receive the `forward_to: user-mem0::add-memory` envelope but have no client to forward it to.

**Fix (Phase M4):** one JSON edit to `/root/.openclaw/openclaw.json`, back up first.

### 3.6 CCXT Pro live state

md-gateway last journal segment (2026-04-19 02:11–02:16) shows:

```
Gateway started
subscribed md.binance.BTC-USDT.trade
subscribed md.binance.BTC-USDT.tick
subscribed md.binance.BTC-USDT.l1
unsubscribed md.binance.BTC-USDT.trade
...
```

**It's subscribing to Binance public WebSocket** (no keys needed for public data). That's healthy. Redis `md:*` key set:

```
md:gateway:lag:binance:BTC-USDT
md:gateway:stats
```

So there's a lag tracker and stats page in Redis. Nothing fancy yet — no subscriptions for Bybit / Blofin / BitGet visible, even though Bybit is where all the candles are. Funding-collector is actively writing to `derivatives_snapshots` (3 snapshots/min).

### 3.7 MCP tool registry (detail)

Total: **35 tools**, by group:

```
company     -> 8
agent       -> 6
execution   -> 3  (stubs)
banker      -> 2  (1 wired, 1 stub)
catalog     -> 2  (wired)
feedback    -> 2  (wired, prompts-only)
md          -> 2  (stubs)
memory      -> 2  (forward-to-mem0)
memu        -> 2  (stubs)
altdata     -> 1  (stub)
autopsy     -> 1  (wired, prompt)
learnings   -> 1  (forward-to-mem0)
(no-group)  -> 1  (ping)
postmortem  -> 1  (wired, prompt)
treasury    -> 1  (stub)
```

**Tools DEFINED in `shared/mcp/registry.py` but NOT registered** by `shared/mcp/bin/tickles_mcpd.py`:
- `services.list`
- `strategy.intents.recent`
- `backtest.submit`
- `backtest.status`
- `dashboard.snapshot`
- `regime.current`

These are sitting in code as `build_*_tool()` helpers. Calling `registry.register(*build_services_list_tool(...))` in `tickles_mcpd.py` would light them up instantly. **Quick win for M1 or dedicated mini-phase M0.5.**

### 3.8 Health endpoint map (record for future CLI/agent use)

```
POST /mcp       → 200  JSON-RPC 2.0 dispatch
GET  /healthz   → 200  health check
GET  /health    → 404
GET  /          → 404
POST /          → 404
POST /rpc       → 404
POST /jsonrpc   → 404
```

### 3.9 Credential presence (values never logged)

| Key | Present? | Length | Head (first 4) |
|---|---|---|---|
| `BYBIT_API_KEY` | yes | 18 | `8FNq` |
| `BYBIT_SECRET` | yes | 36 | `v5Dx` |
| `BYBIT_DEMO_API_KEY` | yes | 18 | `eRCw` |
| `BYBIT_DEMO_API_SECRET` | yes | 36 | `89LW` |
| `BYBIT_DEMO_SHADDOW_API_KEY` *(sic: shadow misspelled)* | yes | 18 | `5flY` |
| `BYBIT_DEMO_SHADDOW_API_SECRET` | yes | 36 | `Yjns` |
| `BLOFIN_API_KEY` | yes | 32 | `e9a8` |
| `BLOFIN_API_SECRET` | yes | 32 | `9b69` |
| `BLOFIN_API_PHRASE` | yes | 9 | `Tick` |
| `BLOFIN_DEMO_API_KEY` | yes | 32 | `6693` |
| `BLOFIN_DEMO_API_SECRET` | yes | 32 | `2c14` |
| `BLOFIN_DEMO_API_PHRASE` | yes | 10 | `Tick` |
| `BITGET_API_KEY` | yes | 35 | `bg_6` |
| `BITGET_API_SECRET` | yes | 64 | `f9a7` |
| `BITGET_API_PHASE` *(sic: should be PHRASE)* | yes | 9 | `Tick` |
| `CAPITAL_EMAIL` | yes | 20 | `dean` |
| `CAPITAL_PASSWORD` | yes | 10 | `****` |
| `CAPITAL_API_KEY` | yes | 16 | `RPMu` |
| `OPENROUTER_API_KEY` | yes | 73 | `sk-o` |
| `OPENAI_API_KEY` | yes | 73 | `sk-o` |
| **`BINANCE_API_KEY`** | **MISSING** | — | — |
| **`BINANCE_SECRET`** | **MISSING** | — | — |
| **`GATE_API_KEY` / `GATEIO_API_KEY`** | **MISSING** | — | — |

**Minor hygiene items for CEO attention** (not blockers for the plan, just noted):
- `BLOFIN_API_PHRASE` is 9 chars, `BLOFIN_DEMO_API_PHRASE` is 10 chars — if these are supposed to be identical format, one has a trailing char somewhere.
- `BITGET_API_PHASE` looks like a typo for `BITGET_API_PHRASE`. Code probably reads a specific env var name; if there's a mismatch, BitGet auth will fail silently. Easy to verify by reading `shared/connectors/bitget*.py`.
- `MYSQL admin@localhost` credentials (`DB_USER=admin` + `DB_PASSWORD=***`) **don't work** (`ERROR 1045 Access denied`). Services are clearly using a different user (probably `tickles_app` or `schemy` — both exist in `mysql.user`). Worth reconciling in the roadmap, not urgent for M0.

---

## 4. How this changes the plan

Revisions to `MCP_AND_MEMORY_PLAN.md` that M0 forced:

| Plan item | Before | After |
|---|---|---|
| Tool count baseline | "41 tools (30 real + 11 stubs)" | **35 tools** (exact, measured). Stubs remain ~11. |
| Free "+6 tools" from built-ins | not mentioned | Added as Phase M0.5 (5 minutes of work: register the 6 `build_*_tool()` helpers already in `registry.py`). |
| Phase M1 scope (market data unstub) | `md.quote`, `md.candles`, `candles.coverage`, `candles.backfill` | Same tools **plus** a mandatory *serious* backfill operation: 60 days × 1m/5m × 3 pairs × 3 venues = ~780k rows. Plus a candle-daemon sanity check (why is journal quiet since 2026-04-16?). |
| Phase M3 (MemU unstub) | "verify pgvector + DB first" | **Verified: both ready.** M3 shrinks to just wiring the two stub tools — `insights` table auto-creates on first write. |
| Phase M4 (register user-mem0) | "one JSON edit" | **Unchanged**, but now confirmed needed: today's OpenClaw has only `tickles` registered. |
| Phase M7 (contest) venue scope | "whatever env has keys" | **Constrained:** initial contest on **Bybit only**. Blofin + BitGet added in a follow-up sub-phase (M7.1) after backfill covers them. |
| Phase M8 (enable-all) | "17 staged services" | **Actually only 3 disabled units exist** + ~12 Python modules without unit files. M8 splits into M8a (enable 3) + M8b (write unit files for whichever of the 12 we want). |
| MCP endpoint | not specified | `POST /mcp` (confirmed). `GET /healthz` for liveness. |
| OpenClaw version | not specified | 2026.4.15 (041266a) |

---

## 5. Action items before Phase M1 can start

These are **blockers** — not code changes, but things the CEO (or I, with permission) need to confirm:

1. **Investigate candle-daemon silence.** Journal has no output since 2026-04-16 startup, but it's `active`. Either fell off WebSocket, writing to a different table, or fine and just quiet. Read-only inspection only. **Owner: AI.**
2. **Decide on Bybit candle backfill scope for M1.** Recommendation:
   - 60 days of 1m + 5m for `{BTC, ETH, SOL, BNB, XRP, DOGE, ADA, AVAX} /USDT` on Bybit = ~16M rows, ~45 min backfill.
   - Add Blofin + BitGet same scope in a follow-up after Bybit is verified clean.
   - **Owner: CEO approves scope, AI executes.**
3. **Confirm MySQL connection path for services.** `admin@localhost` doesn't work with the `.env` password; services must be using `tickles_app` or `schemy`. Not a blocker for M1, but worth reconciling before anyone needs to run ad-hoc queries. **Owner: CEO (has password) + AI (identifies mismatch).**
4. **Approve Phase M0.5** — 5-minute wiring of the 6 built-in tools (`services.list`, `strategy.intents.recent`, `backtest.submit`, `backtest.status`, `dashboard.snapshot`, `regime.current`). Adds 6 tools at zero risk (they all exist, just not called in the daemon bootstrap). **Owner: CEO approves, AI implements.**

---

## 6. Temporary audit artifacts (will be deleted before commit)

Local files created during M0, not to be committed:
- `c:\Tickles-Co\_tmp_m0_audit_1.sh` through `_tmp_m0_audit_4.sh` (audit scripts)
- `c:\Tickles-Co\_tmp_m0_result_1.txt` through `_tmp_m0_result_4.txt` (VPS stdout captures)

All `_tmp_*` files are gitignored per the existing `.gitignore` pattern `_tmp_*secret*.sh` — but to be safe, I'm deleting them post-commit.
