"""
Module: test_news_provider
Purpose: Smoke tests for the Phase X.4 News Feed provider.
Location: /opt/tickles/shared/tests/test_news_provider.py

Coverage:
- Module-level constants match the §11 / Phase X.4 contract.
- Window / source / limit validation behaviour (graceful, no 5xx).
- Per-company narrowing via ``trades.instrument_id`` → shared
  ``instruments.symbol`` resolution.
- Empty-state handling (no rows, no companies, no instruments).
- 250ms budget honoured (slow query → empty list, no exception).
- Sort order (``collected_at DESC``) preserved by the SQL contract.
- Source filter is parameterised (no string interpolation).
- ``has_media`` tri-state filter threads correctly into the SQL.
- Content truncation at ``PREVIEW_CHARS``.
- Malformed ``instruments`` JSON degrades to ``[]`` instead of crashing.
- DB connection failure on either pool returns ``[]`` cleanly.
- Limit clamping to ``[1, NEWS_LIMIT_CAP]``.

Conventions: mirrors ``test_learning_providers.py`` so the FakeConn /
FakePool plumbing is consistent across the dashboard test suite.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Sequence
from unittest.mock import patch

import pytest

from shared.dashboard import news_provider as np
from shared.dashboard.news_provider import (
    ALLOWED_NEWS_WINDOWS,
    ALLOWED_SOURCE_KINDS,
    NEWS_DEFAULT_LIMIT,
    NEWS_LIMIT_CAP,
    NEWS_PROVIDER_TIMEOUT_S,
    NewsFeedProvider,
)


# --------------------------------------------------------------------------- #
# Fakes — mirror test_learning_providers.py
# --------------------------------------------------------------------------- #


class FakeConn:
    """Minimal asyncpg.Connection stand-in for the news provider."""

    def __init__(
        self,
        *,
        rows: Optional[List[Dict[str, Any]]] = None,
        delay: float = 0.0,
        fail: bool = False,
    ) -> None:
        self._rows = rows or []
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


def _patch_shared_pool(pool: FakePool):
    """Patch ``get_shared_pool`` on the news_provider module."""

    async def _fake() -> FakePool:
        return pool

    return patch.object(np, "get_shared_pool", side_effect=_fake)


def _patch_company_pool(mapping: Dict[str, FakePool]):
    """Patch ``get_company_pool`` to return per-company fake pools."""

    async def _fake(company: str) -> FakePool:
        if company not in mapping:
            raise RuntimeError(f"no fake pool for company {company!r}")
        return mapping[company]

    return patch.object(np, "get_company_pool", side_effect=_fake)


def _patch_active_companies(names: List[str]):
    """Patch ``list_active_companies`` to return a fixed list."""

    async def _fake() -> List[str]:
        return list(names)

    return patch.object(np, "list_active_companies", side_effect=_fake)


def _news_row(
    *,
    rid: int = 1,
    source: str = "discord",
    headline: str = "BTC breaks resistance",
    content: Optional[str] = "BTC just punched through 50k.",
    instruments: Optional[List[str]] = None,
    sentiment: Optional[str] = "positive",
    has_media: bool = False,
    media_count: int = 0,
    channel_name: Optional[str] = "alpha-room",
    author: Optional[str] = "trader_x",
    published_at: Optional[Any] = None,
    collected_at: Optional[Any] = None,
    enrichment_status: str = "enriched",
) -> Dict[str, Any]:
    """Build a mock news_items row with reasonable defaults."""
    from datetime import datetime, timezone

    return {
        "id": rid,
        "source": source,
        "headline": headline,
        "content": content,
        "instruments": list(instruments) if instruments is not None else ["BTC/USDT"],
        "sentiment": sentiment,
        "has_media": has_media,
        "media_count": media_count,
        "channel_name": channel_name,
        "author": author,
        "published_at": published_at,
        "collected_at": collected_at or datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc),
        "enrichment_status": enrichment_status,
    }


# --------------------------------------------------------------------------- #
# Module-level invariants
# --------------------------------------------------------------------------- #


def test_allowed_windows_are_one_seven_thirty() -> None:
    """News tab supports 24h / 7d / 30d → {1, 7, 30} day windows."""
    assert ALLOWED_NEWS_WINDOWS == frozenset({1, 7, 30})


def test_provider_timeout_is_250ms_per_phase_y_section_4_3() -> None:
    """Hard 250ms budget — same as the Learning providers."""
    assert NEWS_PROVIDER_TIMEOUT_S == pytest.approx(0.25)


def test_news_limit_cap_and_default() -> None:
    """The cap defends against pathological pagination."""
    assert NEWS_LIMIT_CAP == 500
    assert NEWS_DEFAULT_LIMIT == 100
    assert NEWS_DEFAULT_LIMIT < NEWS_LIMIT_CAP


def test_allowed_source_kinds_match_route_layer() -> None:
    """Source allow-list is the single source of truth shared with the route."""
    assert ALLOWED_SOURCE_KINDS == frozenset(
        {"discord", "telegram", "twitter", "rss", "web", "manual"}
    )


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("good", [1, 7, 30])
def test_validate_window_accepts_allowed(good: int) -> None:
    assert np._validate_window(good) == good


@pytest.mark.parametrize("bad", [0, 2, 14, 31, -1, 365])
def test_validate_window_rejects_others(bad: int) -> None:
    with pytest.raises(ValueError):
        np._validate_window(bad)


def test_validate_source_lowercases_and_passes_allowed() -> None:
    assert np._validate_source("Discord") == "discord"
    assert np._validate_source("RSS") == "rss"


def test_validate_source_returns_none_for_unknown_or_empty() -> None:
    assert np._validate_source(None) is None
    assert np._validate_source("") is None
    assert np._validate_source("not-a-real-source") is None
    assert np._validate_source("   ") is None


@pytest.mark.parametrize("limit_in,expected", [
    (None, NEWS_DEFAULT_LIMIT),
    (0, 1),
    (1, 1),
    (50, 50),
    (NEWS_LIMIT_CAP, NEWS_LIMIT_CAP),
    (NEWS_LIMIT_CAP + 1, NEWS_LIMIT_CAP),
    (10_000, NEWS_LIMIT_CAP),
    (-5, 1),
])
def test_normalise_limit_clamps(limit_in: Optional[int], expected: int) -> None:
    assert np._normalise_limit(limit_in) == expected


def test_normalise_limit_handles_garbage() -> None:
    """Non-numeric strings fall back to default rather than raising."""
    assert np._normalise_limit("abc") == NEWS_DEFAULT_LIMIT  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# _resolve_company
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_resolve_company_returns_none_for_all_or_empty() -> None:
    assert await np._resolve_company(None) is None
    assert await np._resolve_company("all") is None
    assert await np._resolve_company("ALL") is None


@pytest.mark.asyncio
async def test_resolve_company_returns_name_when_active() -> None:
    with _patch_active_companies(["alpha", "rubicon"]):
        assert await np._resolve_company("alpha") == "alpha"


@pytest.mark.asyncio
async def test_resolve_company_returns_none_for_unknown_name() -> None:
    """Typos must NOT 5xx — degrade silently to no-filter."""
    with _patch_active_companies(["alpha"]):
        assert await np._resolve_company("ghost") is None


@pytest.mark.asyncio
async def test_resolve_company_swallows_active_lookup_failure() -> None:
    async def _boom() -> List[str]:
        raise RuntimeError("active lookup down")

    with patch.object(np, "list_active_companies", side_effect=_boom):
        assert await np._resolve_company("alpha") is None


# --------------------------------------------------------------------------- #
# _company_instruments — trades → instruments resolution
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_company_instruments_resolves_via_trades_and_shared() -> None:
    """Happy path: company DB returns IDs, shared DB resolves to symbols."""
    company_conn = FakeConn(rows=[{"instrument_id": 11}, {"instrument_id": 22}])
    shared_conn = FakeConn(rows=[{"symbol": "BTC/USDT"}, {"symbol": "ETH/USDT"}])
    with _patch_company_pool({"alpha": FakePool(company_conn)}), \
         _patch_shared_pool(FakePool(shared_conn)):
        symbols = await np._company_instruments("alpha")
    assert symbols == ["BTC/USDT", "ETH/USDT"]


@pytest.mark.asyncio
async def test_company_instruments_empty_when_company_has_no_trades() -> None:
    """A company that has never traded → no narrowing list."""
    company_conn = FakeConn(rows=[])
    with _patch_company_pool({"alpha": FakePool(company_conn)}):
        symbols = await np._company_instruments("alpha")
    assert symbols == []


@pytest.mark.asyncio
async def test_company_instruments_empty_when_company_pool_fails() -> None:
    """Company DB unreachable → empty list, no raise."""
    with _patch_company_pool({}):  # any company → RuntimeError
        symbols = await np._company_instruments("alpha")
    assert symbols == []


@pytest.mark.asyncio
async def test_company_instruments_empty_when_shared_pool_fails() -> None:
    """IDs found but shared DB read fails → empty list, no raise."""
    company_conn = FakeConn(rows=[{"instrument_id": 11}])
    shared_conn = FakeConn(fail=True)
    with _patch_company_pool({"alpha": FakePool(company_conn)}), \
         _patch_shared_pool(FakePool(shared_conn)):
        symbols = await np._company_instruments("alpha")
    assert symbols == []


@pytest.mark.asyncio
async def test_company_instruments_skips_null_ids() -> None:
    """``NULL`` instrument_id rows are dropped before the symbol lookup."""
    company_conn = FakeConn(rows=[
        {"instrument_id": 11},
        {"instrument_id": None},
        {"instrument_id": 22},
    ])
    shared_conn = FakeConn(rows=[{"symbol": "BTC/USDT"}, {"symbol": "ETH/USDT"}])
    with _patch_company_pool({"alpha": FakePool(company_conn)}), \
         _patch_shared_pool(FakePool(shared_conn)):
        symbols = await np._company_instruments("alpha")
    # The only IDs forwarded to the shared SELECT must be [11, 22].
    forwarded_args = shared_conn.calls[0][1]
    assert forwarded_args[0] == [11, 22]
    assert symbols == ["BTC/USDT", "ETH/USDT"]


@pytest.mark.asyncio
async def test_company_instruments_dedupes_and_sorts_symbols() -> None:
    """Duplicate symbols (e.g. same coin on multiple venues) are deduped."""
    company_conn = FakeConn(rows=[{"instrument_id": 1}, {"instrument_id": 2}])
    shared_conn = FakeConn(rows=[
        {"symbol": "ETH/USDT"},
        {"symbol": "BTC/USDT"},
        {"symbol": "ETH/USDT"},  # duplicate from a different exchange row
        {"symbol": None},  # must be dropped
    ])
    with _patch_company_pool({"alpha": FakePool(company_conn)}), \
         _patch_shared_pool(FakePool(shared_conn)):
        symbols = await np._company_instruments("alpha")
    assert symbols == ["BTC/USDT", "ETH/USDT"]


# --------------------------------------------------------------------------- #
# _run_with_budget
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_run_with_budget_returns_value_on_success() -> None:
    async def _ok() -> int:
        return 42

    out = await np._run_with_budget(_ok, label="t", fallback=0)
    assert out == 42


@pytest.mark.asyncio
async def test_run_with_budget_returns_fallback_on_timeout() -> None:
    async def _slow() -> int:
        await asyncio.sleep(NEWS_PROVIDER_TIMEOUT_S * 4)
        return 99  # never reached

    out = await np._run_with_budget(_slow, label="t", fallback=[])
    assert out == []


@pytest.mark.asyncio
async def test_run_with_budget_returns_fallback_on_exception() -> None:
    async def _boom() -> int:
        raise RuntimeError("boom")

    out = await np._run_with_budget(_boom, label="t", fallback="fb")
    assert out == "fb"


@pytest.mark.asyncio
async def test_run_with_budget_propagates_cancellation() -> None:
    async def _cancel() -> int:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await np._run_with_budget(_cancel, label="t", fallback=None)


# --------------------------------------------------------------------------- #
# NewsFeedProvider.fetch — happy path & edge cases
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fetch_returns_rows_in_documented_shape() -> None:
    rows = [_news_row(rid=1, headline="BTC up"), _news_row(rid=2, headline="ETH up")]
    with _patch_shared_pool(FakePool(FakeConn(rows=rows))):
        out = await NewsFeedProvider().fetch(window_days=1)
    assert isinstance(out, list)
    assert len(out) == 2
    first = out[0]
    # Every documented key is present.
    for key in (
        "id", "source", "headline", "content", "instruments", "sentiment",
        "has_media", "media_count", "channel_name", "author",
        "published_at", "collected_at", "enrichment_status",
    ):
        assert key in first


@pytest.mark.asyncio
async def test_fetch_returns_empty_on_invalid_window() -> None:
    """Bad window → empty list, no DB call, no raise."""
    pool = FakePool(FakeConn(rows=[_news_row()]))
    with _patch_shared_pool(pool):
        out = await NewsFeedProvider().fetch(window_days=99)
    assert out == []


@pytest.mark.asyncio
async def test_fetch_returns_empty_on_db_failure() -> None:
    """Connection failure → empty list, no raise."""
    with _patch_shared_pool(FakePool(FakeConn(fail=True))):
        out = await NewsFeedProvider().fetch(window_days=1)
    assert out == []


@pytest.mark.asyncio
async def test_fetch_returns_empty_on_budget_timeout() -> None:
    """A query slower than 250ms must yield ``[]`` without raising."""
    slow_conn = FakeConn(rows=[_news_row()], delay=NEWS_PROVIDER_TIMEOUT_S * 4)
    with _patch_shared_pool(FakePool(slow_conn)):
        out = await NewsFeedProvider().fetch(window_days=1)
    assert out == []


@pytest.mark.asyncio
async def test_fetch_truncates_long_content_to_preview_chars() -> None:
    """Content longer than PREVIEW_CHARS gets truncated with an ellipsis."""
    long = "x" * 500
    rows = [_news_row(content=long)]
    with _patch_shared_pool(FakePool(FakeConn(rows=rows))):
        out = await NewsFeedProvider().fetch(window_days=1)
    assert len(out) == 1
    assert len(out[0]["content"]) <= NewsFeedProvider.PREVIEW_CHARS
    assert out[0]["content"].endswith("…")


@pytest.mark.asyncio
async def test_fetch_handles_malformed_instruments_jsonb_safely() -> None:
    """If ``instruments`` decodes to a dict (malformed), default to ``[]``."""
    rows = [_news_row(instruments=None), _news_row(instruments=["BTC/USDT"])]
    # Force one row to have a non-list instruments value via dict mutation.
    rows[0]["instruments"] = {"oops": "bad shape"}
    with _patch_shared_pool(FakePool(FakeConn(rows=rows))):
        out = await NewsFeedProvider().fetch(window_days=1)
    assert out[0]["instruments"] == []
    assert out[1]["instruments"] == ["BTC/USDT"]


# --------------------------------------------------------------------------- #
# Filter parameterisation — has_media / source / sort
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fetch_threads_source_filter_into_parameters() -> None:
    """The validated source must be a query parameter, not interpolated."""
    capture = FakeConn(rows=[])
    with _patch_shared_pool(FakePool(capture)):
        await NewsFeedProvider().fetch(window_days=1, source_kind="Discord")
    sql, args = capture.calls[0]
    assert "lower(source)" in sql
    assert "discord" in args  # lower-cased & passed by position


@pytest.mark.asyncio
async def test_fetch_drops_unknown_source_silently() -> None:
    """Unknown source label → no clause, no error."""
    capture = FakeConn(rows=[])
    with _patch_shared_pool(FakePool(capture)):
        await NewsFeedProvider().fetch(window_days=1, source_kind="madeup")
    sql, args = capture.calls[0]
    assert "lower(source)" not in sql
    assert "madeup" not in args


@pytest.mark.asyncio
async def test_fetch_has_media_true_emits_is_true_clause() -> None:
    capture = FakeConn(rows=[])
    with _patch_shared_pool(FakePool(capture)):
        await NewsFeedProvider().fetch(window_days=1, has_media=True)
    sql, _ = capture.calls[0]
    assert "has_media IS TRUE" in sql


@pytest.mark.asyncio
async def test_fetch_has_media_false_emits_is_false_clause() -> None:
    capture = FakeConn(rows=[])
    with _patch_shared_pool(FakePool(capture)):
        await NewsFeedProvider().fetch(window_days=1, has_media=False)
    sql, _ = capture.calls[0]
    assert "has_media IS FALSE" in sql


@pytest.mark.asyncio
async def test_fetch_has_media_none_omits_media_clause() -> None:
    capture = FakeConn(rows=[])
    with _patch_shared_pool(FakePool(capture)):
        await NewsFeedProvider().fetch(window_days=1, has_media=None)
    sql, _ = capture.calls[0]
    assert "has_media IS TRUE" not in sql
    assert "has_media IS FALSE" not in sql


@pytest.mark.asyncio
async def test_fetch_sql_orders_by_collected_at_desc() -> None:
    """Sort order is part of the §11 contract — pin it."""
    capture = FakeConn(rows=[])
    with _patch_shared_pool(FakePool(capture)):
        await NewsFeedProvider().fetch(window_days=1)
    sql, _ = capture.calls[0]
    assert "ORDER BY collected_at DESC" in sql


@pytest.mark.asyncio
async def test_fetch_clamps_limit_in_sql_parameters() -> None:
    """Caller-supplied 10_000 → SQL receives NEWS_LIMIT_CAP."""
    capture = FakeConn(rows=[])
    with _patch_shared_pool(FakePool(capture)):
        await NewsFeedProvider().fetch(window_days=1, limit=10_000)
    _, args = capture.calls[0]
    # Limit is the LAST positional parameter.
    assert args[-1] == NEWS_LIMIT_CAP


# --------------------------------------------------------------------------- #
# Per-company narrowing through fetch()
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fetch_with_company_filter_narrows_by_instruments() -> None:
    """Company filter resolves to symbols and threads them into the JSONB ?| clause."""
    company_conn = FakeConn(rows=[{"instrument_id": 11}])
    # Shared pool is used for BOTH instrument symbol resolution AND the
    # final news_items SELECT — supply a single conn that returns both
    # depending on the SQL shape (the FakeConn ignores the SQL, so we
    # use a side-effect on .fetch via a custom subclass).

    class SmartConn(FakeConn):
        async def fetch(self, query: str, *args: Any) -> List[Dict[str, Any]]:
            self.calls.append((query, args))
            if "FROM instruments" in query:
                return [{"symbol": "BTC/USDT"}]
            if "FROM news_items" in query:
                return [_news_row()]
            return []

    shared_conn = SmartConn()
    with _patch_active_companies(["alpha"]), \
         _patch_company_pool({"alpha": FakePool(company_conn)}), \
         _patch_shared_pool(FakePool(shared_conn)):
        out = await NewsFeedProvider().fetch(
            window_days=1, company_filter="alpha"
        )

    assert len(out) == 1
    # The news_items SELECT must include the JSONB ?| clause and the
    # instruments list parameter.
    news_calls = [c for c in shared_conn.calls if "FROM news_items" in c[0]]
    assert len(news_calls) == 1
    sql, args = news_calls[0]
    assert "instruments ?|" in sql
    assert ["BTC/USDT"] in args


@pytest.mark.asyncio
async def test_fetch_with_unknown_company_skips_narrowing() -> None:
    """Unknown company → no instrument lookup, full feed returned."""
    rows = [_news_row(instruments=["XRP/USDT"])]
    with _patch_active_companies(["alpha"]), \
         _patch_shared_pool(FakePool(FakeConn(rows=rows))):
        out = await NewsFeedProvider().fetch(
            window_days=1, company_filter="ghost"
        )
    assert len(out) == 1
    assert out[0]["instruments"] == ["XRP/USDT"]


@pytest.mark.asyncio
async def test_fetch_with_company_having_no_trades_returns_full_feed() -> None:
    """Active company with no trades yet → no narrowing applied."""
    rows = [_news_row(), _news_row(rid=2, headline="ETH up")]
    with _patch_active_companies(["alpha"]), \
         _patch_company_pool({"alpha": FakePool(FakeConn(rows=[]))}), \
         _patch_shared_pool(FakePool(FakeConn(rows=rows))):
        out = await NewsFeedProvider().fetch(
            window_days=1, company_filter="alpha"
        )
    # No instruments resolved → no ?| clause, full feed surfaces.
    assert len(out) == 2


# --------------------------------------------------------------------------- #
# Empty / null defensive paths
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fetch_empty_rows_returns_empty_list() -> None:
    with _patch_shared_pool(FakePool(FakeConn(rows=[]))):
        out = await NewsFeedProvider().fetch(window_days=7)
    assert out == []


@pytest.mark.asyncio
async def test_fetch_handles_null_optional_fields_gracefully() -> None:
    """NULL headline / content / sentiment / channel / author / published_at."""
    rows = [_news_row(
        headline=None,
        content=None,
        sentiment=None,
        channel_name=None,
        author=None,
        published_at=None,
    )]
    with _patch_shared_pool(FakePool(FakeConn(rows=rows))):
        out = await NewsFeedProvider().fetch(window_days=1)
    assert len(out) == 1
    row = out[0]
    assert row["headline"] is None
    assert row["content"] is None
    assert row["sentiment"] is None
    assert row["channel_name"] is None
    assert row["author"] is None
    assert row["published_at"] is None
    # collected_at is NEVER NULL by contract.
    assert isinstance(row["collected_at"], str)
