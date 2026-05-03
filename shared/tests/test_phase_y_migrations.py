"""
Test: phase_y_migrations
Purpose: Smoke tests for Phase Y.1 SQL migrations — verify the four migration
         files exist, are non-empty, contain the expected DDL, and that their
         ROLLBACK companions cleanly undo what they create.
Location: /opt/tickles/shared/tests/test_phase_y_migrations.py

These tests do NOT execute SQL against a live database; that is covered by
the integration test suite (which provisions a throwaway tickles_<company>).
These run on every commit and are intentionally fast/static.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "intelligence" / "migrations"

SKILL_VIEWS_SQL          = MIGRATIONS_DIR / "2026_05_05_phase_y_skill_views.sql"
SKILL_VIEWS_ROLLBACK     = MIGRATIONS_DIR / "2026_05_05_phase_y_skill_views_ROLLBACK.sql"
RECALL_LOG_SQL           = MIGRATIONS_DIR / "2026_05_05_phase_y_recall_log.sql"
RECALL_LOG_ROLLBACK      = MIGRATIONS_DIR / "2026_05_05_phase_y_recall_log_ROLLBACK.sql"
WEIGHT_REC_SQL           = MIGRATIONS_DIR / "2026_05_05_phase_y_weight_recommendations.sql"
WEIGHT_REC_ROLLBACK      = MIGRATIONS_DIR / "2026_05_05_phase_y_weight_recommendations_ROLLBACK.sql"
FEED_VIEWS_SQL           = MIGRATIONS_DIR / "2026_05_05_phase_y_feed_views.sql"
FEED_VIEWS_ROLLBACK      = MIGRATIONS_DIR / "2026_05_05_phase_y_feed_views_ROLLBACK.sql"

ALL_MIGRATIONS = [
    SKILL_VIEWS_SQL, SKILL_VIEWS_ROLLBACK,
    RECALL_LOG_SQL,  RECALL_LOG_ROLLBACK,
    WEIGHT_REC_SQL,  WEIGHT_REC_ROLLBACK,
    FEED_VIEWS_SQL,  FEED_VIEWS_ROLLBACK,
]


# ---------------------------------------------------------------------------
# Existence + non-empty
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", ALL_MIGRATIONS, ids=lambda p: p.name)
def test_migration_file_exists_and_nonempty(path: Path) -> None:
    assert path.exists(), f"Missing Phase Y.1 migration file: {path}"
    text = path.read_text(encoding="utf-8")
    assert len(text.strip()) > 0, f"Empty migration: {path}"
    # Every Phase Y.1 migration must declare itself as such.
    assert "Phase Y.1" in text, f"Missing 'Phase Y.1' header in {path.name}"


# ---------------------------------------------------------------------------
# 2026_05_05_phase_y_skill_views.sql
# ---------------------------------------------------------------------------
def test_skill_views_creates_compute_function() -> None:
    text = SKILL_VIEWS_SQL.read_text(encoding="utf-8")
    assert re.search(
        r"CREATE OR REPLACE FUNCTION\s+compute_skill_score\s*\(",
        text,
        re.IGNORECASE,
    ), "compute_skill_score function definition not found"
    # Signature must be (TEXT, TEXT, INT) — actor, company, window_days.
    assert "p_actor_id" in text
    assert "p_company_id" in text
    assert "p_window_days" in text


def test_skill_views_creates_three_views() -> None:
    text = SKILL_VIEWS_SQL.read_text(encoding="utf-8")
    for view in ("v_actor_skill_7d", "v_actor_skill_14d", "v_actor_skill_30d"):
        assert re.search(
            rf"CREATE OR REPLACE VIEW\s+{view}\b",
            text,
            re.IGNORECASE,
        ), f"View {view} not created"


def test_skill_views_uses_canonical_weights() -> None:
    text = SKILL_VIEWS_SQL.read_text(encoding="utf-8")
    # The composite line must hold the five canonical weights in canonical order.
    for w in ("0.30", "0.25", "0.20", "0.15", "0.10"):
        assert w in text, f"Canonical weight {w} missing from skill_views SQL"


def test_skill_views_has_minimum_trade_floor() -> None:
    text = SKILL_VIEWS_SQL.read_text(encoding="utf-8")
    # Phase Y.1 §3.2 spec: NULL when n_trades < 5
    assert "v_n_trades < 5" in text, "Minimum-trade floor (n_trades < 5) missing"
    assert "RETURN NULL" in text, "Floor must return NULL"


def test_skill_views_clips_to_unit_interval() -> None:
    text = SKILL_VIEWS_SQL.read_text(encoding="utf-8")
    # Final clamp.
    assert "GREATEST(0" in text and "LEAST(1" in text, \
        "Final score must be clamped to [0, 1]"


def test_skill_views_function_is_stable() -> None:
    text = SKILL_VIEWS_SQL.read_text(encoding="utf-8")
    # PL/pgSQL STABLE volatility marker (read-only, allows planner caching).
    assert re.search(r"\bSTABLE\b", text), "compute_skill_score must be marked STABLE"


def test_skill_views_handles_pattern_confirmed_as_jsonb() -> None:
    text = SKILL_VIEWS_SQL.read_text(encoding="utf-8")
    # pattern_confirmed is JSONB in real schema, not boolean; verify we treat it
    # as such (so the migration won't crash when it ships).
    assert "jsonb_array_length" in text or "jsonb_typeof" in text, \
        "pattern_confirmed must be handled as JSONB, not BOOLEAN"


def test_skill_views_rollback_drops_all_objects() -> None:
    text = SKILL_VIEWS_ROLLBACK.read_text(encoding="utf-8")
    for obj in ("v_actor_skill_7d", "v_actor_skill_14d", "v_actor_skill_30d"):
        assert f"DROP VIEW IF EXISTS {obj}" in text
    assert "DROP FUNCTION IF EXISTS compute_skill_score" in text


# ---------------------------------------------------------------------------
# 2026_05_05_phase_y_recall_log.sql
# ---------------------------------------------------------------------------
def test_recall_log_creates_table() -> None:
    text = RECALL_LOG_SQL.read_text(encoding="utf-8")
    assert re.search(
        r"CREATE TABLE IF NOT EXISTS\s+mem0_recall_log\b",
        text,
        re.IGNORECASE,
    )


def test_recall_log_has_three_match_flags() -> None:
    text = RECALL_LOG_SQL.read_text(encoding="utf-8")
    for col in ("match_outcome", "match_dim", "match_symbol"):
        assert re.search(rf"\b{col}\s+BOOLEAN", text), \
            f"mem0_recall_log.{col} BOOLEAN column missing"


def test_recall_log_has_required_indices() -> None:
    text = RECALL_LOG_SQL.read_text(encoding="utf-8")
    # Window query index (actor_id, company_id, created_at DESC)
    assert "idx_mem0_recall_actor_window" in text
    # position_id partial UNIQUE index (prevents duplicate retroactive matches)
    assert "uq_mem0_recall_position" in text


def test_recall_log_position_id_is_unique() -> None:
    """Bug-hunt fix BH-1 (revised Y.2): a retried postmortem_service.py update
    must not inflate C4 — but the constraint MUST include actor_id, otherwise
    multi-agent recalls for the same position get silently dropped after the
    first one. C4 aggregates per (actor_id, company_id) over a time window,
    so each actor needs its own resolved recall row per position."""
    text = RECALL_LOG_SQL.read_text(encoding="utf-8")
    assert re.search(
        r"CREATE UNIQUE INDEX IF NOT EXISTS\s+uq_mem0_recall_position",
        text,
        re.IGNORECASE,
    ), "mem0_recall_log must have a partial UNIQUE index uq_mem0_recall_position"
    assert "WHERE position_id IS NOT NULL" in text, \
        "UNIQUE index must be partial (pending rows allowed to be NULL)"
    # Must be UNIQUE (actor_id, position_id), NOT UNIQUE (position_id) alone.
    assert re.search(
        r"uq_mem0_recall_position\s+ON\s+mem0_recall_log\s*\(\s*actor_id\s*,\s*position_id\s*\)",
        text,
        re.IGNORECASE,
    ), "UNIQUE index must be on (actor_id, position_id) to allow multi-agent recalls"


def test_recall_log_match_consistency_check() -> None:
    """Bug-hunt fix BH-2: matched_at and match_* flags must be set together."""
    text = RECALL_LOG_SQL.read_text(encoding="utf-8")
    assert "ck_mem0_recall_match_consistency" in text, \
        "matched_at/match_* consistency CHECK missing"


def test_recall_log_rollback_drops_table() -> None:
    text = RECALL_LOG_ROLLBACK.read_text(encoding="utf-8")
    assert "DROP TABLE IF EXISTS mem0_recall_log" in text


# ---------------------------------------------------------------------------
# 2026_05_05_phase_y_weight_recommendations.sql
# ---------------------------------------------------------------------------
def test_weight_rec_creates_table() -> None:
    text = WEIGHT_REC_SQL.read_text(encoding="utf-8")
    assert re.search(
        r"CREATE TABLE IF NOT EXISTS\s+skill_weight_recommendations\b",
        text,
        re.IGNORECASE,
    )


def test_weight_rec_window_check_constraint() -> None:
    text = WEIGHT_REC_SQL.read_text(encoding="utf-8")
    # window_days must be 7, 14, or 30 (matches dashboard tab labels).
    assert re.search(r"window_days\s+IN\s*\(\s*7\s*,\s*14\s*,\s*30\s*\)", text), \
        "window_days CHECK constraint must allow only (7, 14, 30)"


def test_weight_rec_applied_consistency_check() -> None:
    text = WEIGHT_REC_SQL.read_text(encoding="utf-8")
    # The applied + applied_at consistency invariant must be enforced at the DB.
    assert "ck_swr_applied_consistency" in text, \
        "applied/applied_at consistency CHECK missing"


def test_weight_rec_has_audit_columns() -> None:
    text = WEIGHT_REC_SQL.read_text(encoding="utf-8")
    for col in ("requested_by", "current_weights", "proposed_weights",
                "confidence_band", "model_provider", "model_name",
                "correlation_id", "cost_usd"):
        assert col in text, f"Audit column {col} missing"


def test_weight_rec_rollback_drops_table() -> None:
    text = WEIGHT_REC_ROLLBACK.read_text(encoding="utf-8")
    assert "DROP TABLE IF EXISTS skill_weight_recommendations" in text


# ---------------------------------------------------------------------------
# 2026_05_05_phase_y_feed_views.sql
# ---------------------------------------------------------------------------
def test_feed_views_creates_three_views() -> None:
    text = FEED_VIEWS_SQL.read_text(encoding="utf-8")
    for view in ("v_memory_feed_7d", "v_memory_feed_14d", "v_memory_feed_30d"):
        assert re.search(
            rf"CREATE OR REPLACE VIEW\s+{view}\b",
            text,
            re.IGNORECASE,
        ), f"Feed view {view} not created"


def test_feed_views_have_three_distinct_intervals() -> None:
    text = FEED_VIEWS_SQL.read_text(encoding="utf-8")
    # Each window literal must appear at least three times (postmortem_actor +
    # postmortem_company + edge_score_changes lanes).
    for days in ("7 days", "14 days", "30 days"):
        count = text.count(f"interval '{days}'")
        assert count >= 3, \
            f"Expected >=3 occurrences of interval '{days}', got {count}"


def test_feed_views_join_postmortems_to_tracked_positions() -> None:
    text = FEED_VIEWS_SQL.read_text(encoding="utf-8")
    # position_postmortems has no actor_id/company_id; the views MUST JOIN
    # to tracked_positions to derive them. Verify the JOIN appears.
    assert re.search(
        r"FROM\s+position_postmortems\s+pp\s+JOIN\s+tracked_positions\s+tp",
        text,
        re.IGNORECASE,
    ), "Feed views must JOIN position_postmortems to tracked_positions"


def test_feed_views_strip_tickles_prefix_in_fallback() -> None:
    """Bug-hunt fix BH-5: current_database() returns 'tickles_rubicon' but the
    logical company_id is 'rubicon'. Without the strip, orphan edge_score_changes
    rows would never match downstream filters and silently disappear."""
    text = FEED_VIEWS_SQL.read_text(encoding="utf-8")
    assert "regexp_replace(current_database(), '^tickles_'" in text, \
        "Fallback must strip the 'tickles_' prefix from current_database()"
    # Must NOT have the unstripped fallback anywhere.
    assert "COALESCE(actor_co.company_id, current_database())" not in text, \
        "Found unstripped current_database() fallback — BH-5 not fully applied"


def test_skill_views_c3_filter_consistent_with_c2() -> None:
    """Code-review fix CR-2.2: C3's actor-window filter must match C2's, including
    realized_pnl_pct IS NOT NULL — otherwise C2 and C3 disagree on which trades
    count and the composite score becomes inconsistent."""
    text = SKILL_VIEWS_SQL.read_text(encoding="utf-8")
    # Find the C3 block (between the C3 marker and the C4 marker).
    c3_match = re.search(
        r"-- C3:.*?(?=-- C4:)",
        text,
        re.DOTALL,
    )
    assert c3_match, "Could not locate C3 block in skill_views.sql"
    c3_block = c3_match.group(0)
    assert "tp.realized_pnl_pct IS NOT NULL" in c3_block, \
        "C3 filter must include realized_pnl_pct IS NOT NULL for consistency with C2"


def test_feed_views_use_real_edge_score_changes_columns() -> None:
    text = FEED_VIEWS_SQL.read_text(encoding="utf-8")
    # Must use the real column names, not the plan's wrong names.
    for real_col in ("score_after", "score_before", "logged_at", "note"):
        assert f"esc.{real_col}" in text or f"\n           {real_col}" in text \
               or real_col in text, \
            f"edge_score_changes column {real_col} not referenced"
    # Must NOT use the plan's wrong column names.
    for wrong_col in ("esc.event_type", "esc.old_score", "esc.new_score"):
        assert wrong_col not in text, \
            f"Found pre-Y.1 plan column name {wrong_col} — should be reconciled"


def test_feed_views_do_not_reference_clickhouse_table() -> None:
    text = FEED_VIEWS_SQL.read_text(encoding="utf-8")
    # agent_events lives in ClickHouse only; if it appears in the Postgres
    # migration the view will fail to compile.
    assert "FROM agent_events" not in text, \
        "agent_events must not be referenced in the Postgres feed views"
    assert " ae.agent_name" not in text and " ae.event_type" not in text, \
        "agent_events alias 'ae' must not appear"


def test_feed_views_emit_canonical_columns() -> None:
    text = FEED_VIEWS_SQL.read_text(encoding="utf-8")
    # Every feed row must have the documented columns.
    for col in ("source_kind", "tier", "actor", "company", "dimension",
                "body", "raw", "ts", "source_id", "correlation_id"):
        assert col in text, f"Canonical feed column {col} missing"


def test_feed_views_rollback_drops_all_views() -> None:
    text = FEED_VIEWS_ROLLBACK.read_text(encoding="utf-8")
    for view in ("v_memory_feed_7d", "v_memory_feed_14d", "v_memory_feed_30d"):
        assert f"DROP VIEW IF EXISTS {view}" in text


# ---------------------------------------------------------------------------
# Cross-cutting: filename pattern compliance
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", ALL_MIGRATIONS, ids=lambda p: p.name)
def test_filename_follows_phase_pattern(path: Path) -> None:
    # YYYY_MM_DD_phase_y_*.sql or YYYY_MM_DD_phase_y_*_ROLLBACK.sql
    assert re.match(
        r"^\d{4}_\d{2}_\d{2}_phase_y_[a-z_]+(?:_ROLLBACK)?\.sql$",
        path.name,
    ), f"Filename does not follow Phase Y migration pattern: {path.name}"
