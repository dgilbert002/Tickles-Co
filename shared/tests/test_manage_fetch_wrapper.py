"""
Module: test_manage_fetch_wrapper
Purpose: Smoke test verifying manage.js exports the hardened tickFetch wrapper and helpers.
Location: /opt/tickles/shared/tests/test_manage_fetch_wrapper.py
"""

import re
from pathlib import Path

import pytest

_JS_PATH = Path(__file__).resolve().parent.parent / "intelligence" / "manage_panel" / "static" / "manage.js"


def _read_js() -> str:
    """Read the manage.js source."""
    if not _JS_PATH.exists():
        pytest.skip(f"manage.js not found at {_JS_PATH}")
    return _JS_PATH.read_text(encoding="utf-8")


def test_manage_js_exists() -> None:
    """The manage.js static asset must exist."""
    assert _JS_PATH.exists(), f"Expected manage.js at {_JS_PATH}"


def test_tickfetch_function_present() -> None:
    """manage.js must define the tickFetch async function."""
    src = _read_js()
    assert "async function tickFetch" in src or "async function tickFetch(url" in src


def test_csrf_cookie_constant_present() -> None:
    """manage.js must reference the __Host-csrf cookie name."""
    src = _read_js()
    assert "__Host-csrf" in src


def test_csrf_header_constant_present() -> None:
    """manage.js must reference the X-CSRF-Token header name."""
    src = _read_js()
    assert "X-CSRF-Token" in src


def test_error_handlers_present() -> None:
    """manage.js must handle 401, 403, 429, and generic errors."""
    src = _read_js()
    assert "resp.status === 401" in src
    assert "resp.status === 403" in src
    assert "resp.status === 429" in src
    assert "showError" in src


def test_mutation_helpers_present() -> None:
    """manage.js must expose enable/disable helpers for sources, channels, and users."""
    src = _read_js()
    assert "window.disableSource" in src
    assert "window.enableSource" in src
    assert "window.disableChannel" in src
    assert "window.enableChannel" in src
    assert "window.disableUser" in src
    assert "window.enableUser" in src


def test_tickfetch_injects_csrf_on_post() -> None:
    """tickFetch must read the CSRF cookie and inject it as a header on non-GET methods."""
    src = _read_js()
    assert "getCsrfToken()" in src
    assert "opts.headers[CSRF_HEADER] = csrf" in src


def test_tickfetch_sets_credentials_include() -> None:
    """tickFetch must send cookies with every request."""
    src = _read_js()
    assert "opts.credentials = 'include'" in src


def test_no_console_log_for_errors() -> None:
    """Error paths must use showError, not bare console.log."""
    src = _read_js()
    # Allow console.error in catch blocks (used for debugging), but not console.log
    assert "console.log" not in src
