"""
shared.dashboard.store — async DB wrappers for Phase 36 tables.

Only hashes are persisted. Raw OTPs / tokens never leave the caller
that issued them.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from shared.dashboard.protocol import (
    DashboardOtp,
    DashboardSession,
    DashboardUser,
)


def hash_secret(secret: str) -> str:
    """SHA256 hex digest used for OTP codes and session tokens."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class DashboardUserStore:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def upsert(self, user: DashboardUser) -> int:
        sql = (
            "INSERT INTO public.dashboard_users "
            "(chat_id, display_name, role, enabled) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (chat_id) DO UPDATE SET "
            "  display_name = EXCLUDED.display_name, "
            "  role = EXCLUDED.role, "
            "  enabled = EXCLUDED.enabled "
            "RETURNING id"
        )
        row = await self._pool.fetch_one(
            sql,
            (user.chat_id, user.display_name, user.role, user.enabled),
        )
        return int(row["id"]) if row else 0

    async def get(self, chat_id: str) -> Optional[DashboardUser]:
        sql = "SELECT * FROM public.dashboard_users WHERE chat_id = $1"
        row = await self._pool.fetch_one(sql, (chat_id,))
        return _user_row(row) if row else None

    async def touch_login(self, chat_id: str) -> None:
        sql = (
            "UPDATE public.dashboard_users "
            "SET last_login_at = NOW() WHERE chat_id = $1"
        )
        await self._pool.execute(sql, (chat_id,))

    async def list(self, *, enabled_only: bool = False) -> List[DashboardUser]:
        if enabled_only:
            sql = (
                "SELECT * FROM public.dashboard_users "
                "WHERE enabled = TRUE ORDER BY chat_id"
            )
        else:
            sql = (
                "SELECT * FROM public.dashboard_users ORDER BY chat_id"
            )
        rows = await self._pool.fetch_all(sql, ())
        return [_user_row(r) for r in rows]

    async def set_enabled(self, chat_id: str, enabled: bool) -> None:
        sql = (
            "UPDATE public.dashboard_users SET enabled = $2 "
            "WHERE chat_id = $1"
        )
        await self._pool.execute(sql, (chat_id, enabled))


class DashboardOtpStore:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def create(self, otp: DashboardOtp) -> int:
        sql = (
            "INSERT INTO public.dashboard_otps "
            "(chat_id, code_hash, expires_at, client_ip) "
            "VALUES ($1, $2, $3, $4) RETURNING id"
        )
        row = await self._pool.fetch_one(
            sql,
            (otp.chat_id, otp.code_hash, otp.expires_at, otp.client_ip),
        )
        return int(row["id"]) if row else 0

    async def find_active(
        self, chat_id: str, code_hash: str,
    ) -> Optional[DashboardOtp]:
        sql = (
            "SELECT * FROM public.dashboard_otps "
            "WHERE chat_id = $1 AND code_hash = $2 "
            "AND consumed_at IS NULL AND expires_at > NOW() "
            "ORDER BY issued_at DESC LIMIT 1"
        )
        row = await self._pool.fetch_one(sql, (chat_id, code_hash))
        return _otp_row(row) if row else None

    async def consume(self, otp_id: int) -> None:
        sql = (
            "UPDATE public.dashboard_otps "
            "SET consumed_at = NOW(), attempts = attempts + 1 "
            "WHERE id = $1"
        )
        await self._pool.execute(sql, (int(otp_id),))

    async def bump_attempts(self, otp_id: int) -> None:
        sql = (
            "UPDATE public.dashboard_otps "
            "SET attempts = attempts + 1 WHERE id = $1"
        )
        await self._pool.execute(sql, (int(otp_id),))

    async def recent_for(
        self, chat_id: str, *, limit: int = 10,
    ) -> List[DashboardOtp]:
        sql = (
            "SELECT * FROM public.dashboard_otps WHERE chat_id = $1 "
            "ORDER BY issued_at DESC LIMIT $2"
        )
        rows = await self._pool.fetch_all(sql, (chat_id, int(limit)))
        return [_otp_row(r) for r in rows]


class DashboardSessionStore:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def create(self, session: DashboardSession) -> int:
        sql = (
            "INSERT INTO public.dashboard_sessions "
            "(chat_id, token_hash, expires_at, user_agent, client_ip) "
            "VALUES ($1,$2,$3,$4,$5) RETURNING id"
        )
        row = await self._pool.fetch_one(
            sql,
            (
                session.chat_id, session.token_hash, session.expires_at,
                session.user_agent, session.client_ip,
            ),
        )
        return int(row["id"]) if row else 0

    async def find_active(
        self, token_hash: str,
    ) -> Optional[DashboardSession]:
        sql = (
            "SELECT * FROM public.dashboard_sessions_active "
            "WHERE token_hash = $1 LIMIT 1"
        )
        row = await self._pool.fetch_one(sql, (token_hash,))
        return _session_row(row) if row else None

    async def touch(self, session_id: int) -> None:
        sql = (
            "UPDATE public.dashboard_sessions "
            "SET last_seen_at = NOW() WHERE id = $1"
        )
        await self._pool.execute(sql, (int(session_id),))

    async def revoke(self, session_id: int) -> None:
        sql = (
            "UPDATE public.dashboard_sessions "
            "SET revoked_at = NOW() WHERE id = $1"
        )
        await self._pool.execute(sql, (int(session_id),))

    async def revoke_all_for(self, chat_id: str) -> int:
        sql = (
            "UPDATE public.dashboard_sessions "
            "SET revoked_at = NOW() "
            "WHERE chat_id = $1 AND revoked_at IS NULL"
        )
        return await self._pool.execute(sql, (chat_id,))

    async def list_active(
        self, *, chat_id: Optional[str] = None, limit: int = 50,
    ) -> List[DashboardSession]:
        if chat_id is not None:
            sql = (
                "SELECT * FROM public.dashboard_sessions_active "
                "WHERE chat_id = $1 "
                "ORDER BY issued_at DESC LIMIT $2"
            )
            params: Sequence[Any] = (chat_id, int(limit))
        else:
            sql = (
                "SELECT * FROM public.dashboard_sessions_active "
                "ORDER BY issued_at DESC LIMIT $1"
            )
            params = (int(limit),)
        rows = await self._pool.fetch_all(sql, params)
        return [_session_row(r) for r in rows]


# --------------------------------------------------------------- helpers


def _maybe_dt(val: Any) -> Optional[datetime]:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    try:
        return datetime.fromisoformat(str(val))
    except Exception:
        return None


def _user_row(row: Dict[str, Any]) -> DashboardUser:
    return DashboardUser(
        id=int(row["id"]),
        chat_id=str(row["chat_id"]),
        display_name=row.get("display_name"),
        role=row.get("role") or "owner",
        enabled=bool(row.get("enabled", True)),
        created_at=_maybe_dt(row.get("created_at")),
        last_login_at=_maybe_dt(row.get("last_login_at")),
    )


def _otp_row(row: Dict[str, Any]) -> DashboardOtp:
    return DashboardOtp(
        id=int(row["id"]),
        chat_id=str(row["chat_id"]),
        code_hash=str(row["code_hash"]),
        issued_at=_maybe_dt(row.get("issued_at")),
        expires_at=_maybe_dt(row.get("expires_at")),
        consumed_at=_maybe_dt(row.get("consumed_at")),
        attempts=int(row.get("attempts") or 0),
        client_ip=row.get("client_ip"),
    )


def _session_row(row: Dict[str, Any]) -> DashboardSession:
    return DashboardSession(
        id=int(row["id"]),
        chat_id=str(row["chat_id"]),
        token_hash=str(row["token_hash"]),
        issued_at=_maybe_dt(row.get("issued_at")),
        expires_at=_maybe_dt(row.get("expires_at")),
        revoked_at=_maybe_dt(row.get("revoked_at")),
        last_seen_at=_maybe_dt(row.get("last_seen_at")),
        user_agent=row.get("user_agent"),
        client_ip=row.get("client_ip"),
    )


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "DashboardOtpStore",
    "DashboardSessionStore",
    "DashboardUserStore",
    "hash_secret",
    "now_utc",
]
