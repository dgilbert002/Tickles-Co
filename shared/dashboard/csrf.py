"""
Module: csrf
Purpose: CSRF token issuance and validation for the manage panel.
Location: /opt/tickles/shared/dashboard/csrf.py
"""

import logging
import secrets
from typing import Callable, Awaitable

from aiohttp import web

logger = logging.getLogger(__name__)

CSRF_HEADER = "X-CSRF-Token"
CSRF_COOKIE = "__Host-csrf"


def issue_csrf(response: web.Response) -> str:
    """Issue a fresh CSRF token and set it as a cookie.

    Args:
        response: The aiohttp response to attach the cookie to.

    Returns:
        The raw token string (also stored in the cookie).
    """
    token = secrets.token_urlsafe(32)
    response.set_cookie(
        CSRF_COOKIE,
        token,
        secure=True,
        httponly=False,
        samesite="Strict",
        path="/",
        max_age=8 * 3600,
    )
    return token


def csrf_required(handler: Callable[[web.Request], Awaitable[web.StreamResponse]]) -> Callable:
    """Decorator that enforces CSRF token match on mutating HTTP methods.

    GET/HEAD/OPTIONS pass through without check.
    POST/PUT/DELETE require X-CSRF-Token header to match the __Host-csrf cookie.

    Args:
        handler: The aiohttp handler to wrap.

    Returns:
        Wrapped handler with CSRF validation.
    """
    async def wrapped(request: web.Request) -> web.StreamResponse:
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return await handler(request)
        sent = request.headers.get(CSRF_HEADER, "")
        cookie = request.cookies.get(CSRF_COOKIE, "")
        if not sent or not cookie or not secrets.compare_digest(sent, cookie):
            logger.warning("CSRF mismatch: header=%s cookie_present=%s path=%s", bool(sent), bool(cookie), request.path)
            return web.json_response({"error": "csrf_mismatch"}, status=403)
        response = await handler(request)
        # Rotate CSRF token on every successful mutating request
        if request.method in ("POST", "PUT", "DELETE", "PATCH") and hasattr(response, "set_cookie"):
            issue_csrf(response)
        return response
    return wrapped
