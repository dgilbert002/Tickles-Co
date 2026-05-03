"""
Module: test_news_routes
Purpose: Smoke + behaviour tests for the Phase X.4 News Feed HTTP
endpoint exposed by :mod:`shared.dashboard.news_routes`. We inject
a mock provider into ``app["_news_feed"]`` so the tests stay db-free
and exercise the parser/JSON-coercion layer in isolation.
Location: /opt/tickles/shared/tests/test_news_routes.py
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from shared.dashboard.news_provider import (
    NEWS_DEFAULT_LIMIT,
    NEWS_LIMIT_CAP,
)
from shared.dashboard.news_routes import (
    _jsonify,
    _parse_bool,
    _parse_company,
    _parse_limit,
    _parse_source,
    _parse_window,
    attach_routes,
)


# ---------------------------------------------------------------------------
# Mock provider — captures call args, returns a fixed payload.
# ---------------------------------------------------------------------------


class _MockNewsProvider:
    """Stand-in for :class:`NewsFeedProvider` recording each fetch()."""

    def __init__(
        self,
        payload: Optional[List[Dict[str, Any]]] = None,
        raise_exc: Optional[Exception] = None,
    ) -> None:
        self.payload = payload if payload is not None else []
        self.raise_exc = raise_exc
        self.calls: List[Dict[str, Any]] = []

    async def fetch(self, **kwargs: Any) -> List[Dict[str, Any]]:
        self.calls.append(dict(kwargs))
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.payload


def _build_app(provider: Optional[_MockNewsProvider] = None) -> web.Application:
    """Build an aiohttp app with the news routes wired up.

    Args:
        provider: Optional mock provider stored at ``app["_news_feed"]``.

    Returns:
        Configured aiohttp application with routes attached at the root.
    """
    app = web.Application()
    if provider is not None:
        app["_news_feed"] = provider
    attach_routes(app, prefix="")
    return app


# ---------------------------------------------------------------------------
# _parse_window
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    (None, 1),
    ("24h", 1),
    ("24H", 1),
    (" 24h ", 1),
    ("7d", 7),
    ("7D", 7),
    ("30d", 30),
    ("30D", 30),
])
def test_parse_window_accepts_canonical_labels(raw: Any, expected: int) -> None:
    assert _parse_window(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "1d", "14d", "60d", "0", "-1", "junk"])
def test_parse_window_falls_back_to_24h_on_garbage(raw: str) -> None:
    assert _parse_window(raw) == 1


# ---------------------------------------------------------------------------
# _parse_company
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    (None, None),
    ("", None),
    ("   ", None),
    ("all", None),
    ("ALL", None),
    ("All", None),
    ("rubicon", "rubicon"),
    ("  alpha  ", "alpha"),
])
def test_parse_company_normalises(raw: Any, expected: Any) -> None:
    assert _parse_company(raw) == expected


# ---------------------------------------------------------------------------
# _parse_source
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("discord", "discord"),
    ("TELEGRAM", "telegram"),
    (" twitter ", "twitter"),
    ("rss", "rss"),
    ("web", "web"),
    ("manual", "manual"),
])
def test_parse_source_accepts_known_kinds(raw: str, expected: str) -> None:
    assert _parse_source(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "newspaper", "facebook", "x"])
def test_parse_source_drops_unknown_silently(raw: Any) -> None:
    assert _parse_source(raw) is None


# ---------------------------------------------------------------------------
# _parse_bool — tri-state has_media flag
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("true", True),
    ("TRUE", True),
    ("1", True),
    ("yes", True),
    ("Y", True),
    ("false", False),
    ("FALSE", False),
    ("0", False),
    ("no", False),
    ("N", False),
])
def test_parse_bool_recognises_truthy_and_falsy(raw: str, expected: bool) -> None:
    assert _parse_bool(raw) is expected


@pytest.mark.parametrize("raw", [None, "", "   ", "maybe", "2", "junk"])
def test_parse_bool_returns_none_on_unknown(raw: Any) -> None:
    assert _parse_bool(raw) is None


# ---------------------------------------------------------------------------
# _parse_limit
# ---------------------------------------------------------------------------


def test_parse_limit_uses_default_when_missing() -> None:
    assert _parse_limit(None) == NEWS_DEFAULT_LIMIT
    assert _parse_limit("") == NEWS_DEFAULT_LIMIT


def test_parse_limit_uses_default_when_unparseable() -> None:
    assert _parse_limit("xyz") == NEWS_DEFAULT_LIMIT
    assert _parse_limit("1.5") == NEWS_DEFAULT_LIMIT


def test_parse_limit_clamps_to_cap() -> None:
    assert _parse_limit(str(NEWS_LIMIT_CAP + 100)) == NEWS_LIMIT_CAP
    assert _parse_limit("99999") == NEWS_LIMIT_CAP


def test_parse_limit_minimum_one() -> None:
    assert _parse_limit("0") == 1
    assert _parse_limit("-7") == 1


def test_parse_limit_passes_through_in_range() -> None:
    assert _parse_limit("50") == 50
    assert _parse_limit("1") == 1
    assert _parse_limit(str(NEWS_LIMIT_CAP)) == NEWS_LIMIT_CAP


# ---------------------------------------------------------------------------
# _jsonify
# ---------------------------------------------------------------------------


def test_jsonify_handles_decimal() -> None:
    assert _jsonify(Decimal("0.0042")) == "0.0042"


def test_jsonify_handles_datetime_with_tz() -> None:
    dt = datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc)
    assert _jsonify(dt) == "2026-05-01T12:30:00+00:00"


def test_jsonify_handles_naive_datetime() -> None:
    out = _jsonify(datetime(2026, 5, 1, 12, 30))
    assert isinstance(out, str) and out.startswith("2026-05-01T12:30:00")


def test_jsonify_walks_nested_structures() -> None:
    payload = {
        "rows": [
            {
                "id": "abc",
                "sentiment": Decimal("-0.5"),
                "ts": datetime(2026, 5, 1, tzinfo=timezone.utc),
                "instruments": ("BTC/USDT", "ETH/USDT"),
            },
        ],
    }
    out = _jsonify(payload)
    assert out == {
        "rows": [
            {
                "id": "abc",
                "sentiment": "-0.5",
                "ts": "2026-05-01T00:00:00+00:00",
                "instruments": ["BTC/USDT", "ETH/USDT"],
            },
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


def test_attach_routes_registers_feed_endpoint_under_prefix() -> None:
    app = web.Application()
    attach_routes(app, prefix="/dashboard")
    paths = sorted({r.resource.canonical for r in app.router.routes()})
    assert paths == ["/dashboard/api/news/feed"]


def test_attach_routes_supports_dual_mount() -> None:
    """Both '' and '/dashboard' prefixes can coexist on one app."""
    app = web.Application()
    attach_routes(app, prefix="")
    attach_routes(app, prefix="/dashboard")
    paths = {r.resource.canonical for r in app.router.routes()}
    assert "/api/news/feed" in paths
    assert "/dashboard/api/news/feed" in paths


# ---------------------------------------------------------------------------
# End-to-end handler tests via TestClient.
# ---------------------------------------------------------------------------


@pytest.fixture
async def make_client():
    """Yield a factory that builds and starts an aiohttp TestClient.

    The factory accepts a ``provider`` kwarg forwarded to
    :func:`_build_app`. The client and underlying server are torn
    down after the test.
    """
    clients: List[TestClient] = []

    async def _factory(provider: Optional[_MockNewsProvider] = None) -> TestClient:
        app = _build_app(provider=provider)
        client = TestClient(TestServer(app))
        await client.start_server()
        clients.append(client)
        return client

    yield _factory

    for c in clients:
        await c.close()


@pytest.mark.asyncio
async def test_feed_happy_path_threads_default_window(make_client) -> None:
    prov = _MockNewsProvider(payload=[{"id": "a"}, {"id": "b"}])
    client = await make_client(provider=prov)
    resp = await client.get("/api/news/feed")
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["window_days"] == 1
    assert body["company"] is None
    assert body["source"] is None
    assert body["has_media"] is None
    assert body["limit"] == NEWS_DEFAULT_LIMIT
    assert body["rows"] == [{"id": "a"}, {"id": "b"}]
    assert prov.calls == [{
        "window_days": 1,
        "company_filter": None,
        "source_kind": None,
        "has_media": None,
        "limit": NEWS_DEFAULT_LIMIT,
    }]


@pytest.mark.asyncio
async def test_feed_window_label_7d_maps_to_7(make_client) -> None:
    prov = _MockNewsProvider()
    client = await make_client(provider=prov)
    resp = await client.get("/api/news/feed?window=7d")
    assert resp.status == 200
    body = await resp.json()
    assert body["window_days"] == 7
    assert prov.calls[0]["window_days"] == 7


@pytest.mark.asyncio
async def test_feed_window_label_30d_maps_to_30(make_client) -> None:
    prov = _MockNewsProvider()
    client = await make_client(provider=prov)
    resp = await client.get("/api/news/feed?window=30d")
    assert resp.status == 200
    body = await resp.json()
    assert body["window_days"] == 30
    assert prov.calls[0]["window_days"] == 30


@pytest.mark.asyncio
async def test_feed_invalid_window_falls_back_to_24h(make_client) -> None:
    prov = _MockNewsProvider()
    client = await make_client(provider=prov)
    resp = await client.get("/api/news/feed?window=junk")
    assert resp.status == 200
    body = await resp.json()
    assert body["window_days"] == 1


@pytest.mark.asyncio
async def test_feed_company_all_becomes_none(make_client) -> None:
    prov = _MockNewsProvider()
    client = await make_client(provider=prov)
    resp = await client.get("/api/news/feed?company=all")
    assert resp.status == 200
    assert prov.calls[0]["company_filter"] is None


@pytest.mark.asyncio
async def test_feed_company_threads_through(make_client) -> None:
    prov = _MockNewsProvider()
    client = await make_client(provider=prov)
    resp = await client.get("/api/news/feed?company=rubicon")
    assert resp.status == 200
    body = await resp.json()
    assert body["company"] == "rubicon"
    assert prov.calls[0]["company_filter"] == "rubicon"


@pytest.mark.asyncio
async def test_feed_source_filter_threads_through(make_client) -> None:
    prov = _MockNewsProvider()
    client = await make_client(provider=prov)
    resp = await client.get("/api/news/feed?source=discord")
    assert resp.status == 200
    body = await resp.json()
    assert body["source"] == "discord"
    assert prov.calls[0]["source_kind"] == "discord"


@pytest.mark.asyncio
async def test_feed_unknown_source_dropped_silently(make_client) -> None:
    prov = _MockNewsProvider()
    client = await make_client(provider=prov)
    resp = await client.get("/api/news/feed?source=facebook")
    assert resp.status == 200
    body = await resp.json()
    assert body["source"] is None
    assert prov.calls[0]["source_kind"] is None


@pytest.mark.asyncio
async def test_feed_has_media_true(make_client) -> None:
    prov = _MockNewsProvider()
    client = await make_client(provider=prov)
    resp = await client.get("/api/news/feed?has_media=true")
    assert resp.status == 200
    body = await resp.json()
    assert body["has_media"] is True
    assert prov.calls[0]["has_media"] is True


@pytest.mark.asyncio
async def test_feed_has_media_false(make_client) -> None:
    prov = _MockNewsProvider()
    client = await make_client(provider=prov)
    resp = await client.get("/api/news/feed?has_media=0")
    assert resp.status == 200
    body = await resp.json()
    assert body["has_media"] is False
    assert prov.calls[0]["has_media"] is False


@pytest.mark.asyncio
async def test_feed_limit_clamped_to_cap(make_client) -> None:
    prov = _MockNewsProvider()
    client = await make_client(provider=prov)
    resp = await client.get(f"/api/news/feed?limit={NEWS_LIMIT_CAP + 999}")
    assert resp.status == 200
    body = await resp.json()
    assert body["limit"] == NEWS_LIMIT_CAP
    assert prov.calls[0]["limit"] == NEWS_LIMIT_CAP


@pytest.mark.asyncio
async def test_feed_provider_exception_returns_500(make_client) -> None:
    prov = _MockNewsProvider(raise_exc=RuntimeError("db down"))
    client = await make_client(provider=prov)
    resp = await client.get("/api/news/feed")
    assert resp.status == 500
    body = await resp.json()
    assert body == {"ok": False, "error": "news-feed fetch failed"}


@pytest.mark.asyncio
async def test_feed_payload_jsonifies_decimal_and_datetime(make_client) -> None:
    rows = [
        {
            "id": "row-1",
            "sentiment": Decimal("0.75"),
            "published_at": datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
            "instruments": ["BTC/USDT"],
            "media_count": 2,
        }
    ]
    prov = _MockNewsProvider(payload=rows)
    client = await make_client(provider=prov)
    resp = await client.get("/api/news/feed?window=24h")
    assert resp.status == 200
    body = await resp.json()
    assert body["rows"] == [
        {
            "id": "row-1",
            "sentiment": "0.75",
            "published_at": "2026-05-01T09:00:00+00:00",
            "instruments": ["BTC/USDT"],
            "media_count": 2,
        }
    ]


@pytest.mark.asyncio
async def test_feed_provider_cached_across_requests(make_client) -> None:
    """The same provider instance should service every request."""
    mock = _MockNewsProvider(payload=[{"id": "x"}])
    client = await make_client(provider=mock)
    resp = await client.get("/api/news/feed")
    assert resp.status == 200
    body = await resp.json()
    assert body["rows"] == [{"id": "x"}]
    # Second call must reuse the same cached provider instance.
    resp2 = await client.get("/api/news/feed")
    assert resp2.status == 200
    assert client.app["_news_feed"] is mock
    assert len(mock.calls) == 2


@pytest.mark.asyncio
async def test_feed_full_query_combination(make_client) -> None:
    """All filters supplied at once thread through end-to-end."""
    prov = _MockNewsProvider(payload=[])
    client = await make_client(provider=prov)
    url = (
        "/api/news/feed?window=7d&company=rubicon&source=telegram"
        "&has_media=true&limit=42"
    )
    resp = await client.get(url)
    assert resp.status == 200
    body = await resp.json()
    assert body == {
        "ok": True,
        "window_days": 7,
        "company": "rubicon",
        "source": "telegram",
        "has_media": True,
        "limit": 42,
        "rows": [],
    }
    assert prov.calls == [{
        "window_days": 7,
        "company_filter": "rubicon",
        "source_kind": "telegram",
        "has_media": True,
        "limit": 42,
    }]
