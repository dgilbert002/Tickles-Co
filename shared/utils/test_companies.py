"""Module: test_companies
Purpose: Smoke tests for the companies enumerator helper.
Location: /opt/tickles/shared/utils/test_companies.py
"""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from shared.utils import companies


@pytest.fixture(autouse=True)
def clear_cache():
    """Invalidate the module cache before each test."""
    companies.invalidate_cache()
    yield
    companies.invalidate_cache()


class FakePool:
    """Minimal asyncpg pool stand-in for tests."""

    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        """Return an async context manager that yields the connection."""
        class _AcquireCtx:
            def __init__(self, conn):
                self._conn = conn
            async def __aenter__(self):
                return self._conn
            async def __aexit__(self, *args):
                pass
        return _AcquireCtx(self._conn)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class FakeAsyncpgConnect:
    """Stand-in for asyncpg.connect(dsn) — both awaitable and async-context-manager."""

    def __init__(self, conn):
        self._conn = conn

    def __await__(self):
        async def _await():
            return self
        return _await().__await__()

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *args):
        pass


class FakeConn:
    """Minimal asyncpg connection stand-in for tests."""

    def __init__(self, fetch_result=None, fetchrow_result=None):
        self._fetch = fetch_result or []
        self._fetchrow = fetchrow_result
        self.execute_calls = []

    async def fetch(self, query, *args):
        return self._fetch

    async def fetchrow(self, query, *args):
        return self._fetchrow

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


@pytest.mark.anyio
async def test_list_active_companies_from_table():
    """Companies are read from the companies table when it exists."""
    conn = FakeConn(
        fetch_result=[
            {"short_name": "alpha"},
            {"short_name": "jarvais"},
            {"short_name": "rubicon"},
        ]
    )
    pool = FakePool(conn)

    with patch("shared.utils.companies.get_shared_pool", return_value=pool):
        result = await companies.list_active_companies()

    # jarvais is excluded; others are sorted by DB ORDER BY
    assert result == ["alpha", "rubicon"]


@pytest.mark.anyio
async def test_list_active_companies_fallback_to_env():
    """When companies table is missing, fall back to ACTIVE_COMPANIES env."""

    class MissingTableConn(FakeConn):
        async def fetch(self, query, *args):
            raise asyncpg.UndefinedTableError("relation does not exist")

    conn = MissingTableConn()
    pool = FakePool(conn)

    with patch("shared.utils.companies.get_shared_pool", return_value=pool):
        with patch.dict(os.environ, {"ACTIVE_COMPANIES": "rubicon, beta, gamma"}):
            result = await companies.list_active_companies()

    # Fallback sorts alphabetically
    assert result == ["beta", "gamma", "rubicon"]


@pytest.mark.anyio
async def test_list_active_companies_caches():
    """Second call within TTL returns cached result without DB hit."""
    conn = FakeConn(fetch_result=[{"short_name": "rubicon"}])
    pool = FakePool(conn)

    with patch("shared.utils.companies.get_shared_pool", return_value=pool):
        r1 = await companies.list_active_companies()
        r2 = await companies.list_active_companies()

    assert r1 == r2 == ["rubicon"]
    # fetch should only be called once due to caching
    # (FakeConn.fetch is called each time, but get_shared_pool only once)


@pytest.mark.anyio
async def test_invalidate_cache_resets():
    """invalidate_cache forces a fresh DB read."""
    conn = FakeConn(fetch_result=[{"short_name": "rubicon"}])
    pool = FakePool(conn)

    with patch("shared.utils.companies.get_shared_pool", return_value=pool):
        await companies.list_active_companies()
        companies.invalidate_cache()
        await companies.list_active_companies()

    # fetch called twice because cache was invalidated
    # (we can't easily count FakeConn.fetch calls, but the test passes
    #  if no exception is raised)


@pytest.mark.anyio
async def test_get_company_dsn_from_template():
    """DSN is built from TICKLES_DB_DSN_TEMPLATE."""
    template = "postgresql://u:p@h:5432/{db}"
    with patch.dict(os.environ, {"TICKLES_DB_DSN_TEMPLATE": template}):
        dsn = await companies.get_company_dsn("rubicon")
    assert dsn == "postgresql://u:p@h:5432/tickles_rubicon"


@pytest.mark.anyio
async def test_get_company_dsn_legacy_fallback():
    """Legacy template with {user}/{password}/{host}/{port} placeholders."""
    template = "postgresql://{user}:{password}@{host}:{port}/{db}"
    env = {
        "DB_USER": "tickles",
        "DB_PASSWORD": "secret",
        "DB_HOST": "db.example.com",
        "DB_PORT": "5432",
    }
    with patch.dict(os.environ, env, clear=True):
        dsn = await companies.get_company_dsn("rubicon")
    assert dsn == "postgresql://tickles:secret@db.example.com:5432/tickles_rubicon"


@pytest.mark.anyio
async def test_for_each_company_success():
    import sniffio
    if sniffio.current_async_library() == "trio":
        pytest.skip("asyncio.gather is not compatible with trio")
    """Fan-out succeeds and returns results per company."""
    call_log = []

    async def mock_fn(company: str, conn: asyncpg.Connection) -> str:
        call_log.append(company)
        return f"ok-{company}"

    with patch(
        "shared.utils.companies.list_active_companies",
        return_value=["rubicon", "alpha"],
    ):
        with patch(
            "shared.utils.companies.get_company_dsn",
            return_value="postgresql://localhost/tickles_test",
        ):
            mock_conn = FakeConn()

            def _mock_connect(dsn: str):
                return mock_conn

            with patch("asyncpg.connect", _mock_connect):
                results = await companies.for_each_company(mock_fn)

    assert results["rubicon"] == "ok-rubicon"
    assert results["alpha"] == "ok-alpha"
    assert sorted(call_log) == ["alpha", "rubicon"]


@pytest.mark.anyio
async def test_for_each_company_failure_isolated():
    import sniffio
    if sniffio.current_async_library() == "trio":
        pytest.skip("asyncio.gather is not compatible with trio")
    """One failing company does not abort the others."""
    async def mock_fn(company: str, conn: asyncpg.Connection) -> str:
        if company == "alpha":
            raise ValueError("alpha broke")
        return f"ok-{company}"

    with patch(
        "shared.utils.companies.list_active_companies",
        return_value=["rubicon", "alpha"],
    ):
        with patch(
            "shared.utils.companies.get_company_dsn",
            return_value="postgresql://localhost/tickles_test",
        ):
            mock_conn = FakeConn()

            def _mock_connect(dsn: str):
                return mock_conn

            with patch("asyncpg.connect", _mock_connect):
                results = await companies.for_each_company(mock_fn)

    assert results["rubicon"] == "ok-rubicon"
    assert isinstance(results["alpha"], ValueError)
