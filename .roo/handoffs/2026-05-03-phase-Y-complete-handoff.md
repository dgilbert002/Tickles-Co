# Phase Y — Learning Dashboard & Memory Feed — COMPLETE

**Date:** 2026-05-03
**Author:** Roo (Code mode, dev namespace)
**Plan reference:** [`shared/docs/PHASE_Y_LEARNING_DASHBOARD_PLAN.md`](shared/docs/PHASE_Y_LEARNING_DASHBOARD_PLAN.md:1)
**Detailed handoff:** [`shared/docs/PHASE_Y_HANDOFF.md`](shared/docs/PHASE_Y_HANDOFF.md:1)

## Status

All 6 sub-phases of PHASE_Y (Y.0 → Y.5) are landed and green. Targeted PHASE_Y test suite is **267/267 passing**. Broader 18-file integration run is **315/320** with the 5 failures verified as pre-existing infrastructure issues unrelated to PHASE_Y (asyncpg/trio loop-binding in [`test_position_postmortem_uniqueness.py`](shared/tests/test_position_postmortem_uniqueness.py:1) + an auth assertion in [`test_dashboard_ws.py`](shared/tests/test_dashboard_ws.py:1)). None of those failing tests import any Y.5-touched module.

## Sub-phase summary

| Phase | Deliverable | Tests |
|---|---|---|
| Y.0 | MemU enum reconciliation ([`shared/intelligence/memu/enums.py`](shared/intelligence/memu/enums.py:1)) | smoke green |
| Y.1 | `skill_views` migration + Python mirror [`skill_scorer.py`](shared/intelligence/skill_scorer.py:1) | 90/90 |
| Y.2 | Recall-log writer + retroactive updater [`shared/intelligence/recall_log.py`](shared/intelligence/recall_log.py:1) | 170/170 |
| Y.3 | SnapshotBuilder providers [`shared/dashboard/learning_providers.py`](shared/dashboard/learning_providers.py:1) + routes [`learning_routes.py`](shared/dashboard/learning_routes.py:1) | 220/220 |
| Y.4 | Dashboard Learning tab UI ([`shared/dashboard/static/app.js`](shared/dashboard/static/app.js:161), [`web/index.html`](shared/dashboard/web/index.html:185)) | 272/272 |
| Y.5 | Guard sidebar + Prompt Evolution + `skill_minus_luck` wiring + A/A coach seed [`shared/intelligence/coach_aa_seed.py`](shared/intelligence/coach_aa_seed.py:1) | 239/239 |

## Y.5 fixes applied (code-review + bug-hunt)

5 issues fixed (1 CRITICAL, 3 MEDIUM, 1 LOW):

- **B-Y5-C1** — Concurrent provider gather. Signal 3 (A/A seed) now runs in parallel with the per-company guard signals via `asyncio.gather`, both wrapped in `_run_with_budget`, so a slow shared-pool query no longer steals the 250ms budget from per-company calls. ([`learning_providers.py:617-685`](shared/dashboard/learning_providers.py:617))
- **B-Y5-M1** — Connection lifecycle in `run_seed`. [`coach_aa_seed.py`](shared/intelligence/coach_aa_seed.py:259) now opens **one** asyncpg connection per run via `get_company_dsn()` + `asyncpg.connect()` with `try/finally: await conn.close()`, instead of opening a fresh connection per iteration. `_record_assignment` signature changed: dropped `company`, added `conn`.
- **B-Y5-M2** — `correlation_id` collision risk. Replaced `uuid.uuid4().hex[:8]` with full `uuid.uuid4().hex` to keep `(correlation_id, operation)` idempotency intact across many seed runs.
- **B-Y5-M3** — Decimal-as-string contract. `_log_synthetic_call` now stringifies `cost_usd` to preserve full precision through `api_cost_log` JSON.
- **B-Y5-L8** — Standing-rule violation: `import asyncpg` was inline; moved to top-of-file imports per [`/opt/tickles/.roo/rules-code/rules.md`](.roo/rules-code/rules.md:1).

7 LOW findings deferred (non-exploitable today): documented in [`shared/docs/PHASE_Y_HANDOFF.md`](shared/docs/PHASE_Y_HANDOFF.md:1) §"Deferred LOW findings".

## Pre-existing failures (NOT caused by PHASE_Y)

- [`test_dashboard_ws.py::test_queue_ws_unauthorized`](shared/tests/test_dashboard_ws.py:1) — auth assertion. Imports only `dashboard.server`, `dashboard.protocol`, `dashboard.auth`, `dashboard.ws`. None modified by PHASE_Y.
- [`test_position_postmortem_uniqueness.py`](shared/tests/test_position_postmortem_uniqueness.py:1) — 4 asyncio failures + 4 trio errors. All `RuntimeError: ... got Future ... attached to a different loop`. asyncpg/trio loop-binding incompatibility against a real DB; predates PHASE_Y.

## Files modified / created in this session

**Created (untracked):**
- [`shared/intelligence/coach_aa_seed.py`](shared/intelligence/coach_aa_seed.py:1)
- [`shared/intelligence/edge_scorer.py`](shared/intelligence/edge_scorer.py:1) (skill_minus_luck wiring)
- [`shared/dashboard/learning_providers.py`](shared/dashboard/learning_providers.py:1)
- [`shared/tests/test_coach_aa_seed.py`](shared/tests/test_coach_aa_seed.py:1)

(Plus everything from Y.0–Y.4, see plan doc and prior handoffs.)

## Mem0 / Qdrant note

Qdrant MCP store call failed with `400 Bad Request: Wrong input: Not existing vector name error: fast-all-minilm-l6-v2` — Qdrant collection vector-name configuration mismatch in the MCP server. Memory content is folded into [`shared/docs/PHASE_Y_HANDOFF.md`](shared/docs/PHASE_Y_HANDOFF.md:1) until the Qdrant vector config is corrected.

## What's next (backlog)

1. Phase X.4 — News Feed tab
2. Phase X.5 — Cross-tab Interpretation Drawer
3. Phase X.6 — Config tab
4. Phase R closeout

## Resume command

```
Continue with backlog from PHASE_Y_HANDOFF.md. Start Phase X.4 (News Feed tab). Use code-reviewer + bug-hunter between phases. Read shared/docs/PHASE_Y_HANDOFF.md and .roo/handoffs/2026-05-03-phase-Y-complete-handoff.md first.
```
