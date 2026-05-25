"""Monitor the interpretation pipeline every 60 seconds."""
import asyncio, asyncpg, os, sys
from datetime import datetime
from dotenv import load_dotenv
load_dotenv('/opt/tickles/.env')

async def main():
    conn = await asyncpg.connect(host='127.0.0.1', user='admin', password=os.environ['DB_PASSWORD'], database='tickles_shared')
    prev_sigs = await conn.fetchval("SELECT COUNT(*) FROM signal_interpretations WHERE created_at::date = '2026-05-25'")
    prev_open = await conn.fetchval("SELECT COUNT(*) FROM tracked_positions WHERE status = 'open'")
    prev_closed = await conn.fetchval("SELECT COUNT(*) FROM tracked_positions WHERE closed_at::date = '2026-05-25'")
    
    print(f"Starting: {prev_sigs} sigs, {prev_open} open, {prev_closed} closed", flush=True)
    
    while True:
        await asyncio.sleep(60)
        now = datetime.now().strftime('%H:%M:%S')
        
        waiting = await conn.fetchval("SELECT COUNT(*) FROM media_items WHERE processing_status IN ('downloaded','pending') AND created_at::date = '2026-05-25'")
        analyzing = await conn.fetchval("SELECT COUNT(*) FROM media_items WHERE processing_status = 'analyzing' AND created_at::date = '2026-05-25'")
        done = await conn.fetchval("SELECT COUNT(*) FROM media_items WHERE processing_status = 'analyzed' AND created_at::date = '2026-05-25'")
        failed = await conn.fetchval("SELECT COUNT(*) FROM media_items WHERE processing_status = 'failed' AND created_at::date = '2026-05-25'")
        sigs = await conn.fetchval("SELECT COUNT(*) FROM signal_interpretations WHERE created_at::date = '2026-05-25'")
        open_pos = await conn.fetchval("SELECT COUNT(*) FROM tracked_positions WHERE status = 'open'")
        pending = await conn.fetchval("SELECT COUNT(*) FROM tracked_positions WHERE status = 'pending'")
        closed = await conn.fetchval("SELECT COUNT(*) FROM tracked_positions WHERE closed_at::date = '2026-05-25'")
        
        new_sigs = sigs - prev_sigs
        new_open = open_pos - prev_open
        new_closed = closed - prev_closed
        
        parts = [f"[{now}]"]
        parts.append(f"queue:{waiting}")
        if analyzing: parts.append(f"analyzing:{analyzing}")
        parts.append(f"done:{done}")
        if failed: parts.append(f"FAILED:{failed}")
        
        changes = []
        if new_sigs: changes.append(f"+{new_sigs} sigs")
        if new_open: changes.append(f"+{new_open} open")
        if new_closed: changes.append(f"+{new_closed} closed")
        
        if changes:
            parts.append(">>> " + " ".join(changes))
        else:
            parts.append("(no change)")
        
        if pending: parts.append(f"pending:{pending}")
        parts.append(f"open:{open_pos}")
        
        print(" ".join(parts), flush=True)
        
        prev_sigs = sigs
        prev_open = open_pos
        prev_closed = closed
        
        if waiting == 0 and analyzing == 0:
            print("[DONE] Queue empty.", flush=True)
            break

asyncio.run(main())