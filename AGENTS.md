# Tickles — Agent Context

Lean, durable project map for any agent working here (Hermes, Cursor, etc.).
Replaces the old 50KB `CLAUDE.md` (archived). Keep this **short and current** —
put history in `shared/docs/`, not here.

> Last verified: 2026-05-29.

## Host
VPS `vmi3220412`. Tailscale host `vmi3220412.trout-goblin.ts.net` (IP 100.71.74.12).

## Services & ports (verified live)
| Service | Addr | systemd unit |
|---------|------|--------------|
| Paperclip (web app) | `127.0.0.1:3100` | `paperclip.service` |
| Dashboard | `127.0.0.1:3101` (tailscale `/dashboard/`) | `tickles-dashboard.service` |
| MCP daemon (JSON-RPC, 111 tools) | `127.0.0.1:7777` | `tickles-mcpd.service` |
| Postgres (user `admin`) | `localhost:5432` | system |
| Qdrant (mem0 vectors) | `localhost:6333` | docker |
| code-server | `127.0.0.1:8080` (tailscale `:8080`) | `code-server@root` |

Other live workers: `tickles-interpretation`, `-memu-listener`, `-postmortem`,
`-edge-scorer`, `-coach`, `-pipeline-watchdog`, `-position-monitor`,
`-copy-trade-monitor`, `-demo-bridge`, `-chart-hacker-opinion`,
`-{telegram,discord,funding}-collector`, `-candle-daemon`, `-md-gateway`.

## Database reality (IMPORTANT — differs from old docs)
- Live data is written to **`tickles_shared`** with a **`company_id`** discriminator
  column. There are NOT separate per-company DBs in active use. `tickles_jarvais`
  exists but its signal tables are empty; `company_id='jarvais'` is the active tenant.
- Key tables (all in `tickles_shared`): `signal_interpretations`, `trader_performance`,
  `tracked_positions`, `position_postmortems`, `memu_outbox`, `mem0_recall_log`,
  `trader_profiles` (cross-company catalog), `copy_agent_state`, `contest_participants`.
- `memu` is a **separate Postgres DB** (MemU institutional memory): `insights` table,
  pgvector(384), dedup on `(kind, content_hash)`.

## Memory — mem0 (per-agent) & MemU (institutional)
**Never call `Memory` directly. Always use the helpers in `shared/utils/mem0_config.py`.**
- `get_memory(company, agent)` → Qdrant collection `tickles_{company}`,
  `user_id=company`, `agent_id={company}_{agent}`. Embeddings are LOCAL
  (all-MiniLM-L6-v2, 384d); `infer=False` stores raw text; LLM has a fallback chain.
- `get_dev_memory(agent)` → collection `tickles_dev`, `agent_id=dev_{agent}`. Use for
  build/dev session notes (Hermes writes here as `dev_hermes`). NEVER use
  `get_memory("tickles", …)` — it creates a garbage `tickles_tickles` collection.
- `MEM0_MODEL` default in code = `deepseek/deepseek-chat` (env-overridable).
- MemU: `from shared.memu.client import get_memu`; `write_insight(kind, content, …)`.
  Valid kinds in `shared/memu/insight_kinds.py` (`lesson`, `postmortem`, `warning`,
  `playbook`, …). Writes go via `memu_outbox` → `tickles-memu-listener` → `insights`.

## Agents — what actually runs
- **chart_hacker** is the only real "thinking" agent. It is NOT a daemon — it's an AI
  vision identity *inside* `tickles-interpretation.service` (a `trader_profiles` row,
  handle `chart_hacker`). It reads media, runs dual-track (vision LLM + quant), writes
  `signal_interpretations`, arms `tracked_positions`, gets LLM postmortems on closed
  ones, writes graded lessons to per-company mem0, and recalls them before the next
  read (Phase J, `_recall_relevant_memories` → `{recall_context}` prompt slot).
- **Copy agents** (`copy_spot_seq`, `copy_lev_parallel`, `copy_chai_vision`, …) mirror
  trader signals; `copy_chai_vision` additionally decides via vision. They currently
  do **not** learn (no postmortem/recall loop yet).
- **REMOVED 2026-05-29:** all surgeon agents, `chart_hacker_guru`, and OpenClaw
  (binary + workspace). See `shared/docs/SURGEON_GURU_REMOVAL_2026_05_29.md` (incl. rollback).

## Learning loop — current status (the active work)
- ✅ Postmortems written to `position_postmortems` and graded lessons to per-company mem0.
- ✅ Recall (Phase J) runs and is injected into chart_hacker's prompt.
- ❌ **Recall is unmeasured** — `record_recall()`/`link_recall_to_position()` exist in
  `shared/intelligence/recall_log.py` but are never called; `mem0_recall_log` is empty.
- ❌ **MemU is starved** — it holds outcome-less signal snapshots (kind `lesson`), not the
  graded postmortem lessons. `broadcast_insight()` exists but postmortem_service never
  calls it (its "handled elsewhere" comment is a TODO).
- ❌ Copy agents have no learning loop.
Planned: Phase A (promote postmortem lessons → MemU), B (wire recall measurement),
C (give copy agents a learning loop).

## MCP server (the AI-owned base)
`shared/mcp/` — 111 tools over HTTP (`:7777`) + stdio (`tickles_mcp_stdio.py`, generic).
Pattern: `McpTool` record + handler, registered via `registry.register(tool, handler)`;
every call audited to `public.mcp_invocations`. Self-improvement tools: `tools.catalogue`,
`tools.suggest`, `tools.usage_stats`, `tools.request_new`. Known gaps: `services.list`
returns empty via MCP, `dashboard.snapshot`/`regime.current` are stubs.

## Env
`/opt/tickles/.env` + `~/.bashrc`: `OPENROUTER_API_KEY`, `REQUESTY_API`, `DB_PASSWORD`,
`MEM0_MODEL` (optional). Hermes config lives in `~/.hermes/` (`SOUL.md` = persona only).

## Conventions (CEO rules)
Group code by feature, not file type. Always propose a phased plan + get sign-off before
destructive changes. Leave removed code archived/commented with documented rollback.
Log function name + params + return at debug level. Roadmaps in `shared/docs/`.
