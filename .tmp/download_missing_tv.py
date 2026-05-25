"""Download missing TradingView images and link them to media_items."""
import asyncio, asyncpg, os, sys, urllib.request
from pathlib import Path
from dotenv import load_dotenv
load_dotenv('/opt/tickles/.env')

MEDIA_DIR = Path('/opt/tickles/shared/collectors/discord/data/discord_media')

async def main():
    conn = await asyncpg.connect(
        host='127.0.0.1', user='admin', password=os.environ['DB_PASSWORD'],
        database='tickles_shared'
    )
    
    # Find all downloaded media with no local_path but with TradingView source_url
    rows = await conn.fetch("""
        SELECT mi.id, mi.source_url, mi.news_item_id,
               ni.collected_at::timestamptz(0)
        FROM media_items mi
        JOIN news_items ni ON ni.id = mi.news_item_id
        WHERE mi.processing_status = 'downloaded'
          AND mi.local_path IS NULL
          AND mi.source_url LIKE '%tradingview.com%'
        ORDER BY mi.id
    """)
    
    print(f"Found {len(rows)} TradingView media items without local files")
    
    downloaded = 0
    for r in rows:
        url = r['source_url']
        media_id = r['id']
        
        # Extract filename from URL
        fname = url.split('/')[-1]
        save_dir = MEDIA_DIR / 'tradingview'
        save_dir.mkdir(parents=True, exist_ok=True)
        save_path = save_dir / fname
        
        try:
            urllib.request.urlretrieve(url, str(save_path))
            file_size = save_path.stat().st_size
            if file_size < 1000:
                save_path.unlink()
                print(f"  [{media_id}] too small ({file_size}B), skipping")
                continue
            
            await conn.execute("""
                UPDATE media_items 
                SET local_path = $1, 
                    file_size_bytes = $2,
                    mime_type = 'image/png',
                    processing_status = 'downloaded'
                WHERE id = $3
            """, str(save_path), file_size, media_id)
            downloaded += 1
            print(f"  [{media_id}] downloaded {file_size}B → {save_path}")
        except Exception as e:
            print(f"  [{media_id}] FAILED: {e}")
    
    print(f"\nDownloaded {downloaded}/{len(rows)}")
    await conn.close()

asyncio.run(main())