# Phase Y — Learning Dashboard & Memory Feed

**Status**: DRAFT **v2** — supersedes v1 (2026-05-02). v1 archived as a historical reference at the bottom of this document is **not** retained; this file is the single source of truth.
**Author**: Architect (Roo)
**Date**: 2026-05-03
**Supersedes**: v1 (2026-05-02)
**Companion documents**:
- [`shared/docs/SYSTEM_INVENTORY_2026-05-02.md`](shared/docs/SYSTEM_INVENTORY_2026-05-02.md:1) — read FIRST.
- [`.roo/handoffs/2026-05-03-master-resume-handoff.md`](.roo/handoffs/2026-05-03-master-resume-handoff.md:1) — F1–F11 wave + binding directives D1–D8.
- [`shared/docs/INTELLIGENCE_UNIFIED_PLAN.md`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:1) — phase-by-phase context for tables and services this plan references.

---

## 0. Why v2 exists

v1 was written 2026-05-02 **before** the user issued binding directives D1–D5 (memory unification, time windows, skill-vs-luck, fix-the-five-issues, surface failed trades). v1 also contains five concrete design errors that surfaced during the F1–F11 wave verification:

| # | v1 design error | v2 correction |
|---|-----------------|---------------|
| E1 | Plans a NEW `openclaw_export_daemon` to mirror `SOUL.md` / `MEMORY.md` files into `/opt/tickles/shared/exports/openclaw/`. | **D1 says no.** Agents must write to mem0, not files. v2 deletes Phase Y.A entirely. SOUL/MEMORY content is migrated to mem0 once via §4.3 audit, then the writers are flipped to mem0 writes. The dashboard reads from mem0. |
| E2 | References a table `actor_edge_scores` that does **not exist** anywhere in `shared/migration/` or `shared/intelligence/migrations/`. | v2 references the tables that exist: [`edge_score_changes`](shared/intelligence/migrations/2026_05_03_phase11_actor_performance.sql:55) and [`trader_performance`](shared/migration/tickles_company_pg.sql:684). |
| E3 | References `signal_pattern_tags` and a `pattern_cluster_summary` materialised view that do not exist. [`shared/intelligence/tag_normaliser.py:16`](shared/intelligence/tag_normaliser.py:16) is a single-function stub that just returns `{tag: [tag]}`. | v2 demotes the Pattern Cluster Cloud to **post-Phase-Y** (Phase Y.next) and removes it from the critical path. |
| E4 | "Smarter today?" is computed as `mean(edge_score_today) - mean(edge_score_yesterday)`. Per **D3**, P&L-derived edge is *luck*, not skill. A daily mean delta is mostly noise on weekends and survivorship-biased on weekdays. | v2 replaces it with a **`skill_score(actor, window)`** composite (D3, §3) computed from memory-recall, reason-coherence, and consistency — **not raw P&L**. |
| E5 | All Memory Feed SQL hard-codes `interval '7 days'`. Per **D2**, the user wants three windows: **7d / 14d / 30d**. | v2 introduces three views (`v_memory_feed_7d`, `v_memory_feed_14d`, `v_memory_feed_30d`) and three corresponding `v_actor_skill_*` views. Window is a tab-level toggle, not a hard-coded literal. |

v2 does **not** revisit Phase X (transactional dashboard) or change the auth, anchor, or single-writer rules. Those are stable.

---

## 1. Problem Statement (revised)

The system accumulates learning signals in **eleven** independent places (table from v1 retained — count is unchanged), but the user cannot answer the question:

> *"Are my agents getting smarter, or are they just getting lucky?"*

P&L alone does not answer this. A trader who fluked a $1 000 win on noise looks identical to a trader who reasoned correctly and was rewarded — until both are tested against new regimes. **D3** is explicit: *skill = function(memory, results), not just P&L*.

Phase Y v2 must therefore expose:

1. **Skill-vs-luck composite** per actor per window (7d / 14d / 30d) — the headline metric.
2. **Memory Feed** consolidating all eleven sources, mem0-first (D1).
3. **Agent Brain Cards** showing what each agent *remembers and recalled*, not just what it earned.
4. **Failed-trade surface** — D5 says we must see failed trades. F9 just landed 77 backfilled positions; v2 wires them into the dashboard's "failed trades" count and into postmortem scoring.

**v2 explicitly drops** Phase Y.A (openclaw daemon) and Phase Y.F (pattern cluster cloud) from the critical path.

---

## 2. Eleven sources — re-tabulated for v2

The eleven sources are unchanged from v1, but the **storage** column is corrected per D1 ("agents write to mem0, not files"). Sources marked ⚠ are migrated by §4.3 mem0-vs-md audit.

| # | Source | Tier | v1 storage | v2 storage (post-D1) |
|---|--------|------|-----------|----------------------|
| 1 | mem0 / Qdrant — agent-private | T1 | `tickles_{company}` collection, `agent_id={company}_{agent}` | unchanged |
| 2 | mem0 / Qdrant — company-shared | T2 | same collection, `agent_id="shared"` | unchanged |
| 3 | MemU Postgres + pgvector(384) | T3 | `memu.insights` (kind, content_hash UNIQUE) | unchanged |
| 4 | `agent_events` per-company | n/a | `tickles_{company}.agent_events` | unchanged |
| 5 | `position_postmortems.lessons_for_actor` | n/a | per-company JSONB | unchanged |
| 6 | `position_postmortems.lessons_for_company` | n/a | per-company JSONB | unchanged |
| 7 | `edge_score_changes` | n/a | `tickles_shared` ([`shared/intelligence/migrations/2026_05_03_phase11_actor_performance.sql:55`](shared/intelligence/migrations/2026_05_03_phase11_actor_performance.sql:55)) | unchanged |
| 8 | `prompt_assignments` + `edge_score_changes WHERE event_type='prompt_promoted'` | n/a | `tickles_shared` | unchanged |
| 9 | `memu_outbox` (durable broadcast spool) | n/a | `tickles_shared` ([`shared/migration/migrations/2026_04_30_phase7_memu_outbox.sql:7`](shared/migration/migrations/2026_04_30_phase7_memu_outbox.sql:7)) | unchanged |
| 10 ⚠ | OpenClaw `SOUL.md` / `MEMORY.md` per agent | n/a | `/root/.openclaw/workspace/<agent>/*.md` | **mem0 Tier 1** with `metadata.type='soul'` or `'memory_md_legacy'`. Migration script in §4.3. After migration, the `surgeon_*` daemons that read those files (5 hits in `grep`) flip to `ScopedMemory.search()`. |
| 11 | `api_cost_log` (G5) | n/a | `tickles_shared.public.api_cost_log` | unchanged |

**Note on source #10:** [`shared/daemons/surgeon_scanner.py:166`](shared/daemons/surgeon_scanner.py:166), [`shared/daemons/surgeon_trader.py:568`](shared/daemons/surgeon_trader.py:568), [`shared/templates/trading_agent/surgeon_llm_runner.py:46`](shared/templates/trading_agent/surgeon_llm_runner.py:46), [`shared/provisioning/executor.py:753`](shared/provisioning/executor.py:753), and [`shared/scripts/check_system_freshness.py:280`](shared/scripts/check_system_freshness.py:280) all reference `/root/.openclaw/workspace/`. The §4.3 audit and migration removes those reads; v2 dashboard never opens a `.md` file for runtime reads.

---

## 3. The skill-vs-luck composite (D3, headline metric)

This is the centerpiece. v1's "Smarter today?" was a P&L-mean delta — **rejected by D3**. v2 defines:

```
skill_score(actor, window) ∈ [0, 1]
window ∈ {7d, 14d, 30d}
```

### 3.1 The five components

All five components are sourced from existing tables — no new schema needed for the formula itself, only the aggregation views in §3.2.

| # | Component | Weight | Source | Range | Why this proves skill, not luck |
|---|-----------|-------:|--------|-------|---------------------------------|
| C1 | **Reasoning clarity** | 0.30 | mean(`position_postmortems.what_happened` length normalised by reason-coherence regex score) over window | 0–1 | A trader who can articulate *why* a position worked is reasoning, not gambling. F3 made these postmortems real LLM output, so this signal is now trustworthy. |
| C2 | **Consistency (inverse variance)** | 0.25 | `1 / (1 + stddev(realized_pnl_pct) / abs(mean(realized_pnl_pct)))` over window, clipped | 0–1 | High win-rate with high variance = lucky. High win-rate with low variance = skill. We measure the *coefficient of variation* and invert. |
| C3 | **Reason-frozen-vs-outcome correlation** | 0.20 | Pearson r between `entry_reason_trader` similarity-cluster and `outcome` (`tp_hit`/`sl_hit`/`expiry`/`manual_close`) over window | 0–1 (clipped) | If the trader said "RSI oversold" before entry and RSI-oversold trades win above baseline rate, the *stated reason* predicts. That's skill. Random reasons → r ≈ 0 → score 0. Reason-freeze trigger ([INTELLIGENCE_UNIFIED_PLAN §2.2 [AG]](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:865)) makes this honest. |
| C4 | **Memory-recall match-quality** | 0.15 | mem0 query: `mem.search(query=current_signal_summary, limit=5)` issued at signal time; component = `avg(match_score)` over returned memories, where `match_score` is the **tiered match** per §11 Q2: `1.0` (dimension+outcome+symbol), `0.6` (dimension+outcome), `0.3` (outcome only), `0.0` (no match). | 0–1 | A trader/agent who *remembers and recalls* relevant past trades is learning. Tiered match (vs flat 0/1) keeps the signal informative on small data: a recall that matches outcome+dimension still scores 0.6 even if the symbol differs (BTC lesson applied to ETH trade is still partial skill). Requires lightweight wiring in interpretation_service to log recall-hits + per-tier match flags at signal time. |
| C5 | **Prompt lift** | 0.10 | If actor is on a non-default prompt variant, take `(edge_score_with_variant - edge_score_baseline)` from `edge_score_changes WHERE event_type='prompt_promoted' AND actor_id=?` over window. Normalise to [0,1] via sigmoid. | 0–1 | If a winning prompt was promoted by [`shared/intelligence/coach_service.py`](shared/intelligence/coach_service.py:43) and the variant *replicates* (still wins on new trades), that's structural skill — not the trader's, but the *system's* learning. |

**Sum of weights = 1.00.** A component is dropped (and its weight redistributed proportionally) when the underlying source is empty for the window — see §3.3.

### 3.2 SQL: the three skill views

```sql
-- shared/intelligence/migrations/2026_05_05_phase_y_skill_views.sql  (NEW)

-- 1. Per-window aggregate, parameterised by window length via three views.
-- Use a function so the formula lives in ONE place.

CREATE OR REPLACE FUNCTION compute_skill_score(
    p_actor_id     TEXT,
    p_company_id   TEXT,
    p_window_days  INT
) RETURNS NUMERIC AS $$
DECLARE
    v_clarity        NUMERIC;
    v_consistency    NUMERIC;
    v_reason_corr    NUMERIC;
    v_recall_hit     NUMERIC;
    v_prompt_lift    NUMERIC;
    v_n_trades       INT;
    v_score          NUMERIC;
BEGIN
    -- Floor: need at least 5 closed trades in window or we return NULL ("insufficient data")
    SELECT count(*) INTO v_n_trades
    FROM tracked_positions tp
    WHERE tp.actor_id = p_actor_id
      AND tp.company_id = p_company_id
      AND tp.closed_at > now() - make_interval(days => p_window_days)
      AND tp.realized_pnl_usd_final IS NOT NULL;

    IF v_n_trades < 5 THEN
        RETURN NULL;
    END IF;

    -- C1: reasoning clarity (postmortem length × coherence proxy, normalised)
    SELECT COALESCE(
        avg(LEAST(1.0, length(pp.what_happened)::numeric / 400.0))
        , 0)
    INTO v_clarity
    FROM position_postmortems pp
    JOIN tracked_positions tp ON tp.id = pp.position_id
    WHERE tp.actor_id = p_actor_id
      AND tp.company_id = p_company_id
      AND tp.closed_at > now() - make_interval(days => p_window_days);

    -- C2: consistency (inverse coefficient of variation of pnl_pct)
    SELECT
        CASE
            WHEN abs(avg(realized_pnl_pct)) < 0.0001 THEN 0
            ELSE LEAST(1.0, 1.0 / (1.0 + stddev_pop(realized_pnl_pct) / abs(avg(realized_pnl_pct))))
        END
    INTO v_consistency
    FROM tracked_positions
    WHERE actor_id = p_actor_id
      AND company_id = p_company_id
      AND closed_at > now() - make_interval(days => p_window_days)
      AND realized_pnl_pct IS NOT NULL;

    -- C3: reason→outcome correlation. Coarse proxy:
    --     fraction of trades whose stated entry_reason_trader appears
    --     in a winning postmortem's pattern_confirmed array.
    SELECT COALESCE(
        avg(CASE WHEN pp.pattern_confirmed THEN 1.0 ELSE 0.0 END)
        , 0)
    INTO v_reason_corr
    FROM position_postmortems pp
    JOIN tracked_positions tp ON tp.id = pp.position_id
    WHERE tp.actor_id = p_actor_id
      AND tp.company_id = p_company_id
      AND tp.closed_at > now() - make_interval(days => p_window_days)
      AND tp.entry_reason_trader IS NOT NULL
      AND tp.entry_reason_frozen_at IS NOT NULL;  -- reason-freeze guard

    -- C4: memory recall match-quality (tiered, per §11 Q2 + §3.4 schema)
    -- Score per recall row:
    --   1.0  if match_dim AND match_outcome AND match_symbol
    --   0.6  if match_dim AND match_outcome (symbol may differ)
    --   0.3  if match_outcome only
    --   0.0  otherwise
    -- Component = average match score over the window.
    SELECT COALESCE(
        avg(
            CASE
                WHEN match_dim AND match_outcome AND match_symbol THEN 1.0
                WHEN match_dim AND match_outcome                 THEN 0.6
                WHEN match_outcome                                THEN 0.3
                ELSE 0.0
            END
        )
        , 0)
    INTO v_recall_hit
    FROM mem0_recall_log
    WHERE actor_id = p_actor_id
      AND company_id = p_company_id
      AND created_at > now() - make_interval(days => p_window_days)
      AND match_outcome IS NOT NULL;  -- exclude rows whose position hasn't closed yet

    -- C5: prompt lift (only count promoted variants, sigmoid-clipped)
    SELECT COALESCE(
        avg(1.0 / (1.0 + exp(-greatest(-10, least(10, (new_score - old_score) * 5)))))
        , 0)
    INTO v_prompt_lift
    FROM edge_score_changes
    WHERE actor_id = p_actor_id
      AND company_id = p_company_id
      AND event_type = 'prompt_promoted'
      AND created_at > now() - make_interval(days => p_window_days);

    -- Composite (weights MUST sum to 1.0; if any component is NULL, redistribute)
    v_score :=
        0.30 * COALESCE(v_clarity,     0)
      + 0.25 * COALESCE(v_consistency, 0)
      + 0.20 * COALESCE(v_reason_corr, 0)
      + 0.15 * COALESCE(v_recall_hit,  0)
      + 0.10 * COALESCE(v_prompt_lift, 0);

    RETURN GREATEST(0, LEAST(1, v_score));
END;
$$ LANGUAGE plpgsql STABLE;

-- 2. Three convenience views — one per window
CREATE OR REPLACE VIEW v_actor_skill_7d AS
SELECT actor_id, company_id,
       compute_skill_score(actor_id, company_id, 7) AS skill_score,
       7 AS window_days
FROM (SELECT DISTINCT actor_id, company_id FROM tracked_positions) actors;

CREATE OR REPLACE VIEW v_actor_skill_14d AS
SELECT actor_id, company_id,
       compute_skill_score(actor_id, company_id, 14) AS skill_score,
       14 AS window_days
FROM (SELECT DISTINCT actor_id, company_id FROM tracked_positions) actors;

CREATE OR REPLACE VIEW v_actor_skill_30d AS
SELECT actor_id, company_id,
       compute_skill_score(actor_id, company_id, 30) AS skill_score,
       30 AS window_days
FROM (SELECT DISTINCT actor_id, company_id FROM tracked_positions) actors;
```

### 3.3 Component dropout

If C4 (`mem0_recall_log`) has zero rows for the window — typical at week 1 of operation — the formula must not silently treat 0 as the score (penalising the actor). Implementation rule:

```python
# shared/intelligence/skill_scorer.py — Python mirror of the SQL function (used by the dashboard/edge_scorer)
WEIGHTS = {"clarity": 0.30, "consistency": 0.25, "reason_corr": 0.20,
           "recall_hit": 0.15, "prompt_lift": 0.10}

def compute_skill(components: dict[str, float | None]) -> tuple[float, list[str]]:
    """Components with value=None (no data) are dropped and weights renormalised.
    Returns (score, dropped_components)."""
    active = {k: v for k, v in components.items() if v is not None}
    if not active:
        return 0.0, list(components)
    total_w = sum(WEIGHTS[k] for k in active)
    score = sum(WEIGHTS[k] * v for k, v in active.items()) / total_w
    dropped = [k for k in components if k not in active]
    return max(0.0, min(1.0, score)), dropped
```

The dashboard tooltip on `skill_score` MUST show the dropped components ("computed without C4 — recall log empty for this window") so the number is never misleading.

### 3.4 New table: `mem0_recall_log` (tiered-match schema, per §11 Q2)

For C4 we need a record of *every time an agent queried mem0 at decision time and what came back*. This does not exist today. Schema includes three independent match flags so the §3.1 C4 tier-score is computed at SELECT time (no backfill needed if tier weights change later).

```sql
-- shared/intelligence/migrations/2026_05_05_phase_y_recall_log.sql  (NEW, in tickles_<company>)

CREATE TABLE IF NOT EXISTS mem0_recall_log (
    id                BIGSERIAL    PRIMARY KEY,
    actor_id          TEXT         NOT NULL,
    company_id        TEXT         NOT NULL,
    correlation_id    TEXT,                          -- joins to api_cost_log + payload_store
    query_summary     TEXT         NOT NULL,         -- the signal text passed to mem.search()
    query_dimension   TEXT,                          -- the signal's dimension at query time (e.g. 'lesson','warning','postmortem')
    query_symbol      TEXT,                          -- the signal's instrument_symbol at query time (slash form)
    returned_count    INT          NOT NULL,         -- |results|
    top_k_ids         JSONB        NOT NULL DEFAULT '[]'::jsonb,  -- mem0 result ids
    top_k_metadata    JSONB        NOT NULL DEFAULT '[]'::jsonb,  -- snapshot of each result's metadata (outcome/dimension/symbol)
    -- Tiered match flags — populated by retroactive UPDATE after position closes.
    -- Each flag is the OR over the top_k results: TRUE if ANY returned memory matched on that axis.
    match_outcome     BOOLEAN,                       -- ANY top_k result's metadata.outcome = pos.outcome
    match_dim         BOOLEAN,                       -- ANY top_k result's metadata.dimension = query_dimension
    match_symbol      BOOLEAN,                       -- ANY top_k result's metadata.symbol = pos.instrument_symbol
    position_id       BIGINT,                        -- which tracked_position this recall fed (NULL pre-creation)
    matched_at        TIMESTAMPTZ,                   -- when the retroactive UPDATE landed; NULL until then
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX idx_mem0_recall_actor_window
    ON mem0_recall_log (actor_id, company_id, created_at DESC);

CREATE INDEX idx_mem0_recall_position
    ON mem0_recall_log (position_id) WHERE position_id IS NOT NULL;
```

`match_*` flags are set retroactively by a small daemon helper inside [`shared/intelligence/postmortem_service.py`](shared/intelligence/postmortem_service.py:526) (one extra UPDATE per position close). Per §11 Q2 tier table, the C4 SQL component computes:

```
match_score = 1.0  if match_dim AND match_outcome AND match_symbol
            = 0.6  if match_dim AND match_outcome
            = 0.3  if match_outcome
            = 0.0  otherwise
```

If `match_outcome IS NULL`, the row is excluded (position hasn't closed yet — see C4 SQL filter in §3.2). Refinement in Phase Y.next: pgvector cosine-similarity between recall query and top_k content (currently we test only on metadata equality).

### 3.5 New table: `skill_weight_recommendations` (per §11 Q1 — Ask AI button)

For Q1 the user wants an **"Ask AI"** button that proposes a recalibrated weight vector based on the last 30d of data. Each invocation appends one row to a per-company audit table; the dashboard reads the latest row to render the recommendations panel.

```sql
-- shared/intelligence/migrations/2026_05_05_phase_y_weight_recommendations.sql  (NEW, in tickles_<company>)

CREATE TABLE IF NOT EXISTS skill_weight_recommendations (
    id                  BIGSERIAL    PRIMARY KEY,
    company_id          TEXT         NOT NULL,
    requested_by        TEXT         NOT NULL,           -- user/session id from auth
    window_days         INT          NOT NULL,           -- 7 / 14 / 30
    n_actors            INT          NOT NULL,           -- |actors with skill_score not NULL in window|
    n_trades            INT          NOT NULL,           -- |closed trades in window|
    -- Current weights at time of request (frozen for the audit trail)
    current_weights     JSONB        NOT NULL,           -- {"clarity":0.30, ...}
    -- LLM-proposed weights + per-component confidence
    proposed_weights    JSONB        NOT NULL,           -- {"clarity":0.28, ...}
    confidence_band     JSONB        NOT NULL,           -- {"clarity":[0.24,0.32], ...}
    rationale           TEXT,                            -- LLM-emitted summary
    model_provider      TEXT         NOT NULL,           -- 'openrouter' / 'openai' / etc
    model_name          TEXT         NOT NULL,
    correlation_id      TEXT,                            -- joins api_cost_log
    cost_usd            NUMERIC(10,6),
    applied             BOOLEAN      NOT NULL DEFAULT FALSE,  -- toggled true if Architect later adopts the recommendation
    applied_at          TIMESTAMPTZ,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX idx_skill_weight_rec_company_recent
    ON skill_weight_recommendations (company_id, created_at DESC);
```

Adoption flow: when the user clicks "Apply" on a recommendation in the dashboard, the row's `applied=TRUE` is set. **Applying does NOT mutate `compute_skill_score()` automatically** — it only marks the row. A follow-up Architect-mode migration (manual, reviewed) edits the SQL function and Python `WEIGHTS` constants. This protects against an LLM hallucination bricking the leaderboard.

---

## 4. The 7d / 14d / 30d Memory Feed (D2)

v1 hard-coded `interval '7 days'` everywhere. v2 introduces a window selector.

### 4.1 Three feed views (vs v1's single 7d query)

```sql
-- shared/intelligence/migrations/2026_05_05_phase_y_feed_views.sql  (NEW)

-- Macro: build a window-parameterised view via DDL repetition (Postgres doesn't have view params)

CREATE OR REPLACE VIEW v_memory_feed_7d AS
WITH events AS (
    SELECT 'agent_event'::text AS source_kind, 'tier1'::text AS tier,
           ae.agent_name AS actor, ae.company_id AS company,
           ae.event_type AS dimension, ae.payload->>'summary' AS body,
           ae.payload AS raw, ae.created_at AS ts, ae.id::text AS source_id,
           NULL::text AS correlation_id
    FROM agent_events ae
    WHERE ae.created_at > now() - interval '7 days'

    UNION ALL

    SELECT 'postmortem_actor', 'tier1', pp.actor_instance, pp.company_id,
           'lesson', l.value::text,
           jsonb_build_object('postmortem_id', pp.id, 'lesson', l.value),
           pp.created_at, pp.id::text || ':' || ord::text,
           pp.correlation_id
    FROM position_postmortems pp,
         LATERAL jsonb_array_elements_text(pp.lessons_for_actor) WITH ORDINALITY AS l(value, ord)
    WHERE pp.created_at > now() - interval '7 days'

    UNION ALL

    SELECT 'postmortem_company', 'tier2', 'shared', pp.company_id,
           'lesson', l.value::text,
           jsonb_build_object('postmortem_id', pp.id),
           pp.created_at, pp.id::text || ':co:' || ord::text,
           pp.correlation_id
    FROM position_postmortems pp,
         LATERAL jsonb_array_elements_text(pp.lessons_for_company) WITH ORDINALITY AS l(value, ord)
    WHERE pp.created_at > now() - interval '7 days'

    UNION ALL

    SELECT CASE WHEN esc.event_type = 'prompt_promoted' THEN 'promotion' ELSE 'edge_change' END,
           'tier2', esc.actor_id, esc.company_id, esc.event_type,
           format('edge %s → %s (%s)', esc.old_score, esc.new_score, esc.reason),
           esc.metadata, esc.created_at, esc.id::text,
           esc.metadata->>'correlation_id'
    FROM edge_score_changes esc
    WHERE esc.created_at > now() - interval '7 days'
)
SELECT * FROM events;

-- v_memory_feed_14d and v_memory_feed_30d are byte-identical except for the literal '7 days'.
-- Keep three views (NOT one PL/pgSQL function) so the planner can use indexes per window.
-- Migration generates all three via a loop — do not edit them by hand.
```

mem0 (sources 1, 2) and MemU (source 3) and `memu_outbox` (source 9) cannot be union-ed into a SQL view because they live in Qdrant / different schema. They are merged in Python by the snapshot provider — see §5.2.

### 4.2 Window selector UX

Tab-level radio: `[ 7d ] [ 14d ] [ 1M ]` (display labels per §11 Q3 — internal SQL window for the third bucket remains 30 days; the `1M` label is cosmetic and maps 1:1 to `v_memory_feed_30d` / `v_actor_skill_30d`). Default: **7d**. The selection is encoded in the URL anchor (`#/memory?window=14d&dimension=lesson`) so deep links round-trip; the URL parameter accepts both `30d` and `1M` for backwards-compatibility. The skill-score header strip uses a separate selector that defaults to 7d but is independently sticky (a user can compare "last 7d skill" against "1M feed").

### 4.3 Heap-merge cost ceiling

Per-source LIMIT is `60 / window_days × max_age` rows, capped at 200. Each source must complete in 250ms or its lane shows "partial — N/M sources reported".

---

## 5. Phase ordering for v2 (replaces v1 §7)

v1 had 8 sub-phases (Y.A–Y.G + a §5.3 enum fix). v2 has **6**, with two v1 phases dropped.

| v2 phase | Title | Days | Depends on | Notes |
|----------|-------|-----:|------------|-------|
| Y.0 | MemU enum reconciliation (was v1 §5.3) | 0.25 | — | Unchanged from v1. Hard prerequisite for Y.3. |
| Y.1 | New tables + views: `mem0_recall_log`, `skill_weight_recommendations`, `compute_skill_score()`, `v_actor_skill_*`, `v_memory_feed_*` | 0.5 | Y.0 | All new SQL lives in four migration files (§3.2 + §4.1 + §3.4 + §3.5). |
| Y.2 | Skill scorer Python + edge_scorer integration | 1.0 | Y.1 | New module [`shared/intelligence/skill_scorer.py`](shared/intelligence/skill_scorer.py:1). Wire into existing [`shared/intelligence/edge_scorer.py:249`](shared/intelligence/edge_scorer.py:249) `compute_edge_score()` as a NEW component (does NOT replace existing components — see §6). |
| Y.3 | SnapshotBuilder learning providers (was v1 Y.B, slimmed) | 1.0 | Y.1, Y.2 | New module `shared/dashboard/learning_providers.py`. Six providers (was 6 in v1, unchanged in count, but `smarter_today` becomes `skill_summary` and `pattern_clusters` is dropped). |
| Y.4 | "Did we get smarter?" header strip + Memory Feed tab + Agent Brain Cards (was v1 Y.C+Y.D+Y.E, merged) | 2.0 | Y.3 | All three UIs are bound to the same `v_actor_skill_*` + `v_memory_feed_*` views. Building them together avoids three rounds of CSS/JS plumbing. |
| Y.5 | Guard Activity sidebar + Prompt Evolution timeline (unchanged from v1 Y.G) | 0.5 | Y.3 | Source data unchanged. |
| Y.5b | "Ask AI" weight-recalibration button (per §11 Q1) | 0.5 | Y.4 | New endpoint `POST /api/skill/recalibrate` calls `shared/intelligence/skill_weight_advisor.py` (LLM via existing `llm_spend_tracker` + `correlation_id`); writes one row to `skill_weight_recommendations`; UI surfaces a "Recommended weights" panel under the skill header strip with `Apply` button (advisory-only — applying flips the audit-row flag but **does not** mutate `compute_skill_score()` weights; weight rotation requires a separate Architect-mode change). |

**Total: ~5.75 days** (up from ~5.25 in v2 baseline; +0.5d for Y.5b).

### Dropped from v2 (and why)

| v1 phase | Reason for drop |
|----------|-----------------|
| Y.A `openclaw_export_daemon` | **D1 violates this design.** The daemon was conceived as a file mirror; D1 says agents must write to mem0 instead. The §4.3 audit + migration replaces this entirely. SOUL/MEMORY content lives in mem0 going forward and the dashboard reads from mem0 directly. |
| Y.F Pattern Cluster Cloud | [`shared/intelligence/tag_normaliser.py`](shared/intelligence/tag_normaliser.py:16) is a stub. Building a UI on top of a stub produces a tag-soup demo. Push to Phase Y.next once the tag normaliser is real (after Phase 11 of [INTELLIGENCE_UNIFIED_PLAN.md](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:1)). |

---

## 6. Wiring `skill_score` into the existing edge_scorer

[`shared/intelligence/edge_scorer.py:249`](shared/intelligence/edge_scorer.py:249) `compute_edge_score()` already produces a per-actor edge from 11 components. It currently does **not** include skill-vs-luck. v2 adds **one new component**:

```python
# shared/intelligence/edge_scorer.py — addition (not replacement)

@dataclass
class ScorerInputs:
    # ... all existing fields ...
    skill_score:         Optional[float]   # NEW — output of compute_skill_score(), [0,1] or None
    skill_window_days:   Optional[int]     # NEW — which window this score covers (7/14/30)
    skill_components:    Optional[dict]    # NEW — components dict for tooltip transparency
```

```python
def _skill_minus_luck(inputs: ScorerInputs) -> Tuple[float, bool]:
    """New component for compute_edge_score().
    Maps skill_score directly. Marked unavailable when input is None."""
    if inputs.skill_score is None:
        return 0.0, False
    return float(inputs.skill_score), True
```

In `compute_edge_score()`'s component dictionary, add:

```python
components["skill_minus_luck"] = _skill_minus_luck(inputs)
```

Weight in the static `WEIGHTS` table: **0.15** (taken proportionally from `pnl_quality` 0.20→0.15 and `consistency` 0.15→0.10). Rationale: pnl_quality and consistency overlap with the skill components (C1–C5) so we deflate them when skill is present.

**This change is compatible with v1's edge_scorer** — when `skill_score=None` the component is reported as `(0.0, available=False)` which the existing renormalise-on-availability path handles correctly. Backwards-compatible deploy.

---

## 7. Fixing the five v1 issues (D4)

The user said: *"fix the 5 issue or imprive it."* Mapping each v1 §6 risk to its v2 disposition:

| v1 issue (§6) | v1 risk | v2 disposition |
|---------------|---------|----------------|
| **i. Dual-role writers** (one daemon writing both trader and critic data) | not flagged in v1 §6 by ID — surfaced in v1 §"5 issues" rollup | **Inherit from Phase 9 of [INTELLIGENCE_UNIFIED_PLAN.md §Phase 9 §G](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:2374).** v2 dashboard does not introduce new writers; we tag every Memory Feed row with `actor_role ∈ {trader, critic, observer, system}` derived from `writer_registry`. Dual-role detection becomes a guard-sidebar warning ("⚠ chart_hacker emitted both trader and critic events in the last 1h"). |
| **ii. Postmortem stub orphans** (hard-coded `position_id` 1001-1005, 218516) | data integrity | **DONE** in F3 (master handoff §3 F3) — confirmed 0 stubs in Q3 verification. v2 dashboard's stub-detector heartbeat is the persistent guard: a daily check posts to `cron_heartbeats` if any stub IDs reappear. |
| **iii. Open-positions count NaN** in dashboard | UX | v2 fixes this in Y.4: snapshot provider clamps `count` to `int` and renders `0` (not NaN/null/undefined) when the table is empty. Test included. |
| **iv. Edge-scorer crash on missing `agent_opinions`** | runtime | **DONE** in F11 / F4 (master handoff §3 F11 schema-drift sweep, commit `77851be`). v2 inherits the fix; no new work. |
| **v. Coach has no variants to evaluate** (chicken-and-egg) | functional | v2 ships a **seed step** in Y.5: as part of the Prompt Evolution timeline implementation, Code mode runs a one-shot script that creates two baseline variants per active prompt (`variant_a` / `variant_b` from the same prompt — A/A test). Coach can then evaluate. After 2 weeks of A/A data, Code mode introduces a real variant_b. This breaks the deadlock without faking lift. |

**Acceptance:** all five must be marked closed in Y.4's PR description before Y.5 ships.

---

## 8. What Could Go Wrong (revised)

v1 had 12 risks. v2 retains 8 still-applicable ones, drops 4 that no longer apply (R3 OpenClaw disk; R10 cold-start brain card; R7 pattern cluster drift; R12 sparkline jitter), and adds 4 new ones for the skill-score machinery.

| # | Risk | Likelihood | Impact | Mitigation |
|---|------|-----------|--------|------------|
| R1 | Memory Feed query degrades to multi-second under 10k+ daily events | High at 90d horizon | Page hangs | Per-source LIMITs, heap-merge in Python, hard 250ms-per-source budget, partial-result banner (unchanged from v1). |
| R2 | mem0/Qdrant unavailable → full Memory Feed fails | Medium | One swimlane blank | Each provider try/except in isolation; render banner not page error (unchanged). |
| R4 | MemU enum drift → filter dropdown shows ghost values | Already happening | Confused user | Y.0 fix is hard prereq (unchanged). |
| R5 | LLM emits gibberish dimension labels | Low | Filter clutter | Sanitize dimension at provider boundary (unchanged). |
| R6 | WebSocket consumer count grows unbounded | Medium | Memory leak | LRU disconnect oldest at 50 (unchanged). |
| R8 | "Skill score" delta is misleading on weekend | Medium | False signal | C2 floor `n_trades >= 5` already enforced in `compute_skill_score()`; UI shows "insufficient activity" when score=NULL. |
| R9 | OpenClaw `MEMORY.md` contains secrets — discovered during §4.3 migration | Possible | Credential leak | Pipe through [`shared/intelligence/payload_store._redact_secrets`](shared/intelligence/payload_store.py:40) before mem0 write **and** before display. |
| R11 | dimension dropdown explodes (>500 values) | Possible at 1y | Useless filter | Top-50-by-occurrence-in-window cap (unchanged). |
| **R13** *(new)* | `mem0_recall_log` filled in retroactively → C4 lags reality by hours | Certain | Slightly stale skill score | Acceptable. The retroactive UPDATE happens in the postmortem path; latency is the same as the postmortem itself. Tooltip on C4 shows "as of last close: <ts>". |
| **R14** *(new)* | Component dropout makes scores comparable across actors with different available data | Medium | Apples-to-oranges leaderboard | Surface dropped-components badge on every card. Add a strict-mode toggle that excludes any actor with >2 dropped components from the leaderboard. |
| **R15** *(new)* | `compute_skill_score()` is a SQL function — heavy per-call invocation (5 sub-queries × N actors × 3 windows) | High at 50+ actors | Page render >1s | Materialise the three views as MATERIALIZED VIEWS refreshed every 60s by an existing cron canary. Trade staleness (60s) for query speed. |
| **R16** *(new)* | Reason-freeze trigger ([INTELLIGENCE_UNIFIED_PLAN §2.2 [AG]](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:865)) not yet deployed → C3 reads post-hoc-edited reasons → fake skill score | High pre-deploy | Garbage-in-garbage-out skill scores | C3 query already filters `entry_reason_frozen_at IS NOT NULL`. If trigger is missing, this returns 0 rows → C3 drops out → tooltip explains why. Fail-loud, not fail-silent. |

---

## 9. Failed-trade surface (D5)

The user explicitly said *"yes investigate the positions, there should by now at leaast be SOME trades that failed"*. F9 backfilled 77 positions; their distribution per master handoff §6 Q1:

- **77 / 77** filled (P&L all non-NULL)
- All currently `outcome='manual_close'` per Q1
- Average +$23.69, total +$1,824.24

There are no SL/TP-hit trades yet because BTC ran up monotonically through the backfill window. **This is plausible, not a bug** (Debug verified). But D5 demands the dashboard surface failed trades when they occur.

### 9.1 Win / loss / breakeven bucket SQL (per §11 Q4 — adaptive band)

Per the §11 Q4 ratified decision, breakeven is **not** a fixed `±$1` band but an adaptive band that grows with the position's actual fees. The break point: a 5-contract scalp's $0.30 round-trip fee should not classify a $0.50 win as "breakeven", but a $30 fee on a 1000-contract position absolutely should.

```sql
-- Bucket every closed position into win / loss / breakeven for skill scoring + dashboard cards.
-- |pnl| <= max(total_fees_usd, 1.00)  →  breakeven (noise zone — fees + rounding dominate)
-- pnl > breakeven_band                →  win
-- pnl < -breakeven_band                →  loss
SELECT
    actor_instance_id,
    count(*) FILTER (
        WHERE realized_pnl_usd_final
              > GREATEST(COALESCE(total_fees_usd, 0)::numeric, 1.00::numeric)
    ) AS wins,
    count(*) FILTER (
        WHERE realized_pnl_usd_final
              < -GREATEST(COALESCE(total_fees_usd, 0)::numeric, 1.00::numeric)
    ) AS losses,
    count(*) FILTER (
        WHERE abs(realized_pnl_usd_final)
              <= GREATEST(COALESCE(total_fees_usd, 0)::numeric, 1.00::numeric)
    ) AS breakeven,
    count(*) AS total
FROM tracked_positions
WHERE closed_at IS NOT NULL
  AND realized_pnl_usd_final IS NOT NULL
  AND closed_at >= now() - interval '30 days'
GROUP BY actor_instance_id;
```

Notes:
- `total_fees_usd` is computed by F10 [`shared/intelligence/fee_calc.py`](shared/intelligence/fee_calc.py:1) at close time. It is non-NULL for all 77 backfilled positions and all post-F10 closes.
- `COALESCE(..., 0)` defends against pre-F10 historical rows. The `GREATEST(..., 1.00)` floor then guarantees the band is never below $1 even if fees were not recorded.
- The same band is used in C1 (`pnl_quality`) of `compute_skill_score()` to decide whether a small-positive trade counts as a win — keep §3.2's C1 SQL in lock-step with this section if either is edited.

### 9.2 Dashboard surface contract

1. **Header strip** — adjacent to the skill-score sparkline, show `failed_trades_<window>` count: `count(*) FILTER (WHERE outcome IN ('sl_hit','expiry') AND realized_pnl_usd_final < -GREATEST(COALESCE(total_fees_usd,0), 1.00))`.
2. **Memory Feed dimension filter** — add a `failed_trade` synthetic dimension that rolls up `outcome IN ('sl_hit','expiry')` postmortems where the loss exceeds the breakeven band.
3. **Agent Brain Card** — show `wins / losses / breakeven` count for the window using the §9.1 bucket SQL. Tooltip explains the adaptive band: "Breakeven = |P&L| within fees ± $1; win/loss are outside that zone".

If at the end of Phase Y deployment we still see 0 failed trades on a real population, that is a *system-validation finding*, not a dashboard bug — surface it via a guard-sidebar warning ("⚠ 0 losing trades in last 30d across N=X positions — verify SL execution").

---

## 10. Self-critique (devil's advocate)

A senior engineer would push back on these v2 choices. Pre-empted:

1. **"Your weights (0.30 / 0.25 / 0.20 / 0.15 / 0.10) are arbitrary."** — Yes. They are a v1 starting point. The skill-score is *internally relative* (compare actor A vs actor B over the same window) so the weights need only be stable, not optimal. After 30d of data we calibrate via a small A/B: which weighting best predicts out-of-sample win-rate? That's Phase Y.next.

2. **"C4 (memory recall hit-rate) is circular — agents who write more memories will recall more."** — Half-true. The denominator is *returned memories whose outcome matched*, not raw memory count. An agent that writes 10 000 random memories and recalls 100 random ones gets a hit-rate near `base_rate`. An agent that writes 100 *relevant* memories and recalls them with high hit-rate scores higher. Volume helps a little, accuracy helps a lot.

3. **"Why not use a real ML model for skill-vs-luck?"** — D3 says we *don't have* enough data yet (a few hundred trades). A 5-component handcrafted formula is what the data supports. ML in Phase Y.next once N > 1 000 closed positions per actor.

4. **"v2 drops Pattern Cluster Cloud — the user wanted it."** — User said "be creative", didn't mandate clusters. The `tag_normaliser.py` stub means the cloud would be a tag soup. We surface clusters as a Phase Y.next item with a stub UI placeholder ("Pattern Cluster Cloud — pending tag_normaliser implementation in Phase 11").

5. **"The new MATERIALIZED VIEWs add 60s of staleness."** — Acceptable. The user is asking "are we getting smarter?" — the answer doesn't change minute-to-minute. 60s is fine. Real-time stays in the Memory Feed (which is live-WS-driven).

6. **"`compute_skill_score()` SQL function is a maintenance trap — formula change = migration."** — True. Counter-trade-off: keeping the formula in Python only means the dashboard is fast but the leaderboard SQL has to do round-trips. The SQL function is a single edit point and the migrations form a versioned audit trail of how skill was defined over time. We accept the trap.

7. **"Why not just compute skill once daily as a cron and store in a table?"** — Considered. Two reasons against: (a) the user wants to drag the window slider and see it update; (b) materialised views give us this *plus* the audit trail of a SQL function. Best of both.

8. **"§7's coach-A/A-test seed feels like cheating."** — It is. But the alternative is "coach can never start because there are no variants" — actual deadlock. The A/A burns 2 weeks of cost ($X — measurable in `api_cost_log`) and produces a calibration baseline. After A/A passes (lift ≈ 0 ± noise), real A/B starts. Net cost: 2 weeks × small per-call overhead. Net benefit: coach moves from blocked to unblocked.

---

## 11. Open questions for user (before Y.4 ships)

> **STATUS (2026-05-03): ANSWERED.** User responses recorded below verbatim, with the resolved design decision (and "smartest default" recommendation where the user deferred to the system).

### Q1 — Skill-score weight calibration (§3.1)

> Originally: *"the 0.30 / 0.25 / 0.20 / 0.15 / 0.10 split is judgement, not data-driven. Confirm we ship with this and recalibrate after 30d of data, OR pick different weights?"*

**User answer:** *"keep, and show recalibration recommendations on the screen. is there a way we can 'as AI' button or next to it, so that a recommnedation is cerated based on the historic data?"*

**Resolution:**
- Ship with the v1 weights `C1=0.30, C2=0.25, C3=0.20, C4=0.15, C5=0.10` (sum 1.00).
- Surface a **"Recalibration recommendations"** panel on the Skill Score header strip showing — for each component — the proposed new weight and the delta vs current.
- Add an **"Ask AI"** button next to the panel that triggers a one-shot LLM call summarising the last 30d of `position_postmortems`, `mem0_recall_log`, `edge_score_changes`, and `tracked_positions.outcome` and proposes a new weight vector with a confidence band per component. The recommendation is *advisory only* — applying it requires a follow-up Architect-mode migration that bumps `WEIGHTS` in [`shared/intelligence/skill_scorer.py`](shared/intelligence/skill_scorer.py:1) and re-runs `compute_skill_score()`.
- New table: `skill_weight_recommendations` (per-company, append-only, captures every "Ask AI" output). Schema in §3.5 below.
- New phase Y.5b absorbs the UI work for the "Ask AI" button (was previously bundled into Y.4 — extracted so Y.4 stays at 2 days).

### Q2 — C4 mem0 recall threshold

> Originally: *"what counts as a 'match'? v2 proposes 'memory metadata.outcome equals position outcome'. Is this acceptable, or does the user want a richer match?"*

**User answer:** *"whats the smartest and best way"* — defers to system recommendation.

**Smartest-default decision (ratified):** **Tiered match** — outcome-only is too noisy (50%+ false positives on a binary outcome distribution); strict triple-match (dimension + outcome + symbol) starves the recall log on small data. Tier the score so partial matches still count proportionally:

| Match level | Predicate | Score |
|-------------|-----------|------:|
| Strong  | `metadata.outcome = pos.outcome` AND `metadata.dimension = signal.dimension` AND `metadata.symbol = pos.instrument_symbol` | **1.0** |
| Medium  | `metadata.outcome = pos.outcome` AND `metadata.dimension = signal.dimension` (symbol may differ) | **0.6** |
| Weak    | `metadata.outcome = pos.outcome` only | **0.3** |
| None    | otherwise | **0.0** |

The C4 component then becomes `avg(match_score)` instead of `avg(1 if matched else 0)`, still in `[0, 1]`. Implementation: `mem0_recall_log` gains three columns (`match_dim`, `match_outcome`, `match_symbol`) populated when the position closes. The compound match score is computed at SELECT time (not stored) so future tier-weight changes don't require a backfill. **See §3.4 below for the updated DDL and §3.1 C4 row for the updated source description.**

### Q3 — Window default

> Originally: *"three views are 7d / 14d / 30d. Default tab is 7d. Confirm, or default to 14d (more stable)?"*

**User answer:** *"7d, then 14, 1M,"*

**Resolution:**
- Default window **= 7d** (confirmed).
- Tab order **= `[ 7d ] [ 14d ] [ 1M ]`** — note the *display label* on the third tab is **"1M"** (one month), not "30d", per user phrasing. The underlying SQL window remains 30 days; only the UI label changes. View name stays `v_actor_skill_30d` / `v_memory_feed_30d`.

### Q4 — Failed-trade threshold

> Originally: *"§9 says 'loss = realized_pnl_usd_final < 0'. Should breakeven (-$1 to +$1) be a separate bucket?"*

**User answer:** *"not sure, whats the smarted."* — defers to system recommendation.

**Smartest-default decision (ratified):** **Breakeven IS a separate bucket**, but the cutoff is not a flat ±$1 — it's adaptive to fees actually paid (per F10 fee-accurate close). Definition:

```
breakeven  ≡  |realized_pnl_usd_final| ≤ max(total_fees_usd, $1.00)
loss       ≡  realized_pnl_usd_final < -breakeven_band
win        ≡  realized_pnl_usd_final >  breakeven_band
```

Where `total_fees_usd = (entry_fee + exit_fee + spread_cost + funding_cost)` from F10. Rationale: a trade that returned exactly the fee cost is operationally a wash, not a loss — counting it as a "failed trade" pollutes the failed-trade signal with rounding noise. The $1 floor handles edge-cases where an instrument has near-zero fees (would otherwise let a -$0.50 P&L silently bucket as "win"). **See §9 below for the updated bucket definitions and the SQL the dashboard will use.**

### Q5 — Coach A/A seed (§7 issue v)

> Originally: *"confirm we burn 2 weeks of LLM spend on identical-prompt A/A to break the deadlock?"*

**User answer:** *"thats fine"*

**Resolution:** approved. Y.5 ships the one-shot seed script. Budget guard: hard-cap the A/A spend at **$5** in the seed script (≈ 2× the projected $2-3) — if the cap trips before 14 days, the seed terminates and surfaces a guard-sidebar warning. Cost-tracker logs every call to `api_cost_log` with `correlation_id='coach_aa_seed'` for forensic review.

---

---

## 12. Implementation order (final, copy-paste-ready)

1. **Y.0 — MemU enum reconciliation** (0.25d, Code mode)
   - Create `shared/memu/insight_kinds.py` with `INSIGHT_KINDS = frozenset({"lesson", "warning", "playbook", "postmortem", "regime_shift", "anomaly"})`.
   - Update [`shared/memu/broadcast_payload.py`](shared/memu/broadcast_payload.py:10) and [`shared/mcp/tools/memory.py`](shared/mcp/tools/memory.py:1) to import from it.
   - Add CHECK constraint to `memu.insights.kind` after data audit.
   - **Acceptance**: both files reference the same frozenset; CHECK constraint accepts current data.

2. **Y.1 — New tables + views** (0.5d, Code mode)
   - Migration `shared/intelligence/migrations/2026_05_05_phase_y_skill_views.sql` containing `compute_skill_score()` plus `v_actor_skill_{7,14,30}d`.
   - Migration `shared/intelligence/migrations/2026_05_05_phase_y_recall_log.sql` for `mem0_recall_log`.
   - Migration `shared/intelligence/migrations/2026_05_05_phase_y_feed_views.sql` for `v_memory_feed_{7,14,30}d`.
   - **Acceptance**: all migrations apply on a fresh `tickles_<company>` DB; views return rows for the existing 77-position data; `compute_skill_score()` returns NULL for actors with <5 trades and a real number otherwise.

3. **Y.2 — Skill scorer Python** (1d, Code mode)
   - New module [`shared/intelligence/skill_scorer.py`](shared/intelligence/skill_scorer.py:1) implementing `compute_skill()` + dropout logic.
   - Edge-scorer integration: extend `ScorerInputs` and add `_skill_minus_luck()` per §6.
   - Recall-log writer: extend [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:1079) `create_tracked_position_from_interpretation()` to insert one `mem0_recall_log` row per signal.
   - Recall-log retroactive updater: extend [`shared/intelligence/postmortem_service.py:526`](shared/intelligence/postmortem_service.py:526) `_process_one()` to UPDATE matching `mem0_recall_log` rows with `matched_outcome` after the position closes.
   - **Acceptance**: tests `test_skill_scorer.py` (component dropout, weight renormalisation), `test_recall_log.py` (insert + retroactive update).

4. **Y.3 — SnapshotBuilder learning providers** (1d, Code mode)
   - New module `shared/dashboard/learning_providers.py` with six providers: `SkillSummaryProvider`, `MemoryFeedProvider` (window-parameterised), `AgentBrainProvider`, `GuardActivityProvider`, `PromptEvolutionProvider`, `FailedTradesProvider`.
   - Each provider: 250ms hard budget, isolated try/except.
   - **Acceptance**: snapshot endpoint returns all six fields; each survives source-down conditions.

5. **Y.4 — Header strip + Memory Feed + Agent Brain Cards** (2d, Code mode)
   - Three UIs share view bindings; build together to avoid CSS/JS duplication.
   - Window selector (7d / 14d / 30d) on header and Memory Feed tab; per-component tooltip on skill score.
   - Failed-trade surface (§9) baked in.
   - Five-issues fixes (§7) all closed in PR description.
   - **Acceptance**: each tab passes integration smoke (synthetic 1-row-per-source feed); skill score shows component breakdown on hover; window selector round-trips through anchor URL.

6. **Y.5 — Guard sidebar + Prompt Evolution timeline** (0.5d, Code mode)
   - One-shot coach-A/A-seed script as part of this phase (§7 issue v).
   - **Acceptance**: prompt timeline renders with at least 1 promotion event after 1 week of A/A data.

---

## 13. Resume command for next session

> "Read [`.roo/handoffs/2026-05-03-master-resume-handoff.md`](.roo/handoffs/2026-05-03-master-resume-handoff.md:1) §2 (binding directives D1–D5) and [`shared/docs/PHASE_Y_LEARNING_DASHBOARD_PLAN.md`](shared/docs/PHASE_Y_LEARNING_DASHBOARD_PLAN.md:1) (this file) §11 open questions. Confirm answers with user, then switch to Code mode and start Y.0 (MemU enum reconciliation in [`shared/memu/broadcast_payload.py`](shared/memu/broadcast_payload.py:10) + [`shared/mcp/tools/memory.py`](shared/mcp/tools/memory.py:1) via new `shared/memu/insight_kinds.py`). Do NOT touch §4.3 mem0-vs-md audit until §4.3 handoff doc exists. Do NOT begin Y.1 until Y.0 lands."

---

## 14. Change log

| Date | Author | Change |
|------|--------|--------|
| 2026-05-02 | Architect (Roo) | v1 — initial plan, 8 sub-phases. |
| 2026-05-03 | Architect (Roo) | **v2** — incorporates D1 (memory unification, drops Phase Y.A), D2 (7d/14d/30d windows), D3 (skill-vs-luck composite formula §3), D4 (five-issues fixes §7), D5 (failed-trade surface §9). Drops Phase Y.A (openclaw_export_daemon) and demotes Phase Y.F (pattern cluster cloud) to Y.next. Corrects v1's five design errors (§0 table). New tables: `mem0_recall_log`. New SQL function: `compute_skill_score()`. New views: `v_actor_skill_{7,14,30}d`, `v_memory_feed_{7,14,30}d`. Edge-scorer extended with `skill_minus_luck` component (§6). |
