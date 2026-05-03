"""
Module: test_config_routes
Purpose: Smoke + behaviour tests for the Phase X.6 Config snapshot HTTP
endpoint exposed by :mod:`shared.dashboard.config_routes`. We inject
a mock provider into ``app["_config_snapshot"]`` so the tests stay
db-free and exercise the parser/JSON-coercion layer in isolation.
Location: /opt/tickles/shared/tests/test_config_routes.py
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from shared.dashboard.config_provider import SECRET_REDACTION
from shared.dashboard.config_routes import (
    _jsonify,
    _parse_company,
    _provider,
    attach_routes,
)


# ---------------------------------------------------------------------------
# Mock provider — captures call args, returns a fixed payload.
# ---------------------------------------------------------------------------


class _MockConfigProvider:
    """Stand-in for :class:`ConfigSnapshotProvider`."""

    def __init__(
        self,
        payload: Optional[Dict[str, Any]] = None,
        raise_exc: Optional[Exception] = None,
    ) -> None:
        self.payload = payload if payload is not None else {"shared": {}, "companies": {}}
        self.raise_exc = raise_exc
        self.calls: List[Dict[str, Any]] = []

    async def fetch(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(dict(kwargs))
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.payload


def _build_app(provider: Optional[_MockConfigProvider] = None) -> web.Application:
    """Build an aiohttp app with the config routes wired up.

    Args:
        provider: Optional mock provider stored at ``app["_config_snapshot"]``.

    Returns:
        Configured aiohttp application with routes attached at the root.
    """
    app = web.Application()
    if provider is not None:
        app["_config_snapshot"] = provider
    attach_routes(app, prefix="")
    return app


# ---------------------------------------------------------------------------
# _parse_company
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    (None, None),
    ("", None),
    ("   ", None),
    ("all", None),
    ("ALL", None),
    (" all ", None),
    ("rubicon", "rubicon"),
    ("  rubicon  ", "rubicon"),
    ("RUBICON", "rubicon"),
    ("Rubicon", "rubicon"),
    ("  RUBICON  ", "rubicon"),
])
def test_parse_company_normalises(raw: Optional[str], expected: Optional[str]) -> None:
    assert _parse_company(raw) == expected


# ---------------------------------------------------------------------------
# _jsonify
# ---------------------------------------------------------------------------


def test_jsonify_passes_primitives_through() -> None:
    for v in (None, True, False, 0, 1, -1, 1.5, "x"):
        assert _jsonify(v) == v


def test_jsonify_decimal_to_str() -> None:
    assert _jsonify(Decimal("1.50")) == "1.50"


def test_jsonify_datetime_to_iso() -> None:
    ts = datetime(2026, 5, 4, 1, 2, 3, tzinfo=timezone.utc)
    assert _jsonify(ts) == "2026-05-04T01:02:03+00:00"


def test_jsonify_date_to_iso() -> None:
    assert _jsonify(date(2026, 5, 4)) == "2026-05-04"


def test_jsonify_recursive_dict_and_list() -> None:
    payload = {
        "n": 1,
        "ts": datetime(2026, 5, 4, tzinfo=timezone.utc),
        "items": [Decimal("0.5"), {"inner": Decimal("1")}],
        "tup": (1, 2),
        "set": {3, 4},
    }
    out = _jsonify(payload)
    assert out["n"] == 1
    assert out["ts"] == "2026-05-04T00:00:00+00:00"
    assert out["items"] == ["0.5", {"inner": "1"}]
    assert out["tup"] == [1, 2]
    # set ordering not guaranteed; cast for stability.
    assert sorted(out["set"]) == [3, 4]


def test_jsonify_dict_keys_become_strings() -> None:
    out = _jsonify({1: "a", "k": "b"})
    assert out == {"1": "a", "k": "b"}


def test_jsonify_unknown_object_falls_back_to_str() -> None:
    class _Custom:
        def __str__(self) -> str:  # noqa: D401
            return "custom-repr"

    assert _jsonify(_Custom()) == "custom-repr"


# ---------------------------------------------------------------------------
# _provider — caching & wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_returns_cached_instance() -> None:
    app = web.Application()
    sentinel = _MockConfigProvider()
    app["_config_snapshot"] = sentinel

    class _Req:
        def __init__(self, a: web.Application) -> None:
            self.app = a

    assert _provider(_Req(app)) is sentinel


@pytest.mark.asyncio
async def test_provider_lazily_creates_instance() -> None:
    app = web.Application()

    class _Req:
        def __init__(self, a: web.Application) -> None:
            self.app = a

    out = _provider(_Req(app))
    assert out is not None
    # Subsequent calls return the same cached instance.
    again = _provider(_Req(app))
    assert again is out
    assert app["_config_snapshot"] is out


# ---------------------------------------------------------------------------
# attach_routes — prefix wiring
# ---------------------------------------------------------------------------


def test_attach_routes_registers_under_prefix() -> None:
    app = web.Application()
    attach_routes(app, prefix="")
    paths = {r.resource.canonical for r in app.router.routes()}
    assert "/api/config/snapshot" in paths


def test_attach_routes_supports_dual_mount() -> None:
    app = web.Application()
    attach_routes(app, prefix="")
    attach_routes(app, prefix="/dashboard")
    paths = {r.resource.canonical for r in app.router.routes()}
    assert "/api/config/snapshot" in paths
    assert "/dashboard/api/config/snapshot" in paths


# ---------------------------------------------------------------------------
# /api/config/snapshot — happy paths
# ---------------------------------------------------------------------------


@pytest.fixture
async def client_factory(aiohttp_client):
    async def _make(provider: Optional[_MockConfigProvider] = None) -> TestClient:
        return await aiohttp_client(_build_app(provider))

    return _make


@pytest.mark.asyncio
async def test_snapshot_happy_path_returns_payload(client_factory) -> None:
    provider = _MockConfigProvider({
        "shared": {
            "auth": [{"key": "api_token", "value": SECRET_REDACTION, "is_secret": True, "updated_at": None}],
            "candles": [{"key": "retention_1m_days", "value": "90", "is_secret": False, "updated_at": None}],
        },
        "companies": {
            "rubicon": [{"key": "trading_capital", "value": "500", "updated_at": None}],
        },
    })
    client = await client_factory(provider)
    resp = await client.get("/api/config/snapshot")
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["company"] is None
    assert body["shared"]["auth"][0]["value"] == SECRET_REDACTION
    assert body["companies"]["rubicon"][0]["key"] == "trading_capital"


@pytest.mark.asyncio
async def test_snapshot_passes_company_filter_to_provider(client_factory) -> None:
    provider = _MockConfigProvider({"shared": {}, "companies": {"rubicon": []}})
    client = await client_factory(provider)
    resp = await client.get("/api/config/snapshot?company=rubicon")
    assert resp.status == 200
    assert provider.calls[-1] == {"company_filter": "rubicon"}


@pytest.mark.asyncio
async def test_snapshot_company_all_treated_as_no_filter(client_factory) -> None:
    provider = _MockConfigProvider()
    client = await client_factory(provider)
    resp = await client.get("/api/config/snapshot?company=all")
    assert resp.status == 200
    body = await resp.json()
    assert body["company"] is None
    assert provider.calls[-1] == {"company_filter": None}


@pytest.mark.asyncio
async def test_snapshot_company_blank_treated_as_no_filter(client_factory) -> None:
    provider = _MockConfigProvider()
    client = await client_factory(provider)
    resp = await client.get("/api/config/snapshot?company=")
    body = await resp.json()
    assert body["company"] is None
    assert provider.calls[-1] == {"company_filter": None}


@pytest.mark.asyncio
async def test_snapshot_company_whitespace_normalised(client_factory) -> None:
    provider = _MockConfigProvider()
    client = await client_factory(provider)
    resp = await client.get("/api/config/snapshot?company=%20rubicon%20")
    body = await resp.json()
    assert body["company"] == "rubicon"
    assert provider.calls[-1] == {"company_filter": "rubicon"}


@pytest.mark.asyncio
async def test_snapshot_company_uppercase_lowercased(client_factory) -> None:
    # Regression: the active-companies list always uses lower-case
    # short-names, so an uppercase URL like ?company=RUBICON would
    # have silently returned an empty per-company subset before C1
    # was fixed.
    provider = _MockConfigProvider({
        "shared": {},
        "companies": {"rubicon": [{"key": "k", "value": "v", "updated_at": None}]},
    })
    client = await client_factory(provider)
    resp = await client.get("/api/config/snapshot?company=RUBICON")
    body = await resp.json()
    assert body["company"] == "rubicon"
    assert provider.calls[-1] == {"company_filter": "rubicon"}
    assert body["companies"]["rubicon"][0]["key"] == "k"


@pytest.mark.asyncio
async def test_snapshot_empty_payload_round_trips(client_factory) -> None:
    provider = _MockConfigProvider({"shared": {}, "companies": {}})
    client = await client_factory(provider)
    resp = await client.get("/api/config/snapshot")
    body = await resp.json()
    assert body == {"ok": True, "company": None, "shared": {}, "companies": {}}


# ---------------------------------------------------------------------------
# /api/config/snapshot — failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_returns_500_when_provider_raises(client_factory) -> None:
    # Defense-in-depth: provider promises never to raise, but the
    # handler still has to handle it without leaking internals.
    provider = _MockConfigProvider(raise_exc=RuntimeError("kaboom"))
    client = await client_factory(provider)
    resp = await client.get("/api/config/snapshot")
    assert resp.status == 500
    body = await resp.json()
    assert body == {"ok": False, "error": "config snapshot fetch failed"}


# ---------------------------------------------------------------------------
# JSON coercion through the wire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_coerces_decimal_and_datetime_via_jsonify(client_factory) -> None:
    # If the provider ever surfaces non-stringified Decimal / datetime
    # values (e.g. a future schema change widens config_value to NUMERIC),
    # the route must still serialise them.
    provider = _MockConfigProvider({
        "shared": {
            "ns": [{
                "key": "k",
                "value": Decimal("12.50"),
                "is_secret": False,
                "updated_at": datetime(2026, 5, 4, tzinfo=timezone.utc),
            }],
        },
        "companies": {},
    })
    client = await client_factory(provider)
    resp = await client.get("/api/config/snapshot")
    body = await resp.json()
    row = body["shared"]["ns"][0]
    assert row["value"] == "12.50"
    assert row["updated_at"] == "2026-05-04T00:00:00+00:00"


@pytest.mark.asyncio
async def test_snapshot_provider_cached_across_requests(client_factory) -> None:
    provider = _MockConfigProvider()
    client = await client_factory(provider)
    await client.get("/api/config/snapshot")
    await client.get("/api/config/snapshot?company=x")
    # Both requests went through the same mock instance.
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_snapshot_unknown_query_params_ignored(client_factory) -> None:
    # Belt-and-braces: extra query strings shouldn't 4xx; the handler
    # only reads ``company`` and ignores the rest.
    provider = _MockConfigProvider()
    client = await client_factory(provider)
    resp = await client.get("/api/config/snapshot?garbage=1&company=rubicon&other=y")
    assert resp.status == 200
    assert provider.calls[-1] == {"company_filter": "rubicon"}


@pytest.mark.asyncio
async def test_snapshot_response_is_application_json(client_factory) -> None:
    provider = _MockConfigProvider()
    client = await client_factory(provider)
    resp = await client.get("/api/config/snapshot")
    assert resp.headers.get("Content-Type", "").startswith("application/json")


@pytest.mark.asyncio
async def test_snapshot_secret_value_round_trips_through_jsonify(client_factory) -> None:
    # Defensive — the provider already redacts, but verify a value
    # that happens to equal the redaction sentinel survives JSON
    # serialisation untouched. (Catches a future bug where someone
    # adds a sentinel-stripping step in _jsonify.)
    provider = _MockConfigProvider({
        "shared": {"ns": [{"key": "k", "value": SECRET_REDACTION, "is_secret": True, "updated_at": None}]},
        "companies": {},
    })
    client = await client_factory(provider)
    resp = await client.get("/api/config/snapshot")
    body = await resp.json()
    assert body["shared"]["ns"][0]["value"] == SECRET_REDACTION
