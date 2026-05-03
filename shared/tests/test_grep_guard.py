"""
Module: test_grep_guard
Purpose: CI grep gate — verify no direct os.getenv(OPENROUTER|REQUESTY)_API outside gateway_config.py.
Location: /opt/tickles/shared/tests/test_grep_guard.py
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestGrepGuard:
    """CI-style grep gates for direct API key access outside gateway_config."""

    def test_no_direct_openrouter_api_getenv(self) -> None:
        """[R] No os.getenv('OPENROUTER_API_KEY') outside gateway_config.py."""
        # Build search directories that actually exist
        search_dirs = [
            str(REPO_ROOT / "shared" / "intelligence"),
            str(REPO_ROOT / "shared" / "daemons"),
        ]
        openclaw_dir = REPO_ROOT / "shared" / "openclaw"
        if openclaw_dir.exists():
            search_dirs.append(str(openclaw_dir))

        # Try ripgrep first, fall back to grep -r
        rg_cmd = [
            "rg",
            r'os\.getenv\("OPENROUTER_API',
            *search_dirs,
            "-g", "!gateway_config.py",
            "-g", "!test_grep_guard.py",
        ]
        grep_cmd = [
            "grep", "-r",
            r'os\.getenv("OPENROUTER_API',
            *search_dirs,
        ]
        try:
            result = subprocess.run(rg_cmd, capture_output=True, text=True)
        except FileNotFoundError:
            result = subprocess.run(grep_cmd, capture_output=True, text=True)
        # Exit code 1 when no matches found — that is the PASS state
        assert result.returncode == 1, (
            f"Direct OPENROUTER_API_KEY access found outside gateway_config.py:\n"
            f"{result.stdout}"
        )

    def test_no_direct_requesty_api_getenv(self) -> None:
        """[R] No os.getenv('REQUESTY_API') outside gateway_config.py."""
        search_dirs = [
            str(REPO_ROOT / "shared" / "intelligence"),
            str(REPO_ROOT / "shared" / "daemons"),
        ]
        openclaw_dir = REPO_ROOT / "shared" / "openclaw"
        if openclaw_dir.exists():
            search_dirs.append(str(openclaw_dir))

        rg_cmd = [
            "rg",
            r'os\.getenv\("REQUESTY_API',
            *search_dirs,
            "-g", "!gateway_config.py",
            "-g", "!test_grep_guard.py",
        ]
        grep_cmd = [
            "grep", "-r",
            r'os\.getenv("REQUESTY_API',
            *search_dirs,
        ]
        try:
            result = subprocess.run(rg_cmd, capture_output=True, text=True)
        except FileNotFoundError:
            result = subprocess.run(grep_cmd, capture_output=True, text=True)
        assert result.returncode == 1, (
            f"Direct REQUESTY_API access found outside gateway_config.py:\n"
            f"{result.stdout}"
        )

    def test_gateway_config_has_alias_chain(self) -> None:
        """gateway_config.py must contain the REQUESTY_API_KEY alias fallback."""
        path = REPO_ROOT / "shared" / "intelligence" / "gateway_config.py"
        assert path.exists()
        text = path.read_text()
        assert "REQUESTY_API_KEY" in text, "gateway_config.py must reference REQUESTY_API_KEY alias"
        assert "REQUESTY_API" in text, "gateway_config.py must reference REQUESTY_API canonical name"
