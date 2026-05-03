"""
Module: test_config_provider
Purpose: Unit tests for the Phase X.6 ConfigSnapshotProvider.

These tests exercise the provider in isolation by monkey-patching
``get_shared_pool`` / ``get_company_pool`` / ``list_active_companies``
so no real database is touched. They cover the full happy-path,
secret redaction, schema-drift defenses, the 250ms budget wrapper,
and per-company fan-out behaviour.

Location: /opt/tickles/shared/tests/test_config_provider.py
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

import pytest

from shared.dashboard import config_provider as cp_mod
from shared.dashboard.config_provider import (
    CONFIG_PROVIDER_TIMEOUT_S,
    ConfigSnapshotProvider,
    SECRET_REDACTION,
    _company_row_to_dict,
    _group_by_namespace,
    _iso,
    _normalise_company_filter,
    _redact,
    _run_with_budget,
    _shared_row_to_dict,
)


# ---------------------------------------------------------------------------
# Stand-in pool / connection objects.
#
# We build a tiny fake of asyncpg's surface area: an ``acquire()`` async
# context manager that yields a connection whose ``fetch(sql, *params)``
# returns a list of dict-like records. Tests can also configure a per-call
# exception to simulate transient DB failures.
# ---------------------------------------------------------------------------


class _FakeRecord(dict):
    """Dict subclass exposing ``rec[key]`` like asyncpg.Record."""


class _FakeConn:
    def __init__(
        self,
        rows: List[_FakeRecord],
        raise_exc: Optional[Exception] = None,
        delay_s: float = 0.0,
    ) -> None:
        self.rows = rows
        self.raise_exc = raise_exc
        self.delay_s = delay_s
        self.calls: List[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *params: Any) -> List[_FakeRecord]:
        self.calls.append((sql, params))
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)
        if self.raise_exc is not None:
            raise self.raise_exc
        return list(self.rows)


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    def acquire(self) -> "_FakePoolAcquireCM":
        return _FakePoolAcquireCM(self.conn)


class _FakePoolAcquireCM:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self.conn

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


def _make_pool(
    rows: List[Dict[str, Any]],
    *,
    raise_exc: Optional[Exception] = None,
    delay_s: float = 0.0,
) -> _FakePool:
    return _FakePool(
        _FakeConn(
            [_FakeRecord(r) for r in rows],
            raise_exc=raise_exc,
            delay_s=delay_s,
        )
    )


# Reusable fixed timestamp for deterministic ISO output.
_TS = datetime(2026, 5, 4, 12, 30, 45, tzinfo=timezone.utc)
_TS_ISO = "2026-05-04T12:30:45+00:00"


# ---------------------------------------------------------------------------
# constants & primitives
# ---------------------------------------------------------------------------


def test_timeout_constant_is_250ms() -> None:
    assert CONFIG_PROVIDER_TIMEOUT_S == pytest.approx(0.25)


def test_secret_redaction_sentinel_is_three_stars() -> None:
    # Many other Tickles surfaces look for this exact value; don't change
    # it without a coordinated migration.
    assert SECRET_REDACTION == "***"


@pytest.mark.parametrize("raw,expected", [
    (None, None),
    ("", None),
    ("   ", None),
    ("all", None),
    ("ALL", None),
    ("All", None),
    (" all ", None),
    ("rubicon", "rubicon"),
    ("  rubicon  ", "rubicon"),
    ("RUBICON", "rubicon"),
    ("Rubicon", "rubicon"),
    ("  RUBICON  ", "rubicon"),
])
def test_normalise_company_filter(raw: Optional[str], expected: Optional[str]) -> None:
    assert _normalise_company_filter(raw) == expected


def test_iso_handles_datetime_none_and_garbage() -> None:
    assert _iso(_TS) == _TS_ISO
    assert _iso(None) is None
    # Non-datetime objects fall back to str(); whatever they stringify
    # to is fine — we just need it to not raise.
    assert _iso(42) == "42"


def test_redact_returns_none_for_null_value_even_when_secret() -> None:
    assert _redact(None, True) is None
    assert _redact(None, False) is None


def test_redact_replaces_value_only_when_is_secret() -> None:
    assert _redact("hunter2", True) == SECRET_REDACTION
    assert _redact("hunter2", False) == "hunter2"


def test_redact_stringifies_non_string_values() -> None:
    # config_value column is TEXT but defensive str() guards against
    # any future schema drift to JSONB / numeric.
    assert _redact(500, False) == "500"
    assert _redact(0.5, False) == "0.5"


# ---------------------------------------------------------------------------
# row -> dict conversions
# ---------------------------------------------------------------------------


def test_shared_row_to_dict_redacts_secret_rows() -> None:
    rec = _FakeRecord({
        "namespace": "auth",
        "config_key": "api_token",
        "config_value": "super-secret",
        "is_secret": True,
        "updated_at": _TS,
    })
    assert _shared_row_to_dict(rec) == {
        "key": "api_token",
        "value": SECRET_REDACTION,
        "is_secret": True,
        "updated_at": _TS_ISO,
    }


def test_shared_row_to_dict_preserves_non_secret_value() -> None:
    rec = _FakeRecord({
        "namespace": "candles",
        "config_key": "retention_1m_days",
        "config_value": "90",
        "is_secret": False,
        "updated_at": _TS,
    })
    assert _shared_row_to_dict(rec) == {
        "key": "retention_1m_days",
        "value": "90",
        "is_secret": False,
        "updated_at": _TS_ISO,
    }


def test_shared_row_to_dict_handles_null_value() -> None:
    rec = _FakeRecord({
        "namespace": "ns",
        "config_key": "k",
        "config_value": None,
        "is_secret": False,
        "updated_at": None,
    })
    assert _shared_row_to_dict(rec) == {
        "key": "k",
        "value": None,
        "is_secret": False,
        "updated_at": None,
    }


def test_company_row_to_dict_preserves_value_verbatim() -> None:
    rec = _FakeRecord({
        "config_key": "trading_capital",
        "config_value": "500",
        "updated_at": _TS,
    })
    assert _company_row_to_dict(rec) == {
        "key": "trading_capital",
        "value": "500",
        "updated_at": _TS_ISO,
    }


def test_company_row_to_dict_keeps_null_value_as_none() -> None:
    rec = _FakeRecord({
        "config_key": "k",
        "config_value": None,
        "updated_at": _TS,
    })
    assert _company_row_to_dict(rec) == {
        "key": "k",
        "value": None,
        "updated_at": _TS_ISO,
    }


# ---------------------------------------------------------------------------
# _group_by_namespace
# ---------------------------------------------------------------------------


def test_group_by_namespace_groups_and_sorts() -> None:
    rows = [
        _FakeRecord({"namespace": "candles", "config_key": "z", "config_value": "1", "is_secret": False, "updated_at": _TS}),
        _FakeRecord({"namespace": "candles", "config_key": "a", "config_value": "2", "is_secret": False, "updated_at": _TS}),
        _FakeRecord({"namespace": "auth",    "config_key": "m", "config_value": "x", "is_secret": True,  "updated_at": _TS}),
    ]
    grouped = _group_by_namespace(rows)
    # Outer keys sorted lexicographically.
    assert list(grouped.keys()) == ["auth", "candles"]
    # Inner rows sorted by key.
    assert [r["key"] for r in grouped["candles"]] == ["a", "z"]
    # Secret rows are still redacted at the row level.
    assert grouped["auth"][0]["value"] == SECRET_REDACTION


def test_group_by_namespace_treats_null_namespace_as_empty_bucket() -> None:
    rows = [
        _FakeRecord({"namespace": None, "config_key": "orphan", "config_value": "v", "is_secret": False, "updated_at": _TS}),
    ]
    grouped = _group_by_namespace(rows)
    assert grouped == {"": [{"key": "orphan", "value": "v", "is_secret": False, "updated_at": _TS_ISO}]}


def test_group_by_namespace_empty_input_returns_empty_dict() -> None:
    assert _group_by_namespace([]) == {}


# ---------------------------------------------------------------------------
# _run_with_budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_with_budget_returns_value_on_success() -> None:
    async def ok() -> int:
        return 7

    out = await _run_with_budget(ok, label="t", fallback=-1)
    assert out == 7


@pytest.mark.asyncio
async def test_run_with_budget_returns_fallback_on_exception() -> None:
    async def boom() -> None:
        raise RuntimeError("kaboom")

    out = await _run_with_budget(boom, label="t", fallback="fb")
    assert out == "fb"


@pytest.mark.asyncio
async def test_run_with_budget_returns_fallback_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Squeeze the budget so we can verify wait_for cancels promptly.
    monkeypatch.setattr(cp_mod, "CONFIG_PROVIDER_TIMEOUT_S", 0.05)

    async def slow() -> int:
        await asyncio.sleep(1.0)
        return 1

    out = await _run_with_budget(slow, label="t", fallback={"empty": True})
    assert out == {"empty": True}


@pytest.mark.asyncio
async def test_run_with_budget_propagates_cancellation() -> None:
    # CancelledError must bubble so the host loop can shut cleanly.
    async def cancelled() -> None:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await _run_with_budget(cancelled, label="t", fallback=None)


# ---------------------------------------------------------------------------
# fetch — happy paths
# ---------------------------------------------------------------------------


@pytest.fixture
def patch_pools(monkeypatch: pytest.MonkeyPatch):
    """Helper that registers a shared pool and per-company pool map.

    Returns a callable that the test populates with pool instances.
    """

    def _install(
        shared: Optional[_FakePool] = None,
        companies: Optional[Dict[str, _FakePool]] = None,
        active: Optional[List[str]] = None,
        active_raises: Optional[Exception] = None,
        shared_raises: Optional[Exception] = None,
        company_pool_raises: Optional[Dict[str, Exception]] = None,
    ) -> None:
        comp_map = companies or {}
        comp_raises = company_pool_raises or {}

        async def fake_get_shared_pool() -> _FakePool:
            if shared_raises is not None:
                raise shared_raises
            assert shared is not None, "test forgot to wire shared pool"
            return shared

        async def fake_get_company_pool(name: str) -> _FakePool:
            if name in comp_raises:
                raise comp_raises[name]
            if name not in comp_map:
                raise RuntimeError(f"no fake pool registered for {name!r}")
            return comp_map[name]

        async def fake_list_active() -> List[str]:
            if active_raises is not None:
                raise active_raises
            return list(active or [])

        monkeypatch.setattr(cp_mod, "get_shared_pool", fake_get_shared_pool)
        monkeypatch.setattr(cp_mod, "get_company_pool", fake_get_company_pool)
        monkeypatch.setattr(cp_mod, "list_active_companies", fake_list_active)

    return _install


@pytest.mark.asyncio
async def test_fetch_returns_grouped_shared_and_companies(patch_pools) -> None:
    shared = _make_pool([
        {"namespace": "candles", "config_key": "retention_1m_days", "config_value": "90", "is_secret": False, "updated_at": _TS},
        {"namespace": "auth",    "config_key": "api_token",         "config_value": "real-token", "is_secret": True, "updated_at": _TS},
    ])
    rubicon = _make_pool([
        {"config_key": "trading_capital", "config_value": "500", "updated_at": _TS},
    ])
    patch_pools(shared=shared, companies={"rubicon": rubicon}, active=["rubicon"])

    out = await ConfigSnapshotProvider().fetch()
    assert set(out["shared"].keys()) == {"auth", "candles"}
    # Secret value must be redacted before leaving the provider.
    assert out["shared"]["auth"][0]["value"] == SECRET_REDACTION
    assert out["shared"]["candles"][0]["key"] == "retention_1m_days"
    assert out["companies"] == {
        "rubicon": [{"key": "trading_capital", "value": "500", "updated_at": _TS_ISO}],
    }


@pytest.mark.asyncio
async def test_fetch_with_company_filter_narrows_to_one(patch_pools) -> None:
    shared = _make_pool([])
    p_a = _make_pool([{"config_key": "k", "config_value": "1", "updated_at": _TS}])
    p_b = _make_pool([{"config_key": "k", "config_value": "2", "updated_at": _TS}])
    patch_pools(shared=shared, companies={"a": p_a, "b": p_b}, active=["a", "b"])

    out = await ConfigSnapshotProvider().fetch(company_filter="b")
    assert list(out["companies"].keys()) == ["b"]
    assert out["companies"]["b"][0]["value"] == "2"


@pytest.mark.asyncio
async def test_fetch_with_unknown_company_returns_empty_subset(patch_pools) -> None:
    shared = _make_pool([])
    patch_pools(shared=shared, companies={}, active=["a"])
    out = await ConfigSnapshotProvider().fetch(company_filter="zzz")
    assert out["companies"] == {}


@pytest.mark.asyncio
async def test_fetch_with_all_filter_treated_as_no_filter(patch_pools) -> None:
    shared = _make_pool([])
    p_a = _make_pool([{"config_key": "k", "config_value": "1", "updated_at": _TS}])
    p_b = _make_pool([{"config_key": "k", "config_value": "2", "updated_at": _TS}])
    patch_pools(shared=shared, companies={"a": p_a, "b": p_b}, active=["a", "b"])
    out = await ConfigSnapshotProvider().fetch(company_filter="all")
    assert set(out["companies"].keys()) == {"a", "b"}


@pytest.mark.asyncio
async def test_fetch_returns_alphabetically_sorted_company_keys(patch_pools) -> None:
    shared = _make_pool([])
    pools = {
        "zulu": _make_pool([{"config_key": "k", "config_value": "z", "updated_at": _TS}]),
        "alpha": _make_pool([{"config_key": "k", "config_value": "a", "updated_at": _TS}]),
        "mike": _make_pool([{"config_key": "k", "config_value": "m", "updated_at": _TS}]),
    }
    patch_pools(shared=shared, companies=pools, active=["zulu", "alpha", "mike"])
    out = await ConfigSnapshotProvider().fetch()
    assert list(out["companies"].keys()) == ["alpha", "mike", "zulu"]


# ---------------------------------------------------------------------------
# fetch — failure / degradation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_returns_empty_snapshot_when_shared_pool_unreachable(patch_pools) -> None:
    patch_pools(
        companies={},
        active=[],
        shared_raises=RuntimeError("connection refused"),
    )
    out = await ConfigSnapshotProvider().fetch()
    # Provider must NEVER raise — fall back to an empty shared bucket
    # but the rest of the snapshot still shapes correctly.
    assert out == {"shared": {}, "companies": {}}


@pytest.mark.asyncio
async def test_fetch_returns_empty_shared_when_query_errors(patch_pools) -> None:
    bad = _make_pool([], raise_exc=RuntimeError("syntax error"))
    patch_pools(shared=bad, companies={}, active=[])
    out = await ConfigSnapshotProvider().fetch()
    assert out["shared"] == {}


@pytest.mark.asyncio
async def test_fetch_empty_when_active_companies_raises(patch_pools) -> None:
    shared = _make_pool([])
    patch_pools(
        shared=shared,
        companies={},
        active_raises=RuntimeError("companies table missing"),
    )
    out = await ConfigSnapshotProvider().fetch()
    # Shared subset still arrives; companies subset degrades to empty.
    assert out["companies"] == {}


@pytest.mark.asyncio
async def test_fetch_skips_company_when_pool_unavailable(patch_pools) -> None:
    shared = _make_pool([])
    good = _make_pool([{"config_key": "k", "config_value": "1", "updated_at": _TS}])
    patch_pools(
        shared=shared,
        companies={"good": good},
        active=["good", "broken"],
        company_pool_raises={"broken": RuntimeError("connection refused")},
    )
    out = await ConfigSnapshotProvider().fetch()
    # 'broken' simply omitted; 'good' still surfaces.
    assert "broken" not in out["companies"]
    assert out["companies"]["good"] == [{"key": "k", "value": "1", "updated_at": _TS_ISO}]


@pytest.mark.asyncio
async def test_fetch_skips_company_when_query_errors(patch_pools) -> None:
    shared = _make_pool([])
    drift = _make_pool([], raise_exc=RuntimeError("relation does not exist"))
    healthy = _make_pool([{"config_key": "k", "config_value": "ok", "updated_at": _TS}])
    patch_pools(
        shared=shared,
        companies={"drift": drift, "healthy": healthy},
        active=["drift", "healthy"],
    )
    out = await ConfigSnapshotProvider().fetch()
    assert "drift" not in out["companies"]
    assert "healthy" in out["companies"]


@pytest.mark.asyncio
async def test_fetch_returns_empty_company_list_for_company_with_no_rows(patch_pools) -> None:
    shared = _make_pool([])
    empty = _make_pool([])  # zero rows but no error
    patch_pools(shared=shared, companies={"empty": empty}, active=["empty"])
    out = await ConfigSnapshotProvider().fetch()
    assert out["companies"] == {"empty": []}


@pytest.mark.asyncio
async def test_fetch_budget_exceeded_returns_fallback(
    monkeypatch: pytest.MonkeyPatch, patch_pools
) -> None:
    # Slow shared pool query — exceeds budget, must NOT raise.
    monkeypatch.setattr(cp_mod, "CONFIG_PROVIDER_TIMEOUT_S", 0.02)
    slow = _make_pool([], delay_s=0.5)
    patch_pools(shared=slow, companies={}, active=[])
    out = await ConfigSnapshotProvider().fetch()
    assert out == {"shared": {}, "companies": {}}


@pytest.mark.asyncio
async def test_fetch_runs_company_reads_concurrently(
    monkeypatch: pytest.MonkeyPatch, patch_pools
) -> None:
    # Two slow company reads should overlap, not serialise. We pick a
    # delay that's small enough to fit inside the budget when running
    # concurrently but would overrun if serialised.
    monkeypatch.setattr(cp_mod, "CONFIG_PROVIDER_TIMEOUT_S", 0.20)
    shared = _make_pool([])
    a = _make_pool([{"config_key": "k", "config_value": "a", "updated_at": _TS}], delay_s=0.07)
    b = _make_pool([{"config_key": "k", "config_value": "b", "updated_at": _TS}], delay_s=0.07)
    patch_pools(shared=shared, companies={"a": a, "b": b}, active=["a", "b"])

    loop = asyncio.get_event_loop()
    t0 = loop.time()
    out = await ConfigSnapshotProvider().fetch()
    elapsed = loop.time() - t0

    assert set(out["companies"].keys()) == {"a", "b"}
    # Serial would be ~0.14s; concurrent should comfortably finish
    # under 0.13s. Allow a generous margin for CI flakiness while
    # still proving the gather() concurrency.
    assert elapsed < 0.13, f"reads appear serialised (elapsed={elapsed:.3f}s)"


# ---------------------------------------------------------------------------
# SQL surface — prove we send the expected statement.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shared_query_orders_by_namespace_then_key(patch_pools) -> None:
    shared = _make_pool([])
    patch_pools(shared=shared, companies={}, active=[])
    await ConfigSnapshotProvider().fetch()
    assert shared.conn.calls, "expected at least one shared query"
    sql, params = shared.conn.calls[0]
    assert "FROM system_config" in sql
    assert "ORDER BY namespace ASC, config_key ASC" in sql
    # Full-table read — no parameters expected.
    assert params == ()


@pytest.mark.asyncio
async def test_shared_query_excludes_discord_hwm_namespace(patch_pools) -> None:
    # discord_hwm rows are operational cursors rewritten by the ingest
    # pipeline — they're not human-tunable config and would pollute the
    # UI with hundreds of opaque IDs.
    shared = _make_pool([])
    patch_pools(shared=shared, companies={}, active=[])
    await ConfigSnapshotProvider().fetch()
    sql, _params = shared.conn.calls[0]
    assert "namespace <> 'discord_hwm'" in sql


@pytest.mark.asyncio
async def test_company_query_orders_by_key(patch_pools) -> None:
    shared = _make_pool([])
    p = _make_pool([])
    patch_pools(shared=shared, companies={"only": p}, active=["only"])
    await ConfigSnapshotProvider().fetch()
    assert p.conn.calls
    sql, params = p.conn.calls[0]
    assert "FROM company_config" in sql
    assert "ORDER BY config_key ASC" in sql
    assert params == ()
