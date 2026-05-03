# Handoff: Phase 1–7 Benchmark Checklists — 2026-04-30

## Completed Work

All benchmark checklist items from **Phase 1 through Phase 7** of [`shared/docs/INTELLIGENCE_UNIFIED_PLAN.md`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md) have been implemented, tested, and marked `[x]` in the plan file.

### Test Results
- **213 tests passed**, 74 deselected (trio backend), 0 failures
- Run command: `pytest shared/tests/test_env_validate.py shared/tests/test_grep_guard.py shared/tests/test_api_cost_log_precision.py shared/tests/test_api_cost_log_failed_calls.py shared/tests/test_correlation_propagation.py shared/tests/test_budget_guard.py shared/tests/test_reason_freeze_trigger.py shared/tests/test_position_postmortem_uniqueness.py shared/tests/test_vision_model_resolved.py shared/tests/test_payload_retention.py shared/tests/test_signal_review_export.py shared/tests/test_signal_review_xss.py shared/tests/test_manage_panel_routes.py shared/tests/test_manage_panel_auth.py shared/tests/test_manage_csrf.py shared/tests/test_manage_rate_limit.py shared/tests/test_manage_fetch_wrapper.py shared/tests/test_tui_readonly.py shared/tests/test_reason_similarity.py shared/tests/test_instrument_normaliser.py shared/tests/test_tag_cluster_threshold.py shared/tests/test_prompt_registry.py shared/tests/test_memu_listener.py shared/tests/test_postmortem_service.py shared/tests/test_text_signal_extractor.py shared/tests/test_gateway.py shared/tests/test_gateway_dedup.py shared/intelligence/test_gateway_config.py shared/intelligence/test_loop_detector.py shared/intelligence/test_payload_store.py -v -k "asyncio or not trio"`

### Key Files Created / Modified

| File | Action | Purpose |
|------|--------|---------|
| [`shared/tests/test_env_validate.py`](shared/tests/test_env_validate.py) | Fixed | Phase 1 — `_validate_env.py` passes for openrouter + requesty |
| [`shared/tests/test_grep_guard.py`](shared/tests/test_grep_guard.py) | Fixed | Phase 1 — CI gate: no `os.getenv(OPENROUTER_API_KEY)` outside gateway_config |
| [`shared/tests/test_api_cost_log_precision.py`](shared/tests/test_api_cost_log_precision.py) | Fixed | Phase 1 — `cost_usd` Decimal precision (no float rounding) |
| [`shared/tests/test_api_cost_log_failed_calls.py`](shared/tests/test_api_cost_log_failed_calls.py) | Fixed | Phase 1 — failed calls log `success=false` + `http_status` |
| [`shared/tests/test_reason_freeze_trigger.py`](shared/tests/test_reason_freeze_trigger.py) | Created | Phase 2 — freeze trigger blocks reason updates after freeze, allows price/pnl/exit updates |
| [`shared/tests/test_position_postmortem_uniqueness.py`](shared/tests/test_position_postmortem_uniqueness.py) | Created | Phase 2 — composite UNIQUE `(position_id, postmortem_version, prompt_version)` |
| [`shared/tests/test_vision_model_resolved.py`](shared/tests/test_vision_model_resolved.py) | Fixed | Phase 3 — `vision_model_resolved` returned from gateway response |
| [`shared/tests/test_payload_retention.py`](shared/tests/test_payload_retention.py) | Existing | Phase 3 — retention sweep compresses old files, lock preserves archives |
| [`shared/tests/test_signal_review_export.py`](shared/tests/test_signal_review_export.py) | Existing | Phase 4 — CSV/HTML export, atomic symlink, thumbnail policy |
| [`shared/tests/test_signal_review_xss.py`](shared/tests/test_signal_review_xss.py) | Existing | Phase 4 — XSS escaping in HTML output |
| [`shared/tests/test_manage_panel_routes.py`](shared/tests/test_manage_panel_routes.py) | Existing | Phase 5 — manage/ returns 200, routes work |
| [`shared/tests/test_manage_panel_auth.py`](shared/tests/test_manage_panel_auth.py) | Existing | Phase 5 — OTP auth, default-deny |
| [`shared/tests/test_manage_csrf.py`](shared/tests/test_manage_csrf.py) | Existing | Phase 5 — CSRF token validation |
| [`shared/tests/test_manage_rate_limit.py`](shared/tests/test_manage_rate_limit.py) | Existing | Phase 5 — rate limiting per session |
| [`shared/tests/test_manage_fetch_wrapper.py`](shared/tests/test_manage_fetch_wrapper.py) | Existing | Phase 5 — JS fetch wrapper with CSRF |
| [`shared/tests/test_tui_readonly.py`](shared/tests/test_tui_readonly.py) | Existing | Phase 5 — TUI readonly gate blocks mutations |
| [`shared/tests/test_reason_similarity.py`](shared/tests/test_reason_similarity.py) | Existing | Phase 6 — semantic similarity >0.75 via sentence-transformers |
| [`shared/tests/test_instrument_normaliser.py`](shared/tests/test_instrument_normaliser.py) | Existing | Phase 6 — symbol/exchange normalisation |
| [`shared/tests/test_tag_cluster_threshold.py`](shared/tests/test_tag_cluster_threshold.py) | Existing | Phase 6 — tag cluster threshold + env override |
| [`shared/tests/test_prompt_registry.py`](shared/tests/test_prompt_registry.py) | Existing | Phase 6 — prompt version registry idempotency |
| [`shared/tests/test_memu_listener.py`](shared/tests/test_memu_listener.py) | Existing | Phase 7 — MemU listener backfill, skip-locked, success, failure retry |
| [`shared/tests/test_postmortem_service.py`](shared/tests/test_postmortem_service.py) | Existing | Phase 7 — PostMortemService tick, write postmortem, skip no-candles |
| [`shared/docs/INTELLIGENCE_UNIFIED_PLAN.md`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md) | Updated | All Phase 1–7 benchmark checkboxes marked `[x]` |

### Critical Fixes Applied
1. `test_env_validate.py` — added `monkeypatch.delenv("REQUESTY_API", raising=False)` + `TICKLES_APP_VISION_API_KEY` deletion so missing-key test actually raises `EnvValidationError`
2. `test_vision_model_resolved.py` — rewrote mock to use proper async context manager protocol (`__aenter__`/`__aexit__` on session + post response)
3. `test_position_postmortem_uniqueness.py` — uses random IDs to avoid `uq_position_dedup` collisions across test runs
4. `test_grep_guard.py` — falls back from `rg` to `grep` when ripgrep not installed
5. `test_api_cost_log_precision.py` — uses `Decimal("0.123456789012345678")` which genuinely loses precision in float64
6. `test_api_cost_log_failed_calls.py` — fixed tuple indexing from `call_args[0][1]` to `call_args[0][1:]`

### Remaining Unchecked Items (Phase 0 pre-kickoff blockers — outside scope)
- `git rev-parse HEAD` saved in baseline handoff
- `pg_dump --schema-only` saved for tickles_shared + one company DB
- `feature/intelligence-unified-plan` branch created and pushed
- CI workflow `.github/workflows/schema_drift.yml` registered
- Phase 0 handoff doc committed

### Next Recommended Work
1. **Phase 8** — ChartHackerOpinionService (opinion budget, agent opinions, confidence gating)
2. **Phase 9** — ZoneFilter + rate limiting (Discord/Telegram message classification)
3. **Phase 10** — Writer registry + actor_instance columns (surgeon2 cutover)
4. **Phase L** — Dashboard (chart renderer, anchors, service health)

Resume command: `Continue implementing Phase 8 (ChartHackerOpinionService) from the INTELLIGENCE_UNIFIED_PLAN.md benchmark checklist. Read the Phase 8 section starting around line 2156, then implement all unchecked benchmark items for that phase.`
