# OpenMemory Guide — Tickles-Co (dgilbert002/tickles-co)

> **This is NOT JarvAIs.** JarvAIs lives at `c:\JarvAIs`.
> Tickles-Co is a multi-tenant trading platform ("the Building") that hosts
> companies-as-ideas. Keep the two projects separate in OpenMemory.

## Overview

Tickles-Co = platform layer (OpenClaw gateway + Paperclip orchestrator +
Tickles MCP control-plane + market data + memory + banker + treasury +
seven deterministic souls). Tenants = companies (SurgeonCo, PolyDesk,
LPCopyDesk, Capital CFD, MentorObserver, ...). Every company has its own
DB, Qdrant collection, Paperclip agents, and budget.

The CEO (you) talks to the platform through OpenClaw's `main` agent and
Paperclip's UI. Agents inside companies reach platform capabilities
exclusively through the Tickles MCP server (Phase 2).

## Architecture (short)

```
CEO --(OpenClaw chat)--> openclaw-gateway ---\
                                              > Tickles MCP (:7777) ---> platform services
CEO --(Paperclip UI)--> paperclip-server  ---/                           (md-gateway, catalog,
                                                                         backtest, Banker,
                                                                         Treasury, Souls,
                                                                         MemU, Qdrant, MySQL, PG)
```

Three-tier memory:
- **Tier 1** mem0 in Qdrant: `collection=tickles_{company}, user_id={company}, agent_id={company}_{agent}`
- **Tier 2** `tickles_{slug}` SQL DB + shared mem0 entries `agent_id=shared`
- **Tier 3** MemU (Postgres + pgvector) with `pg_notify('memu_broadcast', ...)`

## User Defined Namespaces

- [Leave blank — CEO populates when ready]

## Components (index)

- `shared/mcp/` — MCP server + tool registry + transports.
- `shared/souls/personas/` — Apex, Quant, Ledger, Scout, Curiosity,
  Optimiser, RegimeWatcher.
- `shared/trading/` — Banker (append-only), Treasury (policy gate),
  Capabilities, Sizer.
- `shared/memu/client.py` — cross-company memory, thread-safe, pg_notify.
- `shared/gateway/`, `shared/catalog/`, `shared/collectors/`, `shared/
  altdata/`, `shared/enrichment/`, `shared/events/` — data plane.
- `shared/connectors/` — CCXT + Capital adapters.
- `shared/execution/router.py` — paper / ccxt / nautilus.
- `shared/regime/`, `shared/guardrails/` — regime classifier + crash
  protection.
- `shared/auditor/` — Rule-1 parity (SQLite).
- `shared/backtest/` — engine / runner / queue / workers.
- `shared/migration/*.sql` — shared + per-company schemas.
- `new-project.sh`, `delete-project.sh` — CLI provisioning (MySQL only).

## Patterns

- Append-only trade ledger: every trade → `tickles_{slug}.trades` +
  `order_events` + optional `trade_validations`. Banker snapshots P&L,
  never mutates history.
- Treasury gate: every `execution.submit` must call `treasury.evaluate()`;
  denial reason lands in `treasury_decisions` and surfaces in UI.
- Learning loop (Twilly Templates 01/02/03): every trade → `autopsy.run`;
  every session → `postmortem.run`; every agent reads
  `learnings.read_last_3` before decisions.
- Per-company DB isolation: each tenant can only read/write its own slug;
  platform services can read all.

## Memory Seed (project facts to load into OpenMemory when live)

When the `user-openmemory` MCP server is healthy again, seed the following
facts with `project_id="dgilbert002/tickles-co"` (no `user_preference`):

1. component: `shared/mcp/` — MCP server + tool registry + transports (SSE/HTTP/stdio); built-in tools: ping, services.list, backtest.submit/status, dashboard.snapshot, regime.current. Phase 2 adds provisioning, memory, trading, learning tool groups.
2. component: `shared/souls/personas/` — seven deterministic souls (Apex, Quant, Ledger, Scout, Curiosity, Optimiser, RegimeWatcher) producing policy decisions, not raw LLM calls.
3. component: `shared/trading/treasury.py` — policy gate for every execution.submit; reads Capabilities + Banker snapshot + Sizer limits; emits allow/deny + reason.
4. component: `shared/memu/client.py` — hardened Postgres+pgvector cross-company memory with pg_notify('memu_broadcast', ...), content-hash dedup, thread-safe embedding.
5. project_info: VPS layout is captured in `.cursor/rules/vps-layout.mdc`. Ports 3100 Paperclip UI, 18789 OpenClaw gateway, 5432 system Postgres, 54329 Paperclip embedded PG, 6333 Qdrant, 3306 MySQL, planned 7777 Tickles MCP.
6. project_info: Three-tier memory contract — tier 1 mem0 (Qdrant) scoped by `tickles_{company}`; tier 2 SQL + shared mem0; tier 3 MemU pgvector with broadcast.
7. project_info: Rule-1 parity default for new companies = `advisory`. Backtest runs, never blocks, parity score shown.
8. project_info: Company-DB engine decision = Postgres for NEW companies (same instance as MemU). Existing `tickles_jarvais` + `tickles_shared` stay on MySQL during migration.
9. user_preference (project-specific): CEO prefers grouped files by feature (less files, not long files). Group MCP tools in `shared/mcp/tools/{provisioning,data,memory,trading,learning}.py`.
10. user_preference (project-specific): CEO wants full deep audits, never half-audits. No SwitchMode to plan without explicit request.
11. user_preference (project-specific): Always log `[module.function] params=... -> result`. Never remove code silently; comment-out with ROLLBACK note + roadmap pointer.
12. implementation: Phase-0 rotation checklist lives at `shared/docs/SECURITY_ROTATE_CHECKLIST.md`; .gitignore protects local repo from leaking docs containing third-party keys.
13. implementation: Phase-1 LLM cost tracking — `shared/cost_shipper/` (shipper.py, pricing.py, reconciler.py + systemd units) streams OpenClaw `~/.openclaw/agents/*/sessions/*.jsonl` usage/cost blocks into Paperclip `POST /api/companies/:id/cost-events`. Idempotent via OpenRouter `responseId` in SQLite dedup store. Daemon lives at `/opt/tickles/shared/cost_shipper/` on VPS, runs as `tickles-cost-shipper.service`. Reconciler timer fires daily 00:05 UTC comparing OpenRouter `/api/v1/auth/key` vs Paperclip spend (>5% drift warns). Agent urlKey (`main`, `cody`, `audrey`, `schemy`, `ceo`, `janitor`, `strategy-council-moderator`) is the join to Paperclip `agents.urlKey`.
14. component: `shared/cost_shipper/shipper.py` — tails OpenClaw session jsonls, extracts `{input, output, cacheRead, cost.total, responseId}`, normalizes to UTC Z-suffixed ISO8601, posts as metered_api cost_events. Cursor per session path in `/var/lib/tickles/cost-shipper/cursors.json`, dedup sqlite `shipped.sqlite`, 30-day TTL prune.
15. implementation: Phase-2 Tickles MCP daemon live on `http://127.0.0.1:7777/mcp` as systemd unit `tickles-mcpd.service`. 33 tools across 5 groups: `ping`, provisioning (company/agent CRUD via Paperclip API, 12 tools), data (catalog/md/altdata, 5 tools — catalog.list/get real, md.quote/candles/altdata.search stubs until md-gateway), memory (mem0/memu/learnings, 5 tools — returns canonical forward-to payload for host's mem0 MCP), trading (banker/treasury/execution, 6 tools — banker.snapshot real against Paperclip costs+finance endpoints, rest stubs until shared/trading/* mounted in daemon), learning (autopsy.run/postmortem.run/feedback.loop/feedback.prompts with Twilly Templates 01/02/03 verbatim). HTTP entrypoint `shared/mcp/bin/tickles_mcpd.py` reads `TICKLES_MCP_HOST/PORT/TOKEN` env; localhost binds skip auth. Smoke test at `shared/mcp/tools/_smoketest.sh`.
16. component: `shared/mcp/tools/provisioning.py` — 12 tools covering company + agent lifecycle, fully wired to live Paperclip API (`POST/GET/PATCH/DELETE /api/companies/:id{/agents/:id}`). Stateless; ToolContext carries paperclip_url + optional bearer. Destructive tools tagged `tags.destructive=true` so UI can require confirm.
17. component: `shared/mcp/tools/learning.py` — verbatim Twilly Template 01/02/03 prompts returned by `autopsy.run / postmortem.run / feedback.loop`. Each tool also returns a `memory_write_hint` payload telling the caller which mem0 scope + agent metadata to use for the resulting write; matches the three-tier contract (agent-private for autopsies/feedback, company-shared for postmortems).
18. implementation: Building Phase 3 — Paperclip Company Create Wizard. Provisioning flows POST `/api/companies {provisioning:{enabled,template,ruleOneMode,slug}}` → server seeds `company_provisioning_jobs` row (migration `0055_company_provisioning_jobs.sql`) → fires JSON-RPC `company.provision` to MCP :7777 with `{companyId, jobId, templateId, slug, ruleOneMode, memuSubscriptions}` (all camelCase). MCP executor runs 9 atomic steps (paperclip_row verify, postgres_db `tickles_<slug>`, qdrant_collection, mem0_scopes, memu_subscriptions, treasury_registration, install_skills, hire_agents, register_routines) and streams per-step `running`/`ok`/`skipped`/`failed` events plus a terminal `overallStatus` event to `POST /api/companies/<cid>/provisioning-jobs/<job>/events`. UI poller (`OnboardingWizard.tsx`) reads `GET /api/companies/<cid>/provisioning-status` every 1.5s. 6 templates in `shared/templates/companies/*.json`: blank (Layer 1 only), media, research, surgeon_co, polydesk, mentor_observer.
19. debug: ContextVars do NOT propagate through `loop.run_in_executor(None, fn, *args)` in Python 3.12; they DO propagate through `asyncio.to_thread(fn, *args)` because it calls `contextvars.copy_context()` before dispatch. Caught this when the provisioning job_id (stored in `_CURRENT_JOB_ID` ContextVar inside `shared/provisioning/executor.py`) was invisible to step threads — every step emit went to the legacy `/provisioning-events` URL and 404'd, only the event-loop-thread terminal emit hit `/provisioning-jobs/<job>/events`. Fix: swap all `loop.run_in_executor(None, ...)` inside `run()` for `asyncio.to_thread(...)`. Applies any time a worker thread needs to read a ContextVar set on the event-loop thread. Also bit us once: MCP JSON-RPC `arguments` must be camelCase (`jobId`, `companyId`, `templateId`) — snake_case gets silently dropped by the schema validator.
20. debug: OpenClaw agent discovery requires `adapterConfig.agentId` (NOT `agentKey`) to match a directory `/root/.openclaw/agents/<agentId>/` owned by root. Two prior blockers: (a) `tickles-mcpd.service` ran with systemd `ProtectHome=read-only` so the executor could not create dirs — override at `/etc/systemd/system/tickles-mcpd.service.d/override.conf` with `ReadWritePaths=/root/.openclaw`; (b) executor was setting `agentKey` instead of `agentId`. Fix: `shared/provisioning/executor.py::_hire_one_agent` writes both `agentId` + `agentKey` (for back-compat) to `adapterConfig`, then calls `_openclaw_clone` + `_openclaw_customize` to clone `cody` and rewrite `AGENT.md`/`HEARTBEAT.md` with the new agent's identity. Backfill script `_paperclip_patches/phase4a_backfill.py` recovers pre-existing agents.
21. debug: MCP `memory.add/search` scope param is a TIER LITERAL: `"agent" | "company" | "building"`. It is NOT the resolved namespace. `companyId` + `agentId` are separate camelCase args. Tool returns `{forward_to: "user-mem0::add-memory", arguments: {namespace, user_id, agent_id}}` — the host (OpenClaw/Cursor) forwards to its local `user-mem0` MCP. Agent-facing AGENT.md must document this explicitly; an earlier version listed `"mem0 scopes: agent_<key>, company_<slug>, building"` which was misleading (those are the resolved namespaces, not the scope literal). Fixed in `executor.py::_openclaw_customize` + `phase4a_backfill.py`.
22. implementation: Building Phase 4a-sandbox+executor+backfill — on VPS, created systemd drop-in `/etc/systemd/system/tickles-mcpd.service.d/override.conf` with `ReadWritePaths=/root/.openclaw`; updated `executor.py` to use `agentId` (+ back-compat `agentKey`) and to call `_openclaw_customize` writing AGENT.md/HEARTBEAT.md/meta.json; ran `phase4a_backfill.py` to re-customize Tickles-n-Co + Building agents; trimmed templates catalog to two entries: `blank` (Layer 1 only) and `trading` (Layer 1+2 with 1 pre-hired CEO agent). Paperclip `agents.ts` was patched to auto-inject `adapterConfig.url = ws://127.0.0.1:18789` and `headers["x-openclaw-token"]` from `/etc/paperclip/openclaw-gateway.env` so new agents work one-click.
23. implementation: TradeLab bootstrap smoke (UI-driven) — created via Paperclip wizard at http://localhost:3100 (SSH tunnel 3100→VPS:3100), template=`trading`, Rule-1=`advisory`. Provisioning reported `[9/9 ok] register_routines — provisioning ok` and `hired 1/1 agents`. Agent `tradelab_ceo` (paperclip aid=`0aff984d-e3a4-4f69-8636-ac29546ed5a0`, companyId=`25c28438-1208-4593-82fc-d86b460a4a1e`) visible in BOTH Paperclip (`adapterType=openclaw_gateway`, url+token set) AND OpenClaw (`/root/.openclaw/agents/tradelab_ceo/` with customized AGENT.md + HEARTBEAT.md + meta.json). Layer 1 infra confirmed: Postgres DB `tickles_tradelab`, Qdrant collection `tickles_tradelab` (384-dim cosine, status=green). MCP smoke v2 at `shared/artifacts/tradelab_bootstrap_smoke.json` shows 12/12 tools respond, 9 with real data (ping, catalog.list, memory.add/search/agent+company, memory.search/building, banker.snapshot, learnings.read_last_3, feedback.prompts) and 3 honest Phase 2.5 stubs (md.quote, md.candles, treasury.evaluate).
24. debug: OpenClaw has TWO agent registries that must both be updated for a new agent to appear in the Control UI: (a) the on-disk directory `/root/.openclaw/agents/<id>/` — needed for chat/overlays — and (b) the `agents.list[]` array inside `/root/.openclaw/openclaw.json` — needed for the `/agents` dropdown. Paperclip's `openclaw-gateway` adapter only created (a); `agents.list[]` is empty except for 4 seed legacy agents (`main/cody/schemy/audrey`). Fix in `shared/provisioning/executor.py::_openclaw_register_in_registry`: read → backup (`openclaw.json.bak.phase5-<iso>`) → upsert → write. CRITICAL: the zod schema for entries is STRICT — only `id`, `model`, `heartbeat`, `tools` are allowed. Writing a `paperclip` metadata key crashes the gateway with `Unrecognized key`. Workaround: persist companySlug/role/urlKey to a SIDE-FILE at `/root/.openclaw/tickles-meta-map.json` (same dir, our file). After fix: gateway `ready (7 plugins)`, `http=200`, 12 agents in dropdown.
25. implementation: Building Phase 5 — Agent visibility + 8-file overlay + Services-vs-Agents. `_openclaw_customize` now writes the full OpenClaw overlay set: AGENT.md/SOUL.md/IDENTITY.md/TOOLS.md/USER.md/HEARTBEAT.md/BOOTSTRAP.md/MEMORY.md + meta.json (9 files). Each file starts with `<!-- generated-by: shared/provisioning/executor.py / phase5 -->` so we know which files we own vs hand-edited. `_openclaw_register_in_registry` upserts `agents.list[]` (see memory #24). `phase4a_backfill.py` was rewritten to reuse both helpers via `synth_template_agent`, re-processes all `openclaw_gateway` agents across all companies, and supports `--force-overwrite` to stomp generated files. New agents default to `heartbeat=None` (disabled) — continuous monitoring belongs to services (see ROADMAP §5.4: systemd timers → event-bus → Paperclip `/api/events/publish` → agent wake). Phase 5 mandate TRA-1 proves MCP surface end-to-end in `shared/artifacts/tradelab_ceo_firstrun.md`: 7/11 tools work, 2 stubs (md.quote, treasury.evaluate, marked Phase 2.5), 2 real tool bugs queued as Phase 6 (agent.get hits wrong paperclip route `/api/companies/{cid}/agents/{aid}` instead of `/api/agents/{aid}`; autopsy.run wants `tradeId` not inline trade object).

## Guide discipline

- Update this file before storing new memories.
- After editing a shared component, update the Components section.
- Never store API keys, tokens, passwords, or connection strings here or
  in mem0 / openmemory.
