"""
Module: test_learning_routes
Purpose: Smoke + behaviour tests for the six Phase Y learning HTTP
endpoints exposed by :mod:`shared.dashboard.learning_routes`. We
inject mock provider objects into ``app["_learning_*"]`` so the
tests stay db-free and exercise the parser/JSON-coercion layer in
isolation.
Location: /opt/tickles/shared/tests/test_learning_routes.py
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from shared.dashboard.learning_routes import (
    _jsonify,
    _parse_company,
    _parse_positive_int,
    _parse_window,
    attach_routes,
)


# ---------------------------------------------------------------------------
# Mock providers — capture call args, return fixed payloads.
# ---------------------------------------------------------------------------


class _MockProvider:
    """Generic provider stub recording every ``fetch()`` call."""

    def __init__(self, payload: List[Dict[str, Any]] | None = None,
                 raise_exc: Exception | None = None) -> None:
        self.payload = payload or []
        self.raise_exc = raise_exc
        self.calls: List[Dict[str, Any]] = []

    async def fetch(self, **kwargs: Any) -> List[Dict[str, Any]]:
        self.calls.append(dict(kwargs))
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.payload


def _build_app(**provider_overrides: Any) -> web.Application:
    """Build an aiohttp app with the learning routes wired up.

    Any kwargs are stored as ``app["_learning_<key>"] = value`` so
    the routes pick them up via :func:`_provider`'s cache lookup.
    """
    app = web.Application()
    for key, prov in provider_overrides.items():
        app[f"_learning_{key}"] = prov
    attach_routes(app, prefix="")
    return app


# ---------------------------------------------------------------------------
# _parse_window
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    (None, 7),
    ("", 7),
    ("7", 7),
    ("14", 14),
    ("30", 30),
    ("1m", 30),
    ("1M", 30),
    ("7d", 7),
    ("14d", 14),
    ("30d", 30),
    (" 14D ", 14),
])
def test_parse_window_accepts_canonical_inputs(raw: Any, expected: int) -> None:
    assert _parse_window(raw, default=7) == expected


@pytest.mark.parametrize("raw", ["abc", "0", "5", "60", "-7", "14x"])
def test_parse_window_rejects_invalid_falls_back_to_default(raw: str) -> None:
    assert _parse_window(raw, default=30) == 30


# ---------------------------------------------------------------------------
# _parse_company
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    (None, None),
    ("", None),
    ("   ", None),
    ("all", None),
    ("ALL", None),
    ("rubicon", "rubicon"),
    ("  alpha  ", "alpha"),
])
def test_parse_company_normalises(raw: Any, expected: Any) -> None:
    assert _parse_company(raw) == expected


# ---------------------------------------------------------------------------
# _parse_positive_int
# ---------------------------------------------------------------------------


def test_parse_positive_int_clamps_to_cap() -> None:
    assert _parse_positive_int("9999", default=100, max_cap=500) == 500


def test_parse_positive_int_minimum_one() -> None:
    assert _parse_positive_int("0", default=10, max_cap=100) == 1
    assert _parse_positive_int("-3", default=10, max_cap=100) == 1


def test_parse_positive_int_default_when_unparseable() -> None:
    assert _parse_positive_int("xyz", default=42, max_cap=100) == 42
    assert _parse_positive_int(None, default=42, max_cap=100) == 42


# ---------------------------------------------------------------------------
# _jsonify
# ---------------------------------------------------------------------------


def test_jsonify_handles_decimal() -> None:
    assert _jsonify(Decimal("1.2345")) == "1.2345"


def test_jsonify_handles_datetime_with_tz() -> None:
    dt = datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc)
    assert _jsonify(dt) == "2026-05-01T12:30:00+00:00"


def test_jsonify_handles_naive_datetime() -> None:
    dt = datetime(2026, 5, 1, 12, 30)
    out = _jsonify(dt)
    assert isinstance(out, str) and out.startswith("2026-05-01T12:30:00")


def test_jsonify_walks_nested_structures() -> None:
    payload = {
        "rows": [
            {"score": Decimal("0.42"),
             "ts": datetime(2026, 5, 1, tzinfo=timezone.utc),
             "tags": ("a", "b")},
        ],
    }
    out = _jsonify(payload)
    assert out == {
        "rows": [
            {"score": "0.42", "ts": "2026-05-01T00:00:00+00:00", "tags": ["a", "b"]},
        ],
    }


def test_jsonify_falls_back_to_str_for_unknown() -> None:
    class Custom:
        def __str__(self) -> str:
            return "<custom>"

    assert _jsonify(Custom()) == "<custom>"


def test_jsonify_preserves_none_and_primitives() -> None:
    assert _jsonify(None) is None
    assert _jsonify(True) is True
    assert _jsonify(0) == 0
    assert _jsonify(3.14) == 3.14
    assert _jsonify("hello") == "hello"


# ---------------------------------------------------------------------------
# attach_routes — registration smoke
# ---------------------------------------------------------------------------


def test_attach_routes_registers_six_endpoints_under_prefix() -> None:
    app = web.Application()
    attach_routes(app, prefix="/dashboard")
    # aiohttp's ``add_get`` registers both GET and HEAD entries on the
    # same resource; we collapse to unique canonical paths.
    paths = sorted({r.resource.canonical for r in app.router.routes()})
    assert paths == [
        "/dashboard/api/learning/agent-brain",
        "/dashboard/api/learning/failed-trades",
        "/dashboard/api/learning/guard-activity",
        "/dashboard/api/learning/memory-feed",
        "/dashboard/api/learning/prompt-evolution",
        "/dashboard/api/learning/skill-summary",
    ]


def test_attach_routes_supports_dual_mount() -> None:
    """Both '' and '/dashboard' prefixes can coexist on one app."""
    app = web.Application()
    attach_routes(app, prefix="")
    attach_routes(app, prefix="/dashboard")
    paths = {r.resource.canonical for r in app.router.routes()}
    assert "/api/learning/skill-summary" in paths
    assert "/dashboard/api/learning/skill-summary" in paths


# ---------------------------------------------------------------------------
# End-to-end handler tests via TestClient.
# ---------------------------------------------------------------------------


@pytest.fixture
async def make_client():
    """Yield a factory that builds and starts an aiohttp TestClient.

    The factory accepts provider keyword args (e.g. ``skill=...``) that
    are forwarded to :func:`_build_app`. The client and underlying
    server are torn down after the test.
    """
    clients: List[TestClient] = []

    async def _factory(**provider_overrides: Any) -> TestClient:
        app = _build_app(**provider_overrides)
        client = TestClient(TestServer(app))
        await client.start_server()
        clients.append(client)
        return client

    yield _factory

    for c in clients:
        await c.close()


@pytest.mark.asyncio
async def test_skill_summary_happy_path(make_client) -> None:
    prov = _MockProvider(payload=[
        {"actor_id": "rsi_master", "skill_score": Decimal("0.71"),
         "trades": 42, "as_of": datetime(2026, 5, 1, tzinfo=timezone.utc)},
    ])
    client = await make_client(skill=prov)
    resp = await client.get("/api/learning/skill-summary?window=14&company=rubicon")
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["window_days"] == 14
    assert body["rows"][0]["skill_score"] == "0.71"  # Decimal stringified
    assert body["rows"][0]["actor_id"] == "rsi_master"
    # Provider received the parsed args.
    assert prov.calls == [{"window_days": 14, "company_filter": "rubicon"}]


@pytest.mark.asyncio
async def test_skill_summary_label_1m_maps_to_30(make_client) -> None:
    prov = _MockProvider(payload=[])
    client = await make_client(skill=prov)
    resp = await client.get("/api/learning/skill-summary?window=1M")
    assert resp.status == 200
    body = await resp.json()
    assert body["window_days"] == 30
    assert prov.calls[0]["window_days"] == 30


@pytest.mark.asyncio
async def test_skill_summary_invalid_window_uses_default(make_client) -> None:
    prov = _MockProvider(payload=[])
    client = await make_client(skill=prov)
    resp = await client.get("/api/learning/skill-summary?window=bogus")
    assert resp.status == 200
    body = await resp.json()
    assert body["window_days"] == 7  # default
    assert prov.calls[0]["window_days"] == 7


@pytest.mark.asyncio
async def test_skill_summary_company_all_becomes_none(make_client) -> None:
    prov = _MockProvider(payload=[])
    client = await make_client(skill=prov)
    await client.get("/api/learning/skill-summary?company=all")
    assert prov.calls[0]["company_filter"] is None


@pytest.mark.asyncio
async def test_skill_summary_provider_exception_returns_500(make_client) -> None:
    prov = _MockProvider(raise_exc=RuntimeError("db gone"))
    client = await make_client(skill=prov)
    resp = await client.get("/api/learning/skill-summary")
    assert resp.status == 500
    body = await resp.json()
    assert body["ok"] is False
    assert "skill-summary" in body["error"]


@pytest.mark.asyncio
async def test_memory_feed_threads_dimension_filter(make_client) -> None:
    prov = _MockProvider(payload=[
        {"ts": datetime(2026, 5, 1, tzinfo=timezone.utc), "kind": "lesson",
         "dim": "setup", "outcome": "win", "company_id": "rubicon"},
    ])
    client = await make_client(feed=prov)
    resp = await client.get("/api/learning/memory-feed?window=7&dimension=setup&limit=50")
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["dimension"] == "setup"
    assert body["window_days"] == 7
    assert prov.calls == [{
        "window_days": 7,
        "company_filter": None,
        "dimension_filter": "setup",
        "limit": 50,
    }]


@pytest.mark.asyncio
async def test_memory_feed_blank_dimension_becomes_none(make_client) -> None:
    prov = _MockProvider(payload=[])
    client = await make_client(feed=prov)
    await client.get("/api/learning/memory-feed?dimension=  ")
    assert prov.calls[0]["dimension_filter"] is None


@pytest.mark.asyncio
async def test_memory_feed_limit_clamped_to_cap(make_client) -> None:
    prov = _MockProvider(payload=[])
    client = await make_client(feed=prov)
    await client.get("/api/learning/memory-feed?limit=99999")
    # _parse_positive_int caps at DEFAULT_FEED_LIMIT_CAP — read it from providers.
    from shared.dashboard.learning_providers import DEFAULT_FEED_LIMIT_CAP
    assert prov.calls[0]["limit"] == DEFAULT_FEED_LIMIT_CAP


@pytest.mark.asyncio
async def test_agent_brain_default_window_is_30(make_client) -> None:
    prov = _MockProvider(payload=[
        {"actor_id": "scout", "wins": 4, "losses": 2, "breakeven": 1},
    ])
    client = await make_client(brain=prov)
    resp = await client.get("/api/learning/agent-brain")
    assert resp.status == 200
    body = await resp.json()
    assert body["window_days"] == 30
    assert prov.calls[0]["window_days"] == 30


@pytest.mark.asyncio
async def test_guard_activity_no_window_param(make_client) -> None:
    prov = _MockProvider(payload=[
        {"company_id": "rubicon", "kind": "no_failed_trades", "severity": "warn"},
    ])
    client = await make_client(guard=prov)
    resp = await client.get("/api/learning/guard-activity?company=rubicon")
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert "window_days" not in body  # guard endpoint omits window
    assert prov.calls == [{"company_filter": "rubicon"}]


@pytest.mark.asyncio
async def test_prompt_evolution_caps_window_and_limit(make_client) -> None:
    prov = _MockProvider(payload=[])
    client = await make_client(prompt=prov)
    await client.get("/api/learning/prompt-evolution?window=9999&limit=99999")
    from shared.dashboard.learning_providers import PROMPT_EVOLUTION_LIMIT_CAP
    assert prov.calls[0]["window_days"] == 365
    assert prov.calls[0]["limit"] == PROMPT_EVOLUTION_LIMIT_CAP


@pytest.mark.asyncio
async def test_failed_trades_happy_path(make_client) -> None:
    prov = _MockProvider(payload=[
        {"company_id": "rubicon", "failed_count": 2, "total_closed": 25},
    ])
    client = await make_client(failed=prov)
    resp = await client.get("/api/learning/failed-trades?window=7d")
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["window_days"] == 7
    assert body["rows"][0]["failed_count"] == 2


@pytest.mark.asyncio
async def test_failed_trades_provider_exception_returns_500(make_client) -> None:
    prov = _MockProvider(raise_exc=ValueError("nope"))
    client = await make_client(failed=prov)
    resp = await client.get("/api/learning/failed-trades")
    assert resp.status == 500
    body = await resp.json()
    assert body["ok"] is False


@pytest.mark.asyncio
async def test_payload_jsonifies_decimal_and_datetime_round_trip(make_client) -> None:
    """Regression — asyncpg-style Decimal/datetime values must survive
    the wire without raising in :func:`web.json_response`."""
    prov = _MockProvider(payload=[
        {"score": Decimal("0.5"),
         "as_of": datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc),
         "nested": {"k": Decimal("1")}},
    ])
    client = await make_client(skill=prov)
    resp = await client.get("/api/learning/skill-summary")
    assert resp.status == 200
    body = await resp.json()
    row = body["rows"][0]
    assert row["score"] == "0.5"
    assert row["as_of"] == "2026-05-01T00:00:00+00:00"
    assert row["nested"]["k"] == "1"
