"""
Module: broadcast_payload
Purpose: TypedDict contract for MemU outbox broadcasts between publisher and listener.
Location: /opt/tickles/shared/memu/broadcast_payload.py
"""

from typing import Literal, NotRequired, TypedDict

from shared.memu.insight_kinds import InsightKind


class BroadcastPayload(TypedDict):
    """Typed contract for the pg_notify('memu_broadcast', json) payload.

    Both publisher (interpretation_service.broadcast_insight) and listener
    (memu.listener_service) import this type so schema drift is caught by
    mypy / type-checking rather than silently at runtime.

    The ``insight_kind`` field uses the canonical InsightKind alias defined
    in shared.memu.insight_kinds, which is the single source of truth shared
    with the MCP tool schemas and the memu.insights CHECK constraint
    (Phase Y.0 enum reconciliation).
    """

    schema_version: Literal[1]
    company: str  # e.g. 'rubicon', 'jarvais'
    actor_type: Literal["trader", "agent", "copy_bot", "self", "manual"]
    actor_id: str
    insight_kind: InsightKind
    summary: str
    body_md: str  # markdown lessons body
    instrument_symbol_normalised: NotRequired[str]
    instrument_exchange: NotRequired[str]
    position_id: NotRequired[int]
    correlation_id: str  # propagated from interpretation_service
    created_at_iso: str  # ISO-8601 UTC
