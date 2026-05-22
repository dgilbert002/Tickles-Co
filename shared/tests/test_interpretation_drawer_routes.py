"""
Module: test_interpretation_drawer_routes
Purpose: Smoke + behaviour tests for the Phase X.5 Cross-tab
Interpretation Drawer HTTP endpoint exposed by
:mod:`shared.dashboard.interpretation_drawer_routes`. We inject a
mock provider into ``app["_interp_drawer"]`` so the tests stay
db-free and exercise the parser/JSON-coercion layer in isolation.
Location: /opt/tickles/shared/tests/test_interpretation_drawer_routes.py
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from shared.dashboard.interpretation_drawer_provider import (
    DRAWER_DEFAULT_LIMIT,
    DRAWER_LIMIT_CAP,
)
from shared.dashboard.interpretation_drawer_routes import (
    _jsonify,
    _parse_limit,
    _parse_positive_int,
    attach_routes,
)


# ---------------------------------------------------------------------------
# Mock provider — captures call args, returns a fixed payload.
# ---------------------------------------------------------------------------


class _MockDrawerProvider:
    """Stand-in for :class:`InterpretationDrawerProvider` recording calls.

    Optionally exposes the two enrichment methods the real provider
    grew in Slice 1 (``fetch_media_for_news_item`` and
    ``fetch_news_item_header``). They are only attached when the
    caller passes a non-``None`` payload, so existing tests that don't
    care about enrichment continue to exercise the legacy code path
    where the route uses ``getattr(..., None)`` to skip the optional
    methods.
    """

    def __init__(
        self,
        by_news_payload: Optional[List[Dict[str, Any]]] = None,
        by_id_payload: Optional[List[Dict[str, Any]]] = None,
        raise_exc: Optional[Exception] = None,
        media_payload: Optional[List[Dict[str, Any]]] = None,
        header_payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.by_news_payload = by_news_payload if by_news_payload is not None else []
        self.by_id_payload = by_id_payload if by_id_payload is not None else []
        self.raise_exc = raise_exc
        self.news_calls: List[Dict[str, Any]] = []
        self.id_calls: List[Dict[str, Any]] = []
        self.media_calls: List[Dict[str, Any]] = []
        self.header_calls: List[Dict[str, Any]] = []
        self._media_payload = media_payload
        self._header_payload = header_payload
        if media_payload is not None:
            self.fetch_media_for_news_item = self._fetch_media_for_news_item  # type: ignore[assignment]
        if header_payload is not None:
            self.fetch_news_item_header = self._fetch_news_item_header  # type: ignore[assignment]

    async def fetch_by_news_item(
        self, news_item_id: int, *, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        self.news_calls.append({"news_item_id": news_item_id, "limit": limit})
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.by_news_payload

    async def fetch_by_id(self, interp_id: int) -> List[Dict[str, Any]]:
        self.id_calls.append({"interp_id": interp_id})
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.by_id_payload

    async def _fetch_media_for_news_item(
        self, news_item_id: int
    ) -> List[Dict[str, Any]]:
        self.media_calls.append({"news_item_id": news_item_id})
        return list(self._media_payload or [])

    async def _fetch_news_item_header(
        self, news_item_id: int
    ) -> Optional[Dict[str, Any]]:
        self.header_calls.append({"news_item_id": news_item_id})
        return self._header_payload


def _build_app(provider: Optional[_MockDrawerProvider] = None) -> web.Application:
    """Build an aiohttp app with the drawer routes wired up.

    Args:
        provider: Optional mock provider stored at ``app["_interp_drawer"]``.

    Returns:
        Configured aiohttp application with routes attached at the root.
    """
    app = web.Application()
    if provider is not None:
        app["_interp_drawer"] = provider
    attach_routes(app, prefix="")
    return app


# ---------------------------------------------------------------------------
# _parse_positive_int
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("1", 1),
    ("42", 42),
    ("999999", 999999),
    (" 17 ", 17),
])
def test_parse_positive_int_accepts_positive(raw: str, expected: int) -> None:
    assert _parse_positive_int(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "0", "-1", "-42", "abc", "1.5", "junk"])
def test_parse_positive_int_rejects_invalid(raw: Any) -> None:
    assert _parse_positive_int(raw) is None


# ---------------------------------------------------------------------------
# _parse_limit
# ---------------------------------------------------------------------------


def test_parse_limit_uses_default_when_missing() -> None:
    assert _parse_limit(None) == DRAWER_DEFAULT_LIMIT
    assert _parse_limit("") == DRAWER_DEFAULT_LIMIT


def test_parse_limit_uses_default_when_unparseable() -> None:
    assert _parse_limit("xyz") == DRAWER_DEFAULT_LIMIT
    assert _parse_limit("1.5") == DRAWER_DEFAULT_LIMIT


def test_parse_limit_clamps_to_cap() -> None:
    assert _parse_limit(str(DRAWER_LIMIT_CAP + 50)) == DRAWER_LIMIT_CAP
    assert _parse_limit("9999") == DRAWER_LIMIT_CAP


def test_parse_limit_minimum_one() -> None:
    assert _parse_limit("0") == 1
    assert _parse_limit("-7") == 1


def test_parse_limit_passes_through_in_range() -> None:
    assert _parse_limit("5") == 5
    assert _parse_limit("1") == 1
    assert _parse_limit(str(DRAWER_LIMIT_CAP)) == DRAWER_LIMIT_CAP


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


def test_jsonify_handles_date() -> None:
    assert _jsonify(date(2026, 5, 1)) == "2026-05-01"


def test_jsonify_walks_nested_structures() -> None:
    payload = {
        "rows": [
            {
                "id": 7,
                "confidence": Decimal("0.82"),
                "ts": datetime(2026, 5, 1, tzinfo=timezone.utc),
                "tags": ("breakout", "btc"),
                "nested": {"sub": Decimal("-0.5")},
            },
        ],
    }
    out = _jsonify(payload)
    assert out == {
        "rows": [
            {
                "id": 7,
                "confidence": "0.82",
                "ts": "2026-05-01T00:00:00+00:00",
                "tags": ["breakout", "btc"],
                "nested": {"sub": "-0.5"},
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


def test_attach_routes_registers_drawer_endpoint_under_prefix() -> None:
    app = web.Application()
    attach_routes(app, prefix="/dashboard")
    paths = sorted({r.resource.canonical for r in app.router.routes()})
    assert paths == ["/dashboard/api/interpretations/drawer"]


def test_attach_routes_supports_dual_mount() -> None:
    """Both '' and '/dashboard' prefixes can coexist on one app."""
    app = web.Application()
    attach_routes(app, prefix="")
    attach_routes(app, prefix="/dashboard")
    paths = {r.resource.canonical for r in app.router.routes()}
    assert "/api/interpretations/drawer" in paths
    assert "/dashboard/api/interpretations/drawer" in paths


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

    async def _factory(provider: Optional[_MockDrawerProvider] = None) -> TestClient:
        app = _build_app(provider=provider)
        client = TestClient(TestServer(app))
        await client.start_server()
        clients.append(client)
        return client

    yield _factory

    for c in clients:
        await c.close()


@pytest.mark.asyncio
async def test_drawer_missing_both_ids_returns_400(make_client) -> None:
    prov = _MockDrawerProvider()
    client = await make_client(provider=prov)
    resp = await client.get("/api/interpretations/drawer")
    assert resp.status == 400
    body = await resp.json()
    assert body == {
        "ok": False,
        "error": "must supply exactly one of: news_item_id, id",
    }
    # Provider must NOT have been called.
    assert prov.news_calls == []
    assert prov.id_calls == []


@pytest.mark.asyncio
async def test_drawer_both_ids_returns_400(make_client) -> None:
    prov = _MockDrawerProvider()
    client = await make_client(provider=prov)
    resp = await client.get("/api/interpretations/drawer?news_item_id=1&id=2")
    assert resp.status == 400
    body = await resp.json()
    assert body == {
        "ok": False,
        "error": "supply only one of: news_item_id, id",
    }
    assert prov.news_calls == []
    assert prov.id_calls == []


@pytest.mark.asyncio
async def test_drawer_invalid_news_item_id_treated_as_missing(make_client) -> None:
    """A non-positive / unparseable id behaves like 'no id supplied' → 400."""
    prov = _MockDrawerProvider()
    client = await make_client(provider=prov)
    resp = await client.get("/api/interpretations/drawer?news_item_id=junk")
    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "must supply exactly one of: news_item_id, id"
    assert prov.news_calls == []


@pytest.mark.asyncio
async def test_drawer_invalid_id_treated_as_missing(make_client) -> None:
    prov = _MockDrawerProvider()
    client = await make_client(provider=prov)
    resp = await client.get("/api/interpretations/drawer?id=-7")
    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "must supply exactly one of: news_item_id, id"
    assert prov.id_calls == []


@pytest.mark.asyncio
async def test_drawer_news_item_id_happy_path(make_client) -> None:
    rows = [{"id": 11, "news_item_id": 99}, {"id": 12, "news_item_id": 99}]
    prov = _MockDrawerProvider(by_news_payload=rows)
    client = await make_client(provider=prov)
    resp = await client.get("/api/interpretations/drawer?news_item_id=99")
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["news_item_id"] == 99
    assert body["id"] is None
    assert body["limit"] == DRAWER_DEFAULT_LIMIT
    assert body["rows"] == rows
    assert prov.news_calls == [
        {"news_item_id": 99, "limit": DRAWER_DEFAULT_LIMIT}
    ]
    assert prov.id_calls == []


@pytest.mark.asyncio
async def test_drawer_id_happy_path(make_client) -> None:
    rows = [{"id": 555, "news_item_id": 12}]
    prov = _MockDrawerProvider(by_id_payload=rows)
    client = await make_client(provider=prov)
    resp = await client.get("/api/interpretations/drawer?id=555")
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["news_item_id"] is None
    assert body["id"] == 555
    # limit is intentionally None when fetching a single id.
    assert body["limit"] is None
    assert body["rows"] == rows
    assert prov.id_calls == [{"interp_id": 555}]
    assert prov.news_calls == []


@pytest.mark.asyncio
async def test_drawer_news_item_id_threads_custom_limit(make_client) -> None:
    prov = _MockDrawerProvider(by_news_payload=[])
    client = await make_client(provider=prov)
    resp = await client.get("/api/interpretations/drawer?news_item_id=42&limit=5")
    assert resp.status == 200
    body = await resp.json()
    assert body["limit"] == 5
    assert prov.news_calls == [{"news_item_id": 42, "limit": 5}]


@pytest.mark.asyncio
async def test_drawer_limit_clamped_to_cap(make_client) -> None:
    prov = _MockDrawerProvider(by_news_payload=[])
    client = await make_client(provider=prov)
    url = f"/api/interpretations/drawer?news_item_id=1&limit={DRAWER_LIMIT_CAP + 999}"
    resp = await client.get(url)
    assert resp.status == 200
    body = await resp.json()
    assert body["limit"] == DRAWER_LIMIT_CAP
    assert prov.news_calls[0]["limit"] == DRAWER_LIMIT_CAP


@pytest.mark.asyncio
async def test_drawer_limit_floor_is_one(make_client) -> None:
    prov = _MockDrawerProvider(by_news_payload=[])
    client = await make_client(provider=prov)
    resp = await client.get("/api/interpretations/drawer?news_item_id=1&limit=0")
    assert resp.status == 200
    body = await resp.json()
    assert body["limit"] == 1
    assert prov.news_calls[0]["limit"] == 1


@pytest.mark.asyncio
async def test_drawer_id_path_ignores_limit_param(make_client) -> None:
    """When ``id`` is supplied, ``limit`` is parsed but not threaded
    into the provider, and is reported as ``None`` in the response."""
    prov = _MockDrawerProvider(by_id_payload=[{"id": 1}])
    client = await make_client(provider=prov)
    resp = await client.get("/api/interpretations/drawer?id=1&limit=7")
    assert resp.status == 200
    body = await resp.json()
    assert body["limit"] is None
    assert prov.id_calls == [{"interp_id": 1}]


@pytest.mark.asyncio
async def test_drawer_provider_exception_returns_500(make_client) -> None:
    prov = _MockDrawerProvider(raise_exc=RuntimeError("db down"))
    client = await make_client(provider=prov)
    resp = await client.get("/api/interpretations/drawer?news_item_id=1")
    assert resp.status == 500
    body = await resp.json()
    assert body == {
        "ok": False,
        "error": "interpretation-drawer fetch failed",
    }


@pytest.mark.asyncio
async def test_drawer_provider_exception_on_id_path_returns_500(make_client) -> None:
    prov = _MockDrawerProvider(raise_exc=ValueError("kaboom"))
    client = await make_client(provider=prov)
    resp = await client.get("/api/interpretations/drawer?id=1")
    assert resp.status == 500
    body = await resp.json()
    assert body["ok"] is False


@pytest.mark.asyncio
async def test_drawer_payload_jsonifies_decimal_and_datetime(make_client) -> None:
    rows = [
        {
            "id": 7,
            "confidence": Decimal("0.82"),
            "created_at": datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
            "tags": ["breakout", "btc"],
            "nested": {"score": Decimal("1.25")},
        }
    ]
    prov = _MockDrawerProvider(by_news_payload=rows)
    client = await make_client(provider=prov)
    resp = await client.get("/api/interpretations/drawer?news_item_id=1")
    assert resp.status == 200
    body = await resp.json()
    assert body["rows"] == [
        {
            "id": 7,
            "confidence": "0.82",
            "created_at": "2026-05-01T09:00:00+00:00",
            "tags": ["breakout", "btc"],
            "nested": {"score": "1.25"},
        }
    ]


@pytest.mark.asyncio
async def test_drawer_provider_cached_across_requests(make_client) -> None:
    """The same provider instance should service every request."""
    prov = _MockDrawerProvider(by_news_payload=[{"id": 1}])
    client = await make_client(provider=prov)
    resp = await client.get("/api/interpretations/drawer?news_item_id=1")
    assert resp.status == 200
    resp2 = await client.get("/api/interpretations/drawer?news_item_id=2")
    assert resp2.status == 200
    assert client.app["_interp_drawer"] is prov
    assert len(prov.news_calls) == 2


@pytest.mark.asyncio
async def test_drawer_empty_rows_round_trip(make_client) -> None:
    prov = _MockDrawerProvider(by_news_payload=[])
    client = await make_client(provider=prov)
    resp = await client.get("/api/interpretations/drawer?news_item_id=1")
    assert resp.status == 200
    body = await resp.json()
    assert body == {
        "ok": True,
        "news_item_id": 1,
        "id": None,
        "limit": DRAWER_DEFAULT_LIMIT,
        "rows": [],
    }


# ---------------------------------------------------------------------------
# Slice 1 §3.5 — empty-rows enrichment with news_item_header.
#
# Regression coverage for the P0 found in the first review:
# ``fetch_news_item_header`` returns ``Optional[Dict]`` (a single dict
# or ``None``), but the route used to treat the result as a list and
# unpack ``header_list[0]``, which crashed with ``KeyError: 0`` on a
# real provider response. The test below uses a mock provider whose
# ``fetch_news_item_header`` returns a real dict and asserts the route
# produces a 200 with the dict surfaced under ``news_item``.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drawer_empty_rows_surfaces_news_item_header(make_client) -> None:
    """Empty interpretations + provider header → 200 with ``news_item`` dict.

    This test would have caught the P0 (``header_list[0]`` on a dict
    raised an unhandled ``KeyError`` and bubbled up as a 500).
    """
    header = {
        "id": 99,
        "headline": "BTC breaks 70k",
        "author": "alice",
        "source": "discord",
        "channel_name": "alpha-room",
        "collected_at": datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        "published_at": None,
        "has_media": True,
        "media_count": 2,
    }
    prov = _MockDrawerProvider(
        by_news_payload=[],
        media_payload=[],
        header_payload=header,
    )
    client = await make_client(provider=prov)
    resp = await client.get("/api/interpretations/drawer?news_item_id=99")
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["ok"] is True
    assert body["rows"] == []
    assert "news_item" in body, "news_item must surface when rows are empty"
    assert body["news_item"]["id"] == 99
    assert body["news_item"]["headline"] == "BTC breaks 70k"
    assert body["news_item"]["author"] == "alice"
    assert body["news_item"]["channel_name"] == "alpha-room"
    assert body["news_item"]["has_media"] is True
    assert body["news_item"]["media_count"] == 2
    # The datetime should round-trip through _jsonify.
    assert body["news_item"]["collected_at"] == "2026-05-01T12:00:00+00:00"
    assert prov.header_calls == [{"news_item_id": 99}]


@pytest.mark.asyncio
async def test_drawer_news_item_header_omitted_when_rows_present(make_client) -> None:
    """When rows exist the news_item header is NOT surfaced.

    Slice 1 §3.5 only renders the header for the empty-drawer case;
    when interpretations exist the frontend reads ``rows[0].news``.
    """
    header = {"id": 5, "headline": "ignored", "media_count": 0}
    prov = _MockDrawerProvider(
        by_news_payload=[{"id": 1, "news_item_id": 5}],
        media_payload=[],
        header_payload=header,
    )
    client = await make_client(provider=prov)
    resp = await client.get("/api/interpretations/drawer?news_item_id=5")
    assert resp.status == 200
    body = await resp.json()
    assert body["rows"] == [{"id": 1, "news_item_id": 5}]
    assert "news_item" not in body


@pytest.mark.asyncio
async def test_drawer_header_provider_failure_does_not_break_response(make_client) -> None:
    """A failing header provider degrades gracefully (no 500).

    The rows fetch is the only required call; gallery / header are
    best-effort and may legitimately raise. The handler must drop the
    failed enrichment and still return 200.
    """

    class _BoomProvider(_MockDrawerProvider):
        async def _fetch_news_item_header(
            self, news_item_id: int
        ) -> Optional[Dict[str, Any]]:
            raise RuntimeError("header query exploded")

    prov = _BoomProvider(
        by_news_payload=[],
        media_payload=[],
        header_payload={"id": 1},  # triggers attribute attachment
    )
    client = await make_client(provider=prov)
    resp = await client.get("/api/interpretations/drawer?news_item_id=1")
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["rows"] == []
    assert "news_item" not in body
