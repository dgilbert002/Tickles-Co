"""
shared.dashboard.telegram — tiny OTP-delivery transport.

Two implementations:

* :class:`NullTelegramSender` — writes the OTP to a local log file.
  Used in tests / offline dev. **Never** deployed on the VPS.
* :class:`TelegramBotSender` — posts to the Telegram Bot API using
  ``https://api.telegram.org/bot{token}/sendMessage``. No third-
  party SDK needed; plain :mod:`urllib` keeps the VPS footprint
  small.

Both implementations implement the same protocol:

    async def send_code(chat_id: str, code: str, *, ttl_s: int) -> None

Raises :class:`TelegramSendError` on any non-2xx response.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional, Protocol

LOG = logging.getLogger("tickles.dashboard.telegram")


class TelegramSendError(RuntimeError):
    pass


class TelegramSender(Protocol):
    async def send_code(
        self, chat_id: str, code: str, *, ttl_s: int,
    ) -> None: ...


class NullTelegramSender:
    """Offline sender. Appends delivered codes to a local log file."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = Path(
            path or os.environ.get(
                "TICKLES_OTP_LOG", "./_otp_log.jsonl",
            )
        )
        self.deliveries: list = []

    async def send_code(
        self, chat_id: str, code: str, *, ttl_s: int,
    ) -> None:
        self.deliveries.append({
            "chat_id": chat_id, "code": code, "ttl_s": ttl_s,
        })
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(self.deliveries[-1]) + "\n")
        except OSError as e:  # pragma: no cover - best-effort
            LOG.warning("NullTelegramSender log write failed: %s", e)


class TelegramBotSender:
    """HTTPS sender using the Telegram Bot API."""

    API_BASE = "https://api.telegram.org"

    def __init__(self, bot_token: str, *, timeout_s: float = 10.0) -> None:
        if not bot_token:
            raise ValueError("bot_token is required")
        self._token = bot_token
        self._timeout = float(timeout_s)

    async def send_code(
        self, chat_id: str, code: str, *, ttl_s: int,
    ) -> None:
        text = (
            "Tickles dashboard login code: "
            f"<b>{code}</b>\n"
            f"Valid for {ttl_s // 60} minutes. "
            "Do not share this code."
        )
        data = urllib.parse.urlencode({
            "chat_id": chat_id, "text": text, "parse_mode": "HTML",
        }).encode("utf-8")
        url = f"{self.API_BASE}/bot{self._token}/sendMessage"
        req = urllib.request.Request(url, data=data, method="POST")
        try:
            with urllib.request.urlopen(  # nosec B310
                req, timeout=self._timeout,
            ) as resp:
                body = resp.read().decode("utf-8", "replace")
                payload = json.loads(body)
                if not payload.get("ok"):
                    raise TelegramSendError(
                        f"Telegram API rejected: "
                        f"{payload.get('description')}"
                    )
        except urllib.error.URLError as e:  # pragma: no cover - network
            raise TelegramSendError(
                f"Telegram API URLError: {e}"
            ) from e


def sender_from_env() -> TelegramSender:
    token = os.environ.get("TICKLES_TELEGRAM_BOT_TOKEN")
    if token:
        return TelegramBotSender(token)
    return NullTelegramSender()


__all__ = [
    "NullTelegramSender",
    "TelegramBotSender",
    "TelegramSender",
    "TelegramSendError",
    "sender_from_env",
]
