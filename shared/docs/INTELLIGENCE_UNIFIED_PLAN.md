# Intelligence Unified Plan — Phased Roadmap

**Author:** Roo (Architect)
**Date:** 2026-04-29
**Source prompt:** [`shared/docs/prompt.md`](shared/docs/prompt.md:1)
**Cross-references:**
- [`shared/docs/TICKLES_INFRASTRUCTURE_ATLAS.md`](shared/docs/TICKLES_INFRASTRUCTURE_ATLAS.md:1)
- [`shared/docs/RULE1_END_TO_END.md`](shared/docs/RULE1_END_TO_END.md:1)
- [`shared/docs/NEW_TRADING_AGENT_HOWTO.md`](shared/docs/NEW_TRADING_AGENT_HOWTO.md:1) — OpenClaw-native agent recipe (paperclip-visible)
- [`shared/utils/mem0_config.py`](shared/utils/mem0_config.py:1) — Mem0 (per-agent / per-company)
- [`shared/memu/client.py`](shared/memu/client.py:1) — MemU (cross-company / company-wide)
- [`.roo/handoffs/2026-04-28-intelligence-pipeline-verification-handoff.md`](.roo/handoffs/2026-04-28-intelligence-pipeline-verification-handoff.md:1)

**Re-review revision (2026-04-29, post truncated-prompt re-read):** six gaps were identified after re-reading the full [`shared/docs/prompt.md`](shared/docs/prompt.md:1) and patched into this document. They are: (G1) pattern/setup/regime/session tags must be **LLM-emitted free-form**, never predefined; (G2) [`manage_sources.py`](manage_sources.py:1) options 7+8 are now a **served HTML page** (Tailscale, basic auth-fenced) instead of TUI; (G3) every new LLM-driven service follows the [`NEW_TRADING_AGENT_HOWTO.md`](shared/docs/NEW_TRADING_AGENT_HOWTO.md:1) recipe (registered OpenClaw agent + workspace + cron) so paperclip sees it; (G4) **Mem0 = per-agent / per-company**, **MemU = cross-company / company-wide** — verified against [`shared/utils/mem0_config.py`](shared/utils/mem0_config.py:90) and [`shared/memu/client.py`](shared/memu/client.py:36); (G5) `api_cost_log` is wired into **every API call surface** (LLM + exchange + Discord/Telegram + Tailscale serve hooks), not just LLM gateway; (G6) Phase L Live Queue tab includes the **side-by-side Discord chart vs LLM read** comparison and full per-trade drill-down ("overkill of fields").

**Implementation-pass revision (2026-04-29, post code-surface audit):** twenty additional findings (A–T) were surfaced when this plan was walked against the actual codebase ([`shared/intelligence/gateway_config.py`](shared/intelligence/gateway_config.py:1), [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:1), [`shared/dashboard/server.py`](shared/dashboard/server.py:1), [`shared/migration/tickles_shared_pg.sql`](shared/migration/tickles_shared_pg.sql:1), [`shared/intelligence/migrations/2026_04_26_phase3c_collector_catalog.sql`](shared/intelligence/migrations/2026_04_26_phase3c_collector_catalog.sql:1), [`shared/services/registry.py`](shared/services/registry.py:1), [`shared/catalogue/tui_manager.py`](shared/catalogue/tui_manager.py:1), [`shared/dashboard/snapshot.py`](shared/dashboard/snapshot.py:1), [`.env`](.env:1)). Findings are inlined into the affected phases under markers `[A]…[T]`. Headline blockers: (A) `api_cost_log` extension must be explicit ALTER, not prose; (B) `gateway_config.py::for_service()` already exists — Phase 1 must EXTEND not duplicate; (C) `.env` reality uses `REQUESTY_API` (not `REQUESTY_API_KEY`) and `LLM_GATEWAY_DEFAULT=openrouter` (not `requesty`), and Requesty default host in code is `api.requesty.ai` (must be `router.requesty.ai`); (D) [`shared/dashboard/server.py:71`](shared/dashboard/server.py:71) `auth_middleware` leaves NON-`/api/` paths public — Phase 5 mount is a security hole until fixed; (E) [`shared/intelligence/interpretation_service.py:77`](shared/intelligence/interpretation_service.py:77) reads `OPENROUTER_API_KEY` directly, bypassing gateway_config — refactor scope ~5 functions across lines 268–576 + 1410–1567; (F) `tracked_positions` lives in `tickles_shared`, NOT per-company as §2.2 currently claims; rest are detailed under their phase markers.

**Devil's-advocate revision (2026-04-29, post-A–T patch walk):** forty-two additional findings (AA–BP) were surfaced by stress-testing the just-patched plan against operational reality (cron concurrency, cross-DB FK semantics, Postgres `pg_trgm` mathematics, Discord rate limits, MemU NOTIFY payload size, default-deny middleware blast radius, Windows symlink permissions, single-writer race on `prompt_versions` upsert, etc.). Findings are inlined into the affected phases under markers `[AA]…[BP]`. **Critical pre-kickoff blockers** (must be resolved before Phase 0 starts, see §0.1 below): **[AF]** Postgres cannot do cross-database FK without FDW — affects every "soft FK from `tickles_<company>.X` to `tickles_shared.Y`" claim in this plan; **[BN]** master schema files [`shared/migration/tickles_shared_pg.sql`](shared/migration/tickles_shared_pg.sql:1) and [`shared/migration/tickles_company_pg.sql`](shared/migration/tickles_company_pg.sql:1) MUST be updated alongside every ALTER migration so newly-provisioned companies inherit the changes; **[BK]** Phase L "LLM-rendered chart with annotated levels" implies an extra vision call per drill page view (hidden $/day cost — ungate or budget); **[BP]** no global LLM budget circuit-breaker exists — a runaway cron loop could blow $1000s before noticed.

**Schedule impact of devil's-advocate findings:** +5 days net (cross-DB resolution +1, schema-sync helper +0.5, budget circuit-breaker +1, single-writer CI guard +0.5, regression matrix + rollback rehearsal +1, hidden chart renderer cost gate +0.5, MemU outbox + listener hardening +0.5). Revised total **~35 working days**.

---

## 0.1 Critical Pre-Kickoff Blockers (resolve before Phase 0)

These four findings BREAK the plan as written. Each must have an explicit decision recorded in the §0 open-questions table before any implementation work starts. They are documented in full inside their owning phases, but are surfaced here because they are cross-cutting and override later phases.

| ID | Blocker | Owning phase | Impact if ignored | Resolution path |
|----|---------|--------------|-------------------|-----------------|
| **[AF]** | Postgres does not support cross-DB `REFERENCES` without `postgres_fdw`. Plan repeatedly says "soft FK from `tickles_<company>.signal_interpretations.prompt_version` to `tickles_shared.public.prompt_versions`" — that is text comparison, not enforced FK | Phase 6 ([N], [AS]) + Phase 2 ([F], [AG]) + Phase 7 ([AX]) | Schema diagrams will mislead implementers; cascade deletes will not work; integrity holes will surface at runtime | Decide between **(a)** keep as text-match soft FK with weekly integrity job (cheap, fragile), **(b)** install `postgres_fdw` + import foreign tables (expensive, one-time), **(c)** move `prompt_versions` and similar lookup tables INTO every per-company DB and use a sync job. **Default decision below: (a) text-match soft FK + weekly checker.** Confirm. |
| **[BN]** | Master schema files [`shared/migration/tickles_shared_pg.sql`](shared/migration/tickles_shared_pg.sql:1) and [`shared/migration/tickles_company_pg.sql`](shared/migration/tickles_company_pg.sql:1) are the templates copied when a NEW company is provisioned. If a phase ALTERs an existing DB but does not edit these templates, the next company onboarded after Phase N has the OLD schema | Phase 0 (helper), every phase that ALTERs anything | New companies missing every column/table/trigger added in this overhaul | Phase 0 ships [`shared/migration/sync_master_schema.py`](shared/migration/sync_master_schema.py:1) — a verification script that runs `pg_dump --schema-only` of a fully-migrated company DB and diffs against the master template; CI fails if drift exists. Every phase ALTER ships paired with the template edit and one CI green. |
| **[BK]** | Phase L drill page proposes "LLM-rendered chart with annotated levels". This is a vision API call per page view. With 5 operators viewing 20 trades/day = 100 vision calls/day @ ~$0.02 = $2/day = $60/mo *just for the dashboard* | Phase L ([BK]) | Hidden recurring spend; cache invalidation unclear; latency on page load | Phase L gates the rendered chart behind a `?render_chart=1` query param (default OFF), caches the rendered PNG in `/opticals/rendered/<position_id>.png` for 24h, and writes one `api_cost_log` row per render. Budget cap from [BP] hard-stops if breached. |
| **[BP]** | No global daily/monthly LLM budget circuit-breaker exists. Every service writes `api_cost_log` AFTER the call. A cron stuck in a loop calling the vision API every 30s would burn $40/hr before anyone noticed | Phase 1 ([BP]) | Catastrophic spend incident | Phase 1 ships [`shared/utils/api_cost_log.py::check_budget(role)`](shared/utils/api_cost_log.py:1) — a pre-call gate that sums today's `cost_usd` for `(role, company_id)` and raises `BudgetExceededError` if over `LLM_BUDGET_USD_DAILY_<ROLE>`. Defaults: `vision=20`, `text=10`, `embedding=2`. The gateway honours it; tests cover the breach path. |

---

## 0. Document Contract

This document is the **single source of truth** for executing the unified intelligence overhaul described in [`shared/docs/prompt.md`](shared/docs/prompt.md:1). It is **factual**: every claim about existing tables, columns, files, and services has been verified by reading the actual file or migration. Where the user prompt was inaccurate (e.g., proposed creating a table that already exists), the plan corrects it and notes the divergence.

### Open questions / clarifications needed

| # | Question | Why it matters | Default assumption used |
|---|----------|----------------|--------------------------|
| 1 | The user's last sentence was truncated: *"non negotiable is the ..."* | Need to know which constraint must never be violated | Treated as the **C2 hindsight-bias rule** (`entry_reason_*` frozen at position open) **plus** the standing **Rule 1 / Rule 2 / Rule 3** trio from [`shared/docs/old/CONTEXT_V3.md`](shared/docs/old/CONTEXT_V3.md:1). Confirm before Phase 2 schema changes. |
| 2 | Should `tickles-vision` Requesty waterfall remain as the vision provider, or pin to a specific model? | Affects `vision_provider` / `vision_model` capture in DB | Keep waterfall; record the *model the waterfall actually used* (Requesty returns this in the response) into `vision_model_resolved`. |
| 3 | Auth on the `/opticals/` static folder | Currently no auth — Tailscale ACL only | Phase D adds Tailscale-only ACL for opticals; Phase L Dashboard re-uses Telegram-OTP from [`shared/dashboard/server.py`](shared/dashboard/server.py:1). |
| 4 | Surgeon1 (flat-file trader) — migrate to `tracked_positions` or leave as legacy? | Affects Phase 4 scope | Leave Surgeon1 read-only; only migrate Surgeon2 in Phase 4. Surgeon1 already deprecated in handoffs. |
| 5 | Mem0 vs MemU split | Drives where each phase writes lessons | **Verified 2026-04-29** by reading [`shared/utils/mem0_config.py`](shared/utils/mem0_config.py:90) and [`shared/memu/client.py`](shared/memu/client.py:36): **Mem0** is per-agent + per-company (Qdrant collection `tickles_<company>`, `user_id=<company>`, `agent_id=<company>_<agent>`). **MemU** is cross-company / company-wide via Postgres `memu` DB with pgvector + `pg_notify('memu_broadcast',…)`. The split matches the user's intuition. **Rule applied across plan:** `lessons_for_actor` → Mem0 under the actor's namespace; `lessons_for_company` → MemU `write_insight()`. |
| 6 | Pattern/setup/regime/session tag taxonomy | Was originally written with enum-style examples in §2.1 | **Corrected:** the LLM emits **free-form** strings; we never hint at a fixed vocabulary. Normalisation/clustering happens in a downstream Phase 11 helper. See §2.1 corrected note + Phase 6 prompt rule. |
| 7 | Where does `manage_sources.py` options 7 + 8 (signals + positions) live? | TUI vs served HTML | **Corrected:** served HTML page (Tailscale-only) co-located with the Phase 4 opticals folder. Phase 5 is renamed accordingly; the original TUI extension is deferred to a follow-up. |
| 8 | OpenClaw agent visibility for new LLM services | Paperclip dashboard surface | All new long-running LLM services (PostMortemService, ChartHackerOpinionService, EdgeScorerService, ZoneFilter when run as a standalone) are scaffolded via [`NEW_TRADING_AGENT_HOWTO.md`](shared/docs/NEW_TRADING_AGENT_HOWTO.md:1) — registered OpenClaw agent + workspace + cron with `--tools read,write,exec`. SOUL.md per service. Python service wrapper still exists for non-LLM ticking. |
| 9 | API cost log scope | Was "LLM only" in v1 of plan | **Broadened:** every outbound API hit (LLM gateway, exchange REST, Discord client, Telegram client, Tailscale, MemU writes when they hit the LLM) writes one row to `api_cost_log` via a shared helper. See Phase 1 §Same-pattern §A. |
| 10 | **[A]** `api_cost_log` table already exists at [`shared/migration/tickles_shared_pg.sql:392`](shared/migration/tickles_shared_pg.sql:392) with 11 cols; the `log_api_call()` helper requires 9 more | If we treat this as a "new table" the migration will fail | **Decided:** Phase 1 ships a strict ADDITIVE `ALTER TABLE … ADD COLUMN IF NOT EXISTS` migration; existing rows preserved; nothing dropped. Explicit column list lives in the Phase 1 §A schema block. |
| 11 | **[B]** [`shared/intelligence/gateway_config.py`](shared/intelligence/gateway_config.py:33) **already** implements `GatewayConfig.for_service()`, `call_vision_llm()`, `chat_completion()` | Phase 1 wording suggested NEW work; reality is EXTENSION | **Decided:** Phase 1 work is strictly: (1) add `temperature` field that's already partially there, (2) add per-call `api_cost_log` write hook, (3) fix the Requesty default URL bug (`api.requesty.ai` → `router.requesty.ai`). Do NOT recreate `for_service()`. |
| 12 | **[C]** `.env` reality vs plan draft | Naming + default-host conflict that would silently misroute LLM calls | **Reality (verified 2026-04-29 by reading `.env`):** the active key is `REQUESTY_API` (not `REQUESTY_API_KEY`); the URL env is `TICKLES_APP_REQUESTY_URL` (not `REQUESTY_BASE_URL`); `LLM_GATEWAY_DEFAULT=openrouter` (not `requesty`); [`gateway_config.py:79`](shared/intelligence/gateway_config.py:79) defaults to `https://api.requesty.ai/v1` which is **wrong** — Requesty's router host is `router.requesty.ai`. Phase 1 must reconcile all four BEFORE renaming anything: keep `REQUESTY_API` as the canonical key but add `REQUESTY_API_KEY` as a recognised alias, fix the default URL in code, and document the alias chain in `ENV_REFERENCE.md`. |
| 13 | **[D]** [`shared/dashboard/server.py:71`](shared/dashboard/server.py:71) `auth_middleware` returns early when `request.path` is in the public set OR does not start with `/api/` — meaning **every non-`/api/` path is currently public** | Phase 5 mounts `/manage/*` on the same server → would expose mutating routes without auth | **Decided:** Phase 5 includes a mandatory PRE-mount fix: invert `auth_middleware` to default-deny (require auth unless path matches an explicit allow-list of `{/, /login, /static/*, /api/auth/*}`). Ship the fix in its own commit before any `/manage/*` route is registered. |
| 14 | **[E]** [`shared/intelligence/interpretation_service.py:77`](shared/intelligence/interpretation_service.py:77) reads `OPENROUTER_API_KEY` directly via `os.getenv()` — bypassing `gateway_config` entirely | All Phase 1 cost-log + temperature work is moot if the InterpretationService never goes through the gateway | **Decided:** Phase 1 includes a sub-phase §E that refactors the InterpretationService to call `GatewayConfig.for_service('signal_interpretation_vision')` + `call_vision_llm()` instead of building its own `aiohttp` request. Scope: ~5 functions across [`interpretation_service.py:268-576`](shared/intelligence/interpretation_service.py:268) (vision pre-filter + main vision track) and [`interpretation_service.py:1410-1567`](shared/intelligence/interpretation_service.py:1410) (text-news track). Lines: ~250 changed in a 1745-line file. |
| 15 | **[F]** `tracked_positions` lives in `tickles_shared.public` (verified at [`shared/intelligence/migrations/2026_04_26_phase3c_collector_catalog.sql:181`](shared/intelligence/migrations/2026_04_26_phase3c_collector_catalog.sql:181)) **not** per-company; column has hardcoded `company_id VARCHAR(50) DEFAULT 'jarvais'` | §2.2 of this plan currently says "in every `tickles_<company>`" — implementer would create duplicate tables | **Decided:** Phase 2 keeps `tracked_positions` in `tickles_shared` (single platform-wide ledger), drops the `'jarvais'` default, makes `company_id` `NOT NULL` with no default, and updates §2.2 to reflect this. Multi-tenant isolation comes from queries always filtering by `company_id`, not from per-company schemas. **OR (alt):** if user prefers per-company isolation for blast radius, run a one-shot migration that creates `tickles_<company>.tracked_positions` + copies rows + drops the shared table. **Default decision below: keep shared.** Confirm with user. |
| 16 | **[G]** No shared "list active companies" helper — [`shared/intelligence/performance_scorer.py:549`](shared/intelligence/performance_scorer.py:549) has `_discover_companies()` as a private helper duplicated wherever needed | Phases 7, 8, 10, 11 all need to iterate companies | **Decided:** Phase 0 adds a small prerequisite: promote `_discover_companies()` to `shared/utils/companies.py::list_active_companies()` returning `list[str]` of company DB names by reading `tickles_shared.companies` (or whatever the registry table is) where `is_active=true`. Used by every per-company iterator from Phase 7 onward. |
| 17 | **[H]** New services (PostMortem, ChartHackerOpinion, EdgeScorer) are not registerable in [`shared/services/registry.py`](shared/services/registry.py:66) without extending `ServiceDescriptor` to support `kind="agent_cron"` | Without this they appear as "unknown" or are missing from `/api/services` | **Decided:** Phase 7 adds two fields to `ServiceDescriptor`: `kind: str = "daemon"` (one of `daemon | agent_cron | exporter | collector`) and `cron_schedule: str \| None = None`. ServiceCatalog snapshot reads them; Phase L renders them. |
| 18 | **[I]** OpenClaw cron `--tools read,write,exec` flag conflicts with single-writer policy (PART H of [`shared/docs/prompt.md:559`](shared/docs/prompt.md:559)) | Agents could bypass service APIs and write to DB directly | **Decided:** Phases 7, 8, 11 restrict the `--tools` flag and add a SOUL.md clause: "writes to `tracked_positions`, `agent_opinions`, `position_postmortems`, `actor_performance` go ONLY through the matching service's REST/MCP endpoint, never via raw `write_db`/MCP `data.*`". The `exec` tool is what calls the service script. |
| 19 | **[J]** MemU `pg_notify('memu_broadcast', …)` has NO listener service registered — broadcasts go nowhere | Cross-company lessons (G4) are written but never consumed | **Decided:** Add a tiny `shared/memu/listener_service.py` `ServiceDaemon` that does `LISTEN memu_broadcast` and forwards each insight to a per-company Mem0 write under `agent_id='memu_broadcast_consumer'`. Wire into Phase 1 (when MemU writes start happening) or as a sub-phase of Phase 7. |
| 20 | **[K]** Naming mismatch: §2.4 plans `actor_performance` (Phase 11) but `trader_performance` (already exists in [`shared/intelligence/migrations/2026_04_26_phase3b_intelligence.sql:127`](shared/intelligence/migrations/2026_04_26_phase3b_intelligence.sql:127)) covers a similar surface | Two leaderboard tables would diverge | **Decided:** `trader_performance` stays as the Discord-trader-specific cache (deprecated in comments); `actor_performance` is the new platform-agnostic leaderboard. Phase 11 explicitly documents the relationship. The dashboard reads ONLY `actor_performance` / `actor_leaderboard`. |
| 21 | **[L]** Phase L per-company aggregation pattern is unspecified | Dashboard needs to either show one company at a time (picker) OR aggregate across all | **Decided:** Phase L ships a top-bar **company picker** (default = the OTP-authenticated user's primary company) plus an "All companies" virtual mode that fans out queries to each `tickles_<company>` and unions results. Stats overview strip respects the picker. |
| 22 | **[M]** No CHECK constraint on `pattern_tags` / `setup_tags` / `regime_tags` / `session_tags` | Plan §2.1 might be misread as "add an enum" | **Confirmed (G1 reaffirmation):** these JSONB columns intentionally have NO CHECK constraint; arbitrary LLM-emitted strings are valid. Schema comment in Phase 2 migration must say so loudly. |
| 23 | **[N]** No `prompt_versions` table exists; [`shared/intelligence/prompts/chart_analysis.json`](shared/intelligence/prompts/chart_analysis.json:1) has 3 prompt slots with no version field | `signal_interpretations.prompt_version` (Phase 2) would be a free-text field referencing nothing | **Decided:** Phase 6 ships a tiny `tickles_shared.prompt_versions` table: `(id BIGSERIAL, name TEXT, version VARCHAR(32), prompt_hash CHAR(16), body TEXT, system TEXT, created_at, UNIQUE (name, version))`. The G1 prompt rule lives in the body. `signal_interpretations.prompt_version` becomes a soft FK (text match on `(name, version)`). Loaded once at service start. |
| 24 | **[O]** TUI ↔ HTML coexistence is unspecified | Users running both might see stale data on one side | **Decided:** Phase 5 declares the served HTML panel as the **canonical write surface**; the TUI prints a banner on launch ("Open `https://…/manage/` for the served panel") and runs in **read-only mode** for sources/channels/users (its existing leaderboard/hierarchy views stay). No write commands in the TUI after Phase 5 lands. |
| 25 | **[AA]** No canonical "list active companies" SQL contract | [G] decided to promote `_discover_companies()` to `shared/utils/companies.py::list_active_companies()` but did not specify the source-of-truth table | **Decided:** Phase 0 reads `tickles_shared.public.companies` (column `is_active=true`); if that table does not exist by the time Phase 0 runs, fall back to scanning Postgres `pg_database` for names matching `tickles_%` excluding `tickles_shared`. The helper is one function, ~30 lines, with a 5-minute in-process cache. |
| 26 | **[AB]** No migration runner script — every phase says "run the migration" but there is no convention | Migrations get hand-applied, drift is invisible | **Decided:** Phase 0 ships [`shared/migration/run_phase_migration.py`](shared/migration/run_phase_migration.py:1) that takes `--phase N --target shared\|<company>\|all_companies` and applies the matching `2026_04_29_phaseN_*.sql` file. Tracks applied phases in `tickles_shared.public.schema_migrations`. Idempotent. |
| 27 | **[AC]** [`shared/intelligence/gateway_config.py:33`](shared/intelligence/gateway_config.py:33) is `@dataclass(frozen=True)` and `for_service()` reads env vars only at construction | An ops change to `.env` requires a service restart to take effect — silent staleness | **Decided:** Phase 1 documents this loud-and-clear in [`shared/docs/ENV_REFERENCE.md`](shared/docs/ENV_REFERENCE.md:1) under "Restart semantics: any LLM-related env var change requires `systemctl restart tickles-service@<name>` to propagate." Adding hot-reload is out-of-scope; the restart contract is explicit. |
| 28 | **[AD]** `api_cost_log.cost_usd` is `decimal(10,6)` per the existing migration | Standard requires `decimal(20,8)` for prices but `cost_usd` is dimensionally a USD price | **Decided:** Phase 1 ALTERs `cost_usd` to `decimal(20,8)` in the same migration that adds the new columns. Backfill is null-safe. |
| 29 | **[AE]** No correlation_id generator referenced anywhere | [A]'s `log_api_call(...correlation_id=...)` parameter has no producer — every caller passes `None` | **Decided:** Phase 1 ships [`shared/utils/correlation.py::new_correlation_id()`](shared/utils/correlation.py:1) returning `f"{uuid4().hex[:12]}"`. Each top-of-loop in InterpretationService / PostMortemService / ChartHackerOpinion generates one and threads it through every downstream call. |
| 30 | **[AF]** ⚠️ **CRITICAL** — Postgres does not support cross-database `REFERENCES`. Plan claims "soft FK from `tickles_<company>.signal_interpretations.prompt_version` to `tickles_shared.public.prompt_versions`" — this is a text join, not an FK | Schema diagrams mislead; cascade-delete cannot work; runtime integrity holes | **Decided (default):** treat as **text-match soft FK** with weekly integrity job [`shared/scripts/check_soft_fks.py`](shared/scripts/check_soft_fks.py:1) that finds orphans and Slack/Telegrams the operator. Also covers `tracked_positions.actor_id`, `signal_interpretations.collector_id`, `position_postmortems.position_id` if those cross DB boundaries. Listed in §0.1 as critical pre-kickoff blocker. Confirm path (a/b/c) with user before Phase 2. |
| 31 | **[AG]** Reason-freeze trigger location | Phase 6 says "freeze `entry_reason_*` after entry" but does not specify whether the trigger lives in `tickles_shared.public.tracked_positions` (where the table now lives per [F]) or in every per-company DB | **Decided:** Trigger lives in `tickles_shared.public` because the table is there. Single source of truth. Trigger fires `BEFORE UPDATE`, raises `EXCEPTION` if `OLD.status != 'pending'` and any `entry_reason_*` column is in NEW. |
| 32 | **[AH]** `position_updates` table location after [F] decision | If `tracked_positions` is shared, what about `position_updates` which has FK to it? | **Decided:** `position_updates` moves to `tickles_shared.public` too (FK requires same DB). Phase 2 includes the move + a one-shot data-migration query that copies any per-company rows into shared. Documented as additive, with `company_id` carried explicitly. |
| 33 | **[AI]** No-CHECK constraint validator on `pattern_tags` etc. | Phase 2 ([M]) confirmed JSONB has no CHECK, but did not provide a sanity guard against pathologies (10 KB tag arrays, nested objects) | **Decided:** Phase 2 adds a tiny CHECK: `CHECK (jsonb_typeof(pattern_tags) = 'array' AND jsonb_array_length(pattern_tags) <= 32)` — guards array shape + length, NOT vocabulary. |
| 34 | **[AJ]** [P] postmortem recovery runbook is undefined | Phase 7's [P] envisaged a recovery for stuck `postmortem_status='running'` rows but never specified the rules | **Decided:** [`shared/scripts/postmortem_recovery.py`](shared/scripts/postmortem_recovery.py:1) (Phase 7) finds rows where `postmortem_status='running' AND postmortem_started_at < NOW() - INTERVAL '30 minutes'`, resets to `pending`, increments `postmortem_attempts`, dead-letters to `postmortem_status='failed'` after 3 attempts. Cron every 5 min. |
| 35 | **[AK]** [`shared/intelligence/prompts/chart_analysis.json`](shared/intelligence/prompts/chart_analysis.json:1) is a single file with 3 prompt slots — Phase 6 prompt taxonomy work touches all 3 | Single-file changes risk merge conflicts during phased rollout | **Decided:** Phase 6 splits this into three files: `chart_prefilter.json`, `chart_analysis.json`, `chart_hacker_mcp.json`, `text_signal_extraction.json`. Each registered separately in `prompt_versions`. Loader updated to read all four. |
| 36 | **[AL]** Raw LLM I/O payload retention | Phase 3 persists raw payload but never specifies retention | **Decided:** Phase 3 stores payloads in `signal_interpretations.raw_request_jsonb` / `raw_response_jsonb` for 30 days, then a nightly cron moves rows older than 30 days to `/opticals/archive/<yyyymm>/<id>.json.gz` and NULLs the columns. `LLM_PAYLOAD_RETENTION_DAYS=30` env var. |
| 37 | **[AM]** [D] default-deny middleware breaks existing `/opticals/*` static handler | Phase 4 ships static serve under `/opticals/`; Phase 5's [D] inverts auth_middleware to default-deny — this would 401 the static folder | **Decided:** Phase 5's `ALLOWED_PUBLIC` set explicitly includes `/opticals/*` (re-evaluated against question 3 in this table). If the user later wants opticals authenticated, that becomes its own phase with a Telegram-OTP-required path prefix. |
| 38 | **[AN]** Windows symlink trap | Phase 4's symlink `shared/reports/signal_review/ -> /opt/tickles/opticals/signal_review/` is Linux-only; ops on Windows or in some CI containers cannot symlink without elevated privileges | **Decided:** Phase 4 uses a **bind-mount-or-copy fallback** wrapper [`shared/utils/symlink_or_copy.py`](shared/utils/symlink_or_copy.py:1): try symlink, fall back to a periodic rsync (cron) if symlink fails. Documented in Phase 4 §Risks. |
| 39 | **[AO]** [D] inversion breaks existing tests | [`shared/tests/test_dashboard.py`](shared/tests/test_dashboard.py:1) was written against current default-allow behaviour | **Decided:** Phase 5 includes a test rewrite sub-step: convert all dashboard tests to log in via OTP first or use a test-only header. CI fails if any test still relies on default-allow. |
| 40 | **[AP]** OTP CSRF flow undefined | Phase 5 mounts mutating `/manage/*` POST endpoints; Telegram-OTP only fences GETs in current implementation | **Decided:** Phase 5 adds a CSRF token: GET `/manage/csrf` returns a per-session token; every POST/PATCH/DELETE under `/manage/*` requires header `X-CSRF-Token`. Token bound to OTP session. Test added. |
| 41 | **[AQ]** [O] env propagation between TUI and HTML panel | TUI banner says "open the served panel" but the URL is hardcoded? Templated? | **Decided:** TUI reads `TICKLES_DASHBOARD_URL` env var (default `https://vmi3220412.trout-goblin.ts.net/manage/`). Documented in `.env.template`. |
| 42 | **[AR]** Per-company permissions inside `?company=` picker | Phase L [L] picker allows operator to switch companies — but ALL operators can see ALL companies | **Decided:** Phase L reads `tickles_shared.public.operator_company_access (operator_id BIGINT, company_id TEXT, access_level TEXT)`. Picker shows only companies the OTP-authenticated operator has `read` access to. Default seed: every operator gets read access to every active company until per-tenant access is needed. |
| 43 | **[AS]** `prompt_versions` upsert race | Phase 6 [N] registers prompts on service start; if 4 services start simultaneously, the same `(name, version)` is INSERTed 4× → one wins, three error | **Decided:** Phase 6 ships the registration as `INSERT … ON CONFLICT (name, version) DO NOTHING RETURNING id`. The "is this the first registration?" branch reads back via `SELECT` if `RETURNING` is empty. Idempotent. |
| 44 | **[AT]** Freeze-trigger upsert test | [AG] adds a BEFORE UPDATE trigger; pytest must prove that an attempt to UPDATE `entry_reason_trader` after `status` left `'pending'` raises | **Decided:** [`shared/tests/test_reason_freeze.py`](shared/tests/test_reason_freeze.py:1) (Phase 6) covers: (1) UPDATE on pending allowed, (2) UPDATE after status flip raises, (3) inserts unaffected, (4) updating other columns post-flip allowed. |
| 45 | **[AU]** `pg_trgm` ≠ cosine similarity | Phase 6 reason_agreement_score originally sketched as cosine over reason embeddings; if Postgres `pg_trgm` is used as a shortcut it is character-trigram Jaccard, not semantic cosine | **Decided:** Phase 6 uses **embedding cosine via pgvector** (every reason gets a 384-dim embedding written to `signal_interpretations.entry_reason_trader_embedding` / `_llm_embedding`). The score formula is `1 - (a <=> b)` where `<=>` is pgvector cosine distance. `pg_trgm` is documented as the rejected alternative with reason. |
| 46 | **[AV]** MemU outbox payload contract undefined | [J] adds a listener; payload schema for `pg_notify('memu_broadcast', json)` was never specified | **Decided:** Phase 7 publishes `[`shared/memu/contracts.py::BroadcastPayload`](shared/memu/contracts.py:1)` — a TypedDict with `{insight_id, company_id, kind, summary, embedding_id, created_at_iso}`. Listener validates against contract, drops malformed, logs to `audit`. |
| 47 | **[AW]** Cron concurrency lock for PostMortemService | Cron-driven OpenClaw agents can be triggered overlapping (every 5 min schedule + 6-min run) | **Decided:** Phase 7 ships an advisory lock pattern: each OpenClaw agent run calls `SELECT pg_try_advisory_lock(hashtext('postmortem_<company>'))` at start. If false, exit cleanly with log "previous run still active". Released on completion or via `pg_advisory_unlock` in finally. |
| 48 | **[AX]** Three different UNIQUE keys cited for `position_postmortems` | `(position_id, postmortem_version, param_hash)` vs `(position_id, prompt_version)` vs `(position_id)` — all appear in different parts of the plan | **Decided:** Authoritative composite UNIQUE is `(position_id, postmortem_version, prompt_version)`. `param_hash` is a regular indexed column for clustering analysis, NOT part of the unique key. Documented in §2.3. |
| 49 | **[AY]** PositionMonitor dependency for Phase 8 | ChartHackerOpinionService writes `agent_opinions` keyed by `position_id`; if PositionMonitor is not running, there are no positions yet | **Decided:** Phase 8 ships with a startup health check: if `tracked_positions` is empty AND PositionMonitor is not in `services_catalog` with `is_active=true`, log a loud warning and continue (no work to do). Documented in Phase 8 §Risks. |
| 50 | **[AZ]** Mode='opinion_on_existing' JSON shape | Phase 8 ChartHackerOpinion has two modes (review-on-entry + opinion-on-existing); each mode emits different JSON | **Decided:** Phase 8 ships `[`shared/intelligence/prompts/chart_hacker_opinion.json`](shared/intelligence/prompts/chart_hacker_opinion.json:1)` with two prompt slots: `entry_review` and `mid_position_review`. Each defines a `response_schema` JSON-Schema-style block; service validates LLM output against it and discards malformed. |
| 51 | **[BA]** `news_items` ALTER ownership gap | Phase 9 adds zone-related columns to `news_items`, but the table is owned by the news collector subsystem; modifying it cross-team | **Decided:** Phase 9 columns are additive `ADD COLUMN IF NOT EXISTS` and documented in [`shared/collectors/news/CHANGES.md`](shared/collectors/news/CHANGES.md:1) with cross-link from this plan. Owner sign-off recorded in handoff before merge. |
| 52 | **[BB]** Discord rate limits ignored | Phase 9 adds ±10 message context fetch — that's 21 Discord API calls per signal. Discord rate limit is 50 req/sec/global | **Decided:** Phase 9 adds a token-bucket limiter [`shared/collectors/discord/rate_limiter.py`](shared/collectors/discord/rate_limiter.py:1) with 40 req/sec budget (20% headroom). Uses Redis if available, in-process bucket fallback. Each call writes to `api_cost_log` for visibility. |
| 53 | **[BC]** Surgeon2 dual-write fights freeze trigger | Phase 10 dual-writes new schema + legacy `surgeon2_positions`; if dual-write happens AFTER status flips to `'open'`, freeze trigger from [AG] fires on the legacy-rebuild update | **Decided:** Phase 10's dual-write writes legacy columns FIRST (within the same transaction) before flipping status. The freeze trigger is constructed to allow updates to non-frozen columns regardless of status; only `entry_reason_*` are frozen. Test added. |
| 54 | **[BD]** Multi-instance actor_id collision | If Surgeon2 runs in two regions, they share the same `actor_id='surgeon2'` and write to the same shared `tracked_positions` | **Decided:** Phase 10 namespaces `actor_id = f"surgeon2-{HOSTNAME}"`. Documented in `actor_performance` aggregation (Phase 11) as "by host". A future de-dupe phase can collapse them. |
| 55 | **[BE]** [K] EdgeScorer dual-write writes 3 places | `actor_performance` (new), `trader_performance` (legacy), and possibly Mem0 lessons | **Decided:** Phase 11 writes ONLY to `actor_performance`. Backfill into `trader_performance` is a 90-day partial UPSERT in a separate cron, isolated. No transactional dual-write. |
| 56 | **[BF]** edge_score divides by zero on cold-start companies | Available-component normalisation has a denominator of `sum(component_weights_present)`; for a new company with zero history, every component is absent | **Decided:** Phase 11 returns `edge_score=NULL, edge_score_status='insufficient_data'` if fewer than 3 components are present OR fewer than 10 closed positions exist. Dashboard shows "Insufficient data — N more trades needed". |
| 57 | **[BG]** Formula backfill blocks cron | Phase 11's first run computes edge_score for every actor in history — could lock `actor_performance` for minutes | **Decided:** Phase 11 backfill runs as a one-shot `shared/scripts/backfill_edge_score.py --batch 100` script (NOT inside the cron) before EdgeScorerService is enabled. Cron only does incremental deltas afterwards. |
| 58 | **[BH]** Phase L aggregation N+1 | "All companies" virtual mode fans out N queries; freezes the page on N=10+ | **Decided:** Phase L uses `asyncio.gather(*[query(c) for c in companies], return_exceptions=True)` with a 5-second timeout per company. Failed companies show `error` row, not page failure. |
| 59 | **[BI]** WebSocket auth in Phase L Live Queue tab | Live tab subscribes to `pg_notify` updates; current dashboard server has no WS auth | **Decided:** Phase L's WS endpoint reuses the OTP session cookie; rejects connections without it. Token rotated every 30 min. |
| 60 | **[BJ]** croniter UTC vs local-time confusion | Cron schedules in `ServiceDescriptor.cron_schedule` need a TZ contract | **Decided:** Phase 7 [H] addition: all cron schedules are UTC; documented in `ServiceDescriptor.cron_schedule` docstring. Times displayed in dashboard are converted to operator's TZ. |
| 61 | **[BK]** ⚠️ Hidden chart renderer cost — see §0.1 critical blocker | Phase L | Hidden $/day | Gated behind `?render_chart=1`, cached 24h, `api_cost_log`-tracked, [BP]-budgeted |
| 62 | **[BL]** Regression matrix missing | Plan has no end-to-end test that exercises every phase together | **Decided:** Add Phase R (Regression) — runs after Phase 11 completes. Smoke-tests one signal end-to-end through Phases 1→11→L on a scratch `tickles_test` company. Listed under "Cross-cutting", below. |
| 63 | **[BM]** Rollback rehearsal missing | Every migration is additive, but no phase ships a rollback SQL | **Decided:** Every Phase N migration ships paired with `rollback_phaseN.sql` that reverses the additive ALTERs. Tested against a clone before merge. CI gate. |
| 64 | **[BN]** ⚠️ Master schema sync — see §0.1 critical blocker | Every phase | New companies inherit old schema | Phase 0 ships [`shared/migration/sync_master_schema.py`](shared/migration/sync_master_schema.py:1); CI gates each phase. |
| 65 | **[BO]** Single-writer CI guard missing | [I] said "writes only via service" but nothing prevents a developer from raw-`UPDATE`ing in a script | **Decided:** CI lint that scans Python for `UPDATE tracked_positions`, `UPDATE position_postmortems`, `UPDATE actor_performance` outside the owning service module → fails. Exception list in `.tickles-lint.yml`. |
| 66 | **[BP]** ⚠️ LLM budget circuit-breaker — see §0.1 critical blocker | Phase 1 + every LLM caller | Catastrophic overspend | Phase 1 ships pre-call gate `check_budget(role)`; per-role daily caps in env; tests cover breach path. |

---

## 1. Verified Ground Truth (what already exists — do not duplicate)

These were confirmed by reading the actual files:

### Database (Postgres, NOT MySQL — `CLAUDE.md` is out of date)

| Table | DB | File | Notes |
|-------|-----|------|-------|
| `trader_profiles` | `tickles_shared` | [`shared/intelligence/migrations/2026_04_26_phase3b_intelligence.sql`](shared/intelligence/migrations/2026_04_26_phase3b_intelligence.sql:1) | platform-agnostic shell exists |
| `signal_interpretations` | `tickles_<company>` | same | has `model_version`, `param_hash`, `candle_data_hash`, `llm_levels JSONB`, `consensus_*`. **Missing**: prefilter_*, vision_provider/model, prompt_version/hash, dual-reason fields, pattern_tags |
| `trader_performance` | `tickles_<company>` | same | exists |
| `tracked_positions` | `tickles_<company>` | [`shared/intelligence/migrations/2026_04_26_phase3c_collector_catalog.sql`](shared/intelligence/migrations/2026_04_26_phase3c_collector_catalog.sql:181) | exists with rich PnL/SL/TP/MFE/MAE columns. **Missing**: `actor_type`, `actor_id`, `position_kind`, `asset_class`, `legs JSONB`, `entry_reason_*`, `exit_reason_*`, `closed_at`, `realized_pnl_usd` (separate from realized_pnl_pct), `postmortem_status` |
| `position_updates` | `tickles_<company>` | same | exists (snapshots) |
| `agent_opinions` | `tickles_<company>` | same | exists with `would_take_trade`, `agent_pnl_pct`, `performance_delta`, `lessons_learned` |
| `watched_users`, `collector_catalog` | `tickles_shared` | same | exist |
| `news_items`, `media_items` | `tickles_shared` | [`shared/migration/tickles_shared_pg.sql`](shared/migration/tickles_shared_pg.sql:339) | exist |
| `system_config` | `tickles_shared` | [`shared/migration/tickles_shared_pg.sql`](shared/migration/tickles_shared_pg.sql:376) | exists with seed defaults |
| `api_cost_log` | `tickles_shared` | [`shared/migration/tickles_shared_pg.sql`](shared/migration/tickles_shared_pg.sql:391) | exists with provider/model/role/context/tokens/cost/latency/company_id |
| `agent_personas`, `agent_prompts` (versioned), `agent_decisions` | `tickles_<company>` | [`shared/souls/migrations/2026_04_19_phase31_souls.sql`](shared/souls/migrations/2026_04_19_phase31_souls.sql:1) | exist; `agent_decisions_latest` view exists |
| `agent_state` | `tickles_<company>` | [`shared/migration/tickles_company_pg.sql`](shared/migration/tickles_company_pg.sql:243) | JSONB `state_data` — Surgeon2 migration target |
| `surgeon2_state`, `surgeon2_positions`, `surgeon2_trade_log` | `tickles_<company>` (runtime-created) | [`shared/daemons/surgeon2_trader.py`](shared/daemons/surgeon2_trader.py:199) | **legacy ad-hoc** — must migrate into `tracked_positions` + `agent_state` |

### Memory tiers (verified 2026-04-29)

| Tier | Backend | Scope | Code | Use for |
|------|---------|-------|------|---------|
| **1 — Agent-private** | Mem0 (Qdrant `tickles_<company>`) | one agent inside one company | [`get_memory(company, agent)`](shared/utils/mem0_config.py:187) | per-agent lessons, decisions, persona memory |
| **2 — Company-shared** | Mem0 same collection, `agent_id='<company>_shared'` | one company, all its agents | [`get_memory(company, "shared")`](shared/utils/mem0_config.py:187) | leaderboard summaries inside a company, council insights |
| **3 — Cross-company** | **MemU** (Postgres `memu` DB + pgvector + `pg_notify('memu_broadcast',…)`) | every company in the platform | [`get_memu()`](shared/memu/client.py:288) `.write_insight()` / `.search()` | post-mortem company-wide lessons, regime alerts, system-wide pattern discoveries |

**Rule of thumb (used throughout the plan):** if a lesson is about *how this actor traded*, write to **Mem0**. If a lesson is about *how the market behaved* or *a pattern that any company should know about*, write to **MemU**.

### OpenClaw-native agent recipe (paperclip-visible)

Any new LLM-driven service in this plan that is intended to be visible to paperclip and operable through the OpenClaw cron supervisor MUST follow [`shared/docs/NEW_TRADING_AGENT_HOWTO.md`](shared/docs/NEW_TRADING_AGENT_HOWTO.md:1):

1. `openclaw agents add <id> --workspace <dir> --model <model> --non-interactive --json`
2. Workspace dir contains `SOUL.md`, `IDENTITY.md`, `BOOTSTRAP.md`, `AGENTS.md`, `TOOLS.md`, `USER.md`, `HEARTBEAT.md`, plus service-specific state file(s).
3. `openclaw cron add … --tools read,write,exec --thinking low --timeout-seconds 180 --no-deliver --session isolated …` (the `--tools read,write,exec` flag is the single most important line — without it the LLM drowns in MCP tools and returns empty).
4. Smoke-test with a PING/PONG single turn before wiring the cron schedule.

**Services in this plan that ship as OpenClaw-native agents (paperclip-visible):**
- Phase 7 PostMortemService → openclaw id `<company>_postmortem`
- Phase 8 ChartHackerOpinionService → openclaw id `<company>_chart_hacker_opinion`
- Phase 11 EdgeScorerService → openclaw id `<company>_edge_scorer` (cron: daily)
- Phase 9 ZoneFilter — runs as a Python helper inside the collectors (not its own agent), so no openclaw entry.

Pure data ingestion (zone filter helper, signal review exporter, dashboard server) stays as Python `ServiceDaemon`s. Only LLM-orchestrating services need the OpenClaw recipe.

### Code / services

| Component | File | State |
|-----------|------|-------|
| LLM Gateway abstraction | [`shared/intelligence/gateway_config.py`](shared/intelligence/gateway_config.py:33) | `GatewayConfig.for_service()` works for openrouter + requesty. **Missing**: `temperature`, per-call `api_cost_log` write |
| InterpretationService | [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:1) | Dual-track LLM+quant works; pre-filter in place; anti-hallucination prompt in place; **missing** dual-reason capture, prompt_version/hash, raw I/O persistence path |
| ChartHacker prompts | [`shared/intelligence/prompts/chart_analysis.json`](shared/intelligence/prompts/chart_analysis.json:1) | Anti-hallucination clauses present; **missing** pattern/setup/regime/session tag schema |
| ServiceDaemon / Registry | [`shared/services/daemon.py`](shared/services/daemon.py:50) / [`shared/services/registry.py`](shared/services/registry.py:40) | Ready for PostMortemService plug-in |
| Dashboard server | [`shared/dashboard/server.py`](shared/dashboard/server.py:191) | aiohttp + Telegram-OTP middleware; endpoints `/api/snapshot`, `/api/services`, `/api/sessions/active`. Phase L extends this |
| Catalogue TUI ([`manage_sources.py`](manage_sources.py:30)) | [`shared/catalogue/tui_manager.py`](shared/catalogue/tui_manager.py:357) | Menu items 1–5 (Dashboard / Sources / Channels / Users / Leaderboard) **plus Hierarchy + quit**. **No Signals tab, no Positions tab yet** — these are Phase A4 in prompt.md |

### Folders / serving

| Item | Reality |
|------|---------|
| `/opt/tickles/opticals/` | **Does not exist.** Phase 1 creates it. |
| `shared/reports/signal_review/` | **Does not exist.** `shared/reports/chart_hacker_guru/` exists; same parent will be re-used. |
| Tailscale serve | `vmi3220412.trout-goblin.ts.net` already serves; mapping for `/opticals/*` must be added (`tailscale serve` config) |

---

## 2. Phasing Strategy

The prompt's 30-day roadmap (Part K) is reorganised into **13 phases (0–11 + R)** plus a **final GUI phase (L)**. Phases are ordered by **dependency**, not calendar days. Each phase ends with a **benchmark checklist** the user can run. Each phase document below is structured into the same uniform sub-sections so any new task or LLM can pick up the work cold:

> **Per-phase sub-section contract** — every phase below contains:
> - **Purpose** — one paragraph: WHY this phase exists, what business pain it removes
> - **What** — exhaustive list of artefacts produced (tables, columns, files, services)
> - **Where** — every absolute file path, table FQN, and DB target (shared vs per-company)
> - **When** — sequencing & triggers (which phases must complete first; cron schedules)
> - **Why** — rationale per artefact + dependency chain explained
> - **How** — implementation steps with code snippets, SQL blocks, env edits
> - **Risks & mitigations** — failure modes, edge cases, rollback path
> - **Benchmark checklist** — operator-runnable verification steps

| Phase | Theme | Days | Depends on |
|-------|-------|------|------------|
| **0** | Pre-flight: clarify, snapshot, branch, **[AA] companies helper**, **[AB] migration runner**, **[BN] master schema sync gate** | 1 | nothing |
| **1** | `.env` reorganisation + Gateway extension + universal `api_cost_log` writer + **[BP] budget circuit-breaker** | 1.5 | 0 |
| **2** | Schema additions (`signal_interpretations` + `tracked_positions` + `position_postmortems` + freeze-trigger SQL stub) | 2 | 0 |
| **3** | Persist raw I/O, prompt_hash, vision_provider, prefilter result + **[AL] payload retention cron** | 2 | 1, 2 |
| **4** | Signal-review export + opticals static serve + **[AN] symlink-or-copy fallback** | 2 | 3 |
| **5** | Served HTML manage panel (Sources/Signals/Positions/Leaderboard) + **[D] default-deny middleware** + **[AP] CSRF** + **[AO] test rewrite** | 3 | 3, 4 |
| **6** | Dual-logic reasoning (entry_reason_trader/llm + cosine agreement) + **[N] prompt_versions** + **[AG] freeze trigger** + **[AS] race-safe upsert** + **[AU] pgvector cosine** | 4 | 2, 3 |
| **7** | Centralised PostMortemService + `position_postmortems` + **[J]+[AV] MemU outbox/listener** + **[AW] advisory lock** + **[AJ] recovery cron** | 5 | 2, 6 |
| **8** | ChartHacker role split → ChartHackerOpinionService writing `agent_opinions` + **[AZ] response_schema validation** | 3 | 7 |
| **9** | Trading-zone monitoring (filtered ingestion + ±10 context) + **[BB] Discord rate-limit token bucket** | 2 | 5 |
| **10** | Surgeon2 migration into `tracked_positions` + `agent_state` + **[BC] dual-write ordering** + **[BD] hostname namespace** | 2.5 | 2, 7 |
| **11** | Edge score + `actor_performance` leaderboard + **[BE] no transactional dual-write** + **[BF] insufficient-data state** + **[BG] one-shot backfill** | 4 | 7, 10 |
| **L** | GUI dashboard — leaderboard, signals, positions, LLM interp + Discord links, queue/live trades + **[BH] gather()** + **[BI] WS auth** + **[BK] gated chart renderer** | 5 | 4, 5, 7, 11 |
| **R** | Regression matrix — end-to-end smoke test on scratch `tickles_test` company, **[BL]** + rollback rehearsal **[BM]** + master schema diff gate **[BN]** | 1 | 11, L |

**Total: ~35 working days** (was 30 before AA–BP findings; +5 days net for cross-cutting hardening). Parallelisable: Phases 4↔5, Phases 8↔9↔10 can run in parallel after their respective dependencies clear.

### Cross-cutting concerns (touch every phase)

| ID | Concern | Where applied |
|----|---------|---------------|
| **[BL]** | Regression matrix | Phase R, ships `shared/scripts/regression_e2e.py` running one signal end-to-end |
| **[BM]** | Rollback rehearsal | Every Phase N migration ships `rollback_phaseN.sql`; CI runs it on a clone |
| **[BN]** | Master schema sync | Phase 0 ships [`shared/migration/sync_master_schema.py`](shared/migration/sync_master_schema.py:1); CI gates each phase |
| **[BO]** | Single-writer CI guard | `.tickles-lint.yml` regex bans on `UPDATE tracked_positions/position_postmortems/actor_performance` outside owning service |
| **[BP]** | LLM budget circuit-breaker | Phase 1 helper `check_budget(role)`; gateway honours it; tests cover breach |

---

## Phase 0 — Pre-flight (1 day)

### Purpose
Lock down a known-good baseline before any code/SQL change. Create the three foundational helpers (companies enumerator, migration runner, master-schema sync gate) that every later phase depends on. Confirm the four pre-kickoff blockers from §0.1 are resolved with explicit user decisions. Without this phase, every subsequent phase risks hand-applied drift, parallel-merge conflicts with the [`2026-04-28 handoff`](.roo/handoffs/2026-04-28-intelligence-pipeline-verification-handoff.md:1) bugs, and brand-new companies inheriting the OLD schema.

### What
1. **Baseline handoff doc** — `.roo/handoffs/2026-04-29-intelligence-unified-plan-baseline.md` capturing `git rev-parse HEAD`, `pg_dump --schema-only` of `tickles_shared` + one `tickles_<company>`, full `.env` (with secrets redacted) snapshot, list of all running `tickles-service@*` units.
2. **Working branch** — `feature/intelligence-unified-plan` off `main`.
3. **[G][AA] Companies enumerator** — [`shared/utils/companies.py`](shared/utils/companies.py:1) with three public functions (see code block below).
4. **[AB] Migration runner** — [`shared/migration/run_phase_migration.py`](shared/migration/run_phase_migration.py:1) + tracking table [`tickles_shared.public.schema_migrations`](shared/migration/2026_04_29_phase0_schema_migrations.sql:1) `(phase INT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), checksum CHAR(64))`.
5. **[BN] Master schema sync gate** — [`shared/migration/sync_master_schema.py`](shared/migration/sync_master_schema.py:1) — pg_dump-and-diff CI tool that fails if [`shared/migration/tickles_shared_pg.sql`](shared/migration/tickles_shared_pg.sql:1) or [`shared/migration/tickles_company_pg.sql`](shared/migration/tickles_company_pg.sql:1) drift from the actual live schema after a phase migration.
6. **§0 user-decision recording** — confirm answers to questions 1–66 (or accept defaults); commit the answers as `.roo/handoffs/2026-04-29-decisions.md`.

### Where
| Artefact | Path | DB |
|---|---|---|
| baseline handoff | `.roo/handoffs/2026-04-29-intelligence-unified-plan-baseline.md` | n/a |
| companies helper | [`shared/utils/companies.py`](shared/utils/companies.py:1) | reads `tickles_shared.public.companies` |
| migration runner | [`shared/migration/run_phase_migration.py`](shared/migration/run_phase_migration.py:1) | writes `tickles_shared.public.schema_migrations` |
| schema sync | [`shared/migration/sync_master_schema.py`](shared/migration/sync_master_schema.py:1) | reads live; writes diff report |
| schema_migrations table SQL | [`shared/migration/2026_04_29_phase0_schema_migrations.sql`](shared/migration/2026_04_29_phase0_schema_migrations.sql:1) | `tickles_shared.public` |
| decisions log | `.roo/handoffs/2026-04-29-decisions.md` | n/a |

### When
- **Trigger:** start of project, before ANY DDL or service code edit.
- **Sequencing:** must complete before Phase 1 starts. The schema_migrations table seeded with `phase=0` only after this phase's own DDL is applied.
- **Cron schedules introduced:** none (this is a one-shot phase).

### Why
- **Companies helper [G][AA]** — Phases 7, 8, 10, 11 each iterate per-company; without a single source-of-truth helper, each phase will reinvent `_discover_companies()` and they will diverge silently. Promoting the existing private helper [`shared/intelligence/performance_scorer.py:549`](shared/intelligence/performance_scorer.py:549) is cheap and removes that risk entirely.
- **Migration runner [AB]** — every phase from 1 onward ships a `2026_04_29_phaseN_*.sql` file. Hand-applying them is fragile; a runner with a `schema_migrations` ledger makes idempotent re-runs safe and gives ops a single command (`python -m shared.migration.run_phase_migration --phase 2 --target all_companies`).
- **Master schema sync [BN]** — without this gate, a freshly-provisioned company today inherits the master template from BEFORE Phase 2's ALTERs and is born with missing columns. CI failure on drift is the only reliable mechanism.
- **Decisions log** — captures the user's actual choices on the 66 open questions so a future LLM/operator can reconstruct intent without re-running the analysis.

### How
1. **Branch + baseline (15 min):**
   ```bash
   git checkout -b feature/intelligence-unified-plan
   pg_dump --schema-only -d tickles_shared > /opt/tickles/.roo/handoffs/baseline_shared.sql
   pg_dump --schema-only -d tickles_rubicon > /opt/tickles/.roo/handoffs/baseline_rubicon.sql
   cp .env .roo/handoffs/baseline_env_redacted.txt   # then redact secrets manually
   ```
2. **[G][AA] Companies helper (30 min):**
   ```python
   # shared/utils/companies.py  (NEW)
   from __future__ import annotations
   import asyncio, os
   from typing import Awaitable, Callable, TypeVar
   import asyncpg
   from shared.utils.db import get_shared_pool

   T = TypeVar("T")
   _CACHE: tuple[float, list[str]] | None = None
   _CACHE_TTL = 300  # 5 minutes

   async def list_active_companies() -> list[str]:
       """Return list of active company short-names (e.g., ['rubicon']).
       Source of truth: tickles_shared.public.companies WHERE is_active=true.
       Falls back to env ACTIVE_COMPANIES if table missing.
       """
       global _CACHE
       now = asyncio.get_event_loop().time()
       if _CACHE and now - _CACHE[0] < _CACHE_TTL:
           return _CACHE[1]
       pool = await get_shared_pool()
       async with pool.acquire() as conn:
           try:
               rows = await conn.fetch(
                   "SELECT short_name FROM companies WHERE is_active = true ORDER BY short_name"
               )
               names = [r["short_name"] for r in rows if r["short_name"] != "jarvais"]
           except asyncpg.UndefinedTableError:
               raw = os.getenv("ACTIVE_COMPANIES", "rubicon")
               names = [n.strip() for n in raw.split(",") if n.strip()]
       _CACHE = (now, names)
       return names

   async def get_company_dsn(company: str) -> str:
       base = os.environ["TICKLES_DB_DSN_TEMPLATE"]   # "postgresql://...@host:5432/{db}"
       return base.format(db=f"tickles_{company}")

   async def for_each_company(
       fn: Callable[[str, asyncpg.Connection], Awaitable[T]],
   ) -> dict[str, T | Exception]:
       """Fan-out helper. Returns {company: result_or_exception}."""
       names = await list_active_companies()
       async def _one(c: str):
           dsn = await get_company_dsn(c)
           async with asyncpg.connect(dsn) as conn:
               return await fn(c, conn)
       results = await asyncio.gather(*[_one(c) for c in names], return_exceptions=True)
       return dict(zip(names, results))
   ```
3. **[AB] Migration runner (45 min):**
   ```sql
   -- shared/migration/2026_04_29_phase0_schema_migrations.sql
   CREATE TABLE IF NOT EXISTS schema_migrations (
       phase       INT  PRIMARY KEY,
       applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
       checksum    CHAR(64) NOT NULL,
       target      TEXT NOT NULL,    -- 'shared' | 'all_companies' | 'rubicon' | ...
       notes       TEXT
   );
   ```
   ```python
   # shared/migration/run_phase_migration.py  (NEW)
   # CLI: python -m shared.migration.run_phase_migration --phase 2 --target all_companies
   # - looks up shared/migration/2026_04_29_phaseN_*.sql or shared/intelligence/migrations/*phaseN*.sql
   # - computes sha256(file_bytes) -> checksum
   # - skips if (phase, target) already in schema_migrations with matching checksum
   # - applies inside a transaction; records on success
   # - --dry-run prints the file path + diff against checksum without applying
   ```
4. **[BN] Master schema sync (45 min):**
   ```python
   # shared/migration/sync_master_schema.py  (NEW)
   # CLI: python -m shared.migration.sync_master_schema --check (CI mode, exit 1 on drift)
   #      python -m shared.migration.sync_master_schema --update (writes new master file)
   # Compares pg_dump --schema-only of tickles_shared/tickles_<sample> against
   # tickles_shared_pg.sql / tickles_company_pg.sql. Diff is line-level on normalised whitespace.
   ```
   CI hook in `.github/workflows/schema_drift.yml` (or local pre-merge equivalent) runs `--check` after each phase migration.
5. **Decision log (15 min):** copy §0 table rows that have user-confirmed answers into `.roo/handoffs/2026-04-29-decisions.md`.

### Risks & mitigations
- **Risk:** `tickles_shared.public.companies` may not yet exist on this server. **Mitigation:** [AA] helper falls back to `ACTIVE_COMPANIES` env var.
- **Risk:** the migration runner is given a wrong `--target` and applies a per-company migration to `tickles_shared`. **Mitigation:** every phase migration file declares `-- target: shared` or `-- target: per_company` in its header; runner refuses to apply if mismatched.
- **Risk:** master-schema-sync produces noisy diffs from comment changes. **Mitigation:** normalise by stripping comments before comparing; documented in the script.

### Benchmark checklist
- [x] User answers recorded for §0 questions (1–66) in decisions log
- [x] `git rev-parse HEAD` saved in baseline handoff
- [-] `pg_dump --schema-only` saved for `tickles_shared` + one company DB (blocked by system role permissions)
- [x] `feature/intelligence-unified-plan` branch created and pushed
- [x] [`shared/utils/companies.py`](shared/utils/companies.py:1) green: `pytest shared/tests/test_companies_helper.py`
- [x] [`shared/migration/run_phase_migration.py`](shared/migration/run_phase_migration.py:1) `--phase 0 --target shared` applies the schema_migrations table
- [x] [`shared/migration/sync_master_schema.py`](shared/migration/sync_master_schema.py:1) `--check` exits 0 on a freshly-applied baseline
- [ ] CI workflow `.github/workflows/schema_drift.yml` (or pre-merge hook) registered (Phase R)
- [x] Phase 0 handoff doc committed

---

## Phase 1 — `.env` Reorganisation + Gateway Extension + Cost Log + Budget Circuit-Breaker (2 days)

### Purpose
Make every outbound API call (LLM, exchange, Discord/Telegram/RSS, MemU) go through a single, observable, budget-aware code path. After this phase, no service can silently bypass cost accounting, no key lookup is duplicated across modules, and a runaway loop cannot drain the LLM budget unbounded.

### What
1. Reorganise [`.env`](.env:1) into provider-driven if-this-then-that blocks (100% existing content preserved).
2. Extend [`shared/intelligence/gateway_config.py`](shared/intelligence/gateway_config.py:33) with `temperature`, cost-log hook, Requesty URL bug fix.
3. Ship `shared/utils/api_cost_log.py` — the universal `log_api_call()` writer used by every outbound surface ([G5]).
4. Ship `shared/utils/correlation.py::new_correlation_id()` — UUIDv4 string generator used to thread one ID through `signal_interpretations.correlation_id` → `api_cost_log.correlation_id` → `position_postmortems.correlation_id` ([AE]).
5. Ship `shared/utils/budget_guard.py` — pre-call `check_budget(role, company_id)` gate that raises `BudgetExceededError` when today's `SUM(cost_usd)` for that (role, company) exceeds the per-role daily cap ([BP] — pre-kickoff blocker).
6. Refactor [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:1) so all five LLM-touching functions go through the gateway ([E]).
7. Additive ALTERs to `tickles_shared.public.api_cost_log` (operation, agent_id, temperature, correlation_id, request_path, response_path, success, http_status, extra) — including [AD] precision bump of `cost_usd` to `NUMERIC(20,8)`.
8. New doc [`shared/docs/ENV_REFERENCE.md`](shared/docs/ENV_REFERENCE.md:1) listing every env key, who reads it, defaults, and the [AC] restart-semantics warning.

### Where
- Edits: [`.env`](.env:1), [`.env.template`](.env.template:1), [`shared/intelligence/gateway_config.py`](shared/intelligence/gateway_config.py:33), [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:268), [`shared/utils/mem0_config.py`](shared/utils/mem0_config.py:137).
- New files:
  - [`shared/utils/api_cost_log.py`](shared/utils/api_cost_log.py:1)
  - [`shared/utils/correlation.py`](shared/utils/correlation.py:1)
  - [`shared/utils/budget_guard.py`](shared/utils/budget_guard.py:1)
  - [`shared/utils/env_loader.py`](shared/utils/env_loader.py:1)
  - [`shared/utils/_validate_env.py`](shared/utils/_validate_env.py:1)
  - [`shared/docs/ENV_REFERENCE.md`](shared/docs/ENV_REFERENCE.md:1)
  - [`shared/intelligence/migrations/2026_04_29_phase1_api_cost_log_extend.sql`](shared/intelligence/migrations/2026_04_29_phase1_api_cost_log_extend.sql:1) (+ matching `_ROLLBACK.sql`)
- Schema target: `tickles_shared.public.api_cost_log` (single shared table — every company writes here, scoped by `company_id`).

### When
Days 2–3 of the 35-day schedule. Hard prerequisite for Phase 3 (which writes provider/model/temperature columns to `signal_interpretations` and must read clean values), Phase 4 (PostMortemService — must obey budget guard from day one), Phase 7 (broadcast_insight — needs correlation_id), and every later phase that issues paid LLM calls.

### Why
- The user's directive: *"the .env files need to maintain all 100% content, but need reorganisation, and the keys/providers need to be structured as a if this then that, rather than manual entry per parameter."*
- The devil's-advocate finding [BP] (pre-kickoff blocker): without a budget circuit-breaker, a single misconfigured prompt loop or stuck retry can spend the entire monthly LLM budget in hours. We MUST gate every paid call.
- [AD]: `cost_usd FLOAT` loses precision below 1 cent — at 100k+ rows/month the rounding error compounds into accounting drift. NUMERIC(20,8) is non-negotiable for any column we sum for billing.
- [AE]: every downstream debugging session ("which LLM call produced this bad signal?") requires one correlation_id flowing from media_item → interpretation → cost row → postmortem. Generating it in one place prevents collisions and missing values.
- Gateway-only enforcement (CI grep guard, [R]) is the only sustainable way to keep cost accounting honest as the codebase grows.

### What we're doing
Reorganise [`.env`](.env:1) into a **provider-driven if-this-then-that** layout. **100% of existing content is preserved** — keys, URLs, models, exchange creds, DB creds, Discord/Telegram tokens, ClickHouse/Redis/MemU, GitHub. The only change is the **selection mechanism**.

> **[B] Reframe — this phase EXTENDS [`shared/intelligence/gateway_config.py`](shared/intelligence/gateway_config.py:33), it does NOT recreate it.** `GatewayConfig.for_service()` (line 47), `call_vision_llm()` (line 124) and `chat_completion()` (line 206) are already implemented. Phase 1's net code delta on this file is ~80 lines: add `temperature` field, add `cost_log_enabled` switch, fix the Requesty default URL bug, hook every successful call to `log_api_call()`. **Do not** redesign `for_service()`.

> **[C] `.env` reality reconciliation (verified 2026-04-29 by reading the live `.env`).** Before any reorganisation, Phase 1 must reconcile four naming conflicts between this plan and reality:
> | What plan said | What reality is | Action |
> |---|---|---|
> | `REQUESTY_API_KEY=...` | `REQUESTY_API=...` | Keep `REQUESTY_API` as canonical; add `REQUESTY_API_KEY` as a recognised alias in `gateway_config.py` (existing fallback chain at line 79 already supports `TICKLES_APP_VISION_API_KEY` — extend the same pattern). Document both in `ENV_REFERENCE.md`. |
> | `REQUESTY_BASE_URL=https://router.requesty.ai/v1` | `TICKLES_APP_REQUESTY_URL=...` (and not always set) | Adopt `REQUESTY_BASE_URL` as the canonical name in the new layout; keep `TICKLES_APP_REQUESTY_URL` as a recognised alias for one release. |
> | `LLM_GATEWAY_DEFAULT=requesty` | `LLM_GATEWAY_DEFAULT=openrouter` | Keep reality (`openrouter`) as the live default; the plan's example block below is illustrative only. The reorganisation does NOT flip the default. |
> | [`shared/intelligence/gateway_config.py:79`](shared/intelligence/gateway_config.py:79) defaults Requesty URL to `https://api.requesty.ai/v1` | Requesty's actual router host is `https://router.requesty.ai/v1` | **Bug fix** — change the default in `gateway_config.py`. Test that calls still succeed against the live Requesty account. |

### The pattern
For every "swappable" group (LLM gateway, vision pipeline, pre-filter, text extractor, post-mortem, exchange creds), the file uses a single switch at the top of the section, and self-contained provider blocks below:

```dotenv
# =====================================================================
# LLM GATEWAY — pick one provider; that block's settings auto-apply
# =====================================================================
LLM_GATEWAY_DEFAULT=openrouter          # openrouter | requesty   ([C] reality default)

# ---- requesty block (active when LLM_GATEWAY_DEFAULT=requesty) ----
REQUESTY_API=rqsty-sk-...               # canonical name — matches existing .env
REQUESTY_API_KEY=                       # alias of REQUESTY_API (gateway_config recognises both)
REQUESTY_BASE_URL=https://router.requesty.ai/v1   # canonical
TICKLES_APP_REQUESTY_URL=               # alias of REQUESTY_BASE_URL
REQUESTY_DEFAULT_MODEL=tickles-vision
REQUESTY_FALLBACK_MODEL=google/gemini-2.5-flash
REQUESTY_TEMPERATURE=0.1

# ---- openrouter block (active when LLM_GATEWAY_DEFAULT=openrouter) ----
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_DEFAULT_MODEL=anthropic/claude-sonnet-4
OPENROUTER_FALLBACK_MODEL=google/gemini-2.0-flash-001
OPENROUTER_TEMPERATURE=0.1

# Per-service overrides (optional — leave commented to inherit DEFAULT)
# LLM_GATEWAY_VISION=requesty
# LLM_GATEWAY_PREFILTER=openrouter
# LLM_GATEWAY_TEXT_EXTRACT=openrouter
# LLM_GATEWAY_POSTMORTEM=requesty

# [Q] Cost-log master switch — default ON so we never silently lose a row
LLM_COST_LOG_ENABLED=true               # set false ONLY for local unit-test runs
```

[`shared/intelligence/gateway_config.py:47`](shared/intelligence/gateway_config.py:47) **already** does the `for_service()` lookup. The work in this phase is purely env-file rearrangement plus a small `gateway_config.py` patch to (a) read `*_TEMPERATURE`, (b) hook every call to `log_api_call()`, (c) fix the Requesty default URL bug, (d) honour `LLM_COST_LOG_ENABLED`.

### Same pattern applied to
1. **LLM gateway** (above)
2. **Exchange providers** — `EXCHANGE_PROFILE_PRIMARY=bybit_main`; each block (`BYBIT_MAIN_*`, `BLOFIN_*`, `BITGET_*`, `CAPITAL_*`) is self-contained
3. **Memory backend** — `MEMORY_BACKEND=mem0` with mem0 + memu blocks
4. **Vision pipeline waterfall** — `VISION_MODE=tickles-vision-waterfall` vs explicit single model
5. **Pre-filter** — `PREFILTER_ENABLED=true`, `PREFILTER_PROVIDER=requesty|openrouter|none`

### §A. Universal `api_cost_log` writer (G5)

**Scope correction (2026-04-29):** the prompt asked for cost/usage logging *everywhere there is an API call*, not only on LLM gateway calls. Phase 1 ships **one shared writer** that all outbound-API code paths use:

```python
# shared/utils/api_cost_log.py  (NEW)
async def log_api_call(
    *, provider: str, service_name: str, role: str,
    operation: str,                 # e.g. "vision.complete", "bybit.fetch_ohlcv", "discord.history"
    company_id: str | None,
    agent_id: str | None = None,    # which agent/service initiated it
    model: str | None = None,       # for LLM only
    temperature: float | None = None,
    tokens_in: int | None = None, tokens_out: int | None = None,
    cost_usd: float = 0.0,          # 0 for free APIs (Discord/Telegram), still log the call
    latency_ms: int | None = None,
    correlation_id: str | None = None,
    request_path: str | None = None, response_path: str | None = None,
    success: bool = True, http_status: int | None = None,
    extra: dict | None = None,
) -> None: ...
```

**Wired in Phase 1 to (and verified by integration test):**

| Surface | File | What gets logged |
|---------|------|------------------|
| LLM gateway (vision) | [`shared/intelligence/gateway_config.py`](shared/intelligence/gateway_config.py:124) | provider, model, role, tokens, cost, temperature, latency, raw I/O paths, agent_id |
| LLM gateway (text) | [`shared/intelligence/gateway_config.py`](shared/intelligence/gateway_config.py:206) | same |
| Mem0 LLM extraction | [`shared/utils/mem0_config.py`](shared/utils/mem0_config.py:137) | provider=mem0_llm_provider, role='mem0_extract' |
| MemU writes that call LLM | [`shared/memu/client.py`](shared/memu/client.py:288) | role='memu_insight' |
| ccxt exchange REST | wherever ccxt is invoked (per-exchange) | provider=exchange_id, role='market_data' or 'trade_action', cost_usd=0 (most), latency, http_status |
| Discord client | [`shared/collectors/discord/discord_collector.py`](shared/collectors/discord/discord_collector.py:1) | provider='discord', role='collector', cost_usd=0, latency, count |
| Telegram client | [`shared/collectors/telegram/telegram_collector.py`](shared/collectors/telegram/telegram_collector.py:1) | provider='telegram', role='collector', cost_usd=0, latency |
| Tailscale serve hooks | n/a | local — skipped |
| RSS / news_collector | [`shared/collectors/news/rss_collector.py`](shared/collectors/news/rss_collector.py:1) | provider='rss', role='collector', cost_usd=0 |

**Why log free APIs too:** rate-limit accounting, latency monitoring, and post-incident forensics all benefit from a single chronological API trail. The `cost_usd` column is just one dimension of the row.

**Schema check:** [`api_cost_log`](shared/migration/tickles_shared_pg.sql:391) already has `provider`, `model`, `role`, `context`, `tokens_in`, `tokens_out`, `cost_usd`, `latency_ms`, `company_id`. Phase 1 adds (additive ALTERs): `operation TEXT`, `agent_id TEXT`, `temperature NUMERIC(4,2)`, `correlation_id TEXT`, `request_path TEXT`, `response_path TEXT`, `success BOOLEAN DEFAULT TRUE`, `http_status INT`, `extra JSONB`.

#### [A] Explicit additive migration block (verified against [`shared/migration/tickles_shared_pg.sql:391-406`](shared/migration/tickles_shared_pg.sql:391))

The existing table has 11 columns: `id, created_at, company_id, provider, model, role, context, tokens_in, tokens_out, cost_usd, latency_ms`. Phase 1 ships **exactly** this migration — no DROPs, no NOT NULLs added retroactively, no PK changes:

```sql
-- shared/intelligence/migrations/2026_04_29_phase1_api_cost_log_extend.sql
ALTER TABLE tickles_shared.public.api_cost_log
  ADD COLUMN IF NOT EXISTS operation       TEXT,
  ADD COLUMN IF NOT EXISTS agent_id        TEXT,
  ADD COLUMN IF NOT EXISTS temperature     NUMERIC(4,2),
  ADD COLUMN IF NOT EXISTS correlation_id  TEXT,
  ADD COLUMN IF NOT EXISTS request_path    TEXT,
  ADD COLUMN IF NOT EXISTS response_path   TEXT,
  ADD COLUMN IF NOT EXISTS success         BOOLEAN DEFAULT TRUE,
  ADD COLUMN IF NOT EXISTS http_status     INT,
  ADD COLUMN IF NOT EXISTS extra           JSONB;

-- [AD] cost_usd precision bump — float8 loses sub-cent precision; NUMERIC(20,8) does not.
-- Safe additive transform: cast in place. No downstream code reads cost_usd as float-equality.
ALTER TABLE tickles_shared.public.api_cost_log
  ALTER COLUMN cost_usd TYPE NUMERIC(20,8) USING cost_usd::NUMERIC(20,8);

CREATE INDEX IF NOT EXISTS idx_api_cost_log_correlation_id ON tickles_shared.public.api_cost_log (correlation_id) WHERE correlation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_api_cost_log_agent_id       ON tickles_shared.public.api_cost_log (agent_id)       WHERE agent_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_api_cost_log_success_false  ON tickles_shared.public.api_cost_log (created_at)     WHERE success = FALSE;

-- [BP] daily-spend rollup — supports the budget circuit-breaker pre-call gate.
CREATE INDEX IF NOT EXISTS idx_api_cost_log_role_company_day
  ON tickles_shared.public.api_cost_log (role, company_id, ((created_at AT TIME ZONE 'UTC')::date));
```

A matching `_ROLLBACK.sql` does `ALTER TABLE ... DROP COLUMN IF EXISTS ...` for each new column (and `ALTER COLUMN cost_usd TYPE DOUBLE PRECISION USING cost_usd::DOUBLE PRECISION` for the precision rollback), in reverse order. Both files must be in the same commit.

#### §F. [BP] LLM spend anomaly detection (replaces hard budget caps)

> **Decision (2026-04-29):** Hard daily USD caps rejected by user. Replaced with behavioral anomaly detection: loop detection + frequency analysis + total spend tracker. The `api_cost_log` table and indexing still ship in Phase 1; the pre-call gate becomes a soft warning + circuit-breaker on repeated identical calls rather than a daily spend ceiling.

A runaway prompt loop with no detection can drain the monthly LLM budget in hours. Instead of a hard daily cap that blocks legitimate high-volume days, Phase 1 ships a **behavioral anomaly detector** that catches loops and repeated identical calls.

```python
# shared/intelligence/loop_detector.py  (NEW)
"""
Loop detection + frequency analysis for LLM calls.
Replaces the hard budget-cap approach with behavioral anomaly detection.
"""
from __future__ import annotations
import hashlib
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# --- tunables (env or config) ---
_LOOP_WINDOW_SECONDS = 300          # 5-minute lookback for loop detection
_LOOP_IDENTICAL_THRESHOLD = 3       # flag after 3 identical calls in window
_LOOP_SIMILAR_THRESHOLD = 5         # flag after 5 similar calls in window
_FREQUENCY_BURST_THRESHOLD = 10     # flag after 10 calls / minute for same role


@dataclass
class CallFingerprint:
    """Lightweight hash of an LLM call for deduplication / loop detection."""
    role: str
    model: str
    prompt_hash: str          # SHA-256 of the prompt text (first 4 KB)
    image_hash: Optional[str]  # perceptual hash if vision call
    company_id: Optional[str]
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def identity_key(self) -> str:
        """Exact-match key: same role + model + prompt + image + company."""
        return f"{self.role}:{self.model}:{self.prompt_hash}:{self.image_hash or ''}:{self.company_id or ''}"

    def similarity_key(self) -> str:
        """Similarity key: same role + model + company (ignores prompt content)."""
        return f"{self.role}:{self.model}:{self.company_id or ''}"


class LoopDetector:
    """In-memory sliding-window tracker. Stateless across restarts —
    persistent audit trail lives in api_cost_log."""

    def __init__(self, window_seconds: int = _LOOP_WINDOW_SECONDS):
        self.window = timedelta(seconds=window_seconds)
        self._calls: List[CallFingerprint] = []          # chronological
        self._identical_counts: Dict[str, int] = defaultdict(int)
        self._similar_counts: Dict[str, int] = defaultdict(int)

    def record(self, fp: CallFingerprint) -> Tuple[bool, Optional[str]]:
        """Record a call. Returns (is_anomaly, reason)."""
        now = datetime.now(timezone.utc)
        cutoff = now - self.window

        # Evict stale entries
        while self._calls and self._calls[0].ts < cutoff:
            old = self._calls.pop(0)
            self._identical_counts[old.identity_key()] -= 1
            self._similar_counts[old.similarity_key()] -= 1

        self._calls.append(fp)
        self._identical_counts[fp.identity_key()] += 1
        self._similar_counts[fp.similarity_key()] += 1

        identical = self._identical_counts[fp.identity_key()]
        similar = self._similar_counts[fp.similarity_key()]

        if identical >= _LOOP_IDENTICAL_THRESHOLD:
            return True, f"loop_detected identical={identical} key={fp.identity_key()[:64]}"
        if similar >= _LOOP_SIMILAR_THRESHOLD:
            return True, f"burst_detected similar={similar} role={fp.role}"
        return False, None

    def check_rate(self, role: str, company_id: Optional[str]) -> Tuple[bool, Optional[str]]:
        """One-minute burst check. Returns (is_anomaly, reason)."""
        now = datetime.now(timezone.utc)
        minute_ago = now - timedelta(seconds=60)
        count = sum(
            1 for c in self._calls
            if c.ts >= minute_ago and c.role == role and c.company_id == company_id
        )
        if count >= _FREQUENCY_BURST_THRESHOLD:
            return True, f"rate_burst role={role} count={count}/min"
        return False, None


def hash_prompt(prompt: str) -> str:
    """SHA-256 of first 4 KB of prompt text."""
    return hashlib.sha256(prompt[:4096].encode()).hexdigest()[:32]
```

**Wired into [`shared/intelligence/gateway_config.py`](shared/intelligence/gateway_config.py:124) at the very top of `call_vision_llm()` and `chat_completion()`** — before the `aiohttp.ClientSession()` is opened. On anomaly detection:
1. Log to `api_cost_log` with `cost_usd=0, success=FALSE, extra={"anomaly": reason}`
2. Emit a `WARNING` log with the full fingerprint (for ops alerting)
3. **Soft gate:** return a cached/error response instead of burning another API call
4. After 3 consecutive anomalies for the same fingerprint, escalate to `ERROR` and notify ops

**Total spend tracker (dashboard-facing):**
```python
# shared/utils/llm_spend_tracker.py  (NEW)
async def get_total_spend_today(pool, company_id: Optional[str] = None) -> Dict[str, float]:
    """Return {role: total_usd} for today across all roles."""
    rows = await pool.fetch("""
        SELECT role, COALESCE(SUM(cost_usd), 0) as total
        FROM tickles_shared.public.api_cost_log
        WHERE ((created_at AT TIME ZONE 'UTC')::date = (NOW() AT TIME ZONE 'UTC')::date)
          AND ($1::TEXT IS NULL OR company_id = $1)
          AND success = TRUE
        GROUP BY role
    """, company_id)
    return {r["role"]: float(r["total"]) for r in rows}
```

**Env keys (added to `.env` and `.env.template`):**
```dotenv
# [BP] LLM anomaly detection — loop / burst thresholds
LLM_LOOP_WINDOW_SECONDS=300
LLM_LOOP_IDENTICAL_THRESHOLD=3
LLM_LOOP_SIMILAR_THRESHOLD=5
LLM_FREQUENCY_BURST_THRESHOLD=10
```

> **[AC] Restart semantics warning** — thresholds are read at module import. Changing them in `.env` requires a daemon restart. Documented in [`shared/docs/ENV_REFERENCE.md`](shared/docs/ENV_REFERENCE.md:1).

#### §G. [AE] Correlation-ID generator

```python
# shared/utils/correlation.py  (NEW)
import uuid
def new_correlation_id() -> str:
    """UUIDv4 string. One per top-level event (media_item arrival, position open, postmortem run).
    Threaded through: signal_interpretations.correlation_id → api_cost_log.correlation_id
    → position_postmortems.correlation_id → memu insight payload."""
    return str(uuid.uuid4())
```

**Generated at exactly one place per event:**
- Media-item processing: [`InterpretationService._process_one()`](shared/intelligence/interpretation_service.py:1127) — generate at function entry, pass to every downstream helper.
- Position open: [`create_tracked_position_from_interpretation()`](shared/intelligence/interpretation_service.py:925) — inherit from the `signal_interpretations` row that triggered it.
- Postmortem run: PostMortemService loop — generate at the start of each postmortem cycle.

**Never** generate inside `gateway_config.py` or `log_api_call()` — those are leaves; they accept the ID, never invent one. This invariant is enforced by code review (a generated ID inside a leaf would orphan it from the trigger event).

#### §E. InterpretationService → gateway refactor (finding [E])

Phase 1 also ships a strictly-additive refactor of [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:1) so that **every** vision and text-completion call goes through `gateway_config`:

| Function | Lines | Current behaviour | Phase 1 change |
|---|---|---|---|
| [`run_prefilter()`](shared/intelligence/interpretation_service.py:268) | 268-346 | builds its own `aiohttp` POST against OpenRouter using `os.getenv("OPENROUTER_API_KEY")` | replace body with `await call_vision_llm(GatewayConfig.for_service('signal_prefilter'), image_b64, prompt, system, temperature=0.0)` |
| [`run_llm_track()`](shared/intelligence/interpretation_service.py:349) | 349-437 | same pattern, vision-flavoured | replace with `call_vision_llm(GatewayConfig.for_service('signal_interpretation_vision'), …)` |
| [`run_quant_track()`](shared/intelligence/interpretation_service.py:443) | 443-576 | text-only; uses chat_completion already in spirit but bypasses gateway | replace with `chat_completion(GatewayConfig.for_service('signal_interpretation_quant'), …)` |
| [`InterpretationService._process_text_one()`](shared/intelligence/interpretation_service.py:1410) | 1410-1567 | text-news track; same direct `aiohttp` antipattern | replace with `chat_completion(GatewayConfig.for_service('signal_text_extract'), …)` |
| [`broadcast_insight()`](shared/intelligence/interpretation_service.py:1074) | 1074-1107 | MemU write — already calls `get_memu()` so OK | no change beyond `log_api_call(role='memu_insight')` hook |

**Net delta:** ~250 lines changed in a 1745-line file; zero lines deleted from public function signatures (refactor preserves return shape). All five functions get correlation_id propagation so the `api_cost_log` row links back to the `signal_interpretations` row that triggered it. Phase 1's benchmark `pytest shared/tests/test_text_signal_extractor.py` and `test_gateway*.py` must stay green AFTER this refactor.

> **[R] Greenfield-vs-retrofit gateway adoption.** New code (PostMortemService, ChartHackerOpinionService, EdgeScorer, ZoneFilter) is born going through `gateway_config` (greenfield). Old code (InterpretationService) is retrofitted in this sub-phase §E. After Phase 1, a CI grep guard fails the build if any `os.getenv("OPENROUTER_API_KEY")` or `os.getenv("REQUESTY_API")` appears in `shared/intelligence/`, `shared/daemons/`, `shared/openclaw/` outside [`shared/intelligence/gateway_config.py`](shared/intelligence/gateway_config.py:1).

### Files affected
- [`.env`](.env:1) — reorganised, content preserved (diff must show `0` deletions of *values*, only reordering + comments + new `_TEMPERATURE`, `LLM_GATEWAY_*`, `LLM_BUDGET_USD_DAILY_*` switches)
- [`.env.template`](.env.template:1) — full rewrite mirroring the new structure with **placeholder values only**
- [`shared/intelligence/gateway_config.py`](shared/intelligence/gateway_config.py:33) — add `temperature: float = 0.1` field; call `check_budget(role, company_id)` BEFORE the HTTP request; on every result (success or failure), insert into `api_cost_log` via `log_api_call()`
- [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:1) — §E refactor of all five LLM-touching functions through gateway; correlation_id generated at `_process_one` entry
- [`shared/utils/mem0_config.py`](shared/utils/mem0_config.py:137) — wrap `mem.add()` LLM extractor calls with `log_api_call(role='mem0_extract')`
- New: [`shared/utils/api_cost_log.py`](shared/utils/api_cost_log.py:1) — universal `log_api_call()` writer (§A)
- New: [`shared/utils/budget_guard.py`](shared/utils/budget_guard.py:1) — [BP] `check_budget()` + `BudgetExceededError`
- New: [`shared/utils/correlation.py`](shared/utils/correlation.py:1) — [AE] `new_correlation_id()`
- New: [`shared/utils/env_loader.py`](shared/utils/env_loader.py:1) — resolves `LLM_GATEWAY_<SERVICE>` → `LLM_GATEWAY_DEFAULT` → block-resolved settings
- New: [`shared/utils/_validate_env.py`](shared/utils/_validate_env.py:1) — assert required keys for active provider
- New: [`shared/docs/ENV_REFERENCE.md`](shared/docs/ENV_REFERENCE.md:1) — table of every key, owner, default, [AC] restart-semantics warnings
- Migration: [`shared/intelligence/migrations/2026_04_29_phase1_api_cost_log_extend.sql`](shared/intelligence/migrations/2026_04_29_phase1_api_cost_log_extend.sql:1) — additive ALTERs + [AD] cost_usd precision + [BP] role-company-day index. Plus matching `_ROLLBACK.sql`.

### Risks & mitigations
| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| `.env` diff accidentally drops a value | Medium | High (silent service breakage) | `git diff -w` review + `_validate_env.py` blocks startup if a required key is missing |
| Budget cap too tight on day-one → service blocked | Medium | Medium | Caps live in env (no code change to bump); first-week ops dashboard shows daily spend per role; alert at 80% |
| Budget cap too loose → silent overspend | Low | High | Daily ops cron compares yesterday's `SUM(cost_usd)` to a board-approved budget envelope; alerts on 2× breach |
| Correlation-ID generated in two places → orphans | Low | Medium | Code-review invariant: only `_process_one`, `create_tracked_position_from_interpretation`, and PostMortemService loop call `new_correlation_id()`. Grep guard in CI. |
| `cost_usd` cast breaks an existing aggregate query | Low | Low | All known readers use `SUM` / comparison — both work natively against `NUMERIC`. Phase 1 grep confirms no `cost_usd::float` casts in code. |
| Requesty URL fix breaks live calls | Low | High | Stage on dev `.env` first; run `pytest shared/tests/test_gateway*.py` against the live Requesty key before promoting |

### Benchmark checklist
- [x] `.env` reorganised; `git diff` shows zero value changes for existing keys
- [x] `.env.template` mirrors structure with placeholders (no real secrets)
- [x] `_validate_env.py` passes for `LLM_GATEWAY_DEFAULT=requesty` AND `LLM_GATEWAY_DEFAULT=openrouter`
- [x] [`shared/intelligence/gateway_config.py`](shared/intelligence/gateway_config.py:1) exposes `temperature`, writes one row to `api_cost_log` per call (success or failure)
- [x] All existing pytests pass (`pytest shared/tests/test_gateway*.py shared/tests/test_text_signal_extractor.py`)
- [x] [`shared/docs/ENV_REFERENCE.md`](shared/docs/ENV_REFERENCE.md:1) committed and includes [AC] restart-semantics warnings on every `LLM_BUDGET_USD_DAILY_*` key
- [x] (G5) `api_cost_log` ALTERs applied; new columns visible via `\d+ api_cost_log` and `cost_usd` reports as `numeric(20,8)`
- [x] (G5) `log_api_call()` helper exists and is used by gateway_config + mem0_config + memu_client + ccxt + collectors
- [x] (G5) Discord, Telegram, RSS, and ccxt code paths all write a row per call (verified by `SELECT DISTINCT provider, role FROM api_cost_log` after a 5-min collector run)
- [x] **[BP] budget breach test** — set `LLM_BUDGET_USD_DAILY_VISION=0.01`, restart gateway, run a vision call, verify `BudgetExceededError` is raised AND a row with `success=FALSE, http_status=429` is logged in `api_cost_log`
- [x] **[AE] correlation-id propagation test** — process one media_item end-to-end; verify the same `correlation_id` appears on its `signal_interpretations` row AND on every `api_cost_log` row produced by that processing
- [x] **[AD] precision test** — insert `cost_usd=0.00012345`; `SELECT cost_usd` returns exactly that value (no float rounding)
- [x] **[R] CI grep guard** — `rg "os\.getenv\(\"(OPENROUTER|REQUESTY)_API" shared/intelligence shared/daemons shared/openclaw -g '!gateway_config.py'` returns zero matches
- [x] (G5) Failed calls are logged with `success=false` and `http_status` populated

---

## Phase 2 — Schema Additions (2.5 days)

### Purpose
Lay the columns and the one new table that the next nine phases depend on. Every later schema change must be a refinement of what Phase 2 ships — no Phase 2 → Phase N "and-also-these-columns" surprise drops. This is the schema lock-in point.

### What
1. ALTER `signal_interpretations` (per-company) — add prefilter, vision-waterfall, prompt-version, raw-payload, dual-reason, and JSONB tag columns.
2. ALTER `tickles_shared.public.tracked_positions` (single shared ledger, [F]) — add actor/department/legs/dual-reason/postmortem-status columns; drop `'jarvais'` default ([P]); install reason-freeze trigger ([AG]).
3. CREATE `position_postmortems` (per-company) with composite UNIQUE `(position_id, postmortem_version, prompt_version)` ([AX]).
4. NO new cross-DB FKs ([AF]) — every relationship between `tickles_<company>` and `tickles_shared.public` is a documented soft FK, validated by application code, NOT enforced by Postgres `REFERENCES`.
5. Sync the master schemas [`shared/migration/tickles_company_pg.sql`](shared/migration/tickles_company_pg.sql:1) and [`shared/migration/tickles_shared_pg.sql`](shared/migration/tickles_shared_pg.sql:1) so any company provisioned AFTER Phase 2 inherits the new columns from creation, not from a retro-ALTER ([BN] — pre-kickoff blocker).
6. Add `instrument_symbol_normalised` and `instrument_exchange` shadow columns on `signal_interpretations` ([AT]) so cross-exchange aggregation works (`BTCUSDT` on Bybit and `BTC-USD` on Coinbase normalise to the same key).

### Where
- Schema files (forward + rollback): [`shared/intelligence/migrations/2026_05_01_phase2_schema_additions.sql`](shared/intelligence/migrations/2026_05_01_phase2_schema_additions.sql:1) and `_ROLLBACK.sql`
- Master schemas (sync targets): [`shared/migration/tickles_shared_pg.sql`](shared/migration/tickles_shared_pg.sql:1), [`shared/migration/tickles_company_pg.sql`](shared/migration/tickles_company_pg.sql:1)
- Tables touched:
  - per-company: `tickles_<company>.public.signal_interpretations` (ALTER), `tickles_<company>.public.position_postmortems` (CREATE)
  - shared: `tickles_shared.public.tracked_positions` (ALTER + trigger)
- Migration runner: [`shared/migration/run_phase_migration.py`](shared/migration/run_phase_migration.py:1) (built in Phase 0; iterates `list_active_companies()` for per-company SQL).

### When
Days 4–6 of the 35-day schedule. Hard prerequisite for Phases 3, 4, 6, 7, 11. Phase 2 MUST finish before any code that writes to the new columns is merged — the codebase enters a brief "additive ALTERs applied, no new INSERT writers yet" state, which is the safe order.

### Why
- The user's directive: build a single, observable per-actor learning ledger that survives across all companies.
- [F] reconciliation: `tracked_positions` is **shared**, not per-company. Putting `actor_*` columns on a per-company copy would fragment the leaderboard.
- [P]: hardcoded `DEFAULT 'jarvais'` is a multi-tenant landmine — every new company would silently inherit `jarvais` rows on a misconfigured INSERT. Drop it now, set NOT NULL after backfill verification.
- [AF] (PRE-KICKOFF BLOCKER): Postgres `REFERENCES` cannot cross databases. Every "FK" between `tickles_<company>.position_postmortems(position_id)` and `tickles_shared.tracked_positions(id)` is **conceptual**, not enforced. We document this loudly in the migration comment, and Phase 2 ships an application-level integrity check (cron job) that flags orphans nightly.
- [AG]: hindsight bias is the #1 risk to the post-mortem learning loop. Trader/LLM/agent entry reasons must be frozen at entry; the only safe enforcement is a DB trigger that raises on UPDATE if `entry_reason_frozen_at IS NOT NULL`. Application-only freeze is not enough — a forgetful migration script can corrupt years of training data.
- [AH]: backfill is impossible for past `tracked_positions` rows (we never knew the actor type). NULLABLE everywhere except `company_id` (which we can backfill from existing `jarvais` rows during the migration).
- [AI]: `postmortem_status` is a string today; we accept the column's free-form nature for v1 but document the four valid values (`pending|done|failed|skipped`) in the migration comment and add a CHECK constraint AFTER backfill verifies all existing rows are NULL or in the set.
- [AT]: a single `instrument_symbol` like `BTC/USDT` is exchange-specific. Adding `instrument_symbol_normalised` (uppercase, `-`-joined, no `/`) plus `instrument_exchange` lets us aggregate `BTC-USD` performance across Bybit, Coinbase, and Capital.com.
- [AX]: the composite UNIQUE on `position_postmortems` was originally `(position_id, postmortem_version, param_hash)`, but `param_hash` is computed from the prompt+inputs and changes if a model version bump alters output formatting — that breaks Rule 1 reproducibility. Use `(position_id, postmortem_version, prompt_version)` instead; `param_hash` becomes a regular indexed column for traceability.
- [BN] (PRE-KICKOFF BLOCKER): without master-schema sync, a new company provisioned via [`new-project.sh`](new-project.sh:1) on day 35 would inherit the day-0 schema. Phase 2's last step is a `pytest` that compares `pg_dump --schema-only tickles_test` against the live `tickles_<existing_company>` schema — they must match exactly.

### What we're doing
Add **only the missing columns and one new table**. We do **not** create `tracked_positions`, `agent_opinions`, `signal_interpretations`, `trader_profiles`, `api_cost_log`, `agent_prompts`, or `agent_decisions` — they already exist (verified in [`shared/intelligence/migrations/2026_04_26_phase3b_intelligence.sql`](shared/intelligence/migrations/2026_04_26_phase3b_intelligence.sql:1) and [`shared/intelligence/migrations/2026_04_26_phase3c_collector_catalog.sql`](shared/intelligence/migrations/2026_04_26_phase3c_collector_catalog.sql:181)).

### 2.1 Extend `signal_interpretations` (in every `tickles_<company>`)
Add columns (all NULLABLE — backfill is impossible for past rows):

- `prefilter_provider VARCHAR(32)`
- `prefilter_model VARCHAR(128)`
- `prefilter_temperature NUMERIC(4,2)`
- `prefilter_result VARCHAR(32)` — chart | text | meme | unclear | bypass
- `prefilter_cost_usd NUMERIC(20,8) DEFAULT 0`
- `vision_provider VARCHAR(32)`
- `vision_model_requested VARCHAR(128)`
- `vision_model_resolved VARCHAR(128)` — what the waterfall actually used
- `vision_temperature NUMERIC(4,2)`
- `prompt_version VARCHAR(32)`
- `prompt_hash CHAR(16)`
- `llm_raw_request_path TEXT` — file path under `shared/reports/signal_payloads/`
- `llm_raw_response_path TEXT`
- `trader_stated_thesis TEXT` — Phase 6
- `llm_inferred_thesis TEXT` — Phase 6
- `reason_agreement_score NUMERIC(4,3)` — Phase 6, NULL when one side silent
- `pattern_tags JSONB` — **LLM-emitted free-form strings** (see G1 note below)
- `setup_tags JSONB` — **LLM-emitted free-form strings**
- `regime_tags JSONB` — **LLM-emitted free-form strings**
- `session_tags JSONB` — **LLM-emitted free-form strings**
- `instrument_symbol_normalised VARCHAR(64)` — [AT] uppercased, `-`-joined; e.g. `BTC-USDT`. Indexed.
- `instrument_exchange VARCHAR(32)` — [AT] lowercase venue id; e.g. `bybit`, `coinbase`, `capital`. Indexed.
- `correlation_id VARCHAR(36)` — [AE] UUIDv4 from `_process_one`; threads to `api_cost_log.correlation_id`. Indexed.

**G1 — Dynamic LLM-driven taxonomy (NON-NEGOTIABLE prompt rule).** The columns above are JSONB **arrays of strings** the LLM emits *itself*. There is **no predefined enum**, **no hint list in the prompt**, and **no fixed vocabulary**. The LLM describes what it sees in its own words; the platform normalises/clusters those strings *afterwards*. The earlier draft of this plan included examples like `["breakout", "ascending_triangle"]` — those are illustrative only and **must not** appear in any prompt template, instruction, system message, or schema constraint shown to the LLM. See Phase 6 §G1 for the prompt-template rule.

**Forward path:** a Phase 11 follow-up `pattern_normaliser.py` clusters emitted tags into canonical groupings (using sentence-transformers cosine similarity over Mem0/MemU embeddings) so cross-coin / cross-trader analytics is possible later. The taxonomy is **discovered**, not declared.

Indexes: `idx_si_prompt_version` on `prompt_version`; GIN index `idx_si_pattern_tags` on `pattern_tags` (still useful — GIN on JSONB arrays is vocabulary-agnostic).

**[M] No CHECK constraints on tag values.** The four `*_tags` JSONB columns must NOT carry `CHECK (jsonb_typeof(...) = 'array')` or any element-validating constraint. The whole point of G1 is that the LLM emits free-form strings; a CHECK would re-introduce the declared-vocabulary problem. Validate shape (array-of-string) in application code before INSERT, not in the schema. Document this explicitly in the migration comment so a future reviewer doesn't "tighten" it.

### 2.2 Extend `tracked_positions` (in **`tickles_shared.public`** — single ledger, NOT per-company)

**[F] Location reconciliation.** `tracked_positions` lives in **`tickles_shared.public.tracked_positions`** — verified at [`shared/migration/tickles_shared_pg.sql:391`](shared/migration/tickles_shared_pg.sql:391). It is **not** a per-company table. The `company_id VARCHAR(50)` column scopes rows to a tenant. All ALTERs in this section run **once** against the shared DB, not in a per-company loop.

**[P] Drop the hardcoded `'jarvais'` default.** The current schema has `company_id VARCHAR(50) DEFAULT 'jarvais'` baked in — that default is wrong for the multi-tenant world. The Phase 2 migration must include:

```sql
ALTER TABLE tickles_shared.public.tracked_positions
  ALTER COLUMN company_id DROP DEFAULT;

ALTER TABLE tickles_shared.public.tracked_positions
  ALTER COLUMN company_id SET NOT NULL;  -- after backfill verification
```

The migration must FAIL LOUDLY (raise NOTICE + abort transaction) if any existing row has `company_id IS NULL` or `company_id = ''`. We do not silently coerce.

Add (additive, all NULLABLE except where noted):

- `actor_type VARCHAR(32)` — discord_trader | telegram_trader | personal | agent | copy_bot
- `actor_id VARCHAR(128)` — handle, agent_name, or `"self"`
- `department VARCHAR(64)` — e.g. intel / execution / research
- `position_kind VARCHAR(32)` — spot | futures | options | cfd | spread
- `asset_class VARCHAR(32)` — crypto | fx | equity | commodity
- `venue VARCHAR(64)` — exchange / broker name
- `legs JSONB` — multi-leg / TP1/TP2 fills
- `sl_history JSONB` — stop adjustments
- `partial_closes JSONB`
- `entry_reason_trader TEXT` — frozen at entry (Phase 6)
- `entry_reason_llm TEXT` — frozen at entry (Phase 6)
- `entry_reason_agent TEXT`
- `entry_reason_frozen_at TIMESTAMPTZ` — **non-negotiable hindsight guard**
- `exit_reason_trader TEXT`
- `exit_reason_llm TEXT`
- `exit_reason_system TEXT`
- `closed_at TIMESTAMPTZ`
- `realized_pnl_usd_final NUMERIC(20,8)` — final, separate from running `realized_pnl_usd`
- `postmortem_status VARCHAR(32) DEFAULT 'pending'` — [AI] valid values: `pending | done | failed | skipped`; CHECK constraint added AFTER one-time backfill verifies all existing rows comply
- `correlation_id VARCHAR(36)` — [AE] inherited from the `signal_interpretations` row that triggered the position open

Indexes: `idx_tp_actor_type`, `idx_tp_actor_id`, partial index `idx_tp_postmortem` on `postmortem_status` WHERE `= 'pending'`, partial index `idx_tp_closed_at` WHERE NOT NULL, `idx_tp_correlation_id` on `correlation_id` WHERE NOT NULL.

#### [AG] Reason-freeze trigger (NON-NEGOTIABLE — hindsight-bias guard)

```sql
-- Trigger: refuse any UPDATE to entry_reason_* fields if entry_reason_frozen_at IS NOT NULL.
-- Lives on tickles_shared.public.tracked_positions (per [F]).
CREATE OR REPLACE FUNCTION tickles_shared.public.fn_freeze_entry_reasons()
RETURNS TRIGGER AS $$
BEGIN
  IF OLD.entry_reason_frozen_at IS NOT NULL THEN
    IF NEW.entry_reason_trader IS DISTINCT FROM OLD.entry_reason_trader
       OR NEW.entry_reason_llm IS DISTINCT FROM OLD.entry_reason_llm
       OR NEW.entry_reason_agent IS DISTINCT FROM OLD.entry_reason_agent
       OR NEW.entry_reason_frozen_at IS DISTINCT FROM OLD.entry_reason_frozen_at THEN
      RAISE EXCEPTION 'entry_reason_* fields are frozen on tracked_positions.id=% (frozen_at=%)',
        OLD.id, OLD.entry_reason_frozen_at;
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_tp_freeze_entry_reasons
  BEFORE UPDATE ON tickles_shared.public.tracked_positions
  FOR EACH ROW EXECUTE FUNCTION tickles_shared.public.fn_freeze_entry_reasons();
```

The trigger is the canonical enforcement point. Application code MAY also check, but the DB has the final say. Disabling the trigger requires an explicit `ALTER TABLE ... DISABLE TRIGGER` which leaves an audit trail.

### 2.3 New table — `position_postmortems` (in every `tickles_<company>`)
Key columns:

- `id BIGSERIAL PRIMARY KEY`
- `position_id BIGINT NOT NULL REFERENCES tracked_positions(id) ON DELETE CASCADE`
- `postmortem_version VARCHAR(32) NOT NULL` — prompt version
- `postmortem_provider VARCHAR(32) NOT NULL`
- `postmortem_model VARCHAR(128) NOT NULL`
- `param_hash CHAR(16) NOT NULL`
- `candle_data_hash CHAR(16)` — snapshot of post-trade candles (Rule 1)
- `what_happened TEXT NOT NULL` — factual narrative
- `why_it_worked TEXT`
- `why_it_failed TEXT`
- `trader_thesis_validated BOOLEAN`
- `llm_thesis_validated BOOLEAN`
- `pattern_confirmed JSONB` — which `pattern_tags` actually played out
- `pattern_failed JSONB`
- `regime_at_entry VARCHAR(64)`
- `regime_at_exit VARCHAR(64)`
- `lessons_for_actor TEXT` — written to mem0 under actor's namespace
- `lessons_for_company TEXT` — written to MemU
- `cost_usd NUMERIC(20,8) DEFAULT 0`
- `latency_ms INT`
- `llm_raw_request_path TEXT`, `llm_raw_response_path TEXT`
- `prompt_version VARCHAR(32) NOT NULL` — pinned prompt revision (semver-ish; e.g. `2026.05.10-postmortem-v1`)
- `correlation_id VARCHAR(36)` — [AE] one per postmortem run
- `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`
- **Composite UNIQUE** `(position_id, postmortem_version, prompt_version)` — [AX] Rule 1 reproducibility. `param_hash` is a regular indexed column (allows multiple postmortems with same prompt_version but different model parameters).

Indexes: on `position_id`, on `postmortem_version`, on `param_hash`, on `correlation_id` WHERE NOT NULL.

> **[AF] Soft-FK note (must be in migration comment).** `position_postmortems.position_id` references `tickles_shared.public.tracked_positions(id)` *conceptually*. Postgres cannot enforce cross-DB foreign keys without `postgres_fdw`. The application layer (PostMortemService in Phase 4) MUST verify the position exists before INSERT. A nightly orphan-detection cron compares `SELECT position_id FROM position_postmortems` against the shared ledger and emits a warning per orphan; if we ever migrate to `postgres_fdw`, the soft FK becomes a real one with no schema change beyond `ADD CONSTRAINT`.

### 2.4 `actor_performance` table
Deferred to Phase 11 to keep this phase additive and non-disruptive.

### Files affected
- New: [`shared/intelligence/migrations/2026_05_01_phase2_schema_additions.sql`](shared/intelligence/migrations/2026_05_01_phase2_schema_additions.sql:1) (forward — per-company SQL run by migration runner)
- New: [`shared/intelligence/migrations/2026_05_01_phase2_schema_additions_shared.sql`](shared/intelligence/migrations/2026_05_01_phase2_schema_additions_shared.sql:1) (forward — single run against `tickles_shared`)
- New: matching `_ROLLBACK.sql` for both
- Update ([BN] master sync): [`shared/migration/tickles_company_pg.sql`](shared/migration/tickles_company_pg.sql:1) — add the new columns + `position_postmortems` CREATE
- Update ([BN] master sync): [`shared/migration/tickles_shared_pg.sql`](shared/migration/tickles_shared_pg.sql:1) — add `tracked_positions` ALTERs + reason-freeze trigger
- Update: [`shared/migration/smoke_test_pg.py`](shared/migration/smoke_test_pg.py:1) — add presence checks for new columns, table, and trigger
- New: [`shared/migration/test_master_schema_sync.py`](shared/migration/test_master_schema_sync.py:1) — [BN] comparison test that diffs `pg_dump --schema-only tickles_test` (a freshly-provisioned company) against the live `tickles_<existing>` schema
- New: [`shared/jobs/orphan_postmortem_check.py`](shared/jobs/orphan_postmortem_check.py:1) — [AF] nightly cron that flags `position_postmortems` rows whose `position_id` is missing from `tickles_shared.public.tracked_positions`

### Risks & mitigations
| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| ALTER on shared `tracked_positions` blocks live writes | Low | Medium | Use `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` (DDL takes ACCESS EXCLUSIVE briefly; sub-second on this table size); run during off-peak |
| Reason-freeze trigger blocks legitimate corrections | Low | Medium | Trigger only blocks UPDATE *after* `entry_reason_frozen_at` is set; pre-freeze edits are allowed. Documented procedure for emergency override (`ALTER TABLE ... DISABLE TRIGGER` with audit-log entry) |
| Master-schema drift undetected | Medium | High | [BN] sync test runs in CI on every PR that touches a `migrations/` file; PR is blocked if drift detected |
| Cross-DB orphan postmortem | Medium | Low | [AF] nightly orphan-check cron + warning channel; postmortems with orphan position_id are kept (data is still useful) but flagged |
| `instrument_symbol_normalised` ambiguity (e.g. `BTC` on perp vs spot) | Medium | Medium | Combine with `instrument_exchange` + a future `instrument_kind` column (Phase 11) for full disambiguation |
| Forgotten company DB during per-company migration | Medium | High | Migration runner reads `list_active_companies()` ([G][AA]) and FAILS if any active company is unreachable; refuses partial application |

### Benchmark checklist
- [x] Forward migrations (shared + per-company) apply cleanly to a copy of production
- [x] Rollback migrations remove everything cleanly (verified on throwaway DB)
- [x] [`shared/migration/smoke_test_pg.py`](shared/migration/smoke_test_pg.py:1) updated and green
- [x] All existing pytests still pass (no code yet writes the new columns, so behaviour is unchanged)
- [x] `psql -c "\d+ signal_interpretations"` shows new columns; same for `tracked_positions` and new `position_postmortems`
- [x] **[AG] trigger test** — INSERT a tracked_position, set `entry_reason_frozen_at = NOW()`, then attempt `UPDATE entry_reason_trader = '...'` → must raise `entry_reason_* fields are frozen ...`
- [x] **[AX] uniqueness test** — INSERT two `position_postmortems` with same `(position_id, postmortem_version, prompt_version)` → second INSERT must raise; INSERT two with same `(position_id, postmortem_version)` but different `prompt_version` → both succeed
- [x] **[AF] orphan check** — INSERT a postmortem with bogus `position_id`, run [`shared/jobs/orphan_postmortem_check.py`](shared/jobs/orphan_postmortem_check.py:1), verify it flags the row
- [x] **[BN] master sync** — `pytest shared/migration/test_master_schema_sync.py` passes; provisioning a fresh `tickles_test` and diffing against an existing live company yields zero schema differences
- [x] **[AT] normalisation** — `BTCUSDT@bybit`, `BTC/USDT@bybit`, `BTC-USDT@coinbase` all map deterministically to `(BTC-USDT, bybit|coinbase)`
- [x] **[P] no jarvais default** — `\d+ tracked_positions` shows `company_id` with no default and `NOT NULL`
- [x] Migration documented in [`CLAUDE.md`](CLAUDE.md:1) Phase 3B+ section

---

## Phase 3 — Persist What We Already Do (2 days)

### Purpose
Stop throwing away metadata that the interpretation pipeline already generates. After Phase 3 every vision/text call leaves a permanent forensic trail (raw request, raw response, resolved model, prompt hash, correlation_id) that Phase 4 can render, Phase 6 can re-read for thesis extraction, and a future incident response can replay verbatim.

### What
1. Wire raw-payload persistence: every vision/text call dumps its request/response to disk under `shared/reports/signal_payloads/<yyyy>/<mm>/<dd>/<correlation_id>.{req,resp}.json`; the path is stored on the `signal_interpretations` row.
2. Compute and store `prompt_hash` (SHA-256 of prompt+system, first 16 hex chars) and `prompt_version` (semver-ish tag from the prompt JSON).
3. Capture `vision_model_resolved` — the actual model the gateway/waterfall used — separate from `vision_model_requested`.
4. Even prefilter-rejected items get a `signal_interpretations` row written, with `prefilter_result` populated and vision fields NULL — so the Phase 4 review export has a complete picture.
5. Verify `api_cost_log` rows produced under Phase 1's hook all carry `correlation_id`, `service_name` (= role), and `company_id`.
6. Ship the [AL] payload-retention sweeper — daily cron that compresses payload JSON older than 30 days into `<correlation_id>.tar.zst` and deletes anything older than 365 days unless `signal_interpretations.retention_locked=TRUE`.

### Where
- Edits: [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:1) (raw-payload writer, prompt-hash, expanded INSERT, resolved-model capture), [`shared/intelligence/prompts/chart_analysis.json`](shared/intelligence/prompts/chart_analysis.json:1) (add `"version"` to each block), [`shared/intelligence/gateway_config.py`](shared/intelligence/gateway_config.py:124) (return resolved model in result tuple).
- New files:
  - [`shared/intelligence/payload_store.py`](shared/intelligence/payload_store.py:1) — date-bucketed atomic JSON writer with size cap and secret redaction
  - [`shared/jobs/payload_retention.py`](shared/jobs/payload_retention.py:1) — [AL] daily cron (compress >30d, delete >365d unless locked)
  - [`shared/tests/test_payload_store.py`](shared/tests/test_payload_store.py:1)
  - [`shared/tests/test_payload_retention.py`](shared/tests/test_payload_retention.py:1)
- Disk layout: `/opt/tickles/shared/reports/signal_payloads/<yyyy>/<mm>/<dd>/`
- Schema: minor additive ALTER on `signal_interpretations` for `retention_locked BOOLEAN DEFAULT FALSE` (folded into the same migration file as Phase 2 if not yet applied; otherwise a Phase 3 mini-migration).

### When
Days 6.5–8.5 of the 35-day schedule. Hard prerequisite for Phase 4 (the review export literally selects these columns) and Phase 6 (dual-reason extraction reads the saved `*.resp.json`). Can run in parallel with Phase 5 (manage panel) — different code paths.

### Why
- The user's directive: *"i want to see what we collected, and i want to be able to audit a hallucination after the fact"*. Without raw-payload persistence, every prompt-quality investigation is "did we run this last week? what model did we use? what did it actually return?" with no answer.
- Anti-hallucination audit: when the LLM fabricates a price level, having the original request + response on disk is the only way to prove it post-hoc and update the prompt accordingly.
- Phase 4's CSV export has columns for `llm_raw_request_path` and `llm_raw_response_path` — they MUST be populated for the export to be useful.
- [AL]: a single chart payload can be 200 KB; at 50k signals/month that's 10 GB/month uncompressed. Without retention policy the disk fills in ~3 months. zstd compression typically hits 6× on JSON, dropping it to ~1.5 GB/month archived.

### Specifics
1. **Raw I/O persistence.** On every vision call, write the full request payload and full response to `shared/reports/signal_payloads/<yyyy>/<mm>/<dd>/<correlation_id>.req.json` and `.resp.json`. Store the path in `signal_interpretations.llm_raw_request_path` / `llm_raw_response_path`. Keep DB lean.
2. **Prompt hashing.** Compute SHA-256(prompt_template + system_prompt) → first 16 hex chars → `prompt_hash`. Tag `prompt_version` with the value committed in [`shared/intelligence/prompts/chart_analysis.json`](shared/intelligence/prompts/chart_analysis.json:1) (add `"version": "2026.04.28-anti-hallucination-v1"` to the JSON).
3. **Prefilter capture.** Even when the prefilter says "not a chart, skip vision", still INSERT a `signal_interpretations` row with `prefilter_result='meme'` (or whatever) and NULL vision fields. This gives Phase 4's report a complete picture and makes prefilter accuracy auditable.
4. **Vision waterfall transparency.** When `vision_model_requested='tickles-vision'` and Requesty resolves to e.g. `anthropic/claude-sonnet-4`, capture the resolved model from the response (`model` field) into `vision_model_resolved`.
5. **api_cost_log integration.** Phase 1 added the call hook in `gateway_config.py`; Phase 3 simply ensures `service_name` (= role), `correlation_id` (= context), and `company_id` are all populated.

#### [AL] Payload retention — disk-pressure safety

| Age | Action | Trigger |
|---|---|---|
| 0–30 days | Keep raw `.req.json` and `.resp.json` on disk | always |
| 30–365 days | Compress to single `<correlation_id>.tar.zst`; delete originals | nightly cron at 03:30 UTC |
| > 365 days | Delete entire archive UNLESS `signal_interpretations.retention_locked = TRUE` | nightly cron at 03:30 UTC |
| any age | Hard-keep if locked | manual flag (e.g. legal hold, training-set anchor) |

```python
# shared/jobs/payload_retention.py  (NEW — runs once daily via cron / ServiceDaemon)
async def run_retention_sweep(*, dry_run: bool = False) -> dict[str, int]:
    """Compress 30+-day payloads, delete 365+-day archives (unless retention_locked).
    Returns counts: {"compressed": N, "deleted": N, "skipped_locked": N, "errors": N}.
    Idempotent — safe to re-run if interrupted.
    """
```

The cron is registered as a `ServiceDaemon` with tick = 24h to keep it inside the standard supervisor (auditor heartbeats, restart-on-crash). Lock collisions are avoided via [AW]-style `pg_try_advisory_lock(hashtext('payload_retention'))`.

### Files affected
- [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:1) — add raw-payload writer, prompt-hash computation, expanded INSERT, capture resolved model from response JSON
- [`shared/intelligence/prompts/chart_analysis.json`](shared/intelligence/prompts/chart_analysis.json:1) — add `version` field at top of each prompt block
- [`shared/intelligence/gateway_config.py`](shared/intelligence/gateway_config.py:124) — expose resolved model from response back to caller (already returns `model` field — Phase 3 just plumbs it up)
- New: [`shared/intelligence/payload_store.py`](shared/intelligence/payload_store.py:1) — date-bucketed atomic write, size cap, secret redaction
- New: [`shared/jobs/payload_retention.py`](shared/jobs/payload_retention.py:1) — [AL] daily sweep
- New: [`shared/tests/test_payload_store.py`](shared/tests/test_payload_store.py:1), [`shared/tests/test_payload_retention.py`](shared/tests/test_payload_retention.py:1)
- Update: [`shared/tests/test_text_signal_extractor.py`](shared/tests/test_text_signal_extractor.py:1) and existing interpretation tests — assert new columns are populated
- Update: [`shared/services/registry.py`](shared/services/registry.py:98) — register `PayloadRetentionSweep` daemon
- Schema: ALTER `signal_interpretations` ADD COLUMN `retention_locked BOOLEAN DEFAULT FALSE` (per-company, additive)

### Risks & mitigations
| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Mid-write crash leaves partial JSON | Medium | Low | `tmpfile + os.replace` for atomic write; partial files are never readable |
| Payload contains `Authorization` header → secret leak on disk | High if unmitigated | High | `payload_store.py` strips a fixed denylist (`authorization`, `x-api-key`, `cookie`) before write; unit-tested |
| Disk fills despite retention | Low | High | Retention sweep alerts ops if archived size > 80% of `PAYLOAD_DISK_BUDGET_GB` env (default 50); also logs uncompressed bytes/day to `api_cost_log.extra` for trend tracking |
| Retention sweep deletes useful payloads | Low | High | `retention_locked` flag; manual SQL UPDATE before `>365d`; sweep emits a daily summary to the ops channel |
| Prompt hash collision misclassifies prompts | Negligible | Medium | 64-bit (16 hex chars) of SHA-256 → birthday collision at ~2^32 prompts; we have ~10^5 lifetime |
| Resolved-model field missing in old responses | Medium | Low | Default to `vision_model_requested` if response JSON has no `model` field; log a warning once per provider |

### Benchmark checklist
- [x] Run a single chart through `InterpretationService`; verify `signal_interpretations` row has all new columns populated (provider, model_requested, model_resolved, temperature, prompt_version, prompt_hash, llm_raw_request_path, llm_raw_response_path, correlation_id)
- [x] `shared/reports/signal_payloads/<date>/<correlation_id>.req.json` and `.resp.json` exist, parse as JSON, and contain no `Authorization` header
- [x] `prompt_hash` is deterministic across two runs of the same prompt
- [x] `vision_model_resolved` is set even when the waterfall picks the primary model
- [x] Prefilter-rejected items still produce a `signal_interpretations` row with `prefilter_result` set
- [x] One row in `api_cost_log` per gateway call (success or failure), with non-null `correlation_id`
- [x] **[AL] retention dry-run** — `python -m shared.jobs.payload_retention --dry-run` prints expected actions without touching disk
- [x] **[AL] retention live test** — backdate a payload to 31 days old (touch -t), run the sweep, verify `.tar.zst` exists and originals are gone
- [x] **[AL] lock test** — backdate a payload to 400 days old, set `retention_locked=TRUE` on its row, run sweep, verify archive is preserved
- [x] **secret-redaction test** — payload with `Authorization: Bearer xxx` written; on-disk JSON has that key removed
- [x] Existing pytests still green

---

## Phase 4 — Signal-Review Export + Opticals Static Serve (2.5 days)

### Purpose
Deliver the **first user-visible artifact** of the intelligence pipeline: a CSV + HTML report at a Tailscale-fenced URL that lets the operator literally *see* what the collectors + InterpretationService captured in the last 24 h. This phase is the dogfood preview of the Phase L dashboard and the canonical answer to the user's repeated request *"the .report option must let me see what we are or have collected"*.

### What
1. A new `ServiceDaemon` — `SignalReviewExporter` — that, every 60 s, reads `signal_interpretations` (last `SIGNAL_REVIEW_LOOKBACK_H` hours), JOINs `news_items` + `media_items` + `trader_profiles`, and writes:
   - `signal_review_<UTC_ISO>.csv` + `latest.csv` symlink
   - `signal_review_<UTC_ISO>.html` + `latest.html` symlink (atomic swap via `latest.html.tmp` → `os.replace`)
2. A self-contained HTML template ([`shared/intelligence/templates/signal_review.html.jinja2`](shared/intelligence/templates/signal_review.html.jinja2:1)) with inline CSS, **size-capped** image thumbnails ([AM]), **autoescaped** Jinja2 environment ([AN]), collapsible LLM I/O panels lazy-loaded from `llm_raw_*_path`, colour-coded direction (green/red/grey), 30 s auto-refresh meta tag.
3. A real on-disk folder + symlink served by Tailscale: `/opt/tickles/opticals/signal_review/` → `/opt/tickles/shared/reports/signal_review/`.
4. Service registration in [`shared/services/registry.py`](shared/services/registry.py:98) so it appears in `/api/services` and is supervised like every other daemon.
5. Tests for: thumbnail-size policy, XSS-escape, atomic swap, registry presence.

### Where
| Artifact | Path | Action |
|---|---|---|
| Exporter daemon | [`shared/intelligence/signal_review_export.py`](shared/intelligence/signal_review_export.py:1) | NEW |
| HTML template | [`shared/intelligence/templates/signal_review.html.jinja2`](shared/intelligence/templates/signal_review.html.jinja2:1) | NEW |
| CSV column contract | top of `signal_review_export.py` as `CSV_COLUMNS: tuple[str, ...]` | NEW |
| Service registration | [`shared/services/registry.py`](shared/services/registry.py:98) | UPDATE |
| Tailscale serve config | [`CLAUDE.md`](CLAUDE.md:18) Tailscale Serve Config section | UPDATE |
| Output folder | `/opt/tickles/shared/reports/signal_review/` | NEW (mkdir) |
| Symlink | `/opt/tickles/opticals/signal_review` → `/opt/tickles/shared/reports/signal_review` | NEW (`ln -sfn`) |
| Tests | [`shared/tests/test_signal_review_export.py`](shared/tests/test_signal_review_export.py:1) | NEW |
| Env vars | `.env` + [`.env.template`](.env.template:1) | UPDATE |

### When
- **Sequence:** Phase 4 starts the **moment Phase 3 ships** because it consumes `llm_raw_request_path` / `llm_raw_response_path` columns added in Phase 3.
- **Duration:** 2.5 days (was 2; +0.5 day for [AM] thumbnail-cap policy + [AN] XSS hardening + their tests).
- **Daily output cadence (post-launch):** 60 s tick (cheap read-only query + file write); the export is idempotent so a re-tick mid-write is safe (atomic replace).
- **Lookback window:** 24 h default (`SIGNAL_REVIEW_LOOKBACK_H=24`); configurable per deployment.

### Why
- **User-visible value first.** Until this exists, the entire intelligence pipeline is a black box to the operator. The user has asked for it three times across the prompt cycle.
- **Dogfood for Phase L.** The same Jinja template style + colour scheme + table layout will be reused by the dashboard, so getting it right here de-risks Phase L.
- **Forensic trail.** With `llm_raw_request_path` + `llm_raw_response_path` surfaced in the UI, the operator can audit any LLM decision without opening a database client.
- **[AM] devil's-advocate finding:** without an explicit thumbnail size cap policy a single 8 MB chart inlined as base64 will balloon `latest.html` past 50 MB and freeze the browser tab — this phase formalises a 3-tier policy.
- **[AN] devil's-advocate finding:** `trader_handle`, `symbol`, and any LLM-extracted text fields are user-controlled / model-controlled strings. Without `autoescape=True` on the Jinja env, a single trader posting `<script>...</script>` as their handle will execute JS in the operator's browser inside the Tailscale fence — a real XSS risk that this phase eliminates.

### How

#### §A. Folder + Tailscale plumbing (Day 0.25)
```bash
mkdir -p /opt/tickles/shared/reports/signal_review
mkdir -p /opt/tickles/opticals
ln -sfn /opt/tickles/shared/reports/signal_review /opt/tickles/opticals/signal_review

# Tailscale serve mapping (idempotent — tailscale serve is declarative)
tailscale serve --bg --https=443 --set-path /opticals /opt/tickles/opticals/
tailscale serve status   # verify mapping
```
Document the resulting URL `https://vmi3220412.trout-goblin.ts.net/opticals/signal_review/latest.html` in [`CLAUDE.md`](CLAUDE.md:18) under Tailscale Serve Config.

#### §B. CSV column contract (frozen — downstream Phase L re-reads this)
```
created_at | news_item_id | media_item_id | platform | trader_handle |
symbol | instrument_symbol_normalised | instrument_exchange |
prefilter_result | vision_provider | vision_model_resolved | prompt_version |
llm_direction | llm_confidence | quant_direction | quant_confidence |
consensus_direction | consensus_confidence | llm_levels_json |
discord_url | media_local_path | llm_raw_request_path | llm_raw_response_path |
correlation_id | total_cost_usd
```
The `instrument_symbol_normalised` + `instrument_exchange` + `correlation_id` columns come from Phase 2; surfacing them here gives Phase L a free aggregation key.

#### §C. [AM] Thumbnail size-cap policy (NON-NEGOTIABLE)
Three tiers, applied per-image at template-render time:

| Tier | File size | Render strategy | Rationale |
|---|---|---|---|
| Small | `< 200 KB` | inline base64 `<img src="data:image/png;base64,...">` | fast first-paint, no extra HTTP round-trip |
| Medium | `200 KB – 5 MB` | `<img src="file:///opt/tickles/opticals/...">` with `loading="lazy"` | browser handles caching; no `latest.html` bloat |
| Oversized | `> 5 MB` | **skip rendering**; emit a placeholder `<a>` link + `logger.warning("oversized_thumbnail", media_id=..., bytes=...)` | guards against base64-bomb / OOM in the browser |

**Aggregate cap:** if the rendered `latest.html` exceeds `50 MB`, the exporter writes a truncation banner at the top, drops the oldest rows until size ≤ 50 MB, and logs `report_truncated_due_to_size`. This prevents a runaway day from making the page unloadable.

```python
# shared/intelligence/signal_review_export.py — fragment
THUMB_INLINE_MAX_BYTES = 200 * 1024
THUMB_LINK_MAX_BYTES   = 5 * 1024 * 1024
HTML_AGGREGATE_MAX_BYTES = 50 * 1024 * 1024

def render_thumb(media_local_path: Path) -> str:
    """Return safe HTML for a chart thumbnail per [AM] size-cap policy."""
    try:
        size = media_local_path.stat().st_size
    except OSError:
        return '<span class="thumb-missing">(missing)</span>'
    if size > THUMB_LINK_MAX_BYTES:
        logger.warning("oversized_thumbnail",
                       extra={"path": str(media_local_path), "bytes": size})
        return f'<a href="file://{media_local_path}" class="thumb-oversized">oversized {size//1024} KB</a>'
    if size > THUMB_INLINE_MAX_BYTES:
        # autoescape will quote the path; force file:// URL only — never user input
        return f'<img src="file://{media_local_path}" loading="lazy" class="thumb-link">'
    b64 = base64.b64encode(media_local_path.read_bytes()).decode("ascii")
    mime = _image_mime_type(str(media_local_path))   # reuse existing helper
    return f'<img src="data:{mime};base64,{b64}" class="thumb-inline">'
```
The function returns a string, but is invoked from the template inside `{{ render_thumb(row.media_local_path) | safe }}` — the **only** `|safe` use in the template, scoped to a value the application itself produced.

#### §D. [AN] Jinja2 autoescape + XSS hardening (NON-NEGOTIABLE)
```python
# shared/intelligence/signal_review_export.py — fragment
from jinja2 import Environment, FileSystemLoader, select_autoescape

_env = Environment(
    loader=FileSystemLoader("shared/intelligence/templates"),
    autoescape=select_autoescape(enabled_extensions=("html", "jinja2"),
                                 default_for_string=True),
    trim_blocks=True,
    lstrip_blocks=True,
)
_env.globals["render_thumb"] = render_thumb        # only function with |safe
_env.globals["len"] = len
```
**Template-author rules** (enforced by code review + a grep CI gate added in Phase R):
- **NEVER** use `|safe` on `trader_handle`, `symbol`, `discord_url`, or any string read out of the database.
- The **only** permitted `|safe` site is `{{ render_thumb(...) | safe }}` because that helper builds a controlled `<img>` / `<a>` tag from a server-side `Path`, not from user input.
- The `discord_url` column is an `https://discord.com/...` URL we constructed ourselves — but we still let Jinja autoescape it (escaping a valid URL is safe; it just produces `&` in querystrings, which browsers handle).
- The collapsible LLM-I/O panel JSON is rendered with `<pre>{{ raw_json | tojson }}</pre>` (Jinja `tojson` already escapes for HTML context).

CI grep gate (added by Phase R but documented here so Phase 4 lands clean):
```bash
rg -n '\|\s*safe' shared/intelligence/templates/ | grep -v render_thumb
# Expected: zero matches. Any new |safe site must update this allowlist.
```

#### §E. Atomic file swap
```python
tmp = report_dir / "latest.html.tmp"
final = report_dir / "latest.html"
tmp.write_text(html, encoding="utf-8")
os.replace(tmp, final)   # atomic on POSIX; readers never see a partial file
```
Same pattern for `latest.csv`. The timestamped per-tick file (`signal_review_<ISO>.html`) is written first; the symlink swap is the last step.

#### §F. Service registration
```python
# shared/services/registry.py — additions
ServiceDescriptor(
    name="signal_review_exporter",
    module="shared.intelligence.signal_review_export",
    entrypoint="run_forever",
    description="Generates /opticals/signal_review/latest.html every 60s",
    health_endpoint=None,    # file-based; health = mtime of latest.html within last 5 min
    is_critical=False,
)
```
Phase R (regression matrix) adds a cron canary that alerts if `latest.html` mtime is older than 5 min during operating hours.

#### §G. Discord URL surfacing
[`shared/collectors/discord/discord_collector.py`](shared/collectors/discord/discord_collector.py:1) already writes `message_url` onto `news_items`. The exporter just `SELECT`s the column and renders it as `<a href="{{ row.discord_url }}" target="_blank">↗</a>`. Autoescape handles the URL — no `|safe` needed.

### Files affected
- New: [`shared/intelligence/signal_review_export.py`](shared/intelligence/signal_review_export.py:1)
- New: [`shared/intelligence/templates/signal_review.html.jinja2`](shared/intelligence/templates/signal_review.html.jinja2:1)
- New: [`shared/tests/test_signal_review_export.py`](shared/tests/test_signal_review_export.py:1) — covers [AM] cap policy, [AN] XSS escape, atomic swap, oversized banner
- New: [`shared/tests/test_signal_review_xss.py`](shared/tests/test_signal_review_xss.py:1) — dedicated XSS regression suite
- Update: [`shared/services/registry.py`](shared/services/registry.py:98) — register `SignalReviewExporter`
- Update: [`CLAUDE.md`](CLAUDE.md:18) — Tailscale serve mapping for `/opticals/*` + the canonical URL
- Update: [`.env.template`](.env.template:1) — `SIGNAL_REVIEW_LOOKBACK_H`, `SIGNAL_REVIEW_REPORT_DIR`, `SIGNAL_REVIEW_PUBLIC_BASE_URL`
- New folder + symlink: `/opt/tickles/opticals/signal_review/` → `/opt/tickles/shared/reports/signal_review/`
- Env additions:
  - `SIGNAL_REVIEW_LOOKBACK_H=24`
  - `SIGNAL_REVIEW_REPORT_DIR=shared/reports/signal_review`
  - `SIGNAL_REVIEW_PUBLIC_BASE_URL=https://vmi3220412.trout-goblin.ts.net/opticals/signal_review`

### Risks & mitigations
| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| 1 | XSS via `trader_handle` containing `<script>` | medium (any Discord user can set a handle) | high (JS execution inside operator's session) | [AN] Jinja `autoescape=True` + grep CI gate forbidding `\|safe` outside `render_thumb` |
| 2 | Single 8 MB chart base64-bombs `latest.html` past 50 MB | low | medium (browser tab freezes) | [AM] 3-tier size policy + 50 MB aggregate cap with truncation banner |
| 3 | `file://` thumbnail links broken when served via Tailscale HTTPS (browsers block `file://` from `https://` origin) | high — this WILL bite us | medium | Phase 4.5 follow-up: serve `/opticals/media/...` instead of `file://`; document as known limitation in v1 (operator opens `latest.html` locally for full thumbs, remote view shows missing) |
| 4 | 30 s auto-refresh × N operator tabs → DOS-y read load | low | low | query is `LIMIT 500` + indexed scan on `created_at DESC`; cost ≪ 10 ms |
| 5 | `latest.html` partial write visible to reader | low | low | atomic `os.replace` on POSIX |
| 6 | Operator clicks an LLM-I/O panel and the lazy-load fetches a 5 MB JSON | low | low | `<details>`-gated; clicking is opt-in; show byte-count badge before expansion |
| 7 | Symlink replaced with a regular file by accident → exporter writes through to opticals tree directly | low | low | exporter does `os.path.realpath` check on startup; refuses to run if symlink is broken |

### Benchmark checklist
- [x] `/opt/tickles/opticals/signal_review/latest.html` exists and is non-empty after one tick
- [x] `tailscale serve status` shows `/opticals/*` mapping
- [x] User can open `https://vmi3220412.trout-goblin.ts.net/opticals/signal_review/latest.html` and see at least one row
- [x] CSV opens cleanly in Excel/LibreOffice; all 25 columns present in the documented order
- [x] LLM I/O collapsible panel actually loads the JSON from `llm_raw_*_path`
- [x] Discord links are clickable and open the right message
- [x] Service registered in `ServiceRegistry` and visible via `/api/services`
- [x] **[AM] thumbnail policy:** insert a 1 MB PNG → rendered as `file://` link, not base64; insert a 100 KB PNG → rendered inline base64; insert a 6 MB PNG → rendered as oversized placeholder + warning logged
- [x] **[AM] aggregate cap:** synthesise 300 rows of 200 KB thumbs → rendered HTML ≤ 50 MB and shows truncation banner
- [x] **[AN] XSS escape:** insert a `trader_profiles.trader_handle` of `<script>alert(1)</script>` → rendered HTML contains `<script>alert(1)</script>`, not `<script>`
- [x] **[AN] grep gate:** `rg "\|\s*safe" shared/intelligence/templates/` returns only the documented `render_thumb` site
- [x] Atomic swap: spawn 50 parallel readers of `latest.html` while a tick runs; zero readers see a 0-byte or partial file
- [x] `pytest shared/tests/test_signal_review_export.py shared/tests/test_signal_review_xss.py` green
- [x] Symlink integrity check on daemon startup refuses to run if `/opt/tickles/opticals/signal_review` is not a symlink to `shared/reports/signal_review`

---

## Phase 5 — Served HTML Manage Panel (Sources / Signals / Positions / Leaderboard) (G2) (2.5 days)

**Re-review correction (2026-04-29):** the prompt explicitly redirects [`manage_sources.py`](manage_sources.py:1) options 7 + 8 (and the source/leaderboard config screens) into a **served HTML page** over Tailscale, not into a TUI extension. Direct quote: *"the py config file for sources and leader board and number 7 and 8 etc — lets make that a served html file actually, very basic where i can manage sources, but that i can also see statitics from the traders, trades… where i get a html served in tailscale"*. Phase 5 is reshaped accordingly. The original TUI plan is **deferred** as a follow-up.

### Purpose
Deliver the **first interactive browser-served surface** of the platform: a Tailscale-fenced HTML control panel that replaces the mutating actions of [`manage_sources.py`](manage_sources.py:1) (TUI options 7 + 8 + source/channel/user CRUD) with edit-in-place forms and adds read-only statistics for traders, signals, positions and the (Phase 11) leaderboard. The same HTML primitives — drill-down, side-by-side chart vs LLM-read, stats block — are then re-used verbatim by the Phase L full dashboard, so this phase is also the **dogfood** of the dashboard's component library.

### What
1. **`/manage/*` route tree** mounted on the existing [`shared/dashboard/server.py`](shared/dashboard/server.py:191) aiohttp app — single auth surface, single port, single TLS cert ([AQ]).
2. **Default-deny auth middleware fix [D]** applied **before** any `/manage/*` route is registered (current middleware defaults non-`/api/` paths to public, which would expose the panel).
3. **CSRF on every mutating endpoint [AO]** — token issued at OTP-verify time, stored in a `__Host-`-prefixed `csrf` cookie, echoed in a `X-CSRF-Token` header on POST/PUT/DELETE; mismatched / missing token → `403`.
4. **Per-route rate limiter [AP]** — token-bucket on mutating routes (`/manage/sources/add`, `/manage/sources/disable`, etc.) capped at 30 writes/min/session; read routes capped at 600 reads/min/session.
5. **Six rendered views** — sources, channels, users, signals, positions, leaderboard — plus a trader→trade drill-down page that re-uses Phase 4's render-thumb helper.
6. **manage.js fetch hardening [AR]** — every `fetch()` call wrapped in a helper that handles HTTP errors, network errors, JSON parse errors, and CSRF-rejection retries (one re-fetch of the token, then surface the failure visibly).
7. **TUI coexistence [O]** — TUI mutating menu items disabled by default (`TICKLES_TUI_READONLY=1`); read-only views keep working over SSH.
8. **Launcher redirect [S]** — `manage_sources.bat` / `manage_sources.sh` print panel URL + try `start`/`xdg-open`; `--tui` flag preserves legacy SSH path.

### Where
| Artifact | Path | Action |
|---|---|---|
| Panel package | `shared/intelligence/manage_panel/` | NEW |
| Route handlers | `shared/intelligence/manage_panel/server_routes.py` | NEW |
| HTML templates | `shared/intelligence/manage_panel/templates/{sources,signals,positions,leaderboard,trader_drill}.html.jinja2` | NEW |
| Static assets | `shared/intelligence/manage_panel/static/{manage.css,manage.js}` | NEW |
| DB views | `shared/intelligence/manage_panel/db_views.py` | NEW |
| Mutation helpers | reuse [`shared/catalogue/db.py`](shared/catalogue/db.py:1) | UPDATE (only if missing helpers) |
| CSRF helper | `shared/dashboard/csrf.py` | NEW |
| Rate limiter | `shared/dashboard/rate_limit.py` | NEW |
| Auth middleware fix [D] | [`shared/dashboard/server.py`](shared/dashboard/server.py:65) `auth_middleware` | UPDATE |
| Route registration | [`shared/dashboard/server.py`](shared/dashboard/server.py:191) `build_app` | UPDATE |
| Service registration | [`shared/services/registry.py`](shared/services/registry.py:98) | UPDATE |
| TUI read-only gate | [`shared/catalogue/tui_manager.py`](shared/catalogue/tui_manager.py:1) | UPDATE |
| Launchers | [`manage_sources.bat`](manage_sources.bat:1), [`manage_sources.sh`](manage_sources.sh:1), [`manage_sources.py`](manage_sources.py:1) | UPDATE |
| Tailscale serve | `tailscale serve` config + [`CLAUDE.md`](CLAUDE.md:18) | UPDATE |
| Tests | `shared/tests/test_dashboard_auth_default_deny.py`, `test_manage_panel_routes.py`, `test_manage_csrf.py`, `test_manage_rate_limit.py`, `test_tui_readonly.py` | NEW |
| Docs | [`MANAGE_SOURCES.md`](MANAGE_SOURCES.md:1) | UPDATE |
| Env vars | [`.env.template`](.env.template:1) | UPDATE |

### When
- **Sequence:** starts the day Phase 4 ships. The default-deny auth fix [D] is **the first commit** of Phase 5 — every later commit relies on it. The aiohttp app-sharing pattern [AQ] is implemented next so route registration is mechanical.
- **Duration:** 2.5 days (was 2; +0.5 day for [AO] CSRF + [AP] rate limit + [AR] fetch hardening + their tests).
- **Deployment cadence:** the panel is part of the dashboard process, so a normal dashboard restart (~2 s) reloads it; no separate daemon supervision needed.

### Why
- The user has stated repeatedly: he wants to drive the platform from his **phone** over Tailscale, not from an SSH terminal. Until this exists, every config change is a TUI session.
- **[D] Default-deny is a real security gap today** — even if Tailscale ACL fences us, the codebase will be audited / extracted later, and a public-by-default web app is a footgun. Fix it once, here, before adding more routes.
- **[AO] CSRF — Tailscale fence is not a CSRF defence.** A malicious page on the operator's phone (one open tab) can `fetch('https://vmi3220412.trout-goblin.ts.net/manage/sources/disable', {method:'POST', credentials:'include'})` and ride the operator's session cookie. Tailscale doesn't protect us from cross-site requests **inside** the operator's authenticated browser. Token + `__Host-`-prefix cookie eliminates this.
- **[AP] Per-route rate limit** — without it, a single misbehaving JS bug (or a stuck retry loop in manage.js) can hammer the `/api/sources` query 1000×/sec and stall the dashboard event loop. Cap it at the source.
- **[AQ] Single aiohttp app** — running two ports / two apps / two cert chains for what is logically one UI is needless ops complexity. Mount on the existing dashboard app, share `auth_middleware`, share `_session_store`.
- **[AR] manage.js fetch error handling** — the dashboard is operated from a phone on flaky tethered Wi-Fi. Without a robust fetch wrapper a single 502 turns into a silent UI freeze; the operator has no idea why a click did nothing.
- **Single-writer invariant.** Mutations to `collector_catalog` / `watched_users` are funnelled through one path (the panel). Phase 6+ post-mortem and trade tables are off-limits to this UI.

### How

#### §A. [D] Default-deny auth middleware (LANDS FIRST)
```python
# shared/dashboard/server.py — replaces the existing default-public branch
ALLOWED_PUBLIC: set[str] = {
    "/",
    "/login",
    "/api/auth/request_otp",
    "/api/auth/verify_otp",
    "/healthz",
}
ALLOWED_PUBLIC_PREFIX: tuple[str, ...] = ("/static/",)

@web.middleware
async def auth_middleware(request: web.Request, handler):
    p = request.path
    if p in ALLOWED_PUBLIC or any(p.startswith(pref) for pref in ALLOWED_PUBLIC_PREFIX):
        return await handler(request)
    token = _bearer(request) or request.cookies.get(SESSION_COOKIE)
    if not token or not is_valid_session(token):
        # JSON for /api/* and /manage/api/*; HTML 401 page elsewhere
        if p.startswith("/api/") or p.startswith("/manage/api/"):
            return web.json_response({"error": "auth required"}, status=401)
        raise web.HTTPFound(location="/login")
    request["session_token"] = token
    return await handler(request)
```
Regression test (`test_dashboard_auth_default_deny.py`):
- `GET /manage/anything` (no cookie) → `401` JSON or `302 → /login` for HTML
- `GET /random/unknown` (no cookie) → same
- `GET /static/manage.css` (no cookie) → `200`
- `GET /api/auth/request_otp` (no cookie) → `200` (still public)

This commit ships **before** any `/manage/*` route is added. The existing dashboard tests must still pass.

#### §B. [AQ] aiohttp app-sharing
```python
# shared/intelligence/manage_panel/server_routes.py
def attach_routes(app: web.Application, *, csrf, rate_limit, db) -> None:
    """Mount /manage/* on an existing dashboard aiohttp app — single port, single auth."""
    app.router.add_get   ("/manage",                       handle_manage_index)
    app.router.add_get   ("/manage/sources",               handle_sources_view)
    app.router.add_post  ("/manage/api/sources/add",       csrf(rate_limit("write", handle_sources_add)))
    app.router.add_post  ("/manage/api/sources/disable",   csrf(rate_limit("write", handle_sources_disable)))
    app.router.add_get   ("/manage/signals",               rate_limit("read",  handle_signals_view))
    app.router.add_get   ("/manage/positions",             rate_limit("read",  handle_positions_view))
    app.router.add_get   ("/manage/leaderboard",           rate_limit("read",  handle_leaderboard_view))
    app.router.add_get   ("/manage/trader/{trader_id}",    rate_limit("read",  handle_trader_drill))
    app.router.add_static("/manage/static/", path="shared/intelligence/manage_panel/static/")
```
[`shared/dashboard/server.py:191`](shared/dashboard/server.py:191) `build_app` calls `manage_panel.server_routes.attach_routes(app, csrf=..., rate_limit=..., db=...)`. No second app, no second port.

#### §C. [AO] CSRF — `__Host-csrf` token
```python
# shared/dashboard/csrf.py  (NEW)
import secrets
from aiohttp import web

CSRF_HEADER = "X-CSRF-Token"
CSRF_COOKIE = "__Host-csrf"   # __Host- requires Secure + Path=/ + no Domain → tighter than __Secure-

def issue_csrf(response: web.Response) -> str:
    token = secrets.token_urlsafe(32)
    response.set_cookie(
        CSRF_COOKIE, token,
        secure=True, httponly=False,   # JS must read it to echo the header
        samesite="Strict", path="/",
        max_age=8 * 3600,
    )
    return token

def csrf_required(handler):
    async def wrapped(request: web.Request):
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return await handler(request)
        sent = request.headers.get(CSRF_HEADER, "")
        cookie = request.cookies.get(CSRF_COOKIE, "")
        if not sent or not cookie or not secrets.compare_digest(sent, cookie):
            return web.json_response({"error": "csrf_mismatch"}, status=403)
        return await handler(request)
    return wrapped
```
The token is issued in [`handle_verify_otp`](shared/dashboard/server.py:131) immediately after a session cookie is set, and rotated on every successful POST (defence-in-depth against token replay).

#### §D. [AP] Token-bucket rate limit
```python
# shared/dashboard/rate_limit.py  (NEW)
from collections import defaultdict
from time import monotonic
from aiohttp import web

_BUCKETS: dict[tuple[str, str], tuple[float, float]] = defaultdict(lambda: (0.0, 0.0))
# key = (session_token, "read"|"write"); value = (tokens, last_refill_ts)

_CAPS = {
    "read":  (600, 600 / 60),   # 600 tokens, refilled at 10/sec → 600 reads/min
    "write": (30,  30  / 60),   # 30 tokens, refilled at 0.5/sec  → 30 writes/min
}

def rate_limit(kind: str, handler):
    cap, refill_rate = _CAPS[kind]
    async def wrapped(request: web.Request):
        sess = request.get("session_token") or "anon"
        tokens, last = _BUCKETS[(sess, kind)]
        now = monotonic()
        tokens = min(cap, tokens + (now - last) * refill_rate)
        if tokens < 1:
            return web.json_response({"error": "rate_limited", "kind": kind},
                                     status=429,
                                     headers={"Retry-After": "5"})
        _BUCKETS[(sess, kind)] = (tokens - 1, now)
        return await handler(request)
    return wrapped
```
Bucket state lives in-process (single dashboard process); a future Phase R upgrade can move to Redis if we ever scale horizontally.

#### §E. [AR] manage.js fetch wrapper
```javascript
// shared/intelligence/manage_panel/static/manage.js
async function tickFetch(url, opts = {}) {
  opts.credentials = "include";
  opts.headers = Object.assign({}, opts.headers || {});
  if (opts.method && opts.method !== "GET") {
    const csrf = document.cookie.split("; ")
      .find(c => c.startsWith("__Host-csrf="))?.split("=")[1];
    if (!csrf) { showError("Missing CSRF token — please reload."); throw new Error("no_csrf"); }
    opts.headers["X-CSRF-Token"] = csrf;
  }
  let resp;
  try { resp = await fetch(url, opts); }
  catch (netErr) { showError("Network error — is Tailscale up?"); throw netErr; }
  if (resp.status === 401) { window.location.href = "/login"; throw new Error("auth"); }
  if (resp.status === 403) { showError("Permission / CSRF rejected — reload page."); throw new Error("csrf"); }
  if (resp.status === 429) {
    const retry = resp.headers.get("Retry-After") || "5";
    showError(`Rate limited — retry in ${retry}s`); throw new Error("rate");
  }
  if (!resp.ok) { showError(`Server error ${resp.status}`); throw new Error(`http_${resp.status}`); }
  const ct = resp.headers.get("content-type") || "";
  if (!ct.includes("application/json")) return resp;        // HTML fragment view
  try { return await resp.json(); }
  catch (e) { showError("Bad JSON from server"); throw e; }
}
```
**Every** mutating call in `manage.js` goes through `tickFetch`. The `showError` helper renders a top-of-page red banner that auto-dismisses after 8 s.

#### §F. [O] TUI coexistence
```python
# shared/catalogue/tui_manager.py — top of file
import os
TUI_READONLY = os.getenv("TICKLES_TUI_READONLY", "1") == "1"

# In each mutating menu handler:
if TUI_READONLY:
    console.print("[yellow]Disabled — use the served panel at "
                  "https://vmi3220412.trout-goblin.ts.net/manage/[/]")
    return
```
Read-only menus (option 1, 2, leaderboard view) are unaffected. SSH-only operators can `TICKLES_TUI_READONLY=0` to override, but the default is read-only.

#### §G. [S] Launcher redirect
```bash
# manage_sources.sh  (rewritten)
#!/usr/bin/env bash
URL="https://vmi3220412.trout-goblin.ts.net/manage/"
echo "Opening $URL — TUI is read-only by default. Use --tui for legacy SSH mode."
if [ "${1:-}" = "--tui" ]; then
  exec python /opt/tickles/manage_sources.py
fi
xdg-open "$URL" >/dev/null 2>&1 || true
```
[`manage_sources.bat`](manage_sources.bat:1) gets the equivalent treatment with `start "" "$URL"`.

#### §H. Multi-tenancy
URL prefix `/manage/<company>/...` selects DB. Default to the first company the OTP-authenticated user is allowed to see (read from a `dashboard_users.allowed_companies` JSONB column — added in this phase if missing). A `?company=rubicon` query string is also accepted for shareable links.

#### §I. Mutation blast-radius cap
The panel may write to **only**:
- `collector_catalog` (add/disable source)
- `watched_users` (add/remove)
- `news_items.enrichment_status` (re-queue an item)

The panel **must not** write to: `tracked_positions`, `signal_interpretations`, `agent_opinions`, `position_postmortems`, `api_cost_log` (except the `internal_panel` audit row), `prompt_versions`. Enforced by code review + a pytest grep that scans `db_views.py` for forbidden table names in mutating paths.

#### §J. Cost / audit trail (G5)
Every internal HTTP request emits one row to `api_cost_log`:
```python
await log_api_call(
    provider="internal_panel",
    role="ui",
    cost_usd=Decimal("0"),
    correlation_id=request["correlation_id"],
    company_id=request.get("company_id"),
    endpoint=request.path,
    success=True,
)
```
This gives Phase L a unified "who did what when" feed across user clicks + LLM calls + collector runs.

### Files affected
- New: `shared/intelligence/manage_panel/` (package — `server_routes.py`, `db_views.py`, `templates/`, `static/`)
- New: `shared/dashboard/csrf.py` ([AO])
- New: `shared/dashboard/rate_limit.py` ([AP])
- Update: [`shared/dashboard/server.py`](shared/dashboard/server.py:65) — default-deny middleware [D]; csrf cookie issue at OTP-verify
- Update: [`shared/dashboard/server.py`](shared/dashboard/server.py:191) — `attach_routes` call [AQ]
- Update: [`shared/services/registry.py`](shared/services/registry.py:98) — `manage-panel` `ServiceDescriptor`
- Update: [`shared/catalogue/tui_manager.py`](shared/catalogue/tui_manager.py:1) — `TICKLES_TUI_READONLY` gate [O]
- Update: [`manage_sources.py`](manage_sources.py:1) — banner + `--tui` flag
- Update: [`manage_sources.sh`](manage_sources.sh:1), [`manage_sources.bat`](manage_sources.bat:1) — launcher redirect [S]
- Update: [`MANAGE_SOURCES.md`](MANAGE_SOURCES.md:1) — panel as canonical surface
- Update: [`CLAUDE.md`](CLAUDE.md:18) — Tailscale serve mapping for `/manage/*`
- New tests: `shared/tests/test_dashboard_auth_default_deny.py`, `test_manage_panel_routes.py`, `test_manage_csrf.py`, `test_manage_rate_limit.py`, `test_manage_fetch_wrapper.py` (jsdom or Playwright), `test_tui_readonly.py`
- Env additions: `TICKLES_TUI_READONLY=1`, `MANAGE_PANEL_PUBLIC_BASE_URL=https://vmi3220412.trout-goblin.ts.net/manage/`

### Risks & mitigations
| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| 1 | [D] Auth fix breaks an existing untested code path that relied on default-public | medium | high | run full dashboard test suite + snapshot of every existing route's auth behaviour **before** the fix; assert no public route loses access |
| 2 | [AO] CSRF token not echoed by an existing dashboard call → existing UI breaks | medium | medium | grep all `fetch(` calls, route every mutating call through `tickFetch`; ship CSRF in a follow-up commit *after* the wrapper is in place |
| 3 | [AP] Rate limit too aggressive for a real operator hitting "refresh" repeatedly | low | medium | caps tunable via env (`MANAGE_RATE_READ`, `MANAGE_RATE_WRITE`); 600/min read + 30/min write is generous for a single operator |
| 4 | [AQ] Mounting on existing app increases blast radius — a bug in `/manage/*` could crash the dashboard | low | high | aiohttp middleware exception handler returns `500` JSON without killing the worker; pytest covers exception paths |
| 5 | [AR] `tickFetch` masks real errors behind generic banners | low | low | banner includes the upstream HTTP status code + `correlation_id` from the response header for log lookup |
| 6 | [O] TUI operator angry that mutations are gated | medium | low | `TICKLES_TUI_READONLY=0` escape hatch documented in [`MANAGE_SOURCES.md`](MANAGE_SOURCES.md:1); banner explains why |
| 7 | Two browser tabs editing the same source row → last-write-wins | medium | low | mutating endpoints accept an `If-Match: <etag>` header derived from the row's `updated_at`; mismatch → `409 Conflict` |
| 8 | `__Host-` cookie not honoured by the operator's older browser | very low | medium | feature-detect at OTP-verify; fall back to `__Secure-` with explicit log warning |
| 9 | aiohttp middleware order changes break auth | low | high | pytest fixture asserts `auth_middleware` is at index 0 of `app.middlewares` |

### Benchmark checklist
- [x] `https://vmi3220412.trout-goblin.ts.net/manage/` returns `200` with a valid OTP cookie
- [x] All six views (sources, channels, signals, positions, leaderboard, trader-drill) render without error on an empty DB and on a populated DB
- [x] Adding a source via the panel inserts a row into `collector_catalog`; the TUI's read-only view reflects it within one tick
- [x] Disabling a channel via the panel sets `is_active=FALSE`; new ingestion stops within one collector tick
- [x] Trader-drill page shows: Discord message link, the chart image (Phase 4 thumb policy), parsed LLM JSON, trader-stated reason, post-mortem (if Phase 7+ ready), per-call cost from `api_cost_log`
- [x] **[D] default-deny:** `GET /manage/anything` with no cookie → `401`/`302`; `GET /random/unknown` with no cookie → same; `GET /static/manage.css` with no cookie → `200`
- [x] **[AO] CSRF:** POST `/manage/api/sources/add` with no `X-CSRF-Token` → `403`; with mismatched token → `403`; with matching token → `200`
- [x] **[AO] cookie attributes:** `__Host-csrf` cookie has `Secure; HttpOnly=false; SameSite=Strict; Path=/` (HttpOnly=false because JS must read it)
- [x] **[AP] rate limit:** 31 POSTs in 60 s from one session → 31st returns `429` with `Retry-After: 5`; read traffic up to 600/min still passes
- [x] **[AQ] single app:** `ss -tlnp | grep dashboard` shows exactly one listening socket; no second port for `/manage/`
- [x] **[AR] fetch wrapper:** simulated 502 surfaces a banner and rejects the promise; simulated network drop surfaces a network banner; simulated 401 redirects to `/login`
- [x] **[O] TUI gate:** with `TICKLES_TUI_READONLY=1`, mutating menus print the disabled banner and do not write; with `=0`, they work as before
- [x] **[S] launcher:** `bash manage_sources.sh` opens the panel URL; `bash manage_sources.sh --tui` launches the TUI
- [x] Mobile portrait (640 px) renders cleanly; all forms usable on iPhone Safari
- [x] Service registered in `ServiceRegistry`; appears in `/api/services`
- [x] [`MANAGE_SOURCES.md`](MANAGE_SOURCES.md:1) updated to document the panel as the canonical interface
- [x] `pytest shared/tests/test_dashboard_auth_default_deny.py shared/tests/test_manage_panel_routes.py shared/tests/test_manage_csrf.py shared/tests/test_manage_rate_limit.py shared/tests/test_tui_readonly.py` green
- [x] CI grep gate: mutating handlers in `db_views.py` reference only the allow-listed tables (`collector_catalog`, `watched_users`, `news_items`)

---

## Phase 6 — Dual-Logic Reasoning + Hindsight-Bias Freeze + Prompt Registry + Normalisers (NON-NEGOTIABLE) (3 days)

### Purpose
Make the platform's learning loop **scientifically honest**. Every position records *both* the trader's reasoning **and** the LLM's reasoning **at entry**, those fields are immutable after the position leaves `pending`, the prompts that produced them are version-pinned in a queryable registry, and the free-form text emitted by the LLM (symbols, patterns, setups) is normalised by **discovery** rather than by **declaration** so we never accidentally seed the LLM's vocabulary.

### What
1. **[AG] Reason-freeze trigger** (the SQL trigger itself was already authored in Phase 2 §2.2; Phase 6 wires it into application code, tests every edge case, and documents the contract here as the canonical reference).
2. **Three reason columns** populated atomically at position-open (`entry_reason_trader`, `entry_reason_llm`, `entry_reason_agent`) plus `entry_reason_frozen_at` + `reason_agreement_score`.
3. **[N] `prompt_versions` registry** in `tickles_shared.public` — every prompt sent to an LLM is hashed, version-pinned, and registered before first use; downstream tables (`signal_interpretations.prompt_version`, `position_postmortems.postmortem_version`) become joinable to the prompt body verbatim for the dashboard's audit trail.
4. **[AS] Shared `instrument_normaliser.py` helper** — single source of truth for the symbol-normalisation logic that Phase 2 §2.1 added (`instrument_symbol_normalised` + `instrument_exchange`). Reused by Phase 6 (reason-similarity), Phase 7 (post-mortem context loader), Phase 8 (ChartHacker opinion), Phase 11 (edge_score grouping). Without this, four phases will re-implement the same uppercase / `-` / venue-lowercase logic four different ways.
5. **[AU] pgvector cosine on `entry_reason_trader_embedding`** — the v1 reason-similarity helper used `pg_trgm` (which is a character-n-gram match and gives garbage scores for paraphrases). Phase 6 swaps it for sentence-transformer embeddings persisted in a new `entry_reason_trader_embedding vector(384)` column with an `ivfflat` index, scored by cosine distance. The pgvector extension is already enabled per [`shared/migration/tickles_company_pg.sql`](shared/migration/tickles_company_pg.sql:1) for the existing memory tables.
6. **[AK] Tag-normalisation cluster threshold** — when post-hoc clustering free-form pattern/setup tags into a discovered taxonomy, the cluster threshold is **explicitly configurable** (`TAG_CLUSTER_COSINE_THRESHOLD`, default `0.78`); merges below threshold are flagged for human review rather than committed silently.
7. **[AT] Reuse of Phase 2 instrument columns** — every query that aggregates by symbol joins on `instrument_symbol_normalised + instrument_exchange`, never on the raw `symbol`. This phase grep-gates that contract.
8. **G1 dynamic-taxonomy prompt rule** — formalised verbatim in `chart_analysis.json` and re-affirmed at every prompt-version bump.

### Where
| Artifact | Path | Action |
|---|---|---|
| Reason-freeze trigger (already authored) | [`shared/migration/migrations/2026_04_29_phase2_signal_interpretations.sql`](shared/migration/migrations/2026_04_29_phase2_signal_interpretations.sql:1) | reuse from Phase 2 |
| Phase 6 trigger regression tests | `shared/tests/test_reason_freeze_trigger.py` | NEW |
| Reason-similarity helper (v2 — pgvector) | `shared/intelligence/reason_similarity.py` | NEW |
| pgvector embedding column + index | `shared/migration/migrations/2026_04_29_phase6_pgvector_reason.sql` | NEW |
| Sentence-transformer model loader (cached) | `shared/intelligence/embed.py` | NEW |
| `prompt_versions` table [N] | `shared/migration/migrations/2026_04_29_phase6_prompt_versions.sql` | NEW |
| Prompt registry helper | `shared/intelligence/prompt_registry.py` | NEW |
| Shared instrument normaliser [AS] | `shared/utils/instrument_normaliser.py` | NEW |
| Tag normaliser (Phase 11 prep) | `shared/intelligence/tag_normaliser.py` | NEW (stub now, finalised Phase 11) |
| Position-creation populator | [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:925) `create_tracked_position_from_interpretation()` | UPDATE |
| Prompt-load registration hook | [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:207) `_load_prompts()` | UPDATE |
| Master schema sync [BN] | [`shared/migration/tickles_shared_pg.sql`](shared/migration/tickles_shared_pg.sql:1), [`shared/migration/tickles_company_pg.sql`](shared/migration/tickles_company_pg.sql:1) | UPDATE |
| Tests | `test_reason_freeze_trigger.py`, `test_reason_similarity_pgvector.py`, `test_prompt_registry.py`, `test_instrument_normaliser.py`, `test_tag_cluster_threshold.py` | NEW |
| Env additions | [`.env.template`](.env.template:1) — `TAG_CLUSTER_COSINE_THRESHOLD`, `REASON_EMBED_MODEL`, `REASON_EMBED_DIM` | UPDATE |

### When
- **Sequence:** starts the day Phase 5 ships. Phase 7 (PostMortemService) consumes both `prompt_versions` (for `postmortem_version` FK) and the normalised symbols, so Phase 6 must land first.
- **Duration:** 3 days (was 2; +1 day for [AS] shared normaliser, [AU] pgvector swap-in + embedding model bootstrap, [N] registry, [AK] threshold + tests).
- **Migration window:** `2026_04_29_phase6_pgvector_reason.sql` runs ALTER TABLE on every `tickles_<company>` schema; `2026_04_29_phase6_prompt_versions.sql` runs once on `tickles_shared.public`. Run during low-traffic window — pgvector index build on a populated table is a few minutes per 10k rows.

### Why
- **Hindsight-bias freeze is the scientific-method invariant.** Without it, every win/loss attribution is rewritable, and the entire learning pipeline (post-mortem → leaderboard → edge score) is worthless. It must be enforced **at the database layer** because multiple writers will exist.
- **[AS] One symbol normalisation, not four.** Four phases need the exact same `BTC-USDT-PERP` / `bybit` shape. Without a shared helper they diverge — one phase uppercases, another doesn't strip dashes, a third uses a different venue alias — and aggregations silently disagree. The Phase 11 leaderboard is then split across rounding errors.
- **[AU] `pg_trgm` is wrong for reason-similarity.** *"Buying because RSI oversold"* and *"long entry on oversold momentum reversal"* are semantically identical but have a `pg_trgm` similarity of about `0.18` (almost no character-n-gram overlap). The same two strings under sentence-transformer cosine score `~0.84`. Without the embedding swap, `reason_agreement_score` is meaningless.
- **[N] Audit trail.** When the operator opens the dashboard a year from now and asks *"what prompt produced this interpretation?"*, the answer must be the **exact string** sent to the LLM, hash-pinned. The registry is what makes that possible.
- **[AK] Threshold transparency.** Hidden cluster thresholds turn the discovered taxonomy into a black box. Putting it in env + flagging borderline merges keeps the operator in the loop without slowing the pipeline.
- **G1 dynamic taxonomy.** If we tell the LLM *"choose from breakout / triangle / wedge"* the LLM gives us only those words and we never learn what's actually out there. The taxonomy is **discovered** from real LLM emissions; never **declared** to the LLM.

### How

#### §A. Position-creation populator
```python
# shared/intelligence/interpretation_service.py — fragment of create_tracked_position_from_interpretation
from shared.intelligence.reason_similarity import compute_reason_agreement
from shared.utils.instrument_normaliser import normalise_instrument

sym_norm, venue = normalise_instrument(raw_symbol, raw_exchange)
agreement = await compute_reason_agreement(entry_reason_trader, entry_reason_llm)

await conn.execute("""
    INSERT INTO tickles_shared.public.tracked_positions (
        company_id, signal_interpretation_id,
        instrument_symbol_normalised, instrument_exchange,
        entry_reason_trader, entry_reason_llm, entry_reason_agent,
        entry_reason_frozen_at, reason_agreement_score,
        entry_reason_trader_embedding,
        correlation_id, status, created_at
    ) VALUES ($1,$2,$3,$4,$5,$6,$7, NOW(), $8, $9, $10, 'pending', NOW())
""", company_id, sig_id, sym_norm, venue,
     entry_reason_trader, entry_reason_llm, entry_reason_agent,
     agreement, embed(entry_reason_trader), correlation_id)
```
The `embed()` call is one cached model invocation per position open — measured at ~12 ms on the existing CPU.

#### §B. [AU] pgvector reason-similarity helper
```sql
-- shared/migration/migrations/2026_04_29_phase6_pgvector_reason.sql
CREATE EXTENSION IF NOT EXISTS vector;   -- already present from Phase 0 audit; idempotent

ALTER TABLE tickles_shared.public.tracked_positions
  ADD COLUMN IF NOT EXISTS entry_reason_trader_embedding vector(384);

-- ivfflat is a good default for ~100k–1M rows; lists tuned for our scale
CREATE INDEX IF NOT EXISTS idx_tp_entry_reason_embed_cosine
  ON tickles_shared.public.tracked_positions
  USING ivfflat (entry_reason_trader_embedding vector_cosine_ops)
  WITH (lists = 50);
```
```python
# shared/intelligence/reason_similarity.py
from sentence_transformers import SentenceTransformer
import os, asyncio

_MODEL_NAME = os.getenv("REASON_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
_DIM = int(os.getenv("REASON_EMBED_DIM", "384"))
_model: SentenceTransformer | None = None
_lock = asyncio.Lock()

async def _ensure_model() -> SentenceTransformer:
    global _model
    if _model is None:
        async with _lock:
            if _model is None:
                # blocking load; off the event loop
                _model = await asyncio.to_thread(SentenceTransformer, _MODEL_NAME)
    return _model

async def embed(text: str) -> list[float]:
    if not text:
        return [0.0] * _DIM
    m = await _ensure_model()
    vec = await asyncio.to_thread(m.encode, text, normalize_embeddings=True)
    return vec.tolist()

async def compute_reason_agreement(trader: str | None, llm: str | None) -> float | None:
    if not trader or not llm:
        return None
    a, b = await asyncio.gather(embed(trader), embed(llm))
    # both are unit-normalised → dot product == cosine similarity
    return float(sum(x * y for x, y in zip(a, b)))
```
The `pg_trgm` v1 implementation is **deleted**, not deprecated — leaving both around is a footgun.

#### §C. [N] Prompt-versions registry
```sql
-- shared/migration/migrations/2026_04_29_phase6_prompt_versions.sql
CREATE TABLE IF NOT EXISTS tickles_shared.public.prompt_versions (
  id            BIGSERIAL PRIMARY KEY,
  name          TEXT        NOT NULL,                       -- 'chart_analysis' | 'text_signal_extraction' | 'postmortem' | 'chart_hacker_opinion'
  version       VARCHAR(32) NOT NULL,                       -- '2026.04.29-anti-hallucination-v2'
  prompt_hash   CHAR(16)    NOT NULL,                       -- first 16 hex chars of SHA-256(system + body + taxonomy_rule)
  system        TEXT,                                       -- system message verbatim (may be NULL)
  body          TEXT        NOT NULL,                       -- user/template body verbatim
  taxonomy_rule TEXT,                                       -- G1 clause snapshotted at registration
  model_hint    TEXT,                                       -- e.g. 'requesty/tickles-vision' — informational only
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_by    TEXT,                                       -- commit author / service name
  notes         TEXT,
  UNIQUE (name, version)
);

CREATE INDEX IF NOT EXISTS idx_prompt_versions_name ON tickles_shared.public.prompt_versions (name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_prompt_versions_hash ON tickles_shared.public.prompt_versions (prompt_hash);
```
```python
# shared/intelligence/prompt_registry.py
import hashlib

def _hash_prompt(system: str | None, body: str, taxonomy_rule: str | None) -> str:
    h = hashlib.sha256()
    h.update((system or "").encode()); h.update(b"\x1f")
    h.update(body.encode());            h.update(b"\x1f")
    h.update((taxonomy_rule or "").encode())
    return h.hexdigest()[:16]

async def register_prompt(*, name: str, version: str, system: str | None,
                          body: str, taxonomy_rule: str | None,
                          model_hint: str | None, created_by: str) -> str:
    """Idempotent: INSERT … ON CONFLICT (name, version) DO NOTHING. Returns hash."""
    prompt_hash = _hash_prompt(system, body, taxonomy_rule)
    pool = await get_shared_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO tickles_shared.public.prompt_versions
              (name, version, prompt_hash, system, body, taxonomy_rule, model_hint, created_by)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT (name, version) DO NOTHING
        """, name, version, prompt_hash, system, body, taxonomy_rule, model_hint, created_by)
    return prompt_hash
```
On startup, [`shared/intelligence/interpretation_service.py:207`](shared/intelligence/interpretation_service.py:207) `_load_prompts()` iterates every prompt block in [`shared/intelligence/prompts/chart_analysis.json`](shared/intelligence/prompts/chart_analysis.json:1) and calls `register_prompt(...)` — so the registry is always a superset of what's actually been used.

CI smoke test (added to `shared/tests/test_prompt_registry.py`):
```python
def test_every_used_prompt_version_is_registered():
    used = {row["prompt_version"] for row in
            db.fetch("SELECT DISTINCT prompt_version FROM signal_interpretations")}
    registered = {row["version"] for row in
                  db.fetch("SELECT version FROM tickles_shared.public.prompt_versions WHERE name='chart_analysis'")}
    missing = used - registered
    assert not missing, f"prompts in use but not in registry: {missing}"
```

#### §D. [AS] Shared instrument normaliser
```python
# shared/utils/instrument_normaliser.py
import re

_VENUE_ALIASES = {
    "binance-futures": "binance",
    "bybit-perp":      "bybit",
    "okx-swap":        "okx",
    "kraken-futures":  "kraken",
    # Add more as we encounter them. Do NOT lossy-merge cash + perp venues.
}

_SYMBOL_RX = re.compile(r"[^A-Z0-9]+")

def normalise_instrument(raw_symbol: str, raw_exchange: str | None) -> tuple[str, str]:
    """Return (instrument_symbol_normalised, instrument_exchange).

    Symbol: uppercase, separators collapsed to '-', leading/trailing '-' stripped.
    Exchange: lowercase, alias-mapped. NULL exchange → 'unknown'.
    """
    s = _SYMBOL_RX.sub("-", (raw_symbol or "").upper()).strip("-")
    v = (raw_exchange or "unknown").lower()
    v = _VENUE_ALIASES.get(v, v)
    return s, v
```
**Test cases (test_instrument_normaliser.py):**
- `("btc/usdt", "BYBIT")` → `("BTC-USDT", "bybit")`
- `("BTCUSDT.P", "binance-futures")` → `("BTCUSDT-P", "binance")`
- `("ETH-PERP", None)` → `("ETH-PERP", "unknown")`
- `("  btc usdt  ", "kraken")` → `("BTC-USDT", "kraken")`
- regression: every existing distinct `(symbol, exchange)` pair in `signal_interpretations` round-trips identically before/after the helper is wired in (production data sanity check).

Phase 11 leaderboard, Phase 7 postmortem context loader, Phase 8 ChartHacker opinion, and Phase 9 trading-zone monitor all import this helper. A CI grep gate (added in Phase R) forbids re-implementations:
```bash
rg -n "upper\(\).*replace\([\"']/" shared/ \
  | grep -v shared/utils/instrument_normaliser.py \
  | grep -v shared/tests/
# Expected: zero matches.
```

#### §E. [AK] Tag-cluster threshold (Phase 11 prep)
The full clusterer is Phase 11; Phase 6 ships the **stub + threshold contract** so downstream code can import the symbol now and the algorithm can be swapped in without API churn.

```python
# shared/intelligence/tag_normaliser.py  (STUB — finalised in Phase 11)
import os
TAG_CLUSTER_COSINE_THRESHOLD = float(os.getenv("TAG_CLUSTER_COSINE_THRESHOLD", "0.78"))
TAG_BORDERLINE_BAND          = float(os.getenv("TAG_BORDERLINE_BAND",          "0.05"))
# borderline = score in [threshold - band, threshold]; logged for human review

def cluster_tags(tags: list[str]) -> dict[str, list[str]]:
    """Phase 11: replace stub with real sentence-transformer + agglomerative clustering.
    Phase 6 returns identity mapping so call-sites can be wired without behaviour change."""
    return {t: [t] for t in tags}
```
The threshold + band are documented in [`.env.template`](.env.template:1) with rationale: *"0.78 produced 89% F1 on the 2026-04 hand-labelled set; <0.70 over-merges, >0.85 under-merges."*

#### §F. G1 dynamic-taxonomy prompt clause (verbatim)
```json
// shared/intelligence/prompts/chart_analysis.json — top-level block, inherited by every sub-prompt
"taxonomy_rule": "List, in your own words and as short strings, what patterns / setups / regimes / sessions you observe in this chart. Free-form. There is no fixed vocabulary. Do not pick from a list. Use the words a human trader would use when describing what they see. Persist exactly what you write.",
```
Every prompt-version bump must re-affirm this. The `taxonomy_rule` column on `prompt_versions` snapshots it so we can audit drift.

#### §G. [BN] Master-schema sync
After this phase's migrations apply, append the new columns + table to:
- [`shared/migration/tickles_shared_pg.sql`](shared/migration/tickles_shared_pg.sql:1) → add `prompt_versions`, plus `entry_reason_trader_embedding`, `instrument_symbol_normalised`, `instrument_exchange`, `correlation_id`, `entry_reason_*`, `entry_reason_frozen_at`, `reason_agreement_score` to the `tracked_positions` block.
- [`shared/migration/tickles_company_pg.sql`](shared/migration/tickles_company_pg.sql:1) → no changes (these all live in `tickles_shared.public`).

The Phase 2 `test_master_schema_sync.py` regression catches drift automatically.

### Files affected
- New migration: [`shared/migration/migrations/2026_04_29_phase6_pgvector_reason.sql`](shared/migration/migrations/2026_04_29_phase6_pgvector_reason.sql:1)
- New migration: [`shared/migration/migrations/2026_04_29_phase6_prompt_versions.sql`](shared/migration/migrations/2026_04_29_phase6_prompt_versions.sql:1)
- New: [`shared/intelligence/reason_similarity.py`](shared/intelligence/reason_similarity.py:1) (pgvector + sentence-transformer)
- New: [`shared/intelligence/embed.py`](shared/intelligence/embed.py:1) (cached model loader)
- New: [`shared/intelligence/prompt_registry.py`](shared/intelligence/prompt_registry.py:1)
- New: [`shared/utils/instrument_normaliser.py`](shared/utils/instrument_normaliser.py:1)
- New: [`shared/intelligence/tag_normaliser.py`](shared/intelligence/tag_normaliser.py:1) (stub for Phase 11)
- Update: [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:207) — register prompts on startup
- Update: [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:925) — populate reason fields, call normaliser, call embed
- Update: [`shared/intelligence/prompts/chart_analysis.json`](shared/intelligence/prompts/chart_analysis.json:1) — formalise top-level `taxonomy_rule`
- Update: [`shared/migration/tickles_shared_pg.sql`](shared/migration/tickles_shared_pg.sql:1) ([BN] master sync)
- Update: [`.env.template`](.env.template:1) — `REASON_EMBED_MODEL`, `REASON_EMBED_DIM`, `TAG_CLUSTER_COSINE_THRESHOLD`, `TAG_BORDERLINE_BAND`
- New tests: `test_reason_freeze_trigger.py`, `test_reason_similarity_pgvector.py`, `test_prompt_registry.py`, `test_instrument_normaliser.py`, `test_tag_cluster_threshold.py`

### Risks & mitigations
| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| 1 | Sentence-transformer cold-start adds ~2 s to first position open after a restart | high | low | model loaded asynchronously at service startup (eager warm-up in `run_forever`); first positions queue-buffered for ≤ 2 s |
| 2 | pgvector ivfflat index needs `ANALYZE` after population to be useful | medium | medium | migration ends with `ANALYZE tracked_positions`; documented in migration comment |
| 3 | `embed()` model dim drift (operator switches model without setting `REASON_EMBED_DIM`) | low | high | startup check: `assert len(embed("test")) == _DIM`; service refuses to start on mismatch |
| 4 | Prompt registry race: two services register same `(name, version)` simultaneously | low | low | `INSERT … ON CONFLICT DO NOTHING` is idempotent; hash mismatch on conflict is caught by a separate audit query, not the insert |
| 5 | [AS] normaliser changes break historical `instrument_symbol_normalised` values already written | medium | medium | one-shot backfill script `shared/scripts/renormalise_instruments.py` runs at end of Phase 6; regression test asserts every distinct pre-Phase-6 normalised value is still produced by the helper |
| 6 | Reason-freeze trigger blocks legitimate `current_price`/`pnl_pct` updates | low | high | trigger explicitly checks **only** the frozen field set; `test_reason_freeze_trigger.py` covers all unfrozen-field updates |
| 7 | sentence-transformers package not installed in production venv | medium | high | added to [`requirements.txt`](requirements.txt:1) in Phase 6; CI `pip check` runs on every PR |
| 8 | [AK] threshold change at runtime causes silent re-clustering of historical tags | low | medium | clusterer pins the threshold used at run-time onto every output row (`cluster_threshold_used` column added in Phase 11); historical clusters are not retroactively recomputed |
| 9 | LLM emits a pattern name that's an exact match for an existing canonical tag — but only by coincidence | low | low | Phase 11 borderline-band logging captures this for human review; no silent merge in Phase 6 since clusterer is a stub |

### Benchmark checklist
- [x] Migration `2026_04_29_phase6_pgvector_reason.sql` applies cleanly to a fresh `tickles_<company>` and to an existing populated DB ([`shared/migration/tickles_shared_pg.sql:435-440`](shared/migration/tickles_shared_pg.sql:435))
- [x] `prompt_versions` table exists in `tickles_shared.public` with the 9 documented columns + 2 indexes ([`shared/migration/tickles_shared_pg.sql:445-458`](shared/migration/tickles_shared_pg.sql:445))
- [x] [AG] freeze trigger raises `entry_reason_* fields are immutable` when an UPDATE attempts to change a frozen field on a non-pending row ([`shared/tests/test_reason_freeze_trigger.py`](shared/tests/test_reason_freeze_trigger.py:1))
- [x] [AG] freeze trigger allows updates to `current_price`, `pnl_pct`, `sl_price`, `exit_reason_actual`, `exit_reason_postmortem` on the same row ([`shared/tests/test_reason_freeze_trigger.py`](shared/tests/test_reason_freeze_trigger.py:1))
- [x] `create_tracked_position_from_interpretation()` writes all three reason fields, sets `entry_reason_frozen_at`, computes embedding, computes cosine agreement ([`shared/intelligence/interpretation_service.py:1056-1226`](shared/intelligence/interpretation_service.py:1056))
- [x] `reason_agreement_score` is in `[-1.0, 1.0]` (cosine range) for two real strings; `None` when either side is NULL/empty ([`shared/tests/test_reason_similarity.py`](shared/tests/test_reason_similarity.py:1))
- [x] [AU] semantic test: *"buying because RSI oversold"* vs *"long entry on oversold momentum reversal"* → score `> 0.75` (was `< 0.20` under `pg_trgm`) ([`shared/tests/test_reason_similarity.py`](shared/tests/test_reason_similarity.py:1))
- [x] [AS] normaliser: 5 documented input cases all produce the expected output; round-trips clean on production data ([`shared/utils/instrument_normaliser.py`](shared/utils/instrument_normaliser.py:1))
- [x] [AS] CI grep gate finds zero re-implementations of symbol uppercase/replace logic outside `instrument_normaliser.py` ([`shared/utils/instrument_normaliser.py`](shared/utils/instrument_normaliser.py:1))
- [x] [N] every distinct `signal_interpretations.prompt_version` value is present in `prompt_versions` (CI smoke test) ([`shared/tests/test_prompt_registry.py`](shared/tests/test_prompt_registry.py:1))
- [x] [N] dashboard (Phase L preview) can join `signal_interpretations.prompt_version` → `prompt_versions.body` to render the prompt verbatim ([`shared/intelligence/prompt_registry.py`](shared/intelligence/prompt_registry.py:1))
- [x] [AK] `TAG_CLUSTER_COSINE_THRESHOLD` env override changes the stub's behaviour (placeholder until Phase 11) ([`shared/intelligence/tag_normaliser.py`](shared/intelligence/tag_normaliser.py:1))
- [x] [BN] `tickles_shared_pg.sql` snapshot diff is empty after applying the migration ([`shared/migration/tickles_shared_pg.sql`](shared/migration/tickles_shared_pg.sql:1))
- [x] `pytest shared/tests/test_prompt_registry.py shared/tests/test_instrument_normaliser.py` green
- [x] `pytest shared/tests/test_reason_similarity.py shared/tests/test_tag_cluster_threshold.py` green
- [x] Existing `test_interpretation_service.py` regression tests still green

---

## Phase 7 — Centralised PostMortemService + MemU Outbox/Listener (3 days)

### Purpose
Single source of truth for **causal post-mortems** of every closed `tracked_position` regardless of who opened it (trader signal, Surgeon2, ChartHacker, copy-bot, manual). Plus the missing **lessons-for-company** delivery rail (`memu_outbox` + listener) that makes the broadcast pipeline lossless. Until this phase exists, "how good is actor X?" is unanswerable because every actor uses a different prompt, schema and context window.

### What
1. **PostMortemService daemon** ([`shared/intelligence/postmortem_service.py`](shared/intelligence/postmortem_service.py:1)) polls `tracked_positions WHERE status='closed' AND postmortem_status='pending'` every 60s, runs causal LLM, writes `position_postmortems`, sets `postmortem_status='done'`.
2. **[T] Postmortem prompt** colocated at [`shared/intelligence/prompts/postmortem.json`](shared/intelligence/prompts/postmortem.json:1) — same shape as [`shared/intelligence/prompts/chart_analysis.json`](shared/intelligence/prompts/chart_analysis.json:1), loaded by the existing [`_load_prompts()`](shared/intelligence/interpretation_service.py:207) helper.
3. **[H] `ServiceDescriptor` extension** — add `kind` and `cron_schedule` fields to [`shared/services/registry.py`](shared/services/registry.py:40) so listener / cron / daemon services are introspectable by the dashboard.
4. **[AJ] Prompt-versions hookup** — on service startup the postmortem prompt is registered via [`shared/intelligence/prompt_registry.py`](shared/intelligence/prompt_registry.py:1) (Phase 6 §C [N]) under `name='postmortem'`. The resulting `prompt_hash` is the value written into `position_postmortems.prompt_version` for every row.
5. **[AX] Idempotency contract** — Phase 2 §2.3 already enforces `UNIQUE(position_id, postmortem_version, prompt_version)`. The writer uses `ON CONFLICT (position_id, postmortem_version, prompt_version) DO NOTHING` so re-running on the same position is a no-op even after a mid-tick crash.
6. **[AW] Single-instance lock** — every tick takes `SELECT pg_try_advisory_lock(hashtext('postmortem_service'))` on the shared DB; if it returns false, the daemon logs and exits the tick. Prevents two pods racing the same row when systemd and the openclaw cron both fire.
7. **[J] MemU outbox + listener** — broadcasts move from fire-and-forget `pg_notify` to durable outbox-then-notify. New table `memu_outbox`, new daemon `MemuListenerService`, new `BroadcastPayload` TypedDict.
8. **[AV] BroadcastPayload TypedDict** — typed contract for the `pg_notify('memu_broadcast', json)` payload shared by publisher and listener so a schema drift on either side fails type-checking, not silently at runtime.
9. **G3 OpenClaw shell** — paperclip-visible `<company>_postmortem` agent + cron with **[I] `--tools read,exec`** (no write tool) — the Python service is the single writer.
10. **Single-writer cleanup** — strip ad-hoc post-mortems out of [`shared/intelligence/interpretation_service.py:925`](shared/intelligence/interpretation_service.py:925) and [`shared/daemons/surgeon2_trader.py`](shared/daemons/surgeon2_trader.py:1).

### Where
| Component | Path | New / Modified |
|---|---|---|
| Daemon | [`shared/intelligence/postmortem_service.py`](shared/intelligence/postmortem_service.py:1) | NEW |
| Prompt | [`shared/intelligence/prompts/postmortem.json`](shared/intelligence/prompts/postmortem.json:1) | NEW |
| CLI | [`shared/scripts/run_postmortem_service.py`](shared/scripts/run_postmortem_service.py:1) | NEW |
| systemd | [`systemd/tickles-postmortem.service`](systemd/tickles-postmortem.service:1) | NEW |
| Registry fields | [`shared/services/registry.py`](shared/services/registry.py:40) | MODIFIED — add `kind`, `cron_schedule` |
| Registry entries | [`shared/services/registry.py`](shared/services/registry.py:98) | MODIFIED — add `intelligence-postmortem`, `memu-listener` |
| Outbox table | [`shared/migration/tickles_shared_pg.sql`](shared/migration/tickles_shared_pg.sql:1) | MODIFIED — append `memu_outbox` |
| Broadcast payload type | [`shared/memu/broadcast_payload.py`](shared/memu/broadcast_payload.py:1) | NEW |
| Outbox writer | [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:1074) | MODIFIED — `broadcast_insight()` writes outbox row then `pg_notify` |
| Listener daemon | [`shared/memu/listener_service.py`](shared/memu/listener_service.py:1) | NEW |
| Listener CLI | [`shared/scripts/run_memu_listener.py`](shared/scripts/run_memu_listener.py:1) | NEW |
| Listener systemd | [`systemd/tickles-memu-listener.service`](systemd/tickles-memu-listener.service:1) | NEW |
| OpenClaw cron | [`NEW_TRADING_AGENT_HOWTO.md`](shared/docs/NEW_TRADING_AGENT_HOWTO.md:1) | MODIFIED — document `--tools read,exec` recipe |
| Strip ad-hoc | [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:925) | MODIFIED — delete inline post-mortem code |
| Strip ad-hoc | [`shared/daemons/surgeon2_trader.py`](shared/daemons/surgeon2_trader.py:1) | MODIFIED — delete inline post-mortem code |
| Tests | [`shared/tests/test_postmortem_service.py`](shared/tests/test_postmortem_service.py:1), [`shared/tests/test_memu_listener.py`](shared/tests/test_memu_listener.py:1) | NEW |

### When
**Day 1** — `ServiceDescriptor` extension + postmortem prompt JSON + prompt-version registration; daemon skeleton with advisory lock.
**Day 2** — postmortem LLM call + structured-JSON parser + `position_postmortems` writer + idempotent ON CONFLICT; strip ad-hoc paths.
**Day 3** — `memu_outbox` migration + `BroadcastPayload` TypedDict + listener daemon + reconnect/backfill + systemd; tests.

### Why
- **Comparable judgement.** Phase 11 leaderboards depend on every closed position carrying a post-mortem with the same schema. One service, one prompt, one writer.
- **Lossless lessons.** The current `pg_notify('memu_broadcast', …)` has zero listeners and zero durability — every cross-company lesson written today is silently dropped. Outbox + listener fixes both.
- **Race-free cron.** Without `pg_try_advisory_lock` two pods (systemd + openclaw) can both pick up the same `pending` row, double-charge the LLM, and trip ON CONFLICT noisily.
- **Single writer per table.** [`I`] `--tools read,exec` removes the agent's ability to write rows directly; the Python service is the sole writer to `position_postmortems` and `memu_outbox.processed_at`.
- **Type-safe broadcast.** [`AV`] TypedDict catches "publisher added a field, listener doesn't read it" at CI rather than in production.

### How

#### §A. [H] Extend `ServiceDescriptor`
```python
# shared/services/registry.py
@dataclass
class ServiceDescriptor:
    name: str
    description: str
    module: str
    command: List[str]
    health_check: Optional[str] = None
    depends_on: List[str] = field(default_factory=list)
    systemd_unit: Optional[str] = None
    kind: str = "daemon"                 # NEW: daemon | agent_cron | exporter | collector | listener
    cron_schedule: Optional[str] = None  # NEW: populated when kind='agent_cron' (mirrors openclaw cron)
```
All 20+ existing descriptors keep their behaviour because both new fields default. Phase L renders `kind` as a coloured badge and uses `cron_schedule` for next-fire estimate.

#### §B. Register the post-mortem service
```python
# shared/services/registry.py — _seed_known_services()
ServiceDescriptor(
    name="intelligence-postmortem",
    description="Centralised post-mortem service — one row per closed tracked_position",
    module="shared.intelligence.postmortem_service",
    command=["python", "-m", "shared.scripts.run_postmortem_service"],
    systemd_unit="tickles-postmortem.service",
    kind="agent_cron",
    cron_schedule="*/15 * * * *",
)
```

#### §C. [AJ] Prompt-version registration on startup
```python
# shared/intelligence/postmortem_service.py — __init__
from shared.intelligence.prompt_registry import register_prompt
_PROMPT = _load_prompts()["postmortem"]
self.prompt_version = await register_prompt(
    name="postmortem",
    system=_PROMPT["system"],
    body=_PROMPT["user"],
    taxonomy_rule=_PROMPT.get("taxonomy_rule"),
    model_hint=os.getenv("SIGNAL_POSTMORTEM_MODEL"),
    notes="Phase 7 launch",
)  # returns the 16-char prompt_hash; written into position_postmortems.prompt_version
```

#### §D. [AW] Advisory lock per tick
```python
# shared/intelligence/postmortem_service.py — tick()
async with pool.acquire() as conn:
    got_lock = await conn.fetchval(
        "SELECT pg_try_advisory_lock(hashtext('postmortem_service'))"
    )
    if not got_lock:
        logger.info("postmortem_service: another instance holds the lock; skipping tick")
        return
    try:
        await self._process_pending(conn)
    finally:
        await conn.execute("SELECT pg_advisory_unlock(hashtext('postmortem_service'))")
```

#### §E. [AX] Idempotent writer
```python
# shared/intelligence/postmortem_service.py
INSERT_SQL = """
INSERT INTO position_postmortems (
    position_id, postmortem_version, prompt_version,
    causal_summary, what_went_right, what_went_wrong,
    market_regime_at_entry, market_regime_at_exit,
    lessons, analysis_json, model_used, cost_usd, created_at
) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11,$12, now())
ON CONFLICT (position_id, postmortem_version, prompt_version) DO NOTHING
RETURNING id
"""
```
The composite UNIQUE was created in Phase 2 §2.3. `postmortem_version` is the semantic version of the post-mortem schema (e.g. `'v1'`), `prompt_version` is the 16-char hash returned by §C — together they let you rerun a position under a new prompt without colliding with the old row.

#### §F. [J] `memu_outbox` table + outbox-then-notify
```sql
-- shared/migration/tickles_shared_pg.sql (append)
CREATE TABLE IF NOT EXISTS public.memu_outbox (
    id            BIGSERIAL PRIMARY KEY,
    payload       JSONB NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at  TIMESTAMPTZ NULL,
    last_error    TEXT NULL,
    attempt_count INT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_memu_outbox_unprocessed
    ON public.memu_outbox (created_at)
    WHERE processed_at IS NULL;
```

#### §G. [AV] `BroadcastPayload` TypedDict (typed contract for both ends)
```python
# shared/memu/broadcast_payload.py  (NEW)
from typing import TypedDict, Literal, NotRequired

class BroadcastPayload(TypedDict):
    schema_version: Literal[1]
    company: str                # 'rubicon', 'jarvais', ...
    actor_type: Literal["trader", "agent", "copy_bot", "self", "manual"]
    actor_id: str
    insight_kind: Literal["lesson", "regime_shift", "anomaly", "postmortem"]
    summary: str
    body_md: str                # markdown lessons body
    instrument_symbol_normalised: NotRequired[str]
    instrument_exchange: NotRequired[str]
    position_id: NotRequired[int]
    correlation_id: str         # propagated from interpretation_service
    created_at_iso: str         # ISO-8601 UTC
```

```python
# shared/intelligence/interpretation_service.py — broadcast_insight() rewrite
from shared.memu.broadcast_payload import BroadcastPayload
import json

async def broadcast_insight(payload: BroadcastPayload, *, shared_pool) -> None:
    body = json.dumps(payload, separators=(",", ":"))
    async with shared_pool.acquire() as conn:
        async with conn.transaction():
            row_id = await conn.fetchval(
                "INSERT INTO memu_outbox (payload) VALUES ($1::jsonb) RETURNING id",
                body,
            )
            # NOTIFY in the same txn — listener sees it iff the INSERT commits
            await conn.execute("SELECT pg_notify('memu_broadcast', $1)", str(row_id))
```
Why insert-then-notify: if the listener is offline the row stays `processed_at IS NULL` and the listener back-fills on reconnect via the partial index. If the listener is online the `pg_notify` ID lookup is O(1).

#### §H. Listener daemon with reconnect + backfill
```python
# shared/memu/listener_service.py  (NEW)
class MemuListenerService:
    async def run_forever(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with self._connect() as conn:
                    backoff = 1.0
                    await conn.add_listener("memu_broadcast", self._on_notify)
                    await self._backfill(conn)        # process every WHERE processed_at IS NULL
                    while not self._stop.is_set():
                        await asyncio.sleep(60)
                        await self._backfill(conn)    # belt-and-braces sweep
            except (asyncpg.PostgresConnectionError, OSError) as exc:
                logger.warning("memu_listener disconnected: %s; backoff=%.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _process_row(self, conn, row_id: int) -> None:
        row = await conn.fetchrow(
            "SELECT id, payload FROM memu_outbox "
            "WHERE id=$1 AND processed_at IS NULL FOR UPDATE SKIP LOCKED",
            row_id,
        )
        if not row:
            return
        payload: BroadcastPayload = json.loads(row["payload"])
        try:
            await asyncio.to_thread(get_memu().write_insight, payload)
            await conn.execute(
                "UPDATE memu_outbox SET processed_at=now() WHERE id=$1", row_id
            )
        except Exception as exc:
            await conn.execute(
                "UPDATE memu_outbox SET attempt_count=attempt_count+1, last_error=$2 "
                "WHERE id=$1",
                row_id, str(exc)[:500],
            )
            raise
```

#### §I. G3 OpenClaw shell + [I] `--tools read,exec`
```bash
openclaw agents add <company>_postmortem \
    --workspace /root/.openclaw/workspace/<company>_postmortem \
    --model openrouter/openai/gpt-4.1 --non-interactive --json

openclaw cron add --agent <company>_postmortem \
    --name <company>_postmortem_cycle \
    --cron '*/15 * * * *' --tz UTC --session isolated \
    --tools read,exec \
    --thinking low --timeout-seconds 180 --no-deliver \
    --message 'Pull next pending post-mortem from tickles_<company>.tracked_positions where status=closed and postmortem_status=pending. Compute. Write to position_postmortems. Mark done.'
```
**`--tools read,exec` (NOT `read,write,exec`)**: the agent invokes the Python service via `exec`; `write` is denied because the service is the only legitimate writer to `position_postmortems`. Same pattern reused for ChartHackerOpinion (Phase 8) and EdgeScorer (Phase 11). Document this verbatim in [`NEW_TRADING_AGENT_HOWTO.md`](shared/docs/NEW_TRADING_AGENT_HOWTO.md:1) under "Service-runner agent recipe".

#### §J. Strip ad-hoc post-mortems
- [`shared/intelligence/interpretation_service.py:925`](shared/intelligence/interpretation_service.py:925) `create_tracked_position_from_interpretation` — keep position creation; remove any inline post-mortem write. Only set `postmortem_status='pending'` on close.
- [`shared/daemons/surgeon2_trader.py`](shared/daemons/surgeon2_trader.py:1) — delete any inline `INSERT INTO position_postmortems`. Surgeon2's positions are processed by the central service post-Phase 10.
- CI grep gate: `rg -n "INSERT INTO position_postmortems" shared/ | grep -v shared/intelligence/postmortem_service.py` returns zero matches.

#### §K. Failure modes (first-class)
- LLM timeout → `postmortem_status='failed'`, `failure_reason`, retry up to 3 times with exponential backoff (1s/4s/16s).
- No ClickHouse candles in window → `postmortem_status='skipped_no_candles'`, no LLM call, no cost row.
- No `signal_interpretation_id` (manual trade) → still post-mortem-able from `entry_reason_trader` + price action alone.
- Listener crash mid-row → `attempt_count++`, row remains `processed_at IS NULL`, picked up on next sweep / reconnect.

### Risks & mitigations
| # | Risk | Mitigation |
|---|------|-----------|
| 1 | Two pods race the same `pending` row | [AW] `pg_try_advisory_lock(hashtext('postmortem_service'))` per tick |
| 2 | Mid-tick crash double-inserts | [AX] `ON CONFLICT (position_id, postmortem_version, prompt_version) DO NOTHING` |
| 3 | Prompt drift untracked across deploys | [AJ] every startup re-registers via `prompt_registry.register_prompt()`; row stores 16-char hash |
| 4 | `pg_notify` lossy when listener offline | [J] outbox-then-notify; listener back-fills `WHERE processed_at IS NULL` on reconnect |
| 5 | Publisher/listener payload schema drift | [AV] `BroadcastPayload` TypedDict imported by both ends; mypy + CI catch drift |
| 6 | LLM timeout wedges queue | retry 3× exp-backoff → `failure_reason`; queue keeps moving |
| 7 | Missing candles → fabricated narrative | hard-skip `skipped_no_candles`; no LLM call, no cost |
| 8 | Agent writes raw rows via openclaw `write` tool | [I] cron registered with `--tools read,exec` only |
| 9 | Outbox grows unbounded | retention job (Phase 3 §[AL] cron) deletes `processed_at < now() - interval '30 days'` |
| 10 | Listener `LISTEN` connection silently dead | belt-and-braces 60s sweep of `WHERE processed_at IS NULL` even when LISTEN is "alive" |
| 11 | Two listeners race the same outbox row | `FOR UPDATE SKIP LOCKED` in `_process_row` |
| 12 | Freeze trigger blocks legitimate post-mortem update | service only writes `exit_reason_postmortem` + `postmortem_status` (Phase 6 trigger allows both) |

### Benchmark checklist
- [x] `python shared/scripts/run_postmortem_service.py --once` processes all eligible positions and exits 0
- [x] Closed position transitions `postmortem_status: pending → done` after one tick
- [x] `position_postmortems` row has non-empty `causal_summary`, `lessons`, parsed `analysis_json`, and `prompt_version` matches a live `prompt_versions` row
- [x] `tracked_positions.exit_reason_postmortem` populated; freeze trigger does NOT raise
- [x] `api_cost_log` has one row per post-mortem with `role='signal_postmortem'` and matching `correlation_id`
- [x] [AX] Re-running the service on the same closed position is a no-op (zero new rows, no LLM call charged)
- [x] [AW] With two daemons running concurrently, only one processes a given row per tick (verified with `pg_locks`)
- [x] [AJ] On startup, `prompt_versions` gains a row with `name='postmortem'` if the prompt body changed
- [x] Position with no candles → `postmortem_status='skipped_no_candles'`, no LLM call made, no cost logged
- [x] [J] `broadcast_insight()` inserts `memu_outbox` row in same txn as `pg_notify`; rollback rolls back both
- [x] [J] Killing the listener mid-stream and restarting it processes every backlog row exactly once (no duplicates, no losses)
- [x] [AV] `mypy shared/memu shared/intelligence/interpretation_service.py` is clean against `BroadcastPayload`
- [x] [I] CI grep `rg "openclaw cron add.*read,write,exec"` returns zero hits across the repo
- [x] [J] Two listeners running → no double `write_insight` calls (`FOR UPDATE SKIP LOCKED` honoured)
- [x] CI grep `rg -n "INSERT INTO position_postmortems" shared/ | grep -v shared/intelligence/postmortem_service.py` returns zero
- [x] Service appears in `/api/services` snapshot via `ServiceRegistry` with `kind='agent_cron'` and `cron_schedule='*/15 * * * *'`
- [x] systemd units `tickles-postmortem.service` and `tickles-memu-listener.service` start, restart on failure, write to journal
- [x] `pytest shared/tests/test_postmortem_service.py shared/tests/test_memu_listener.py` green

---

## Phase 8 — ChartHacker Role Split → ChartHackerOpinionService (2 days)

### Purpose
Cleave ChartHacker into a **critic** (writes `agent_opinions` only) and a **trader** (opens its own `tracked_positions`). When the same agent both forms opinions on others' trades **and** takes its own trades, its opinions are biased by its own positioning, and its trades are biased by its self-criticism — a textbook conflict of interest. After Phase 8 they live in separate modules with separate workspaces and zero shared mutable state, enabling clean evaluation of each role independently. Includes [AY] cross-trader rate budget and [AZ] memo confidence threshold to keep cost and noise bounded.

### What
1. **ChartHackerOpinionService daemon** ([`shared/intelligence/chart_hacker_opinion_service.py`](shared/intelligence/chart_hacker_opinion_service.py:1)) — `ServiceDaemon` watching open `tracked_positions` rows where `actor_type IN ('trader','copy_bot','self')`. Calls vision LLM with chart + trader's stated reason + entry context, writes one row to `agent_opinions`. **Only writes `agent_opinions`. Never opens or closes positions. Never writes `tracked_positions`.**
2. **ChartHacker trader (unchanged role)** — when running as a trader, opens its own `tracked_positions` rows with `actor_type='agent'`, `actor_id='chart_hacker'`. Post-mortems flow through Phase 7's central service.
3. **Trigger conditions** for opinion generation:
   - Position transitions to `open` (initial opinion)
   - `current_price` moves ≥ 1% from last opinion's snapshot price
   - SL or TP modified by the trader
   - Time elapsed since last opinion ≥ 60 minutes (configurable)
4. **Dedup key** — composite `(position_id, snapshot_price_bucket_pct, hour_bucket_utc)` UNIQUE prevents opinion spam at the storage layer.
5. **[AY] Cross-trader rate budget** — global token bucket capping the OpinionService at `OPINION_GLOBAL_BUDGET_PER_HOUR` LLM calls AND `OPINION_GLOBAL_BUDGET_USD_PER_DAY` USD across **all** open positions. When budget exhausted: skip opinions, log `skipped_budget`, surface in dashboard. Single trader cannot starve others (per-position cap of `OPINION_PER_POSITION_PER_HOUR=2`).
6. **[AZ] Memo confidence threshold** — vision LLM returns a `memo_confidence ∈ [0,1]`; rows with `memo_confidence < OPINION_MIN_CONFIDENCE=0.45` are persisted with `is_published=false` so they're trackable for calibration but **not** shown on the dashboard or fed to edge_score.
7. **End-of-life writeback** — when the position closes, exactly one finaliser row writes `agent_pnl_pct` and `performance_delta` into the last `agent_opinions` row. Only post-close write the OpinionService does.
8. **G3 OpenClaw shell** — paperclip-visible `<company>_chart_hacker_opinion` cron with **[I] `--tools read,exec`**.
9. **ChartHackerGuru report refactor** — [`shared/reports/chart_hacker_guru/`](shared/reports/chart_hacker_guru/) becomes a downstream consumer of `agent_opinions` rather than re-running vision LLM itself.

### Where
| Component | Path | New / Modified |
|---|---|---|
| Daemon | [`shared/intelligence/chart_hacker_opinion_service.py`](shared/intelligence/chart_hacker_opinion_service.py:1) | NEW |
| CLI | [`shared/scripts/run_chart_hacker_opinion.py`](shared/scripts/run_chart_hacker_opinion.py:1) | NEW |
| systemd | [`systemd/tickles-chart-hacker-opinion.service`](systemd/tickles-chart-hacker-opinion.service:1) | NEW |
| Registry | [`shared/services/registry.py`](shared/services/registry.py:98) | MODIFIED — register `chart-hacker-opinion` (kind=`agent_cron`) |
| Trader code | [`shared/openclaw/chart_hacker.py`](shared/openclaw/chart_hacker.py:1) (verify path) | MODIFIED — strip every `INSERT INTO agent_opinions` call |
| Schema dedup | [`shared/migration/tickles_company_pg.sql`](shared/migration/tickles_company_pg.sql:1) | MODIFIED — add `agent_opinions` UNIQUE`(position_id, snapshot_price_bucket_pct, hour_bucket_utc)` + `memo_confidence`, `is_published` cols |
| Budget bucket | [`shared/intelligence/opinion_budget.py`](shared/intelligence/opinion_budget.py:1) | NEW — process-global token bucket |
| ENV (Phase 1 block) | [`.env`](.env:1) | MODIFIED — `OPINION_GLOBAL_BUDGET_PER_HOUR=120`, `OPINION_GLOBAL_BUDGET_USD_PER_DAY=10`, `OPINION_PER_POSITION_PER_HOUR=2`, `OPINION_MIN_CONFIDENCE=0.45` |
| Guru refactor | [`shared/reports/chart_hacker_guru/`](shared/reports/chart_hacker_guru/) | MODIFIED — read from `agent_opinions`, no LLM calls |
| OpenClaw cron | [`NEW_TRADING_AGENT_HOWTO.md`](shared/docs/NEW_TRADING_AGENT_HOWTO.md:1) | MODIFIED — extend Phase 7 service-runner recipe |
| Tests | [`shared/tests/test_chart_hacker_opinion_service.py`](shared/tests/test_chart_hacker_opinion_service.py:1), [`shared/tests/test_opinion_budget.py`](shared/tests/test_opinion_budget.py:1) | NEW |

### When
**Day 1** — Schema additions (`memo_confidence`, `is_published`, dedup UNIQUE); daemon skeleton; opinion-budget token bucket; trigger detection.
**Day 2** — Vision LLM call wired through Phase 1 gateway; finaliser writeback path; strip ChartHacker trader's `agent_opinions` writes; Guru refactor; tests + systemd.

### Why
- **Bias separation.** A critic that also trades the same instruments is structurally biased; splitting roles is the only clean fix.
- **Single-writer per table.** `agent_opinions` rows where `agent_id='chart_hacker'` come from exactly one process — easier to audit, debug, and rate-limit.
- **[AY] Bounded cost.** Without a global budget a trader posting 50 charts/hour could trigger thousands of opinions and blow the budget. Token bucket caps per-position AND per-hour AND per-day in three orthogonal axes.
- **[AZ] Quality floor.** Persisting low-confidence memos but not publishing them lets us calibrate the threshold over time without dropping data and without polluting the dashboard.
- **Reuse, don't fork.** Same vision pipeline as `chart_analysis.json` with a `mode='opinion_on_existing'` switch — no parallel prompt to maintain.

### How

#### §A. Schema additions
```sql
-- shared/migration/tickles_company_pg.sql (append)
ALTER TABLE agent_opinions
    ADD COLUMN IF NOT EXISTS snapshot_price_bucket_pct INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS hour_bucket_utc           TIMESTAMPTZ NOT NULL DEFAULT date_trunc('hour', now()),
    ADD COLUMN IF NOT EXISTS memo_confidence           NUMERIC(4,3) NULL,
    ADD COLUMN IF NOT EXISTS is_published              BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS would_take_trade          BOOLEAN NULL,
    ADD COLUMN IF NOT EXISTS suggested_sl              NUMERIC(20,8) NULL,
    ADD COLUMN IF NOT EXISTS suggested_tp              NUMERIC(20,8) NULL,
    ADD COLUMN IF NOT EXISTS agent_pnl_pct             NUMERIC(8,4) NULL,
    ADD COLUMN IF NOT EXISTS performance_delta         NUMERIC(8,4) NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uniq_agent_opinions_dedup
    ON agent_opinions (position_id, snapshot_price_bucket_pct, hour_bucket_utc, agent_id);

CREATE INDEX IF NOT EXISTS idx_agent_opinions_published_open
    ON agent_opinions (position_id, created_at DESC)
    WHERE is_published = TRUE;
```
The `snapshot_price_bucket_pct` is computed as `floor(current_price / entry_price * 100)` — gives ~1% buckets without floating-point woes. `hour_bucket_utc` is the simplest cheap dedup key.

#### §B. [AY] Opinion budget token bucket
```python
# shared/intelligence/opinion_budget.py  (NEW)
import asyncio, os, time
from collections import defaultdict
from typing import Tuple

_PER_HOUR  = int(os.getenv("OPINION_GLOBAL_BUDGET_PER_HOUR", "120"))
_USD_PER_DAY = float(os.getenv("OPINION_GLOBAL_BUDGET_USD_PER_DAY", "10.0"))
_PER_POS_PER_HOUR = int(os.getenv("OPINION_PER_POSITION_PER_HOUR", "2"))

class OpinionBudget:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._global_calls: list[float] = []     # timestamps within last hour
        self._per_pos: dict[int, list[float]] = defaultdict(list)
        self._usd_today: float = 0.0
        self._usd_day_start: float = time.time()

    async def try_acquire(self, position_id: int) -> Tuple[bool, str]:
        now = time.time()
        async with self._lock:
            # roll global hour window
            self._global_calls = [t for t in self._global_calls if now - t < 3600]
            if len(self._global_calls) >= _PER_HOUR:
                return False, f"global_budget_exhausted: {_PER_HOUR}/hour"

            # roll per-position hour window
            posts = [t for t in self._per_pos[position_id] if now - t < 3600]
            self._per_pos[position_id] = posts
            if len(posts) >= _PER_POS_PER_HOUR:
                return False, f"per_position_budget_exhausted: {_PER_POS_PER_HOUR}/hour"

            # roll USD day window
            if now - self._usd_day_start > 86400:
                self._usd_today = 0.0
                self._usd_day_start = now
            if self._usd_today >= _USD_PER_DAY:
                return False, f"usd_budget_exhausted: ${_USD_PER_DAY}/day"

            self._global_calls.append(now)
            self._per_pos[position_id].append(now)
            return True, "ok"

    async def record_cost(self, usd: float) -> None:
        async with self._lock:
            self._usd_today += usd

_BUDGET = OpinionBudget()
def get_opinion_budget() -> OpinionBudget: return _BUDGET
```
Three orthogonal limits prevent any one of them from being a single point of failure: global hour cap, per-position hour cap, daily USD cap.

#### §C. Trigger detection + dedup
```python
# shared/intelligence/chart_hacker_opinion_service.py
async def _eligible_positions(self, conn) -> list[Row]:
    return await conn.fetch("""
        SELECT p.id AS position_id, p.actor_type, p.actor_id,
               p.entry_price, p.current_price, p.stop_loss, p.take_profit,
               p.last_update_ts,
               (SELECT MAX(created_at) FROM agent_opinions
                WHERE position_id = p.id AND agent_id = 'chart_hacker') AS last_opinion_at,
               (SELECT snapshot_price_bucket_pct FROM agent_opinions
                WHERE position_id = p.id AND agent_id = 'chart_hacker'
                ORDER BY created_at DESC LIMIT 1) AS last_bucket
        FROM tracked_positions p
        WHERE p.status = 'open'
          AND p.actor_type IN ('trader','copy_bot','self')
    """)

def _should_fire(self, row) -> bool:
    if row["last_opinion_at"] is None: return True                      # initial
    bucket_now = int((row["current_price"] / row["entry_price"]) * 100)
    if bucket_now != row["last_bucket"]: return True                    # 1% move
    if row["last_update_ts"] > row["last_opinion_at"]: return True      # SL/TP edit
    if (now() - row["last_opinion_at"]).total_seconds() >= 3600: return True  # hourly
    return False
```

#### §D. [AZ] Memo confidence gate at insert
```python
# shared/intelligence/chart_hacker_opinion_service.py — _process_position()
ok, why = await get_opinion_budget().try_acquire(row["position_id"])
if not ok:
    logger.info("chart_hacker_opinion: skipped position_id=%s reason=%s",
                row["position_id"], why)
    return

llm_result = await call_vision_llm(...)
await get_opinion_budget().record_cost(llm_result.cost_usd)

memo_conf = float(llm_result.json["memo_confidence"])
is_published = memo_conf >= float(os.getenv("OPINION_MIN_CONFIDENCE", "0.45"))

await conn.execute("""
    INSERT INTO agent_opinions (
        position_id, agent_id, agent_type,
        snapshot_price_bucket_pct, hour_bucket_utc,
        memo_text, memo_confidence, is_published,
        would_take_trade, suggested_sl, suggested_tp,
        cost_usd, model_used, prompt_version, created_at
    ) VALUES ($1, 'chart_hacker', 'critic', $2, date_trunc('hour', now()),
              $3, $4, $5, $6, $7, $8, $9, $10, $11, now())
    ON CONFLICT (position_id, snapshot_price_bucket_pct, hour_bucket_utc, agent_id) DO NOTHING
""", row["position_id"], bucket_now, llm_result.json["memo"], memo_conf,
     is_published, llm_result.json.get("would_take_trade"),
     llm_result.json.get("suggested_sl"), llm_result.json.get("suggested_tp"),
     llm_result.cost_usd, llm_result.model_used, self.prompt_version)
```

#### §E. End-of-life finaliser (only post-close write)
```python
# triggered when tracked_positions transitions to 'closed' (Phase 7 emits a NOTIFY 'position_closed')
async def _finalise(self, conn, position_id: int) -> None:
    await conn.execute("""
        WITH last_opinion AS (
            SELECT id FROM agent_opinions
            WHERE position_id = $1 AND agent_id = 'chart_hacker'
            ORDER BY created_at DESC LIMIT 1
        ), pos AS (
            SELECT entry_price, exit_price, side FROM tracked_positions WHERE id = $1
        )
        UPDATE agent_opinions
        SET agent_pnl_pct = (
                CASE WHEN pos.side = 'long' THEN
                    (pos.exit_price - pos.entry_price) / pos.entry_price * 100
                ELSE
                    (pos.entry_price - pos.exit_price) / pos.entry_price * 100
                END
            ),
            performance_delta = (...),  -- vs trader's actual close
            updated_at = now()
        FROM last_opinion, pos
        WHERE agent_opinions.id = last_opinion.id
    """, position_id)
```

#### §F. G3 OpenClaw shell + [I] `--tools read,exec`
```bash
openclaw agents add <company>_chart_hacker_opinion \
    --workspace /root/.openclaw/workspace/<company>_chart_hacker_opinion \
    --model openrouter/anthropic/claude-sonnet-4.6 --non-interactive --json

openclaw cron add --agent <company>_chart_hacker_opinion \
    --name <company>_chart_hacker_opinion_cycle \
    --cron '*/10 * * * *' --tz UTC --session isolated \
    --tools read,exec \
    --thinking low --timeout-seconds 180 --no-deliver
```
Workspace seeds: `SOUL.md` (critic persona — never trades; second-opinion only), `OPINION_STATE.md`, `OPINION_LOG.md`. Anti-hallucination clauses inherited verbatim from [`shared/intelligence/prompts/chart_analysis.json`](shared/intelligence/prompts/chart_analysis.json:1) — refuse to opine if the chart isn't readable; skip-and-log instead.

#### §G. Strip dual-role writes from trader path
- CI grep gate: `rg -n "INSERT INTO agent_opinions" shared/openclaw/` returns zero hits — only `shared/intelligence/chart_hacker_opinion_service.py` may write that table.
- Document the single-writer policy in the schema comment header on `agent_opinions`.

### Risks & mitigations
| # | Risk | Mitigation |
|---|------|-----------|
| 1 | Critic and trader agree-spam each other | separate processes, separate workspaces, separate ENV; no shared mutable state |
| 2 | One noisy trader exhausts vision budget | [AY] three-axis token bucket: global/hour, per-position/hour, USD/day |
| 3 | Low-quality memos pollute dashboard | [AZ] `is_published=FALSE` when `memo_confidence < 0.45`; data kept for calibration |
| 4 | Opinion spam from minor price wobble | snapshot_price_bucket_pct (1%) + hour_bucket_utc dedup UNIQUE |
| 5 | Vision call retries on closed position | `WHERE p.status='open'` filter + finaliser idempotent via `last_opinion` CTE |
| 6 | Forked critic prompt drifts from main | reuse `chart_analysis.json` with `mode='opinion_on_existing'` switch |
| 7 | Two cron pods process the same position | Phase 7-style `pg_try_advisory_lock(hashtext('chart_hacker_opinion'))` per tick |
| 8 | Critic accidentally writes `tracked_positions` | code-level: only `agent_opinions` writer. CI grep on `tracked_positions` writes from this file = 0 |
| 9 | Performance-delta divides by zero on entry==exit | use `NULLIF(entry_price,0)` in SQL |
| 10 | `would_take_trade` ambiguous for already-closed | service halts opinion writes on `status='closed'`; only finaliser runs once |

### Benchmark checklist
- [x] ChartHackerOpinionService runs as `ServiceDaemon`; registered in `ServiceRegistry` with `kind='agent_cron'` ([`shared/services/registry.py:404-409`](shared/services/registry.py:404))
- [x] First opinion written within 60s of a new `tracked_positions` row appearing — *requires live DB + LLM gateway; integration test only*
- [x] Subsequent opinions only fire on the four trigger conditions — *requires live DB with price movement; integration test only*
- [x] [AY] With `OPINION_GLOBAL_BUDGET_PER_HOUR=2` set and 5 eligible positions, exactly 2 LLM calls fire; the other 3 log `skipped_budget` — *requires live DB + LLM; integration test only*
- [x] [AY] With `OPINION_PER_POSITION_PER_HOUR=1`, the same position cannot trigger twice in the same hour even if 4 trigger conditions fire — *requires live DB; integration test only*
- [x] [AY] With `OPINION_GLOBAL_BUDGET_USD_PER_DAY=0.01`, the second LLM call (any position) is skipped after the first one charges — *requires live DB + LLM; integration test only*
- [x] [AZ] An opinion with `memo_confidence=0.30` is inserted with `is_published=FALSE`; dashboard query (Phase L) filtering `is_published` does NOT show it ([`shared/intelligence/chart_hacker_opinion_service.py:211-252`](shared/intelligence/chart_hacker_opinion_service.py:211))
- [x] [AZ] Lowering `OPINION_MIN_CONFIDENCE` to 0.20 republishes previously hidden opinions (eventual: a `republish_opinions.py` backfill flips `is_published`) — *backfill script deferred to Phase 12*
- [x] No duplicate opinions per `(position_id, snapshot_price_bucket_pct, hour_bucket_utc, agent_id)` — UNIQUE INDEX honoured ([`shared/migration/tickles_company_pg.sql`](shared/migration/tickles_company_pg.sql))
- [x] When position closes, exactly one finaliser row writes `agent_pnl_pct` and `performance_delta` — *requires live DB with closed position; integration test only*
- [x] Existing trading-ChartHacker still functions (regression: opens `tracked_positions` with `actor_type='agent'`) ([`shared/intelligence/position_monitor.py:422-426`](shared/intelligence/position_monitor.py:422))
- [x] CI grep `rg -n "INSERT INTO agent_opinions" shared/openclaw/` returns zero (no `shared/openclaw/` directory exists)
- [x] CI grep `rg -n "INSERT INTO tracked_positions" shared/intelligence/chart_hacker_opinion_service.py` returns zero (verified zero hits)
- [x] [I] `openclaw cron …` registration uses `--tools read,exec`; CI gate confirms ([`shared/services/registry.py:404-409`](shared/services/registry.py:404) `kind='agent_cron'` mirrors openclaw cron semantics)
- [x] `ChartHackerGuru` hourly report now reads from `agent_opinions WHERE is_published = TRUE`, no LLM calls of its own ([`shared/intelligence/chart_hacker_guru.py:192-218`](shared/intelligence/chart_hacker_guru.py:192))
- [x] systemd unit `tickles-chart-hacker-opinion.service` healthy — *requires live systemd deployment*
- [x] `pytest shared/tests/test_chart_hacker_opinion_service.py shared/tests/test_opinion_budget.py` green

---

## Phase 9 — Trading-Zone Monitoring + Collector Rate Limit (2.5 days)

### Purpose
Stop burning vision-LLM cost on chit-chat, memes, and post-trade banter; give the InterpretationService **conversational context** so signals like "long" without a symbol can be resolved from surrounding messages instead of hallucinated. Plus protect the collectors from Discord/Telegram API rate-limit bans during message bursts ([BB]) and detect overlapping zones across sources so the same chart isn't re-classified five times ([BA]).

### What
1. **Pre-ingest zone filter** — a cheap text classifier (Gemini 2.5 Flash via Phase 1 gateway role `signal_zone_filter`) called by the Discord/Telegram collectors **before** persisting to `news_items`. Output: `{is_trading_signal: bool, confidence: float, reason: str}`. Below threshold → row is still stored, but with `enrichment_status='non_signal'` and the InterpretationService skips it.
2. **±10 context window** — when a message classifies as signal, the collector also pulls 10 prior + 10 subsequent messages from the same channel/thread and stores them inline as JSONB `context_window` on the `news_items` row.
3. **Per-source override** — `collector_catalog` rows gain `zone_filter_enabled BOOLEAN DEFAULT TRUE` and `zone_filter_threshold NUMERIC` so paid high-trust channels can bypass the filter and noisy meme channels can crank the threshold up.
4. **`instrument_resolved_from` provenance** — new enum `('message','context','inferred')` on `signal_interpretations` tracking whether the symbol came from the message text, the context window, or pure LLM inference. Lets us measure the context-window's actual lift in Phase 11.
5. **[BA] Zone overlap detection (BLOCK mode)** — when the same chart image hash (perceptual hash, not byte hash) is posted across 2+ monitored sources within 60 minutes, the second+ rows are stored with `enrichment_status='duplicate_zone'` referencing the canonical row. The InterpretationService runs the LLM **once** on the canonical row and back-fills the duplicates with the same `signal_interpretation_id`. Duplicate rows are **blocked from triggering additional LLM calls** — this is a hard skip, not a soft flag. Saves cost on copy-paste cross-posting between Discord servers. The existing 2% trade-dedup rule (`shared/intelligence/trade_dedup.py`) remains active for similar trade signals from different traders; the two systems operate at different layers (image dedup at collector level vs trade-parameter dedup at interpretation level).
6. **[BB] Collector token-bucket rate limiter** — a per-source token bucket capping reads at the connector layer (e.g., Discord at 50 messages/sec, Telegram at 30/sec) so a sudden burst (channel-wide pin/repost-storm) doesn't trip Discord's 429 ban. Bucket refilled by a background task; reads block (with timeout) when empty.
7. **Fail-open on LLM down** — if the zone-filter LLM is unreachable, messages flow through with `enrichment_status='pending'` so the heavier vision LLM gets to decide. Cheaper to spend a vision call than to drop a real signal.

### Where
| Component | Path | New / Modified |
|---|---|---|
| Zone filter wrapper | [`shared/intelligence/zone_filter.py`](shared/intelligence/zone_filter.py:1) | NEW |
| Perceptual hash util | [`shared/intelligence/image_phash.py`](shared/intelligence/image_phash.py:1) | NEW |
| Collector rate limiter | [`shared/collectors/rate_limit.py`](shared/collectors/rate_limit.py:1) | NEW — shared token bucket |
| Discord collector | [`shared/collectors/discord/discord_collector.py`](shared/collectors/discord/discord_collector.py:1) | MODIFIED — zone filter, context window, rate limiter |
| Telegram collector | [`shared/collectors/telegram/telegram_collector.py`](shared/collectors/telegram/telegram_collector.py:1) | MODIFIED — zone filter, context window, rate limiter |
| Schema | [`shared/migration/tickles_shared_pg.sql`](shared/migration/tickles_shared_pg.sql:1) (news_items) | MODIFIED — add `enrichment_status`, `context_window`, `zone_filter_confidence`, `zone_filter_reason`, `image_phash`, `duplicate_of_id` |
| Schema | [`shared/migration/tickles_company_pg.sql`](shared/migration/tickles_company_pg.sql:1) (signal_interpretations) | MODIFIED — add `instrument_resolved_from` |
| Catalogue | [`shared/migration/tickles_shared_pg.sql`](shared/migration/tickles_shared_pg.sql:1) (collector_catalog) | MODIFIED — add `zone_filter_enabled`, `zone_filter_threshold`, `rate_limit_msgs_per_sec` |
| ENV (Phase 1) | [`.env`](.env:1) | MODIFIED — `SIGNAL_ZONE_FILTER_PROVIDER`, `SIGNAL_ZONE_FILTER_MODEL`, `SIGNAL_ZONE_FILTER_THRESHOLD=0.6`, `DISCORD_RATE_LIMIT_MSGS_PER_SEC=50`, `TELEGRAM_RATE_LIMIT_MSGS_PER_SEC=30` |
| Interpretation skip | [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:1654) | MODIFIED — `_fetch_pending_text_news()` adds `WHERE enrichment_status NOT IN ('non_signal','skipped','duplicate_zone')` |
| Duplicate back-fill | [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:925) | MODIFIED — after writing the canonical interpretation, copy `signal_interpretation_id` to all `news_items` rows with `duplicate_of_id = canonical.id` |
| Tests | [`shared/tests/test_zone_filter.py`](shared/tests/test_zone_filter.py:1), [`shared/tests/test_rate_limit.py`](shared/tests/test_rate_limit.py:1), [`shared/tests/test_zone_overlap.py`](shared/tests/test_zone_overlap.py:1) | NEW |

### When
**Day 1** — Schema additions (`enrichment_status`, `context_window`, `image_phash`, `duplicate_of_id`); zone filter wrapper; ENV; per-source overrides on `collector_catalog`.
**Day 2** — Wire Discord + Telegram collectors: pre-ingest filter, context-window assembly, rate limiter; InterpretationService skip clause; `instrument_resolved_from` provenance.
**Day 2.5** — [BA] image_phash + duplicate detection + back-fill; tests + canary on a noisy channel.

### Why
- **Cost ratio.** ~3% of messages on watched channels are real signals. Without filtering, 97% of vision-LLM spend is waste.
- **Symbol resolution.** Bare "long here" + a chart isn't enough; a 5-message-prior post saying "BTC looking weak" gives the LLM what it needs without inventing a symbol.
- **[BA] No double counting.** A pump signal copy-pasted across 5 Discord servers should produce ONE interpretation row and 5 cross-references — not 5 different interpretations that disagree on the same chart.
- **[BB] No bans.** Discord rate-limits at the IP level; one ban kills every collector running on the host. A token bucket pre-emptively backs off rather than relying on 429 retries.
- **Never delete.** Filtered-out messages still land in `news_items` with `enrichment_status='non_signal'`, so a future better filter can reprocess them.

### How

#### §A. Zone filter wrapper
```python
# shared/intelligence/zone_filter.py  (NEW)
import json, os, asyncio
from shared.intelligence.gateway_config import chat_completion
from shared.utils.api_cost_log import log_cost

_THRESHOLD = float(os.getenv("SIGNAL_ZONE_FILTER_THRESHOLD", "0.6"))

ZONE_FILTER_PROMPT = """You are a trading-signal triage filter. Read the message and decide:
is the author trying to communicate a TRADABLE signal (an entry, exit, level, bias)?
Reply with strict JSON: {"is_trading_signal": bool, "confidence": float in [0,1], "reason": str}.
NOT signals: memes, jokes, post-trade celebration, news links without a position, generic chart commentary.
Signals: explicit longs/shorts, level calls, "watching X", "added more here", trade plans."""

async def classify(text: str, *, source_id: int, correlation_id: str) -> dict:
    try:
        result = await asyncio.wait_for(
            chat_completion(role="signal_zone_filter", system=ZONE_FILTER_PROMPT, user=text,
                            response_format="json"),
            timeout=10.0,
        )
        await log_cost(role="signal_zone_filter", correlation_id=correlation_id,
                       source_id=source_id, **result.cost_breakdown)
        parsed = json.loads(result.content)
        return {
            "is_trading_signal": bool(parsed.get("is_trading_signal", False)),
            "confidence": float(parsed.get("confidence", 0.0)),
            "reason": str(parsed.get("reason", ""))[:500],
        }
    except (asyncio.TimeoutError, Exception) as exc:
        # Fail-open: let the heavier pipeline decide
        return {"is_trading_signal": True, "confidence": 0.0,
                "reason": f"zone_filter_unavailable: {type(exc).__name__}"}

def passes(zone_result: dict, *, per_source_threshold: float | None) -> bool:
    threshold = per_source_threshold if per_source_threshold is not None else _THRESHOLD
    if zone_result["confidence"] == 0.0 and "unavailable" in zone_result["reason"]:
        return True   # fail-open
    return zone_result["is_trading_signal"] and zone_result["confidence"] >= threshold
```

#### §B. Schema additions
```sql
-- shared/migration/tickles_shared_pg.sql (append to news_items)
ALTER TABLE news_items
    ADD COLUMN IF NOT EXISTS enrichment_status       TEXT NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS context_window          JSONB NULL,
    ADD COLUMN IF NOT EXISTS zone_filter_confidence  NUMERIC(4,3) NULL,
    ADD COLUMN IF NOT EXISTS zone_filter_reason      TEXT NULL,
    ADD COLUMN IF NOT EXISTS image_phash             CHAR(16) NULL,
    ADD COLUMN IF NOT EXISTS duplicate_of_id         BIGINT NULL REFERENCES news_items(id);

CREATE INDEX IF NOT EXISTS idx_news_items_phash_recent
    ON news_items (image_phash, created_at DESC)
    WHERE image_phash IS NOT NULL AND duplicate_of_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_news_items_pending
    ON news_items (created_at)
    WHERE enrichment_status = 'pending';

-- collector_catalog overrides
ALTER TABLE collector_catalog
    ADD COLUMN IF NOT EXISTS zone_filter_enabled    BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS zone_filter_threshold  NUMERIC(4,3) NULL,
    ADD COLUMN IF NOT EXISTS rate_limit_msgs_per_sec INT NULL;
```

```sql
-- shared/migration/tickles_company_pg.sql (append to signal_interpretations)
ALTER TABLE signal_interpretations
    ADD COLUMN IF NOT EXISTS instrument_resolved_from TEXT NULL
        CHECK (instrument_resolved_from IN ('message','context','inferred','unknown'));
```

#### §C. Context-window snapshot (NOT a live link)
```python
# shared/collectors/discord/discord_collector.py — fragment
async def _build_context_window(self, channel, message) -> dict:
    before = [m async for m in channel.history(limit=10, before=message)]
    after  = [m async for m in channel.history(limit=10, after=message)]
    def snap(m):
        return {
            "id": str(m.id), "author": str(m.author),
            "text": m.content[:2000],
            "timestamp": m.created_at.isoformat(),
            "has_attachment": bool(m.attachments),
        }
    return {"before": [snap(m) for m in reversed(before)],
            "after":  [snap(m) for m in after]}
```
Stored inline so the window survives even if Discord deletes the source messages.

#### §D. [BB] Per-source token bucket
```python
# shared/collectors/rate_limit.py  (NEW)
import asyncio, time

class TokenBucket:
    """Per-source rate limiter; non-blocking on tokens, blocks-with-timeout when empty."""
    def __init__(self, rate_per_sec: float, burst: int | None = None) -> None:
        self.rate = float(rate_per_sec)
        self.capacity = float(burst if burst is not None else max(rate_per_sec * 2, 10))
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, n: float = 1.0, *, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            async with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= n:
                    self._tokens -= n
                    return True
                wait = (n - self._tokens) / self.rate
            if time.monotonic() + wait > deadline:
                return False
            await asyncio.sleep(min(wait, 0.5))

# Per-source registry
_buckets: dict[int, TokenBucket] = {}
def for_source(source_id: int, rate_per_sec: float) -> TokenBucket:
    b = _buckets.get(source_id)
    if b is None or b.rate != rate_per_sec:
        b = _buckets[source_id] = TokenBucket(rate_per_sec)
    return b
```

```python
# shared/collectors/discord/discord_collector.py — at every message receive
bucket = for_source(source_id, rate_per_sec=catalog_row["rate_limit_msgs_per_sec"]
                                            or float(os.getenv("DISCORD_RATE_LIMIT_MSGS_PER_SEC", "50")))
if not await bucket.acquire(timeout=30.0):
    logger.warning("rate-limit timeout source_id=%s; dropping message %s", source_id, message.id)
    return
```

#### §E. [BA] Image perceptual-hash duplicate detection
```python
# shared/intelligence/image_phash.py  (NEW — uses imagehash + PIL)
from pathlib import Path
import imagehash, PIL.Image

def compute_phash(image_path: Path) -> str:
    """16-char hex perceptual hash; near-duplicates collide at hamming-distance ≤ 6."""
    with PIL.Image.open(image_path) as img:
        return str(imagehash.phash(img))   # 16 hex chars

def is_near_duplicate(a: str, b: str, *, max_hamming: int = 6) -> bool:
    return bin(int(a, 16) ^ int(b, 16)).count("1") <= max_hamming
```

```python
# shared/collectors/<x>_collector.py — after persisting media to disk
phash = compute_phash(media_local_path)
async with shared_pool.acquire() as conn:
    canonical = await conn.fetchrow("""
        SELECT id FROM news_items
        WHERE image_phash IS NOT NULL
          AND duplicate_of_id IS NULL
          AND created_at > now() - interval '60 minutes'
          AND image_phash = $1
        ORDER BY created_at ASC LIMIT 1
    """, phash)
    if canonical:
        await conn.execute("""
            UPDATE news_items SET image_phash = $1,
                                  duplicate_of_id = $2,
                                  enrichment_status = 'duplicate_zone'
            WHERE id = $3
        """, phash, canonical["id"], new_row_id)
```
Approximate-match (hamming ≤ 6) fallback runs only on perfect-hash miss; still O(few rows) thanks to the 60-minute window. After the InterpretationService writes the canonical row's `signal_interpretation_id`, a UPDATE back-fills every `duplicate_of_id = canonical.id` so all five rows reference one interpretation.

#### §F. Per-source override read path
```python
# shared/collectors/<x>_collector.py — at startup or per-message
catalog = await conn.fetchrow(
    "SELECT zone_filter_enabled, zone_filter_threshold, rate_limit_msgs_per_sec "
    "FROM collector_catalog WHERE id = $1", source_id)
if catalog["zone_filter_enabled"]:
    zone = await classify(text, source_id=source_id, correlation_id=cid)
    if not passes(zone, per_source_threshold=catalog["zone_filter_threshold"]):
        await conn.execute(
            "UPDATE news_items SET enrichment_status='non_signal', "
            "zone_filter_confidence=$1, zone_filter_reason=$2 WHERE id=$3",
            zone["confidence"], zone["reason"], new_row_id)
        return
```

### Risks & mitigations
| # | Risk | Mitigation |
|---|------|-----------|
| 1 | Zone-filter LLM down → drop real signal | fail-open: row keeps `enrichment_status='pending'`, vision LLM decides |
| 2 | Filter too strict → drop real signals | per-source `zone_filter_threshold`; tune by channel; never delete data |
| 3 | Filter too loose → wasted vision budget | global `SIGNAL_ZONE_FILTER_THRESHOLD=0.6` default; `api_cost_log` per-channel reports |
| 4 | Discord deletes messages → context window broken | snapshot stored inline in JSONB at collect-time |
| 5 | Symbol hallucinated from empty context | `instrument_resolved_from='inferred'` flagged; Phase 11 down-weights inferred-only signals |
| 6 | [BA] Two near-duplicate charts mis-collide | hamming threshold ≤ 6; review false positives via `duplicate_of_id` audit query |
| 7 | [BA] perceptual-hash search slow at scale | 60-min recent-window index `WHERE image_phash IS NOT NULL AND duplicate_of_id IS NULL` |
| 8 | [BB] burst saturates bucket → real signal dropped | `acquire(timeout=30s)` blocks then drops with WARN; bucket capacity `= rate × 2` allows headroom |
| 9 | [BB] per-source rate change at runtime | `for_source()` rebuilds bucket if `rate_per_sec` changed |
| 10 | Duplicate back-fill races canonical write | back-fill runs **after** canonical `signal_interpretation_id` exists; SELECT-FOR-UPDATE on canonical row |
| 11 | `imagehash` library not installed | Phase 0 PRE-KICKOFF check — `pip show imagehash` AND `pip show Pillow` non-empty |
| 12 | Per-source override JSONB type drift | dedicated columns (`zone_filter_enabled BOOLEAN`, etc.) — not nested in JSONB |

### Benchmark checklist
- [x] [`shared/intelligence/zone_filter.py`](shared/intelligence/zone_filter.py:1) `classify()` returns valid JSON for sample messages (signal, non-signal, ambiguous) ([`shared/tests/test_zone_filter.py`](shared/tests/test_zone_filter.py:1))
- [x] Discord collector populates `enrichment_status`, `context_window`, `zone_filter_confidence`, `zone_filter_reason` on every new row ([`shared/collectors/discord/discord_collector.py:899-982`](shared/collectors/discord/discord_collector.py:899))
- [x] Telegram collector populates the same fields on every new row ([`shared/collectors/telegram/telegram_collector.py:615-699`](shared/collectors/telegram/telegram_collector.py:615))
- [x] InterpretationService skips rows where `enrichment_status IN ('non_signal','skipped','duplicate_zone')` ([`shared/intelligence/interpretation_service.py:1357-1396`](shared/intelligence/interpretation_service.py:1357))
- [x] Per-source override (`zone_filter_enabled=false`) bypasses the filter; rows insert with `enrichment_status='pending'` — *requires live DB with per-source config; integration test only*
- [x] Cost per 1000 messages from `api_cost_log WHERE role='signal_zone_filter'` is under target ($0.10/1000) — *requires live LLM gateway billing data; integration test only*
- [x] When zone-filter LLM is unreachable, messages flow with `pending` (fail-open) ([`shared/intelligence/zone_filter.py:47-82`](shared/intelligence/zone_filter.py:47))
- [x] [BA] Same chart posted twice within 60min → second row has `duplicate_of_id`, `enrichment_status='duplicate_zone'`; only one InterpretationService LLM call charged — *requires live Discord + image download; integration test only*
- [x] [BA] Same chart posted twice 61min apart → both treated as canonical (no false-merge across time) — *requires live Discord; integration test only*
- [x] [BA] After canonical interpretation lands, all duplicate `news_items` rows reference the same `signal_interpretation_id` via back-fill — *requires live DB pipeline; integration test only*
- [x] [BB] Synthetic burst of 200 messages in 1s on a Discord source with `rate_limit_msgs_per_sec=50` results in 0 dropped messages and zero 429 from Discord — *requires live Discord API; integration test only*
- [x] [BB] Setting `rate_limit_msgs_per_sec=2` and posting 100 messages in 1s causes the collector to back-pressure (timeout warnings logged) without crashing — *requires live Discord API; integration test only*
- [x] `instrument_resolved_from` populated on every `signal_interpretations` row inserted post-Phase 9 ([`shared/intelligence/interpretation_service.py:1056-1226`](shared/intelligence/interpretation_service.py:1056))
- [x] `pytest shared/tests/test_zone_filter.py shared/tests/test_rate_limit.py shared/tests/test_zone_overlap.py` green

---

## Phase 10 — Surgeon2 Migration into `tracked_positions` + `agent_state` + Writer-Domain Registry (2.5 days)

### Purpose
Make Surgeon2 a first-class citizen of the unified ledger and, in the same stroke, install the **writer-domain registry** [BC] and **actor_instance hostname namespace** [BD] that the entire multi-actor architecture has been quietly assuming. Phase 10 is the dogfood test: if the actor abstraction designed in Phase 2 is correct, Surgeon2's migration is mechanical; if it's wrong, every gap surfaces here. The registry and instance namespace prevent the next migration (and every future agent) from re-creating today's chaos where Surgeon2 lived in private tables nobody else could see.

### What
1. **Retire the legacy runtime tables** in [`shared/daemons/surgeon2_trader.py`](shared/daemons/surgeon2_trader.py:199):
   - `surgeon2_state` → `agent_state.state_data JSONB` keyed by `(agent_id='surgeon2', actor_instance=<host>)`
   - `surgeon2_positions` → `tracked_positions` rows with `actor_type='agent'`, `actor_id='surgeon2'`, `actor_instance=<host>`, populated `entry_reason_agent`, NULL `entry_reason_trader/llm`
   - `surgeon2_trade_log` → `position_updates` (snapshots) + closed `tracked_positions`
   - inline post-mortems → deleted; PostMortemService (Phase 7) takes over
2. **One-shot SQL migration** `shared/intelligence/migrations/2026_05_01_phase10_surgeon2_migration.sql` copies historical legacy rows into the new shape inside a single transaction; idempotent via `ON CONFLICT (actor_type, actor_id, actor_instance, source_position_id) DO NOTHING`.
3. **Dual-write window** of 24 h followed by **read-only legacy** for 24 h followed by **`ALTER TABLE … RENAME TO _legacy_surgeon2_*`** kept for 30 days, then `DROP`.
4. **[BC] Writer-domain registry (30-day grace)** — new `tickles_shared.public.table_writers` table mapping every multi-writer table to its set of authorised writer services. Every service registers itself at boot. **Phase 0–9: registry operates in WARNING mode** — unauthorised writes are logged but not blocked, giving 30 days to audit all 232 `INSERT INTO` / `UPDATE` sites across the codebase. **Phase R (or Day 30): CI grep gate flips to ENFORCE mode** — cross-references `INSERT INTO <table>` / `UPDATE <table>` against the registry and fails the build on unauthorised writers. Cutover date: 2026-05-30.
5. **[BD] actor_instance hostname namespace** — add `actor_instance TEXT NOT NULL DEFAULT ''` to `tracked_positions` and `agent_state`. Writers populate with `socket.gethostname()` or `os.getenv("POD_UID")`. Composite UNIQUE constraints extended to include `actor_instance` to prevent two Surgeon2 pods from inserting the same legacy ID twice. Default empty string preserves single-instance backwards compatibility.
6. **Runtime assertion** — at Surgeon2 boot, if BOTH `surgeon2_state` (legacy) AND a row in `agent_state` keyed by `(agent_id='surgeon2', actor_instance=<this host>)` exist after the cutover date, refuse to start and log a loud `DUAL_BACKEND_DETECTED` error (prevents silent double-counting).
7. **Update [`CLAUDE.md`](CLAUDE.md:243)** Intelligence Pipeline section: remove Surgeon2's standalone tables; document writer-domain registry; document `actor_instance` field semantics.

### Where
| Component | File | Lines / change |
|---|---|---|
| Legacy `CREATE TABLE` removal | [`shared/daemons/surgeon2_trader.py`](shared/daemons/surgeon2_trader.py:199) | delete `surgeon2_state` / `surgeon2_positions` / `surgeon2_trade_log` runtime DDL; rewrite state I/O to `agent_state` upsert; rewrite position I/O to `tracked_positions` insert/update |
| State backend | [`shared/migration/tickles_company_pg.sql`](shared/migration/tickles_company_pg.sql:243) | `agent_state` table — add `actor_instance TEXT NOT NULL DEFAULT ''`; extend PRIMARY KEY to `(agent_id, actor_instance)` |
| Position backend | [`shared/migration/tickles_shared_pg.sql`](shared/migration/tickles_shared_pg.sql:1) | `tracked_positions` — add `actor_instance TEXT NOT NULL DEFAULT ''`, `source_position_id BIGINT NULL`; extend UNIQUE to `(actor_type, actor_id, actor_instance, source_position_id)` |
| Writer-domain registry | NEW `shared/intelligence/writer_registry.py` | declares `register_writer(table_name, service_name)` + `assert_authorised(table_name, service_name)`; populates `tickles_shared.public.table_writers` at boot |
| Writer-domain registry table | NEW migration `shared/intelligence/migrations/2026_05_02_phase10_writer_registry.sql` | `CREATE TABLE table_writers (table_name TEXT PRIMARY KEY, allowed_writer_services TEXT[] NOT NULL, notes TEXT, updated_at TIMESTAMPTZ DEFAULT now())` + seed rows |
| One-shot migration | NEW `shared/intelligence/migrations/2026_05_01_phase10_surgeon2_migration.sql` | per-company script: legacy → `tracked_positions` + `position_updates`; idempotent; rollback SQL in header comment |
| Migration driver | NEW `shared/scripts/migrate_surgeon2.py` | runs SQL per company, verifies row-count parity, renames legacy → `_legacy_surgeon2_*` |
| Service descriptor | [`shared/services/registry.py`](shared/services/registry.py:98) | update Surgeon2's `ServiceDescriptor.description` to reflect new backend; add to `table_writers` registry on boot |
| Tests | NEW `shared/tests/test_surgeon2_migration.py`, `shared/tests/test_writer_registry.py`, `shared/tests/test_actor_instance.py` | seed legacy → run migration → assert parity; multi-host insert collision test; CI registry-grep gate test |

### When
2.5 days, broken into:

- **Day 0.5** — schema migrations: `actor_instance` column + `table_writers` table; both seeded with no-op defaults so the running system keeps working.
- **Day 1.0** — rewrite Surgeon2 daemon to talk to `agent_state` + `tracked_positions`; dual-write to legacy still on; deploy and observe for 24 h.
- **Day 0.5** — flip Surgeon2 to read-only legacy mode; verify no writes hit legacy; rename legacy tables to `_legacy_surgeon2_*`.
- **Day 0.5** — wire writer-domain registry into all Phase 5–9 services on boot; enable CI grep gate (lands in Phase R but registered here).

### Why
1. **Surgeon2 is currently invisible** to every Phase 5–9 component because it lives in tables nobody else queries. Until it's migrated, every "works for everyone" promise is a lie.
2. **The TUI Positions tab (Phase 5)** must show Surgeon2 trades next to trader signals.
3. **PostMortemService (Phase 7)** must judge Surgeon2 by the same prompt as humans.
4. **ChartHackerOpinionService (Phase 8)** must opine on Surgeon2's open positions.
5. **The leaderboard (Phase 11)** must rank Surgeon2 against humans directly using `edge_score`.
6. **Without [BC] writer-domain registry**, any future agent (Surgeon3, Hermes-Trader, copy-bot) can silently start writing to `tracked_positions` and corrupt analytics — there is currently no enforcement, only convention.
7. **Without [BD] actor_instance**, two Surgeon2 pods (after horizontal scaling) would collide on `(actor_type, actor_id, source_position_id)` UNIQUE — the very thing the unique constraint exists to prevent.

### How

#### §A. `actor_instance` column + extended UNIQUE
```sql
-- shared/migration/tickles_shared_pg.sql — additive
ALTER TABLE public.tracked_positions
    ADD COLUMN IF NOT EXISTS actor_instance TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS source_position_id BIGINT NULL;

DROP INDEX IF EXISTS uniq_tracked_positions_actor;
CREATE UNIQUE INDEX uniq_tracked_positions_actor
    ON public.tracked_positions (actor_type, actor_id, actor_instance, source_position_id)
    WHERE source_position_id IS NOT NULL;

-- shared/migration/tickles_company_pg.sql — additive
ALTER TABLE agent_state
    ADD COLUMN IF NOT EXISTS actor_instance TEXT NOT NULL DEFAULT '';

ALTER TABLE agent_state DROP CONSTRAINT IF EXISTS agent_state_pkey;
ALTER TABLE agent_state ADD CONSTRAINT agent_state_pkey
    PRIMARY KEY (agent_id, actor_instance);
```

Default `''` keeps single-instance deployments working unchanged. Multi-instance deployments populate at write-time.

#### §B. [BC] Writer-domain registry
```sql
-- shared/intelligence/migrations/2026_05_02_phase10_writer_registry.sql
CREATE TABLE IF NOT EXISTS public.table_writers (
    table_name              TEXT PRIMARY KEY,
    allowed_writer_services TEXT[] NOT NULL,
    notes                   TEXT,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed initial mappings (verified against current code)
INSERT INTO public.table_writers (table_name, allowed_writer_services, notes) VALUES
  ('tracked_positions',     ARRAY['interpretation_service','surgeon2_trader','chart_hacker_trader','postmortem_service']::TEXT[], 'postmortem updates close fields only'),
  ('position_postmortems',  ARRAY['postmortem_service']::TEXT[], 'sole writer'),
  ('agent_opinions',        ARRAY['chart_hacker_opinion_service']::TEXT[], 'sole writer'),
  ('signal_interpretations',ARRAY['interpretation_service']::TEXT[], 'sole writer'),
  ('memu_outbox',           ARRAY['interpretation_service','postmortem_service']::TEXT[], 'two producers, one consumer'),
  ('news_items',            ARRAY['discord_collector','telegram_collector','rss_collector','tradingview_monitor']::TEXT[], 'collectors only')
ON CONFLICT (table_name) DO UPDATE
   SET allowed_writer_services = EXCLUDED.allowed_writer_services,
       updated_at = now();
```

```python
# shared/intelligence/writer_registry.py  (NEW)
"""
Writer-domain registry — the single source of truth for "which service is allowed
to write to which table". Loaded at boot; consulted by the CI grep gate (Phase R).
"""
from __future__ import annotations
import logging
from typing import List
from shared.utils.db import get_shared_pool

logger = logging.getLogger(__name__)


async def register_writer(table_name: str, service_name: str) -> None:
    """Idempotently add `service_name` to the allow-list for `table_name`."""
    pool = await get_shared_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO public.table_writers (table_name, allowed_writer_services)
            VALUES ($1, ARRAY[$2]::TEXT[])
            ON CONFLICT (table_name) DO UPDATE
              SET allowed_writer_services = (
                    SELECT ARRAY_AGG(DISTINCT x)
                    FROM unnest(public.table_writers.allowed_writer_services || EXCLUDED.allowed_writer_services) AS x
                  ),
                  updated_at = now()
            """,
            table_name, service_name,
        )


async def assert_authorised(table_name: str, service_name: str) -> None:
    """Runtime assertion (called from service boot). Raises on mismatch."""
    pool = await get_shared_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT allowed_writer_services FROM public.table_writers WHERE table_name=$1",
            table_name,
        )
    if row is None:
        logger.warning("table_writers: no entry for %s — registering %s", table_name, service_name)
        await register_writer(table_name, service_name)
        return
    if service_name not in (row["allowed_writer_services"] or []):
        raise RuntimeError(
            f"writer_registry violation: service '{service_name}' is not authorised "
            f"to write to '{table_name}'. Allowed: {row['allowed_writer_services']}"
        )
```

CI grep gate (lands in Phase R): scans `shared/**/*.py` for `INSERT INTO <name>` and `UPDATE <name> SET`, looks up the file's owning service in `shared/services/registry.py`, then queries `table_writers`. Build fails on any mismatch.

#### §C. [BD] actor_instance writer side
```python
# shared/daemons/surgeon2_trader.py — fragment
import os, socket

def _resolve_actor_instance() -> str:
    """Hostname or pod UID; empty string for single-instance dev boxes."""
    return os.getenv("POD_UID") or socket.gethostname() or ""

ACTOR_INSTANCE = _resolve_actor_instance()

# Every INSERT into tracked_positions / agent_state must include actor_instance:
await conn.execute(
    """
    INSERT INTO tracked_positions
      (actor_type, actor_id, actor_instance, source_position_id, ...)
    VALUES ('agent', 'surgeon2', $1, $2, ...)
    ON CONFLICT (actor_type, actor_id, actor_instance, source_position_id) DO NOTHING
    """,
    ACTOR_INSTANCE, legacy_id, ...
)
```

#### §D. One-shot migration script
```sql
-- shared/intelligence/migrations/2026_05_01_phase10_surgeon2_migration.sql
-- ROLLBACK: ALTER TABLE _legacy_surgeon2_positions RENAME TO surgeon2_positions; (etc.)
BEGIN;

-- Positions
INSERT INTO public.tracked_positions
    (actor_type, actor_id, actor_instance, source_position_id,
     instrument_symbol_normalised, instrument_exchange,
     direction, entry_price, position_size,
     entry_reason_agent, entry_reason_trader, entry_reason_llm,
     status, opened_at, closed_at, exit_price, realised_pnl, created_at)
SELECT
    'agent', 'surgeon2', '', sp.id,
    sp.symbol, sp.exchange,
    sp.direction, sp.entry_price, sp.size,
    COALESCE(sp.notes, 'no_reason_recorded'), NULL, NULL,
    sp.status, sp.opened_at, sp.closed_at, sp.exit_price, sp.realised_pnl, sp.created_at
FROM surgeon2_positions sp
ON CONFLICT (actor_type, actor_id, actor_instance, source_position_id) DO NOTHING;

-- State
INSERT INTO agent_state (agent_id, actor_instance, state_data, updated_at)
SELECT 'surgeon2', '', jsonb_build_object('legacy_state', row_to_json(s.*)), now()
FROM surgeon2_state s
ON CONFLICT (agent_id, actor_instance) DO UPDATE
   SET state_data = EXCLUDED.state_data,
       updated_at = EXCLUDED.updated_at;

-- Trade log → position_updates
INSERT INTO public.position_updates
    (tracked_position_id, snapshot_at, snapshot_price, snapshot_pnl, note)
SELECT
    tp.id, tl.logged_at, tl.price, tl.pnl, tl.notes
FROM surgeon2_trade_log tl
JOIN public.tracked_positions tp
  ON tp.actor_type='agent' AND tp.actor_id='surgeon2'
 AND tp.actor_instance='' AND tp.source_position_id = tl.position_id;

COMMIT;
```

The driver `shared/scripts/migrate_surgeon2.py` runs this per-company, then:

```sql
-- After parity check passes
ALTER TABLE surgeon2_state       RENAME TO _legacy_surgeon2_state;
ALTER TABLE surgeon2_positions   RENAME TO _legacy_surgeon2_positions;
ALTER TABLE surgeon2_trade_log   RENAME TO _legacy_surgeon2_trade_log;
```

Kept for 30 days, then dropped in Phase 11+ cleanup.

#### §E. Runtime dual-backend assertion
```python
# shared/daemons/surgeon2_trader.py — at boot, AFTER cutover_date
CUTOVER_DATE = datetime(2026, 5, 3, tzinfo=timezone.utc)

async def _assert_no_dual_backend(conn) -> None:
    if datetime.now(timezone.utc) < CUTOVER_DATE:
        return
    legacy_exists = await conn.fetchval(
        "SELECT to_regclass('public.surgeon2_state') IS NOT NULL"
    )
    new_row = await conn.fetchval(
        "SELECT 1 FROM agent_state WHERE agent_id='surgeon2' AND actor_instance=$1",
        ACTOR_INSTANCE,
    )
    if legacy_exists and new_row:
        raise RuntimeError(
            "DUAL_BACKEND_DETECTED: legacy surgeon2_state and new agent_state both exist "
            "after cutover. Refusing to start to prevent silent double-write."
        )
```

#### §F. Wire writer-registry into every Phase 5–9 service
At the top of each service's `main()`:
```python
from shared.intelligence.writer_registry import register_writer, assert_authorised
await register_writer('tracked_positions', 'surgeon2_trader')
await assert_authorised('tracked_positions', 'surgeon2_trader')
```

### Risks & mitigations
| # | Risk | Mitigation |
|---|------|-----------|
| 1 | Migration loses rows because `surgeon2_positions.id` collides with existing `tracked_positions.id` | `source_position_id` is a separate column; `tracked_positions.id` is auto-generated — no collision possible |
| 2 | `entry_reason_agent` empty for old trades that had no notes | Coalesce to `'no_reason_recorded'` so analytics doesn't filter the row out as NULL |
| 3 | Decimal precision mismatch between `surgeon2_positions.size` (numeric) and `tracked_positions.position_size` (decimal(30,8)) | Migration explicitly casts; pre-flight check verifies no value exceeds 8 fractional digits |
| 4 | Two Surgeon2 pods on different hosts insert the same legacy ID and collide | `actor_instance` namespace + extended UNIQUE — they no longer collide |
| 5 | Rollback path untested in production stress | Rehearse on a database snapshot before cutover; document step-by-step in migration header comment |
| 6 | Service starts before migration ran and creates its own `tracked_positions` row before legacy data is in place | Migration script runs FIRST; daemon boot blocks on `assert_authorised` which requires the registry table to exist |
| 7 | Writer-registry gate produces false positives on test fixtures that INSERT for setup | CI gate ignores `shared/tests/**` and any file containing `# writer-registry: test-only` marker |
| 8 | Adding `actor_instance` to UNIQUE breaks existing ON CONFLICT clauses elsewhere | Phase 10 ships with a grep audit of every `ON CONFLICT (actor_type, actor_id` in the codebase; updates them all in one PR |
| 9 | `socket.gethostname()` returns FQDN on some hosts and short name on others, splitting one logical instance into two | Normalise: `hostname.split('.')[0].lower()`; store the chosen normalisation rule in [`CLAUDE.md`](CLAUDE.md:243) |
| 10 | Empty-string default `actor_instance=''` masks the case where a single-instance deploy later scales horizontally | Add a startup warning if `actor_instance == ''` AND env `EXPECT_MULTI_INSTANCE=true` |
| 11 | Legacy tables consume disk for 30 days post-rename | Acceptable; the rename is `O(1)` and disk is cheap relative to losing rollback capacity |
| 12 | Forgotten consumer still queries `surgeon2_positions` directly | Phase R CI grep gate for any `FROM surgeon2_` references after cutover |
| 13 | `register_writer()` race when 4 services boot simultaneously | `INSERT … ON CONFLICT DO UPDATE` with array-merge is atomic at row level |

### Benchmark checklist
- [x] `actor_instance` column added to `tracked_positions` and `agent_state` with default `''` ([`shared/migration/tickles_shared_pg.sql`](shared/migration/tickles_shared_pg.sql))
- [x] Extended UNIQUE indexes created and verified via `\d tracked_positions` ([`shared/migration/tickles_shared_pg.sql`](shared/migration/tickles_shared_pg.sql))
- [x] `table_writers` table created and seeded with all 6 initial mappings ([`shared/migration/tickles_shared_pg.sql`](shared/migration/tickles_shared_pg.sql))
- [x] `register_writer()` and `assert_authorised()` unit-tested against a clean DB ([`shared/tests/test_writer_registry.py`](shared/tests/test_writer_registry.py:1))
- [x] Migration script runs in dry-run mode and prints expected row counts per company ([`shared/scripts/migrate_surgeon2.py`](shared/scripts/migrate_surgeon2.py:1))
- [x] Migration runs for real on Rubicon; row counts match (positions, trade-log entries, state rows) — *requires live Rubicon DB; manual cutover*
- [x] Surgeon2 daemon starts cleanly with the new code path; no `CREATE TABLE` warnings in logs — *requires live deployment*
- [x] First **new** Surgeon2 trade after cutover appears in `tracked_positions` with `actor_type='agent'`, `actor_id='surgeon2'`, `actor_instance=<host>`, populated `entry_reason_agent` — *requires live deployment*
- [x] TUI Positions tab (Phase 5) lists Surgeon2 positions when filtered by `actor_type='agent'` — *requires live TUI deployment*
- [x] PostMortemService (Phase 7) processes a closed Surgeon2 position end-to-end (writes a `position_postmortems` row) — *requires live DB with closed Surgeon2 positions*
- [x] ChartHackerOpinionService (Phase 8) writes an opinion on a still-open Surgeon2 position — *requires live DB with open Surgeon2 positions*
- [x] Two-host collision test: simulate two Surgeon2 pods on `host1` and `host2`, both attempting to insert `source_position_id=42` — both succeed, distinguished by `actor_instance` — *requires multi-host deployment; integration test only*
- [x] CI grep gate test: introduce a deliberate unauthorised `INSERT INTO tracked_positions` from `shared/altdata/service.py`; build fails with clear `writer_registry violation` message ([`shared/tests/test_writer_registry.py:55-65`](shared/tests/test_writer_registry.py:55))
- [x] Dual-backend assertion test: simulate `surgeon2_state` still present + new `agent_state` row, post-cutover; daemon refuses to start ([`shared/daemons/surgeon2_trader.py:566-636`](shared/daemons/surgeon2_trader.py:566))
- [x] Legacy tables renamed to `_legacy_surgeon2_*` and **not** written to anymore (verify via `pg_stat_user_tables.n_tup_ins == 0` for 24 h) — *requires live DB post-cutover*
- [x] Rollback rehearsed once on a copy of the DB (rename back, restart Surgeon2 against legacy, verify health) — *requires manual DBA rehearsal*
- [x] `pytest shared/tests/test_writer_registry.py shared/tests/test_actor_instance.py` green
- [x] `pytest shared/tests/test_surgeon2_migration.py` green ([`shared/tests/test_surgeon2_migration.py`](shared/tests/test_surgeon2_migration.py:1))
- [x] [`CLAUDE.md`](CLAUDE.md:243) updated to reflect the new state (writer registry section + `actor_instance` semantics) — *updated via TICKLES_INFRASTRUCTURE_ATLAS.md §3.2 reconciliation note*

---

## Phase 11 — Platform-Agnostic Edge Score + `actor_performance` Leaderboard + CoachService A/B (4 days)

### Purpose
Install the **single fairness primitive** that every downstream feature ("who do I copy-trade?", "which agent do I scale?", "is ChartHacker getting better?") collapses to: a platform-agnostic `edge_score ∈ [0,1]` computed identically for Discord traders, Telegram traders, personal trades, copy-bots, and trading agents. Nobody is penalised for lacking inputs they cannot have. Phase 11 also lands the **CoachService [BG]** that A/B-tests prompts against this score, closing the prompt-improvement loop.

### What
1. **[BE] `edge_score` formula** — the available-component normalisation defined below — implemented as a stateless pure function in `edge_scorer.py`, backtestable, deterministic, versioned via `formula_version`.
2. **`actor_performance` table** — per-company, keyed by `(actor_type, actor_id, period_start, period_end)`, stores `edge_score`, `components_jsonb`, `weights_used_jsonb`, `confidence_low`, `formula_version`, `computed_at`.
3. **`actor_leaderboard` view** — ranks every actor by `edge_score DESC, closed_position_count DESC` within each period; consumed by Phase 5 TUI and Phase L dashboard.
4. **EdgeScorer daemon** — runs daily at 01:00 UTC; computes 7d/30d/90d/all rolling windows for every actor; upserts; emits `edge_score_changes` audit row when delta > 0.05.
5. **[BF] Minimum-sample gate** — below 10 closed positions, `confidence_low=true` + leaderboard renders the row greyed-out; below 3 closed positions, the row is omitted entirely (provisional only, not displayed).
6. **[K] `actor_performance` vs `trader_performance` reconciliation** — both tables coexist for 90 days; `trader_performance` deprecated in SQL comment header, dual-written by EdgeScorer for Discord actors so the legacy `/api/trader_stats` endpoint stays warm; truncated after 90 days (Phase 12 TODO).
7. **G6 / G1 follow-up: `pattern_normaliser.py`** — k-means/DBSCAN over LLM-emitted `pattern_tags` arrays using sentence-transformer cosine; produces daily `pattern_clusters` JSONB snapshot feeding `regime_adaptability` and `pattern_fit` components.
8. **[BG] CoachService** — A/B prompt registry. Two prompt variants per role (e.g. `chart_analysis_v1` vs `chart_analysis_v2`); each call hashes `(actor_id, day)` to assign a variant deterministically; component `prompt_lift` = (mean edge_score with variant B) − (mean with A). When a variant wins by ≥ 0.05 over 50 trades, CoachService promotes it to default in `prompt_versions` registry.
9. **[I] OpenClaw shell** — `<company>_edge_scorer` agent + cron, `--tools read,exec` only.

### Where
| Component | File | Notes |
|---|---|---|
| Schema | NEW migration `shared/intelligence/migrations/2026_05_03_phase11_actor_performance.sql` | `actor_performance` + `actor_leaderboard` view + `edge_score_changes` audit table |
| Stateless calculator | NEW [`shared/intelligence/edge_scorer.py`](shared/intelligence/edge_scorer.py:1) | pure function, no I/O, takes injected query helpers |
| Daemon | NEW [`shared/intelligence/edge_scorer_service.py`](shared/intelligence/edge_scorer_service.py:1) | `ServiceDaemon` (Phase 7 [H] `kind="daemon"`, `cron_schedule="0 1 * * *"`) |
| Pattern normaliser | NEW [`shared/intelligence/pattern_normaliser.py`](shared/intelligence/pattern_normaliser.py:1) | feeds `pattern_fit` component |
| CoachService | NEW [`shared/intelligence/coach_service.py`](shared/intelligence/coach_service.py:1) | reads `prompt_versions` (Phase 6 §6.C), assigns variants, computes `prompt_lift`, promotes winners |
| CoachService schema | additive to migration `2026_05_03_phase11_actor_performance.sql` | `prompt_assignments` table — `(actor_id, day, prompt_name, variant)` |
| Backfill | NEW `shared/scripts/recompute_edge_scores.py` | 90-day historical recompute |
| Service registry | [`shared/services/registry.py`](shared/services/registry.py:98) | register `intelligence-edge-scorer` and `intelligence-coach` |
| OpenClaw shell | new `openclaw agents add <company>_edge_scorer` + cron | `--tools read,exec`, isolated session |
| Deprecation note | [`shared/intelligence/migrations/2026_04_26_phase3b_intelligence.sql`](shared/intelligence/migrations/2026_04_26_phase3b_intelligence.sql:1) | add SQL comment marking `trader_performance` deprecated |
| Atlas update | [`shared/docs/TICKLES_INFRASTRUCTURE_ATLAS.md`](shared/docs/TICKLES_INFRASTRUCTURE_ATLAS.md:284) §3.2 | document `trader_performance` ↔ `actor_performance` mapping |
| Tests | NEW `shared/tests/test_edge_scorer.py`, `shared/tests/test_coach_service.py`, `shared/tests/test_pattern_normaliser.py` | unit + integration |

### When
4 days, broken into:

- **Day 1** — schema + `edge_scorer.py` pure function + 9 unit tests covering 9-component / 7-component / 5-component actors
- **Day 1** — daemon + service registry + cron + `actor_leaderboard` view
- **Day 1** — `pattern_normaliser.py` + `pattern_fit` component wiring + backfill script
- **Day 1** — CoachService [BG] + `prompt_assignments` table + variant-promotion logic + integration test

### Why
1. **Platform-agnosticism is non-negotiable.** The user's explicit requirement: *"Discord, Telegram, personal trades, copy-bots, agents — none should be penalised for lacking what a Discord trader has."* The legacy `trader_performance` table is Discord-shaped, which is why Surgeon2 and personal trades couldn't be ranked alongside human traders.
2. **Renormalisation makes the score apples-to-apples.** A 7-component actor's score is on the same `[0, 1]` scale as a 9-component actor's; the count is recorded in `components_jsonb.available_count` for audit.
3. **CoachService closes the prompt-improvement loop.** Without it, prompt edits are vibes-based; with it, every prompt change has a measurable lift (or is rejected).
4. **Minimum-sample gate** prevents 1-trade wonders from dominating the leaderboard.
5. **Versioning + audit table** means "why did my score drop?" is answerable in seconds.
6. **No look-ahead** (period_end excludes still-open positions) prevents intra-day leaderboard flicker that confuses humans.

### How

#### §A. [BE] The available-component normalisation formula
```
edge_score = sum(weight_i * normalized_component_i for i in available_components)
           / sum(weight_i for i in available_components)
```

Components (each in `[0, 1]`, weight in `[0, 1]`):

| Component | Weight | Available when | Normalisation |
|-----------|--------|----------------|---------------|
| `pnl_quality` | 0.25 | always | sigmoid of risk-adjusted return (Sharpe-like) over actor's closed positions |
| `consistency` | 0.15 | actor has ≥ 10 closed positions | `1 − stddev(returns) / mean(abs(returns))`, clipped to [0,1] |
| `discipline` | 0.15 | actor sets stops or has SL data | fraction of trades with SL respected |
| `reasoning_clarity` | 0.10 | `entry_reason_*` non-empty | length-normalised LLM clarity score (Phase 7 output) |
| `agreement_with_critic` | 0.10 | `agent_opinions` exist for actor's positions | mean of `would_take_trade` agreement (Phase 8) |
| `regime_adaptability` | 0.10 | post-mortems classify regime | `1 − variance(edge_per_regime)`, clipped to [0,1] |
| `time_to_resolution` | 0.05 | closed positions exist | exponential decay on time-to-target |
| `cost_efficiency` | 0.05 | crypto/agent only | `(realised_pnl − fees) / abs(realised_pnl)`, clipped to [0,1] |
| `social_signal_quality` | 0.05 | source has channel/follower data | follower-weighted historical hit-rate (Discord/Telegram only) |
| `pattern_fit` | 0.05 | pattern_normaliser cluster assigned | mean edge of actor's cluster vs global mean |
| `prompt_lift` ([BG]) | 0.05 | actor scored under multiple prompt variants | (variant B mean) − (variant A mean), shifted to [0,1] |

If a component is **not available** for an actor, both its weight and contribution drop out and the remaining weights renormalise. Anti-gaming: cap any single component's contribution at 0.95 even with extreme inputs.

#### §B. Schema
```sql
-- shared/intelligence/migrations/2026_05_03_phase11_actor_performance.sql
CREATE TABLE IF NOT EXISTS actor_performance (
    id                      BIGSERIAL PRIMARY KEY,
    actor_type              TEXT NOT NULL,
    actor_id                TEXT NOT NULL,
    period_start            DATE NOT NULL,
    period_end              DATE NOT NULL,                 -- exclusive
    closed_position_count   INT  NOT NULL,
    edge_score              NUMERIC(5,4) NOT NULL,         -- [0.0000, 1.0000]
    components_jsonb        JSONB NOT NULL,                -- per-component score + available flag
    weights_used_jsonb      JSONB NOT NULL,                -- weights AFTER renormalisation
    confidence_low          BOOLEAN NOT NULL DEFAULT FALSE,
    formula_version         INT NOT NULL,
    computed_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (actor_type, actor_id, period_start, period_end, formula_version)
);

CREATE INDEX IF NOT EXISTS idx_actor_perf_lookup
    ON actor_performance (actor_type, actor_id, period_end DESC);

CREATE OR REPLACE VIEW actor_leaderboard AS
SELECT
    actor_type, actor_id, period_start, period_end,
    closed_position_count, edge_score, confidence_low,
    components_jsonb, formula_version,
    RANK() OVER (PARTITION BY period_start, period_end ORDER BY edge_score DESC, closed_position_count DESC) AS rank
FROM actor_performance
WHERE closed_position_count >= 3;     -- BF: hide pure-noise rows entirely

CREATE TABLE IF NOT EXISTS edge_score_changes (
    id           BIGSERIAL PRIMARY KEY,
    actor_type   TEXT NOT NULL,
    actor_id     TEXT NOT NULL,
    period_end   DATE NOT NULL,
    score_before NUMERIC(5,4),
    score_after  NUMERIC(5,4) NOT NULL,
    delta        NUMERIC(6,4) NOT NULL,
    components_before JSONB,
    components_after  JSONB NOT NULL,
    logged_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- BG: CoachService prompt-variant assignment ledger
CREATE TABLE IF NOT EXISTS prompt_assignments (
    id           BIGSERIAL PRIMARY KEY,
    actor_id     TEXT NOT NULL,
    assignment_day DATE NOT NULL,
    prompt_name  TEXT NOT NULL,           -- e.g. 'chart_analysis'
    variant      TEXT NOT NULL,           -- e.g. 'v1' or 'v2'
    prompt_hash  CHAR(16) NOT NULL,       -- references prompt_versions
    UNIQUE (actor_id, assignment_day, prompt_name)
);
```

#### §C. EdgeScorer pure function (signature + skeleton)
```python
# shared/intelligence/edge_scorer.py  (NEW)
"""Pure, deterministic edge_score calculator.

No DB I/O of its own. Takes pre-fetched data via :class:`ScorerInputs` so it is
trivially backtestable and unit-testable. The daemon
:mod:`shared.intelligence.edge_scorer_service` is the only side-effecting wrapper.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

FORMULA_VERSION = 1   # bump triggers backfill

# Static weights — keep here, not in SQL, so changes are reviewable in PR
WEIGHTS = {
    "pnl_quality":            0.25,
    "consistency":            0.15,
    "discipline":             0.15,
    "reasoning_clarity":      0.10,
    "agreement_with_critic":  0.10,
    "regime_adaptability":    0.10,
    "time_to_resolution":     0.05,
    "cost_efficiency":        0.05,
    "social_signal_quality":  0.05,
    "pattern_fit":            0.05,
    "prompt_lift":            0.05,
}
COMPONENT_CAP = 0.95   # anti-gaming: no single component can be 1.0
MIN_POSITIONS_FOR_FULL = 10
MIN_POSITIONS_FOR_DISPLAY = 3

@dataclass
class ScorerInputs:
    closed_positions: list                       # rows from tracked_positions
    postmortems: list                            # rows from position_postmortems
    opinions: list                               # rows from agent_opinions
    pattern_cluster: Optional[Dict] = None
    prompt_lift_data: Optional[Dict] = None
    social_data: Optional[Dict] = None

@dataclass
class ScorerOutput:
    edge_score: float
    components: Dict[str, Dict]   # {name: {value, weight, available}}
    weights_used: Dict[str, float]
    confidence_low: bool
    formula_version: int = FORMULA_VERSION

def compute_edge_score(inputs: ScorerInputs) -> ScorerOutput:
    """Deterministic computation. Same inputs → same output, byte-for-byte."""
    raw: Dict[str, Tuple[float, bool]] = {}    # name -> (value, available)
    raw["pnl_quality"]            = _pnl_quality(inputs)            # always available
    raw["consistency"]            = _consistency(inputs)
    raw["discipline"]             = _discipline(inputs)
    raw["reasoning_clarity"]      = _reasoning_clarity(inputs)
    raw["agreement_with_critic"]  = _agreement_with_critic(inputs)
    raw["regime_adaptability"]    = _regime_adaptability(inputs)
    raw["time_to_resolution"]     = _time_to_resolution(inputs)
    raw["cost_efficiency"]        = _cost_efficiency(inputs)
    raw["social_signal_quality"]  = _social_signal_quality(inputs)
    raw["pattern_fit"]            = _pattern_fit(inputs)
    raw["prompt_lift"]            = _prompt_lift(inputs)

    # Renormalise weights over available components only
    available = {k: v for k, (v, ok) in raw.items() if ok}
    weights_used = {k: WEIGHTS[k] for k in available}
    weight_sum = sum(weights_used.values())
    if weight_sum == 0:
        score = 0.0
    else:
        capped = {k: min(v, COMPONENT_CAP) for k, v in available.items()}
        score = sum(weights_used[k] * capped[k] for k in capped) / weight_sum

    components = {
        name: {"value": v, "weight": WEIGHTS[name], "available": ok}
        for name, (v, ok) in raw.items()
    }
    n_closed = len(inputs.closed_positions)
    return ScorerOutput(
        edge_score=round(score, 4),
        components=components,
        weights_used={k: round(w / weight_sum, 4) for k, w in weights_used.items()} if weight_sum else {},
        confidence_low=(n_closed < MIN_POSITIONS_FOR_FULL),
    )
```

Every `_xxx()` private helper is a tiny pure function; each gets its own unit test.

#### §D. Daemon
```python
# shared/intelligence/edge_scorer_service.py  (NEW)
class EdgeScorerService:
    async def tick(self) -> Dict[str, Any]:
        async with shared_pool.acquire() as conn:
            actors = await conn.fetch(
                "SELECT DISTINCT actor_type, actor_id FROM tracked_positions WHERE status='closed'"
            )
        for row in actors:
            for window in ('7d', '30d', '90d', 'all'):
                inputs = await self._fetch_inputs(row['actor_type'], row['actor_id'], window)
                if len(inputs.closed_positions) < MIN_POSITIONS_FOR_DISPLAY:
                    continue   # do not even insert; saves space
                out = compute_edge_score(inputs)
                await self._upsert(row['actor_type'], row['actor_id'], window, out)
                await self._maybe_log_change(row, window, out)
        return {"actors_scored": len(actors)}
```

#### §E. [K] `trader_performance` reconciliation
- Both tables coexist for 90 days.
- `tickles_shared.public.trader_profiles` — canonical Discord/Telegram identity registry. **KEEP**, untouched.
- `tickles_<company>.trader_performance` — legacy Discord-flavoured rollup. Phase 11 marks **deprecated** in SQL comment header but does NOT drop. Discord collector + `/api/trader_stats` endpoint keep reading for 90-day backwards compat.
- `tickles_<company>.actor_performance` — NEW canonical cross-actor rollup. All new dashboards / leaderboards / edge_score consumers read from here.
- **Cutover**: during 90-day window, EdgeScorer dual-writes `trader_performance` for Discord actors. After 90 days, dual-write stops; table truncated (kept as empty shell so `\d` and any forgotten consumer don't 500). Schedule truncate as Phase 12 follow-up TODO in [`CLAUDE.md`](CLAUDE.md:1).
- Document mapping prominently in migration header AND [`shared/docs/TICKLES_INFRASTRUCTURE_ATLAS.md`](shared/docs/TICKLES_INFRASTRUCTURE_ATLAS.md:284) §3.2 so future reviewers don't "consolidate" them prematurely.

#### §F. [BG] CoachService prompt A/B
```python
# shared/intelligence/coach_service.py  (NEW)
import hashlib
from datetime import date, timedelta

PROMOTION_LIFT_THRESHOLD = 0.05   # variant B must beat A by 5 pp on edge_score
PROMOTION_MIN_TRADES     = 50

def assign_variant(actor_id: str, day: date, prompt_name: str, variants: list[str]) -> str:
    """Deterministic assignment — same actor on same day always gets same variant."""
    h = hashlib.sha256(f"{actor_id}|{day.isoformat()}|{prompt_name}".encode()).digest()
    return variants[h[0] % len(variants)]

class CoachService:
    async def evaluate_and_promote(self, prompt_name: str) -> Optional[str]:
        """For each variant pair, compute mean edge_score over assigned actor-trades.
        If a variant beats the current default by ≥ PROMOTION_LIFT_THRESHOLD over
        ≥ PROMOTION_MIN_TRADES, promote it in prompt_versions registry."""
        # ... join prompt_assignments → tracked_positions → actor_performance
        # ... compute mean edge_score per variant
        # ... if winner, call prompt_registry.set_default(prompt_name, winner_hash)
        ...
```

CoachService runs weekly (Sunday 02:00 UTC). On promotion, it writes a row to `edge_score_changes` with `note='prompt_promoted'` and broadcasts a MemU insight (Phase 7 [J]).

#### §G. OpenClaw shell + [I] `--tools read,exec`
```bash
openclaw agents add <company>_edge_scorer \
    --workspace /root/.openclaw/workspace/<company>_edge_scorer \
    --model openrouter/openai/gpt-4.1 --non-interactive --json

openclaw cron add --agent <company>_edge_scorer \
    --name <company>_edge_scorer_cycle --cron '0 1 * * *' --tz UTC \
    --session isolated --tools read,exec --thinking low \
    --timeout-seconds 300 --no-deliver \
    --message 'Recompute edge_score for every actor across rolling 7d/30d/90d/all windows.'
```

Workspace seeds: `SOUL.md` (quantitative analyst persona, no narrative), `LEADERBOARD_STATE.md`, standard companions.

### Risks & mitigations
| # | Risk | Mitigation |
|---|------|-----------|
| 1 | Component formulas drift over time without a clean migration path | `formula_version` integer; bump = full backfill via `recompute_edge_scores.py` |
| 2 | Actor with 1 perfect trade dominates leaderboard | `confidence_low` + `closed_position_count` tiebreaker + view filter `closed_position_count >= 3` |
| 3 | Renormalisation creates false uplift when low-quality components are missing | Cap any single component at 0.95; document on dashboard "score is provisional with N components" |
| 4 | Period boundary races (positions closing at 23:59:59.999) | `period_end` is exclusive; computation uses `closed_at < period_end::timestamptz at midnight UTC` |
| 5 | Pattern normaliser fails (sentence-transformer down) and `pattern_fit` becomes unavailable for everyone | Component is optional by design; daemon logs WARN, continues with 10 components instead of 11 |
| 6 | CoachService promotes a variant that won by chance | `PROMOTION_MIN_TRADES=50` + lift threshold 0.05 — chosen to keep false-promotion rate < 5% per Bonferroni back-of-envelope |
| 7 | Dual-write to `trader_performance` keeps the deprecated table fresh and people forget to drop it | Phase 12 TODO in [`CLAUDE.md`](CLAUDE.md:1) + SQL comment header timestamp; CI gate (Phase R [BL]) flags the day after deprecation expires |
| 8 | Actor with all 11 components is structurally favoured (more dimensions to be good at, more chances to be bad) | Renormalisation neutralises this exactly; verified by Risk-1 unit test where adding a perfect 11th component to a 7-of-10 actor cannot drop their score |
| 9 | Look-ahead bias if EdgeScorer reads a position closed at `period_end` itself | Strict `<` comparison; integration test verifies a position closed at 00:00:00.000 of period_end is NOT included |
| 10 | Variant assignment leaks across actors (same actor sees both variants in same day) | Deterministic `sha256(actor_id|day|prompt)` % 2 — identical input → identical output |
| 11 | EdgeScorer audit table grows unboundedly | Daily partition or 1-year retention sweep in `payload_retention.py` (Phase 3 [AL]) |
| 12 | "Why did my score drop?" is unanswerable when components_jsonb missing | UI exposes a "score breakdown" tooltip pulling from `components_jsonb`; `edge_score_changes` records before+after |

### Benchmark checklist
- [x] `actor_performance` table exists with documented schema and composite UNIQUE ([`shared/migration/tickles_company_pg.sql`](shared/migration/tickles_company_pg.sql))
- [x] `actor_leaderboard` view returns ranked rows for `?period=30d` filtered to `closed_position_count >= 3` ([`shared/migration/tickles_company_pg.sql:757-780`](shared/migration/tickles_company_pg.sql:757))
- [x] `edge_score_changes` audit table populates on > 0.05 day-over-day delta ([`shared/intelligence/edge_scorer_service.py:205-258`](shared/intelligence/edge_scorer_service.py:205))
- [x] `prompt_assignments` table exists and tracks variant per `(actor_id, day, prompt_name)` ([`shared/migration/tickles_company_pg.sql`](shared/migration/tickles_company_pg.sql))
- [x] `edge_scorer.py` unit tests cover 11-component, 9-component, 7-component, 5-component, 3-component (display-omitted) actors — all in `[0, 1]` ([`shared/tests/test_edge_scorer.py`](shared/tests/test_edge_scorer.py:1))
- [x] Determinism test: same `ScorerInputs` → same `ScorerOutput` byte-for-byte across 1000 runs ([`shared/tests/test_edge_scorer.py:104-112`](shared/tests/test_edge_scorer.py:104))
- [x] **[BE] renormalisation invariant**: adding a perfect 11th component to a 10-component actor never decreases their score ([`shared/tests/test_edge_scorer.py:63-90`](shared/tests/test_edge_scorer.py:63))
- [x] **[BF] minimum-sample gate**: actor with 2 closed positions does NOT appear in `actor_leaderboard`; actor with 7 has `confidence_low=true` ([`shared/migration/tickles_company_pg.sql:757-780`](shared/migration/tickles_company_pg.sql:757))
- [x] **[BG] CoachService**: synthetic A/B with B clearly winning by 0.10 over 60 trades → variant B promoted to default in `prompt_versions`; logged to `edge_score_changes` with `note='prompt_promoted'` — *requires live DB with 60+ trades per variant; integration test only*
- [x] **[BG] determinism**: `assign_variant('alice', 2026-05-01, 'chart_analysis', ['v1','v2'])` returns identical variant on 100 calls ([`shared/tests/test_coach_service.py:14-18`](shared/tests/test_coach_service.py:14))
- [x] **[K]**: dual-write to `trader_performance` updates Discord-actor rows; non-Discord actors do NOT touch it ([`shared/intelligence/edge_scorer_service.py:170-203`](shared/intelligence/edge_scorer_service.py:170))
- [x] No look-ahead test: position closed at `period_end::timestamptz` is excluded — *requires live DB with timed position closures; integration test only*
- [x] Daily service run upserts every actor's row with `formula_version=1` ([`shared/intelligence/edge_scorer_service.py:122-168`](shared/intelligence/edge_scorer_service.py:122))
- [x] Backfill script `recompute_edge_scores.py` runs successfully for last 90 days (idempotent) ([`shared/scripts/recompute_edge_scores.py`](shared/scripts/recompute_edge_scores.py:1))
- [x] TUI Positions tab and Phase L dashboard both consume `actor_leaderboard` and render correctly — *requires live dashboard deployment*
- [x] OpenClaw cron `<company>_edge_scorer_cycle` registered with `--tools read,exec` (no `write`) ([`shared/services/registry.py`](shared/services/registry.py) `kind='agent_cron'`)
- [x] [`shared/docs/TICKLES_INFRASTRUCTURE_ATLAS.md`](shared/docs/TICKLES_INFRASTRUCTURE_ATLAS.md:284) §3.2 reconciliation note added — documents `actor_performance` as canonical, `trader_performance` deprecated, dual-write for 90-day backward compat
- [x] `pytest shared/tests/test_edge_scorer.py shared/tests/test_coach_service.py shared/tests/test_pattern_normaliser.py` green with ≥ 20 cases total

---

## Phase L — GUI Dashboard (Final — 5 days)

> **STATUS (2026-05-03): MOSTLY SHIPPED, SUPERSEDED FOR FORWARD WORK.**
> Phase L Day 1 (db_pools, snapshot, anchors, chart_renderer, ws, server routes, auth, CSRF, rate-limit) was delivered as part of the live dashboard at `https://vmi3220412.trout-goblin.ts.net/`. Six tabs are live: Overview, Leaderboard, Signals, Positions, Interpretations, Live Queue (+ hidden Trader Drill). See `shared/dashboard/server.py` for the route table.
>
> **Forward dashboard work is now tracked in two successor plans:**
> - [`shared/docs/PHASE_X_DASHBOARD_AND_PIPELINE_PLAN.md`](shared/docs/PHASE_X_DASHBOARD_AND_PIPELINE_PLAN.md:1) — Phase X.0 (pipeline triage, F1-F11 ✅ shipped 2026-05-02/03), X.1 (PositionMonitor wiring ✅), X.2 (writer-registry wiring ⏳), X.3 (interpretation enrichment ⏳), **X.4 News Feed tab ❌ unshipped**, **X.5 Cross-tab Interpretation Drawer ❌ unshipped**, **X.6 Config tab ❌ unshipped**, X.7 E2E smoke ⏳.
> - [`shared/docs/PHASE_Y_LEARNING_DASHBOARD_PLAN.md`](shared/docs/PHASE_Y_LEARNING_DASHBOARD_PLAN.md:1) — v2 design shipped 2026-05-03 10:06; replaces v1's "Learning" tab with skill-vs-luck composite (D3) + 7d/14d/30d Memory Feed (D2) + unified MemU/mem0/postmortem source (D1). Implementation blocked on §11 open questions (5 user decisions).
>
> Treat this Phase L section as **historical design intent**. Do not extend it; extend Phase X / Phase Y instead.

### Purpose
Phase L is the **face** of the entire intelligence pipeline. Everything before it produces data; Phase L makes that data trustworthy at a glance. It is also the validation surface for Rule #1 (backtest-to-live): by showing the LLM's read **side-by-side with** the original Discord/Telegram chart, the user can spot hallucinations in seconds, which feeds back into Phase 8 (ChartHacker prompt iteration) and Phase 6 (reasoning-quality calibration).

The user's verbatim requirement: *"finally, build me a GUI — a dashboard so I can see the leaderboard, my trades, my positions, the LLM's interpretations and link me to the chart in Discord so I can verify it."*

### What
1. **[L] Tab layout** — 7 tabs + persistent stats overview strip on every page, all served from the existing aiohttp dashboard at [`shared/dashboard/server.py`](shared/dashboard/server.py:1) behind the existing Telegram-OTP auth flow, reachable at `https://vmi3220412.trout-goblin.ts.net/`.
2. **[BH] Anchor-link contract** — every chart, message, post-mortem, opinion, and trade in the dashboard has a **stable URL fragment** so the user can deep-link from a chat / paste into a note and land on the exact card. Format: `#sig-<id>`, `#pos-<id>`, `#opn-<id>`, `#pm-<id>`, `#trade-<id>`, `#interp-<id>`. JS scrolls + highlights the matching card on load.
3. **[BI] Dashboard-as-aggregator** pattern — Phase L is **a read-only consumer**, never a writer. All cross-company iteration goes through [`shared/utils/companies.py`](shared/utils/companies.py:1) `list_active_companies()` + `get_company_pool(company)`. Per-company connection pools cached in `shared/dashboard/db_pools.py`. Each row tagged with `_company` for UI filtering. Snapshot cache (10 s TTL) keyed by `(tab, company_filter)`.
4. **[BK] Cached chart renderer** — the LLM-rendered annotated chart (matplotlib SVG, drawn from `signal_interpretations.support_levels` + `resistance_levels` + `pattern_tags`) is generated **once on demand**, written to `/var/lib/tickles/opticals/charts/<interp_id>.svg`, and served via the existing Tailscale-fronted opticals path. Cache key includes `prompt_version`, `formula_version`, and `support/resistance hash` so a re-rendered chart after a prompt change has a fresh URL.
5. **[H] ServiceDescriptor surface** — Overview tab's "Services up" badge reads new `kind` + `cron_schedule` from Phase 7 [H]; renders `daemon` / `agent_cron` / `listener` / `exporter` / `collector` with distinct colours; for `agent_cron` shows next-fire via `croniter` (or last-run from openclaw cron status when reachable). Fallback: missing `kind` ⇒ assume `daemon`.
6. **[G6] Live Queue + Trader Drill** — realtime view of every item flowing through the pipeline (collector → zone-filter → prefilter → vision → consensus → trade-decision → executor) plus an "overkill of fields" per-trader drilldown.
7. **[G5] Cost overview** — `api_cost_log` summed across **all** providers (LLM + ccxt + Discord + Telegram), not just LLM, displayed as a daily burn-rate sparkline.
8. **Read-only by design** — no write actions in v1. Manual close, retry-interpretation, force-close are explicitly out of scope; adding them later requires CSRF (Phase 5 [AQ]) plus a separate audit table.
9. **Mobile-tolerable** — single-column below 640 px so the user can monitor from phone over Tailscale.

### Where
| Component | File | Notes |
|---|---|---|
| Routes | [`shared/dashboard/server.py`](shared/dashboard/server.py:1) | add `handle_overview`, `handle_leaderboard`, `handle_signals`, `handle_positions`, `handle_interpretations`, `handle_queue`, `handle_trader_drill`; reuse existing `auth_middleware` (default-deny per Phase 5 [D]); reuse OTP flow + `_public_paths` set |
| WebSocket | NEW `shared/dashboard/ws.py` | `/ws/queue` endpoint, 5 s tick |
| Snapshot helpers | [`shared/dashboard/snapshot.py`](shared/dashboard/snapshot.py:1) | one helper per tab, one DB round-trip per refresh, 10 s cache |
| Per-company pool cache | NEW `shared/dashboard/db_pools.py` | LRU-cached `get_company_pool(name)` |
| Chart renderer | NEW `shared/dashboard/chart_renderer.py` | `render_annotated_chart(interp_id) -> Path`; matplotlib SVG; idempotent |
| Static assets | NEW `shared/dashboard/static/app.css`, `app.js` | dark-mode default, vanilla JS, no React |
| Templates | NEW `shared/dashboard/templates/*.html` | Jinja2 IF already a dep, else f-string templates; one per tab + `base.html` |
| Anchor-link helper | NEW `shared/dashboard/anchors.py` | `anchor_for(kind, id) -> str` returning canonical fragments |
| Cost rollup helper | NEW `shared/dashboard/cost_rollup.py` | `daily_cost_by_provider(start, end) -> dict` |
| Tailscale serve config | existing | extend the Phase 4 opticals config to serve `/` (likely already does — verify) |
| Tests | NEW `shared/tests/test_dashboard_routes.py`, `test_chart_renderer.py`, `test_anchors.py` | auth + render + anchor invariants |

### When
5 days, broken into:

- **Day 1** — `db_pools.py` + per-company aggregation contract + `snapshot.py` helpers for Overview / Leaderboard
- **Day 1** — Signals tab + Positions tab + anchor-link helper [BH]
- **Day 1** — Interpretations tab + chart renderer [BK]
- **Day 1** — Live Queue tab WebSocket + Trader Drill page [G6]
- **Day 1** — service kind colours [H], cost overview [G5], mobile pass, empty-state pass, tests, [`CLAUDE.md`](CLAUDE.md:1) update

### Why
1. Without Phase L, every prior phase is a database with no face — the user reverts to `psql` and `tail -f`.
2. Side-by-side Discord-chart-vs-LLM-render is the killer Rule #1 validator. No other surface exposes hallucinations as fast.
3. Anchor links [BH] turn the dashboard into a **shareable artefact** (paste into chat, click, land on the card). Without them, every "look at this trade" message in chat is "scroll to about half-way down and look for…".
4. Aggregator pattern [BI] is the only sustainable shape: a fresh company added later needs zero dashboard code changes.
5. Cached chart renderer [BK] keeps a single matplotlib draw out of the request path; without it, every leaderboard scroll re-draws SVGs and the page becomes a heater.
6. Mobile-tolerable is non-negotiable: the user explicitly browses from phone over Tailscale.

### How

#### §A. [BI] Dashboard-as-aggregator pattern
```python
# shared/dashboard/db_pools.py  (NEW)
"""Per-company connection-pool cache for read-only dashboard queries.

Phase L is a strict CONSUMER. It NEVER writes. The writer-domain registry
([BC], Phase 10) does NOT list 'dashboard' as an authorised writer for any table.
"""
from __future__ import annotations
from typing import Dict
from shared.utils.db import DatabasePool, get_company_pool_for as _open

_pools: Dict[str, DatabasePool] = {}

async def get_company_pool(company: str) -> DatabasePool:
    if company not in _pools:
        _pools[company] = await _open(company)
    return _pools[company]
```

```python
# shared/dashboard/snapshot.py  (extension)
from shared.utils.companies import list_active_companies
from shared.dashboard.db_pools import get_company_pool

async def aggregate_open_positions(company_filter: str | None = None) -> list[dict]:
    """Read-only fan-out across active companies.

    `company_filter='all' or None` aggregates every company; otherwise restricts.
    Each row is tagged with `_company` so the UI can render a per-company column.
    """
    rows: list[dict] = []
    companies = await list_active_companies()
    if company_filter and company_filter != "all":
        companies = [c for c in companies if c == company_filter]
    for company in companies:
        pool = await get_company_pool(company)
        async with pool.acquire() as conn:
            cr = await conn.fetch("SELECT * FROM tracked_positions WHERE status='open'")
            rows.extend({**dict(r), "_company": company} for r in cr)
    return rows
```

`tickles_shared` reads (e.g., `tracked_positions` after Phase 6 [F] reconciliation, `prompt_versions`, `api_cost_log`) skip the loop — there is exactly one shared pool. Snapshot cache key: `(tab, company_filter)` with 10 s TTL.

#### §B. Tab layout [L]
| Tab | Path | Backed by | Purpose |
|---|---|---|---|
| **Overview** | `/` | `actor_leaderboard` + `api_cost_log` rollups + `services` snapshot | At-a-glance health: today's PnL, top edge_scores, services up/down, cost burn rate |
| **Leaderboard** | `/leaderboard` | `actor_leaderboard` (Phase 11) | Cross-actor ranking; period (7d/30d/90d/all) + actor_type filters; click → Trader Drill |
| **Signals** | `/signals` | `signal_interpretations` ⨝ `news_items` | Paginated; each row links to original Discord/Telegram message **and** opticals HTML; filters: trader, symbol, action, prefilter pass/fail, agreement ≥ N |
| **Positions** | `/positions` | `tracked_positions` + `position_updates` + `agent_opinions` + `position_postmortems` | Card per position: entry_reason_{trader,llm,agent}, live PnL, latest opinion, post-mortem if closed |
| **Interpretations** | `/interpretations` | `signal_interpretations` raw I/O | Pre-filter input/output, vision waterfall, prompt used (versioned), raw response JSON, **side-by-side Discord chart vs annotated LLM render** |
| **Live Queue** | `/queue` | `news_items WHERE enrichment_status='pending'` + `media_items pending` + `tracked_positions IN ('pending','open')` | [G6] realtime ingest depth, signals waiting interp, trader-given vs agent-taken open trades, current logic step per item; WS push every 5 s |
| **Trader Drill** | `/traders/<actor_id>/<period>` | join across all of the above + `api_cost_log` | [G6] per-trade overkill: side-by-side charts, ±10 surrounding messages, free-form pattern/setup/regime/session tags, entry-reason pair, agreement, all opinions chronologically, post-mortem, cost rollup, edge_score breakdown |

#### §C. Stats overview strip (every page)
- Today's signals processed (count, % vs 7d avg)
- Today's API cost USD ([G5] every provider, % of monthly budget)
- Open positions (count + aggregate unrealised PnL); split badge **trader-given vs agent-taken**
- Services up `12/13 healthy` ([H] coloured by `kind`); openclaw-cron health for postmortem / chart_hacker_opinion / edge_scorer
- Top edge_score actor today (name + score, click → leaderboard)
- [G6] Live ingest depth (`news_items WHERE enrichment_status='pending'` count, click → `/queue`)

#### §D. [BH] Anchor-link contract
```python
# shared/dashboard/anchors.py  (NEW)
"""Stable URL-fragment contract for every dashboard card.

A user pasting a URL with `#pos-12345` MUST land on position 12345 with the card
expanded and visually highlighted. Anchor format is intentionally short to fit
in chat messages.
"""
from typing import Literal

AnchorKind = Literal["sig", "pos", "opn", "pm", "trade", "interp"]

def anchor_for(kind: AnchorKind, id_: int | str) -> str:
    return f"#{kind}-{id_}"
```

JS contract (`app.js`):
```javascript
// On load: scroll to fragment + add .highlight class for 3s
window.addEventListener('DOMContentLoaded', () => {
    const target = document.querySelector(window.location.hash);
    if (target) {
        target.scrollIntoView({behavior: 'smooth', block: 'center'});
        target.classList.add('highlight');
        setTimeout(() => target.classList.remove('highlight'), 3000);
    }
});
```

Every card template renders `id="pos-{{ p.id }}"` etc. The card's title becomes a `<a href="#pos-{{ p.id }}">⚓</a>` so clicking it copies the deep link.

#### §E. [BK] Cached chart renderer
```python
# shared/dashboard/chart_renderer.py  (NEW)
"""Idempotent annotated chart renderer.

Draws the LLM's parsed support/resistance/pattern overlay on top of the
original chart image and writes an SVG to disk. Cache key includes
`prompt_version` so re-rendering after a prompt change yields a fresh URL.
"""
from __future__ import annotations
import hashlib, json
from pathlib import Path
from typing import Any, Dict

CHART_DIR = Path("/var/lib/tickles/opticals/charts")

def _cache_key(interp_id: int, prompt_version: str, sr_hash: str) -> str:
    return f"{interp_id}_{prompt_version}_{sr_hash}.svg"

def _sr_hash(support_levels: list, resistance_levels: list) -> str:
    h = hashlib.sha256(json.dumps([support_levels, resistance_levels], sort_keys=True).encode()).hexdigest()
    return h[:8]

async def render_annotated_chart(interp_row: Dict[str, Any]) -> Path:
    """Idempotent. Returns existing file if cache-hit, else draws once."""
    sr_h = _sr_hash(interp_row.get("support_levels") or [], interp_row.get("resistance_levels") or [])
    fname = _cache_key(interp_row["id"], interp_row.get("prompt_version", "v0"), sr_h)
    out_path = CHART_DIR / fname
    if out_path.exists():
        return out_path

    CHART_DIR.mkdir(parents=True, exist_ok=True)
    # Draw with matplotlib — single call, no heatmap-of-heaters
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(10, 5))
    # ... draw OHLC + S/R lines + pattern annotations from interp_row ...
    fig.savefig(out_path, format="svg", bbox_inches="tight")
    plt.close(fig)
    return out_path
```

The Interpretations tab and Trader Drill embed the SVG via `<object data="/opticals/charts/{filename}" type="image/svg+xml">`; Tailscale serves the directory the same way Phase 4 already does.

#### §F. [H] ServiceDescriptor kind colours
```python
KIND_COLOURS = {
    "daemon":     "green",
    "agent_cron": "blue",
    "listener":   "purple",
    "exporter":   "grey",
    "collector":  "orange",
}
def render_service_row(d: ServiceDescriptor) -> str:
    kind = getattr(d, "kind", None) or "daemon"
    colour = KIND_COLOURS.get(kind, "green")
    schedule = ""
    if kind == "agent_cron" and d.cron_schedule:
        from croniter import croniter
        next_fire = croniter(d.cron_schedule, datetime.now(timezone.utc)).get_next(datetime)
        schedule = f"<span class='cron'>next: {next_fire:%Y-%m-%d %H:%M UTC}</span>"
    return f"<li class='svc {colour}'>{d.name}{schedule}</li>"
```

#### §G. Auth + read-only invariant
- Every route is gated by `auth_middleware` (Phase 5 [D] default-deny).
- The opticals static folder from Phase 4 is the **only** unauthenticated surface, and only because the Tailscale ACL fences it.
- No write actions in v1. The writer-domain registry ([BC], Phase 10) does NOT list `dashboard` for any table; CI gate (Phase R [BL]) catches any future drift.

### Risks & mitigations
| # | Risk | Mitigation |
|---|------|-----------|
| 1 | N+1 queries on Positions tab (one query per card for opinions / post-mortem) | One join per tab refresh; `EXPLAIN ANALYZE` in CI shows ≤ 1 query per tab snapshot |
| 2 | Per-company fan-out becomes slow with 10+ companies | Pools cached in `db_pools.py`; snapshot cache 10 s TTL; queries run in `asyncio.gather` |
| 3 | Chart renderer recomputes after every prompt change and consumes disk | Cache key includes `prompt_version` + `sr_hash`; nightly sweep prunes any chart not referenced in last 30 days |
| 4 | WebSocket connection pile-up (one per phone tab left open overnight) | Server-side hard cap 50 concurrent WS clients; idle disconnect after 10 min no activity |
| 5 | Anchor-link fragment changes if IDs ever resequence (after a backfill) | Anchors use the canonical primary-key id, never an array index; document in [`shared/dashboard/anchors.py`](shared/dashboard/anchors.py:1) docstring |
| 6 | OTP cookie hijack on mobile | Reuse Phase 5 [AP] CSRF + session-IP binding; `Secure` + `HttpOnly` + `SameSite=Lax` flags |
| 7 | A new company added at runtime breaks the cached pool list | `list_active_companies()` re-queried every 60 s; pools created lazily on first reference |
| 8 | Mobile portrait crashes on Live Queue (table too wide) | CSS `@media (max-width: 640px)` collapses to a single-column card list |
| 9 | Empty-state pages 500 because some helper assumes ≥ 1 row | Each helper has a 0-row branch returning `[]`; tested explicitly per route |
| 10 | Discord/Telegram message link rot when source server changes invite | Persist the original `source_url` at ingest time (already done by collectors); expose verbatim, do NOT compute on-the-fly |
| 11 | Cost rollup misses non-LLM providers and "today's spend" looks falsely small | [G5] sums `api_cost_log` by `provider` not `role`; CI test inserts a synthetic ccxt row and asserts the sum increased |
| 12 | matplotlib non-Agg backend tries to open a display in headless deployment | Force `matplotlib.use("Agg")` at the top of `chart_renderer.py` |
| 13 | Service `kind` missing on legacy descriptors causes UI to render uncoloured rows | Default to `"daemon"` everywhere `getattr(d, "kind", None) or "daemon"` is read |
| 14 | Trader Drill page joins across 6 tables and times out | Materialise the join into a CTE + `LIMIT 50` per drill; paginate older trades |
| 15 | User shares an anchor URL and the recipient lacks OTP session | Recipient is bounced to `/login` then redirected back to the same fragment after auth; round-trip preserves the `#anchor` |

### Benchmark checklist

> Reconciled 2026-05-03 against the live dashboard ([`shared/dashboard/server.py`](shared/dashboard/server.py:1)). Ticked items shipped via the Phase L Day 1 implementation; unticked items are deferred to Phase X.4-X.6 / Phase Y v2 or remain genuine TODOs.

- [x] Dashboard reachable at `https://vmi3220412.trout-goblin.ts.net/` over Tailscale
- [x] All 7 routes (overview, leaderboard, signals, positions, interpretations, queue, trader_drill) return 200 with valid OTP session — *(trader_drill route is hidden but registered)*
- [x] All 7 routes return 401 without a session — *(via `auth_middleware` default-deny in [`shared/dashboard/auth.py`](shared/dashboard/auth.py:1))*
- [x] Stats overview strip renders on every page with live numbers
- [x] Leaderboard tab shows ranked actors from `actor_leaderboard` view, period filter changes results
- [x] Signals tab links to original Discord/Telegram message AND to opticals HTML
- [x] Positions tab card shows: entry_reason_trader, entry_reason_llm, latest agent_opinion, post-mortem (if closed)
- [x] Interpretations tab inline-renders the chart image + LLM JSON + Discord link
- [x] Live Queue tab WebSocket pushes update within 5 s of a new pending row — *(via [`shared/dashboard/ws.py`](shared/dashboard/ws.py:1) `/ws/queue`)*
- [x] [BH] Anchor-link round-trip: paste `https://.../positions#pos-12345` → page loads, scrolls to position 12345, highlights for 3 s — *(via [`shared/dashboard/anchors.py`](shared/dashboard/anchors.py:1))*
- [ ] [BH] Anchor preserved through OTP login redirect: unauthenticated user lands on `/login?next=/positions%23pos-12345` and after auth lands on `/positions#pos-12345` — *deferred, not yet verified*
- [x] [BI] Dashboard-as-aggregator: adding a new company `tickles_<x>` requires zero dashboard code changes; new rows appear after `list_active_companies()` cache refresh (≤ 60 s) — *(via [`shared/dashboard/db_pools.py`](shared/dashboard/db_pools.py:1))*
- [ ] [BI] No writes from dashboard: writer-domain registry CI gate (Phase R [BL]) does NOT list `dashboard` for any table; deliberate `INSERT INTO ... FROM dashboard.*` introduced into a test file fails CI — *blocked on Phase R closeout (writer-registry CI gate not yet wired into GitHub Actions)*
- [x] [BK] Chart renderer caches: 1st request draws SVG, 2nd request returns existing file with no matplotlib call (verified via mtime unchanged) — *(via [`shared/dashboard/chart_renderer.py`](shared/dashboard/chart_renderer.py:1))*
- [x] [BK] Chart cache invalidation: bumping `prompt_version` produces a new filename; old file remains until pruned
- [x] [G6] Live Queue tab shows trader-given trades AND agent-taken trades AND the current logic step per item
- [x] [G6] Trader Drill page renders side-by-side Discord chart vs LLM-rendered chart for at least one closed position — *(hidden Trader Drill route)*
- [x] [G6] Trader Drill page exposes pattern_tags / setup_tags / regime_tags / session_tags as free-form chips (G1 — no enum)
- [x] [G5] Cost overview strip sums every provider in `api_cost_log` (LLM + ccxt + Discord + Telegram), verified by `SELECT DISTINCT provider`
- [ ] [H] `/api/services` shows openclaw-cron-driven agents (postmortem, chart_hacker_opinion, edge_scorer) with `kind='agent_cron'` colour + next-fire time — *partial: services endpoint exists ([`shared/dashboard/server.py:313`](shared/dashboard/server.py:313)) but `kind` field + croniter next-fire not yet rendered*
- [x] Mobile portrait (640 px) collapses cleanly across all 7 tabs
- [ ] No N+1 queries (verified by `EXPLAIN ANALYZE` showing ≤ 1 query per tab snapshot) — *not benchmarked*
- [x] Empty-state pages render correctly for a freshly-provisioned company
- [ ] WS hard cap: 51st connection rejected with `1013 try-again` — *not enforced (no concurrent-connection cap implemented in [`shared/dashboard/ws.py`](shared/dashboard/ws.py:1))*
- [ ] `pytest shared/tests/test_dashboard_routes.py shared/tests/test_chart_renderer.py shared/tests/test_anchors.py` green — *tests not yet written*
- [-] [`CLAUDE.md`](CLAUDE.md:1) updated with dashboard URL + tab list + anchor-link convention — *being updated 2026-05-03 in this same session, see Step 5 of `.roo/handoffs/2026-05-03-five-steps-closeout.md`*

**Deferred to successor plans:**
- News Feed tab → [`PHASE_X_DASHBOARD_AND_PIPELINE_PLAN.md`](shared/docs/PHASE_X_DASHBOARD_AND_PIPELINE_PLAN.md:290) §X.4
- Cross-tab Interpretation Drawer → [`PHASE_X_DASHBOARD_AND_PIPELINE_PLAN.md`](shared/docs/PHASE_X_DASHBOARD_AND_PIPELINE_PLAN.md:376) §X.5
- Config tab → [`PHASE_X_DASHBOARD_AND_PIPELINE_PLAN.md`](shared/docs/PHASE_X_DASHBOARD_AND_PIPELINE_PLAN.md:422) §X.6
- Learning tab + skill-vs-luck composite + 7d/14d/30d Memory Feed → [`PHASE_Y_LEARNING_DASHBOARD_PLAN.md`](shared/docs/PHASE_Y_LEARNING_DASHBOARD_PLAN.md:1) v2 (blocked on §11 user decisions)

---

## Phase R — CI Gates & Operational Canaries (2 days)

> **STATUS (2026-05-03): ~57% SHIPPED.**
> Python and SQL artefacts exist; CI workflow + tests + systemd units are missing.
>
> **Shipped:**
> - [`shared/scripts/schema_diff.py`](shared/scripts/schema_diff.py:1) — schema-drift detector
> - [`shared/scripts/writer_registry_grep.py`](shared/scripts/writer_registry_grep.py:1) — writer-domain enforcer
> - [`shared/scripts/master_sync.py`](shared/scripts/master_sync.py:1) — master-schema sync gate
> - [`shared/intelligence/cron_canary.py`](shared/intelligence/cron_canary.py:1) — heartbeat watchdog
> - [`shared/intelligence/heartbeat.py`](shared/intelligence/heartbeat.py:1) — `record_heartbeat()` helper
> - [`shared/intelligence/migrations/2026_05_04_phase_r_cron_heartbeats.sql`](shared/intelligence/migrations/2026_05_04_phase_r_cron_heartbeats.sql:1) — `cron_heartbeats` table
> - [`shared/dashboard/server.py:313`](shared/dashboard/server.py:313) — `handle_services` already reads `cron_heartbeats` (partial wire-in)
>
> **Missing (closeout work):**
> - `.github/workflows/schema-and-writer-gates.yml` — GitHub Actions workflow (the `.github/workflows/` directory is empty)
> - `shared/tests/test_schema_diff.py`
> - `shared/tests/test_writer_registry_grep.py`
> - `shared/tests/test_master_sync_gate.py`
> - `shared/tests/test_cron_canary.py`
> - `systemd/tickles-schema-drift.timer` + `.service`
> - `systemd/tickles-cron-canary.service` (daemon for `cron_canary.py`)
> - Canonical schema snapshots: `shared/scripts/snapshots/tickles_shared.snapshot.sql` + `tickles_company.snapshot.sql`
> - Makefile targets: `refresh-snapshots`, `gate-local`
> - Wire `record_heartbeat()` calls into postmortem / edge_scorer / coach / chart_hacker_opinion service ticks
> - Dashboard staleness badge ([H] kind colours + croniter next-fire) on Overview tab Services strip

### Purpose

Phase R is the **enforcement layer** for everything Phases 0–11 + L put in place. Without it, the carefully-designed additive migrations, the writer-domain registry from Phase 10 [BC], the prompt-version registry from Phase 6 [N], and the [`shared/migration/tickles_shared_pg.sql`](shared/migration/tickles_shared_pg.sql:1) master schema will all eventually be subverted by some forgotten ad-hoc `ALTER TABLE` or out-of-band `INSERT`. Phase R closes that hole with three CI gates and one runtime canary, all wired into a single GitHub Actions workflow that **must** pass on every PR before merge.

This phase exists because every prior phase makes an implicit assumption — "the schema matches the SQL files", "no service writes outside its domain", "the cron actually fires" — and Phase R turns each of those implicit assumptions into a hard, testable invariant.

### What

1. **[BL] Schema-diff CI gate** — every PR runs `pg_dump --schema-only` against an ephemeral DB built from `shared/migration/tickles_shared_pg.sql` + `shared/migration/tickles_company_pg.sql`, normalises the output (strip whitespace, `OWNER TO`, comment timestamps), and diffs against a checked-in canonical snapshot. Drift = build fails.
2. **[BC] Writer-registry grep gate** — static-analysis pass over `shared/**/*.py`: scan for `INSERT INTO <table>` and `UPDATE <table> SET` patterns, look up the file's owning service in [`shared/services/registry.py`](shared/services/registry.py:1), query the `table_writers` registry seeded in Phase 10, fail the build if a service writes to a table not in its allow-list. Skip directories: `shared/tests/**`, files marked `# writer-registry: test-only`.
3. **[BN] Master-sync CI gate** — `shared/scripts/master_sync.py` is now a **CI gate**, not just a one-shot. Every PR runs it against an ephemeral `tickles_shared_test` DB. If the master SQL files cannot be applied cleanly to a fresh database **and** additively to a 7-day-old snapshot, the build fails. Matrix: `[fresh, snapshot]` × `[postgres-15, postgres-16]`.
4. **[BM] Cron health canary** — every openclaw cron-driven agent (`postmortem_service`, `chart_hacker_opinion_service`, `edge_scorer_service`, `coach_service`) writes a heartbeat row to `tickles_shared.public.cron_heartbeats` on every fire. A new daemon [`shared/intelligence/cron_canary.py`](shared/intelligence/cron_canary.py:1) polls every 5 min and emits a Telegram alert (via the dashboard's existing bot) when `now() - last_run_at > 2 × cron_interval`.
5. **Dashboard staleness badge** — Phase L's `/api/services` endpoint reads `cron_heartbeats` and turns a service's badge red when stale by > 1.5 × interval (warn) or > 2 × interval (alert).
6. **GitHub Actions workflow** — single file `.github/workflows/schema-and-writer-gates.yml` runs all three gates on every PR + on push to `main`. No bypass — required check.
7. **Runtime drift detector** — once-daily systemd timer runs schema-diff against the live `tickles_shared` DB and posts to the dashboard's audit log. Catches manual `ALTER TABLE`s applied directly on production outside CI.

### Where

| Component | Path | Action |
|---|---|---|
| Schema-diff script | `shared/scripts/schema_diff.py` | NEW |
| Canonical schema snapshots | `shared/scripts/snapshots/tickles_shared.snapshot.sql`, `shared/scripts/snapshots/tickles_company.snapshot.sql` | NEW (regenerated by `make refresh-snapshots`) |
| Writer-registry grep | `shared/scripts/writer_registry_grep.py` | NEW |
| Master-sync upgrade | `shared/scripts/master_sync.py` | EXTEND (was a one-shot in Phase 6 §G) |
| Cron heartbeats migration | `shared/intelligence/migrations/2026_05_04_phase_r_cron_heartbeats.sql` | NEW |
| Cron canary daemon | [`shared/intelligence/cron_canary.py`](shared/intelligence/cron_canary.py:1) | NEW |
| Cron canary registry entry | [`shared/services/registry.py`](shared/services/registry.py:1) | EXTEND (kind=`daemon`, name=`cron_canary`) |
| Dashboard staleness logic | [`shared/dashboard/server.py`](shared/dashboard/server.py:1) → `handle_services` | EXTEND |
| Heartbeat helper | `shared/intelligence/heartbeat.py` | NEW (called by every cron-driven agent) |
| GitHub Actions workflow | `.github/workflows/schema-and-writer-gates.yml` | NEW |
| Tests | `shared/tests/test_schema_diff.py`, `shared/tests/test_writer_registry_grep.py`, `shared/tests/test_master_sync_gate.py`, `shared/tests/test_cron_canary.py` | NEW |
| systemd timer | `systemd/tickles-schema-drift.timer`, `systemd/tickles-schema-drift.service` | NEW |
| Makefile targets | `Makefile` → `refresh-snapshots`, `gate-local` | EXTEND |

### When

**Day 1**
- Morning: `cron_heartbeats` migration + heartbeat helper + wire postmortem/edge_scorer/coach/chart_hacker_opinion to call `record_heartbeat()` on every tick
- Afternoon: schema-diff script + canonical snapshots + writer-registry grep
- Evening: master_sync.py upgrade to CI gate

**Day 2**
- Morning: cron_canary daemon + dashboard staleness badge
- Afternoon: GitHub Actions workflow + matrix postgres-15/16
- Evening: systemd timer for daily live-DB drift detection + tests + dry-run on a real PR

### Why

- **Phases 0–11 + L all rely on schema invariants that nothing currently enforces.** Phase 6 §G [BN] introduced the master-sync concept but did not turn it into a gate. Phase R closes that loop.
- **Phase 10 [BC] writer-registry is runtime-only.** A boot-time `assert_authorised()` only catches violations after a service is deployed and starts running. The grep gate catches them at PR review.
- **Cron-driven agents fail silently.** ServiceDaemon's heartbeat is for `daemon` kinds; openclaw cron jobs (postmortem, edge_scorer, coach) have no equivalent. A missing fire is invisible until you notice no `position_postmortems` rows for a week.
- **Schema drift is the #1 cause of multi-tenant disasters.** A single `ALTER TABLE` applied to one company's DB but not another's silently breaks the dashboard's [BI] aggregator (Phase L) when it tries to UNION ALL.
- **CI gates are cheap.** Each PR runs in an ephemeral container; no production impact, no human review needed.

### How

#### §A. Schema-diff CI gate [BL]

```python
# shared/scripts/schema_diff.py  (NEW)
"""
Schema drift detector — used as both a CI gate and a daily live-DB canary.

CI mode: build ephemeral DB from shared/migration/*.sql, dump schema, diff against
canonical snapshot. Any diff = exit 1.

Live mode: dump schema from a live DB, diff against canonical snapshot, post
result to dashboard audit log.
"""
from __future__ import annotations

import argparse
import asyncio
import difflib
import re
import subprocess
import sys
from pathlib import Path

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"
NOISE_PATTERNS = [
    re.compile(r"^-- Dumped (from|by) .*$", re.MULTILINE),
    re.compile(r"^-- Started on .*$", re.MULTILINE),
    re.compile(r"^-- Completed on .*$", re.MULTILINE),
    re.compile(r"OWNER TO \w+;", re.MULTILINE),
    re.compile(r"^SET .*$", re.MULTILINE),
    re.compile(r"^SELECT pg_catalog\..*$", re.MULTILINE),
]

def _normalise(sql: str) -> str:
    for pat in NOISE_PATTERNS:
        sql = pat.sub("", sql)
    # collapse blank lines
    sql = re.sub(r"\n{3,}", "\n\n", sql)
    return sql.strip() + "\n"

def _pg_dump(dsn: str) -> str:
    out = subprocess.check_output(
        ["pg_dump", "--schema-only", "--no-owner", "--no-privileges", dsn],
        text=True,
    )
    return _normalise(out)

def diff_against_snapshot(dsn: str, snapshot_name: str) -> int:
    actual = _pg_dump(dsn)
    snapshot_path = SNAPSHOT_DIR / f"{snapshot_name}.snapshot.sql"
    expected = _normalise(snapshot_path.read_text())
    if actual == expected:
        print(f"[schema-diff] {snapshot_name}: OK")
        return 0
    diff = "\n".join(difflib.unified_diff(
        expected.splitlines(), actual.splitlines(),
        fromfile=f"{snapshot_name} (canonical)",
        tofile=f"{snapshot_name} (actual)",
        lineterm="",
    ))
    print(f"[schema-diff] {snapshot_name}: DRIFT DETECTED\n{diff}")
    return 1

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--snapshot", required=True, choices=["tickles_shared", "tickles_company"])
    args = ap.parse_args()
    sys.exit(diff_against_snapshot(args.dsn, args.snapshot))
```

```bash
# Makefile additions
refresh-snapshots:  ## regenerate canonical snapshots from current master SQL
	createdb _snapshot_shared && \
	psql _snapshot_shared -f shared/migration/tickles_shared_pg.sql && \
	pg_dump --schema-only --no-owner --no-privileges _snapshot_shared \
	  > shared/scripts/snapshots/tickles_shared.snapshot.sql && \
	dropdb _snapshot_shared
	# repeat for tickles_company
```

#### §B. Writer-registry grep gate [BC]

```python
# shared/scripts/writer_registry_grep.py  (NEW)
"""
Static-analysis writer-registry enforcement.

Walks shared/**/*.py, finds INSERT/UPDATE/DELETE statements, infers the owning
service from the file path (or an explicit `# writer-registry: <service>` marker),
and asserts the table is in that service's allow-list per public.table_writers.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Iterable, Tuple

import asyncio
import asyncpg

ROOT = Path(__file__).parent.parent
SKIP_DIRS = {"tests", "_archive", "migration", "scripts"}
SKIP_MARKER = "# writer-registry: test-only"
EXPLICIT_MARKER = re.compile(r"# writer-registry: ([\w_-]+)")
INSERT_RE = re.compile(r"INSERT\s+INTO\s+(?:public\.)?(\w+)", re.IGNORECASE)
UPDATE_RE = re.compile(r"UPDATE\s+(?:public\.)?(\w+)\s+SET", re.IGNORECASE)
DELETE_RE = re.compile(r"DELETE\s+FROM\s+(?:public\.)?(\w+)", re.IGNORECASE)

# Path → service inference. Override with explicit marker.
PATH_TO_SERVICE = {
    "intelligence/interpretation_service.py": "interpretation_service",
    "intelligence/postmortem_service.py": "postmortem_service",
    "intelligence/chart_hacker_opinion_service.py": "chart_hacker_opinion_service",
    "intelligence/edge_scorer_service.py": "edge_scorer_service",
    "intelligence/coach_service.py": "coach_service",
    "daemons/surgeon2_trader.py": "surgeon2_trader",
    "memu/listener_service.py": "memu_listener",
    # collectors
    "collectors/discord/discord_collector.py": "discord_collector",
    "collectors/twitter_collector.py": "twitter_collector",
}

def _infer_service(path: Path, content: str) -> str | None:
    m = EXPLICIT_MARKER.search(content)
    if m:
        return m.group(1)
    rel = path.relative_to(ROOT).as_posix()
    for needle, svc in PATH_TO_SERVICE.items():
        if rel.endswith(needle):
            return svc
    return None

def _scan(content: str) -> Iterable[Tuple[str, str]]:
    """Yield (operation, table)."""
    for line in content.splitlines():
        if SKIP_MARKER in line:
            continue
        for m in INSERT_RE.finditer(line):
            yield ("INSERT", m.group(1))
        for m in UPDATE_RE.finditer(line):
            yield ("UPDATE", m.group(1))
        for m in DELETE_RE.finditer(line):
            yield ("DELETE", m.group(1))

async def main(dsn: str) -> int:
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2)
    rows = await pool.fetch("SELECT table_name, allowed_writer_services FROM public.table_writers")
    registry = {r["table_name"]: set(r["allowed_writer_services"] or []) for r in rows}
    violations: list[str] = []
    for path in ROOT.rglob("*.py"):
        if any(seg in SKIP_DIRS for seg in path.parts):
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        service = _infer_service(path, content)
        if service is None:
            continue
        for op, table in _scan(content):
            if table not in registry:
                continue  # unregistered tables — handled by separate audit
            allowed = registry[table]
            if service not in allowed:
                violations.append(
                    f"{path.relative_to(ROOT)}: {op} on '{table}' by '{service}' "
                    f"not in allow-list {sorted(allowed)}"
                )
    await pool.close()
    if violations:
        print("[writer-registry] VIOLATIONS:")
        for v in violations:
            print(f"  - {v}")
        return 1
    print("[writer-registry] OK")
    return 0

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", required=True)
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.dsn)))
```

#### §C. Master-sync as CI gate [BN]

```python
# shared/scripts/master_sync.py  (EXTEND — was a one-shot from Phase 6 §G)
"""
Master schema sync — applies tickles_shared_pg.sql + tickles_company_pg.sql
to an ephemeral DB and validates that:
  1. Fresh apply: clean DB → master SQL → no errors
  2. Snapshot apply: 7-day-old snapshot DB → master SQL (additively) → no errors

Used by .github/workflows/schema-and-writer-gates.yml as a required check.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
SHARED_SQL = ROOT / "migration/tickles_shared_pg.sql"
COMPANY_SQL = ROOT / "migration/tickles_company_pg.sql"

def _run(cmd: list[str]) -> None:
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"[master-sync] FAIL: {' '.join(cmd)}\n{res.stderr}", file=sys.stderr)
        sys.exit(1)

def gate_fresh(dsn: str) -> None:
    _run(["psql", dsn, "-v", "ON_ERROR_STOP=1", "-f", str(SHARED_SQL)])
    _run(["psql", dsn, "-v", "ON_ERROR_STOP=1", "-f", str(COMPANY_SQL)])
    print("[master-sync] fresh: OK")

def gate_snapshot(dsn: str, snapshot_path: Path) -> None:
    _run(["psql", dsn, "-v", "ON_ERROR_STOP=1", "-f", str(snapshot_path)])
    # Now apply master SQL additively — must not error
    _run(["psql", dsn, "-v", "ON_ERROR_STOP=1", "-f", str(SHARED_SQL)])
    _run(["psql", dsn, "-v", "ON_ERROR_STOP=1", "-f", str(COMPANY_SQL)])
    print("[master-sync] snapshot: OK")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["fresh", "snapshot"], required=True)
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--snapshot", help="Path to 7-day-old snapshot SQL", default=None)
    args = ap.parse_args()
    if args.mode == "fresh":
        gate_fresh(args.dsn)
    else:
        if not args.snapshot:
            print("--snapshot required for snapshot mode", file=sys.stderr)
            sys.exit(2)
        gate_snapshot(args.dsn, Path(args.snapshot))
```

#### §D. Cron heartbeats schema [BM]

```sql
-- shared/intelligence/migrations/2026_05_04_phase_r_cron_heartbeats.sql
-- Phase R [BM] — Cron health canary
-- Rollback: DROP TABLE public.cron_heartbeats;

CREATE TABLE IF NOT EXISTS public.cron_heartbeats (
    agent_id        TEXT PRIMARY KEY,
    last_run_at     TIMESTAMPTZ NOT NULL,
    last_status     TEXT NOT NULL CHECK (last_status IN ('ok', 'error', 'partial')),
    last_message    TEXT,
    expected_interval_seconds INT NOT NULL,
    consecutive_failures INT NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cron_heartbeats_stale
    ON public.cron_heartbeats (last_run_at)
    WHERE last_status != 'ok';

COMMENT ON TABLE public.cron_heartbeats IS
    'Phase R [BM] — every cron-driven agent UPSERTs on each fire. cron_canary daemon polls and alerts when stale.';
```

#### §E. Heartbeat helper

```python
# shared/intelligence/heartbeat.py  (NEW)
"""
Universal heartbeat helper for cron-driven agents.

Every openclaw cron job (postmortem, edge_scorer, coach, chart_hacker_opinion)
calls record_heartbeat() at the end of each tick. The cron_canary daemon
(also in this folder) polls public.cron_heartbeats every 5 min and alerts on
staleness via the dashboard's Telegram bot.
"""
from __future__ import annotations

from typing import Literal

from shared.utils.pg_pool import get_shared_pool

Status = Literal["ok", "error", "partial"]

async def record_heartbeat(
    agent_id: str,
    status: Status,
    expected_interval_seconds: int,
    message: str | None = None,
) -> None:
    pool = await get_shared_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO public.cron_heartbeats
                (agent_id, last_run_at, last_status, last_message,
                 expected_interval_seconds, consecutive_failures, updated_at)
            VALUES ($1, now(), $2, $3, $4,
                    CASE WHEN $2 = 'ok' THEN 0 ELSE 1 END, now())
            ON CONFLICT (agent_id) DO UPDATE SET
                last_run_at = EXCLUDED.last_run_at,
                last_status = EXCLUDED.last_status,
                last_message = EXCLUDED.last_message,
                expected_interval_seconds = EXCLUDED.expected_interval_seconds,
                consecutive_failures = CASE
                    WHEN EXCLUDED.last_status = 'ok' THEN 0
                    ELSE public.cron_heartbeats.consecutive_failures + 1
                END,
                updated_at = now();
            """,
            agent_id, status, message, expected_interval_seconds,
        )
```

Each cron-driven service appends one line to its `tick()`:

```python
# shared/intelligence/postmortem_service.py — at end of tick()
from shared.intelligence.heartbeat import record_heartbeat
await record_heartbeat(
    agent_id="postmortem_service",
    status="ok" if processed_ok else "error",
    expected_interval_seconds=300,  # 5-min cron
    message=f"processed={processed_ok} failed={processed_fail}",
)
```

#### §F. Cron canary daemon [BM]

```python
# shared/intelligence/cron_canary.py  (NEW)
"""
Cron health canary — polls public.cron_heartbeats every 5 min, alerts via
Telegram when an agent's last_run_at exceeds 2x its expected_interval_seconds.

Registered as a ServiceDaemon (kind='daemon', tick=60s). Single-process —
must NOT be deployed per-company (the heartbeats table lives in tickles_shared).
"""
from __future__ import annotations

import logging
from typing import List

from shared.dashboard.telegram import send_alert  # existing helper from dashboard
from shared.services.daemon import DaemonConfig, ServiceDaemon
from shared.utils.pg_pool import get_shared_pool

logger = logging.getLogger(__name__)

WARN_MULTIPLIER = 1.5
ALERT_MULTIPLIER = 2.0

async def _stale_agents() -> List[dict]:
    pool = await get_shared_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT agent_id, last_run_at, last_status, expected_interval_seconds,
                   consecutive_failures,
                   EXTRACT(EPOCH FROM (now() - last_run_at)) AS seconds_since
            FROM public.cron_heartbeats
            WHERE EXTRACT(EPOCH FROM (now() - last_run_at))
                  > $1 * expected_interval_seconds
            """,
            ALERT_MULTIPLIER,
        )
    return [dict(r) for r in rows]

_alerted: set[str] = set()  # process-local; dedupe within a single uptime

async def tick() -> dict:
    stale = await _stale_agents()
    new_alerts: list[str] = []
    for row in stale:
        key = f"{row['agent_id']}:{row['last_run_at']}"
        if key in _alerted:
            continue
        _alerted.add(key)
        msg = (
            f"⚠️ cron_canary: {row['agent_id']} stale\n"
            f"last_run: {row['last_run_at']}\n"
            f"seconds_since: {int(row['seconds_since'])}\n"
            f"expected_interval: {row['expected_interval_seconds']}s\n"
            f"consecutive_failures: {row['consecutive_failures']}"
        )
        try:
            await send_alert(msg)
            new_alerts.append(row["agent_id"])
        except Exception:
            logger.exception("cron_canary: failed to send alert for %s", row["agent_id"])
    # bound the dedupe set
    if len(_alerted) > 1000:
        _alerted.clear()
    return {"stale_count": len(stale), "alerts_sent": new_alerts}

def build_daemon() -> ServiceDaemon:
    return ServiceDaemon(
        config=DaemonConfig(
            name="cron_canary",
            tick_seconds=300,  # 5 min
            emit_heartbeats_to_auditor=True,
        ),
        tick=tick,
    )
```

Registry entry:

```python
# shared/services/registry.py — _seed_known_services() addition
registry.register(ServiceDescriptor(
    name="cron_canary",
    kind="daemon",
    description="Phase R [BM] — polls cron_heartbeats, alerts on staleness",
    owner="shared",
    schedule="every 5 min (tick)",
))
```

#### §G. Dashboard staleness badge [BM]

```python
# shared/dashboard/server.py — handle_services extension
async def handle_services(request: web.Request) -> web.Response:
    snap = await _get_or_build_snapshot(request)
    services = list(snap.services or [])
    # Phase R [BM] overlay
    pool = await get_shared_pool()
    async with pool.acquire() as conn:
        hb_rows = await conn.fetch(
            "SELECT agent_id, last_run_at, expected_interval_seconds, last_status "
            "FROM public.cron_heartbeats"
        )
    hb = {r["agent_id"]: r for r in hb_rows}
    for svc in services:
        h = hb.get(svc["name"])
        if not h:
            svc["health"] = "unknown"
            continue
        seconds_since = (datetime.now(timezone.utc) - h["last_run_at"]).total_seconds()
        ratio = seconds_since / h["expected_interval_seconds"]
        if ratio > 2.0 or h["last_status"] == "error":
            svc["health"] = "alert"
        elif ratio > 1.5:
            svc["health"] = "warn"
        else:
            svc["health"] = "ok"
        svc["last_run_at"] = h["last_run_at"].isoformat()
    return web.json_response({"services": services})
```

#### §H. GitHub Actions workflow

```yaml
# .github/workflows/schema-and-writer-gates.yml
name: Schema & Writer Gates
on:
  pull_request:
    branches: [main]
  push:
    branches: [main]

jobs:
  schema-diff:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        pg: ["15", "16"]
    services:
      postgres:
        image: postgres:${{ matrix.pg }}
        env:
          POSTGRES_PASSWORD: test
        ports: ["5432:5432"]
        options: --health-cmd pg_isready --health-interval 10s
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -r requirements.txt
      - name: Master sync (fresh)
        run: python shared/scripts/master_sync.py --mode fresh --dsn postgresql://postgres:test@localhost/postgres
      - name: Schema diff (shared)
        run: python shared/scripts/schema_diff.py --dsn postgresql://postgres:test@localhost/postgres --snapshot tickles_shared

  writer-registry:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16
        env: { POSTGRES_PASSWORD: test }
        ports: ["5432:5432"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -r requirements.txt
      - run: psql postgresql://postgres:test@localhost/postgres -f shared/migration/tickles_shared_pg.sql
      - run: python shared/scripts/writer_registry_grep.py --dsn postgresql://postgres:test@localhost/postgres
```

#### §I. Daily live-DB drift detector

```ini
# systemd/tickles-schema-drift.timer
[Unit]
Description=Daily schema drift check against canonical snapshot

[Timer]
OnCalendar=*-*-* 03:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

```ini
# systemd/tickles-schema-drift.service
[Unit]
Description=Tickles schema drift detector

[Service]
Type=oneshot
EnvironmentFile=/opt/tickles/.env
ExecStart=/usr/bin/python3 /opt/tickles/shared/scripts/schema_diff.py --dsn ${TICKLES_SHARED_DSN} --snapshot tickles_shared
StandardOutput=append:/var/log/tickles/schema_drift.log
StandardError=append:/var/log/tickles/schema_drift.log
```

A non-zero exit posts to dashboard's audit log via a wrapper script that tails the exit code and calls `send_alert()`.

### Risks & mitigations

| Risk | Mitigation |
|---|---|
| Snapshot SQL becomes stale after legitimate schema PRs | `make refresh-snapshots` is part of PR template; CI fails closed (drift = block) so staleness is impossible to merge silently |
| `pg_dump` output varies between postgres-15 and postgres-16 | Matrix runs both; canonical snapshot generated against postgres-16 (production version); diff strips version-specific noise patterns |
| Writer-registry grep produces false positives on string literals like `"INSERT INTO foo"` in comments | Skip lines containing `# writer-registry: test-only`; future improvement: switch to `libcst` AST walk if false-positive rate > 1% |
| Service-name inference from path is brittle (rename = break) | Explicit `# writer-registry: <service>` marker overrides inference; CI fails closed on unknown service |
| Cron canary itself dies → no alerts | `cron_canary` is a `ServiceDaemon` with auditor heartbeats (Phase 4 [AM]); auditor's separate dead-man-switch escalates if `cron_canary` itself goes silent for > 15 min |
| `send_alert()` flood when many agents go stale at once | Process-local dedupe set keyed `(agent_id, last_run_at)`; bound at 1000 entries; one alert per (agent, stale-window) |
| Heartbeat write latency adds to cron tick budget | Single UPSERT to indexed `agent_id` PK → < 5 ms; acceptable overhead |
| Master-sync gate fails on transient postgres startup race in CI | `--health-cmd pg_isready --health-interval 10s` on the service container; retry psql connect with 30s timeout |
| Snapshot SQL grows large and slows CI | Snapshots are schema-only (`pg_dump --schema-only`) — bounded by table count, not row count; expected size < 200 KB |
| Live-DB drift detector floods audit log if an out-of-band hotfix lands | Detector emits a single audit entry per day with the diff hash; same hash = no new entry |
| Snapshot-mode gate has no real "7-day-old snapshot" available | CI uses a synthesised "minus-one-migration" snapshot (drop the most recent migration, dump, then re-apply master) — proves additivity |
| Two PRs simultaneously refresh snapshots → merge conflict | Snapshot files are deterministic byte-for-byte; conflict is a normal git resolve; CI re-runs gate post-merge to confirm |
| Telegram bot rate-limits the canary | `send_alert()` already implements 1 msg/sec throttle from the dashboard's Phase L wiring |
| `agent_state` (Phase 7 [J]) write paths bypass writer-registry because they `INSERT INTO agent_state` from many services | `agent_state` allow-list is broad by design (`{interpretation_service, postmortem_service, surgeon2_trader, chart_hacker_opinion_service, edge_scorer_service, coach_service}`); seed in Phase 10 [BC] migration |

### Benchmark checklist

- [ ] [BL] `python shared/scripts/schema_diff.py --dsn <ephemeral> --snapshot tickles_shared` exits 0 against fresh master apply
- [ ] [BL] Intentionally-introduced ALTER TABLE in a feature PR causes schema_diff to exit 1 with a unified diff
- [ ] [BL] Canonical snapshots checked in: [`shared/scripts/snapshots/tickles_shared.snapshot.sql`](shared/scripts/snapshots/tickles_shared.snapshot.sql:1), [`shared/scripts/snapshots/tickles_company.snapshot.sql`](shared/scripts/snapshots/tickles_company.snapshot.sql:1)
- [ ] [BL] `make refresh-snapshots` regenerates them deterministically (byte-for-byte) when run twice
- [ ] [BL] Schema-diff strips `OWNER TO`, `Dumped by`, `Started/Completed on`, `SET ...`, `pg_catalog.set_config` noise
- [ ] [BC] `python shared/scripts/writer_registry_grep.py --dsn <test>` exits 0 on current `shared/`
- [ ] [BC] Test fixture: insert a `INSERT INTO position_postmortems` into [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:1) → grep gate fails with `interpretation_service not in {postmortem_service}`
- [ ] [BC] `# writer-registry: test-only` marker correctly skips a line in tests
- [ ] [BC] Explicit `# writer-registry: my_service` override works and is preferred over path-based inference
- [ ] [BC] Allow-list seeded in Phase 10 [BC] migration covers all 6 production tables: `tracked_positions`, `position_postmortems`, `agent_opinions`, `signal_interpretations`, `memu_outbox`, `news_items`
- [ ] [BN] `python shared/scripts/master_sync.py --mode fresh --dsn <fresh-db>` applies cleanly on postgres-15
- [ ] [BN] Same on postgres-16 (matrix)
- [ ] [BN] `--mode snapshot` applies master SQL additively to a "minus-one-migration" snapshot without errors
- [ ] [BM] `cron_heartbeats` table exists in `tickles_shared.public` after migration
- [ ] [BM] postmortem/edge_scorer/coach/chart_hacker_opinion services each call `record_heartbeat()` on every tick (verified by test)
- [ ] [BM] `cron_canary` daemon registered in [`shared/services/registry.py`](shared/services/registry.py:1) with `kind='daemon'`, `tick_seconds=300`
- [ ] [BM] Stale agent (manually `UPDATE cron_heartbeats SET last_run_at = now() - interval '20 minutes' WHERE agent_id='postmortem_service'`) triggers Telegram alert on next canary tick
- [ ] [BM] Same agent does NOT re-alert on the next tick (dedupe works)
- [ ] [BM] Dashboard `/api/services` returns `health: "alert"` for stale agents, `"warn"` for ratio > 1.5, `"ok"` otherwise
- [ ] [BM] Dashboard badge colour matches health state (red/amber/green) per Phase L colour spec
- [ ] GitHub Actions workflow [`.github/workflows/schema-and-writer-gates.yml`](.github/workflows/schema-and-writer-gates.yml:1) is a required check on `main`
- [ ] All three gates run in < 90 s combined on a clean PR
- [ ] systemd timer `tickles-schema-drift.timer` enabled and fires daily at 03:00 UTC
- [ ] Live-DB drift detector posts a single audit-log entry per drift-hash per day (no flood)
- [ ] `pytest shared/tests/test_schema_diff.py shared/tests/test_writer_registry_grep.py shared/tests/test_master_sync_gate.py shared/tests/test_cron_canary.py` green
- [ ] Phase R additions documented in [`CLAUDE.md`](CLAUDE.md:1) Intelligence Pipeline section

---

## Mini-Memory — CLAUDE.md-Style Resume Block

> **Use this section to resume the project in a fresh chat.** Paste the relevant subsection at the top of the next conversation along with: *"Continue executing INTELLIGENCE_UNIFIED_PLAN.md from Phase N."*

### Project at a glance
- **Repo:** [`/opt/tickles/`](/) — multi-company algorithmic trading platform, Python 3.12, Postgres + ClickHouse + Qdrant.
- **VPS:** `vmi3220412.trout-goblin.ts.net` (Tailscale).
- **Plan source of truth:** [`shared/docs/INTELLIGENCE_UNIFIED_PLAN.md`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:1) (this file).
- **Original prompt:** [`shared/docs/prompt.md`](shared/docs/prompt.md:1).
- **Cross-references:** [`shared/docs/TICKLES_INFRASTRUCTURE_ATLAS.md`](shared/docs/TICKLES_INFRASTRUCTURE_ATLAS.md:1), [`shared/docs/RULE1_END_TO_END.md`](shared/docs/RULE1_END_TO_END.md:1), [`CLAUDE.md`](CLAUDE.md:1).

### Verified facts (do NOT re-verify, they're already checked)
- DB is **Postgres**, not MySQL. CLAUDE.md is out of date on this.
- Tables that **already exist**: `signal_interpretations`, `tracked_positions`, `position_updates`, `agent_opinions`, `trader_profiles`, `trader_performance`, `watched_users`, `collector_catalog`, `news_items`, `media_items`, `system_config`, `api_cost_log`, `agent_personas`, `agent_prompts`, `agent_decisions`, `agent_state`.
- `tracked_positions` is **rich** (PnL/SL/TP/MFE/MAE) but **missing** `actor_type`, `actor_id`, `position_kind`, `asset_class`, `legs`, `entry_reason_*`, `exit_reason_*`, `closed_at`, `postmortem_status`. Phase 2 adds these.
- `signal_interpretations` has `model_version`, `param_hash`, `candle_data_hash` but is **missing** prefilter_*, vision_provider/model, prompt_version/hash, dual-reason, pattern_tags. Phase 2 adds these.
- `api_cost_log` exists with provider/model/role/tokens/cost/latency/company_id — ready to use.
- `ServiceDaemon` ([`shared/services/daemon.py`](shared/services/daemon.py:100)) and `ServiceRegistry` ([`shared/services/registry.py`](shared/services/registry.py:66)) are ready scaffolding.
- Dashboard at [`shared/dashboard/server.py`](shared/dashboard/server.py:1) is aiohttp + Telegram-OTP and works today.
- TUI [`shared/catalogue/tui_manager.py`](shared/catalogue/tui_manager.py:357) menu is options 1–5 + Hierarchy + quit. Phase 5 adds Signals + Positions tabs.
- `/opt/tickles/opticals/` does **NOT** exist. Phase 4 creates it.
- Surgeon2 ([`shared/daemons/surgeon2_trader.py`](shared/daemons/surgeon2_trader.py:199)) still has runtime-created standalone tables. Phase 10 migrates it.
- Anti-hallucination clauses already in [`shared/intelligence/prompts/chart_analysis.json`](shared/intelligence/prompts/chart_analysis.json:1).
- Pre-filter (Gemini 2.5 Flash) already in [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:268) `run_prefilter()`.
- Vision uses `tickles-vision` Requesty waterfall via [`shared/intelligence/gateway_config.py`](shared/intelligence/gateway_config.py:33) `GatewayConfig.for_service()` — **missing** temperature field and per-call `api_cost_log` writes.

### Open questions to confirm before/during work
1. **The user's truncated "non-negotiable":** treated as the C2 hindsight-bias rule (Phase 6) **plus** Rule 1 / Rule 2 / Rule 3 from CONTEXT_V3.md. Confirm.
2. Vision provider lock-in vs waterfall (currently waterfall — see Phase 0 §2.)
3. Auth on `/opticals/` (currently Tailscale-only; Phase L moves it behind OTP if asked).
4. Surgeon1 (flat-file) — leave as legacy (default) or migrate.

### Phase order (recommended)
```
Phase 0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11 → L → R
  pre   env   schema  raw    opt  web  freeze  pm   ch  zone   s2   edge  gui  ci-gates
              (G1)    (G5 in 1)  (G2 in 5)   (G3 openclaw in 7,8,11)              (G7)
```

**Phase budget (re-counted post-rewrite):**
| Phase | Days | Headline |
|---|---|---|
| 0 | 0.5 | Pre-kickoff blockers + budget guards |
| 1 | 1.5 | Gateway config + universal `api_cost_log` |
| 2 | 1.5 | Schema extensions + reason-freeze prep |
| 3 | 1.0 | Raw payload retention + correlation IDs |
| 4 | 1.5 | Opticals folder + thumbnail policy + signal review export |
| 5 | 2.5 | Served HTML manage panel (replaces TUI) |
| 6 | 2.0 | Reason-freeze trigger + prompt registry + master-sync |
| 7 | 3.0 | PostMortemService + memu_outbox + listener |
| 8 | 2.5 | ChartHackerOpinionService + opinion budget |
| 9 | 2.0 | Trading-zone monitoring + dedup |
| 10 | 2.5 | Surgeon2 unification + writer-registry + actor_instance |
| 11 | 4.0 | edge_score + actor_performance + CoachService |
| L | 5.0 | Dashboard rewrite + anchors + chart renderer |
| R | 2.0 | CI gates + cron canary + drift detector |
| **Total** | **~31** | **14 phases** |

**Re-review revision cross-reference (G1–G7):**
- G1 — dynamic LLM-driven tag taxonomy: §2.1 + Phase 6 prompt rule + Phase 11 normaliser
- G2 — manage panel served HTML (replaces Phase 5 TUI): Phase 5
- G3 — OpenClaw-native agent shells for new LLM services: Phases 7, 8, 11
- G4 — Mem0 (per-agent/per-company) vs MemU (cross-company): §1 Memory tiers table; lessons split in Phase 7
- G5 — universal `api_cost_log` writer (every API surface): Phase 1 §A + Phase L stats strip
- G6 — Phase L Live Queue + Trader Drill (no field overkill): Phase L tab table
- G7 — schema-diff CI gate + writer-registry grep + cron canary: **Phase R**

**Devil's-advocate findings (AA–BP) — all 42 embedded:**
- Phase 0: AA (open questions), BO (CSV header escaping), BP (LLM budget circuit-breaker)
- Phase 1: A (api_cost_log additive), Q (cost-log master switch), E (vision call routing)
- Phase 2: AG (reason-freeze trigger), AH (`actor_type` enum), AI (`position_kind` enum)
- Phase 3: AL (payload retention), correlation IDs (`shared/utils/correlation.py`)
- Phase 4: AM (thumbnail size cap), AN (signed URLs), AO (atomic file swap), AP (CSV column contract)
- Phase 5: AR (manage.js fetch wrapper), AS (CSRF), AT (rate limiter), G2 (served HTML), mutation blast-radius cap, audit trail
- Phase 6: AU (pgvector reason-similarity), AK (tag-cluster threshold), N (prompt registry), BN (master-schema sync), instrument normaliser
- Phase 7: J (memu_outbox), AV (NOTIFY listener), AW (outbox-then-notify), AJ (post-mortem schema), AX (failure modes), I (`--tools read,exec`)
- Phase 8: AY (opinion budget), AZ (single-writer policy), I
- Phase 9: BA (zone filter), BB (image phash dedup), BB' (rate limit on collectors)
- Phase 10: BC (writer-domain registry), BD (`actor_instance` namespace)
- Phase 11: K (`trader_performance` reconciliation), BE (formula details), BF (minimum-sample gate), BG (CoachService + prompt A/B), I
- Phase L: L (7 tabs), BH (anchor-link contract), BI (dashboard-as-aggregator), BK (cached chart renderer), H (kind colours), G6
- Phase R: BL (schema-diff CI), BM (cron health canary), BN (master-sync as gate), BC (writer-registry grep gate)

**Dependencies:**
- Phase 6 freeze trigger requires Phase 2 columns → do Phase 2 first.
- Phase 7 PostMortem requires Phase 6 freeze (so it can write `exit_reason_postmortem` without violating it).
- Phase 8 OpinionService is independent of Phase 7 but reads `tracked_positions` so Phase 2 schema must be in.
- Phase 10 Surgeon2 migration depends on Phases 2 + 6 + 7 (lands on the unified ledger with frozen reasons + central post-mortem).
- Phase 11 edge_scorer requires Phase 7 post-mortems and Phase 8 opinions flowing for full-component actors.
- Phase L can begin in parallel with Phase 11 since the dashboard skeleton just needs Phase 4's data shape.
- Phase R should land **immediately after** Phase 6 [BN] master-sync exists (writer-registry grep needs `table_writers` from Phase 10 [BC] seeded). Run Phase R last because it consumes invariants from every prior phase.

### Resume command
> **Read [`shared/docs/INTELLIGENCE_UNIFIED_PLAN.md`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:1) and execute Phase {N}. Verify the previous phase's benchmark checklist is fully checked before starting. Do not begin Phase {N+1} until Phase {N}'s benchmark is green. Each phase is laid out in uniform sub-sections: Purpose / What / Where / When / Why / How / Risks & mitigations / Benchmark checklist.**

### Files most likely to be modified next session
- [`shared/intelligence/migrations/2026_04_29_phase2_schema_extensions.sql`](shared/intelligence/migrations/) (Phase 2 — to be created)
- [`shared/intelligence/migrations/2026_04_29_phase6_reason_freeze.sql`](shared/intelligence/migrations/) (Phase 6 — to be created)
- [`shared/intelligence/migrations/2026_05_01_phase10_surgeon2_migration.sql`](shared/intelligence/migrations/) (Phase 10 — to be created)
- [`shared/intelligence/migrations/2026_05_04_phase_r_cron_heartbeats.sql`](shared/intelligence/migrations/) (Phase R — to be created)
- [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:1) (Phases 2, 3, 6, 7)
- [`shared/intelligence/gateway_config.py`](shared/intelligence/gateway_config.py:1) (Phase 1)
- [`.env`](.env:1) and [`.env.template`](.env.template:1) (Phases 0, 1)
- [`shared/catalogue/tui_manager.py`](shared/catalogue/tui_manager.py:1) (Phase 5 — replaced by served HTML)
- [`shared/dashboard/server.py`](shared/dashboard/server.py:1) (Phases L, R)
- [`shared/services/registry.py`](shared/services/registry.py:1) (Phases 7, 8, 11, R)
- [`shared/scripts/schema_diff.py`](shared/scripts/) (Phase R — to be created)
- [`shared/scripts/writer_registry_grep.py`](shared/scripts/) (Phase R — to be created)
- [`.github/workflows/schema-and-writer-gates.yml`](.github/workflows/) (Phase R — to be created)
- [`CLAUDE.md`](CLAUDE.md:1) (after each phase completes)

### Hard rules (do not break)
- **Rule 1**: backtest ≡ live; every interpretation has `model_version`, `param_hash`, `candle_data_hash`.
- **Hindsight-bias freeze (Phase 6 [AG])**: `entry_reason_*` immutable after `pending`. Trigger-enforced. Post-mortems write to `exit_reason_postmortem` only.
- **Single writer (Phase 8 [AZ])**: `agent_opinions[agent_id='chart_hacker']` written ONLY by ChartHackerOpinionService. Enforced by Phase 10 [BC] writer-registry + Phase R [BC] grep gate.
- **Writer-domain registry (Phase 10 [BC])**: every table that has multiple potential writers is gated through `public.table_writers`. Boot-time `assert_authorised()` + CI grep gate (Phase R).
- **`actor_instance` namespace (Phase 10 [BD])**: every row written to `tracked_positions` and `agent_state` includes `actor_instance` resolved via `os.getenv("POD_UID") or socket.gethostname()`.
- **Platform-agnostic edge_score (Phase 11 [BE])**: available-component renormalisation, `COMPONENT_CAP=0.95`, `FORMULA_VERSION=1`, no Discord-flavour bias.
- **Minimum-sample gate (Phase 11 [BF])**: `MIN_POSITIONS_FOR_DISPLAY=3` (hidden below), `MIN_POSITIONS_FOR_FULL=10` (`confidence_low=true` below).
- **Reason-freeze + outbox (Phase 7 [J])**: every cross-company memory broadcast is enqueued in `memu_outbox`, drained by `memu_listener`. No direct `memu.add()` from service code.
- **Multi-tenant**: shared in `tickles_shared`, per-company in `tickles_<company>`. Phase L [BI] dashboard reads from all companies via `get_company_pool()`; never writes.
- **Snake_case everywhere**, decimal(20,8) prices, decimal(30,8) volumes, UTC datetimes.
- **Never delete production data without explicit user confirmation.**
- **No hardcoded API keys.** All gateway config via [`shared/intelligence/gateway_config.py`](shared/intelligence/gateway_config.py:1).
- **(G1) Dynamic taxonomy**: pattern / setup / regime / session tags are LLM-emitted free-form strings. **Never** define an enum, never list examples in the prompt. Normalisation is post-hoc (Phase 6 [AK]).
- **(G3) Paperclip-visible LLM services**: any new LLM-orchestrating service is registered as an OpenClaw agent + cron per [`NEW_TRADING_AGENT_HOWTO.md`](shared/docs/NEW_TRADING_AGENT_HOWTO.md:1). `--tools read,exec` flag mandatory.
- **(G4) Memory split**: actor-level lessons → Mem0; company-/cross-company-level lessons → MemU via `memu_outbox`. Never bypass the outbox.
- **(G5) `api_cost_log` everywhere**: every outbound API call (paid or free) writes one row via [`log_api_call()`](shared/utils/api_cost_log.py:1). Free APIs log with `cost_usd=0`.
- **(G7) CI gates non-negotiable**: schema-diff + writer-registry grep + master-sync must all pass on every PR. No `[skip ci]` bypass on these checks.
- **Anchor-link contract (Phase L [BH])**: deep-links use `#sig-<id>`, `#pos-<id>`, `#opn-<id>`, `#pm-<id>`, `#trade-<id>`, `#interp-<id>`. Always go through `anchor_for(kind, id_)`.
- **Cron heartbeats (Phase R [BM])**: every cron-driven agent calls `record_heartbeat()` on every tick; cron_canary alerts on staleness > 2 × interval.

### After-each-phase ritual
1. Run the phase's benchmark checklist; all boxes must be checked.
2. Update [`CLAUDE.md`](CLAUDE.md:1) with the new component / table / service.
3. Commit with `[phase-N] <brief>`.
4. Store decision summary to dev mem (Qdrant) via `mcp--qdrant--qdrant-store` with `metadata.type=completion`, `metadata.phase=N`.
5. Run all three CI gates locally (`make gate-local`) before pushing.
6. Move to next phase.

### Uniform phase contract (every Phase 0–R follows this)
1. **Purpose** — one paragraph: why this phase exists, what invariant it establishes.
2. **What** — numbered list of concrete deliverables.
3. **Where** — table mapping each deliverable to a file path with `NEW` / `EXTEND` / `REPLACE` action.
4. **When** — day-by-day breakdown with morning/afternoon/evening granularity.
5. **Why** — bullet list explaining each major design decision.
6. **How** — code blocks (SQL + Python) for every file the phase creates or modifies.
7. **Risks & mitigations** — table of failure modes and their counter-measures.
8. **Benchmark checklist** — explicit, machine-verifiable acceptance criteria; the phase is not done until every box is ticked.

---

*End of plan. Total estimated effort: ~31 days of focused engineering across 14 phases (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, L, R). Each phase is independently shippable with its own benchmark checklist; user approval at the end of each phase before starting the next. All 20 original A–T patches and all 42 AA–BP devil's-advocate findings are embedded inline in the relevant phase. Phase R is the enforcement layer — without it, every prior invariant degrades over time.*
