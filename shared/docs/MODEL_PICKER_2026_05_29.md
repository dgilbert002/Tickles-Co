# Model Picker & Gateway Selector — Roadmap (2026-05-29)

**Owner:** project owner · **Builder:** AI dev · **Status:** IN PROGRESS

This is the single source of truth for the "choose provider + model per slot in
Settings" feature. If we ever need to roll back, read the **Rollback** section
of each phase — every change is listed with how to undo it.

---

## 1. Plain-English goal (explain-like-I'm-21)

Right now the app reads charts with an AI "vision" model. Which model it uses is
half-controlled by the database and half-controlled by a messy `.env` file, and
you can only pick from a short hand-typed list. You can't choose the *provider*
(OpenRouter vs Requesty) from the UI, and you can't see your Requesty routing
**policies** (like `policy/tickles-vision`) at all.

We're building a proper **Settings → Models** panel where, for every place the
app calls an LLM (vision + text), you can:

1. Pick the **provider**: OpenRouter, Requesty, or "any".
2. Scroll a **live list** of models pulled straight from each provider's API.
3. See your **custom policies pinned at the top**, then the models below.
4. See each row's **provider, context size, and $ cost per 1M tokens**.
5. Get **vision-only filtering** on vision slots; text slots show text+vision.
6. Have your choice **saved in the database** and remembered across restarts.

Your API keys already work. The `.env` only needs the keys; everything else is
pulled live and cached in the DB.

---

## 2. How the system works TODAY (so we know what we're changing)

There are **two independent layers** that together decide which model runs:

### Layer 1 — Provider (endpoint + key)
`shared/intelligence/gateway_config.py` → `GatewayConfig.for_service(name)` reads:
- `LLM_GATEWAY_<SERVICE>` → `LLM_GATEWAY_DEFAULT` → `"openrouter"`.
- For `requesty`: base URL `router.requesty.ai/v1`, key from
  `REQUESTY_API_KEY` → `REQUESTY_API` → `TICKLES_APP_VISION_API_KEY`.
- For `openrouter`: `OPENROUTER_API_KEY`, `OPENROUTER_BASE_URL`.

This ONLY decides *where* the call goes + which key signs it.

### Layer 2 — The model string
The InterpretationService does NOT use `gateway_cfg.default_model`. It calls
`shared/intelligence/model_config.py` → `get_model(SLOT_PRIMARY/FALLBACK)`,
resolved as: **DB row in `public.system_config` (namespace `chart_hacker`,
key `model.primary`/`model.fallback`/`model.prefilter`) → env
`CHART_HACKER_MODEL_*` → code default (`qwen/qwen3-vl-32b-instruct`)**. Whatever
that resolves to is sent verbatim as the `"model"` field in the HTTP payload.

Text services (postmortem, guru, MCP, text-extract) instead use
`gateway_cfg.default_model` (env-driven). So model selection is **inconsistent**
between vision and text — we will unify this.

### The Settings panel today
- `shared/dashboard/settings_routes.py` — REST endpoints for vision-model
  get/set/test/history (+ dedup, copy-sizing, sources, prompts).
- `shared/intelligence/model_config.py` — 3 fixed slots, DB→env→default,
  60s cache, allow-list from a **static** `vision_model_catalogue.json`.
- The static JSON is why `policy/tickles-vision` is not selectable and why the
  list goes stale.

---

## 3. Audit findings (2026-05-29) — the "Qwen mystery" + bugs

Verified against the live DB + provider APIs:

1. **Qwen IS being used.** `api_cost_log` shows `qwen/qwen3-vl-32b-instruct`,
   role=interpretation, **494 calls, last today 10:37**. It's been primary
   since the dashboard dropdown set the DB row on 2026-05-24. "Not used in 34
   days" was a **dashboard display gap**, not reality.
2. **BUG — `signal_interpretations.vision_provider` is NULL on every row.**
   `interpretation_service.py` never writes that column (grep: 0 matches), even
   though `api_cost_log.provider` is recorded correctly. Any UI reading
   `vision_provider` shows blank. → **Fix in M1.**
3. **Fallback fires more than primary.** Sonnet fallback
   (`anthropic/claude-4-sonnet-20250522`) had **2130** interpretation calls vs
   qwen's 494 in the same window — i.e. **qwen primary is failing/timing out
   and silently falling back to Sonnet a lot**, inflating cost. → flag for a
   post-picker investigation.
4. **Requesty has NEVER run in production.** Every cost-log row is
   `provider='openrouter'`. The cutover to `policy/tickles-vision` is a first.
5. **`OPENROUTER_DEFAULT_MODEL` / `REQUESTY_DEFAULT_MODEL` are NO-OPs.** No code
   reads them (only docs/`.env.template`). The owner's earlier edit of
   `OPENROUTER_DEFAULT_MODEL=...sonnet-4.6` had no effect on the vision model.
6. **`.env` bugs (fixed in M0):** empty `REQUESTY_API_KEY=` shadowed
   `REQUESTY_API` (would break Requesty auth); corrupted `#6`/`CC` lines.

### Provider API facts (probed live)
- **OpenRouter** `GET /api/v1/models` (no key) → 357 models. Fields:
  `id`, `name`, `context_length`, `architecture.input_modalities`
  (contains `"image"` ⇒ vision), `pricing.prompt`/`pricing.completion`
  (per-token USD strings).
- **Requesty** `GET /v1/models` (Bearer key) → models **and policies in one
  list**. Policies have `id` starting `policy/`, `owned_by:"system"`,
  `supports_vision`, `context_window`, `max_output_tokens`. Policy prices are
  `0` (real cost depends on the underlying model chosen at inference).
  There is **no** separate policies endpoint (all variants 404).

---

## 4. Owner-approved decisions (2026-05-29)

- **Scope:** ALL LLM slots — the 3 vision slots (primary/fallback/prefilter)
  PLUS text services (postmortem, guru, MCP, text-extract).
- **Sync cadence:** on startup + manual Refresh button + once daily.
- **`.env`:** clean it now (backed up first). ✅ done in M0.
- **First cutover:** vision PRIMARY → Requesty `policy/tickles-vision`.

---

## 5. Phases

### Phase M0 — `.env` hygiene + audit ✅ COMPLETE
- **What:** backed up `.env`; commented the empty `REQUESTY_API_KEY=`; fixed
  corrupted `#6`/`CC` lines; ran the audit above.
- **Files:** `.env` (edited), backup at
  `shared/reports/model_picker_2026_05_29/backup/.env.<ts>.bak`.
- **Rollback:** `cp` the backup back over `.env`.

### Phase M1 — Per-slot {provider, model} persistence  ✅ COMPLETE (2026-05-29)
- **What shipped:**
  - `model_config.py`: added `SLOT_REGISTRY` (8 slots: vision primary/fallback/
    prefilter + text postmortem/guru/mcp/text_extract/chart_hacker_opinion),
    `get_slot()`, `set_slot()`, `get_all_slots_v2()`, `list_slots()`,
    `slot_is_vision()`, `invalidate_slot_cache()`. Storage: namespace
    `model_slots`, key=`<slot>`, value JSON `{provider, model}`; vision slots
    also mirror the model into the legacy `chart_hacker/model.<slot>` row.
    All legacy functions (`get_model`, `set_model`, `get_all_slots`) untouched.
  - `gateway_config.py`: added `GatewayConfig.for_provider(provider, service)`
    (explicit provider, bypasses `LLM_GATEWAY_*`) + `resolve_slot_gateway(slot)`
    → `(GatewayConfig, model)`. Also hardened the empty-`REQUESTY_API_KEY`
    gotcha in the requesty branch.
  - `interpretation_service.py`: primary/fallback now resolve a gateway PER
    SLOT (so primary=Requesty + fallback=OpenRouter works); prefilter too.
    Added `LlmResult.provider`; INSERT now writes `vision_provider` ($49) —
    **fixes bug #2 (blank provider)**.
  - `postmortem_service.py`, `text_signal_extractor.py`,
    `chart_hacker_opinion_service.py`: now resolve their slot via
    `resolve_slot_gateway(...)` (postmortem keeps an explicit `--model`
    override and strips a legacy `openrouter/` prefix).
- **Cutover:** vision `primary` slot set to `requesty` / `policy/tickles-vision`
  (fallback=OpenRouter sonnet-4, prefilter=OpenRouter gemini-2.5-flash).
  Verified live: 12:25:22 UTC daemon call logged `provider='requesty'`, policy
  routed to `gemini-3-flash-preview`. (NOTE: operator expected Sonnet-4.6 first —
  that's a Requesty-side policy ORDER setting, adjustable in the Requesty UI.)
- **STILL PENDING:** wire `guru` + `mcp` services to `resolve_slot_gateway`
  (registry entries exist; services not yet swapped — lower traffic).
- **Rollback:** `set_slot('primary','openrouter','qwen/qwen3-vl-32b-instruct')`
  or delete the `model_slots`/`primary` row; restart `tickles-interpretation`.
  Revert code via git.

### Phase M2 — Live catalogue sync  ✅ COMPLETE (2026-05-29)
- Migration `migrations/2026_05_29_model_catalogue.sql` (+ ROLLBACK): new
  `public.model_catalogue` (models + policies, `is_policy`/`is_vision` flags,
  context, in/out $/Mtok, raw JSONB, `synced_at`); also RELAXED the Round-10
  `model_config_audit_slot_chk` CHECK to allow the new text slots.
- `shared/intelligence/model_catalogue_sync.py`: `fetch_openrouter()` (no key),
  `fetch_requesty()` (key), `sync_all()` (upsert + prune-stale, per-provider
  error isolation), `list_for_picker()` (grouped policies+models, vision filter),
  `catalogue_count()`. First sync: **357 OpenRouter + 505 Requesty = 862 rows.**
- Pricing normalised to $/1M tokens; policy "$0" placeholders → NULL ("cost
  varies"). Vision = OpenRouter `input_modalities∋image` / Requesty
  `supports_vision`. Policies = Requesty id starts `policy/`.
- **Rollback:** run the `_ROLLBACK.sql` (drops table, restores CHECK).

### Phase M3 — Settings API upgrade  ✅ COMPLETE (2026-05-29)
- `shared/dashboard/settings_routes.py`:
  - `GET  /api/settings/slots` → all 8 slots + resolved provider/model/sources.
  - `GET  /api/settings/catalogue?slot=&provider=&kind=` → grouped
    `{policies, models}` filtered by the slot's vision kind, with provider +
    context + in/out $/Mtok per row.
  - `POST /api/settings/slot` → `{slot, provider, model}` (validated).
  - `POST /api/settings/catalogue/refresh` → immediate re-sync.
- `shared/dashboard/server.py`: `cleanup_ctx` task syncs the catalogue on
  startup + every `MODEL_CATALOGUE_SYNC_INTERVAL_S` (default 24h).
- Dashboard runs on **port 3101**, exposed at `…/dashboard/` via Tailscale.

### Phase M4 — Settings UI  ✅ COMPLETE (2026-05-29)
- `web/index.html`: header reworded; cache-bust `app.css?v=…-model-picker2`,
  `app.js?v=27-model-picker2`.
- `static/app.js`: `renderModelPicker()` + helpers replace the old 3 dropdowns.
  Per slot: provider filter (All/OpenRouter/Requesty), live search, scrollable
  list with **routing policies pinned on top**, then models; each row shows
  name · provider badge · context · in/out $/Mtok; vision slots show vision +
  policies only, text slots show everything. Selecting POSTs to
  `/api/settings/slot`; a global "Refresh model list" button re-syncs.
- `static/app.css`: `.mp-*` styles (dark theme); `.mp-grid` 2-col responsive.
- Verified in-browser: all 8 slots render, primary shows
  `REQUESTY policy/tickles-vision`, vision vs text filtering correct.

### Phase M5 — Tests  ✅ COMPLETE (2026-05-29)
- `shared/tests/test_model_catalogue_sync.py` — 15 tests (pricing normaliser,
  OpenRouter + Requesty normalisers with sample payloads, slot-registry
  integrity, vision flags, validation). All pass; no regressions in the
  Round-10 picker / gateway / vision-resolved suites (43 pass).

### Still pending
- **M1.b** — wire `guru` + `mcp` services to `resolve_slot_gateway` (registry
  entries + UI already exist; their service code still uses the legacy path).
- Optional: investigate the qwen-primary→sonnet-fallback rate (fallback fired
  2130 vs primary 494) once the dust settles.

---

## 6. Global rollback
1. `cp shared/reports/model_picker_2026_05_29/backup/.env.<ts>.bak .env`
2. `git revert`/checkout the feature commits.
3. `DROP TABLE IF EXISTS public.model_catalogue;`
4. Delete `system_config` rows under the new slot namespace (slots revert to
   env/default).
5. Restart `tickles-interpretation`, `tickles-dashboard`.
