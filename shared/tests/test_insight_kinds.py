"""
Module: test_insight_kinds
Purpose: Phase Y.0 smoke tests — guarantees the canonical INSIGHT_KINDS set
         stays in lockstep with every consumer (BroadcastPayload Literal,
         MCP tool JSON schemas, the SQL CHECK constraint rendered into
         memu.insights, and the migration script).
Location: /opt/tickles/shared/tests/test_insight_kinds.py

Run with: python -m pytest shared/tests/test_insight_kinds.py -v
"""
from __future__ import annotations

import re
import typing
from pathlib import Path

import pytest

from shared.memu.broadcast_payload import BroadcastPayload
from shared.memu.insight_kinds import (
    INSIGHT_KINDS,
    InsightKind,
    check_constraint_sql,
    is_valid_kind,
    validate_kind,
)


# ---------------------------------------------------------------------------
# 1. Canonical set basic invariants
# ---------------------------------------------------------------------------

def test_insight_kinds_is_frozenset_of_str() -> None:
    """INSIGHT_KINDS must be an immutable frozenset of strings."""
    assert isinstance(INSIGHT_KINDS, frozenset)
    assert all(isinstance(k, str) for k in INSIGHT_KINDS)
    # Refuse to ship if the set is empty or accidentally mutated.
    assert len(INSIGHT_KINDS) >= 1


def test_canonical_six_kinds_present() -> None:
    """The Phase Y.0 design fixes the union at exactly these six values.

    Adding a new kind is a deliberate cross-cutting change that must update
    the migration, the DDL, and probably consumers — so this test exists
    specifically to make accidental additions (or removals) loud.
    """
    expected = {
        "lesson",
        "warning",
        "playbook",
        "postmortem",
        "regime_shift",
        "anomaly",
    }
    assert INSIGHT_KINDS == frozenset(expected)


# ---------------------------------------------------------------------------
# 2. Literal alias matches the runtime set
# ---------------------------------------------------------------------------

def test_insight_kind_literal_matches_runtime_set() -> None:
    """The static InsightKind alias and runtime INSIGHT_KINDS must agree.

    typing.get_args() returns the Literal values; we compare as a set so
    ordering doesn't matter.
    """
    literal_values = set(typing.get_args(InsightKind))
    assert literal_values == set(INSIGHT_KINDS), (
        "InsightKind Literal drifted from INSIGHT_KINDS frozenset. "
        f"Literal={literal_values} runtime={set(INSIGHT_KINDS)}"
    )


def test_broadcast_payload_uses_canonical_alias() -> None:
    """BroadcastPayload.insight_kind must resolve to InsightKind (= same Literal)."""
    hints = typing.get_type_hints(BroadcastPayload)
    field_type = hints["insight_kind"]
    field_values = set(typing.get_args(field_type))
    assert field_values == set(INSIGHT_KINDS), (
        "BroadcastPayload.insight_kind drifted from INSIGHT_KINDS. "
        f"payload={field_values} canonical={set(INSIGHT_KINDS)}"
    )


# ---------------------------------------------------------------------------
# 3. Runtime validators
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", sorted(INSIGHT_KINDS))
def test_is_valid_kind_accepts_every_canonical_value(kind: str) -> None:
    """Every member of the canonical set passes is_valid_kind()."""
    assert is_valid_kind(kind) is True


@pytest.mark.parametrize("kind", ["", "Lesson", "LESSON", "unknown", "lesson "])
def test_is_valid_kind_rejects_non_canonical(kind: str) -> None:
    """Case-sensitivity and whitespace are NOT forgiven — by design."""
    assert is_valid_kind(kind) is False


def test_is_valid_kind_handles_non_string_safely() -> None:
    """A non-hashable input must not raise — it must return False."""
    assert is_valid_kind([1, 2]) is False  # type: ignore[arg-type]


def test_validate_kind_returns_kind_on_success() -> None:
    """validate_kind() returns the unchanged kind for chained-call ergonomics."""
    assert validate_kind("lesson") == "lesson"


def test_validate_kind_raises_with_helpful_message() -> None:
    """The error message must list every valid kind so callers know what to fix."""
    with pytest.raises(ValueError) as exc_info:
        validate_kind("not_a_kind")
    msg = str(exc_info.value)
    assert "not_a_kind" in msg
    for kind in INSIGHT_KINDS:
        assert kind in msg


def test_validate_kind_rejects_non_string() -> None:
    """Passing a non-string yields a ValueError, never a TypeError leak."""
    with pytest.raises(ValueError):
        validate_kind(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 4. SQL CHECK constraint generator
# ---------------------------------------------------------------------------

def test_check_constraint_sql_is_deterministic_and_sorted() -> None:
    """Output must be sorted alphabetically so diffs across runs are stable."""
    sql = check_constraint_sql("kind")
    assert sql.startswith("CHECK (kind IN (")
    assert sql.endswith("))")
    # Extract the value list and verify it's the sorted canonical set.
    inner = sql[len("CHECK (kind IN ("):-2]
    values = [v.strip().strip("'") for v in inner.split(",")]
    assert values == sorted(INSIGHT_KINDS)


def test_check_constraint_sql_custom_column() -> None:
    """The column argument is honoured (used by future tables)."""
    sql = check_constraint_sql("category")
    assert sql.startswith("CHECK (category IN (")


def test_check_constraint_sql_rejects_bad_identifier() -> None:
    """SQL-injection-shaped column names are refused."""
    with pytest.raises(ValueError):
        check_constraint_sql("kind; DROP TABLE insights; --")


# ---------------------------------------------------------------------------
# 5. Migration file mirrors the canonical set
# ---------------------------------------------------------------------------

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "memu"
    / "migrations"
    / "2026_05_05_phase_y0_insight_kinds_check.sql"
)


def test_migration_file_exists() -> None:
    """The retrofit migration must be present alongside the module."""
    assert _MIGRATION_PATH.is_file(), (
        f"Phase Y.0 migration missing at {_MIGRATION_PATH}"
    )


def test_migration_lists_every_canonical_kind() -> None:
    """Every kind in INSIGHT_KINDS must appear, quoted, inside the migration.

    Belt-and-braces: prevents an editor from updating insight_kinds.py but
    forgetting to refresh the migration that production deployments run.
    """
    sql = _MIGRATION_PATH.read_text(encoding="utf-8")
    for kind in INSIGHT_KINDS:
        assert f"'{kind}'" in sql, (
            f"kind {kind!r} missing from migration {_MIGRATION_PATH.name}; "
            "update the IN-list in the audit DO-block AND the ADD CONSTRAINT."
        )


def test_migration_uses_canonical_constraint_name() -> None:
    """The constraint name MUST match _KIND_CHECK_NAME used by client.py DDL.

    If they diverge, fresh schema creation (CREATE TABLE) and migrated
    schemas (ALTER TABLE) end up with different constraint names, which
    breaks the DROP CONSTRAINT IF EXISTS idempotency guarantee.
    """
    sql = _MIGRATION_PATH.read_text(encoding="utf-8")
    # Both the DROP and the ADD must reference the same name.
    assert re.search(
        r"DROP\s+CONSTRAINT\s+IF\s+EXISTS\s+ck_insights_kind_enum",
        sql,
        re.IGNORECASE,
    )
    assert re.search(
        r"ADD\s+CONSTRAINT\s+ck_insights_kind_enum",
        sql,
        re.IGNORECASE,
    )


# ---------------------------------------------------------------------------
# 6. MCP tool schema embeds the canonical set
# ---------------------------------------------------------------------------

def test_mcp_memory_module_imports_canonical_set() -> None:
    """shared.mcp.tools.memory must import INSIGHT_KINDS, not hardcode kinds.

    This guards against a regression where an editor copy-pastes a literal
    list back into the JSON schemas.
    """
    memory_path = (
        Path(__file__).resolve().parents[1]
        / "mcp"
        / "tools"
        / "memory.py"
    )
    src = memory_path.read_text(encoding="utf-8")
    assert "from shared.memu.insight_kinds import INSIGHT_KINDS" in src, (
        "shared/mcp/tools/memory.py must import INSIGHT_KINDS"
    )
    # The four-value pre-Y.0 hardcoded list must not reappear anywhere.
    legacy = '["lesson", "warning", "playbook", "postmortem"]'
    assert legacy not in src, (
        "Pre-Y.0 hardcoded enum re-introduced in shared/mcp/tools/memory.py"
    )
