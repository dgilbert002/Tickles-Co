"""
Module: recall_log
Purpose: Writer + retroactive updater for the ``mem0_recall_log`` table.
Location: /opt/tickles/shared/intelligence/recall_log.py

Phase Y.2 — see [`shared/docs/PHASE_Y_LEARNING_DASHBOARD_PLAN.md`](shared/docs/PHASE_Y_LEARNING_DASHBOARD_PLAN.md:257)
§3.4. The table records every mem0 recall made at decision time and is
read by C4 of [`compute_skill_score()`](shared/intelligence/migrations/2026_05_05_phase_y_skill_views.sql:46).

Lifecycle:
  1. Decision time:  ``record_recall()`` inserts one row per signal with
     match_* / matched_at all NULL ("pending"). If ``position_id`` is
     known at this point (rare — usually the position is created right
     after), pass it in directly; otherwise call ``link_recall_to_position()``
     once the tracked_position row id is known.
  2. Position close: ``match_recall_to_outcome()`` is called by
     [`postmortem_service._process_one()`](shared/intelligence/postmortem_service.py:526)
     for each pending recall whose ``position_id`` matches the closing
     position. It evaluates the three tiered match flags by scanning
     ``top_k_metadata`` JSONB and writes them all together with
     ``matched_at = now()`` (CHECK constraint
     ``ck_mem0_recall_match_consistency`` enforces this all-or-nothing
     rule).

Tier scoring is performed on the SQL side by C4 of
``compute_skill_score()``; this module only persists the booleans.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

import asyncpg

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum length we will store for query_summary — keep INSERT payloads small
# and protect against a multi-MB raw signal blowing up the row.
_QUERY_SUMMARY_MAX = 2000

# Maximum length per text column (query_dimension, query_symbol, etc.).
_SHORT_TEXT_MAX = 200


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _truncate(value: Optional[str], limit: int) -> Optional[str]:
    """Trim ``value`` to ``limit`` characters, preserving ``None``.

    Args:
        value: Optional string to trim.
        limit: Maximum length in characters.

    Returns:
        Trimmed string, or ``None`` if input was ``None``.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    if len(value) <= limit:
        return value
    return value[:limit]


def _normalise_outcome(outcome: Optional[str]) -> Optional[str]:
    """Normalise a position outcome label to lowercase canonical form.

    The recall_log match logic only compares lowercase-stripped outcome
    labels; this prevents mismatches like ``'WIN'`` vs ``'win'`` silently
    inflating C4.

    Args:
        outcome: Raw outcome string (``'win'``, ``'loss'``, ``'breakeven'``,
            etc.) or ``None``.

    Returns:
        Lowercased + stripped outcome, or ``None``.
    """
    if outcome is None:
        return None
    if not isinstance(outcome, str):
        return None
    norm = outcome.strip().lower()
    return norm or None


def _evaluate_matches(
    top_k_metadata: Sequence[Dict[str, Any]],
    *,
    position_outcome: Optional[str],
    position_symbol: Optional[str],
    query_dimension: Optional[str],
) -> Dict[str, bool]:
    """Compute the three tiered match booleans from a top-k metadata list.

    Per [`PHASE_Y plan`](shared/docs/PHASE_Y_LEARNING_DASHBOARD_PLAN.md:262)
    §3.4, each flag is the OR over top_k results: TRUE if ANY returned
    memory matched on that axis.

    Args:
        top_k_metadata: List of mem0 result metadata dicts as returned by
            ``ScopedMemory.search()`` (the ``metadata`` field of each hit).
            May contain mixed types or missing keys — we tolerate both.
        position_outcome: Closed-position outcome label (``'win'`` /
            ``'loss'`` / ``'breakeven'`` / ...). Compared case-insensitively.
        position_symbol: Closed-position instrument symbol (slash form,
            e.g. ``'BTC/USDT'``). Compared case-sensitive (symbols are
            already canonicalised upstream by ``normalise_instrument``).
        query_dimension: The ``query_dimension`` recorded at decision time
            (``'lesson'`` / ``'warning'`` / ``'postmortem'`` / etc).

    Returns:
        Dict with keys ``match_outcome``, ``match_dim``, ``match_symbol``,
        each a non-null bool. Even if every input is None or empty we
        return all-False (never None) so the CHECK constraint's
        all-non-NULL branch is always satisfied.
    """
    norm_outcome = _normalise_outcome(position_outcome)
    norm_dim = (query_dimension or "").strip().lower() or None

    match_outcome = False
    match_dim = False
    match_symbol = False

    for entry in top_k_metadata or []:
        if not isinstance(entry, dict):
            continue

        if norm_outcome is not None and not match_outcome:
            entry_outcome = _normalise_outcome(entry.get("outcome"))
            if entry_outcome is not None and entry_outcome == norm_outcome:
                match_outcome = True

        if norm_dim is not None and not match_dim:
            entry_dim = entry.get("dimension")
            if isinstance(entry_dim, str):
                if entry_dim.strip().lower() == norm_dim:
                    match_dim = True

        if position_symbol and not match_symbol:
            entry_symbol = entry.get("symbol")
            if isinstance(entry_symbol, str):
                if entry_symbol == position_symbol:
                    match_symbol = True

        # Short-circuit: once all three are TRUE, no further work needed.
        if match_outcome and match_dim and match_symbol:
            break

    return {
        "match_outcome": match_outcome,
        "match_dim": match_dim,
        "match_symbol": match_symbol,
    }


def _coerce_metadata_list(value: Any) -> List[Dict[str, Any]]:
    """Coerce a raw JSONB column value into a list of metadata dicts.

    asyncpg returns JSONB either as a parsed Python object (when the
    pool's codec is configured for it) or as a raw string (default).
    We accept both and tolerate non-list inputs by returning ``[]``.

    Args:
        value: The JSONB column value.

    Returns:
        A list of dicts; never None. Non-dict elements are dropped.
    """
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            logger.warning("recall_log: malformed top_k_metadata JSON: %s", exc)
            return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


_INSERT_SQL = """
INSERT INTO mem0_recall_log (
    actor_id, company_id, correlation_id,
    query_summary, query_dimension, query_symbol,
    returned_count, top_k_ids, top_k_metadata,
    position_id
) VALUES (
    $1, $2, $3,
    $4, $5, $6,
    $7, $8::jsonb, $9::jsonb,
    $10
)
RETURNING id
"""


async def record_recall(
    conn: asyncpg.Connection,
    *,
    actor_id: str,
    company_id: str,
    query_summary: str,
    correlation_id: Optional[str] = None,
    query_dimension: Optional[str] = None,
    query_symbol: Optional[str] = None,
    top_k_ids: Optional[Iterable[Any]] = None,
    top_k_metadata: Optional[Iterable[Dict[str, Any]]] = None,
    position_id: Optional[int] = None,
) -> Optional[int]:
    """Insert one ``mem0_recall_log`` row for a recall made at decision time.

    The match_* columns are left NULL ("pending") and filled retroactively
    by ``match_recall_to_outcome()`` after the corresponding tracked
    position closes.

    Args:
        conn: Active asyncpg connection on the per-company database.
        actor_id: Identifier of the agent that performed the recall.
        company_id: Per-company namespace (matches ``tracked_positions.company_id``).
        query_summary: Raw signal text passed to ``mem.search()``. Truncated
            to 2000 chars on insert.
        correlation_id: Optional trace ID joining api_cost_log + payload_store.
        query_dimension: Memory dimension queried (``'lesson'`` / ``'warning'`` /
            ``'postmortem'`` / etc). Used by C4 match_dim evaluation.
        query_symbol: Slash-form instrument symbol queried, if any.
        top_k_ids: Iterable of top-k memory IDs returned by mem0. Persisted
            as JSONB. Any iterable of JSON-serialisable scalars.
        top_k_metadata: Iterable of metadata dicts (one per top-k result).
            Persisted as JSONB. Used by the retroactive matcher.
        position_id: Pre-link to ``tracked_positions.id`` if the position
            row already exists. Usually NULL at insert time and patched by
            ``link_recall_to_position()`` once the position id is known.

    Returns:
        Inserted row id, or ``None`` on database error. Errors are logged
        but never re-raised — recall logging must not break the trading
        decision path.
    """
    if not actor_id or not isinstance(actor_id, str):
        logger.warning("recall_log.record_recall: missing/invalid actor_id; skipping")
        return None
    if not company_id or not isinstance(company_id, str):
        logger.warning("recall_log.record_recall: missing/invalid company_id; skipping")
        return None
    if query_summary is None:
        query_summary = ""
    if not isinstance(query_summary, str):
        query_summary = str(query_summary)

    summary = _truncate(query_summary, _QUERY_SUMMARY_MAX) or ""
    dim = _truncate(query_dimension, _SHORT_TEXT_MAX)
    symbol = _truncate(query_symbol, _SHORT_TEXT_MAX)
    cid = _truncate(correlation_id, _SHORT_TEXT_MAX)

    # Mirror the validation in link_recall_to_position: reject obviously
    # bad position ids upfront rather than relying on the FK to catch
    # them. ``None`` stays valid (the row is "pending" and will be
    # patched by link_recall_to_position later).
    if position_id is not None and (
        not isinstance(position_id, int)
        or isinstance(position_id, bool)
        or position_id <= 0
    ):
        logger.warning(
            "recall_log.record_recall: invalid position_id=%r — coercing to NULL",
            position_id,
        )
        position_id = None

    ids_list = list(top_k_ids) if top_k_ids is not None else []
    md_list = list(top_k_metadata) if top_k_metadata is not None else []
    md_clean = [m for m in md_list if isinstance(m, dict)]
    returned_count = len(md_clean) if md_clean else len(ids_list)

    try:
        ids_json = json.dumps(ids_list, default=str)
        md_json = json.dumps(md_clean, default=str)
    except (TypeError, ValueError) as exc:
        logger.warning(
            "recall_log.record_recall: failed to serialise top_k payload "
            "(actor=%s company=%s): %s",
            actor_id, company_id, exc,
        )
        return None

    try:
        row_id = await conn.fetchval(
            _INSERT_SQL,
            actor_id,
            company_id,
            cid,
            summary,
            dim,
            symbol,
            returned_count,
            ids_json,
            md_json,
            position_id,
        )
        return int(row_id) if row_id is not None else None
    except asyncpg.PostgresError as exc:
        logger.warning(
            "recall_log.record_recall: INSERT failed actor=%s company=%s: %s",
            actor_id, company_id, exc,
        )
        return None


# ---------------------------------------------------------------------------
# Linker — set position_id on a pending row
# ---------------------------------------------------------------------------


async def link_recall_to_position(
    conn: asyncpg.Connection,
    *,
    recall_id: int,
    position_id: int,
) -> bool:
    """Patch ``position_id`` onto a previously inserted recall row.

    Used when the mem0 recall is made *before* the tracked_position row
    exists (the common ordering in the interpretation pipeline). The
    recall is inserted with ``position_id = NULL`` and patched here as
    soon as the new tracked_position id is known.

    The partial UNIQUE index ``uq_mem0_recall_position`` is on
    ``(actor_id, position_id) WHERE position_id IS NOT NULL``, which
    enforces at most one resolved recall *per actor* per position —
    multiple actors recalling for the same position each get their own
    row. Pending rows (position_id IS NULL) are unconstrained. A
    ``UniqueViolationError`` here means this same actor already has a
    resolved recall row for this position (typically from a retried
    postmortem update) and is treated as a no-op.

    Args:
        conn: Active asyncpg connection.
        recall_id: ``mem0_recall_log.id`` to patch.
        position_id: ``tracked_positions.id`` to link.

    Returns:
        True on successful update of exactly one row, False otherwise
        (already linked, conflict on UNIQUE, or DB error). Failures are
        logged but never re-raised.
    """
    if not isinstance(recall_id, int) or recall_id <= 0:
        logger.warning(
            "recall_log.link_recall_to_position: invalid recall_id=%r",
            recall_id,
        )
        return False
    if not isinstance(position_id, int) or position_id <= 0:
        logger.warning(
            "recall_log.link_recall_to_position: invalid position_id=%r",
            position_id,
        )
        return False

    try:
        result = await conn.execute(
            """
            UPDATE mem0_recall_log
               SET position_id = $1
             WHERE id = $2
               AND position_id IS NULL
            """,
            position_id,
            recall_id,
        )
        # asyncpg returns "UPDATE n" — extract the count.
        parts = result.split()
        if len(parts) == 2 and parts[0] == "UPDATE":
            try:
                rowcount = int(parts[1])
            except ValueError:
                rowcount = 0
        else:
            rowcount = 0
        return rowcount == 1
    except asyncpg.UniqueViolationError as exc:
        logger.warning(
            "recall_log.link_recall_to_position: UNIQUE conflict "
            "recall_id=%s position_id=%s: %s",
            recall_id, position_id, exc,
        )
        return False
    except asyncpg.PostgresError as exc:
        logger.warning(
            "recall_log.link_recall_to_position: UPDATE failed "
            "recall_id=%s position_id=%s: %s",
            recall_id, position_id, exc,
        )
        return False


# ---------------------------------------------------------------------------
# Retroactive matcher
# ---------------------------------------------------------------------------


_FETCH_PENDING_FOR_POSITION_SQL = """
SELECT id, query_dimension, top_k_metadata
  FROM mem0_recall_log
 WHERE position_id = $1
   AND matched_at IS NULL
"""

_UPDATE_MATCH_SQL = """
UPDATE mem0_recall_log
   SET match_outcome = $2,
       match_dim     = $3,
       match_symbol  = $4,
       matched_at    = $5
 WHERE id = $1
   AND matched_at IS NULL
"""


async def match_recall_to_outcome(
    conn: asyncpg.Connection,
    *,
    position_id: int,
    position_outcome: Optional[str],
    position_symbol: Optional[str],
) -> int:
    """Set match_* booleans + matched_at on every pending recall for a position.

    Called by [`postmortem_service._process_one()`](shared/intelligence/postmortem_service.py:526)
    when a tracked_position has just closed. The function:

      1. Fetches every pending recall row linked to the closed position
         (``position_id = $1 AND matched_at IS NULL``).
      2. Evaluates the three tiered match booleans from each row's
         own ``query_dimension`` + ``top_k_metadata`` against the
         position's outcome / symbol.
      3. Writes match_outcome, match_dim, match_symbol, matched_at all
         together (CHECK constraint ``ck_mem0_recall_match_consistency``
         enforces all-or-nothing).

    The CHECK constraint also forbids ``match_outcome IS NULL`` once
    ``matched_at IS NOT NULL``, which is why ``_evaluate_matches`` always
    returns three non-null bools.

    Args:
        conn: Active asyncpg connection (typically the same one held
            under the postmortem advisory lock).
        position_id: ``tracked_positions.id`` of the just-closed position.
        position_outcome: Closed position's ``outcome`` value. Compared
            case-insensitively to each top_k entry's ``outcome``.
        position_symbol: Closed position's ``instrument_symbol`` (slash
            form, e.g. ``'BTC/USDT'``).

    Returns:
        Number of rows updated. ``0`` is normal (no recall recorded for
        this position; common pre-Y.5 since the live pipeline doesn't
        call ``mem.search()`` yet). Errors are logged and surface a
        return of ``0`` rather than re-raising — postmortem service
        must not be derailed by a recall_log issue.
    """
    if not isinstance(position_id, int) or position_id <= 0:
        logger.warning(
            "recall_log.match_recall_to_outcome: invalid position_id=%r",
            position_id,
        )
        return 0

    try:
        rows = await conn.fetch(_FETCH_PENDING_FOR_POSITION_SQL, position_id)
    except asyncpg.PostgresError as exc:
        logger.warning(
            "recall_log.match_recall_to_outcome: fetch failed position_id=%s: %s",
            position_id, exc,
        )
        return 0

    if not rows:
        return 0

    now = datetime.now(timezone.utc)
    updated = 0
    for row in rows:
        try:
            recall_id = int(row["id"])
            metadata_list = _coerce_metadata_list(row["top_k_metadata"])
            flags = _evaluate_matches(
                metadata_list,
                position_outcome=position_outcome,
                position_symbol=position_symbol,
                query_dimension=row["query_dimension"],
            )
            result = await conn.execute(
                _UPDATE_MATCH_SQL,
                recall_id,
                flags["match_outcome"],
                flags["match_dim"],
                flags["match_symbol"],
                now,
            )
            parts = result.split()
            if len(parts) == 2 and parts[0] == "UPDATE":
                try:
                    if int(parts[1]) == 1:
                        updated += 1
                except ValueError:
                    pass
        except asyncpg.PostgresError as exc:
            # CHECK constraint violation, UNIQUE conflict, or transient DB
            # error — log and continue with the next row. We never let one
            # bad row stop the others from being matched.
            logger.warning(
                "recall_log.match_recall_to_outcome: UPDATE failed "
                "recall_id=%s position_id=%s: %s",
                row["id"], position_id, exc,
            )
            continue
        except (TypeError, ValueError) as exc:
            logger.warning(
                "recall_log.match_recall_to_outcome: bad row "
                "recall_id=%s position_id=%s: %s",
                row.get("id"), position_id, exc,
            )
            continue

    if updated:
        logger.info(
            "recall_log: matched %d recall row(s) for position_id=%s outcome=%r symbol=%r",
            updated, position_id, position_outcome, position_symbol,
        )
    return updated
