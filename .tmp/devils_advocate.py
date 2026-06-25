#!/usr/bin/env python3
import asyncio, asyncpg, os, sys

async def main():
    pw = os.environ.get("DB_PASSWORD")
    user = os.environ.get("DB_USER", "admin")
    host = os.environ.get("DB_HOST", "127.0.0.1")
    port = int(os.environ.get("DB_PORT", "5432"))
    db = os.environ.get("DB_NAME_SHARED", "tickles_shared")
    conn = await asyncpg.connect(host=host, port=port, user=user, password=pw, database=db)
    qs = sys.argv[1:]
    for q in qs:
        print(f"--- {q.strip()[:80]}")
        try:
            rows = await conn.fetch(q)
            for r in rows:
                print("|".join(str(c) for c in r))
        except Exception as e:
            print("ERR:", e)
    await conn.close()

asyncio.run(main())
