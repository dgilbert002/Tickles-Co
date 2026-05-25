"""Test: read Rose channel messages and download sample media."""
import asyncio, os, sys
sys.path.insert(0, '/opt/tickles')
from dotenv import load_dotenv
load_dotenv('/opt/tickles/.env')

ROSE_ID = -1001755624949

async def main():
    from telethon import TelegramClient
    api_id = int(os.environ['TELEGRAM_API_ID'])
    api_hash = os.environ['TELEGRAM_API_HASH']
    session_path = '/opt/tickles/shared/collectors/telegram/data/tickles_telegram'
    
    client = TelegramClient(session_path, api_id, api_hash)
    await client.start()
    
    entity = await client.get_entity(ROSE_ID)
    print(f'Channel: {entity.title}')
    print(f'Participants: {getattr(entity, "participants_count", "?")}')
    print()
    
    # Download a sample chart
    os.makedirs('/opt/tickles/.tmp/rose_media', exist_ok=True)
    
    media_count = 0
    async for msg in client.iter_messages(ROSE_ID, limit=30):
        has_media = bool(msg.photo or msg.document)
        text = (msg.text or '')[:150]
        print(f'[{msg.date}] {"📷" if has_media else "  "} {text}')
        
        if has_media and media_count < 3:
            path = f'/opt/tickles/.tmp/rose_media/msg_{msg.id}.jpg'
            await msg.download_media(file=path)
            print(f'  -> saved {path}')
            media_count += 1
    
    await client.disconnect()
    print(f'\nDone — {media_count} sample charts saved to .tmp/rose_media/')

asyncio.run(main())
