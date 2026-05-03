"""
Module: test_reason_freeze_trigger
Purpose: Verify the entry_reason freeze trigger on tracked_positions.
Location: /opt/tickles/shared/tests/test_reason_freeze_trigger.py
"""

import os
import random
from decimal import Decimal

import asyncpg
import pytest

DB_DSN = (
    f"postgresql://{os.environ.get('DB_USER', 'admin')}"
    f":{os.environ.get('DB_PASSWORD', 'Tickles21!')}"
    f"@{os.environ.get('DB_HOST', '127.0.0.1')}"
    f":{os.environ.get('DB_PORT', '5432')}"
    f"/{os.environ.get('DB_NAME_SHARED', 'tickles_shared')}"
)


@pytest.fixture
async def db_conn():
    conn = await asyncpg.connect(DB_DSN)
    try:
        yield conn
    finally:
        await conn.close()


async def _insert_minimal_position(conn: asyncpg.Connection) -> int:
    """Insert a minimal tracked_position row and return its id."""
    # Use random IDs to avoid uq_position_dedup collisions across tests
    rand_id = random.randint(100000, 999999)
    row = await conn.fetchrow(
        """
        INSERT INTO tracked_positions (
            news_item_id, trader_profile_id, instrument_symbol,
            instrument_exchange, direction, signal_timestamp,
            company_id, status
        ) VALUES (
            $1, $2, 'BTCUSDT', 'bybit', 'long',
            NOW(), 'test_company', 'open'
        )
        RETURNING id
        """,
        rand_id,
        rand_id + 1,
    )
    assert row is not None
    return int(row["id"])


@pytest.mark.anyio
async def test_freeze_trigger_allows_update_before_frozen(db_conn):
    """[AG] Updates to entry_reason_* are allowed before frozen_at is set."""
    pos_id = await _insert_minimal_position(db_conn)

    await db_conn.execute(
        """
        UPDATE tracked_positions
        SET entry_reason_trader = 'RSI oversold',
            entry_reason_llm = 'Bullish breakout detected',
            entry_reason_agent = 'Surgeon2 auto-entry'
        WHERE id = $1
        """,
        pos_id,
    )

    row = await db_conn.fetchrow(
        "SELECT entry_reason_trader FROM tracked_positions WHERE id = $1", pos_id
    )
    assert row is not None
    assert row["entry_reason_trader"] == "RSI oversold"


@pytest.mark.anyio
async def test_freeze_trigger_blocks_reason_update_after_frozen(db_conn):
    """[AG] Updating entry_reason_trader after freeze must raise."""
    pos_id = await _insert_minimal_position(db_conn)

    # Set reasons and freeze
    await db_conn.execute(
        """
        UPDATE tracked_positions
        SET entry_reason_trader = 'RSI oversold',
            entry_reason_llm = 'Bullish breakout',
            entry_reason_agent = 'Auto',
            entry_reason_frozen_at = NOW()
        WHERE id = $1
        """,
        pos_id,
    )

    with pytest.raises(asyncpg.exceptions.RaiseError):
        await db_conn.execute(
            """
            UPDATE tracked_positions
            SET entry_reason_trader = 'Changed!'
            WHERE id = $1
            """,
            pos_id,
        )


@pytest.mark.anyio
async def test_freeze_trigger_allows_non_reason_updates_after_frozen(db_conn):
    """[AG] current_price, pnl, sl_price, exit_reason may still update."""
    pos_id = await _insert_minimal_position(db_conn)

    await db_conn.execute(
        """
        UPDATE tracked_positions
        SET entry_reason_trader = 'RSI oversold',
            entry_reason_llm = 'Bullish breakout',
            entry_reason_agent = 'Auto',
            entry_reason_frozen_at = NOW(),
            current_price = 65000.00,
            unrealized_pnl_pct = 2.5,
            stop_loss = 64000.00,
            exit_reason = 'Manual close'
        WHERE id = $1
        """,
        pos_id,
    )

    row = await db_conn.fetchrow(
        """
        SELECT current_price, unrealized_pnl_pct, stop_loss, exit_reason
        FROM tracked_positions WHERE id = $1
        """,
        pos_id,
    )
    assert row is not None
    assert Decimal(row["current_price"]) == Decimal("65000.00")
    assert Decimal(row["unrealized_pnl_pct"]) == Decimal("2.5")
    assert Decimal(row["stop_loss"]) == Decimal("64000.00")
    assert row["exit_reason"] == "Manual close"


@pytest.mark.anyio
async def test_freeze_trigger_blocks_frozen_at_change(db_conn):
    """[AG] Changing entry_reason_frozen_at itself after freeze must raise."""
    pos_id = await _insert_minimal_position(db_conn)

    await db_conn.execute(
        """
        UPDATE tracked_positions
        SET entry_reason_frozen_at = NOW()
        WHERE id = $1
        """,
        pos_id,
    )

    with pytest.raises(asyncpg.exceptions.RaiseError):
        await db_conn.execute(
            """
            UPDATE tracked_positions
            SET entry_reason_frozen_at = NOW() + INTERVAL '1 day'
            WHERE id = $1
            """,
            pos_id,
        )


@pytest.mark.anyio
async def test_freeze_trigger_allows_noop_update(db_conn):
    """[AG] Updating a frozen row with identical reason values is a no-op."""
    pos_id = await _insert_minimal_position(db_conn)

    await db_conn.execute(
        """
        UPDATE tracked_positions
        SET entry_reason_trader = 'RSI oversold',
            entry_reason_frozen_at = NOW()
        WHERE id = $1
        """,
        pos_id,
    )

    # Same value — should pass (IS DISTINCT FROM is false)
    await db_conn.execute(
        """
        UPDATE tracked_positions
        SET entry_reason_trader = 'RSI oversold'
        WHERE id = $1
        """,
        pos_id,
    )

    row = await db_conn.fetchrow(
        "SELECT entry_reason_trader FROM tracked_positions WHERE id = $1", pos_id
    )
    assert row is not None
    assert row["entry_reason_trader"] == "RSI oversold"
