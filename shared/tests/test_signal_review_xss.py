"""
Module: test_signal_review_xss
Purpose: Dedicated XSS regression suite for signal_review_export.py [AN].
Location: /opt/tickles/shared/tests/test_signal_review_xss.py
"""

from pathlib import Path
from typing import Any, Dict, List

import pytest

from shared.intelligence.signal_review_export import _render_html


class TestXssEscape:
    """Tests that user-controlled strings are escaped in rendered HTML."""

    def _make_row(self, **overrides: Any) -> Dict[str, Any]:
        """Return a default row dict with overrides applied."""
        defaults: Dict[str, Any] = {
            "created_at": "2024-01-01T00:00:00+00:00",
            "platform": "discord",
            "trader_handle": "alice",
            "symbol": "BTCUSDT",
            "llm_direction": "long",
            "llm_confidence": 0.9,
            "quant_direction": "short",
            "quant_confidence": 0.7,
            "consensus_direction": "long",
            "consensus_confidence": 0.85,
            "media_local_path": None,
            "llm_raw_request_path": None,
            "llm_raw_response_path": None,
            "discord_url": None,
            "news_item_id": 1,
            "media_item_id": 2,
        }
        defaults.update(overrides)
        return defaults

    def test_trader_handle_script_tag_escaped(self) -> None:
        """A trader handle containing <script> must be escaped, not executed."""
        rows = [self._make_row(trader_handle="<script>alert(1)</script>")]
        html = _render_html(rows, 24)
        # The escaped form contains < not literal <
        assert "<script>" in html or "<script>alert(1)</script>" not in html
        # Ensure no raw script tag survives
        assert "<script>alert(1)</script>" not in html

    def test_symbol_with_angle_brackets_escaped(self) -> None:
        """Symbols containing angle brackets must be escaped."""
        rows = [self._make_row(symbol="<img src=x onerror=alert(1)>")]
        html = _render_html(rows, 24)
        assert "<img src=x onerror=alert(1)>" not in html
        assert "<img" in html or "<" in html

    def test_discord_url_escaped(self) -> None:
        """Discord URLs with query strings are escaped safely."""
        rows = [self._make_row(discord_url="https://discord.com/channels/1/2/3?foo=<bar>")]
        html = _render_html(rows, 24)
        # Autoescape turns < into < in href attributes
        assert "<bar>" not in html
        assert "<bar>" in html or "<" in html

    def test_llm_direction_escaped(self) -> None:
        """Unexpected direction values are escaped."""
        rows = [self._make_row(llm_direction="<b>evil</b>")]
        html = _render_html(rows, 24)
        assert "<b>evil</b>" not in html
        assert "<b>evil</b>" in html or "evil" in html

    def test_prompt_version_escaped(self) -> None:
        """Prompt version strings are escaped."""
        rows = [self._make_row(prompt_version="<script>evil</script>")]
        html = _render_html(rows, 24)
        assert "<script>evil</script>" not in html

    def test_correlation_id_escaped(self) -> None:
        """Correlation IDs are escaped."""
        rows = [self._make_row(correlation_id="<iframe src='evil'>")]
        html = _render_html(rows, 24)
        assert "<iframe" not in html

    def test_no_safe_filter_on_user_fields(self) -> None:
        """No |safe filter is applied to user-controlled fields."""
        # This is a design-level test: we verify the template source
        # does not contain |safe on any variable interpolation except
        # the documented render_thumb site.
        template_path = Path("shared/intelligence/templates/signal_review.html.jinja2")
        source = template_path.read_text(encoding="utf-8")
        # Find all |safe occurrences
        safe_lines = [line for line in source.splitlines() if "| safe" in line or "|safe" in line]
        # Only permitted site is render_thumb
        for line in safe_lines:
            assert "render_thumb" in line, f"Unexpected |safe on line: {line.strip()}"

    def test_tojson_used_for_llm_io_panel(self) -> None:
        """LLM I/O panel uses tojson filter which auto-escapes."""
        template_path = Path("shared/intelligence/templates/signal_review.html.jinja2")
        source = template_path.read_text(encoding="utf-8")
        assert "tojson" in source
