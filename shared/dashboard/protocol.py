"""
shared.dashboard.protocol — dataclasses + constants for Phase 36.

We deliberately store *hashes* of both OTP codes and session tokens.
The raw secret only ever appears in the response to the operator
(delivered to their Telegram chat); the database only ever sees the
SHA256 hex.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


ROLE_OWNER = "owner"
ROLE_VIEWER = "viewer"
ROLES = (ROLE_OWNER, ROLE_VIEWER)


@dataclass
class DashboardUser:
    id: Optional[int]
    chat_id: str
    display_name: Optional[str] = None
    role: str = ROLE_OWNER
    enabled: bool = True
    created_at: Optional[datetime] = None
    last_login_at: Optional[datetime] = None

    def can_authenticate(self) -> bool:
        return self.enabled


@dataclass
class DashboardOtp:
    id: Optional[int]
    chat_id: str
    code_hash: str
    issued_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    consumed_at: Optional[datetime] = None
    attempts: int = 0
    client_ip: Optional[str] = None

    @property
    def is_consumed(self) -> bool:
        return self.consumed_at is not None

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at is not None and self.expires_at <= now


@dataclass
class DashboardSession:
    id: Optional[int]
    chat_id: str
    token_hash: str
    issued_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    user_agent: Optional[str] = None
    client_ip: Optional[str] = None

    def is_active(self, now: datetime) -> bool:
        if self.revoked_at is not None:
            return False
        if self.expires_at is None:
            return False
        return self.expires_at > now


@dataclass
class OtpIssueResult:
    chat_id: str
    code: str                     # raw 6-digit code - display once
    expires_at: datetime
    delivery_channel: str = "telegram"
    delivery_ok: bool = True
    delivery_error: Optional[str] = None


@dataclass
class SessionIssueResult:
    chat_id: str
    token: str                    # raw session token - display once
    expires_at: datetime
    session_id: Optional[int] = None


@dataclass
class DashboardSnapshot:
    """What the dashboard API returns - a JSON-ready summary."""

    generated_at: datetime
    # Overview stats
    signals_today_count: int = 0
    signals_today_pct_change: float = 0.0
    api_cost_today_usd: float = 0.0
    api_cost_monthly_pct: float = 0.0
    open_positions_count: int = 0
    open_positions_unrealized_pnl: float = 0.0
    open_positions_trader_count: int = 0
    open_positions_agent_count: int = 0
    services_healthy_count: int = 0
    services_total_count: int = 0
    top_actor_name: Optional[str] = None
    top_actor_score: float = 0.0
    ingest_depth: int = 0

    # Tab data
    services: list = field(default_factory=list)
    submissions_active: int = 0
    submissions_recent: list = field(default_factory=list)
    latest_intents: list = field(default_factory=list)
    regime_current: Optional[dict] = None
    guardrails_active: list = field(default_factory=list)
    positions: list = field(default_factory=list)
    leaderboard: list = field(default_factory=list)
    signals: list = field(default_factory=list)
    interpretations: list = field(default_factory=list)
    queue: list = field(default_factory=list)
    
    notes: list = field(default_factory=list)


__all__ = [
    "DashboardOtp",
    "DashboardSession",
    "DashboardSnapshot",
    "DashboardUser",
    "OtpIssueResult",
    "ROLES",
    "ROLE_OWNER",
    "ROLE_VIEWER",
    "SessionIssueResult",
]
