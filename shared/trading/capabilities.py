"""
shared.trading.capabilities — Phase 25 capability check + policy store.

Capabilities answer the question: **"is this company (or strategy, or
agent) allowed to make this trade right now?"**

Design rules
------------
* Capabilities are **pure rules** — a :class:`CapabilityChecker` is
  deterministic and does no I/O. Unit-testable with frozen inputs.
* The DB-backed :class:`CapabilityStore` only loads and persists rows;
  it never evaluates them.
* Scopes are hierarchical: ``company`` -> ``venue`` -> ``strategy`` ->
  ``agent``. When multiple capabilities match an intent, the most
  restrictive constraint wins (smallest max, strictest allowlist,
  union of denylists).
* Every decision that touches money goes through
  :meth:`CapabilityChecker.evaluate` — Treasury never side-steps it.

This module is import-light: it never depends on asyncpg or the DB
directly. ``CapabilityStore`` takes the same duck-typed async pool
used by ``ServicesCatalog`` so tests can swap in a fake.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

# ---------------------------------------------------------------------------
# Scope constants
# ---------------------------------------------------------------------------

SCOPE_COMPANY = "company"
SCOPE_STRATEGY = "strategy"
SCOPE_AGENT = "agent"
SCOPE_VENUE = "venue"

VALID_SCOPES = frozenset({SCOPE_COMPANY, SCOPE_STRATEGY, SCOPE_AGENT, SCOPE_VENUE})

VALID_DIRECTIONS = frozenset({"long", "short"})
VALID_ORDER_TYPES = frozenset({"market", "limit", "stop", "stop_limit"})


# ---------------------------------------------------------------------------
# Intent + capability models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TradeIntent:
    """Abstract description of a trade before sizing/OMS.

    Treasury evaluates this against the capability layer. Not yet the
    same object the OMS will pass to the exchange — that lives in
    Phase 26. We keep the surface minimal and frozen (hashable).
    """

    company_id: str
    exchange: str
    symbol: str
    direction: str            # long | short
    strategy_id: Optional[str] = None
    agent_id: Optional[str] = None
    order_type: str = "market"
    requested_notional_usd: Optional[float] = None
    requested_leverage: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None

    def stable_hash(self) -> str:
        """Stable SHA-256 hash of intent — used as idempotency anchor."""
        payload = {
            "company_id": self.company_id,
            "exchange": self.exchange,
            "symbol": self.symbol,
            "direction": self.direction,
            "strategy_id": self.strategy_id or "",
            "agent_id": self.agent_id or "",
            "order_type": self.order_type,
            "requested_notional_usd": self.requested_notional_usd,
            "requested_leverage": self.requested_leverage,
            # metadata excluded deliberately — callers can still stamp it
        }
        blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()


@dataclass
class Capability:
    """Single capability row — one policy rule for one scope."""

    company_id: str
    scope_kind: str = SCOPE_COMPANY
    scope_id: str = "global"
    id: Optional[int] = None
    max_notional_usd: Optional[float] = None
    max_leverage: Optional[int] = None
    max_daily_loss_usd: Optional[float] = None
    max_open_positions: Optional[int] = None
    allow_venues: List[str] = field(default_factory=list)
    deny_venues: List[str] = field(default_factory=list)
    allow_symbols: List[str] = field(default_factory=list)
    deny_symbols: List[str] = field(default_factory=list)
    allow_directions: List[str] = field(default_factory=lambda: ["long", "short"])
    allow_order_types: List[str] = field(default_factory=lambda: ["market", "limit"])
    active: bool = True
    notes: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.scope_kind not in VALID_SCOPES:
            raise ValueError(f"invalid scope_kind: {self.scope_kind}")
        bad_dirs = [d for d in self.allow_directions if d not in VALID_DIRECTIONS]
        if bad_dirs:
            raise ValueError(f"invalid allow_directions: {bad_dirs}")
        bad_ots = [o for o in self.allow_order_types if o not in VALID_ORDER_TYPES]
        if bad_ots:
            raise ValueError(f"invalid allow_order_types: {bad_ots}")

    def applies_to(self, intent: TradeIntent) -> bool:
        """Does this capability's scope match the given intent?"""
        if not self.active:
            return False
        if self.company_id != intent.company_id:
            return False
        if self.scope_kind == SCOPE_COMPANY:
            return True
        if self.scope_kind == SCOPE_STRATEGY:
            return bool(intent.strategy_id) and self.scope_id == intent.strategy_id
        if self.scope_kind == SCOPE_AGENT:
            return bool(intent.agent_id) and self.scope_id == intent.agent_id
        if self.scope_kind == SCOPE_VENUE:
            return self.scope_id == intent.exchange
        return False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "company_id": self.company_id,
            "scope_kind": self.scope_kind,
            "scope_id": self.scope_id,
            "max_notional_usd": self.max_notional_usd,
            "max_leverage": self.max_leverage,
            "max_daily_loss_usd": self.max_daily_loss_usd,
            "max_open_positions": self.max_open_positions,
            "allow_venues": list(self.allow_venues),
            "deny_venues": list(self.deny_venues),
            "allow_symbols": list(self.allow_symbols),
            "deny_symbols": list(self.deny_symbols),
            "allow_directions": list(self.allow_directions),
            "allow_order_types": list(self.allow_order_types),
            "active": self.active,
            "notes": self.notes,
            "metadata": dict(self.metadata),
            "created_at": (
                self.created_at.isoformat() if self.created_at is not None else None
            ),
            "updated_at": (
                self.updated_at.isoformat() if self.updated_at is not None else None
            ),
        }


# ---------------------------------------------------------------------------
# Evaluation result
# ---------------------------------------------------------------------------


@dataclass
class CapabilityCheck:
    """Result of evaluating a :class:`TradeIntent` against capabilities."""

    approved: bool
    reasons: List[str] = field(default_factory=list)
    matched_capability_ids: List[int] = field(default_factory=list)
    effective_max_notional_usd: Optional[float] = None
    effective_max_leverage: Optional[int] = None
    effective_max_open_positions: Optional[int] = None
    effective_max_daily_loss_usd: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "approved": self.approved,
            "reasons": list(self.reasons),
            "matched_capability_ids": list(self.matched_capability_ids),
            "effective_max_notional_usd": self.effective_max_notional_usd,
            "effective_max_leverage": self.effective_max_leverage,
            "effective_max_open_positions": self.effective_max_open_positions,
            "effective_max_daily_loss_usd": self.effective_max_daily_loss_usd,
        }


# ---------------------------------------------------------------------------
# Pure checker
# ---------------------------------------------------------------------------


def _min_optional(values: Iterable[Optional[float]]) -> Optional[float]:
    finite = [v for v in values if v is not None]
    return min(finite) if finite else None


def _min_optional_int(values: Iterable[Optional[int]]) -> Optional[int]:
    finite = [v for v in values if v is not None]
    return min(finite) if finite else None


class CapabilityChecker:
    """Pure capability evaluator — no I/O, no DB."""

    def __init__(self, capabilities: Sequence[Capability]) -> None:
        self._capabilities: List[Capability] = list(capabilities)

    @property
    def all(self) -> List[Capability]:
        return list(self._capabilities)

    def evaluate(self, intent: TradeIntent) -> CapabilityCheck:
        reasons: List[str] = []
        applicable: List[Capability] = [
            c for c in self._capabilities if c.applies_to(intent)
        ]

        if not applicable:
            # Fail-closed: if no capability says "yes", the trade is denied.
            return CapabilityCheck(
                approved=False,
                reasons=[f"no active capability matches company={intent.company_id} "
                         f"strategy={intent.strategy_id} agent={intent.agent_id}"],
            )

        # Direction / order-type / venue / symbol gates: ALL applicable caps
        # must permit the trade.
        for cap in applicable:
            if intent.direction not in cap.allow_directions:
                reasons.append(
                    f"cap {cap.scope_kind}:{cap.scope_id} forbids direction "
                    f"'{intent.direction}'"
                )
            if intent.order_type not in cap.allow_order_types:
                reasons.append(
                    f"cap {cap.scope_kind}:{cap.scope_id} forbids order_type "
                    f"'{intent.order_type}'"
                )
            if cap.allow_venues and intent.exchange not in cap.allow_venues:
                reasons.append(
                    f"cap {cap.scope_kind}:{cap.scope_id} allowlist excludes "
                    f"venue '{intent.exchange}'"
                )
            if intent.exchange in cap.deny_venues:
                reasons.append(
                    f"cap {cap.scope_kind}:{cap.scope_id} denylist includes "
                    f"venue '{intent.exchange}'"
                )
            if cap.allow_symbols and intent.symbol not in cap.allow_symbols:
                reasons.append(
                    f"cap {cap.scope_kind}:{cap.scope_id} allowlist excludes "
                    f"symbol '{intent.symbol}'"
                )
            if intent.symbol in cap.deny_symbols:
                reasons.append(
                    f"cap {cap.scope_kind}:{cap.scope_id} denylist includes "
                    f"symbol '{intent.symbol}'"
                )

        # Most-restrictive numeric caps across all applicable rules.
        effective_max_notional = _min_optional(
            c.max_notional_usd for c in applicable
        )
        effective_max_leverage = _min_optional_int(
            c.max_leverage for c in applicable
        )
        effective_max_open = _min_optional_int(
            c.max_open_positions for c in applicable
        )
        effective_max_daily_loss = _min_optional(
            c.max_daily_loss_usd for c in applicable
        )

        # Requested notional / leverage validated only if the intent
        # supplied them. Sizer will read these effective caps later.
        if (
            intent.requested_notional_usd is not None
            and effective_max_notional is not None
            and intent.requested_notional_usd > effective_max_notional
        ):
            reasons.append(
                f"requested notional ${intent.requested_notional_usd:.2f} > "
                f"effective cap ${effective_max_notional:.2f}"
            )
        if (
            intent.requested_leverage is not None
            and effective_max_leverage is not None
            and intent.requested_leverage > effective_max_leverage
        ):
            reasons.append(
                f"requested leverage {intent.requested_leverage}x > "
                f"effective cap {effective_max_leverage}x"
            )

        matched_ids = [c.id for c in applicable if c.id is not None]
        return CapabilityCheck(
            approved=len(reasons) == 0,
            reasons=reasons,
            matched_capability_ids=matched_ids,
            effective_max_notional_usd=effective_max_notional,
            effective_max_leverage=effective_max_leverage,
            effective_max_open_positions=effective_max_open,
            effective_max_daily_loss_usd=effective_max_daily_loss,
        )


# ---------------------------------------------------------------------------
# DB store
# ---------------------------------------------------------------------------


_UPSERT_CAPABILITY_SQL = """
INSERT INTO public.capabilities (
    company_id, scope_kind, scope_id,
    max_notional_usd, max_leverage, max_daily_loss_usd, max_open_positions,
    allow_venues, deny_venues, allow_symbols, deny_symbols,
    allow_directions, allow_order_types,
    active, notes, metadata
) VALUES (
    $1, $2, $3, $4, $5, $6, $7,
    $8::text[], $9::text[], $10::text[], $11::text[],
    $12::text[], $13::text[],
    $14, $15, $16::jsonb
)
ON CONFLICT (company_id, scope_kind, scope_id) DO UPDATE SET
    max_notional_usd = EXCLUDED.max_notional_usd,
    max_leverage = EXCLUDED.max_leverage,
    max_daily_loss_usd = EXCLUDED.max_daily_loss_usd,
    max_open_positions = EXCLUDED.max_open_positions,
    allow_venues = EXCLUDED.allow_venues,
    deny_venues = EXCLUDED.deny_venues,
    allow_symbols = EXCLUDED.allow_symbols,
    deny_symbols = EXCLUDED.deny_symbols,
    allow_directions = EXCLUDED.allow_directions,
    allow_order_types = EXCLUDED.allow_order_types,
    active = EXCLUDED.active,
    notes = EXCLUDED.notes,
    metadata = EXCLUDED.metadata,
    updated_at = now()
"""

_SELECT_CAPABILITIES_COMPANY = """
SELECT * FROM public.capabilities
WHERE company_id = $1
ORDER BY scope_kind, scope_id
"""

_SELECT_CAPABILITY_ONE = """
SELECT * FROM public.capabilities
WHERE company_id = $1 AND scope_kind = $2 AND scope_id = $3
"""

_DELETE_CAPABILITY_SQL = """
DELETE FROM public.capabilities
WHERE company_id = $1 AND scope_kind = $2 AND scope_id = $3
"""


def _row_to_capability(row: Dict[str, Any]) -> Capability:
    def _list(v: Any) -> List[str]:
        if v is None:
            return []
        if isinstance(v, list):
            return [str(x) for x in v]
        return [str(v)]

    md = row.get("metadata")
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except json.JSONDecodeError:
            md = {}
    if md is None:
        md = {}

    def _num(v: Any) -> Optional[float]:
        return float(v) if v is not None else None

    def _int(v: Any) -> Optional[int]:
        return int(v) if v is not None else None

    created = row.get("created_at")
    updated = row.get("updated_at")
    if isinstance(created, str):
        created = datetime.fromisoformat(created)
    if isinstance(updated, str):
        updated = datetime.fromisoformat(updated)

    return Capability(
        id=_int(row.get("id")),
        company_id=str(row["company_id"]),
        scope_kind=str(row["scope_kind"]),
        scope_id=str(row["scope_id"]),
        max_notional_usd=_num(row.get("max_notional_usd")),
        max_leverage=_int(row.get("max_leverage")),
        max_daily_loss_usd=_num(row.get("max_daily_loss_usd")),
        max_open_positions=_int(row.get("max_open_positions")),
        allow_venues=_list(row.get("allow_venues")),
        deny_venues=_list(row.get("deny_venues")),
        allow_symbols=_list(row.get("allow_symbols")),
        deny_symbols=_list(row.get("deny_symbols")),
        allow_directions=_list(row.get("allow_directions")) or ["long", "short"],
        allow_order_types=_list(row.get("allow_order_types")) or ["market", "limit"],
        active=bool(row.get("active", True)),
        notes=str(row.get("notes") or ""),
        metadata=dict(md) if isinstance(md, dict) else {},
        created_at=created if isinstance(created, datetime) else None,
        updated_at=updated if isinstance(updated, datetime) else None,
    )


class CapabilityStore:
    """Thin async DB wrapper for capability rows. No evaluation logic."""

    async def upsert(self, pool: Any, cap: Capability) -> int:
        if cap.scope_kind not in VALID_SCOPES:
            raise ValueError(f"invalid scope_kind: {cap.scope_kind}")
        params = (
            cap.company_id,
            cap.scope_kind,
            cap.scope_id,
            cap.max_notional_usd,
            cap.max_leverage,
            cap.max_daily_loss_usd,
            cap.max_open_positions,
            list(cap.allow_venues),
            list(cap.deny_venues),
            list(cap.allow_symbols),
            list(cap.deny_symbols),
            list(cap.allow_directions),
            list(cap.allow_order_types),
            bool(cap.active),
            str(cap.notes or ""),
            json.dumps(cap.metadata or {}, sort_keys=True, default=str),
        )
        await pool.execute(_UPSERT_CAPABILITY_SQL, params)
        return 1

    async def list_for_company(
        self, pool: Any, company_id: str, active_only: bool = False
    ) -> List[Capability]:
        rows = await pool.fetch_all(_SELECT_CAPABILITIES_COMPANY, (company_id,))
        caps = [_row_to_capability(r) for r in rows]
        if active_only:
            caps = [c for c in caps if c.active]
        return caps

    async def get(
        self, pool: Any, company_id: str, scope_kind: str, scope_id: str
    ) -> Optional[Capability]:
        row = await pool.fetch_one(
            _SELECT_CAPABILITY_ONE, (company_id, scope_kind, scope_id)
        )
        return _row_to_capability(row) if row else None

    async def delete(
        self, pool: Any, company_id: str, scope_kind: str, scope_id: str
    ) -> int:
        return await pool.execute(
            _DELETE_CAPABILITY_SQL, (company_id, scope_kind, scope_id)
        )


# ---------------------------------------------------------------------------
# Testing helper
# ---------------------------------------------------------------------------


def default_capability(company_id: str) -> Capability:
    """A safe, permissive default cap used as a template by the CLI."""
    return Capability(
        company_id=company_id,
        scope_kind=SCOPE_COMPANY,
        scope_id="global",
        max_notional_usd=1_000.0,
        max_leverage=5,
        max_daily_loss_usd=100.0,
        max_open_positions=5,
        allow_venues=[],
        deny_venues=[],
        allow_symbols=[],
        deny_symbols=[],
        allow_directions=["long", "short"],
        allow_order_types=["market", "limit"],
        active=True,
        notes="default capability seeded by treasury_cli capabilities seed-default",
        metadata={"seeded": True, "at": datetime.now(timezone.utc).isoformat()},
    )


__all__ = [
    "SCOPE_COMPANY",
    "SCOPE_STRATEGY",
    "SCOPE_AGENT",
    "SCOPE_VENUE",
    "TradeIntent",
    "Capability",
    "CapabilityCheck",
    "CapabilityChecker",
    "CapabilityStore",
    "default_capability",
]
