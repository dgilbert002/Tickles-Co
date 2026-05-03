"""
Module: test_position_postmortem_uniqueness
Purpose: Verify composite UNIQUE on position_postmortems.
Location: /opt/tickles/shared/tests/test_position_postmortem_uniqueness.py
"""

import os
import random

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
        # Clean up any stale rows from previous aborted runs
        await conn.execute("DELETE FROM position_postmortems WHERE position_id >= 100000")
        yield conn
    finally:
        await conn.close()


def _rand_id() -> int:
    return random.randint(100000, 999999)


async def _insert_postmortem(
    conn: asyncpg.Connection,
    position_id: int,
    postmortem_version: str,
    prompt_version: str,
) -> int:
    """Insert a minimal position_postmortems row and return its id."""
    row = await conn.fetchrow(
        """
        INSERT INTO position_postmortems (
            position_id, postmortem_version, postmortem_provider,
            postmortem_model, param_hash, what_happened, prompt_version
        ) VALUES (
            $1, $2, 'openrouter', 'gpt-4', 'deadbeef00000000',
            'Price hit stop-loss', $3
        )
        RETURNING id
        """,
        position_id,
        postmortem_version,
        prompt_version,
    )
    assert row is not None
    return int(row["id"])


@pytest.mark.asyncio
async def test_uniqueness_blocks_duplicate_triple(db_conn):
    """[AX] Same (position_id, postmortem_version, prompt_version) must raise."""
    pid = _rand_id()
    await _insert_postmortem(db_conn, pid, "v1.0.0", "prompt-hash-a")

    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await _insert_postmortem(db_conn, pid, "v1.0.0", "prompt-hash-a")


@pytest.mark.asyncio
async def test_same_position_different_prompt_versions_succeed(db_conn):
    """[AX] Same (position_id, postmortem_version) with different prompt_version OK."""
    pid = _rand_id()
    id1 = await _insert_postmortem(db_conn, pid, "v1.0.0", "prompt-hash-a")
    id2 = await _insert_postmortem(db_conn, pid, "v1.0.0", "prompt-hash-b")

    assert id1 != id2


@pytest.mark.asyncio
async def test_same_prompt_different_position_succeed(db_conn):
    """[AX] Same (postmortem_version, prompt_version) on different positions OK."""
    pid1 = _rand_id()
    pid2 = _rand_id()
    id1 = await _insert_postmortem(db_conn, pid1, "v1.0.0", "prompt-hash-a")
    id2 = await _insert_postmortem(db_conn, pid2, "v1.0.0", "prompt-hash-a")

    assert id1 != id2


@pytest.mark.asyncio
async def test_different_postmortem_version_same_prompt_succeed(db_conn):
    """[AX] Same position + prompt but different postmortem_version OK."""
    pid = _rand_id()
    id1 = await _insert_postmortem(db_conn, pid, "v1.0.0", "prompt-hash-a")
    id2 = await _insert_postmortem(db_conn, pid, "v1.1.0", "prompt-hash-a")

    assert id1 != id2
