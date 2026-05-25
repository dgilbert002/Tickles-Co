"""Quick pipeline snapshot"""
import asyncio, asyncpg, os
from datetime import datetime
from dotenv import load_dotenv
load_dotenv('/opt/tickles/.env')

async def snap():
    conn = await asyncpg.connect(host='127.0.0.1', user='admin', 
                                   password=os.environ['DB_PASSWORD'], 
                                   database='tickles_shared')
    now = datetime.now().strftime('%H:%M:%S')
    w = await conn.fetchval("SELECT COUNT(*) FROM media_items WHERE processing_status = 'downloaded' AND created_at::date = '2026-05-25'")
    a = await conn.fetchval("SELECT COUNT(*) FROM media_items WHERE processing_status = 'analyzing' AND created_at::date = '2026-05-25'")
    d = await conn.fetchval("SELECT COUNT(*) FROM media_items WHERE processing_status = 'analyzed' AND created_at::date = '2026-05-25'")
    f = await conn.fetchval("SELECT COUNT(*) FROM media_items WHERE processing_status = 'failed' AND created_at::date = '2026-05-25'")
    s = await conn.fetchval("SELECT COUNT(*) FROM signal_interpretations WHERE created_at::date = '2026-05-25'")
    o = await conn.fetchval("SELECT COUNT(*) FROM tracked_positions WHERE status = 'open'")
    p = await conn.fetchval("SELECT COUNT(*) FROM tracked_positions WHERE status = 'pending'")
    c = await conn.fetchval("SELECT COUNT(*) FROM tracked_positions WHERE closed_at::date = '2026-05-25'")
    
    latest = await conn.fetch("""
        SELECT tp.instrument_symbol, tp.direction, tp.entry_price::numeric(12,4), 
               tp.status, ni.author, tp.created_at::timestamptz(0) as ts
        FROM tracked_positions tp
        JOIN news_items ni ON ni.id = tp.news_item_id
        WHERE tp.created_at > now() - interval '2 minutes'
        ORDER BY tp.created_at DESC LIMIT 8
    """)
    await conn.close()
    
    print(f"[{now}] queue:{w} analyzing:{a} done:{d} failed:{f} | sigs:{s} open:{o} pending:{p} closed:{c}")
    if latest:
        for r in latest:
            print(f"  NEW: {r['instrument_symbol']} {r['direction']} @{r['entry_price']} [{r['status']}] by {r['author']}")
    else:
        print("  (no new positions in last 2min)")

asyncio.run(snap())