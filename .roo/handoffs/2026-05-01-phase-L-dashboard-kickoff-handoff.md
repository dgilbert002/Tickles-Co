# Phase L Dashboard — Kickoff Handoff

**Date:** 2026-05-01
**Author:** Roo (Architect → Code)
**Predecessor:** [`2026-05-01-phase6-11-complete-handoff.md`](.roo/handoffs/2026-05-01-phase6-11-complete-handoff.md:1)
**Plan reference:** [`shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:3310-3574`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:3310)

---

## 1. Where we are (state at kickoff)

### Phases 0–11: ✅ COMPLETE
Full audit performed 2026-05-01. All checkboxes in the plan (lines 358–3304) are `[x]` except a single `[-]` for `pg_dump --schema-only` (blocked by system role permissions, not a code issue) and `[ ]` for Phase R items (out of scope until later). Every daemon registered, every schema column present, every test green.

### One bug found and fixed during audit
Three SQL migrations used invalid PostgreSQL syntax `CREATE TRIGGER IF NOT EXISTS` (Postgres only allows `IF NOT EXISTS` on `CREATE TABLE` / `CREATE INDEX`, not triggers). Fixed by switching to the idempotent `DROP TRIGGER IF EXISTS … ; CREATE TRIGGER …` pattern that the same files already used elsewhere.

| File | Line | Trigger |
|------|------|---------|
| [`shared/intelligence/migrations/2026_04_29_phase2_schema_additions_shared.sql`](shared/intelligence/migrations/2026_04_29_phase2_schema_additions_shared.sql:135) | 135 | `trg_tracked_positions_updated` |
| [`shared/intelligence/migrations/2026_04_26_phase3b_intelligence.sql`](shared/intelligence/migrations/2026_04_26_phase3b_intelligence.sql:205) | 205 | `trg_signal_interpretations_updated` |
| [`shared/intelligence/migrations/2026_04_26_phase3b_intelligence.sql`](shared/intelligence/migrations/2026_04_26_phase3b_intelligence.sql:264) | 264 | `trg_trader_performance_updated` |

Verification: `rg "CREATE TRIGGER IF NOT EXISTS" shared/` → 0 hits.

---

## 2. What Phase L is

A **served HTML dashboard** (NOT manage-panel — that's Phase 5) at `https://vmi3220412.trout-goblin.ts.net/` over Tailscale. **Read-only**. Aggregates every active company via [`shared/utils/companies.py`](shared/utils/companies.py:1) `list_active_companies()` so adding a new `tickles_<x>` requires zero dashboard code changes.

### Seven tabs (plan §B, lines 3415-3423)

1. **Overview** — stats strip, system health, cost rollup
2. **Leaderboard** — `actor_leaderboard` view, period filter (7d/30d/90d/all)
3. **Signals** — `signal_interpretations` with Discord/Telegram/opticals links
4. **Positions** — `tracked_positions` cards with entry reasons, latest opinion, post-mortem
5. **Interpretations** — chart image + LLM JSON + Discord link inline
6. **Live Queue** — WebSocket push, ≤ 5 s latency
7. **Trader Drill** — side-by-side Discord chart vs LLM-rendered chart, free-form chip tags

### New code surfaces (plan §A, §D, §E)

- [`shared/dashboard/db_pools.py`](shared/dashboard/db_pools.py:1) — **NEW** — multi-company pool registry
- [`shared/dashboard/snapshot.py`](shared/dashboard/snapshot.py:1) — **EXTEND** — currently exists, needs Phase L additions
- [`shared/dashboard/anchors.py`](shared/dashboard/anchors.py:1) — **NEW** — `[BH]` `pos-12345` anchor scrolling + OTP redirect preservation
- [`shared/dashboard/chart_renderer.py`](shared/dashboard/chart_renderer.py:1) — **NEW** — `[BK]` SVG cache: 1st request renders matplotlib, 2nd serves cached file
- [`shared/dashboard/server.py`](shared/dashboard/server.py:1) — **EXTEND** — already exists, add 7 tab handlers + WS endpoint

### Cross-cutting concerns (plan §F, §G)

- **`[H]` ServiceDescriptor kind colours** — daemon/cron/agent_cron each get a distinct colour
- **`[BI]` Dashboard-as-aggregator** — strict no-write invariant; writer-registry CI gate (Phase R) must NOT list `dashboard` for any table
- **`[BK]` Chart cache** — invalidate when `prompt_version` bumps; old files pruned by retention
- **`[G5]` Cost overview** — sum every provider in `api_cost_log` (LLM + ccxt + Discord + Telegram)
- **`[G6]` Live Queue + Trader Drill** — show trader-given AND agent-taken trades + current logic step

---

## 3. Plan reading order for Phase L

Read these sections in [`shared/docs/INTELLIGENCE_UNIFIED_PLAN.md`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:3310) **in order** before writing any code:

| Lines | Topic |
|-------|-------|
| 3310-3357 | Purpose, What, Where, When, Why |
| 3362-3404 | §A db_pools.py + snapshot.py extension |
| 3405-3415 | §B Tab layout `[L]` |
| 3416-3423 | §C Stats overview strip |
| 3426-3455 | §D anchors.py `[BH]` |
| 3458-3500 | §E chart_renderer.py `[BK]` |
| 3501-3520 | §F ServiceDescriptor kind colours `[H]` |
| 3521-3525 | §G Auth + read-only invariant |
| 3526-3544 | Risks & mitigations |
| 3545-3574 | **Benchmark checklist (27 items, currently all `[ ]`)** |

---

## 4. Verified facts (do NOT re-verify)

- **Database:** PostgreSQL, NOT MySQL. `tickles_shared` (single ledger) + `tickles_<company>` per-company. `CLAUDE.md` was historically wrong about this — the plan corrected it.
- **`actor_leaderboard` view exists** in [`shared/migration/tickles_company_pg.sql:757-780`](shared/migration/tickles_company_pg.sql:757) — Phase 11 §B already defined it; Phase L only consumes it.
- **`api_cost_log` has all required columns** (provider, role, cost_usd numeric(20,8), correlation_id, success, http_status) per Phase 1 [G5] sweep.
- **OTP auth already exists** in [`shared/dashboard/auth.py`](shared/dashboard/auth.py:1).
- **CSRF + rate-limit modules exist** in [`shared/dashboard/csrf.py`](shared/dashboard/csrf.py:1) and [`shared/dashboard/rate_limit.py`](shared/dashboard/rate_limit.py:1) — Phase 5 built them; Phase L reuses.
- **Tailscale URL:** `https://vmi3220412.trout-goblin.ts.net/`
- **Existing dashboard server:** [`shared/dashboard/server.py`](shared/dashboard/server.py:1) is already running and serves `/manage/` (Phase 5). Phase L adds the public-read routes alongside.

---

## 5. Open questions to confirm before/during work

1. **Q-L1:** Does the user want the dashboard to default to a specific company (e.g. `rubicon`) on first page load, or always start with the aggregate view?
2. **Q-L2:** WebSocket cap is 50 hard, 51st gets `1013 try-again` per plan line 3569 — confirm this is per-server or per-user.
3. **Q-L3:** Chart cache directory location — `/opt/tickles/opticals/dashboard_charts/`? Or under `shared/reports/`?
4. **Q-L4:** Anchor format `pos-12345` confirmed in plan; should signals use `sig-12345` and interpretations `int-12345` for symmetry?

These are not blockers — sensible defaults exist in the plan — but worth raising in the first reply.

---

## 6. Hard rules (do not break)

- **NO writes from dashboard.** Ever. The writer-registry [BC] gate (Phase R) will fail the build if dashboard writes any table.
- **All queries parameterised.** Never f-string SQL.
- **All timestamps UTC.** Never local time.
- **No hardcoded API keys or DB DSNs.** Read from environment, fail fast on missing.
- **Default-deny auth.** Any new route inherits the `[D]` default-deny middleware; explicit allow-list for `/login`, `/static/*`, `/healthz`.
- **Mobile portrait (640 px)** must render cleanly on every tab.
- **No N+1 queries.** Verify with `EXPLAIN ANALYZE` showing ≤ 1 query per tab snapshot.

---

## 7. After-each-phase ritual

When Phase L is complete:
1. Tick every `[ ]` checkbox at lines 3546-3571 of the plan to `[x]` (or `[-]` if blocked).
2. Run `pytest shared/tests/test_dashboard_routes.py shared/tests/test_chart_renderer.py shared/tests/test_anchors.py` — must be green.
3. Update [`CLAUDE.md`](CLAUDE.md:1) Intelligence Pipeline section with the dashboard URL + tab list + anchor-link convention.
4. Write a `2026-MM-DD-phase-L-complete-handoff.md` summarising what was built and any deviations.
5. Move to Phase R (final regression / CI gates phase).

---

## 8. Recommended sub-task breakdown for Phase L

Suggested execution order (5 sub-tasks, each ~1 day):

1. **L.1 Foundation** — `db_pools.py`, extend `snapshot.py`, register routes, get an empty Overview tab returning 200.
2. **L.2 Read-only tabs** — Leaderboard, Signals, Positions, Interpretations rendering from existing tables/views.
3. **L.3 Trader Drill + chart_renderer** — `[BK]` SVG cache, side-by-side rendering, anchor links `[BH]`.
4. **L.4 Live Queue WebSocket** — `[G6]` push updates ≤ 5 s, hard cap 50, dedupe.
5. **L.5 Polish + auth + mobile + tests** — `[H]` kind colours, default-deny verification, 640 px portrait, the 27 benchmark checks.

Each sub-task should end with the corresponding plan checkboxes ticked and a sub-task completion handoff appended to this document.

---

## 9. Resume command

```
Read .roo/handoffs/2026-05-01-phase-L-dashboard-kickoff-handoff.md
then read shared/docs/INTELLIGENCE_UNIFIED_PLAN.md lines 3310-3574 in full,
then start Phase L sub-task L.1 (Foundation): build shared/dashboard/db_pools.py
and extend shared/dashboard/snapshot.py per plan §A. Confirm Q-L1 through Q-L4
before writing code.
```

---

## Appendix A — Phase 0–11 final scoreboard

| Phase | Status | Checkboxes | Notes |
|-------|--------|------------|-------|
| 0 | ✅ | 8/9 `[x]`, 1 `[-]` | `[-]` is `pg_dump --schema-only` blocked by system role permissions |
| 1 | ✅ | 14/14 `[x]` | BudgetGuard via `[BP]` anomaly-detection (final plan, not early draft) |
| 2 | ✅ | 12/12 `[x]` | **Bug fixed during audit:** 3× `CREATE TRIGGER IF NOT EXISTS` |
| 3 | ✅ | 11/11 `[x]` | payload_store + retention + orphan check |
| 4 | ✅ | 14/14 `[x]` | signal_review_export + Jinja template |
| 5 | ✅ | 18/18 `[x]` | manage-panel + csrf + rate_limit |
| 6 | ✅ | 16/16 `[x]` | prompt_registry + reason_similarity (pgvector) |
| 7 | ✅ | 18/18 `[x]` | postmortem + memu_outbox + listener |
| 8 | ✅ | 17/17 `[x]` | chart_hacker_opinion + opinion_budget [AY] |
| 9 | ✅ | 15/15 `[x]` | zone_filter + image_phash + rate_limit |
| 10 | ✅ | 19/19 `[x]` | writer_registry + surgeon2 migration |
| 11 | ✅ | 19/19 `[x]` | edge_scorer (11 components) + coach_service |
| **L** | ⏳ | 0/27 `[ ]` | **THIS HANDOFF** |
| R | ⏳ | 0/26 `[ ]` | After L |

**Total:** 181 of 234 plan benchmark checkboxes complete (77%).

---

*Handoff complete. Ready to begin Phase L.*
