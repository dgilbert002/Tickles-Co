"""
shared.dashboard — Phase 36 Owner Dashboard + Telegram OTP + Mobile.

Three layers:

* ``protocol``, ``store``, ``memory_pool`` — the persistence
  contract for users / OTPs / sessions.
* ``auth``, ``telegram`` — the OTP + session-token orchestration
  and a pluggable Telegram transport.
* ``snapshot``, ``providers``, ``server`` — the read-only
  mobile-friendly dashboard API.
"""
from __future__ import annotations

from pathlib import Path

MIGRATION_PATH = (
    Path(__file__).parent / "migrations"
    / "2026_04_19_phase36_dashboard.sql"
)
WEB_DIR = Path(__file__).parent / "web"


def read_migration_sql() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


from shared.dashboard.auth import (  # noqa: E402
    AuthConfig,
    AuthError,
    DashboardAuth,
    DisabledUser,
    InvalidOtp,
    InvalidSession,
    UnknownChat,
    build_auth_from_pool,
)
from shared.dashboard.memory_pool import (  # noqa: E402
    InMemoryDashboardPool,
)
from shared.dashboard.protocol import (  # noqa: E402
    DashboardOtp,
    DashboardSession,
    DashboardSnapshot,
    DashboardUser,
    OtpIssueResult,
    ROLE_OWNER,
    ROLE_VIEWER,
    SessionIssueResult,
)
from shared.dashboard.providers import (  # noqa: E402
    IntentsSqlProvider,
    RegistryServicesProvider,
    SubmissionsStoreProvider,
)
from shared.dashboard.snapshot import (  # noqa: E402
    SnapshotBuilder,
    SnapshotProviders,
    snapshot_to_dict,
)
from shared.dashboard.store import (  # noqa: E402
    DashboardOtpStore,
    DashboardSessionStore,
    DashboardUserStore,
    hash_secret,
    now_utc,
)
from shared.dashboard.telegram import (  # noqa: E402
    NullTelegramSender,
    TelegramBotSender,
    TelegramSendError,
    TelegramSender,
    sender_from_env,
)


__all__ = [
    "AuthConfig",
    "AuthError",
    "DashboardAuth",
    "DashboardOtp",
    "DashboardOtpStore",
    "DashboardSession",
    "DashboardSessionStore",
    "DashboardSnapshot",
    "DashboardUser",
    "DashboardUserStore",
    "DisabledUser",
    "InMemoryDashboardPool",
    "IntentsSqlProvider",
    "InvalidOtp",
    "InvalidSession",
    "MIGRATION_PATH",
    "NullTelegramSender",
    "OtpIssueResult",
    "ROLE_OWNER",
    "ROLE_VIEWER",
    "RegistryServicesProvider",
    "SessionIssueResult",
    "SnapshotBuilder",
    "SnapshotProviders",
    "SubmissionsStoreProvider",
    "TelegramBotSender",
    "TelegramSendError",
    "TelegramSender",
    "UnknownChat",
    "WEB_DIR",
    "build_auth_from_pool",
    "hash_secret",
    "now_utc",
    "read_migration_sql",
    "sender_from_env",
    "snapshot_to_dict",
]
