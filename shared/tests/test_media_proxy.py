"""
Module: test_media_proxy
Purpose: Unit + integration tests for
    :mod:`shared.dashboard.media_proxy`. Exercises the host
    allow-list, URL validator, rate limiter, and end-to-end happy
    path against a local upstream server.
Location: /opt/tickles/shared/tests/test_media_proxy.py

Design notes:

- The DNS-resolution / IP check inside :func:`_validate_url` is
  patched out per-test (we replace ``_resolve_and_check_ips`` with a
  no-op coroutine) because we don't want test runs to hit real DNS,
  and because the happy-path test needs to forward an in-test URL
  that points at ``127.0.0.1`` — which the production validator
  would (correctly) reject as non-public.
- For the happy-path streaming test we use :mod:`aiohttp.test_utils`
  to stand up a real upstream server returning a tiny PNG, and a
  second :class:`TestClient` for the proxy itself. The proxy is
  pointed at the upstream by patching its host allow-list and DNS
  guard so the per-test localhost address is acceptable.
"""

from __future__ import annotations

import asyncio
import struct
import zlib
from typing import Any, Dict, List, Optional

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from shared.dashboard import media_proxy as mp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_tiny_png() -> bytes:
    """Return a valid 1x1 PNG byte string.

    Synthesised from the spec rather than reading a fixture file so
    the test stays self-contained.
    """
    sig = b"\x89PNG\r\n\x1a\n"

    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00\xff\xff\xff"  # one filter byte + RGB pixel
    idat = zlib.compress(raw)
    return sig + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    """Wipe the module-level rate-limit buckets between tests."""
    mp._RATE_LIMIT_BUCKETS.clear()
    yield
    mp._RATE_LIMIT_BUCKETS.clear()


# ---------------------------------------------------------------------------
# _host_in_allowlist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", [
    "s3.tradingview.com",
    "www.tradingview.com",
    "static.tradingview.com",
    "cdn.discordapp.com",
    "media.discordapp.net",
    "pbs.twimg.com",
    "i.imgur.com",
])
def test_host_in_allowlist_accepts_known_hosts(host: str) -> None:
    """All hard-coded allow-listed hosts and obvious subdomains pass."""
    assert mp._host_in_allowlist(host) is True


@pytest.mark.parametrize("host", [
    "",
    "evil.example",
    "tradingview.com.evil.example",
    "s3.tradingview.com.evil.example",
    "notdiscordapp.com",
    "pbs.twimg.com.attacker.test",
    "imgur.com",                       # bare imgur.com is NOT allow-listed; only i.imgur.com
    "xn--tradingvew-1ya.com",          # IDN homoglyph; punycode form should NOT match
    "TRADINGVIEW.COM",                 # validator lower-cases before calling; this proves
                                       # the helper itself is case-sensitive on the right side.
])
def test_host_in_allowlist_rejects_unknown_hosts(host: str) -> None:
    """Subdomain-bypass / IDN / case-sensitive checks all fail closed."""
    assert mp._host_in_allowlist(host) is False


# ---------------------------------------------------------------------------
# _validate_url
# ---------------------------------------------------------------------------


@pytest.fixture
def _patch_dns_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the DNS+IP guard with a no-op coroutine.

    Used by tests that want to assert behaviour of the URL validator
    independent of the system resolver.
    """

    async def _noop(_host: str) -> None:
        return None

    monkeypatch.setattr(mp, "_resolve_and_check_ips", _noop)


@pytest.fixture
def _patch_dns_private(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the DNS+IP guard with one that always rejects."""

    async def _reject(host: str) -> None:
        raise mp._ProxyError(502, f"resolved IP for {host} is not public")

    monkeypatch.setattr(mp, "_resolve_and_check_ips", _reject)


@pytest.mark.asyncio
async def test_validate_url_rejects_missing_param(_patch_dns_ok: None) -> None:
    with pytest.raises(mp._ProxyError) as exc:
        await mp._validate_url(None)
    assert exc.value.status == 400


@pytest.mark.asyncio
async def test_validate_url_rejects_blank_param(_patch_dns_ok: None) -> None:
    with pytest.raises(mp._ProxyError) as exc:
        await mp._validate_url("   ")
    assert exc.value.status == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "http://i.imgur.com/cat.png",     # http (not https)
    "ftp://i.imgur.com/cat.png",      # not http(s)
    "javascript:alert(1)",            # script scheme
    "file:///etc/passwd",             # file scheme
    "data:image/png;base64,AAAA",     # data url
])
async def test_validate_url_rejects_non_https_schemes(
    _patch_dns_ok: None, url: str
) -> None:
    with pytest.raises(mp._ProxyError) as exc:
        await mp._validate_url(url)
    assert exc.value.status == 400


@pytest.mark.asyncio
async def test_validate_url_rejects_non_allow_listed_host(
    _patch_dns_ok: None,
) -> None:
    with pytest.raises(mp._ProxyError) as exc:
        await mp._validate_url("https://evil.example/foo.png")
    assert exc.value.status == 403
    assert "evil.example" in exc.value.message


@pytest.mark.asyncio
async def test_validate_url_rejects_private_ip_after_resolution(
    _patch_dns_private: None,
) -> None:
    """Even an allow-listed host fails if DNS resolves to a private IP."""
    with pytest.raises(mp._ProxyError) as exc:
        await mp._validate_url("https://i.imgur.com/cat.png")
    assert exc.value.status == 502


@pytest.mark.asyncio
async def test_validate_url_accepts_allow_listed_https(
    _patch_dns_ok: None,
) -> None:
    out = await mp._validate_url("https://i.imgur.com/cat.png")
    assert out == "https://i.imgur.com/cat.png"


# ---------------------------------------------------------------------------
# _resolve_and_check_ips — async
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_and_check_ips_rejects_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """getaddrinfo returning 127.0.0.1 should be rejected."""

    async def _fake(host, port, *args, **kwargs):  # noqa: ARG001
        return [(0, 0, 0, "", ("127.0.0.1", 0))]

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", _fake)
    with pytest.raises(mp._ProxyError) as exc:
        await mp._resolve_and_check_ips("anything.example")
    assert exc.value.status == 502


@pytest.mark.asyncio
async def test_resolve_and_check_ips_rejects_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """getaddrinfo returning 10.0.0.1 should be rejected."""

    async def _fake(host, port, *args, **kwargs):  # noqa: ARG001
        return [(0, 0, 0, "", ("10.0.0.1", 0))]

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", _fake)
    with pytest.raises(mp._ProxyError) as exc:
        await mp._resolve_and_check_ips("anything.example")
    assert exc.value.status == 502


@pytest.mark.asyncio
async def test_resolve_and_check_ips_accepts_public(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A public IP must pass without raising."""

    async def _fake(host, port, *args, **kwargs):  # noqa: ARG001
        return [(0, 0, 0, "", ("8.8.8.8", 0))]

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", _fake)
    await mp._resolve_and_check_ips("anything.example")  # must not raise


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limiter_allows_up_to_cap() -> None:
    """The 1st through Nth requests in the window are admitted."""
    key = "test-bucket-1"
    for _ in range(mp._RATE_LIMIT_MAX_REQUESTS):
        retry = await mp._check_rate_limit(key)
        assert retry is None


@pytest.mark.asyncio
async def test_rate_limiter_rejects_overflow() -> None:
    """Request N+1 in the window is rejected with a positive Retry-After."""
    key = "test-bucket-2"
    for _ in range(mp._RATE_LIMIT_MAX_REQUESTS):
        await mp._check_rate_limit(key)
    retry = await mp._check_rate_limit(key)
    assert retry is not None and retry > 0


@pytest.mark.asyncio
async def test_rate_limiter_buckets_are_independent() -> None:
    """Filling one bucket does not affect a different key."""
    key_a = "test-bucket-a"
    key_b = "test-bucket-b"
    for _ in range(mp._RATE_LIMIT_MAX_REQUESTS):
        await mp._check_rate_limit(key_a)
    # bucket A is full
    assert (await mp._check_rate_limit(key_a)) is not None
    # bucket B is fresh
    assert (await mp._check_rate_limit(key_b)) is None


# ---------------------------------------------------------------------------
# _normalise_content_type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("image/png", "image/png"),
    ("image/png; charset=binary", "image/png"),
    ("IMAGE/PNG", "image/png"),
    ("  image/jpeg ; foo=bar ", "image/jpeg"),
    (None, ""),
    ("", ""),
])
def test_normalise_content_type(raw: Optional[str], expected: str) -> None:
    assert mp._normalise_content_type(raw) == expected


# ---------------------------------------------------------------------------
# SVG rejection (via the streaming path).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Happy-path: end-to-end through a real proxy + real upstream.
#
# Strategy:
#   1. Stand up an "upstream" aiohttp server that returns a PNG.
#   2. Stand up a "proxy" aiohttp server with handle_media_proxy.
#   3. Patch the validator's allow-list + DNS guard so the proxy
#      will accept ``http://127.0.0.1:<port>/foo.png`` for the
#      duration of the test only.
# ---------------------------------------------------------------------------


@pytest.fixture
async def upstream_server():
    """Yield (TestClient, [requests]) for a configurable upstream."""
    requests: List[Dict[str, Any]] = []
    state: Dict[str, Any] = {
        "body": _build_tiny_png(),
        "content_type": "image/png",
        "status": 200,
    }

    async def _handler(request: web.Request) -> web.Response:
        requests.append({
            "path": request.path,
            "headers": dict(request.headers),
            "method": request.method,
        })
        return web.Response(
            body=state["body"],
            status=state["status"],
            headers={"Content-Type": state["content_type"]},
        )

    app = web.Application()
    app.router.add_get("/{tail:.*}", _handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, requests, state
    finally:
        await client.close()


@pytest.fixture
async def proxy_client(monkeypatch: pytest.MonkeyPatch):
    """Yield a TestClient for the media_proxy mounted at ``/api/media/proxy``.

    The host allow-list and DNS guard are relaxed to accept the
    in-test ``127.0.0.1`` upstream. Only the URL validator is
    relaxed — every other guard (scheme, content-type, body cap,
    rate limiter) is exercised normally.
    """
    # Allow http (the in-test upstream isn't TLS) and any host.
    original_validate = mp._validate_url

    async def _relaxed_validate(raw: Optional[str]) -> str:
        if raw is None or not raw.strip():
            raise mp._ProxyError(400, "url query parameter is required")
        return raw.strip()

    monkeypatch.setattr(mp, "_validate_url", _relaxed_validate)

    app = web.Application()
    mp.attach_routes(app, prefix="")
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client
    finally:
        # Restore (monkeypatch fixture does this automatically on teardown).
        monkeypatch.setattr(mp, "_validate_url", original_validate)
        await client.close()


@pytest.mark.asyncio
async def test_proxy_streams_png_happy_path(
    upstream_server, proxy_client
) -> None:
    """A 200 PNG upstream is streamed back with the right content type."""
    upstream, _requests, _state = upstream_server
    upstream_url = str(upstream.make_url("/chart.png"))
    resp = await proxy_client.get(
        "/api/media/proxy", params={"url": upstream_url}
    )
    assert resp.status == 200, await resp.text()
    assert resp.headers["Content-Type"] == "image/png"
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    body = await resp.read()
    assert body.startswith(b"\x89PNG"), "body must be a real PNG"
    assert body == _build_tiny_png()


@pytest.mark.asyncio
async def test_proxy_rejects_svg_upstream(
    upstream_server, proxy_client
) -> None:
    """SVG is dangerous (can carry <script>) and must be rejected 415."""
    upstream, _requests, state = upstream_server
    state["body"] = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"
    state["content_type"] = "image/svg+xml"
    upstream_url = str(upstream.make_url("/evil.svg"))
    resp = await proxy_client.get(
        "/api/media/proxy", params={"url": upstream_url}
    )
    assert resp.status == 415
    body = await resp.json()
    assert body["ok"] is False
    assert "image/svg" in body["error"]


@pytest.mark.asyncio
async def test_proxy_rate_limit_returns_429(
    upstream_server, proxy_client
) -> None:
    """The 31st request from the same bucket gets HTTP 429."""
    upstream, _requests, _state = upstream_server
    upstream_url = str(upstream.make_url("/chart.png"))
    # Fire exactly RATE_LIMIT_MAX_REQUESTS allowed calls.
    for _ in range(mp._RATE_LIMIT_MAX_REQUESTS):
        resp = await proxy_client.get(
            "/api/media/proxy", params={"url": upstream_url}
        )
        assert resp.status == 200
        await resp.read()
    # Next one trips the limiter.
    resp = await proxy_client.get(
        "/api/media/proxy", params={"url": upstream_url}
    )
    assert resp.status == 429
    assert "Retry-After" in resp.headers
    assert int(resp.headers["Retry-After"]) >= 1
