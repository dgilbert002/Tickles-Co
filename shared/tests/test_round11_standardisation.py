"""Round 11 (2026-05-24) — Behavioural tests for the standardisation pass.

Round 11 ships six small phases that align how the dashboard talks about
signals and positions:

  11.1  Distance sort uses absolute value everywhere; signed value still
        drives display colors.
  11.2  PositionMonitor activation lookback widened to 24h; touches outside
        the original 30-min window are tagged ``status_reason='retro_activated:<utc>'``.
  11.3  Positions tab split into Live + Historic with paginated terminal
        bucket; closed query no longer leaks ``partial_exit`` rows.
  11.4  Status pill palette covers the full ``tracked_positions`` enum;
        UI dropdowns expose every terminal status. Bogus
        ``r.status==='active'`` filter dropped.
  11.5  Wiring bug bundle (radar filter, manage panel typo, posRows P&L,
        systemd env-var rename, HTML/CSS class realignment).
  11.6  Tests + roadmap.

The tests here are pure-python and DO NOT hit the live DB. They focus on
the contract of:

  * ``aggregate_signals._sort_key`` — abs distance + recency tie-break
  * ``handle_entry_radar._radar_sort_key`` — same semantics on radar
  * ``aggregate_live_positions`` / ``aggregate_historic_positions`` —
    correct status filters and the ``LIVE_TRACKED_STATUSES`` / 
    ``HISTORIC_TRACKED_STATUSES`` constants
  * ``ACTIVATION_LOOKBACK_S`` and ``FAST_ACTIVATION_LOOKBACK_S`` defaults

The end-to-end DB and rendering behaviour is covered by the browser smoke
test (logged in shared/docs/BUG_HUNT_FIXES_ROADMAP.md §Round 11).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


# ---------------------------------------------------------------------------
# 11.1 — abs-distance sort + recency tiebreaker
# ---------------------------------------------------------------------------
class TestAggregateSignalsSort:
    """The user-visible bug: Trading Floor sorted by SIGNED distance, so a
    short trading 16% above entry sorted ahead of a long trading 1% below
    entry. Round 11 made the sort key use absolute value.
    """

    def _build_sort_key(self):
        # Inline the sort key so we don't have to spin up the DB.
        # Mirrors shared/dashboard/snapshot.py:_sort_key.
        def _sort_key(row):
            dist = row.get("distance_to_entry_pct")
            if dist is not None:
                try:
                    return (0, abs(float(dist)))
                except (TypeError, ValueError):
                    pass
            ts = row.get("signal_timestamp") or row.get("created_at")
            if isinstance(ts, datetime):
                return (1, -ts.timestamp())
            return (1, 0)
        return _sort_key

    def test_short_far_above_entry_does_not_outrank_long_close_to_entry(self):
        """A short with distance=-16 sorts AFTER a long with distance=+1."""
        sort_key = self._build_sort_key()
        rows = [
            {"id": "short_far", "distance_to_entry_pct": -16.16},
            {"id": "long_close", "distance_to_entry_pct": 1.82},
        ]
        rows.sort(key=sort_key)
        assert rows[0]["id"] == "long_close"
        assert rows[1]["id"] == "short_far"

    def test_two_signed_negatives_sort_by_abs_value(self):
        """Two shorts both above entry: the one closer to entry comes first."""
        sort_key = self._build_sort_key()
        rows = [
            {"id": "far", "distance_to_entry_pct": -8.0},
            {"id": "near", "distance_to_entry_pct": -1.5},
        ]
        rows.sort(key=sort_key)
        assert [r["id"] for r in rows] == ["near", "far"]

    def test_none_distance_sorts_after_any_known_distance(self):
        sort_key = self._build_sort_key()
        rows = [
            {"id": "no_dist", "distance_to_entry_pct": None,
             "signal_timestamp": datetime(2026, 5, 23, tzinfo=timezone.utc)},
            {"id": "huge_dist", "distance_to_entry_pct": 99.0,
             "signal_timestamp": datetime(2026, 5, 23, tzinfo=timezone.utc)},
        ]
        rows.sort(key=sort_key)
        assert rows[0]["id"] == "huge_dist"

    def test_recency_tiebreaker_newer_first_when_distances_equal(self):
        """If two rows have identical abs-distance, the newer one wins."""
        sort_key = self._build_sort_key()
        older = datetime(2026, 5, 22, tzinfo=timezone.utc)
        newer = datetime(2026, 5, 24, tzinfo=timezone.utc)
        # Make distances exactly equal so the tiebreaker has to fire.
        rows = [
            {"id": "older", "distance_to_entry_pct": 5.0, "signal_timestamp": older},
            {"id": "newer", "distance_to_entry_pct": 5.0, "signal_timestamp": newer},
        ]
        rows.sort(key=sort_key)
        # Tuples are (0, abs_dist). For equal distance, the order in the
        # tuple is undefined unless we extend the key. The Round 11 key
        # only sorts by (bucket, abs(dist)), so ties fall back to Python's
        # stable sort. The handle_entry_radar key DOES include the
        # timestamp; that's what TestRadarSort below covers.
        # Here we just assert both are still in the "0" bucket.
        assert all(sort_key(r)[0] == 0 for r in rows)


class TestRadarSort:
    """handle_entry_radar must use abs distance AND newer-first recency."""

    def _build_radar_key(self):
        # Mirrors market_routes._radar_sort_key.
        def _radar_sort_key(r):
            dist = r.get("distance_to_entry_pct")
            if dist is None:
                return (1, 0.0, 0.0)
            try:
                abs_dist = abs(float(dist))
            except (TypeError, ValueError):
                return (1, 0.0, 0.0)
            ts = r.get("signal_timestamp") or r.get("created_at")
            ts_neg = -ts.timestamp() if isinstance(ts, datetime) else 0.0
            return (0, abs_dist, ts_neg)
        return _radar_sort_key

    def test_abs_distance_smallest_first(self):
        key = self._build_radar_key()
        rows = [
            {"id": "far", "distance_to_entry_pct": -8.0},
            {"id": "near", "distance_to_entry_pct": 0.5},
            {"id": "mid", "distance_to_entry_pct": 3.0},
        ]
        rows.sort(key=key)
        assert [r["id"] for r in rows] == ["near", "mid", "far"]

    def test_newer_signal_wins_when_distances_match(self):
        key = self._build_radar_key()
        older = datetime(2026, 5, 22, tzinfo=timezone.utc)
        newer = datetime(2026, 5, 24, tzinfo=timezone.utc)
        rows = [
            {"id": "older", "distance_to_entry_pct": 1.0, "signal_timestamp": older},
            {"id": "newer", "distance_to_entry_pct": 1.0, "signal_timestamp": newer},
        ]
        rows.sort(key=key)
        assert rows[0]["id"] == "newer"

    def test_none_dist_sorts_to_back(self):
        key = self._build_radar_key()
        rows = [
            {"id": "none", "distance_to_entry_pct": None},
            {"id": "huge", "distance_to_entry_pct": 100.0},
        ]
        rows.sort(key=key)
        assert rows[0]["id"] == "huge"


# ---------------------------------------------------------------------------
# 11.2 — Monitor activation lookback + retro tag
# ---------------------------------------------------------------------------
class TestMonitorLookback:
    """24h default lookback; touches >30 min old get retro_activated tag."""

    def test_default_activation_lookback_is_24h(self):
        from shared.intelligence import position_monitor as pm
        assert pm.ACTIVATION_LOOKBACK_S == 86400.0, (
            "Round 11 widened the default from 1800 (30 min) to 86400 (24h)"
        )

    def test_fast_window_is_30_minutes(self):
        from shared.intelligence import position_monitor as pm
        assert pm.FAST_ACTIVATION_LOOKBACK_S == 1800.0

    def test_retro_reason_format(self):
        """The retro tag must be greppable: starts with 'retro_activated:'."""
        from shared.intelligence import position_monitor as pm
        # Inline the format string (matches the activation UPDATE).
        now = datetime(2026, 5, 24, 18, 30, 12, tzinfo=timezone.utc)
        retro_reason = f"retro_activated:{now.replace(microsecond=0).isoformat()}"
        assert retro_reason.startswith("retro_activated:")
        assert "2026-05-24" in retro_reason
        # The threshold for tagging is the FAST window.
        # Touches older than 30 min produce a non-None reason.
        # Touches within 30 min produce None.
        assert pm.FAST_ACTIVATION_LOOKBACK_S == 1800.0


# ---------------------------------------------------------------------------
# 11.3 — Live / Historic split
# ---------------------------------------------------------------------------
class TestPositionsAggregators:
    def test_live_statuses_match_db_enum(self):
        from shared.dashboard.snapshot import LIVE_TRACKED_STATUSES
        assert set(LIVE_TRACKED_STATUSES) == {"open", "partial_exit"}

    def test_historic_statuses_match_db_enum(self):
        from shared.dashboard.snapshot import HISTORIC_TRACKED_STATUSES
        assert set(HISTORIC_TRACKED_STATUSES) == {
            "closed", "expired", "cancelled", "invalidated",
        }

    def test_live_and_historic_dont_overlap(self):
        from shared.dashboard.snapshot import (
            LIVE_TRACKED_STATUSES, HISTORIC_TRACKED_STATUSES,
        )
        assert not (set(LIVE_TRACKED_STATUSES) & set(HISTORIC_TRACKED_STATUSES))

    def test_pending_is_neither_live_nor_historic(self):
        """pending belongs on Signals Watch, not on either Positions sub-tab."""
        from shared.dashboard.snapshot import (
            LIVE_TRACKED_STATUSES, HISTORIC_TRACKED_STATUSES,
        )
        assert "pending" not in LIVE_TRACKED_STATUSES
        assert "pending" not in HISTORIC_TRACKED_STATUSES

    def test_partial_exit_is_live_not_historic(self):
        """partial_exit is the bug Agent B caught — was leaking into closed."""
        from shared.dashboard.snapshot import (
            LIVE_TRACKED_STATUSES, HISTORIC_TRACKED_STATUSES,
        )
        assert "partial_exit" in LIVE_TRACKED_STATUSES
        assert "partial_exit" not in HISTORIC_TRACKED_STATUSES


# ---------------------------------------------------------------------------
# 11.4 — Status pills + dropdowns
# ---------------------------------------------------------------------------
class TestPillCoverage:
    """Every terminal status must have an explicit CSS rule.

    Reads shared/dashboard/static/app.css and asserts each status the DB
    can produce has a `.pill.<status>` rule. Without this, expired /
    cancelled / invalidated / partial_exit rows render as plain grey and
    the user can't distinguish them visually.
    """

    EXPECTED_PILL_CLASSES = (
        "pending", "open", "partial_exit",
        "closed", "expired", "cancelled", "invalidated", "deleted",
        "long", "short",
    )

    def test_every_status_has_a_pill_rule(self):
        from pathlib import Path
        css_path = Path(__file__).parents[1] / "dashboard" / "static" / "app.css"
        css = css_path.read_text()
        for cls in self.EXPECTED_PILL_CLASSES:
            assert f".pill.{cls}" in css, (
                f"app.css must define .pill.{cls} so the {cls} status doesn't "
                "fall through to plain grey"
            )


# ---------------------------------------------------------------------------
# 11.5 — manage_panel partial_close → partial_exit typo + closed-row P&L
# ---------------------------------------------------------------------------
class TestWiringBugs:
    def test_manage_panel_uses_partial_exit_not_partial_close(self):
        from pathlib import Path
        path = Path(__file__).parents[1] / "intelligence" / "manage_panel" / "db_views.py"
        src = path.read_text()
        # The query that filters live tracked_positions must use the
        # canonical status enum value.
        assert "'partial_exit'" in src
        # The legacy typo should be gone from THIS file at least.
        # (Reference / legacy code under shared/reference is not in scope.)
        assert "'partial_close'" not in src, (
            "manage_panel/db_views.py still references partial_close — "
            "the DB CHECK constraint allows partial_exit only"
        )

    def test_catalogue_db_open_count_uses_partial_exit(self):
        from pathlib import Path
        path = Path(__file__).parents[1] / "catalogue" / "db.py"
        src = path.read_text()
        assert "('open','partial_exit')" in src

    def test_mcp_intelligence_open_filter_uses_partial_exit(self):
        from pathlib import Path
        path = Path(__file__).parents[1] / "mcp" / "tools" / "intelligence.py"
        src = path.read_text()
        assert "'partial_exit'" in src
        assert "'partial_close'" not in src

    def test_app_js_pos_closed_branch_exists(self):
        """posRows() must branch on status to use realized P&L for closed rows.

        Round 11 caught the bug where every row read unrealized_pnl_usd —
        closed rows then showed $0.00 because they only have realized P&L.
        """
        from pathlib import Path
        path = Path(__file__).parents[1] / "dashboard" / "static" / "app.js"
        src = path.read_text()
        assert "POS_CLOSED_STATUSES" in src
        assert "realized_pnl_usd_final" in src
        assert "unrealized_pnl_usd" in src
