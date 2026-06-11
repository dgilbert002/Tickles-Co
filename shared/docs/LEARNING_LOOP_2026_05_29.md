# Learning Loop — closing the loop (2026-05-29)

> Goal (in plain words): when a trade finishes, the system should *learn* from it,
> remember the lesson somewhere it can find again, and actually *use* that lesson
> the next time it sees a similar setup. Right now the system writes graded
> postmortems but the lessons mostly got stranded. This doc tracks the fixes that
> close the loop.

The loop has three pieces:

| Phase | What it does | Status |
|-------|--------------|--------|
| **A** | Promote graded postmortem **lessons → MemU** (cross-company institutional memory) | ✅ DONE (this doc) |
| **B** | **Measure recall** — record when a past lesson is pulled before a decision, and link it to the resulting position so we can prove recall improves outcomes | ⏳ next |
| **C** | Give the **copy agents a learning loop** (copy_chai_vision + mirror agents) | ⏳ later |

---

## Phase A — promote postmortem lessons to MemU

### The problem (explained simply)

There are two memory systems:

- **mem0** = each company's *private notebook*. Per-company. Already received
  postmortem lessons.
- **MemU** = the *shared library* every agent across every company can read.
  This is where institutional, cross-company wisdom is supposed to live.

Before this change, MemU's `insights` table was full of **signal snapshots**
(what a chart looked like at the moment of a signal) but had **zero** graded
**postmortems** (what actually happened and what we learned). The postmortem
service had a comment literally saying broadcast-to-MemU "is handled elsewhere"
— but it wasn't handled anywhere. The lessons died in `position_postmortems`
and per-company mem0.

### What was changed

**1. Live path —
[`shared/intelligence/postmortem_service.py`](../intelligence/postmortem_service.py)**

- Added `_build_postmortem_broadcast(...)` — a **pure** function that turns a
  closed-position postmortem into a `BroadcastPayload` dict (kind=`postmortem`).
  It returns `None` when there's no cross-company value (no company lesson and no
  detected edge), so we don't pollute MemU with empty rows.
  - `signal_source='chart_hacker'` → `actor_type='agent'`, `actor_id='chart_hacker'`
  - otherwise → `actor_type='trader'`, `actor_id=<trader handle>`
  - `body_md` is a small markdown block: Lesson / Edge detected / Why it
    failed|worked / Actor note.
- Added `_broadcast_to_memu(pool, payload)` — does the durable
  `INSERT INTO public.memu_outbox` + `pg_notify('memu_broadcast', …)`. This is a
  **local copy** of `interpretation_service.broadcast_insight` on purpose — we
  do NOT import `interpretation_service` here (it's heavy and would risk a
  circular import).
- Wired both into `_push_lessons_to_mem0(...)` right after the company-lesson
  mem0 write. It's **best-effort**: any MemU hiccup is logged and swallowed so a
  postmortem is never blocked by a memory write.
- Removed the stale "handled elsewhere" comment.

The `memu-listener` service (already running) drains `memu_outbox` and writes
each row into MemU `insights`. MemU **dedups on `(kind, content_hash)`**, so the
same lesson can be queued twice with no duplicate insight created.

**2. Back-fill —
[`shared/scripts/backfill_postmortems_to_memu.py`](../scripts/backfill_postmortems_to_memu.py)
(NEW)**

A one-shot script that promotes the **existing** historical postmortems using
the *exact same* `_build_postmortem_broadcast` builder (single source of truth
for payload format). It is **dry-run by default**; `--apply` actually queues the
outbox rows. Idempotent at the insights layer thanks to MemU dedup.

```bash
# preview (no writes)
python3 -m shared.scripts.backfill_postmortems_to_memu
# execute
python3 -m shared.scripts.backfill_postmortems_to_memu --apply
# one company only
python3 -m shared.scripts.backfill_postmortems_to_memu --company jarvais --apply
```

**3. Tests —
[`shared/tests/test_postmortem_service.py`](../tests/test_postmortem_service.py)**

- Fixed a **pre-existing** failing test (`test_process_one_writes_postmortem_and_marks_done`):
  the mock position was missing the Phase J columns (`signal_source`, etc.) that
  the real query returns. Completed the fixture and isolated the learning-loop
  side-effect by patching `_push_lessons_to_mem0` (that path now has its own
  dedicated unit tests).
- Added 3 new pure-function tests for `_build_postmortem_broadcast` (skip-when-empty,
  trader payload shape, chart_hacker→agent mapping).
- Result: **34 passed** (was 30 passed + 1 failed).

### Verification (what actually happened on the box)

- MemU before: `lesson:1092, chart_interpretation:16, playbook:1, warning:1`,
  **postmortem: 0**.
- Back-fill dry-run: **137 promotable** out of 456 postmortems (the other ~319
  had empty `lessons_for_company` AND empty `edge_detected`, so nothing worth
  promoting — correctly skipped).
- `--apply` → queued 137 → listener drained the outbox to 0.
- MemU after: **postmortem: 137**.
- Semantic search smoke test (`MemU.search('why do BTC short trades hit stop
  loss', kind='postmortem')`) returns relevant graded lessons. ✅

### How to roll back

Nothing here is destructive, but if you must undo:

1. **Stop new promotions (code):** revert
   [`shared/intelligence/postmortem_service.py`](../intelligence/postmortem_service.py)
   — remove `_build_postmortem_broadcast`, `_broadcast_to_memu`, and the
   "Phase A — promote the graded lesson to MemU" block inside
   `_push_lessons_to_mem0`. Re-add the old `# … handled elsewhere …` comment if
   you want byte-parity.
2. **Remove the back-filled insights (data):**
   ```sql
   -- MemU lives in its own DB
   DELETE FROM insights WHERE kind = 'postmortem';
   ```
   (Only postmortems were added by this work; lessons/snapshots are untouched.)
3. **Optionally clear the outbox rows** (already processed, harmless to leave):
   ```sql
   -- tickles_shared
   DELETE FROM public.memu_outbox
   WHERE payload->>'insight_kind' = 'postmortem';
   ```
4. Delete the back-fill script if undesired:
   `shared/scripts/backfill_postmortems_to_memu.py`.

### Caveats

- The live path only promotes lessons with **cross-company value** (a company
  lesson or a detected edge). Actor-only notes stay in per-company mem0 — that's
  intentional (MemU is the *shared* library, not a dumping ground).
- `correlation_id` defaults to `pm-<position_id>` when the caller doesn't supply
  one, so every promoted lesson is still traceable back to its position.

---

## Phase B — recall measurement (2026-05-30 update)

### What changed

1. **`record_recall()` wired** in `_recall_relevant_memories()` — two rows per chart:
   `(correlation_id, actor_id, signal_source='trader'|'chart_hacker')`.
2. **`link_recall_by_correlation()`** links the correct arm when a tracked position
   is created (`signal_source` from the position row).
3. **Migration** `shared/intelligence/migrations/2026_05_30_recall_signal_source.sql`
   adds `signal_source` column + partial index for pending lookups.
4. **Mem0 process cache** — `get_cached_memory()` / `_MEM0_CACHE` in
   `mem0_config.py` (same pattern as MCP memory tools). One embed client per
   `(company, agent)` per process.
5. **Embed/Qdrant retry** — one retry on vector-store failures only (not LLM).

### Verify

```sql
-- tickles_shared
SELECT signal_source, COUNT(*) FROM mem0_recall_log
 WHERE created_at > NOW() - INTERVAL '24 hours'
 GROUP BY 1;
```

### Rollback

```bash
git checkout HEAD -- shared/intelligence/recall_log.py shared/intelligence/interpretation_service.py
# column is additive — optional:
# ALTER TABLE mem0_recall_log DROP COLUMN IF EXISTS signal_source;
sudo systemctl restart tickles-interpretation tickles-postmortem
```

See restart checklist: [`RESTART_TICKLES_WORKERS.md`](RESTART_TICKLES_WORKERS.md).

---

## Copy-trade monitor accuracy (2026-05-30)

Paper agents now mirror the real replay model:

| Rule | Behaviour |
|------|-----------|
| **No SL → skip** | `validate_levels()` returns `None`; trade not taken |
| **SL but no TP** | TP = 2× SL distance (1:2 RR) |
| **Entry touch** | Price must touch entry in last `COPY_ENTRY_TOUCH_CANDLES` (default 60) 1m candles |
| **Fill time** | `entered_at` = trader's `activated_at` (not `NOW()`) |
| **Margin queue** | When balance tight, queue by distance-to-entry; expire if TP/SL hit before fill |

Rollback: `git checkout HEAD -- shared/intelligence/copy_trade_monitor.py` + restart
`tickles-copy-trade-monitor`.

---

## OpenClaw removal cleanup (2026-05-30)

Deleted `/root/_archive_openclaw_deleted_20260520` and `/home/paperclip/.openclaw`.
**Required follow-up:** cleared `ReadWritePaths=/root/.openclaw` from
`/etc/systemd/system/tickles-mcpd.service.d/override.conf` (systemd fails mount
namespacing if the path is missing). `tickles-cost-shipper` still references
`OPENCLAW_ROOT` — harmless if OpenClaw is gone (no new usage to ship).

## Phase C — copy-agent learning loop (later)

Plan: give `copy_chai_vision` and the mirror agents their own
close → postmortem → lesson → recall cycle. *(Not yet designed.)*

---

## Mem0 lean recall (2026-05-30)

### Decision

Operator chose **stay lean**: memory recall is **local-only** — no cloud LLM on
search or store. Postmortem compression stays in `postmortem_service` (one LLM
pass when a trade closes). Mem0 is just the searchable index.

### What changed

[`shared/utils/mem0_config.py`](../utils/mem0_config.py):

1. **Guard fix** — `_mem0_env_guard()` now wraps the full `add` / `search` call
   (not just `Memory.from_config`). This stops accidental OpenRouter traffic
   from mem0 seeing `OPENROUTER_API_KEY` at call time.
2. **Lean search** — `rerank=False`; vector similarity only (local embed + Qdrant).
3. **Lean add** — still `infer=False`; no multi-model cloud fallback loop.
4. **Stub LLM config** — mem0 requires an `llm` block in its schema; we wire a
   noop stub (`local/noop`) that is never called in lean mode.
5. **Process cache** — `_MEM0_CACHE` keyed by `(company, agent)`; use
   `get_cached_memory()` in hot paths.
6. **Retry once** on Qdrant/embed failures only.

Recall prompt size is unchanged: interpretation still pulls max **4 self + 3
trader** lessons (~7 short snippets), regardless of total memories in Qdrant.

### Rollback

```bash
git checkout HEAD -- shared/utils/mem0_config.py
sudo systemctl restart tickles-interpretation tickles-postmortem tickles-chart-hacker-opinion
```
