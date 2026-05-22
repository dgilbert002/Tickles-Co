"""
Module: media_proxy
Purpose: aiohttp handler that proxies external chart images for the
         interpretation drawer, with a host allow-list, SSRF guards,
         a per-session token-bucket rate limit, and a raw-bytes body
         cap.
Location: /opt/tickles/shared/dashboard/media_proxy.py

Slice 1 — Drawer UX Overhaul (§3.2). The frontend uses this when a
``media_items.source_url`` points at TradingView / Discord / Twitter /
Imgur and the browser cannot load the URL directly (referrer / CORS /
hotlink-protection). All other media — anything we have a local copy
of — flows through the existing :func:`shared.dashboard.server.handle_media`
path-param route.

Security posture (see plan §6.1):

- **Primary defence:** strict host allow-list. Only six third-party
  domains we know serve charts.
- **Secondary defence:** post-resolution IP check rejects private,
  loopback, link-local, and reserved addresses. Documented residual
  gap: DNS-rebind on an allow-listed domain. Slice 1 accepts that.
- **Tertiary defence:** in-process per-session token bucket
  (30 req / 60 s) caps blast radius. State is wiped on dashboard
  restart; that is acceptable for the single-instance dashboard.

Body handling:

- ``auto_decompress=False`` so the 10 MB cap applies to raw on-the-wire
  bytes (a gzipped bomb would otherwise expand past the cap).
- Upstream ``Content-Encoding`` is forwarded so the browser
  decompresses normally.
- Only ``image/png|jpeg|webp|gif`` is forwarded. SVG is rejected with
  HTTP 415 (it can carry ``<script>``).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple
from urllib.parse import urlparse

import aiohttp
from aiohttp import web

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Allow-listed hosts. Wildcard suffixes (``.tradingview.com``) match any
# subdomain on a strict dot boundary so ``s3.tradingview.com.evil.example``
# does NOT match.
_MEDIA_PROXY_ALLOWED_HOSTS: Tuple[str, ...] = (
    "s3.tradingview.com",
    ".tradingview.com",
    "cdn.discordapp.com",
    "media.discordapp.net",
    "pbs.twimg.com",
    "i.imgur.com",
)

# Per-host Referer header table. Lookup is by registered-domain suffix
# (``host.endswith(suffix)``). Default is no Referer.
_MEDIA_PROXY_REFERERS: Dict[str, str] = {
    "tradingview.com": "https://www.tradingview.com/",
    "discordapp.com": "https://discord.com/",
    "discordapp.net": "https://discord.com/",
    "twimg.com": "https://twitter.com/",
    "imgur.com": "https://imgur.com/",
}

# Stock browser User-Agent. Some allow-listed CDNs (TradingView in
# particular) gate hot-linked assets on a non-bot UA.
_MEDIA_PROXY_UA: str = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Allowed upstream content types (lower-cased; any ``; charset=…`` is
# stripped before comparison).
_MEDIA_PROXY_ALLOWED_MIME: frozenset = frozenset({
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
})

# Hard cap on raw upstream bytes. 10 MB.
_MEDIA_PROXY_MAX_BYTES: int = 10 * 1024 * 1024

# Streaming chunk size when forwarding to client.
_MEDIA_PROXY_CHUNK_SIZE: int = 64 * 1024

# Total request timeout (seconds).
_MEDIA_PROXY_TIMEOUT_S: float = 15.0

# Hard cap on redirects we are willing to follow. Each hop is
# re-validated against the allow-list and IP check.
_MEDIA_PROXY_MAX_REDIRECTS: int = 3

# Token-bucket rate limit: window length and request cap per session.
# 2026-05-22 — bumped from 30/60s to 240/60s. The Discord feed and the
# drawer can request 30+ thumbnails on a single page load (one per news
# item with a chart). At 30/min the page never finishes painting. 240
# is still well below the upstream rate limits at TradingView / Discord
# CDN / Twitter and gives ~4 page loads per minute headroom. Env-tunable
# via ``MEDIA_PROXY_RATE_LIMIT`` and ``MEDIA_PROXY_RATE_WINDOW_S``.
import os as _os
_RATE_LIMIT_WINDOW_S: float = float(_os.environ.get("MEDIA_PROXY_RATE_WINDOW_S", "60"))
_RATE_LIMIT_MAX_REQUESTS: int = int(_os.environ.get("MEDIA_PROXY_RATE_LIMIT", "240"))

# Per-session FIFO of recent request timestamps. Module-level state;
# wiped on process restart.
_RATE_LIMIT_BUCKETS: Dict[str, Deque[float]] = {}
_RATE_LIMIT_LOCK = asyncio.Lock()


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


class _ProxyError(Exception):
    """Raised by validation helpers with an HTTP status code attached.

    Args:
        status: HTTP status code to return to the caller.
        message: Short human-readable error message.
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _host_in_allowlist(host: str) -> bool:
    """Return True iff ``host`` matches the allow-list.

    Wildcard entries (those starting with ``.``) match on a strict
    dot boundary so ``s3.tradingview.com.evil.example`` is rejected.

    Args:
        host: Lower-cased hostname.

    Returns:
        True when allow-listed.
    """
    if not host:
        return False
    for entry in _MEDIA_PROXY_ALLOWED_HOSTS:
        if entry.startswith("."):
            if host.endswith(entry) or host == entry[1:]:
                return True
        elif host == entry:
            return True
    return False


async def _resolve_and_check_ips(host: str) -> None:
    """Resolve ``host`` asynchronously and reject if any IP is non-public.

    Uses :meth:`asyncio.AbstractEventLoop.getaddrinfo` so DNS lookups
    do not block the event loop (the synchronous
    :func:`socket.getaddrinfo` would stall every other request on the
    dashboard while the resolver waits).

    This is the secondary SSRF defence after the host allow-list. It
    does NOT defend against DNS-rebind on an allow-listed domain;
    see plan §6.1.

    Args:
        host: Lower-cased hostname.

    Raises:
        _ProxyError: If DNS resolution fails or any resolved IP is
            private / loopback / link-local / reserved.
    """
    loop = asyncio.get_running_loop()
    try:
        addr_infos = await loop.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise _ProxyError(502, f"dns resolution failed: {exc}") from exc
    except OSError as exc:
        raise _ProxyError(502, f"dns resolution failed: {exc}") from exc
    seen = set()
    for info in addr_infos:
        # Each entry is (family, type, proto, canonname, sockaddr).
        sockaddr = info[4]
        ip_text = sockaddr[0]
        if ip_text in seen:
            continue
        seen.add(ip_text)
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError:
            raise _ProxyError(502, f"invalid resolved IP: {ip_text}")
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise _ProxyError(
                502, f"resolved IP {ip_text} is not public"
            )


async def _validate_url(raw: Optional[str]) -> str:
    """Validate the ``?url=`` query parameter.

    Performs the host allow-list check (primary) and the post-DNS IP
    check (secondary). Returns the validated URL or raises
    :class:`_ProxyError`.

    The DNS resolution step is async so the event loop is not
    blocked while waiting for the resolver.

    Args:
        raw: The ``?url=`` value from the request, or ``None``.

    Returns:
        The validated URL string (unchanged from input).

    Raises:
        _ProxyError: On any validation failure.
    """
    if raw is None or not raw.strip():
        raise _ProxyError(400, "url query parameter is required")
    candidate = raw.strip()
    parsed = urlparse(candidate)
    if parsed.scheme != "https":
        raise _ProxyError(400, "only https:// urls are accepted")
    if not parsed.netloc:
        raise _ProxyError(400, "url is missing a host")
    host = (parsed.hostname or "").lower()
    if not host:
        raise _ProxyError(400, "url is missing a host")
    if not _host_in_allowlist(host):
        raise _ProxyError(403, f"host not allow-listed: {host}")
    await _resolve_and_check_ips(host)
    return candidate


def _referer_for_host(host: str) -> Optional[str]:
    """Return the per-host Referer header value, or ``None``.

    Args:
        host: Lower-cased hostname.

    Returns:
        Referer URL when the host's registered-domain suffix is in
        :data:`_MEDIA_PROXY_REFERERS`; otherwise ``None``.
    """
    if not host:
        return None
    for suffix, referer in _MEDIA_PROXY_REFERERS.items():
        if host == suffix or host.endswith("." + suffix):
            return referer
    return None


def _normalise_content_type(raw: Optional[str]) -> str:
    """Strip parameters and lower-case an HTTP ``Content-Type`` header.

    Args:
        raw: Raw header value, e.g. ``"image/png; charset=binary"``.

    Returns:
        Bare lower-cased media type, e.g. ``"image/png"``. Returns the
        empty string when ``raw`` is falsy.
    """
    if not raw:
        return ""
    return raw.split(";", 1)[0].strip().lower()


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def _rate_limit_key(request: web.Request) -> str:
    """Return a stable identifier for the rate-limit bucket.

    Prefers the dashboard session cookie so a single browser session
    is one bucket regardless of changing client IPs (Wi-Fi → LTE).
    Falls back to the peer IP otherwise. Both values come from
    aiohttp directly; this function never trusts proxy headers
    blindly.

    Args:
        request: The aiohttp request object.

    Returns:
        Bucket key string.
    """
    cookie = request.cookies.get("__Host-session")
    if cookie:
        return f"sess:{cookie[:32]}"
    peer = (
        request.transport.get_extra_info("peername")
        if request.transport
        else None
    )
    return f"ip:{peer[0]}" if peer else "ip:unknown"


async def _check_rate_limit(key: str) -> Optional[int]:
    """Apply the token-bucket policy. Return retry-after on rejection.

    Args:
        key: Bucket key from :func:`_rate_limit_key`.

    Returns:
        ``None`` when the request is allowed. When rejected, returns
        the integer number of seconds the caller should wait before
        retrying.
    """
    now = time.monotonic()
    cutoff = now - _RATE_LIMIT_WINDOW_S
    async with _RATE_LIMIT_LOCK:
        bucket = _RATE_LIMIT_BUCKETS.get(key)
        if bucket is None:
            bucket = deque()
            _RATE_LIMIT_BUCKETS[key] = bucket
        # Discard timestamps outside the window.
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= _RATE_LIMIT_MAX_REQUESTS:
            oldest = bucket[0]
            retry_after = max(1, int(oldest + _RATE_LIMIT_WINDOW_S - now) + 1)
            return retry_after
        bucket.append(now)
        # Opportunistic eviction: prune any other bucket that is now empty
        # so the dict does not grow O(n_unique_clients) over the lifetime
        # of the long-running daemon. Bounded to a few keys per call to
        # keep this fast under the lock.
        _evict_empty_buckets(cutoff, max_evictions=8, exclude=key)
        return None


def _evict_empty_buckets(
    cutoff: float, *, max_evictions: int, exclude: str,
) -> None:
    """Prune empty rate-limit buckets so the dict cannot grow forever.

    Args:
        cutoff: Monotonic-time cutoff; entries older than this are stale.
        max_evictions: Hard cap on keys removed in one call to keep work
            bounded under the rate-limit lock.
        exclude: Key that must not be evicted (the caller's own bucket
            which was just appended to).
    """
    removed = 0
    for k, dq in list(_RATE_LIMIT_BUCKETS.items()):
        if removed >= max_evictions:
            return
        if k == exclude:
            continue
        # Trim stale entries off the front so empty buckets are detectable.
        while dq and dq[0] < cutoff:
            dq.popleft()
        if not dq:
            _RATE_LIMIT_BUCKETS.pop(k, None)
            removed += 1


# ---------------------------------------------------------------------------
# Upstream fetch helpers
# ---------------------------------------------------------------------------


def _build_upstream_headers(host: str) -> Dict[str, str]:
    """Construct the headers we send upstream.

    Args:
        host: Lower-cased hostname of the upstream URL.

    Returns:
        Mapping suitable for ``aiohttp.ClientSession.get(headers=…)``.
    """
    headers: Dict[str, str] = {
        "User-Agent": _MEDIA_PROXY_UA,
        "Accept": "image/png,image/jpeg,image/webp,image/gif,image/*;q=0.8",
    }
    referer = _referer_for_host(host)
    if referer:
        headers["Referer"] = referer
    return headers


async def _follow_with_validation(
    session: aiohttp.ClientSession,
    url: str,
) -> aiohttp.ClientResponse:
    """Follow up to :data:`_MEDIA_PROXY_MAX_REDIRECTS` redirects manually.

    Each redirect target is re-validated against the host allow-list
    AND the post-resolution IP check; an attacker who controls an
    allow-listed CDN cannot redirect us to an internal IP.

    Args:
        session: Open aiohttp session (``auto_decompress=False``).
        url: The validated entry-point URL.

    Returns:
        Open :class:`aiohttp.ClientResponse` for the final hop. The
        caller MUST close it (use ``async with`` at the call site).

    Raises:
        _ProxyError: On any validation failure or redirect-limit
            breach.
        aiohttp.ClientError: Re-raised on transport failure.
    """
    current = url
    for hop in range(_MEDIA_PROXY_MAX_REDIRECTS + 1):
        parsed = urlparse(current)
        host = (parsed.hostname or "").lower()
        headers = _build_upstream_headers(host)
        resp = await session.get(
            current, headers=headers, allow_redirects=False
        )
        if resp.status in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            resp.release()
            if not location:
                raise _ProxyError(502, "redirect missing Location header")
            # Resolve relative redirects against the current URL.
            from urllib.parse import urljoin
            next_url = urljoin(current, location)
            try:
                await _validate_url(next_url)
            except _ProxyError as exc:
                raise _ProxyError(
                    502,
                    f"redirect target rejected: {exc.message}",
                ) from exc
            current = next_url
            if hop >= _MEDIA_PROXY_MAX_REDIRECTS:
                raise _ProxyError(502, "too many redirects")
            continue
        return resp
    raise _ProxyError(502, "too many redirects")


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


async def handle_media_proxy(request: web.Request) -> web.StreamResponse:
    """``GET /api/media/proxy?url=<external https url>`` — see module docstring.

    Args:
        request: The aiohttp request.

    Returns:
        A streamed image response on success, or a JSON error
        response on validation / upstream failure.
    """
    # Validate URL first so a missing param fails before we touch the
    # rate limiter (avoids polluting the bucket on obviously bad calls).
    try:
        url = await _validate_url(request.query.get("url"))
    except _ProxyError as exc:
        return web.json_response(
            {"ok": False, "error": exc.message}, status=exc.status
        )

    rl_key = _rate_limit_key(request)
    retry_after = await _check_rate_limit(rl_key)
    if retry_after is not None:
        return web.json_response(
            {"ok": False, "error": "rate limit exceeded"},
            status=429,
            headers={"Retry-After": str(retry_after)},
        )

    try:
        return await _stream_upstream(request, url)
    except _ProxyError as exc:
        return web.json_response(
            {"ok": False, "error": exc.message}, status=exc.status
        )
    except asyncio.TimeoutError:
        logger.warning("media_proxy: timeout fetching %s", url)
        return web.json_response(
            {"ok": False, "error": "upstream timeout"}, status=504
        )
    except aiohttp.ClientError as exc:
        logger.warning("media_proxy: client error for %s: %s", url, exc)
        return web.json_response(
            {"ok": False, "error": f"upstream client error: {exc}"},
            status=502,
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("media_proxy: unexpected error for %s: %s", url, exc)
        return web.json_response(
            {"ok": False, "error": "internal proxy error"}, status=500
        )


async def _stream_upstream(
    request: web.Request, url: str
) -> web.StreamResponse:
    """Fetch ``url`` and stream the body back to the caller.

    Caps raw bytes at :data:`_MEDIA_PROXY_MAX_BYTES`, validates the
    upstream ``Content-Type``, forwards ``Content-Encoding`` so the
    browser decompresses normally.

    Args:
        request: Inbound aiohttp request (used for streaming back).
        url: Validated upstream URL.

    Returns:
        A streamed response that has been fully written.

    Raises:
        _ProxyError: On any upstream protocol violation.
    """
    timeout = aiohttp.ClientTimeout(total=_MEDIA_PROXY_TIMEOUT_S)
    async with aiohttp.ClientSession(
        timeout=timeout, auto_decompress=False
    ) as session:
        upstream = await _follow_with_validation(session, url)
        try:
            if upstream.status != 200:
                logger.warning(
                    "media_proxy: upstream %s returned %d for %s",
                    urlparse(url).hostname,
                    upstream.status,
                    url,
                )
                raise _ProxyError(
                    502, f"upstream returned {upstream.status}"
                )
            content_type = _normalise_content_type(
                upstream.headers.get("Content-Type")
            )
            if content_type not in _MEDIA_PROXY_ALLOWED_MIME:
                raise _ProxyError(
                    415, f"unsupported content-type: {content_type or 'none'}"
                )
            content_length = upstream.headers.get("Content-Length")
            if content_length is not None:
                try:
                    if int(content_length) > _MEDIA_PROXY_MAX_BYTES:
                        raise _ProxyError(502, "upstream body too large")
                except ValueError:
                    pass

            response = web.StreamResponse(
                status=200,
                headers=_build_response_headers(
                    content_type,
                    upstream.headers.get("Content-Encoding"),
                ),
            )
            await response.prepare(request)

            total = 0
            cap_exceeded = False
            async for chunk in upstream.content.iter_chunked(
                _MEDIA_PROXY_CHUNK_SIZE
            ):
                total += len(chunk)
                if total > _MEDIA_PROXY_MAX_BYTES:
                    logger.warning(
                        "media_proxy: cap exceeded (%d bytes) for %s",
                        total,
                        url,
                    )
                    cap_exceeded = True
                    break
                await response.write(chunk)
            if cap_exceeded:
                # Headers are already prepared, so we cannot change the
                # status to 502. Force-close the underlying transport
                # WITHOUT calling write_eof() — the browser then surfaces
                # ERR_CONTENT_LENGTH_MISMATCH / connection-aborted instead
                # of rendering a silently-truncated image.
                if response.transport is not None:
                    try:
                        response.transport.close()
                    except Exception as exc:  # pragma: no cover - defensive
                        logger.debug(
                            "media_proxy: transport.close() failed: %s", exc,
                        )
                return response
            await response.write_eof()
            return response
        finally:
            upstream.release()


def _build_response_headers(
    content_type: str, content_encoding: Optional[str]
) -> Dict[str, str]:
    """Construct the headers we send back to the browser.

    Args:
        content_type: Validated upstream media type.
        content_encoding: Upstream ``Content-Encoding`` header value
            or ``None``.

    Returns:
        Mapping suitable for :class:`aiohttp.web.StreamResponse`.
    """
    headers: Dict[str, str] = {
        "Content-Type": content_type,
        "Cache-Control": "public, max-age=86400",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }
    if content_encoding:
        headers["Content-Encoding"] = content_encoding
    return headers


def attach_routes(app: web.Application, *, prefix: str = "") -> None:
    """Register the proxy endpoint on ``app`` under ``prefix``.

    Args:
        app: aiohttp application.
        prefix: URL prefix (no trailing slash).
    """
    app.router.add_get(prefix + "/api/media/proxy", handle_media_proxy)


__all__ = [
    "attach_routes",
    "handle_media_proxy",
]
