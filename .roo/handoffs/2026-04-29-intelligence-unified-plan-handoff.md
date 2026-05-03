# Handoff: Intelligence Unified Plan — Master Roadmap Complete

**Date:** 2026-04-29
**Topic:** Consolidation of all 62 findings (A–T + AA–BP) into a single uniform-contract master plan
**Status:** ✅ Plan complete, audited, embedded, and pointer-linked from `CLAUDE.md`. Implementation has **not** started yet — Phase 0 is the next action.

---

## TL;DR for Incoming AI

You are picking up a **freshly finalised master roadmap**, not in-flight code work.

- The single source of truth is [`shared/docs/INTELLIGENCE_UNIFIED_PLAN.md`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:1) (4232 lines, 14 phases, ~31 days).
- Every phase follows a **uniform 8-section contract**: Purpose / What / Where / When / Why / How / Risks & mitigations / Benchmark checklist.
- All **20 original A–T patches** + all **42 devil's-advocate AA–BP findings** = **62 distinct improvements** are inlined with operational content (verified by 3 parallel regex sweeps — 231 total tag occurrences).
- [`CLAUDE.md`](CLAUDE.md:243) Intelligence Pipeline section now opens with a **canonical plan pointer callout** (status, phase order, embedded findings, resume command).
- **No code has been written yet for any of these 62 items.** The plan is the deliverable; implementation is Phase 0+ work.

**The only valid next move is to start Phase 0 — Document Contract & Pre-Kickoff Blockers.**

---

## Completed Work (this session and immediately prior)

### 1. Plan rewrite — every phase rewritten to uniform contract
[`shared/docs/INTELLIGENCE_UNIFIED_PLAN.md`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:1) phases now in order:

| Phase | Topic | Days | Key tags embedded |
|-------|-------|------|-------------------|
| **0.1** | Pre-kickoff blockers | — | [AF] [BN] [BK] [BP] [BO] |
| **0** | Document contract + companies + master sync | 0.5 | [G] [AA] [AB] [BN] |
| **1** | Universal API cost log + gateway refactor | 2 | [A] [AD] [B] [C] [Q] [E] [R] [AC] [AE] [BP] |
| **2** | Schema unification (signals, positions, postmortems) | 3 | [F] [AH] [AT] [AG] [AI] [M] [P] [AX] |
| **3** | Signal review export (CSV+HTML+thumbs) | 1.5 | [AL] |
| **4** | HTML render polish + thumbnail safety | 0.5 | [AM] [AN] |
| **5** | Served HTML manage panel | 2.5 | [D] [AQ] [AO] [AP] [AR] [O] [S] |
| **6** | Reasoning + similarity + prompt registry | 2 | [N] [AU] [AS] [AK] [AG] |
| **7** | Postmortem service + memu_outbox + scheduler | 3 | [J] [AV] [AW] [AJ] [AX] [T] [BJ] [I] |
| **8** | ChartHacker opinion split | 2 | [AY] [AZ] [I] |
| **9** | Discord + zone/phash + rate-limit | 2 | [BA] [BB] |
| **10** | Single-writer policy + actor_instance | 2 | [BC] [BD] |
| **11** | edge_score + CoachService + leaderboard reconciliation | 4 | [K] [BE] [BF] [BG] [I] |
| **L** | Dashboard-as-aggregator (read-only) | 3 | [L] [BH] [BI] [BK] [H] |
| **R** | Enforcement layer (CI gates + heartbeats) | 3 | [BL] [BM] [BN] [BO] [BC] |

**Total ≈ 31 days.**

### 2. Mini-Memory section appended
At [`INTELLIGENCE_UNIFIED_PLAN.md`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:4141): project glance, verified facts (do-NOT-re-verify), open questions, file hot-list, hard rules, after-each-phase ritual, and the uniform phase contract spelled out.

### 3. Self-review (devil's-advocate pass) completed
Found and patched: pre-kickoff section ordering, cross-cutting concerns table at [`INTELLIGENCE_UNIFIED_PLAN.md:217`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:217), explicit naming of [BO] single-writer guard.

### 4. CLAUDE.md pointer added
[`CLAUDE.md:243`](CLAUDE.md:243) now starts the Intelligence Pipeline section with a callout box pointing at the unified plan as source of truth, with the resume command verbatim and a note that legacy 3B is "already shipped — do not duplicate".

### 5. Tag-coverage audit completed
Three parallel `search_files` sweeps (A–T, AA–AZ, BA–BP) returned **74 + 83 + 74 = 231 occurrences for 62 unique tags** (avg 3.7× cross-referenced). Per-tag confirmation table is in the previous `attempt_completion` reply and reproduced in section "Per-tag anchor table" below.

---

## Critical Context (Read Before Touching Anything)

### Verified ground truth — DO NOT re-verify
1. **Database is PostgreSQL, NOT MySQL.** [`CLAUDE.md`](CLAUDE.md:1) is out of date on this point. Schema files: [`shared/migration/tickles_shared_pg.sql`](shared/migration/tickles_shared_pg.sql:1) and per-company `tickles_<company>_pg.sql`.
2. **No cross-DB foreign keys.** `tickles_shared` and `tickles_<company>` are separate logical databases — FKs across them won't exist; integrity is enforced in code. [AF].
3. **`tracked_positions` lives in `tickles_shared.public` (single ledger), NOT per-company.** [F][AH]. Same for `position_updates`. Postmortems and signal_interpretations stay per-company.
4. **Memory tiers verified 2026-04-29:** Tier 1 mem0/Qdrant per-agent, Tier 2 mem0/Qdrant per-company, Tier 3 MemU cross-company. Dev work uses [`shared/utils/mem0_config.py`](shared/utils/mem0_config.py:208) `get_dev_memory()` — never write dev decisions to a trading namespace.
5. **OpenClaw-native agents are paperclip-visible** — see [`shared/docs/TICKLES_INFRASTRUCTURE_ATLAS.md`](shared/docs/TICKLES_INFRASTRUCTURE_ATLAS.md:1052) §10 for recipe. ChartHacker opinion role and CoachService both use this pattern with `--tools read,exec`. [I].
6. **No CHECK constraints in Postgres on enum-like columns** — use Postgres ENUM types instead. [M].
7. **Drop `'jarvais'` defaults from new schema** — jarvais is frozen legacy V1, do not write. [P].

### Pre-kickoff blockers (resolve in Phase 0 before anything else)
| Tag | Blocker | Where in plan |
|-----|---------|---------------|
| [AF] | Cross-DB no-FK contract — every schema must compile under separate-database assumption | [`INTELLIGENCE_UNIFIED_PLAN.md:716`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:716) |
| [BN] | Master schema sync gate — single dump that updates every company DB atomically | [`INTELLIGENCE_UNIFIED_PLAN.md:3746`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:3746) |
| [BK] | Cached chart renderer must exist before Phase L — otherwise dashboard rendering blows budget | [`INTELLIGENCE_UNIFIED_PLAN.md:3383`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:3383) |
| [BP] | LLM budget circuit-breaker — daily $/role/company caps before any new LLM caller goes live | [`INTELLIGENCE_UNIFIED_PLAN.md:539`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:539) |
| [BO] | Single-writer CI guard — must be in place before Phase 5 manage-panel exposes mutations | [`INTELLIGENCE_UNIFIED_PLAN.md:217`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:217) |

### Cross-cutting concerns (touch every phase)
G1 dynamic taxonomy · G2 manage panel · G3 OpenClaw shells · G4 MemU broadcasts · G5 universal cost log · G6 dashboard surfaces · G7 CI gates non-negotiable. Table at [`INTELLIGENCE_UNIFIED_PLAN.md:217`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:217).

---

## Per-Tag Anchor Table (audit output)

### A–T (20 findings)
| Tag | Phase §  | Anchor |
|-----|---------|--------|
| A | Phase 1 §A | [`INTELLIGENCE_UNIFIED_PLAN.md:506`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:506) |
| B | Phase 1 callout | [`INTELLIGENCE_UNIFIED_PLAN.md:401`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:401) |
| C | Phase 1 .env reconciliation | [`INTELLIGENCE_UNIFIED_PLAN.md:421`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:421) |
| D | Phase 5 §A | [`INTELLIGENCE_UNIFIED_PLAN.md:1241`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:1241) |
| E | Phase 1 §E | [`INTELLIGENCE_UNIFIED_PLAN.md:619`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:619) |
| F | Phase 2 §2.2 | [`INTELLIGENCE_UNIFIED_PLAN.md:751`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:751) |
| G | Phase 0 §G[AA] | [`INTELLIGENCE_UNIFIED_PLAN.md:263`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:263) |
| H | Phase 7 §A + Phase L §F | [`INTELLIGENCE_UNIFIED_PLAN.md:1840`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:1840) |
| I | Phases 7/8/11 OpenClaw shells | [`INTELLIGENCE_UNIFIED_PLAN.md:2013`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:2013) |
| J | Phase 7 §F | [`INTELLIGENCE_UNIFIED_PLAN.md:1916`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:1916) |
| K | Phase 11 §E | [`INTELLIGENCE_UNIFIED_PLAN.md:3145`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:3145) |
| L | Phase L §B | [`INTELLIGENCE_UNIFIED_PLAN.md:3330`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:3330) |
| M | Phase 2 | [`INTELLIGENCE_UNIFIED_PLAN.md:716`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:716) |
| N | Phase 6 §C | [`INTELLIGENCE_UNIFIED_PLAN.md:1608`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:1608) |
| O | Phase 5 §F | [`INTELLIGENCE_UNIFIED_PLAN.md:1386`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:1386) |
| P | Phase 2 | [`INTELLIGENCE_UNIFIED_PLAN.md:716`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:716) |
| Q | Phase 1 cost-log switch | [`INTELLIGENCE_UNIFIED_PLAN.md:452`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:452) |
| R | Phase 1 §E grep guard | [`INTELLIGENCE_UNIFIED_PLAN.md:619`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:619) |
| S | Phase 5 §G | [`INTELLIGENCE_UNIFIED_PLAN.md:1400`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:1400) |
| T | Phase 7 prompt path | [`INTELLIGENCE_UNIFIED_PLAN.md:1871`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:1871) |

### AA–AZ (26 findings)
| Tag | Phase §  | Anchor |
|-----|---------|--------|
| AA | Phase 0 §G[AA] | [`INTELLIGENCE_UNIFIED_PLAN.md:263`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:263) |
| AB | Phase 0 migration runner | [`INTELLIGENCE_UNIFIED_PLAN.md:263`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:263) |
| AC | Phase 1 §F restart-semantics | [`INTELLIGENCE_UNIFIED_PLAN.md:539`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:539) |
| AD | Phase 1 §A precision | [`INTELLIGENCE_UNIFIED_PLAN.md:506`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:506) |
| AE | Phase 1 §G correlation | [`INTELLIGENCE_UNIFIED_PLAN.md:603`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:603) |
| AF | Phase 2 + Pre-kickoff | [`INTELLIGENCE_UNIFIED_PLAN.md:716`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:716) |
| AG | Phase 2 §2.2 trigger | [`INTELLIGENCE_UNIFIED_PLAN.md:792`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:792) |
| AH | Phase 2 position_updates | [`INTELLIGENCE_UNIFIED_PLAN.md:751`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:751) |
| AI | Phase 2 §2.3 status enum | [`INTELLIGENCE_UNIFIED_PLAN.md:820`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:820) |
| AJ | Phase 7 §C registration | [`INTELLIGENCE_UNIFIED_PLAN.md:1871`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:1871) |
| AK | Phase 6 §E threshold | [`INTELLIGENCE_UNIFIED_PLAN.md:1709`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:1709) |
| AL | Phase 3 retention | [`INTELLIGENCE_UNIFIED_PLAN.md:931`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:931) |
| AM | Phase 4 §C cap | [`INTELLIGENCE_UNIFIED_PLAN.md:1053`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:1053) |
| AN | Phase 4 §D autoescape | [`INTELLIGENCE_UNIFIED_PLAN.md:1091`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:1091) |
| AO | Phase 5 §C CSRF | [`INTELLIGENCE_UNIFIED_PLAN.md:1292`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:1292) |
| AP | Phase 5 §D rate limiter | [`INTELLIGENCE_UNIFIED_PLAN.md:1324`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:1324) |
| AQ | Phase 5 §B aiohttp | [`INTELLIGENCE_UNIFIED_PLAN.md:1275`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:1275) |
| AR | Phase 5 §E fetch wrapper | [`INTELLIGENCE_UNIFIED_PLAN.md:1354`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:1354) |
| AS | Phase 6 §D normaliser | [`INTELLIGENCE_UNIFIED_PLAN.md:1670`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:1670) |
| AT | Phase 2 instrument cols | [`INTELLIGENCE_UNIFIED_PLAN.md:716`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:716) |
| AU | Phase 6 §B pgvector | [`INTELLIGENCE_UNIFIED_PLAN.md:1573`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:1573) |
| AV | Phase 7 §G TypedDict | [`INTELLIGENCE_UNIFIED_PLAN.md:1934`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:1934) |
| AW | Phase 7 §D advisory lock | [`INTELLIGENCE_UNIFIED_PLAN.md:1972`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:1972) |
| AX | Phase 2 §2.3 + Phase 7 §E | [`INTELLIGENCE_UNIFIED_PLAN.md:820`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:820) |
| AY | Phase 8 §B opinion budget | [`INTELLIGENCE_UNIFIED_PLAN.md:2150`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:2150) |
| AZ | Phase 8 §D confidence gate | [`INTELLIGENCE_UNIFIED_PLAN.md:2230`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:2230) |

### BA–BP (16 findings)
| Tag | Phase §  | Anchor |
|-----|---------|--------|
| BA | Phase 9 §E phash | [`INTELLIGENCE_UNIFIED_PLAN.md:2525`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:2525) |
| BB | Phase 9 §D rate-limit | [`INTELLIGENCE_UNIFIED_PLAN.md:2478`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:2478) |
| BC | Phase 10 §B + Phase R §B | [`INTELLIGENCE_UNIFIED_PLAN.md:2683`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:2683) |
| BD | Phase 10 §A actor_instance | [`INTELLIGENCE_UNIFIED_PLAN.md:2660`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:2660) |
| BE | Phase 11 §A formula | [`INTELLIGENCE_UNIFIED_PLAN.md:2956`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:2956) |
| BF | Phase 11 §C sample gate | [`INTELLIGENCE_UNIFIED_PLAN.md:3038`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:3038) |
| BG | Phase 11 §F CoachService | [`INTELLIGENCE_UNIFIED_PLAN.md:3155`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:3155) |
| BH | Phase L §D anchors | [`INTELLIGENCE_UNIFIED_PLAN.md:3351`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:3351) |
| BI | Phase L §A aggregator | [`INTELLIGENCE_UNIFIED_PLAN.md:3287`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:3287) |
| BJ | Phase 7 [H] croniter | [`INTELLIGENCE_UNIFIED_PLAN.md:1886`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:1886) |
| BK | Phase L §E + Pre-kickoff | [`INTELLIGENCE_UNIFIED_PLAN.md:3383`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:3383) |
| BL | Phase R §A schema-diff | [`INTELLIGENCE_UNIFIED_PLAN.md:3561`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:3561) |
| BM | Phase R §D/§F/§G heartbeats | [`INTELLIGENCE_UNIFIED_PLAN.md:3799`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:3799) |
| BN | Phase 0 step 4 + Phase R §C | [`INTELLIGENCE_UNIFIED_PLAN.md:3746`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:3746) |
| BO | Cross-cutting + Phase R §B | [`INTELLIGENCE_UNIFIED_PLAN.md:217`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:217) |
| BP | Phase 1 §F + Pre-kickoff | [`INTELLIGENCE_UNIFIED_PLAN.md:539`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:539) |

---

## Decisions Made (irreversible without an architect-mode review)

1. **Postgres-only.** No MySQL anywhere in the plan; CLAUDE.md will be corrected during a future doc pass.
2. **Per-DB schemas, no cross-DB FKs.** Integrity in code, validated by [BL] schema-diff CI gate + [BN] master-sync gate.
3. **`tracked_positions` and `position_updates` live in `tickles_shared.public`.** Single ledger across companies. [F][AH].
4. **`signal_interpretations`, `position_postmortems`, `prompt_versions` are per-company** — they reference shared positions by id, not FK.
5. **OpenClaw `--tools read,exec`** for ChartHacker opinion service, postmortem service, CoachService. No write tools.
6. **Single-writer policy enforced.** [BC] writer-domain registry maps `{table → owning service}`; [BO] CI guard greps for foreign writers; runtime advisory locks where needed.
7. **Default-deny dashboard auth.** [D] middleware blocks anything not on a public allowlist.
8. **Cost log is universal and master-switched.** [Q]/[A]/[AD]: every LLM caller writes `api_cost_log` with `cost_usd numeric(20,8)` and `correlation_id`.
9. **Hindsight-bias guard is non-negotiable.** [AG] DB trigger prevents updating reasoning fields after position close.
10. **Resume rule:** never start a new phase until the previous phase's Benchmark checklist is fully ticked.

---

## Pending TODOs (in priority order — for the incoming session)

1. **Phase 0 — Document Contract & Pre-Kickoff Blockers** (0.5 day)
   - Implement [`shared/utils/companies.py`](shared/utils/companies.py:1) helper from [G][AA] spec
   - Stand up [BN] master schema sync runner per [`INTELLIGENCE_UNIFIED_PLAN.md:3746`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:3746)
   - Verify [AF] no-cross-DB-FK contract holds across all existing migration files
2. **Phase 1 — Universal API cost log** (2 days). Start with [`shared/utils/api_cost_log.py`](shared/utils/api_cost_log.py:1) per the §A skeleton at [`INTELLIGENCE_UNIFIED_PLAN.md:469`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:469).
3. **Phase 2 — Schema unification** (3 days). Watch out: `tracked_positions` MUST land in `tickles_shared.public`, not per-company.
4. *(continue in plan order)*

---

## Resolved Decisions (2026-04-29)

All four open questions from [`INTELLIGENCE_UNIFIED_PLAN.md:41`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:41) §0.1 have been resolved:

| # | Question | Decision | Rationale |
|---|----------|----------|-----------|
| 1 | ClickHouse scope | **Confirmed in-scope** for backtest/forward-test results only | ClickHouse stores `backtest_runs`, `backtest_trades`, `backtest_forward_runs`, `backtest_forward_trades`, `signal_feed`, `top_sharpe_per_indicator`, `agent_events`. Operational trading state (positions, signals, news) stays in Postgres. Verified against [`shared/migration/clickhouse_schema.sql`](shared/migration/clickhouse_schema.sql:1) and [`shared/backtest/ch_writer.py`](shared/backtest/ch_writer.py:84). |
| 2 | [BC] Writer-registry cutover | **Option B — 30-day grace period** | Registry table built immediately. Enforcement starts as warnings-only for 30 days to allow audit of all 232 `INSERT INTO` sites across the codebase. After 30 days, CI gate fails builds on unauthorized writers. Cutover date: 2026-05-30. |
| 3 | [BP] Budget circuit-breaker | **Rejected hard daily caps** | User wants loop detection (stop after N identical calls) + frequency analysis (detect repeated same-behavior patterns) + total LLM spend tracker across the entire app. Hard USD/day caps replaced with behavioral anomaly detection. |
| 4 | [BA] Zone-overlap block vs flag | **BLOCK duplicate images** | Same chart image posted across multiple sources within 60 minutes → marked `duplicate_zone`, vision LLM runs **once** on first copy only. Existing 2% trade-dedup rule ([`shared/intelligence/trade_dedup.py`](shared/intelligence/trade_dedup.py:4)) remains active for similar trade signals from different traders. Both systems work together at different layers. |

### Phase 0 Revised Scope

With these decisions, Phase 0 now includes:
- [G][AA] `shared/utils/companies.py` helper
- [BN] Master schema sync runner
- [AF] No-cross-DB-FK verification
- **[NEW]** `shared/intelligence/loop_detector.py` — behavioral loop detection + frequency analysis (replaces [BP] hard caps)
- **[NEW]** `shared/utils/llm_spend_tracker.py` — total LLM spend dashboard across all roles/companies
- **[NEW]** `shared/intelligence/writer_registry.py` — table + 30-day warning-mode registration

---

## Key File Paths (where work happens)

| Purpose | Path |
|---------|------|
| Master plan (source of truth) | [`shared/docs/INTELLIGENCE_UNIFIED_PLAN.md`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:1) |
| Project pointer | [`CLAUDE.md`](CLAUDE.md:243) |
| Shared schema (Postgres) | [`shared/migration/tickles_shared_pg.sql`](shared/migration/tickles_shared_pg.sql:1) |
| Per-company schema template | `shared/migration/tickles_<company>_pg.sql` |
| Service registry | [`shared/services/registry.py`](shared/services/registry.py:40) |
| Service daemon supervisor | [`shared/services/daemon.py`](shared/services/daemon.py:100) |
| Existing interp service (refactor target Phase 1) | [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:1113) |
| Gateway helper (extend, do not replace) | [`shared/intelligence/gateway_config.py`](shared/intelligence/gateway_config.py:124) |
| Dashboard server (default-deny target Phase 5) | [`shared/dashboard/server.py`](shared/dashboard/server.py:191) |
| Dashboard snapshot builder | [`shared/dashboard/snapshot.py`](shared/dashboard/snapshot.py:53) |
| TUI manager (coexists, Phase 5 §F) | [`shared/catalogue/tui_manager.py`](shared/catalogue/tui_manager.py:382) |
| Manage entry point | [`manage_sources.py`](manage_sources.py:1) |
| Dev memory helper | [`shared/utils/mem0_config.py`](shared/utils/mem0_config.py:208) |
| Prompts JSON | [`shared/intelligence/prompts/chart_analysis.json`](shared/intelligence/prompts/chart_analysis.json:1) |
| Atlas (cross-system reference) | [`shared/docs/TICKLES_INFRASTRUCTURE_ATLAS.md`](shared/docs/TICKLES_INFRASTRUCTURE_ATLAS.md:1) |

---

## Hard Rules (do not break)

1. **Resume rule:** Read the plan, find the next unchecked benchmark in the latest phase, execute it. Do **not** start a new phase until the previous Benchmark checklist is fully ticked.
2. **Single-writer policy:** Every table has exactly one writing service. [BC]+[BO] guards enforce this in CI.
3. **Postgres only.** Treat any reference to MySQL in older docs as obsolete.
4. **Default-deny dashboard.** Public paths must be on an explicit allowlist.
5. **No reasoning rewrites post-close.** The [AG] trigger prevents hindsight-bias fixes.
6. **Cost log every LLM call.** No new LLM caller ships without writing to `api_cost_log` with `correlation_id`.
7. **Mem0 dev memory only.** Use [`get_dev_memory(agent="architect")`](shared/utils/mem0_config.py:208) — never write into trading namespaces.
8. **Pre-kickoff blockers first.** [AF][BN][BK][BP][BO] resolve in Phase 0; downstream phases assume they exist.

---

## Resume command

> *"Read [`shared/docs/INTELLIGENCE_UNIFIED_PLAN.md`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:1) in full, then start Phase 0 — Document Contract. Implement the [`companies`](shared/utils/companies.py:1) helper, the migration runner, and the master-schema sync gate per [G][AA][AB][BN]. Tick the Phase 0 Benchmark checklist as you go. Do not start Phase 1 until Phase 0 benchmarks are fully ticked."*
