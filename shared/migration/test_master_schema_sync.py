"""
Module: test_master_schema_sync
Purpose: Static verification that master schema templates contain Phase 2 structures.
Location: /opt/tickles/shared/migration/test_master_schema_sync.py

This test does NOT require a live database.  It reads the SQL template files
and asserts that the expected tables, columns, indexes, and triggers are
present.  This catches the [BN] master-schema drift risk: a developer who
adds a column to the migration but forgets to update the master template.
"""

import re
from pathlib import Path

import pytest

MIGRATION_DIR = Path(__file__).parent

SHARED_TEMPLATE = MIGRATION_DIR / "tickles_shared_pg.sql"
COMPANY_TEMPLATE = MIGRATION_DIR / "tickles_company_pg.sql"


class TestSharedMasterSchema:
    """Assertions against tickles_shared_pg.sql."""

    @pytest.fixture(scope="class")
    def shared_sql(self) -> str:
        assert SHARED_TEMPLATE.exists(), f"{SHARED_TEMPLATE} missing"
        return SHARED_TEMPLATE.read_text()

    def test_tracked_positions_table_exists(self, shared_sql: str) -> None:
        assert "CREATE TABLE IF NOT EXISTS public.tracked_positions" in shared_sql

    def test_tracked_positions_has_phase2_columns(self, shared_sql: str) -> None:
        required = [
            "actor_type",
            "actor_id",
            "department",
            "position_kind",
            "asset_class",
            "venue",
            "legs",
            "sl_history",
            "partial_closes",
            "entry_reason_trader",
            "entry_reason_llm",
            "entry_reason_agent",
            "entry_reason_frozen_at",
            "exit_reason_trader",
            "exit_reason_llm",
            "exit_reason_system",
            "closed_at",
            "realized_pnl_usd_final",
            "postmortem_status",
            "correlation_id",
        ]
        for col in required:
            assert col in shared_sql, f"Missing Phase 2 column in shared template: {col}"

    def test_tracked_positions_no_jarvais_default(self, shared_sql: str) -> None:
        # The old default was 'jarvais' — it must not appear in the CREATE
        match = re.search(
            r"company_id\s+VARCHAR\(\d+\)\s+.*DEFAULT\s+'jarvais'",
            shared_sql,
            re.IGNORECASE,
        )
        assert match is None, "tracked_positions still has DEFAULT 'jarvais'"

    def test_tracked_positions_company_id_not_null(self, shared_sql: str) -> None:
        # After Phase 2, company_id must be NOT NULL
        assert "company_id            VARCHAR(50)     NOT NULL" in shared_sql

    def test_freeze_trigger_function_exists(self, shared_sql: str) -> None:
        assert "CREATE OR REPLACE FUNCTION public.fn_freeze_entry_reasons()" in shared_sql

    def test_freeze_trigger_installed(self, shared_sql: str) -> None:
        assert "CREATE TRIGGER trg_tp_freeze_entry_reasons" in shared_sql

    def test_phase2_indexes_exist(self, shared_sql: str) -> None:
        required_indexes = [
            "idx_tp_actor_type",
            "idx_tp_actor_id",
            "idx_tp_postmortem",
            "idx_tp_closed_at",
            "idx_tp_correlation_id",
        ]
        for idx in required_indexes:
            assert idx in shared_sql, f"Missing index in shared template: {idx}"

    def test_tracked_positions_has_phase6_columns(self, shared_sql: str) -> None:
        required = [
            "instrument_symbol_normalised",
            "reason_agreement_score",
            "entry_reason_trader_embedding",
        ]
        for col in required:
            assert col in shared_sql, f"Missing Phase 6 column in shared template: {col}"

    def test_phase6_indexes_exist(self, shared_sql: str) -> None:
        required_indexes = [
            "idx_tp_symbol_norm",
            "idx_tp_entry_reason_embed_cosine",
        ]
        for idx in required_indexes:
            assert idx in shared_sql, f"Missing Phase 6 index in shared template: {idx}"

    def test_prompt_versions_table_exists(self, shared_sql: str) -> None:
        assert "CREATE TABLE IF NOT EXISTS public.prompt_versions" in shared_sql

    def test_prompt_versions_indexes_exist(self, shared_sql: str) -> None:
        required_indexes = [
            "idx_prompt_versions_name",
            "idx_prompt_versions_hash",
        ]
        for idx in required_indexes:
            assert idx in shared_sql, f"Missing index in prompt_versions: {idx}"

    def test_memu_outbox_table_exists(self, shared_sql: str) -> None:
        assert "CREATE TABLE IF NOT EXISTS public.memu_outbox" in shared_sql

    def test_memu_outbox_index_exists(self, shared_sql: str) -> None:
        assert "idx_memu_outbox_unprocessed" in shared_sql

    # Phase 9 assertions
    def test_news_items_has_phase9_columns(self, shared_sql: str) -> None:
        required = [
            "enrichment_status",
            "context_window",
            "zone_filter_confidence",
            "zone_filter_reason",
            "image_phash",
            "duplicate_of_id",
        ]
        for col in required:
            assert col in shared_sql, f"Missing Phase 9 column in news_items: {col}"

    def test_news_items_phase9_indexes_exist(self, shared_sql: str) -> None:
        required_indexes = [
            "idx_news_items_phash_recent",
            "idx_news_items_pending",
        ]
        for idx in required_indexes:
            assert idx in shared_sql, f"Missing Phase 9 index in news_items: {idx}"

    def test_collector_sources_has_phase9_columns(self, shared_sql: str) -> None:
        required = [
            "zone_filter_enabled",
            "zone_filter_threshold",
            "rate_limit_msgs_per_sec",
        ]
        for col in required:
            assert col in shared_sql, f"Missing Phase 9 column in collector_sources: {col}"

    # Phase 10 assertions
    def test_tracked_positions_has_phase10_columns(self, shared_sql: str) -> None:
        required = [
            "actor_instance",
            "source_position_id",
        ]
        for col in required:
            assert col in shared_sql, f"Missing Phase 10 column in tracked_positions: {col}"

    def test_tracked_positions_phase10_unique_index(self, shared_sql: str) -> None:
        assert "uniq_tracked_positions_actor" in shared_sql
        assert "actor_type, actor_id, actor_instance, source_position_id" in shared_sql


class TestCompanyMasterSchema:
    """Assertions against tickles_company_pg.sql."""

    @pytest.fixture(scope="class")
    def company_sql(self) -> str:
        assert COMPANY_TEMPLATE.exists(), f"{COMPANY_TEMPLATE} missing"
        return COMPANY_TEMPLATE.read_text()

    def test_signal_interpretations_has_phase2_columns(self, company_sql: str) -> None:
        required = [
            "prefilter_provider",
            "prefilter_model",
            "prefilter_temperature",
            "prefilter_result",
            "prefilter_cost_usd",
            "vision_provider",
            "vision_model_requested",
            "vision_model_resolved",
            "vision_temperature",
            "prompt_version",
            "prompt_hash",
            "llm_raw_request_path",
            "llm_raw_response_path",
            "trader_stated_thesis",
            "llm_inferred_thesis",
            "reason_agreement_score",
            "pattern_tags",
            "setup_tags",
            "regime_tags",
            "session_tags",
            "instrument_symbol_normalised",
            "instrument_exchange",
            "correlation_id",
        ]
        for col in required:
            assert col in company_sql, f"Missing Phase 2 column in company template: {col}"

    def test_signal_interpretations_indexes_exist(self, company_sql: str) -> None:
        required_indexes = [
            "idx_si_prompt_version",
            "idx_si_pattern_tags",
            "idx_si_setup_tags",
            "idx_si_regime_tags",
            "idx_si_session_tags",
            "idx_si_symbol_norm",
            "idx_si_exchange",
            "idx_si_correlation_id",
        ]
        for idx in required_indexes:
            assert idx in company_sql, f"Missing index in company template: {idx}"

    def test_position_postmortems_table_exists(self, company_sql: str) -> None:
        assert "CREATE TABLE position_postmortems" in company_sql

    def test_position_postmortems_composite_unique(self, company_sql: str) -> None:
        # The composite UNIQUE must use (position_id, postmortem_version, prompt_version)
        assert (
            "UNIQUE (position_id, postmortem_version, prompt_version)"
            in company_sql
        )

    def test_position_postmortems_indexes_exist(self, company_sql: str) -> None:
        required_indexes = [
            "idx_pm_position_id",
            "idx_pm_postmortem_version",
            "idx_pm_param_hash",
            "idx_pm_correlation_id",
        ]
        for idx in required_indexes:
            assert idx in company_sql, f"Missing index in company template: {idx}"

    def test_no_check_constraints_on_tags(self, company_sql: str) -> None:
        """G1: No CHECK constraints on *_tags JSONB columns.

        The LLM emits free-form strings; a CHECK would re-introduce the
        declared-vocabulary problem.  Verify none exist in the template.
        """
        tag_check = re.search(
            r"CHECK\s*\(\s*jsonb_typeof\s*\(\s*(pattern_tags|setup_tags|regime_tags|session_tags)",
            company_sql,
            re.IGNORECASE,
        )
        assert tag_check is None, f"Found forbidden CHECK on JSONB tags: {tag_check.group(0)}"

    # Phase 8 assertions
    def test_agent_opinions_has_phase8_columns(self, company_sql: str) -> None:
        required = [
            "snapshot_price_bucket_pct",
            "hour_bucket_utc",
            "memo_confidence",
            "is_published",
        ]
        for col in required:
            assert col in company_sql, f"Missing Phase 8 column in agent_opinions: {col}"

    def test_agent_opinions_phase8_indexes_exist(self, company_sql: str) -> None:
        required_indexes = [
            "uniq_agent_opinions_dedup",
            "idx_agent_opinions_published_open",
        ]
        for idx in required_indexes:
            assert idx in company_sql, f"Missing Phase 8 index in agent_opinions: {idx}"

    # Phase 9 assertions
    def test_signal_interpretations_has_instrument_resolved_from(self, company_sql: str) -> None:
        assert "instrument_resolved_from" in company_sql
        assert "CHECK (instrument_resolved_from IN ('message','context','inferred','unknown'))" in company_sql

    # Phase 10 assertions
    def test_agent_state_has_actor_instance(self, company_sql: str) -> None:
        assert "actor_instance" in company_sql
        assert "uq_agent_name UNIQUE (agent_name, actor_instance)" in company_sql

    # Phase 11 assertions
    def test_actor_performance_table_exists(self, company_sql: str) -> None:
        assert "CREATE TABLE IF NOT EXISTS actor_performance" in company_sql

    def test_actor_performance_columns(self, company_sql: str) -> None:
        required = [
            "actor_type",
            "actor_id",
            "period_start",
            "period_end",
            "closed_position_count",
            "edge_score",
            "components_jsonb",
            "weights_used_jsonb",
            "confidence_low",
            "formula_version",
        ]
        for col in required:
            assert col in company_sql, f"Missing Phase 11 column in actor_performance: {col}"

    def test_actor_leaderboard_view_exists(self, company_sql: str) -> None:
        assert "CREATE OR REPLACE VIEW actor_leaderboard" in company_sql

    def test_edge_score_changes_table_exists(self, company_sql: str) -> None:
        assert "CREATE TABLE IF NOT EXISTS edge_score_changes" in company_sql

    def test_prompt_assignments_table_exists(self, company_sql: str) -> None:
        assert "CREATE TABLE IF NOT EXISTS prompt_assignments" in company_sql
