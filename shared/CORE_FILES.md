# CORE_FILES.md — Janitor allowlist

> **Purpose.** Human-controlled list of files and globs the Janitor agent (Phase
> 1B) **must never touch** — regardless of mtime, atime, or LLM "this looks
> unused" opinions.
> **Rule.** If in doubt, add it here. The Janitor treats anything listed as
> Tier 0 / Tier 2 (see `ROADMAP_V3.md §Phase 1B`).
> **Who edits this file.** Only Dean, or an agent that has explicit
> instruction from Dean to add/remove an entry. Never the Janitor itself.

---

## Format

Each entry is one line:
```
<glob>  # <status>: <why>
```

Statuses:

- `core` — actively used, in a running service, or an identity document.
- `pending_cutover` — not running today, but part of the planned architecture.
  Janitor treats this the same as `core`.
- `keep_history` — read-only historical record. Never moved, never rewritten.

---

## Identity documents (never touch)

```
SOUL.md                                      # core: agent identity
MEMORY.md                                    # core: living memory
ROADMAP_V2.md                                # keep_history: pre-V3 plan
ROADMAP_V3.md                                # core: current roadmap
TOOLS.md                                     # core: tool registry
CORE_FILES.md                                # core: this file
ARCHITECTURE.md                              # core: system design
CompanyIdeas.md                              # core: company backlog
README.md                                    # core: repo entrypoint
.env                                         # core: secrets (never archived)
```

## Cursor/IDE rules

```
.cursor/rules/**/*.mdc                       # core: editor behaviour rules
.agents/**                                   # core: skills + subagent defs
```

## Running services (systemd units + their targets)

```
systemd/**                                   # core: all unit files
backtest/**                                  # core: active backtest stack (tickles-bt-workers.service)
candles/**                                   # core: active candle daemon (tickles-candle-daemon.service)
catalog/**                                   # core: active catalog API (tickles-catalog.service)
collectors/**.py                             # core: collector base + media extractor (services/run_all_collectors.py entrypoint)
connectors/**                                # core: exchange read adapters (CCXT, Capital.com)
market_data/**                               # core: gap detector, retention, timing service, candle service
memu/**                                      # core: MemU client + schema
services/**                                  # core: cross-cutting daemons (run_all_collectors.py)
utils/**                                     # core: shared utilities (db, config, timing, mem0)
guardrails/**                                # core: invariant checks
scripts/e2e_smoke.py                         # core: regression harness
scripts/seed_reference_data.py               # core: idempotent reference data
local_runner/**                              # core: Dean's desktop runner (local-only, not on VPS)
```

## Pending cutover (planned but not running yet)

```
discord_collector.py                         # pending_cutover: root-level; moves under collectors/ when Phase 3 lands
telegram_collector.py                        # pending_cutover: root-level; moves under collectors/ when Phase 3 lands
trading/**                                   # pending_cutover: Phase 2 home (sizer, treasury, oms, risk_agent)
agents/**                                    # pending_cutover: Phase 1B (janitor) / Phase 9 (validator, optimizer)
migration/**                                 # core: live SQL + seeds + historical planning docs
```

## Data directories (large, non-code; never archive)

```
collectors/discord/**                        # core: discord_config.json + media cache (cache deleted 2026-04-18, will regrow when collector is re-enabled)
collectors/telegram/**                       # core: media cache
collectors/news/**                           # core: cached articles
collectors/tradingview/**                    # core: chart captures
```

## Secrets / env files (world-blind, never archive)

```
/etc/tickles/discord.env                     # core: DISCORD_BOT_TOKEN (600 root:root, not in git)
/opt/tickles/.env                            # core: shared env file (600, not in git)
```

## History to preserve (never move, never rewrite)

```
_archive/**                                  # keep_history: every archived version stays
ROADMAP_V2.md                                # keep_history: referenced by past commits
```

---

## How the Janitor uses this file

1. Load all globs above into memory.
2. For every candidate file found by `find`, match against these globs.
3. A match on **any** `core` or `pending_cutover` entry → skip, do not report.
4. A match on `keep_history` → report but never propose a move.
5. A file that matches nothing here **and** is reached by the Python import
   graph from any `core` file → treated as Tier 1 (also skipped).
6. Only files that survive steps 1-5 AND have `atime > 60d` AND `mtime > 60d`
   are candidates for the Tier 3 report.

## Change log

| Date | Who | Change |
|---|---|---|
| 2026-04-17 | Dean + Opus | initial draft, Phase 1A kickoff |
| 2026-04-18 | Opus | sync-from-VPS: replaced invented subfolders (market_data/collectors, market_data/adapters, trading/adapters) with VPS canon (top-level `collectors/` and `connectors/`). Added `services/`, `guardrails/`, `collectors/*` data dirs to allowlist. |
| 2026-04-18 | Opus | Phase 3A.1: discord media cache deleted (was 25 GB, none of it persisted to DB). Added `/etc/tickles/discord.env` + `/opt/tickles/.env` to the secrets allowlist. |
