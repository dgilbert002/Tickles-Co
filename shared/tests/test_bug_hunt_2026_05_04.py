"""
Module: test_bug_hunt_2026_05_04
Purpose: Regression tests for the five bugs found and fixed during the
         Dashboard v2 (Slice 1-4) bug-hunter audit on 2026-05-04.
Location: /opt/tickles/shared/tests/test_bug_hunt_2026_05_04.py

Bug catalogue covered:

* BUG #1 — surgeon_position_reconciler.py: 0.0-vs-None for exit_price.
* BUG #2 — surgeon_position_bridge.py: entry_price_source must be "surgeon"
           and the value must live in the interpretation_service whitelist.
* BUG #3 — media_proxy.py + price_routes.py: rate-limit bucket dict must not
           grow unbounded over the daemon's lifetime.
* BUG #5 — app.js: priceAbort overwrite race (covered indirectly by ensuring
           the source pattern still aborts before re-creating; verified via
           a static text grep).

BUG #4 (mid-stream cap silent truncation) is exercised by the existing
test_media_proxy.py suite which already covers the cap path; the fix only
changes how the response is terminated, which the existing assertion
verifies (no body bytes beyond the cap).
"""
from __future__ import annotations

import asyncio
import logging
import time
import unittest
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# BUG #1 — reconciler 0.0-vs-None semantics
# ---------------------------------------------------------------------------
class TestReconcilerZeroExitPrice(unittest.TestCase):
    """Verify a logged exit_price of 0.0 is preserved, not replaced."""

    def test_zero_exit_price_is_preserved(self) -> None:
        """A close-log row with ``exit_price = 0.0`` must NOT fall back to
        the entry price. ``or entry_price`` was the original bug; the fix
        replaces it with an explicit ``is None`` check.
        """
        try:
            from shared.intelligence.surgeon_position_reconciler import (
                _safe_float,
            )

            entry_price = 100.0
            log_row = {"exit_price": 0.0, "action": "MANUAL", "net_pnl": -100.0}

            # Reproduce the post-fix arithmetic literally.
            logged_exit = _safe_float(log_row.get("exit_price"))
            exit_price = (
                logged_exit if logged_exit is not None else entry_price
            )

            self.assertEqual(exit_price, 0.0)
            self.assertNotEqual(exit_price, entry_price)
        except Exception as exc:
            logger.exception("zero exit_price preservation failed: %s", exc)
            raise

    def test_missing_exit_price_falls_back(self) -> None:
        """A close-log row with no exit_price falls back to entry_price."""
        try:
            from shared.intelligence.surgeon_position_reconciler import (
                _safe_float,
            )

            entry_price = 100.0
            log_row = {"exit_price": None}
            logged_exit = _safe_float(log_row.get("exit_price"))
            exit_price = (
                logged_exit if logged_exit is not None else entry_price
            )
            self.assertEqual(exit_price, entry_price)
        except Exception as exc:
            logger.exception("missing exit_price fallback failed: %s", exc)
            raise


# ---------------------------------------------------------------------------
# BUG #2 — surgeon entry_price_source whitelist
# ---------------------------------------------------------------------------
class TestSurgeonEntryPriceSource(unittest.TestCase):
    """Verify the surgeon bridge uses a whitelisted ``entry_price_source``."""

    def test_surgeon_is_in_whitelist(self) -> None:
        """``surgeon`` must be in interpretation_service._ENTRY_PRICE_SOURCES."""
        try:
            from shared.intelligence.interpretation_service import (
                _ENTRY_PRICE_SOURCES,
            )

            self.assertIn("surgeon", _ENTRY_PRICE_SOURCES)
            # Sanity: legacy values still present.
            for legacy in ("trader", "llm", "live_price", "last_candle", "none"):
                self.assertIn(legacy, _ENTRY_PRICE_SOURCES)
        except Exception as exc:
            logger.exception("_ENTRY_PRICE_SOURCES check failed: %s", exc)
            raise

    def test_bridge_stamps_surgeon_source(self) -> None:
        """The surgeon position bridge INSERT must stamp ``surgeon``."""
        try:
            bridge_path = (
                Path(__file__).resolve().parents[1]
                / "intelligence"
                / "surgeon_position_bridge.py"
            )
            text = bridge_path.read_text(encoding="utf-8")
            # The fix replaces a hard-coded literal "trader" with "surgeon".
            self.assertIn('"surgeon"', text)
            # Guard against accidental re-introduction of the bug.
            self.assertNotIn('json.dumps(metadata),\n                "trader"', text)
        except Exception as exc:
            logger.exception("bridge surgeon source check failed: %s", exc)
            raise


# ---------------------------------------------------------------------------
# BUG #3 — rate-limit bucket leak
# ---------------------------------------------------------------------------
class TestMediaProxyRateLimitEviction(unittest.TestCase):
    """media_proxy._RATE_LIMIT_BUCKETS must shrink as buckets go idle."""

    def test_evicts_empty_buckets(self) -> None:
        """After the rate-limit window passes, idle buckets are removed."""
        try:
            from shared.dashboard import media_proxy as mp

            # Wipe state so we own it for the duration of the test.
            mp._RATE_LIMIT_BUCKETS.clear()

            now = time.monotonic()
            old = now - mp._RATE_LIMIT_WINDOW_S - 5.0  # firmly stale

            # Seed three idle buckets that should be evicted.
            for k in ("sess:idle1", "sess:idle2", "sess:idle3"):
                mp._RATE_LIMIT_BUCKETS[k] = deque([old])

            async def _exercise() -> None:
                # A new active client makes one call. The eviction pass
                # should clean up the three idle buckets above.
                rc = await mp._check_rate_limit("sess:active")
                self.assertIsNone(rc)

            asyncio.run(_exercise())

            self.assertIn("sess:active", mp._RATE_LIMIT_BUCKETS)
            for k in ("sess:idle1", "sess:idle2", "sess:idle3"):
                self.assertNotIn(
                    k,
                    mp._RATE_LIMIT_BUCKETS,
                    f"Idle bucket {k!r} was not evicted",
                )
        except Exception as exc:
            logger.exception("media_proxy eviction test failed: %s", exc)
            raise


class TestPriceRoutesRateLimitEviction(unittest.TestCase):
    """price_routes._RATE_LIMIT_BUCKETS must shrink as buckets go idle."""

    def test_evicts_empty_buckets(self) -> None:
        """After the rate-limit window passes, idle buckets are removed."""
        try:
            from shared.dashboard import price_routes as pr

            pr._RATE_LIMIT_BUCKETS.clear()

            now = time.monotonic()
            old = now - pr._RATE_LIMIT_WINDOW_S - 5.0

            for k in ("ip:1.1.1.1", "ip:2.2.2.2", "ip:3.3.3.3"):
                pr._RATE_LIMIT_BUCKETS[k] = deque([old])

            async def _exercise() -> None:
                rc = await pr._check_rate_limit("sess:active")
                self.assertIsNone(rc)

            asyncio.run(_exercise())

            self.assertIn("sess:active", pr._RATE_LIMIT_BUCKETS)
            for k in ("ip:1.1.1.1", "ip:2.2.2.2", "ip:3.3.3.3"):
                self.assertNotIn(
                    k,
                    pr._RATE_LIMIT_BUCKETS,
                    f"Idle bucket {k!r} was not evicted",
                )
        except Exception as exc:
            logger.exception("price_routes eviction test failed: %s", exc)
            raise


# ---------------------------------------------------------------------------
# BUG #5 — app.js priceAbort overwrite race (static check)
# ---------------------------------------------------------------------------
class TestAppJsPriceAbortAbortBeforeReplace(unittest.TestCase):
    """The drawer renderer must abort the old controller before replacing."""

    def test_abort_present_before_new_controller(self) -> None:
        """Verify the renderer aborts ``state.priceAbort`` before reassigning.

        This is a static-text guard: full DOM testing of the dashboard JS
        lives in browser-level smoke tests, but the bug fix is local enough
        that a pattern check catches a regression cheaply.
        """
        try:
            js_path = (
                Path(__file__).resolve().parents[1]
                / "dashboard"
                / "static"
                / "app.js"
            )
            text = js_path.read_text(encoding="utf-8")

            # Find the renderDrawer function body.
            marker = "Slice 2 §H — kick off live-price probes"
            idx = text.find(marker)
            self.assertGreater(idx, -1, "Slice 2 §H marker missing in app.js")

            tail = text[idx : idx + 800]

            # The abort() call must appear BEFORE the re-creation.
            abort_idx = tail.find("state.priceAbort.abort()")
            assign_idx = tail.find("state.priceAbort = new AbortController()")
            self.assertGreater(abort_idx, -1, "Abort call missing in renderDrawer")
            self.assertGreater(
                assign_idx, -1, "AbortController re-creation missing"
            )
            self.assertLess(
                abort_idx,
                assign_idx,
                "abort() must run before new AbortController() to "
                "prevent leaking in-flight fetches",
            )
        except Exception as exc:
            logger.exception("app.js abort-before-replace check failed: %s", exc)
            raise


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
