"""
Module: insight_kinds
Purpose: Canonical enum of MemU insight kinds — single source of truth for
         the BroadcastPayload TypedDict, the MCP tool JSON schemas, the
         memu.insights CHECK constraint, and any runtime validators.
Location: /opt/tickles/shared/memu/insight_kinds.py

Phase Y.0 — reconciles three pre-existing drift points that disagreed:
  * shared/memu/broadcast_payload.py had  {lesson, regime_shift, anomaly, postmortem}
  * shared/mcp/tools/memory.py     had  {lesson, warning, playbook, postmortem}
  * shared/memu/client.py DDL      had  no CHECK constraint at all

The canonical set below is the union of both, matching PHASE_Y v2 §3.4 / §11 Q2
and the design intent recorded in
shared/docs/PHASE_Y_LEARNING_DASHBOARD_PLAN.md.

Adding a new kind requires:
  1. Add it here (INSIGHT_KINDS + InsightKind Literal).
  2. Run the bundled migration to refresh the CHECK constraint:
     shared/memu/migrations/2026_05_05_phase_y0_insight_kinds_check.sql
     (or the latest superseding migration).
  3. Update consumers that branch on kind (dashboard providers, etc.).
"""
from __future__ import annotations

import logging
from typing import FrozenSet, Literal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Runtime canonical set — used by validators, DDL builders, smoke tests.
# ---------------------------------------------------------------------------
INSIGHT_KINDS: FrozenSet[str] = frozenset({
    "lesson",
    "warning",
    "playbook",
    "postmortem",
    "regime_shift",
    "anomaly",
})

# ---------------------------------------------------------------------------
# Static type alias — used by TypedDicts and function signatures so mypy /
# type-checkers catch drift at edit time. MUST be kept in lockstep with
# INSIGHT_KINDS above; the smoke test test_insight_kinds.py asserts they match.
# ---------------------------------------------------------------------------
InsightKind = Literal[
    "lesson",
    "warning",
    "playbook",
    "postmortem",
    "regime_shift",
    "anomaly",
]


def is_valid_kind(kind: str) -> bool:
    """Return True iff ``kind`` is one of the canonical INSIGHT_KINDS values.

    Args:
        kind: Candidate insight kind string (case-sensitive).

    Returns:
        True when ``kind`` is in the canonical set, False otherwise.
    """
    try:
        return kind in INSIGHT_KINDS
    except TypeError:
        # Defensive: someone passed a non-hashable (e.g. list). Reject loudly.
        logger.warning("is_valid_kind: non-string kind rejected: %r", kind)
        return False


def validate_kind(kind: str) -> str:
    """Validate ``kind`` against INSIGHT_KINDS or raise ``ValueError``.

    Args:
        kind: Candidate insight kind string.

    Returns:
        The validated ``kind`` unchanged (for use in fluent-style call chains).

    Raises:
        ValueError: If ``kind`` is not in the canonical set. Error message
            lists every valid kind in alphabetical order to aid debugging.
    """
    try:
        if kind in INSIGHT_KINDS:
            return kind
    except TypeError as exc:
        raise ValueError(
            f"insight kind must be a string, got {type(kind).__name__}"
        ) from exc

    valid = ", ".join(sorted(INSIGHT_KINDS))
    raise ValueError(
        f"invalid insight kind {kind!r}; expected one of: {valid}"
    )


def check_constraint_sql(column: str = "kind") -> str:
    """Render a Postgres ``CHECK (... IN (...))`` clause for ``column``.

    Args:
        column: Column name to constrain (default ``"kind"``). Must be a
            valid SQL identifier — caller is responsible for quoting if
            it contains anything other than ``[A-Za-z0-9_]``.

    Returns:
        A SQL fragment such as ``CHECK (kind IN ('anomaly', 'lesson', ...))``
        with values quoted and sorted for deterministic output.
    """
    if not column or not column.replace("_", "").isalnum():
        raise ValueError(
            f"check_constraint_sql: invalid column identifier {column!r}"
        )
    quoted = ", ".join(f"'{k}'" for k in sorted(INSIGHT_KINDS))
    return f"CHECK ({column} IN ({quoted}))"


__all__ = [
    "INSIGHT_KINDS",
    "InsightKind",
    "is_valid_kind",
    "validate_kind",
    "check_constraint_sql",
]
