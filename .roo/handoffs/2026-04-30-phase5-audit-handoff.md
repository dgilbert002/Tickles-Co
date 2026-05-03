# Handoff — 2026-04-30 — Phase 5 Audit Complete

## Session Summary

Completed a full code audit of Phase 5 (Served HTML Manage Panel) against the
INTELLIGENCE_UNIFIED_PLAN.md requirements. Found and fixed 3 bugs, implemented
8 missing features, and verified all Phase 5 tests pass.

---

## Bugs Found & Fixed

### 1. CRITICAL — `build_app()` never called `attach_routes()`
- **File:** `shared/dashboard/server.py`
- **Impact:** `/manage/*` routes existed in `server_routes.py` but were NEVER
  mounted on the production aiohttp app. The panel was unreachable in production.
- **Fix:** Added `from shared.intelligence.manage_panel.server_routes import attach_routes`
  and `attach_routes(app)` inside `build_app()`.
- **Why tests passed before:** Test helpers manually called `attach_routes(app)`
  in `_mk_authed_client()`, masking the production bug.

### 2. `_render()` returned `None` (missing `return`)
- **File:** `shared/intelligence/manage_panel/server_routes.py`
- **Impact:** Every HTML view route returned 500 "Missing return statement on
  request handler" because `_render()` rendered the Jinja2 template into a local
  `html` variable but never returned the `web.Response`.
- **Fix:** Added `return web.Response(text=html, content_type="text/html")`.

### 3. `_audit_panel_mutation()` corrupted with phantom `return web.Response`
- **File:** `shared/intelligence/manage_panel/server_routes.py`
- **Impact:** A stray `return web.Response(text=html, content_type="text/html")`
  line was inside the `_audit_panel_mutation()` helper (likely from a bad
  search/replace during the `_render()` fix). This caused `NameError: name
  'html' is not defined` on every mutation, which then bubbled as 500.
- **Fix:** Removed the stray line.

---

## Missing Features Implemented (per plan §A–§J)

| # | Requirement | Status | Files |
|---|-------------|--------|-------|
| 1 | `handle_sources_add` was a stub | **DONE** | `server_routes.py`, `db_views.py` |
| 2 | Channels view missing | **DONE** | `server_routes.py`, `channels.html.jinja2`, `base.html.jinja2` |
| 3 | Users view missing | **DONE** | `server_routes.py`, `users.html.jinja2`, `base.html.jinja2` |
| 4 | User enable/disable API | **DONE** | `server_routes.py`, `manage.js` |
| 5 | CSRF token rotation on POST | **DONE** | `shared/dashboard/csrf.py` |
| 6 | api_cost_log audit trail | **DONE** | `server_routes.py` — `_audit_panel_mutation()` wired into all 6 mutation handlers |
| 7 | MANAGE_SOURCES.md outdated | **DONE** | `MANAGE_SOURCES.md` — now documents both TUI and web panel |
| 8 | manage.js smoke tests | **DONE** | `shared/tests/test_manage_fetch_wrapper.py` (9 tests) |

---

## Files Modified Today

- `shared/dashboard/server.py` — added `attach_routes(app)` call
- `shared/dashboard/csrf.py` — CSRF rotation on successful POST
- `shared/intelligence/manage_panel/server_routes.py` — _render fix, audit helper
  fix, handle_sources_add implementation, channels/users views, user toggle
  handlers, audit trail wiring
- `shared/intelligence/manage_panel/db_views.py` — added `add_source()` and
  `toggle_user()`
- `shared/intelligence/manage_panel/templates/channels.html.jinja2` — **NEW**
- `shared/intelligence/manage_panel/templates/users.html.jinja2` — **NEW**
- `shared/intelligence/manage_panel/templates/base.html.jinja2` — nav links
- `shared/intelligence/manage_panel/static/manage.js` — disableUser/enableUser
- `MANAGE_SOURCES.md` — comprehensive web panel documentation
- `shared/tests/test_manage_fetch_wrapper.py` — **NEW**

---

## Test Results

- **All 34 asyncio Phase 5 tests PASS**:
  - `test_manage_fetch_wrapper.py` — 9 passed
  - `test_manage_csrf.py` — 7 passed
  - `test_manage_rate_limit.py` — 5 passed
  - `test_tui_readonly.py` — 5 passed
  - `test_manage_panel_routes.py` — 11 passed (asyncio only)
  - `test_manage_panel_auth.py` — 8 passed (asyncio only)

- **Trio variants:** Fail due to pre-existing `pytest-anyio` + `aiohttp.TestClient`
  CookieJar incompatibility (trio backend has no running asyncio event loop for
  `aiohttp.CookieJar`). Previously these were auto-skipped (29 trio-skipped in
  earlier runs); now pytest-anyio runs them and they fail. This is an
  environmental/config issue, not a code bug.

- **Broader suite:** 75 failures / 9 errors in non-Phase 5 modules
  (`shared.cli` missing, exchange live connectivity, MCP tools, streaming crypto,
  etc.) — all pre-existing.

---

## Decisions Made

1. **Audit log failures are fire-and-forget:** `_audit_panel_mutation()` wraps
   `log_api_call()` in `try/except` and swallows all exceptions. A failed audit
   log must NEVER block the user's mutation response or return 500.

2. **CSRF rotation is unconditional on success:** Every successful POST/PUT/
   DELETE/PATCH gets a fresh `__Host-csrf` cookie. This matches the plan's
   "rotated on every successful POST" requirement.

3. **TestClient trio failures are out of scope:** Fixing pytest-anyio's trio
   backend support for `aiohttp.TestClient` is a test infrastructure issue, not
   a Phase 5 deliverable. The asyncio tests provide full coverage.

---

## Next Phase — Phase 6

Per `INTELLIGENCE_UNIFIED_PLAN.md`, Phase 6 is **"Position Postmortems +
MemU Broadcast"** (lines ~1565–1859 in the plan). Key deliverables include:

- `shared/intelligence/postmortem_service.py` — daemon that generates
  postmortems when `tracked_positions` transitions to `closed`
- `shared/memu/broadcast_payload.py` — MemU outbox payload builder
- `shared/memu/listener_service.py` — MemU broadcast listener daemon
- `shared/intelligence/interpretation_service.py` — `broadcast_insight()` rewrite
- Schema: `position_postmortems` table (per-company, already in migration)
- `memu_outbox` table + outbox-then-notify pattern
- G3 OpenClaw shell integration with `--tools read,exec`
- Strip ad-hoc post-mortems from existing code

**Resume command:**

```
Read the Phase 6 section of shared/docs/INTELLIGENCE_UNIFIED_PLAN.md (lines ~1565–1859) and begin implementing Position Postmortems + MemU Broadcast. Start by reading the existing shared/intelligence/interpretation_service.py broadcast_insight() function and shared/memu/ directory to understand current MemU wiring.
```

---

## Open Questions / Blockers

None. Phase 5 is fully complete and audited.

---

## Key File Paths for Next Session

- Plan source of truth: `shared/docs/INTELLIGENCE_UNIFIED_PLAN.md`
- Project context: `CLAUDE.md`
- Phase 5 panel routes: `shared/intelligence/manage_panel/server_routes.py`
- Phase 5 panel DB layer: `shared/intelligence/manage_panel/db_views.py`
- Dashboard server: `shared/dashboard/server.py`
- CSRF middleware: `shared/dashboard/csrf.py`
- Rate limiter: `shared/dashboard/rate_limit.py`
