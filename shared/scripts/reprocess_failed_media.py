#!/usr/bin/env python3
"""
Reprocess failed media items through the interpretation pipeline.
Usage: python3 shared/scripts/reprocess_failed_media.py [--all] [--dry-run] [media_id ...]

Without --all: reprocess only prefilter_version_label failures (15 items).
With --all: reprocess ALL failed media (24 items).
Without args: dry-run by default — just show what would be reprocessed.
"""
import asyncio, asyncpg, logging, os, sys
from datetime import timezone
from dotenv import load_dotenv
load_dotenv('/opt/tickles/.env')

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('reprocess')

async def main():
    dry_run = '--dry-run' in sys.argv or (len(sys.argv) == 1)
    all_failed = '--all' in sys.argv
    specific_ids = [int(a) for a in sys.argv[1:] if a.isdigit()]
    
    pool = await asyncpg.create_pool(
        dsn='postgresql://admin@localhost/tickles_shared',
        password=os.environ['DB_PASSWORD']
    )
    
    async with pool.acquire() as conn:
        # Find failed items
        if specific_ids:
            rows = await conn.fetch("""
                SELECT id, news_item_id, source_url, local_path, processing_error, created_at
                FROM media_items WHERE id = ANY($1) AND processing_status = 'failed'
            """, specific_ids)
        elif all_failed:
            rows = await conn.fetch("""
                SELECT id, news_item_id, source_url, local_path, processing_error, created_at
                FROM media_items WHERE processing_status = 'failed'
                AND created_at >= '2026-05-28'
                ORDER BY id
            """)
        else:
            rows = await conn.fetch("""
                SELECT id, news_item_id, source_url, local_path, processing_error, created_at
                FROM media_items WHERE processing_status = 'failed'
                AND processing_error LIKE '%prefilter_version_label%'
                ORDER BY id
            """)
        
        logger.info(f'Found {len(rows)} failed media items to reprocess')
        
        for r in rows:
            mid = r['id']
            nid = r['news_item_id']
            url = r['source_url']
            path = r['local_path']
            err = (r['processing_error'] or '')[:80]
            
            if dry_run:
                logger.info(f'  [DRY RUN] media_id={mid} news_id={nid} error={err}')
                continue
            
            # Reset to pending so the pipeline picks it up
            # We also need to update news_items.collected_at to be recent
            # so the max_age_hours filter doesn't block it
            await conn.execute("""
                UPDATE news_items 
                SET collected_at = NOW()
                WHERE id = $1
            """, nid)
            
            await conn.execute("""
                UPDATE media_items
                SET processing_status = 'pending',
                    processing_error = NULL,
                    processing_result = NULL,
                    processed_at = NULL
                WHERE id = $1
            """, mid)
            
            logger.info(f'  REPROCESS media_id={mid} news_id={nid} symbol_from_url={url[-20:]}')
        
        if not dry_run:
            logger.info(f'Reset {len(rows)} items to pending. InterpretationService will pick them up.')
            logger.info('Monitor: journalctl -u tickles-interpretation -f')
        else:
            logger.info(f'Dry run — {len(rows)} items would be reprocessed. Add --all for all failures, or pass media IDs.')

asyncio.run(main())
