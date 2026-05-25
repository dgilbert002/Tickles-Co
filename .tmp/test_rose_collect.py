"""Test: run Telegram collector for one cycle against Rose, write to news_items."""
import asyncio, os, sys, logging
sys.path.insert(0, '/opt/tickles')
from dotenv import load_dotenv
load_dotenv('/opt/tickles/.env')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

async def main():
    from shared.collectors.telegram.telegram_collector import TelegramCollector
    
    collector = TelegramCollector()
    print(f'Loaded {len(collector._channels)} channels:')
    for ch in collector._channels:
        print(f'  {ch["name"]} (id={ch["id"]}, media={ch.get("download_media")})')
    
    print('\nRunning one collect cycle...')
    items = await collector.collect()
    
    print(f'\nCollected {len(items)} items:')
    for item in items[:10]:
        print(f'  [{item.published_at}] {item.author}: {item.headline[:100]}')
        if item.media_path:
            print(f'    media: {item.media_path}')
    
    await collector.close()

asyncio.run(main())