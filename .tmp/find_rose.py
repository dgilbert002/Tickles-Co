"""Test: find Rose Telegram channel and list recent messages."""
import asyncio, os, sys
sys.path.insert(0, '/opt/tickles')
from dotenv import load_dotenv
load_dotenv('/opt/tickles/.env')

async def main():
    from telethon import TelegramClient
    api_id = int(os.environ['TELEGRAM_API_ID'])
    api_hash = os.environ['TELEGRAM_API_HASH']
    session_path = '/opt/tickles/shared/collectors/telegram/data/tickles_telegram'
    
    client = TelegramClient(session_path, api_id, api_hash)
    await client.start()
    print('Connected as:', (await client.get_me()).username)
    
    # Search for "Rose" in dialogs
    print('\n--- Searching dialogs for "Rose" ---')
    async for dialog in client.iter_dialogs():
        name = dialog.name or ''
        if 'rose' in name.lower() or '⚡' in name:
            print(f'  {dialog.name} | id={dialog.id} | type={type(dialog.entity).__name__}')
            # Show last 5 messages
            print(f'  Last 5 messages:')
            async for msg in client.iter_messages(dialog.id, limit=5):
                has_media = bool(msg.photo or msg.document)
                text = (msg.text or '')[:100]
                print(f'    [{msg.date}] {text} {"📷" if has_media else ""}')
            print()
    
    await client.disconnect()

asyncio.run(main())
