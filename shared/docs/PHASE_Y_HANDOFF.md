# PHASE_Y — Learning Dashboard & Memory Feed — Handoff

**Status:** ✅ COMPLETE — all six sub-phases (Y.0–Y.5) landed, code-reviewed, bug-hunted, fixed, and re-tested.
**Date:** 2026-05-03 (Europe/Berlin)
**Plan of record:** [`shared/docs/PHASE_Y_LEARNING_DASHBOARD_PLAN.md`](PHASE_Y_LEARNING_DASHBOARD_PLAN.md:1)
**Short handoff:** [`.roo/handoffs/2026-05-03-phase-Y-complete-handoff.md`](../../.roo/handoffs/2026-05-03-phase-Y-complete-handoff.md:1)

---

## 1. Final test status

### Targeted PHASE_Y suite (10 files) — **267/267 ✅**
```
shared/tests/test_learning_providers.py     58 tests
shared/tests/test_coach_aa_seed.py          13 tests
shared/tests/test_coach_service.py           5 tests
shared/tests/test_edge_scorer.py            17 tests
shared/tests/test_phase_y_migrations.py     44 tests
shared/tests/test_recall_log.py             53 tests
shared/tests/test_learning_routes.py        49 tests
shared/tests/test_skill_scorer.py           20 tests
shared/tests/test_prompt_registry.py         3 tests
shared/tests/test_dashboard_tabs.py          5 tests
                                          ===========
                                          267 passed in 2.34s
```

### Broader 18-file integration run — **315/320** (5 pre-existing failures)
```
5 failed, 315 passed, 4 errors in 52.43s
```

The 5 failures + 4 trio errors are **NOT caused by PHASE_Y** and were verified by:

1. **Import audit:** [`test_dashboard_ws.py`](../tests/test_dashboard_ws.py:1) only imports `dashboard.server`, `dashboard.protocol`, `dashboard.auth`, `dashboard.ws` — none touched by PHASE_Y.
2. **Failure mode:** [`test_position_postmortem_uniqueness.py`](../tests/test_position_postmortem_uniqueness.py:1) all 8 cases raise `RuntimeError: Task ... got Future ... attached to a different loop` — a known asyncpg/anyio-trio loop-binding incompatibility against a real Postgres DB.
3. **Git status:** every PHASE_Y file is still untracked (`??`), so it has no prior committed version that test runs predating my edits could have referenced.

### Excluded from full-repo run

11 unrelated test files fail to **collect** with `ModuleNotFoundError: No module named 'shared.cli'`:
```
test_strategies.py, test_canon_repo.py, test_paper_runtime.py, test_workflow_state.py,
test_router_integration.py, test_event_kinds.py, test_orchestrator_app.py,
test_positions_writer.py, test_strategy_health_dashboard.py, test_tool_registry.py,
test_pipeline_outbox.py
```
All predate PHASE_Y. Recommend adding `shared/cli/__init__.py` (or removing the `from shared.cli import ...` lines) in a separate housekeeping pass.

---

## 1.1 Resolved (housekeeping pass, 2026-05-03)

The pre-existing failures called out above were resolved before opening Phase X.4. None of these touched PHASE_Y code.

### A0 — `shared.cli` ModuleNotFoundError
The real CLI modules live at `shared/candles/cli/` but tests historically import them as `shared.cli.X`. Fix: created a parallel `shared/cli/` package with one thin shim per submodule. Each shim swaps itself out in `sys.modules` for the real `shared.candles.cli.X` module so introspection (including underscore-prefixed helpers like `_build`) works, and falls through to `runpy.run_module` when invoked as `python -m shared.cli.X`. Result: collection now picks up **1340 tests, zero errors**.
- Files: `shared/cli/__init__.py`, `shared/cli/_common.py`, plus 26 generated submodule shims.

### A1 — `test_dashboard_ws::test_queue_ws_unauthorized`
The test asserts middleware-enforced 401/302 for missing/invalid tokens, but `auth_middleware` was deliberately disabled in `shared/dashboard/server.py:91` ("AUTH DISABLED PER USER REQUEST", 2026-05-02). The test no longer reflects production behaviour. Marked `@pytest.mark.skip` with a reason that points back here so it's easy to unskip if auth is re-enabled.
- File: `shared/tests/test_dashboard_ws.py`

### A2 — `test_position_postmortem_uniqueness` loop-binding
All 4 asyncio failures + 4 trio errors stemmed from `@pytest.mark.anyio` running each test against both backends while the project's `pytest.ini` already sets `asyncio_mode = auto`. The dual setup created two event loops and asyncpg's connection objects ended up bound to the wrong one. Fix: replaced `@pytest.mark.anyio` with `@pytest.mark.asyncio` so only the asyncio backend runs (asyncpg only supports asyncio, so trio coverage was never meaningful). All 4 tests now pass.
- File: `shared/tests/test_position_postmortem_uniqueness.py`

---

## 2. Sub-phase deliverables

### Y.0 — MemU enum reconciliation
- [`shared/intelligence/memu/enums.py`](../intelligence/memu/enums.py:1) — single source of truth for MemU dimension/outcome enums.
- All call sites migrated.
- 90/90 smoke green.

### Y.1 — skill_views migration + Python mirror
- Migration adds 3 views (`v_actor_skill_7d`, `v_actor_skill_14d`, `v_actor_skill_30d`) + `compute_skill()` SQL function.
- Python mirror: [`shared/intelligence/skill_scorer.py`](../intelligence/skill_scorer.py:49)
- §11 Q4 adaptive-band: `GREATEST(COALESCE(stddev,0), 1.00)`.
- §11 Q1 weights table: `skill_weight_recommendations`.

### Y.2 — Recall-log
- Schema migration: `mem0_recall_log` (3 match flags, position_id unique, match_consistency CHECK).
- Writer + retroactive updater: [`shared/intelligence/recall_log.py`](../intelligence/recall_log.py:1)
  - [`record_recall()`](../intelligence/recall_log.py:216) — insert at decision time
  - [`link_recall_to_position()`](../intelligence/recall_log.py:333) — patch position_id
  - [`match_recall_to_outcome()`](../intelligence/recall_log.py:438) — set match_* booleans + matched_at
- 53 tests green.

### Y.3 — SnapshotBuilder learning providers + routes
- [`shared/dashboard/learning_providers.py`](../dashboard/learning_providers.py:1) — six providers all wrapped in 250ms `_run_with_budget()`:
  - [`SkillSummaryProvider`](../dashboard/learning_providers.py:188)
  - [`MemoryFeedProvider`](../dashboard/learning_providers.py:266)
  - [`AgentBrainProvider`](../dashboard/learning_providers.py:387)
  - [`GuardActivityProvider`](../dashboard/learning_providers.py:494)
  - [`PromptEvolutionProvider`](../dashboard/learning_providers.py:698)
  - [`FailedTradesProvider`](../dashboard/learning_providers.py:817)
- Routes: [`shared/dashboard/learning_routes.py`](../dashboard/learning_routes.py:1) — six `GET /api/learning/*` handlers.

### Y.4 — Dashboard Learning tab
- HTML: [`shared/dashboard/web/index.html`](../dashboard/web/index.html:185) — Learning tab structure, window-tabs (7d/14d/30d), 4-card grid.
- JS: [`shared/dashboard/static/app.js`](../dashboard/static/app.js:161) — [`renderLearning()`](../dashboard/static/app.js:161) with `Promise.all` fan-out to all six endpoints.
- 4 Y.4 fixes during code-review pass: handleAnchors deep-link wiring, `_outcomePill` color guard, failed-badge guard, stale-data clear on company-filter change.

### Y.5 — Guard sidebar + Prompt Evolution + skill_minus_luck wiring + A/A coach seed
- [`shared/intelligence/coach_aa_seed.py`](../intelligence/coach_aa_seed.py:1) — A/A seed CLI per §11 Q5:
  - $5 hard-cap, [`run_seed()`](../intelligence/coach_aa_seed.py:259), [`_log_budget_warning()`](../intelligence/coach_aa_seed.py:211) emits `role='aa_seed_budget_warn'`, `correlation_id='coach_aa_seed'`.
- [`shared/intelligence/edge_scorer.py`](../intelligence/edge_scorer.py:263) — [`_skill_minus_luck()`](../intelligence/edge_scorer.py:263) component with NaN/Inf guard + range clip, registered in `compute_edge_score`.
- Guard signal 3 ([`learning_providers.py:617`](../dashboard/learning_providers.py:617)) lights from a `aa_seed_budget_warn` row.
- Prompt Evolution = [`PromptEvolutionProvider`](../dashboard/learning_providers.py:698) reading `edge_score_changes` rows where `note='prompt_promoted'`.

---

## 3. Y.5 fixes (B-Y5-C1, M1, M2, M3, L8)

### B-Y5-C1 — CRITICAL: concurrent provider gather
**Problem:** Signal 3 (A/A seed shared-pool query) ran sequentially after the per-company guard signals, both inside the same `_run_with_budget(250ms)` call. A slow shared-pool query stole the entire budget from the per-company signals.

**Fix:** Split the guard provider into two coroutines: [`_do_fetch()`](../dashboard/learning_providers.py:538) (per-company, signals 1+2) and [`_do_fetch_aa_seed()`](../dashboard/learning_providers.py:617) (shared pool, signal 3). Each is wrapped in its **own** `_run_with_budget(250ms)`, and they run concurrently via `asyncio.gather`. Now a slow shared-pool call only kills signal 3.

### B-Y5-M1 — MEDIUM: per-iteration connection churn
**Problem:** [`run_seed()`](../intelligence/coach_aa_seed.py:259) opened a fresh asyncpg connection inside the loop for every iteration via `_record_assignment`. With `--calls=20` that's 20 TCP+TLS+auth handshakes against the company DB.

**Fix:** [`run_seed()`](../intelligence/coach_aa_seed.py:322) now resolves the DSN with [`get_company_dsn()`](../utils/companies.py:69) once, calls [`asyncpg.connect(dsn=dsn, timeout=10.0)`](../intelligence/coach_aa_seed.py:323) once, wraps the loop in `try/finally: await conn.close()`, and threads the connection through [`_record_assignment(conn=conn, ...)`](../intelligence/coach_aa_seed.py:135) and [`_log_synthetic_call(conn=conn, ...)`](../intelligence/coach_aa_seed.py:172). Test [`test_run_seed_happy_path`](../tests/test_coach_aa_seed.py:187) updated to assert `"conn" in call` instead of `call["company"] == "rubicon"`.

### B-Y5-M2 — MEDIUM: correlation_id collision risk
**Problem:** `correlation_id = uuid.uuid4().hex[:8]` truncated to 32 bits → birthday-paradox collision odds at ~50k seed runs would silently de-duplicate `(correlation_id, operation)` rows in `api_cost_log`.

**Fix:** Use full `uuid.uuid4().hex` (128 bits, collision-free for any realistic run count). Idempotency contract preserved.

### B-Y5-M3 — MEDIUM: Decimal precision loss
**Problem:** `_log_synthetic_call(cost_usd=Decimal("0.0500"))` was being passed straight into `log_api_call`, where JSON serialisation via `json.dumps` would convert it to a float and lose the trailing zero.

**Fix:** Stringify `cost_usd` to `"0.0500"` before passing to `log_api_call`. Decimal-as-string contract honoured end-to-end.

### B-Y5-L8 — LOW (standing-rule violation): inline import
**Problem:** `import asyncpg` was inside [`run_seed()`](../intelligence/coach_aa_seed.py:259), violating the project rule "all imports at the top of the file".

**Fix:** Moved `import asyncpg` to the top of [`shared/intelligence/coach_aa_seed.py`](../intelligence/coach_aa_seed.py:1).

---

## 4. Deferred LOW findings (non-exploitable today)

| ID | Finding | Why deferred |
|---|---|---|
| L1 | `AgentBrainProvider` SQL: `LATERAL (SELECT ...)` could be a CTE | Cosmetic. Plan executes identically; explain analyse confirmed. |
| L2 | `MemoryFeedProvider._merge_window` uses `heapq.merge` ordering on tuples | Equivalent to a 3-way `sort+limit`. Both are O(N log K). |
| L3 | `_validate_window` rejects 8d/13d/29d but plan only specifies 7/14/30 | Working as intended per §4.2. |
| L4 | `compute_skill` returns `None` instead of `0.0` when all components missing | Plan §3.4 explicitly requires `None` to distinguish "no data" from "0 skill". |
| L5 | `_evaluate_matches` runs all 3 tiers even after the first hit | Cheap; tier outputs are independent metadata used downstream. |
| L6 | `record_recall` re-hashes prompt body each call | Cached upstream by `prompt_registry`; sub-microsecond. |
| L7 | Guard signal SQL uses `2 hours` window literal | Documented in §11 Q5; tunable later via env if needed. |

All seven warrant tracking; none block production.

---

## 5. Schema changes inventory

```
shared/db/migrations/2026_05_XX_phase_y0_memu_enums.sql
shared/db/migrations/2026_05_XX_phase_y1_skill_views.sql
shared/db/migrations/2026_05_XX_phase_y1_skill_views.rollback.sql
shared/db/migrations/2026_05_XX_phase_y2_recall_log.sql
shared/db/migrations/2026_05_XX_phase_y2_recall_log.rollback.sql
shared/db/migrations/2026_05_XX_phase_y2_skill_weight_recommendations.sql
shared/db/migrations/2026_05_XX_phase_y2_skill_weight_recommendations.rollback.sql
shared/db/migrations/2026_05_XX_phase_y3_feed_views.sql
shared/db/migrations/2026_05_XX_phase_y3_feed_views.rollback.sql
```

44 migration tests in [`test_phase_y_migrations.py`](../tests/test_phase_y_migrations.py:1).

---

## 6. PHASE_Y §11 answers (recap)

- **Q1** — Skill weights: 5 components (pnl_quality, consistency, discipline, regime_adaptability, skill_minus_luck) × ratified weights. UI exposes Ask-AI button writing to `skill_weight_recommendations`.
- **Q2** — mem0 recall threshold: tier-1 ≥0.85, tier-2 ≥0.75, tier-3 ≥0.65 (per [`recall_log._evaluate_matches`](../intelligence/recall_log.py:99)).
- **Q3** — Window default: 7d.
- **Q4** — Failed-trade adaptive band: `GREATEST(COALESCE(stddev_realised_pnl_pct, 0), 1.00)`.
- **Q5** — Coach A/A seed: $5 hard-cap; budget tripping writes a single `aa_seed_budget_warn` row to `api_cost_log` with `correlation_id='coach_aa_seed'`.

---

## 7. Mem0 / Qdrant note

The Qdrant MCP server's `qdrant-store` call returned:
```
400 Bad Request: Wrong input: Not existing vector name error: fast-all-minilm-l6-v2
```

The collection's vector configuration does not include the `fast-all-minilm-l6-v2` vector name expected by the MCP server. **This is an MCP infrastructure issue, not a PHASE_Y bug.** Two options:
1. Update the Qdrant collection's vectors_config to include `fast-all-minilm-l6-v2`.
2. Update the MCP server config to point at the correct vector name in the existing collection.

Until that's resolved, decision memory is captured in this handoff doc.

---

## 8. Backlog (next session)

| # | Phase | Description |
|---|---|---|
| 20 | X.4 | News Feed tab — surface recent ingested news in dashboard |
| 21 | X.5 | Cross-tab Interpretation Drawer — slide-in detail pane |
| 22 | X.6 | Config tab — surface tunable parameters with safe-edit UX |
| 23 | R-closeout | Close out remaining R-phase items |

Recommended order: X.4 → X.5 → X.6 → R-closeout, with code-reviewer + bug-hunter passes between each.

---

## 9. Resume command

```
Continue with PHASE_Y backlog. Start with Phase X.4 (News Feed tab).
First read shared/docs/PHASE_Y_HANDOFF.md and .roo/handoffs/2026-05-03-phase-Y-complete-handoff.md.
Use code-reviewer + bug-hunter skills between phases. Continue all phases to the end.
```
