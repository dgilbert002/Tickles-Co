"""Smoke test for the Postgres-era db.py.

Exercises:
  * Pool lifecycle (get_shared_pool, get_company_pool)
  * execute() with $N and %s placeholders
  * fetch_one / fetch_all / fetch_val
  * execute_many batch insert
  * JSONB round-trip
  * Clean-up (DELETE the test rows)

Run:  python3 /opt/tickles/smoke_test_pg.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/opt/tickles")

from shared.utils import db

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("smoke")


async def test_shared_pool() -> None:
    log.info("=== Testing shared pool (tickles_shared) ===")
    pool = await db.get_shared_pool()

    version = await pool.fetch_val("SELECT version()")
    log.info("PG version: %s", version.split()[1] if version else "?")

    row = await pool.fetch_one(
        "SELECT namespace, config_key, config_value "
        "FROM system_config WHERE config_key = %s",
        ("version",),
    )
    assert row is not None, "system_config 'version' row missing"
    log.info("system_config.version -> %s", row)

    # Insert a test instrument
    inst_id = await pool.fetch_val(
        "INSERT INTO instruments (symbol, exchange, asset_class, opening_hours) "
        "VALUES ($1, $2, $3, $4) RETURNING id",
        ("SMOKETEST", "bybit", "crypto", json.dumps({"mon": "00:00-23:59"})),
    )
    log.info("inserted instrument id=%s", inst_id)

    # Read back with JSONB extraction
    inst = await pool.fetch_one(
        "SELECT id, symbol, exchange, opening_hours FROM instruments WHERE id = %s",
        (inst_id,),
    )
    log.info("round-tripped -> %s", inst)
    assert inst["symbol"] == "SMOKETEST"

    # Test candle insert (partition routing happens automatically)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    await pool.execute(
        "INSERT INTO candles "
        "(instrument_id, timeframe, source, timestamp, open, high, low, close, volume) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (inst_id, "1m", "smoke", now, 100.0, 101.0, 99.0, 100.5, 1234.5),
    )
    got = await pool.fetch_one(
        "SELECT open, high, low, close, volume FROM candles "
        "WHERE instrument_id = $1 AND timeframe = $2",
        (inst_id, "1m"),
    )
    log.info("candle round-trip -> %s", got)

    # Cleanup
    await pool.execute("DELETE FROM candles WHERE instrument_id = $1", (inst_id,))
    await pool.execute("DELETE FROM instruments WHERE id = $1", (inst_id,))
    log.info("cleanup OK")


async def test_company_pool() -> None:
    log.info("=== Testing company pool (tickles_jarvais) ===")
    pool = await db.get_company_pool("jarvais")

    # Insert an account then delete
    acc_id = await pool.fetch_val(
        "INSERT INTO accounts (exchange, account_id_external, account_type) "
        "VALUES ($1, $2, $3) RETURNING id",
        ("smoke", "smoke-001", "demo"),
    )
    log.info("inserted account id=%s", acc_id)

    # execute_many
    rows = [
        (acc_id, 100.0, 100.0, 0.0, 0.0, "exchange_api", datetime.now(timezone.utc))
        for _ in range(5)
    ]
    n = await pool.execute_many(
        "INSERT INTO balance_snapshots "
        "(account_id, balance, equity, margin_used, unrealized_pnl, snapshot_source, snapshot_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7)",
        rows,
    )
    log.info("execute_many inserted %s rows", n)

    count = await pool.fetch_val(
        "SELECT COUNT(*) FROM balance_snapshots WHERE account_id = $1", (acc_id,)
    )
    assert count == 5, f"expected 5 snapshots, got {count}"

    await pool.execute("DELETE FROM balance_snapshots WHERE account_id = $1", (acc_id,))
    await pool.execute("DELETE FROM accounts WHERE id = $1", (acc_id,))
    log.info("cleanup OK")


async def test_placeholder_translator() -> None:
    log.info("=== Testing placeholder translator ===")
    from shared.utils.db import _translate_placeholders
    assert _translate_placeholders("SELECT 1") == "SELECT 1"
    assert _translate_placeholders("WHERE a = %s") == "WHERE a = $1"
    assert _translate_placeholders(
        "WHERE a = %s AND b = %s"
    ) == "WHERE a = $1 AND b = $2"
    # Ensure it doesn't touch literal %s inside quotes
    assert _translate_placeholders(
        "WHERE x = %s AND y = 'literal %s stays'"
    ) == "WHERE x = $1 AND y = 'literal %s stays'"
    log.info("placeholder translator OK")


async def main() -> None:
    await test_placeholder_translator()
    await test_shared_pool()
    await test_company_pool()
    await db.close_all_pools()
    log.info("=== ALL TESTS PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
