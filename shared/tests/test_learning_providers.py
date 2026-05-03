"""
Module: test_learning_providers
Purpose: Smoke tests for the six Phase Y SnapshotBuilder learning providers.
Location: /opt/tickles/shared/tests/test_learning_providers.py

Coverage:
- Each provider returns a list (or empty list) and never raises into callers.
- 250ms timeout budget is honoured (slow source → empty list, no exception).
- Per-company failures are isolated (one company fails, others still report).
- Source-down (no active companies) returns empty list cleanly.
- Window validation rejects values outside ``{7, 14, 30}``.
- Per-source row LIMIT respects the §4.3 cap.
- §9.1 / §9.2 SQL contract is preserved verbatim in shipped queries.
- No SQL injection surface — every dynamic identifier comes from a
  closed allowlist.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Sequence
from unittest.mock import patch

import pytest

from shared.dashboard import learning_providers as lp
from shared.dashboard.learning_providers import (
    ALLOWED_WINDOWS,
    DEFAULT_FEED_LIMIT_CAP,
    PROMPT_EVOLUTION_LIMIT_CAP,
    PROVIDER_TIMEOUT_S,
    AgentBrainProvider,
    FailedTradesProvider,
    GuardActivityProvider,
    MemoryFeedProvider,
    PromptEvolutionProvider,
    SkillSummaryProvider,
)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeConn:
    """Minimal asyncpg.Connection stand-in for the dashboard providers."""

    def __init__(
        self,
        *,
        rows: Optional[List[Dict[str, Any]]] = None,
        single: Optional[Dict[str, Any]] = None,
        scalar: Any = None,
        delay: float = 0.0,
        fail: bool = False,
    ) -> None:
        self._rows = rows or []
        self._single = single
        self._scalar = scalar
        self._delay = delay
        self._fail = fail
        self.calls: List[tuple[str, Sequence[Any]]] = []

    async def fetch(self, query: str, *args: Any) -> List[Dict[str, Any]]:
        if self._delay:
            await asyncio.sleep(self._delay)
        self.calls.append((query, args))
        if self._fail:
            raise RuntimeError("simulated DB failure")
        return list(self._rows)

    async def fetchrow(self, query: str, *args: Any) -> Optional[Dict[str, Any]]:
        if self._delay:
            await asyncio.sleep(self._delay)
        self.calls.append((query, args))
        if self._fail:
            raise RuntimeError("simulated DB failure")
        return dict(self._single) if self._single is not None else None

    async def fetchval(self, query: str, *args: Any) -> Any:
        if self._delay:
            await asyncio.sleep(self._delay)
        self.calls.append((query, args))
        if self._fail:
            raise RuntimeError("simulated DB failure")
        return self._scalar


class _AcquireCtx:
    """async-with stand-in returning a FakeConn."""

    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> FakeConn:
        return self._conn

    async def __aexit__(self, *exc: Any) -> None:
        return None


class FakePool:
    """Minimal DatabasePool stand-in."""

    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> _AcquireCtx:
        return _AcquireCtx(self._conn)


def _patch_pool_factory(mapping: Dict[str, FakePool]):
    """Patch ``get_company_pool`` to return per-company fake pools.

    Args:
        mapping: company short-name → FakePool instance.

    Returns:
        Context manager via ``patch`` ready to be used in ``with`` blocks.
    """

    async def _fake_get_company_pool(company: str):
        if company not in mapping:
            raise RuntimeError(f"no fake pool for company {company!r}")
        return mapping[company]

    return patch.object(lp, "get_company_pool", side_effect=_fake_get_company_pool)


def _patch_active_companies(names: List[str]):
    """Patch ``list_active_companies`` to return a fixed list."""

    async def _fake() -> List[str]:
        return list(names)

    return patch.object(lp, "list_active_companies", side_effect=_fake)


# --------------------------------------------------------------------------- #
# Module-level invariants
# --------------------------------------------------------------------------- #


def test_allowed_windows_are_seven_fourteen_thirty() -> None:
    assert ALLOWED_WINDOWS == frozenset({7, 14, 30})


def test_provider_timeout_is_250ms_per_phase_y_section_4_3() -> None:
    assert PROVIDER_TIMEOUT_S == pytest.approx(0.25)


def test_default_feed_limit_cap_is_two_hundred() -> None:
    assert DEFAULT_FEED_LIMIT_CAP == 200


def test_validate_window_accepts_7_14_30() -> None:
    for w in (7, 14, 30):
        assert lp._validate_window(w) == w


@pytest.mark.parametrize("bad", [0, 1, 6, 8, 13, 15, 29, 31, 60, -7])
def test_validate_window_rejects_other_values(bad: int) -> None:
    with pytest.raises(ValueError):
        lp._validate_window(bad)


def test_feed_limit_for_window_is_capped() -> None:
    for w in (7, 14, 30):
        n = lp._feed_limit_for_window(w)
        assert 1 <= n <= DEFAULT_FEED_LIMIT_CAP


# --------------------------------------------------------------------------- #
# _resolve_companies
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_resolve_companies_all_returns_full_list() -> None:
    with _patch_active_companies(["alpha", "rubicon"]):
        result = await lp._resolve_companies(None)
        assert result == ["alpha", "rubicon"]
        result = await lp._resolve_companies("all")
        assert result == ["alpha", "rubicon"]


@pytest.mark.asyncio
async def test_resolve_companies_filter_intersects_active_list() -> None:
    with _patch_active_companies(["alpha", "rubicon"]):
        assert await lp._resolve_companies("rubicon") == ["rubicon"]
        # Filter for inactive company → empty list (no leak).
        assert await lp._resolve_companies("ghost") == []


@pytest.mark.asyncio
async def test_resolve_companies_swallows_lookup_failure() -> None:
    async def _boom() -> List[str]:
        raise RuntimeError("companies table down")

    with patch.object(lp, "list_active_companies", side_effect=_boom):
        assert await lp._resolve_companies(None) == []


# --------------------------------------------------------------------------- #
# _run_with_budget
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_run_with_budget_returns_fallback_on_timeout() -> None:
    async def _slow() -> str:
        await asyncio.sleep(1.0)
        return "should not arrive"

    out = await lp._run_with_budget(_slow, label="t", fallback=[])
    assert out == []


@pytest.mark.asyncio
async def test_run_with_budget_returns_fallback_on_exception() -> None:
    async def _boom() -> str:
        raise ValueError("boom")

    out = await lp._run_with_budget(_boom, label="t", fallback="fallback")
    assert out == "fallback"


@pytest.mark.asyncio
async def test_run_with_budget_returns_value_on_success() -> None:
    async def _ok() -> int:
        return 42

    out = await lp._run_with_budget(_ok, label="t", fallback=0)
    assert out == 42


@pytest.mark.asyncio
async def test_run_with_budget_propagates_cancellation() -> None:
    async def _cancel() -> None:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await lp._run_with_budget(_cancel, label="t", fallback=None)


# --------------------------------------------------------------------------- #
# SkillSummaryProvider
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_skill_summary_returns_rows_per_company() -> None:
    rows_alpha = [
        {"actor_id": "trader_a", "company_id": "co1", "skill_score": 0.62, "window_days": 7},
        {"actor_id": "trader_b", "company_id": "co1", "skill_score": None, "window_days": 7},
    ]
    rows_rub = [
        {"actor_id": "trader_c", "company_id": "co2", "skill_score": 0.71, "window_days": 7},
    ]
    pool_alpha = FakePool(FakeConn(rows=rows_alpha))
    pool_rub = FakePool(FakeConn(rows=rows_rub))

    with _patch_active_companies(["alpha", "rubicon"]), _patch_pool_factory(
        {"alpha": pool_alpha, "rubicon": pool_rub}
    ):
        out = await SkillSummaryProvider().fetch(window_days=7)

    assert len(out) == 3
    assert {r["company"] for r in out} == {"alpha", "rubicon"}
    found_b = next(r for r in out if r["actor_id"] == "trader_b")
    assert found_b["skill_score"] is None
    found_a = next(r for r in out if r["actor_id"] == "trader_a")
    assert found_a["skill_score"] == pytest.approx(0.62)


@pytest.mark.asyncio
async def test_skill_summary_isolates_per_company_failure() -> None:
    pool_alpha = FakePool(FakeConn(fail=True))  # simulated DB failure
    pool_rub = FakePool(
        FakeConn(rows=[{"actor_id": "trader_c", "company_id": "co2",
                        "skill_score": 0.5, "window_days": 14}])
    )
    with _patch_active_companies(["alpha", "rubicon"]), _patch_pool_factory(
        {"alpha": pool_alpha, "rubicon": pool_rub}
    ):
        out = await SkillSummaryProvider().fetch(window_days=14)

    assert len(out) == 1
    assert out[0]["company"] == "rubicon"


@pytest.mark.asyncio
async def test_skill_summary_invalid_window_returns_empty() -> None:
    with _patch_active_companies(["rubicon"]):
        out = await SkillSummaryProvider().fetch(window_days=99)
    assert out == []


@pytest.mark.asyncio
async def test_skill_summary_no_active_companies_returns_empty() -> None:
    with _patch_active_companies([]):
        out = await SkillSummaryProvider().fetch(window_days=7)
    assert out == []


@pytest.mark.asyncio
async def test_skill_summary_query_uses_window_view() -> None:
    """The view name embeds window_days; verify each window gets its own view."""
    captured: List[str] = []

    class CapturingConn(FakeConn):
        async def fetch(self, query: str, *args: Any) -> List[Dict[str, Any]]:
            captured.append(query)
            return []

    with _patch_active_companies(["rubicon"]), _patch_pool_factory(
        {"rubicon": FakePool(CapturingConn())}
    ):
        for w in (7, 14, 30):
            await SkillSummaryProvider().fetch(window_days=w)

    assert any("v_actor_skill_7d" in q for q in captured)
    assert any("v_actor_skill_14d" in q for q in captured)
    assert any("v_actor_skill_30d" in q for q in captured)


# --------------------------------------------------------------------------- #
# MemoryFeedProvider
# --------------------------------------------------------------------------- #


def _feed_row(**kw: Any) -> Dict[str, Any]:
    base = {
        "source_kind": "postmortem_actor",
        "tier": "actor",
        "actor": "chart_hacker",
        "company": "rubicon",
        "dimension": "lesson",
        "body": "test body",
        "raw": {"foo": "bar"},
        "ts": "2026-05-03T12:00:00+00:00",
        "source_id": "src-1",
        "correlation_id": "corr-1",
    }
    base.update(kw)
    return base


@pytest.mark.asyncio
async def test_memory_feed_merges_and_sorts_descending() -> None:
    rows_a = [
        _feed_row(ts="2026-05-03T10:00:00+00:00", source_id="a-1"),
        _feed_row(ts="2026-05-03T08:00:00+00:00", source_id="a-2"),
    ]
    rows_b = [
        _feed_row(ts="2026-05-03T11:00:00+00:00", source_id="b-1"),
    ]
    with _patch_active_companies(["alpha", "rubicon"]), _patch_pool_factory(
        {"alpha": FakePool(FakeConn(rows=rows_a)),
         "rubicon": FakePool(FakeConn(rows=rows_b))}
    ):
        out = await MemoryFeedProvider().fetch(window_days=7)

    assert [r["source_id"] for r in out] == ["b-1", "a-1", "a-2"]
    for r in out:
        assert r["window_days"] == 7


@pytest.mark.asyncio
async def test_memory_feed_dimension_filter_threads_through_query() -> None:
    captured: List[tuple[str, Sequence[Any]]] = []

    class CapturingConn(FakeConn):
        async def fetch(self, query: str, *args: Any) -> List[Dict[str, Any]]:
            captured.append((query, args))
            return []

    with _patch_active_companies(["rubicon"]), _patch_pool_factory(
        {"rubicon": FakePool(CapturingConn())}
    ):
        await MemoryFeedProvider().fetch(
            window_days=14, dimension_filter="lesson", limit=42
        )

    assert captured, "expected at least one query"
    q, args = captured[0]
    assert "WHERE dimension = $1" in q
    assert "v_memory_feed_14d" in q
    assert args == ("lesson", 42)


@pytest.mark.asyncio
async def test_memory_feed_invalid_window_returns_empty() -> None:
    with _patch_active_companies(["rubicon"]):
        out = await MemoryFeedProvider().fetch(window_days=21)
    assert out == []


@pytest.mark.asyncio
async def test_memory_feed_caps_total_payload_at_default_limit_cap() -> None:
    huge = [_feed_row(ts=f"2026-05-{(i % 28) + 1:02d}T00:00:00+00:00",
                      source_id=f"x-{i}")
            for i in range(DEFAULT_FEED_LIMIT_CAP * 2)]
    with _patch_active_companies(["rubicon"]), _patch_pool_factory(
        {"rubicon": FakePool(FakeConn(rows=huge))}
    ):
        out = await MemoryFeedProvider().fetch(window_days=7)
    assert len(out) <= DEFAULT_FEED_LIMIT_CAP


@pytest.mark.asyncio
async def test_memory_feed_limit_is_capped_at_default_cap() -> None:
    """User-supplied limit > DEFAULT_FEED_LIMIT_CAP must be clamped down."""
    captured_args: List[Sequence[Any]] = []

    class CapturingConn(FakeConn):
        async def fetch(self, query: str, *args: Any) -> List[Dict[str, Any]]:
            captured_args.append(args)
            return []

    with _patch_active_companies(["rubicon"]), _patch_pool_factory(
        {"rubicon": FakePool(CapturingConn())}
    ):
        await MemoryFeedProvider().fetch(window_days=7, limit=10_000)

    assert captured_args[0][-1] == DEFAULT_FEED_LIMIT_CAP


@pytest.mark.asyncio
async def test_memory_feed_handles_null_ts_in_sort() -> None:
    rows = [_feed_row(ts=None, source_id="null"),
            _feed_row(ts="2026-05-03T10:00:00+00:00", source_id="real")]
    with _patch_active_companies(["rubicon"]), _patch_pool_factory(
        {"rubicon": FakePool(FakeConn(rows=rows))}
    ):
        out = await MemoryFeedProvider().fetch(window_days=7)
    # Real ts should rank higher than NULL.
    assert out[0]["source_id"] == "real"


# --------------------------------------------------------------------------- #
# AgentBrainProvider
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_agent_brain_returns_buckets_per_actor() -> None:
    rows = [
        {"actor_id": "trader_a", "wins": 5, "losses": 2, "breakeven": 1, "total": 8},
        {"actor_id": None, "wins": 0, "losses": 0, "breakeven": 0, "total": 0},
    ]
    with _patch_active_companies(["rubicon"]), _patch_pool_factory(
        {"rubicon": FakePool(FakeConn(rows=rows))}
    ):
        out = await AgentBrainProvider().fetch(window_days=30)

    assert len(out) == 2
    a = next(r for r in out if r["actor_id"] == "trader_a")
    assert a["wins"] == 5 and a["losses"] == 2 and a["breakeven"] == 1 and a["total"] == 8
    assert a["window_days"] == 30


@pytest.mark.asyncio
async def test_agent_brain_sql_uses_adaptive_band() -> None:
    """The §9.1 / §11 Q4 ratified SQL must use GREATEST(COALESCE(...,0), 1.00)."""
    captured: List[str] = []

    class CapturingConn(FakeConn):
        async def fetch(self, query: str, *args: Any) -> List[Dict[str, Any]]:
            captured.append(query)
            return []

    with _patch_active_companies(["rubicon"]), _patch_pool_factory(
        {"rubicon": FakePool(CapturingConn())}
    ):
        await AgentBrainProvider().fetch(window_days=14)

    q = captured[0]
    assert "GREATEST(COALESCE(total_fees_usd, 0)::numeric, 1.00::numeric)" in q
    assert "abs(realized_pnl_usd_final)" in q  # breakeven uses abs()
    assert "tracked_positions" in q


@pytest.mark.asyncio
async def test_agent_brain_invalid_window_returns_empty() -> None:
    with _patch_active_companies(["rubicon"]):
        out = await AgentBrainProvider().fetch(window_days=999)
    assert out == []


# --------------------------------------------------------------------------- #
# GuardActivityProvider
# --------------------------------------------------------------------------- #


def _patch_shared_pool(pool: "FakePool"):
    """Patch ``learning_providers.get_shared_pool`` to return a fake pool.

    Args:
        pool: the FakePool stand-in to return for every call.

    Returns:
        ``unittest.mock.patch`` ready for use in a ``with`` block.
    """

    async def _fake_get_shared_pool() -> "FakePool":
        return pool

    return patch.object(lp, "get_shared_pool", _fake_get_shared_pool)


def _empty_shared_pool() -> "FakePool":
    """Return a FakePool whose ``fetch`` yields no api_cost_log rows.

    Used by every GuardActivity test that does not specifically exercise
    the Signal 3 (``aa_seed_budget_warn``) path. Without this stub the
    real :func:`shared.utils.db.get_shared_pool` would be reached and
    blow the 250ms provider budget.
    """
    return FakePool(FakeConn(rows=[]))


@pytest.mark.asyncio
async def test_guard_activity_emits_no_failed_trades_warning() -> None:
    """20+ closed trades, 0 losing → guard-sidebar warning."""
    conn = FakeConn(
        single={"total": 25, "losing": 0},  # for fetchrow no_fail
        scalar=0,                            # for fetchval pre_f10
    )
    with _patch_active_companies(["rubicon"]), _patch_pool_factory(
        {"rubicon": FakePool(conn)}
    ), _patch_shared_pool(_empty_shared_pool()):
        out = await GuardActivityProvider().fetch()

    kinds = {w["kind"] for w in out}
    assert "no_failed_trades" in kinds
    assert all(w["company"] == "rubicon" for w in out)


@pytest.mark.asyncio
async def test_guard_activity_skips_warning_below_threshold() -> None:
    """<20 closed trades and 0 losing is too small a sample → no warning."""
    conn = FakeConn(single={"total": 5, "losing": 0}, scalar=0)
    with _patch_active_companies(["rubicon"]), _patch_pool_factory(
        {"rubicon": FakePool(conn)}
    ), _patch_shared_pool(_empty_shared_pool()):
        out = await GuardActivityProvider().fetch()
    assert all(w["kind"] != "no_failed_trades" for w in out)


@pytest.mark.asyncio
async def test_guard_activity_emits_pre_f10_warning() -> None:
    conn = FakeConn(single={"total": 5, "losing": 1}, scalar=7)
    with _patch_active_companies(["rubicon"]), _patch_pool_factory(
        {"rubicon": FakePool(conn)}
    ), _patch_shared_pool(_empty_shared_pool()):
        out = await GuardActivityProvider().fetch()
    pre = [w for w in out if w["kind"] == "pre_f10_fees_missing"]
    assert pre and pre[0]["context"]["count"] == 7
    assert pre[0]["severity"] == "info"


@pytest.mark.asyncio
async def test_guard_activity_isolates_per_company_failure() -> None:
    good = FakeConn(single={"total": 25, "losing": 0}, scalar=0)
    bad = FakeConn(fail=True)
    with _patch_active_companies(["alpha", "rubicon"]), _patch_pool_factory(
        {"alpha": FakePool(bad), "rubicon": FakePool(good)}
    ), _patch_shared_pool(_empty_shared_pool()):
        out = await GuardActivityProvider().fetch()
    # rubicon's no_failed_trades warning still surfaces; alpha is silently
    # dropped.
    assert any(w["company"] == "rubicon" for w in out)
    assert not any(w["company"] == "alpha" for w in out)


class _RecordLike:
    """Subscript-only stand-in for ``asyncpg.Record``.

    asyncpg.Record supports ``__getitem__`` but NOT ``.get(...)``. Tests
    that pass plain ``dict`` instances mask any provider code that
    accidentally calls ``.get()``. This class exposes only ``__getitem__``
    so such regressions trip a clean AttributeError.
    """

    def __init__(self, data: Dict[str, Any]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]


@pytest.mark.asyncio
async def test_guard_activity_works_with_record_like_objects() -> None:
    """Regression — provider must not call ``.get()`` on asyncpg Records."""

    class RecordConn(FakeConn):
        async def fetchrow(self, query: str, *args: Any) -> Any:
            return _RecordLike({"total": 30, "losing": 0})

        async def fetchval(self, query: str, *args: Any) -> Any:
            return 0

    with _patch_active_companies(["rubicon"]), _patch_pool_factory(
        {"rubicon": FakePool(RecordConn())}
    ), _patch_shared_pool(_empty_shared_pool()):
        out = await GuardActivityProvider().fetch()

    # If we got here without AttributeError, the regression guard holds.
    assert any(w["kind"] == "no_failed_trades" for w in out)


@pytest.mark.asyncio
async def test_guard_activity_handles_none_fetchrow_safely() -> None:
    """``fetchrow`` returning None must not crash the provider."""
    conn = FakeConn(single=None, scalar=0)
    with _patch_active_companies(["rubicon"]), _patch_pool_factory(
        {"rubicon": FakePool(conn)}
    ), _patch_shared_pool(_empty_shared_pool()):
        out = await GuardActivityProvider().fetch()
    # No warnings emitted because total=0 < min population.
    assert all(w["kind"] != "no_failed_trades" for w in out)


@pytest.mark.asyncio
async def test_guard_activity_emits_aa_seed_budget_warn() -> None:
    """A row in api_cost_log with role='aa_seed_budget_warn' lights the guard."""
    from datetime import datetime as _dt, timezone as _tz

    company_conn = FakeConn(single={"total": 0, "losing": 0}, scalar=0)
    seed_rows = [
        {
            "company_id": "rubicon",
            "context": "spent=$5.00 calls=100",
            "extra": {"calls": 100, "budget_usd": "5.00"},
            "cost_usd": 0,
            "http_status": 429,
            "created_at": _dt(2026, 5, 3, 12, tzinfo=_tz.utc),
        }
    ]
    shared_conn = FakeConn(rows=seed_rows)
    with _patch_active_companies(["rubicon"]), _patch_pool_factory(
        {"rubicon": FakePool(company_conn)}
    ), _patch_shared_pool(FakePool(shared_conn)):
        out = await GuardActivityProvider().fetch()

    seed_warns = [w for w in out if w["kind"] == "aa_seed_budget_warn"]
    assert len(seed_warns) == 1
    w = seed_warns[0]
    assert w["severity"] == "warn"
    assert w["company"] == "rubicon"
    assert "A/A coach seed budget cap reached" in w["message"]
    assert w["context"]["http_status"] == 429
    assert w["context"]["extra"] == {"calls": 100, "budget_usd": "5.00"}


@pytest.mark.asyncio
async def test_guard_activity_aa_seed_filters_to_resolved_companies() -> None:
    """Rows for companies outside the resolved set must be skipped."""
    from datetime import datetime as _dt, timezone as _tz

    company_conn = FakeConn(single={"total": 0, "losing": 0}, scalar=0)
    seed_rows = [
        {
            "company_id": "rubicon",
            "context": "spent=$5.00",
            "extra": None,
            "cost_usd": 0,
            "http_status": 429,
            "created_at": _dt(2026, 5, 3, 12, tzinfo=_tz.utc),
        },
        {
            "company_id": "alpha",  # not in resolved set
            "context": "spent=$5.00",
            "extra": None,
            "cost_usd": 0,
            "http_status": 429,
            "created_at": _dt(2026, 5, 3, 13, tzinfo=_tz.utc),
        },
    ]
    shared_conn = FakeConn(rows=seed_rows)
    with _patch_active_companies(["rubicon"]), _patch_pool_factory(
        {"rubicon": FakePool(company_conn)}
    ), _patch_shared_pool(FakePool(shared_conn)):
        out = await GuardActivityProvider().fetch()

    seed_warns = [w for w in out if w["kind"] == "aa_seed_budget_warn"]
    assert len(seed_warns) == 1
    assert seed_warns[0]["company"] == "rubicon"


@pytest.mark.asyncio
async def test_guard_activity_aa_seed_handles_string_extra() -> None:
    """``extra`` arriving as a JSON string (not jsonb-decoded) must parse."""
    from datetime import datetime as _dt, timezone as _tz

    company_conn = FakeConn(single={"total": 0, "losing": 0}, scalar=0)
    seed_rows = [
        {
            "company_id": "rubicon",
            "context": "cap reached",
            "extra": '{"calls": 50}',
            "cost_usd": 0,
            "http_status": 429,
            "created_at": _dt(2026, 5, 3, 12, tzinfo=_tz.utc),
        }
    ]
    shared_conn = FakeConn(rows=seed_rows)
    with _patch_active_companies(["rubicon"]), _patch_pool_factory(
        {"rubicon": FakePool(company_conn)}
    ), _patch_shared_pool(FakePool(shared_conn)):
        out = await GuardActivityProvider().fetch()

    seed_warns = [w for w in out if w["kind"] == "aa_seed_budget_warn"]
    assert len(seed_warns) == 1
    assert seed_warns[0]["context"]["extra"] == {"calls": 50}


@pytest.mark.asyncio
async def test_guard_activity_aa_seed_isolates_shared_pool_failure() -> None:
    """A failure on the shared pool must not break per-company signals."""
    company_conn = FakeConn(single={"total": 25, "losing": 0}, scalar=0)
    failing_shared = FakeConn(fail=True)
    with _patch_active_companies(["rubicon"]), _patch_pool_factory(
        {"rubicon": FakePool(company_conn)}
    ), _patch_shared_pool(FakePool(failing_shared)):
        out = await GuardActivityProvider().fetch()

    # Signal 1 still emits despite the shared-pool blow-up.
    assert any(w["kind"] == "no_failed_trades" for w in out)
    # No Signal 3 leaked through.
    assert not any(w["kind"] == "aa_seed_budget_warn" for w in out)


@pytest.mark.asyncio
async def test_guard_activity_aa_seed_query_filters_role_and_correlation() -> None:
    """The shared-pool query must hard-pin role + correlation_id values."""
    company_conn = FakeConn(single={"total": 0, "losing": 0}, scalar=0)
    shared_conn = FakeConn(rows=[])
    with _patch_active_companies(["rubicon"]), _patch_pool_factory(
        {"rubicon": FakePool(company_conn)}
    ), _patch_shared_pool(FakePool(shared_conn)):
        await GuardActivityProvider().fetch()

    # Last call on the shared pool is the seed query.
    assert shared_conn.calls, "shared pool was not queried"
    seed_query, _args = shared_conn.calls[-1]
    assert "role = 'aa_seed_budget_warn'" in seed_query
    assert "correlation_id = 'coach_aa_seed'" in seed_query
    assert "interval '30 days'" in seed_query
    assert "ORDER BY created_at DESC" in seed_query


@pytest.mark.asyncio
async def test_memory_feed_sort_handles_datetime_and_none_mix() -> None:
    """Sort must not TypeError when ts is a mix of datetime and None."""
    from datetime import datetime as _dt, timezone as _tz

    rows = [
        _feed_row(ts=None, source_id="null"),
        _feed_row(ts=_dt(2026, 5, 3, 12, tzinfo=_tz.utc), source_id="dt-late"),
        _feed_row(ts=_dt(2026, 5, 3, 8, tzinfo=_tz.utc), source_id="dt-early"),
    ]
    with _patch_active_companies(["rubicon"]), _patch_pool_factory(
        {"rubicon": FakePool(FakeConn(rows=rows))}
    ):
        out = await MemoryFeedProvider().fetch(window_days=7)

    # Most recent datetime first, NULL last.
    ids_in_order = [r["source_id"] for r in out]
    assert ids_in_order[0] == "dt-late"
    assert ids_in_order[-1] == "null"


# --------------------------------------------------------------------------- #
# PromptEvolutionProvider
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_prompt_evolution_returns_promotions() -> None:
    rows = [
        {
            "id": 1, "actor_type": "trader", "actor_id": "chart_hacker",
            "period_end": "2026-05-01", "score_before": 0.50, "score_after": 0.55,
            "delta": 0.05, "components_before": {"a": 1}, "components_after": {"a": 2},
            "note": "prompt_promoted", "logged_at": "2026-05-01T12:00:00+00:00",
        }
    ]
    with _patch_active_companies(["rubicon"]), _patch_pool_factory(
        {"rubicon": FakePool(FakeConn(rows=rows))}
    ):
        out = await PromptEvolutionProvider().fetch()

    assert len(out) == 1
    r = out[0]
    assert r["company"] == "rubicon"
    assert r["note"] == "prompt_promoted"
    assert r["score_after"] == pytest.approx(0.55)


@pytest.mark.asyncio
async def test_prompt_evolution_query_filters_to_promoted_note() -> None:
    captured: List[str] = []

    class CapturingConn(FakeConn):
        async def fetch(self, query: str, *args: Any) -> List[Dict[str, Any]]:
            captured.append(query)
            return []

    with _patch_active_companies(["rubicon"]), _patch_pool_factory(
        {"rubicon": FakePool(CapturingConn())}
    ):
        await PromptEvolutionProvider().fetch(window_days=30, limit=50)

    assert captured
    assert "edge_score_changes" in captured[0]
    assert "note = 'prompt_promoted'" in captured[0]


@pytest.mark.asyncio
async def test_prompt_evolution_caps_limit() -> None:
    captured_args: List[Sequence[Any]] = []

    class CapturingConn(FakeConn):
        async def fetch(self, query: str, *args: Any) -> List[Dict[str, Any]]:
            captured_args.append(args)
            return []

    with _patch_active_companies(["rubicon"]), _patch_pool_factory(
        {"rubicon": FakePool(CapturingConn())}
    ):
        await PromptEvolutionProvider().fetch(window_days=30, limit=10_000)

    assert captured_args[0][-1] == PROMPT_EVOLUTION_LIMIT_CAP


@pytest.mark.asyncio
async def test_prompt_evolution_rejects_non_positive_window() -> None:
    with _patch_active_companies(["rubicon"]):
        assert await PromptEvolutionProvider().fetch(window_days=0) == []
        assert await PromptEvolutionProvider().fetch(window_days=-7) == []


# --------------------------------------------------------------------------- #
# FailedTradesProvider
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_failed_trades_returns_one_row_per_company() -> None:
    a = FakeConn(single={"failed_count": 3, "total_closed": 25})
    b = FakeConn(single={"failed_count": 0, "total_closed": 5})
    with _patch_active_companies(["alpha", "rubicon"]), _patch_pool_factory(
        {"alpha": FakePool(a), "rubicon": FakePool(b)}
    ):
        out = await FailedTradesProvider().fetch(window_days=7)

    assert len(out) == 2
    by_co = {r["company"]: r for r in out}
    assert by_co["alpha"]["failed_count"] == 3
    assert by_co["alpha"]["total_closed"] == 25
    assert by_co["rubicon"]["failed_count"] == 0
    assert all(r["window_days"] == 7 for r in out)


@pytest.mark.asyncio
async def test_failed_trades_sql_matches_section_9_2_contract() -> None:
    captured: List[str] = []

    class CapturingConn(FakeConn):
        async def fetchrow(self, query: str, *args: Any) -> Optional[Dict[str, Any]]:
            captured.append(query)
            return {"failed_count": 0, "total_closed": 0}

    with _patch_active_companies(["rubicon"]), _patch_pool_factory(
        {"rubicon": FakePool(CapturingConn())}
    ):
        await FailedTradesProvider().fetch(window_days=30)

    q = captured[0]
    assert "outcome IN ('sl_hit','expiry')" in q
    assert "GREATEST(COALESCE(total_fees_usd, 0)::numeric, 1.00::numeric)" in q
    assert "tracked_positions" in q


@pytest.mark.asyncio
async def test_failed_trades_invalid_window_returns_empty() -> None:
    with _patch_active_companies(["rubicon"]):
        out = await FailedTradesProvider().fetch(window_days=42)
    assert out == []


@pytest.mark.asyncio
async def test_failed_trades_isolates_per_company_failure() -> None:
    a = FakeConn(fail=True)
    b = FakeConn(single={"failed_count": 1, "total_closed": 10})
    with _patch_active_companies(["alpha", "rubicon"]), _patch_pool_factory(
        {"alpha": FakePool(a), "rubicon": FakePool(b)}
    ):
        out = await FailedTradesProvider().fetch(window_days=7)
    assert len(out) == 1
    assert out[0]["company"] == "rubicon"


# --------------------------------------------------------------------------- #
# Cross-cutting: 250ms budget enforcement
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_skill_summary_returns_empty_when_query_exceeds_budget() -> None:
    """A query slower than 250ms must yield [] without raising."""
    slow = FakeConn(rows=[], delay=PROVIDER_TIMEOUT_S * 4)
    with _patch_active_companies(["rubicon"]), _patch_pool_factory(
        {"rubicon": FakePool(slow)}
    ):
        out = await SkillSummaryProvider().fetch(window_days=7)
    assert out == []


@pytest.mark.asyncio
async def test_failed_trades_returns_empty_when_query_exceeds_budget() -> None:
    slow = FakeConn(single={"failed_count": 0, "total_closed": 0},
                    delay=PROVIDER_TIMEOUT_S * 4)
    with _patch_active_companies(["rubicon"]), _patch_pool_factory(
        {"rubicon": FakePool(slow)}
    ):
        out = await FailedTradesProvider().fetch(window_days=7)
    assert out == []
