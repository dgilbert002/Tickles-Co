"""
shared.dashboard.auth — OTP + session orchestration for Phase 36.

The contract is tight:

* ``issue_otp(chat_id)`` validates the chat is allowlisted and
  enabled, generates a 6-digit numeric code using
  :func:`secrets.randbelow`, hashes it for storage, delivers the raw
  code through a :class:`TelegramSender`, and returns the raw code +
  expiry **once**.
* ``verify_otp(chat_id, code)`` locates a matching un-consumed row,
  consumes it, and issues a fresh session token.
* ``authenticate_token(token)`` resolves a raw session token to an
  active :class:`DashboardSession` (+ user), touching
  ``last_seen_at`` so operators can see idle sessions.

Every failure path is explicit with :class:`AuthError` subclasses so
the HTTP layer can map them cleanly to 401/403/404 responses.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional, Tuple

from shared.dashboard.protocol import (
    DashboardOtp,
    DashboardSession,
    DashboardUser,
    OtpIssueResult,
    SessionIssueResult,
)
from shared.dashboard.store import (
    DashboardOtpStore,
    DashboardSessionStore,
    DashboardUserStore,
    hash_secret,
    now_utc,
)
from shared.dashboard.telegram import (
    NullTelegramSender,
    TelegramSendError,
    TelegramSender,
)

LOG = logging.getLogger("tickles.dashboard.auth")

OTP_LENGTH = 6
OTP_TTL_S = 5 * 60
SESSION_TTL_S = 12 * 3600
SESSION_TOKEN_BYTES = 32


class AuthError(RuntimeError):
    http_status = 401


class UnknownChat(AuthError):
    http_status = 404


class DisabledUser(AuthError):
    http_status = 403


class OtpDeliveryFailed(AuthError):
    http_status = 502


class InvalidOtp(AuthError):
    http_status = 401


class InvalidSession(AuthError):
    http_status = 401


@dataclass
class AuthConfig:
    otp_ttl_s: int = OTP_TTL_S
    session_ttl_s: int = SESSION_TTL_S
    otp_length: int = OTP_LENGTH


class DashboardAuth:
    def __init__(
        self,
        users: DashboardUserStore,
        otps: DashboardOtpStore,
        sessions: DashboardSessionStore,
        *,
        sender: Optional[TelegramSender] = None,
        config: Optional[AuthConfig] = None,
        now_fn=now_utc,
    ) -> None:
        self._users = users
        self._otps = otps
        self._sessions = sessions
        self._sender = sender or NullTelegramSender()
        self._cfg = config or AuthConfig()
        self._now = now_fn

    @property
    def config(self) -> AuthConfig:
        return self._cfg

    @property
    def sender(self) -> TelegramSender:
        return self._sender

    async def issue_otp(
        self,
        chat_id: str,
        *,
        client_ip: Optional[str] = None,
    ) -> OtpIssueResult:
        user = await self._users.get(chat_id)
        if user is None:
            raise UnknownChat(f"chat_id {chat_id} not enrolled")
        if not user.can_authenticate():
            raise DisabledUser(f"chat_id {chat_id} is disabled")

        code = self._generate_code()
        code_hash = hash_secret(code)
        expires_at = self._now() + timedelta(seconds=self._cfg.otp_ttl_s)
        await self._otps.create(DashboardOtp(
            id=None, chat_id=chat_id, code_hash=code_hash,
            expires_at=expires_at, client_ip=client_ip,
        ))

        delivery_ok = True
        delivery_error: Optional[str] = None
        try:
            await self._sender.send_code(
                chat_id=chat_id, code=code,
                ttl_s=self._cfg.otp_ttl_s,
            )
        except TelegramSendError as e:
            delivery_ok = False
            delivery_error = str(e)
            LOG.warning("OTP delivery failed for %s: %s", chat_id, e)

        return OtpIssueResult(
            chat_id=chat_id, code=code, expires_at=expires_at,
            delivery_channel="telegram",
            delivery_ok=delivery_ok, delivery_error=delivery_error,
        )

    async def verify_otp(
        self,
        chat_id: str,
        code: str,
        *,
        user_agent: Optional[str] = None,
        client_ip: Optional[str] = None,
    ) -> SessionIssueResult:
        user = await self._users.get(chat_id)
        if user is None:
            raise UnknownChat(f"chat_id {chat_id} not enrolled")
        if not user.can_authenticate():
            raise DisabledUser(f"chat_id {chat_id} is disabled")

        code_hash = hash_secret(code.strip())
        otp = await self._otps.find_active(chat_id, code_hash)
        if otp is None or otp.id is None:
            raise InvalidOtp("OTP is invalid, consumed, or expired")
        await self._otps.consume(otp.id)

        token = self._generate_token()
        token_hash = hash_secret(token)
        expires = self._now() + timedelta(seconds=self._cfg.session_ttl_s)
        session_id = await self._sessions.create(DashboardSession(
            id=None, chat_id=chat_id, token_hash=token_hash,
            expires_at=expires, user_agent=user_agent, client_ip=client_ip,
        ))
        await self._users.touch_login(chat_id)

        return SessionIssueResult(
            chat_id=chat_id, token=token, expires_at=expires,
            session_id=session_id,
        )

    async def authenticate_token(
        self, token: str,
    ) -> Tuple[DashboardUser, DashboardSession]:
        token_hash = hash_secret(token)
        session = await self._sessions.find_active(token_hash)
        if session is None or session.id is None:
            raise InvalidSession("session token unknown or expired")
        if not session.is_active(self._now()):
            raise InvalidSession("session is not active")
        user = await self._users.get(session.chat_id)
        if user is None or not user.can_authenticate():
            raise InvalidSession("user disabled or deleted")
        await self._sessions.touch(session.id)
        return user, session

    async def revoke(self, session_id: int) -> None:
        await self._sessions.revoke(session_id)

    async def revoke_all_for(self, chat_id: str) -> int:
        return await self._sessions.revoke_all_for(chat_id)

    def _generate_code(self) -> str:
        top = 10 ** self._cfg.otp_length
        return f"{secrets.randbelow(top):0{self._cfg.otp_length}d}"

    def _generate_token(self) -> str:
        return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def build_auth_from_pool(
    pool, *,
    sender: Optional[TelegramSender] = None,
    config: Optional[AuthConfig] = None,
    now_fn=now_utc,
) -> DashboardAuth:
    return DashboardAuth(
        users=DashboardUserStore(pool),
        otps=DashboardOtpStore(pool),
        sessions=DashboardSessionStore(pool),
        sender=sender, config=config, now_fn=now_fn,
    )


__all__ = [
    "AuthConfig",
    "AuthError",
    "DashboardAuth",
    "DisabledUser",
    "InvalidOtp",
    "InvalidSession",
    "OtpDeliveryFailed",
    "OTP_LENGTH",
    "OTP_TTL_S",
    "SESSION_TOKEN_BYTES",
    "SESSION_TTL_S",
    "UnknownChat",
    "build_auth_from_pool",
]
