# Phase 0 Implementation Plan

**Status:** Ready for implementation  
**Depends on:** All four pre-kickoff blockers resolved (see `.roo/handoffs/2026-04-29-intelligence-unified-plan-handoff.md:192`)  
**Estimated effort:** 1 day (original) + 0.5 day (resolved decision additions) = **1.5 days**  
**Branch:** `feature/intelligence-unified-plan`

---

## 1. Deliverables

### Original Phase 0 (from unified plan)
| # | Component | File | Purpose |
|---|-----------|------|---------|
| 1 | Baseline handoff doc | `.roo/handoffs/2026-04-29-intelligence-unified-plan-baseline.md` | `git rev-parse HEAD`, `pg_dump --schema-only` of `tickles_shared` + one company, redacted `.env`, running services list |
| 2 | Companies enumerator | `shared/utils/companies.py` | `list_active_companies()`, `get_company_dsn()`, `for_each_company()` — single source of truth for per-company iteration |
| 3 | Migration runner | `shared/migration/run_phase_migration.py` | Idempotent phase migration runner with `schema_migrations` ledger |
| 4 | Schema migrations table | `shared/migration/2026_04_29_phase0_schema_migrations.sql` | Tracks which phases applied, when, with checksum |
| 5 | Master schema sync gate | `shared/migration/sync_master_schema.py` | `pg_dump --schema-only` + diff against master templates; CI fails on drift |

### Resolved-Decision Additions (new to Phase 0)
| # | Component | File | Purpose | Decision |
|---|-----------|------|---------|----------|
| 6 | **Loop detector** | `shared/intelligence/loop_detector.py` | Behavioral anomaly detection: stops runaway LLM prompt loops, flags burst patterns | [BP] Reject hard daily caps — detect loops instead |
| 7 | **LLM spend tracker** | `shared/utils/llm_spend_tracker.py` | Total LLM spend dashboard across all roles/companies/agents | [BP] Track total spend, don't gate it |
| 8 | **Writer registry** | `shared/intelligence/writer_registry.py` | `table_writers` registry mapping tables → authorized services; 30-day WARNING mode | [BC] Option B — 30-day grace before enforcement |

---

## 2. Implementation Order

**Day 1 — Morning (4 hours)**
1. **Branch + baseline** (30 min) — `git checkout -b feature/intelligence-unified-plan`, snapshot schemas, redact `.env`
2. **Schema migrations table** (15 min) — Run `2026_04_29_phase0_schema_migrations.sql` against `tickles_shared`
3. **Companies helper** (45 min) — Write `shared/utils/companies.py` with 5-minute cache, fallback to `ACTIVE_COMPANIES` env
4. **Migration runner** (60 min) — Write `shared/migration/run_phase_migration.py` with `--phase N --target shared|company|all_companies`
5. **Master schema sync gate** (60 min) — Write `shared/migration/sync_master_schema.py` with `pg_dump --schema-only` + `diff`
6. **Writer registry table + module** (60 min) — Create `table_writers` table, write `shared/intelligence/writer_registry.py` with WARNING-mode registration

**Day 1 — Afternoon (4 hours)**
7. **Loop detector** (90 min) — Write `shared/intelligence/loop_detector.py` with `CallFingerprint` + `LoopDetector` classes
8. **LLM spend tracker** (90 min) — Write `shared/utils/llm_spend_tracker.py` with daily/weekly/monthly rollups
9. **Integration wiring** (60 min) — Wire loop detector into `gateway_config.py` call sites; wire spend tracker into `api_cost_log` write path
10. **Tests + benchmark** (60 min) — `pytest shared/tests/test_loop_detector.py shared/tests/test_llm_spend_tracker.py shared/tests/test_writer_registry.py shared/tests/test_companies.py`

---

## 3. Component Specifications

### 3.1 Companies Helper (`shared/utils/companies.py`)

```python
"""Canonical company enumeration helper.

All per-company iteration (Phases 7, 8, 10, 11) MUST use this module.
Do NOT reinvent _discover_companies() in another file.
"""
from __future__ import annotations
import asyncio
import os
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
    Caches for 5 minutes to avoid N+1 queries in loops.
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
    """Build a company DSN from TICKLES_DB_DSN_TEMPLATE."""
    base = os.environ["TICKLES_DB_DSN_TEMPLATE"]  # "postgresql://...@host:5432/{db}"
    return base.format(db=f"tickles_{company}")


async def for_each_company(
    fn: Callable[[str, asyncpg.Connection], Awaitable[T]],
) -> dict[str, T | Exception]:
    """Fan-out a function across all active companies.

    Returns {company: result_or_exception}.  Exceptions are captured,
    not raised, so one failing company does not abort the rest.
    """
    names = await list_active_companies()

    async def _one(c: str):
        dsn = await get_company_dsn(c)
        async with asyncpg.connect(dsn) as conn:
            return await fn(c, conn)

    results = await asyncio.gather(*[_one(c) for c in names], return_exceptions=True)
    return dict(zip(names, results))
```

**Why:** `shared/intelligence/performance_scorer.py:549` has `_discover_companies()` duplicated privately. Promoting it to shared removes silent divergence risk.

---

### 3.2 Migration Runner (`shared/migration/run_phase_migration.py`)

**CLI:** `python -m shared.migration.run_phase_migration --phase 0 --target all_companies`

**Behavior:**
1. Reads `shared/migration/2026_04_29_phase{N}_*.sql` files
2. Computes SHA-256 checksum
3. Checks `tickles_shared.public.schema_migrations` — if `phase=N` exists with matching checksum, skip (idempotent)
4. If checksum differs, raise `MigrationChecksumMismatch`
5. If missing, execute SQL in a transaction, insert `schema_migrations` row
6. For `--target all_companies`, fan out to every company DB via `for_each_company`

**Schema migrations table:**
```sql
-- shared/migration/2026_04_29_phase0_schema_migrations.sql
CREATE TABLE IF NOT EXISTS schema_migrations (
    phase       INT  PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    checksum    CHAR(64) NOT NULL,
    target      TEXT NOT NULL DEFAULT 'shared',  -- 'shared' | 'rubicon' | 'all_companies'
    applied_by  TEXT NOT NULL DEFAULT CURRENT_USER
);

-- Seed Phase 0 itself (meta)
INSERT INTO schema_migrations (phase, checksum, target)
VALUES (0, 'phase0_bootstrap', 'shared')
ON CONFLICT (phase) DO NOTHING;
```

---

### 3.3 Master Schema Sync Gate (`shared/migration/sync_master_schema.py`)

**CLI:** `python -m shared.migration.sync_master_schema --check`

**Behavior:**
1. `pg_dump --schema-only -d tickles_shared` → temp file
2. Diff against `shared/migration/tickles_shared_pg.sql`
3. If any difference → print diff, exit code 1 (CI fails)
4. Same for `tickles_company_pg.sql` against a fully-migrated company DB

**CI integration:** `.github/workflows/schema-drift.yml` runs this on every PR.

---

### 3.4 Writer Registry (`shared/intelligence/writer_registry.py` + SQL)

**Decision:** [BC] Option B — 30-day grace period. Registry operates in **WARNING** mode until 2026-05-30, then CI flips to **ENFORCE**.

**SQL:**
```sql
-- shared/intelligence/migrations/2026_04_29_phase0_writer_registry.sql
CREATE TABLE IF NOT EXISTS public.table_writers (
    table_name              TEXT PRIMARY KEY,
    allowed_writer_services TEXT[] NOT NULL,
    notes                   TEXT,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed initial mappings (verified against 232 INSERT INTO sites across codebase)
INSERT INTO public.table_writers (table_name, allowed_writer_services, notes) VALUES
  ('tracked_positions',     ARRAY['interpretation_service','surgeon2_trader','chart_hacker_trader','postmortem_service']::TEXT[], 'postmortem updates close fields only'),
  ('position_postmortems',  ARRAY['postmortem_service']::TEXT[], 'sole writer'),
  ('agent_opinions',        ARRAY['chart_hacker_opinion_service']::TEXT[], 'sole writer'),
  ('signal_interpretations',ARRAY['interpretation_service']::TEXT[], 'sole writer'),
  ('api_cost_log',          ARRAY['gateway_config','mem0_config','memu_client','discord_collector','telegram_collector','rss_collector']::TEXT[], 'multi-writer OK — audit table'),
  ('schema_migrations',     ARRAY['run_phase_migration']::TEXT[], 'sole writer')
ON CONFLICT (table_name) DO NOTHING;
```

**Python:**
```python
"""Writer-domain registry — single-writer policy enforcement.

Phase 0–9: WARNING mode (logs unauthorised writes, does not block).
Phase R / 2026-05-30: ENFORCE mode (CI gate fails builds on unauthorised writers).
"""
from __future__ import annotations
import logging
import os
from datetime import datetime, timezone
from typing import Optional
import asyncpg
from shared.utils.db import get_shared_pool

logger = logging.getLogger(__name__)

# Phase 0–9: WARNING.  Phase R: ENFORCE.
_MODE = os.getenv("WRITER_REGISTRY_MODE", "WARNING").upper()
_CUTOVER_DATE = os.getenv("WRITER_REGISTRY_CUTOVER", "2026-05-30")


async def register_writer(
    table_name: str,
    service_name: str,
    conn: asyncpg.Connection | None = None,
) -> None:
    """Idempotently add service_name to the allowed-writers list for table_name."""
    pool = await get_shared_pool()
    async with pool.acquire() as c:
        await c.execute(
            """
            INSERT INTO table_writers (table_name, allowed_writer_services, notes)
            VALUES ($1, ARRAY[$2]::TEXT[], $3)
            ON CONFLICT (table_name)
            DO UPDATE SET
                allowed_writer_services = array_distinct(table_writers.allowed_writer_services || EXCLUDED.allowed_writer_services),
                updated_at = NOW(),
                notes = COALESCE(table_writers.notes, '') || E'\n' || EXCLUDED.notes
            """,
            table_name,
            service_name,
            f"registered by {service_name} at {datetime.now(timezone.utc).isoformat()}",
        )


async def assert_authorised(
    table_name: str,
    service_name: str,
    conn: asyncpg.Connection | None = None,
) -> bool:
    """Return True if service_name is authorised to write to table_name.

    In WARNING mode: logs a loud warning but returns True.
    In ENFORCE mode: raises RuntimeError on unauthorised write.
    """
    pool = await get_shared_pool()
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT allowed_writer_services FROM table_writers WHERE table_name = $1",
            table_name,
        )
    if row is None:
        msg = f"[WRITER-REGISTRY] table '{table_name}' has NO registered writers. " \
              f"'{service_name}' is attempting to write."
        if _MODE == "ENFORCE":
            raise RuntimeError(msg)
        logger.warning(msg + " (WARNING mode — allowing)")
        return True

    allowed = set(row["allowed_writer_services"])
    if service_name in allowed:
        return True

    msg = f"[WRITER-REGISTRY] '{service_name}' is NOT authorised for '{table_name}'. " \
          f"Allowed: {sorted(allowed)}."
    if _MODE == "ENFORCE":
        raise RuntimeError(msg)
    logger.warning(msg + " (WARNING mode — allowing)")
    return True
```

**Why:** 232 `INSERT INTO` sites found across the codebase. A 30-day warning period lets us audit every site and fix unauthorised writes before enforcement.

---

### 3.5 Loop Detector (`shared/intelligence/loop_detector.py`)

**Decision:** [BP] Reject hard daily USD caps. Detect behavioral anomalies (loops, bursts) instead.

```python
"""Loop detection + frequency analysis for LLM calls.

Replaces the hard budget-cap approach with behavioral anomaly detection.
A runaway prompt loop with no detection can drain the monthly LLM budget
in hours. This module catches loops and repeated identical calls.

Wired into GatewayConfig.call_vision_llm() and chat_completion() BEFORE
the actual API call. If a loop is detected, raises LoopDetectedError
which the caller can catch and back off.
"""
from __future__ import annotations
import hashlib
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


class LoopDetectedError(Exception):
    """Raised when a loop or burst is detected. Caller should back off."""
    pass


class LoopDetector:
    """In-memory sliding-window tracker. Stateless across restarts —
    persistent audit trail lives in api_cost_log.
    """

    def __init__(self, window_seconds: int = _LOOP_WINDOW_SECONDS):
        self.window = timedelta(seconds=window_seconds)
        self._calls: List[CallFingerprint] = []          # chronological
        self._identical_counts: Dict[str, int] = defaultdict(int)
        self._similar_counts: Dict[str, int] = defaultdict(int)

    def record(self, fp: CallFingerprint) -> Tuple[bool, Optional[str]]:
        """Record a call and return (is_anomaly, reason_or_None).

        Raises LoopDetectedError if the call breaches a threshold.
        The caller (GatewayConfig) catches this and returns a cached
        response or backs off exponentially.
        """
        now = fp.ts
        cutoff = now - self.window

        # Evict old calls outside the window
        while self._calls and self._calls[0].ts < cutoff:
            old = self._calls.pop(0)
            self._identical_counts[old.identity_key()] -= 1
            self._similar_counts[old.similarity_key()] -= 1

        # Record new call
        self._calls.append(fp)
        self._identical_counts[fp.identity_key()] += 1
        self._similar_counts[fp.similarity_key()] += 1

        # Check thresholds
        identical_count = self._identical_counts[fp.identity_key()]
        similar_count = self._similar_counts[fp.similarity_key()]

        if identical_count >= _LOOP_IDENTICAL_THRESHOLD:
            msg = (
                f"LOOP DETECTED: {identical_count} identical calls to "
                f"{fp.identity_key()} in {window_seconds}s. "
                f"Threshold={_LOOP_IDENTICAL_THRESHOLD}. Backing off."
            )
            logger.error(msg)
            raise LoopDetectedError(msg)

        if similar_count >= _LOOP_SIMILAR_THRESHOLD:
            msg = (
                f"BURST DETECTED: {similar_count} similar calls for "
                f"{fp.similarity_key()} in {window_seconds}s. "
                f"Threshold={_LOOP_SIMILAR_THRESHOLD}."
            )
            logger.warning(msg)
            return True, msg

        # Frequency check: calls per minute for this similarity key
        recent = [c for c in self._calls if c.similarity_key() == fp.similarity_key() and c.ts > now - timedelta(minutes=1)]
        if len(recent) >= _FREQUENCY_BURST_THRESHOLD:
            msg = (
                f"FREQUENCY BURST: {len(recent)} calls/minute for "
                f"{fp.similarity_key()}. Threshold={_FREQUENCY_BURST_THRESHOLD}."
            )
            logger.warning(msg)
            return True, msg

        return False, None

    @staticmethod
    def hash_prompt(prompt: str) -> str:
        """SHA-256 of first 4 KB of prompt text."""
        return hashlib.sha256(prompt[:4096].encode()).hexdigest()[:16]
```

**Integration point:** `shared/intelligence/gateway_config.py` — before every `call_vision_llm()` and `chat_completion()`, build a `CallFingerprint`, call `loop_detector.record()`, catch `LoopDetectedError` and return a cached/empty response with exponential backoff.

---

### 3.6 LLM Spend Tracker (`shared/utils/llm_spend_tracker.py`)

**Decision:** [BP] Track total LLM spend across the entire app. No hard caps — visibility first.

```python
"""Total LLM spend tracker across all roles, companies, and agents.

Reads from api_cost_log (which every API caller writes to) and provides
roll-up views: daily, weekly, monthly, by role, by company, by agent.

No hard caps — this is a visibility/dashboard tool. The loop detector
handles runaway spend prevention.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
import asyncpg
from shared.utils.db import get_shared_pool


@dataclass
class SpendSummary:
    period: str                    # 'day', 'week', 'month'
    period_start: datetime
    total_usd: float
    by_role: Dict[str, float]
    by_company: Dict[str, float]
    by_agent: Dict[str, float]
    call_count: int
    total_tokens: int


async def get_spend_summary(
    period: str = "day",
    company_id: Optional[str] = None,
    role: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> SpendSummary:
    """Roll up api_cost_log for the given period.

    period: 'day' | 'week' | 'month' — looks back 1 period from now.
    Filters are ANDed (company_id AND role AND agent_id).
    """
    now = datetime.now(timezone.utc)
    if period == "day":
        start = now - timedelta(days=1)
    elif period == "week":
        start = now - timedelta(weeks=1)
    elif period == "month":
        start = now - timedelta(days=30)
    else:
        raise ValueError(f"Unknown period: {period}")

    pool = await get_shared_pool()
    async with pool.acquire() as conn:
        # Base query with filters
        where_clauses = ["created_at >= $1"]
        params: List = [start]
        param_idx = 2

        if company_id:
            where_clauses.append(f"company_id = ${param_idx}")
            params.append(company_id)
            param_idx += 1
        if role:
            where_clauses.append(f"role = ${param_idx}")
            params.append(role)
            param_idx += 1
        if agent_id:
            where_clauses.append(f"agent_id = ${param_idx}")
            params.append(agent_id)
            param_idx += 1

        where_sql = " AND ".join(where_clauses)

        row = await conn.fetchrow(
            f"""
            SELECT
                COALESCE(SUM(cost_usd), 0) as total_usd,
                COUNT(*) as call_count,
                COALESCE(SUM(tokens_in + tokens_out), 0) as total_tokens
            FROM api_cost_log
            WHERE {where_sql}
            """,
            *params,
        )

        # By-role breakdown
        role_rows = await conn.fetch(
            f"""
            SELECT role, COALESCE(SUM(cost_usd), 0) as usd
            FROM api_cost_log
            WHERE {where_sql}
            GROUP BY role
            ORDER BY usd DESC
            """,
            *params,
        )

        # By-company breakdown
        company_rows = await conn.fetch(
            f"""
            SELECT company_id, COALESCE(SUM(cost_usd), 0) as usd
            FROM api_cost_log
            WHERE {where_sql}
            GROUP BY company_id
            ORDER BY usd DESC
            """,
            *params,
        )

        # By-agent breakdown
        agent_rows = await conn.fetch(
            f"""
            SELECT agent_id, COALESCE(SUM(cost_usd), 0) as usd
            FROM api_cost_log
            WHERE {where_sql} AND agent_id IS NOT NULL
            GROUP BY agent_id
            ORDER BY usd DESC
            """,
            *params,
        )

    return SpendSummary(
        period=period,
        period_start=start,
        total_usd=float(row["total_usd"]),
        by_role={r["role"]: float(r["usd"]) for r in role_rows},
        by_company={r["company_id"] or "unknown": float(r["usd"]) for r in company_rows},
        by_agent={r["agent_id"]: float(r["usd"]) for r in agent_rows},
        call_count=int(row["call_count"]),
        total_tokens=int(row["total_tokens"]),
    )


async def get_daily_spend_series(
    days: int = 30,
    company_id: Optional[str] = None,
) -> List[Dict]:
    """Return daily spend for the last N days, one row per day.

    Format: [{date: '2026-04-29', total_usd: 12.34, call_count: 56}, ...]
    """
    start = datetime.now(timezone.utc) - timedelta(days=days)
    pool = await get_shared_pool()
    async with pool.acquire() as conn:
        company_filter = "AND company_id = $2" if company_id else ""
        params = [start, company_id] if company_id else [start]

        rows = await conn.fetch(
            f"""
            SELECT
                (created_at AT TIME ZONE 'UTC')::date as day,
                COALESCE(SUM(cost_usd), 0) as total_usd,
                COUNT(*) as call_count
            FROM api_cost_log
            WHERE created_at >= $1 {company_filter}
            GROUP BY day
            ORDER BY day DESC
            """,
            *params,
        )
    return [
        {
            "date": str(r["day"]),
            "total_usd": float(r["total_usd"]),
            "call_count": int(r["call_count"]),
        }
        for r in rows
    ]
```

**Dashboard integration:** Phase L (Dashboard rebuild) will call `get_spend_summary()` and `get_daily_spend_series()` to render the "Stats overview strip" on every page.

---

## 4. Risks & Mitigations

| # | Risk | Mitigation |
|---|------|------------|
| 1 | `companies` table does not exist yet | Fallback to `ACTIVE_COMPANIES` env var; Phase 2 creates the table |
| 2 | Writer registry WARNING mode ignored by developers | Loud `logger.warning()` with `[WRITER-REGISTRY]` prefix; grep-able; CI gate counts warnings |
| 3 | Loop detector in-memory state lost on restart | Acceptable — persistent audit in `api_cost_log`; restart resets window, which is conservative (safer) |
| 4 | Loop detector false-positive on legitimate burst | Tunable thresholds via env vars; default thresholds are conservative (3 identical, 5 similar, 10/min) |
| 5 | Spend tracker query slow on large `api_cost_log` | Indexes already planned in Phase 1 (`idx_api_cost_log_role_company_day`); if still slow, add materialized view in Phase R |
| 6 | 30-day grace period forgotten | Cutover date `2026-05-30` hardcoded in module + env var; calendar reminder in handoff doc |
| 7 | Migration runner fails mid-way | Each phase SQL is wrapped in a transaction; `schema_migrations` row inserted only on commit; re-run is idempotent |
| 8 | Master schema sync false-positive on ordering | `pg_dump` output is deterministic with `--schema-only`; diff ignores whitespace/comments |

---

## 5. Benchmark Checklist

Run these in order before declaring Phase 0 complete:

- [ ] `git checkout -b feature/intelligence-unified-plan` succeeds
- [ ] `pg_dump --schema-only -d tickles_shared > baseline_shared.sql` produces readable SQL
- [ ] `python -c "from shared.utils.companies import list_active_companies; import asyncio; print(asyncio.run(list_active_companies()))"` returns `['rubicon']` (or env fallback)
- [ ] `python -m shared.migration.run_phase_migration --phase 0 --target shared` exits 0, idempotent re-run exits 0
- [ ] `python -m shared.migration.sync_master_schema --check` exits 0 (no drift on fresh baseline)
- [ ] `python -c "from shared.intelligence.writer_registry import assert_authorised; import asyncio; print(asyncio.run(assert_authorised('tracked_positions', 'interpretation_service')))"` returns `True`
- [ ] `python -c "from shared.intelligence.writer_registry import assert_authorised; import asyncio; print(asyncio.run(assert_authorised('tracked_positions', 'hacker_service')))"` logs WARNING (not error) in WARNING mode
- [ ] `python -c "from shared.intelligence.loop_detector import LoopDetector, CallFingerprint; d=LoopDetector(); fp=CallFingerprint('vision','gpt-4','abc123',None,'rubicon'); [d.record(fp) for _ in range(3)]"` raises `LoopDetectedError` on 3rd call
- [ ] `python -c "from shared.utils.llm_spend_tracker import get_spend_summary; import asyncio; print(asyncio.run(get_spend_summary('day')))"` returns `SpendSummary` with `total_usd >= 0`
- [ ] `pytest shared/tests/test_companies.py shared/tests/test_writer_registry.py shared/tests/test_loop_detector.py shared/tests/test_llm_spend_tracker.py` — all green
- [ ] `.roo/handoffs/2026-04-29-intelligence-unified-plan-baseline.md` exists with git SHA, schema dumps, service list
- [ ] `schema_migrations` table has row `phase=0, target='shared'`
- [ ] `table_writers` table has ≥ 6 seeded rows

---

## 6. What Comes Next

After Phase 0 benchmarks are ticked:

1. **Phase 1** — Universal LLM gateway + cost log + loop detection integration (1.5 days)
2. **Phase 2** — Schema unification: `signal_interpretations`, `tracked_positions`, `position_postmortems` (1.5 days)
3. Continue in plan order: 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11 → L → R

**Resume command for next session:**
> "Read `shared/docs/PHASE0_IMPLEMENTATION_PLAN.md`, start with Benchmark checklist item 1, tick them in order. Do not start Phase 1 until all Phase 0 benchmarks are green."
