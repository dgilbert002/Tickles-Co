"""Module: test_tui_readonly
Purpose: Tests for TUI read-only gate on mutating menu handlers.
Location: /opt/tickles/shared/tests/test_tui_readonly.py
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

from shared.catalogue.tui_manager import _readonly_guard, _TUI_READONLY


# ---------------------------------------------------------------------------
# _readonly_guard unit tests
# ---------------------------------------------------------------------------

def test_readonly_guard_returns_false_when_readonly_enabled():
    """When TICKLES_TUI_READONLY=1, _readonly_guard returns False and prints warning."""
    with patch("shared.catalogue.tui_manager._TUI_READONLY", True):
        with patch("shared.catalogue.tui_manager.console.print") as mock_print:
            result = _readonly_guard("test action")
            assert result is False
            mock_print.assert_called_once()
            call_text = str(mock_print.call_args)
            assert "Read-only mode" in call_text


def test_readonly_guard_returns_true_when_readonly_disabled():
    """When TICKLES_TUI_READONLY=0, _readonly_guard returns True."""
    with patch("shared.catalogue.tui_manager._TUI_READONLY", False):
        with patch("shared.catalogue.tui_manager.console.print") as mock_print:
            result = _readonly_guard("test action")
            assert result is True
            mock_print.assert_not_called()


# ---------------------------------------------------------------------------
# Environment variable parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("1", True),
    ("true", True),
    ("True", True),
    ("TRUE", True),
    ("yes", True),
    ("on", True),
    ("0", False),
    ("false", False),
    ("False", False),
    ("FALSE", False),
    ("no", False),
    ("off", False),
    ("", False),
])
def test_tui_readonly_env_parsing(value, expected):
    """TICKLES_TUI_READONLY env var is parsed correctly for various values."""
    with patch.dict(os.environ, {"TICKLES_TUI_READONLY": value}, clear=False):
        # Re-import to pick up new env value
        import importlib
        from shared.catalogue import tui_manager
        importlib.reload(tui_manager)
        assert tui_manager._TUI_READONLY == expected


# ---------------------------------------------------------------------------
# Integration: view_sources respects readonly
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_view_sources_toggle_blocked_in_readonly():
    """In read-only mode, source toggle prompts are blocked."""
    import importlib
    from shared.catalogue import tui_manager
    importlib.reload(tui_manager)

    mock_sources = [
        {"id": 1, "source_type": "discord", "source_name": "Test", "source_slug": "test", "is_enabled": True, "description": ""},
    ]

    with patch("shared.catalogue.tui_manager._TUI_READONLY", True):
        with patch("shared.catalogue.db.list_sources", new=AsyncMock(return_value=mock_sources)):
            with patch("shared.catalogue.tui_manager.Prompt.ask", return_value="1") as mock_prompt:
                with patch("shared.catalogue.tui_manager.console.print") as mock_print:
                    await tui_manager.view_sources()
                    # The prompt should still be shown, but toggle should be blocked
                    mock_prompt.assert_called_once()
                    # Check that readonly warning was printed
                    call_texts = [str(c) for c in mock_print.call_args_list]
                    assert any("Read-only mode" in t for t in call_texts)


@pytest.mark.anyio
async def test_view_sources_toggle_allowed_when_not_readonly():
    """When not read-only, source toggle proceeds normally."""
    import importlib
    from shared.catalogue import tui_manager
    importlib.reload(tui_manager)

    mock_sources = [
        {"id": 1, "source_type": "discord", "source_name": "Test", "source_slug": "test", "is_enabled": True, "description": ""},
    ]

    with patch("shared.catalogue.tui_manager._TUI_READONLY", False):
        with patch("shared.catalogue.db.list_sources", new=AsyncMock(return_value=mock_sources)):
            with patch("shared.catalogue.tui_manager.Prompt.ask", return_value="1") as mock_prompt:
                with patch("shared.catalogue.db.toggle_source", new=AsyncMock(return_value=1)) as mock_toggle:
                    with patch("shared.catalogue.tui_manager.console.print"):
                        await tui_manager.view_sources()
                        mock_prompt.assert_called_once()
                        mock_toggle.assert_called_once_with(1, False)


# ---------------------------------------------------------------------------
# Integration: view_channels respects readonly
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_view_channels_toggle_blocked_in_readonly():
    """In read-only mode, channel toggle is blocked."""
    import importlib
    from shared.catalogue import tui_manager
    importlib.reload(tui_manager)

    mock_channels = [
        {"id": 1, "catalog_id": 1, "channel_name": "TestCh", "channel_slug": "testch",
         "is_enabled": True, "collect_text": True, "collect_images": True,
         "collect_charts": True, "collect_videos": False, "poll_interval_seconds": 60},
    ]

    with patch("shared.catalogue.tui_manager._TUI_READONLY", True):
        with patch("shared.catalogue.db.list_channels", new=AsyncMock(return_value=mock_channels)):
            with patch("shared.catalogue.tui_manager.Prompt.ask", return_value="1"):
                with patch("shared.catalogue.tui_manager.console.print") as mock_print:
                    await tui_manager.view_channels()
                    call_texts = [str(c) for c in mock_print.call_args_list]
                    assert any("Read-only mode" in t for t in call_texts)


# ---------------------------------------------------------------------------
# Integration: view_users respects readonly
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_view_users_toggle_blocked_in_readonly():
    """In read-only mode, user toggle is blocked."""
    import importlib
    from shared.catalogue import tui_manager
    importlib.reload(tui_manager)

    mock_users = [
        {"id": 1, "channel_id": 1, "platform_handle": "trader1", "display_name": "Trader One",
         "is_enabled": True, "track_trades": True, "track_charts": True,
         "track_commentary": True, "track_advice": True},
    ]

    with patch("shared.catalogue.tui_manager._TUI_READONLY", True):
        with patch("shared.catalogue.db.list_users", new=AsyncMock(return_value=mock_users)):
            with patch("shared.catalogue.tui_manager.Prompt.ask", return_value="1"):
                with patch("shared.catalogue.tui_manager.console.print") as mock_print:
                    await tui_manager.view_users()
                    call_texts = [str(c) for c in mock_print.call_args_list]
                    assert any("Read-only mode" in t for t in call_texts)
