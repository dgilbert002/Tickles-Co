"""
Module: telegram_auth_setup
Purpose: One-time interactive authentication for Telegram collector
Location: /opt/tickles/shared/utils/telegram_auth_setup.py

Run this script to authenticate Telegram and create the .session file.
After first auth, the session persists and no human interaction is needed.
"""

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from telethon import TelegramClient

logger = logging.getLogger("tickles.telegram_auth")

load_dotenv()

API_ID = int(os.environ.get("TELEGRAM_API_ID", "0"))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
PHONE = os.environ.get("TELEGRAM_PHONE", "")
SESSION_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "data", "telegram_sessions", "tickles_telegram"
)


async def main():
    """Run interactive Telegram authentication."""
    if not all([API_ID, API_HASH, PHONE]):
        logger.error("Missing Telegram credentials in .env")
        logger.error("Need: TELEGRAM_API_ID, TELEGRAM_API_HASH, TELELEGRAM_PHONE")
        return

    os.makedirs(os.path.dirname(SESSION_PATH), exist_ok=True)

    client = TelegramClient(
        SESSION_PATH,
        API_ID,
        API_HASH,
    )

    await client.connect()

    if await client.is_user_authorized():
        logger.info("Already authenticated! Session file is valid.")
        await client.disconnect()
        return

    logger.info("Sending verification code to %s", PHONE)
    await client.send_code_request(PHONE)

    code = input("Enter the verification code you received: ").strip()

    try:
        await client.sign_in(PHONE, code)
        logger.info("Authentication successful! Session saved to %s.session", SESSION_PATH)
    except Exception as e:
        logger.error("Authentication failed: %s", e)
        await client.disconnect()
        return

    me = await client.get_me()
    logger.info("Logged in as: %s (ID: %s)", me.first_name, me.id)

    # List channels/groups
    dialog_count = 0
    async for dialog in client.iter_dialogs(limit=10):
        if dialog.is_group or dialog.is_channel:
            dialog_count += 1
            logger.info("  - %s (ID: %d, type: %s)", dialog.name, dialog.id, "group" if dialog.is_group else "channel")

    logger.info("Found %d channels/groups total", dialog_count)

    await client.disconnect()
    logger.info("Session saved. Future runs will use the saved session file.")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    asyncio.run(main())
