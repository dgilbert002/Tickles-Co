#!/usr/bin/env python3
"""
One-shot backfill: populate techniques_validated on all existing postmortems
that have NULL. Uses pattern_tags from signal_interpretations via news_item_id.
"""
import asyncio, os, json
from dotenv import load_dotenv
load_dotenv("/opt/tickles/.env")
import asyncpg

async def run():
    conn = await asyncpg.connect(
        host="localhost", database="tickles_shared",
        user="admin", password=os.environ["DB_PASSWORD"])
    
    rows = await conn.fetch("""
        SELECT pm.id, pm.position_id, tp.realized_pnl_usd,
               tp.news_item_id, tp.signal_interpretation_id,
               si.pattern_tags AS direct_tags
        FROM position_postmortems pm
        JOIN tracked_positions tp ON tp.id = pm.position_id
        LEFT JOIN signal_interpretations si ON si.id = tp.signal_interpretation_id
        WHERE pm.techniques_validated IS NULL
    """)
    
    print(f"Postmortems to backfill: {len(rows)}")
    
    # Pre-fetch all pattern_tags by news_item_id
    news_ids = [r["news_item_id"] for r in rows if r["news_item_id"]]
    if news_ids:
        tag_rows = await conn.fetch("""
            SELECT DISTINCT ON (news_item_id) news_item_id, pattern_tags
            FROM signal_interpretations
            WHERE news_item_id = ANY($1) AND pattern_tags IS NOT NULL
              AND jsonb_array_length(pattern_tags) > 0
            ORDER BY news_item_id, created_at DESC
        """, news_ids)
        tag_map = {r["news_item_id"]: r["pattern_tags"] for r in tag_rows}
    else:
        tag_map = {}
    
    updated = 0
    for r in rows:
        tags = r["direct_tags"]
        if not tags and r["news_item_id"]:
            tags = tag_map.get(r["news_item_id"])
        if not tags:
            continue
        if isinstance(tags, str):
            tags = json.loads(tags)
        
        pnl = float(r["realized_pnl_usd"] or 0)
        played_out = True if pnl > 0 else (False if pnl < 0 else None)
        
        validations = [
            {"technique": t, "played_out": played_out,
             "note": "backfilled from pattern_tags + trade outcome"}
            for t in tags[:5]
        ]
        
        await conn.execute(
            "UPDATE position_postmortems SET techniques_validated = $1 WHERE id = $2",
            json.dumps(validations), r["id"])
        updated += 1
    
    print(f"Backfilled {updated} postmortems with techniques_validated")
    
    # Now grade them all via technique_tracker
    from shared.intelligence.technique_tracker import grade_techniques_for_position
    graded = 0
    rows2 = await conn.fetch("""
        SELECT pm.id, pm.techniques_validated
        FROM position_postmortems pm
        WHERE pm.techniques_validated IS NOT NULL
    """)
    for r in rows2:
        try:
            await grade_techniques_for_position(
                conn, r["id"],
                validations=json.loads(r["techniques_validated"]) if isinstance(r["techniques_validated"], str) else r["techniques_validated"]
            )
            graded += 1
        except Exception as e:
            print(f"  grade failed for pm {r['id']}: {e}")
    
    print(f"Graded {graded} postmortems via technique_tracker")
    
    # Check technique_stats
    count = await conn.fetchval("SELECT COUNT(*) FROM technique_stats WHERE sample_count > 0")
    print(f"technique_stats rows with data: {count}")
    
    await conn.close()

asyncio.run(run())
