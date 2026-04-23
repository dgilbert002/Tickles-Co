"""
MemU Wrapper — Shared institutional memory for all agents (hardened 2026-04-17)
==============================================================================

HARDENING (audit 2026-04-17):
  * Thread-safe lazy embedder: previously two callers could race on loading
    the SentenceTransformer, duplicating the ~100 MB model in memory.
    Guarded with a `threading.Lock`. [P0-F4]
  * `pg_notify` payload now clamped to <= 7900 bytes (Postgres hard limit
    is 8000) and failures on NOTIFY do NOT prevent the INSERT from
    committing. [P0-F5]
  * Added a UNIQUE key on (kind, content_hash) via a dedup column to
    prevent duplicate insights from flooding the table. [P1-F6]
  * Explicit `close()` method for tests and graceful shutdown. [P2-F18]
  * Auto-reconnect on stale connection (PG `OperationalError`).
  * Password NOT hardcoded: raises if DB_PASSWORD missing and enabled.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_SHARED = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_SHARED)
for p in (_ROOT, _SHARED):
    if p not in sys.path:
        sys.path.insert(0, p)

log = logging.getLogger("tickles.memu")


MEMU_ENABLED = os.getenv("MEMU_ENABLED", "true").lower() == "true"
MEMU_DB = os.getenv("MEMU_DB_NAME", "memu")
MEMU_EMBED_MODEL = os.getenv("MEMU_EMBED_MODEL", "all-MiniLM-L6-v2")

# Postgres NOTIFY payload limit is ~8000 bytes. Clamp well below to leave
# headroom for envelope overhead.
_NOTIFY_MAX_BYTES = 7900


_SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS insights (
    id            UUID PRIMARY KEY,
    created_at    TIMESTAMPTZ(3) NOT NULL DEFAULT NOW(),
    kind          VARCHAR(32)  NOT NULL,
    source_agent  VARCHAR(64),
    content       TEXT         NOT NULL,
    content_hash  CHAR(64)     NOT NULL,
    metadata      JSONB,
    embedding     vector(384)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_insights_kind_hash
    ON insights (kind, content_hash);
CREATE INDEX IF NOT EXISTS idx_insights_kind       ON insights (kind);
CREATE INDEX IF NOT EXISTS idx_insights_created_at ON insights (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_insights_meta_gin   ON insights USING GIN (metadata);
"""


class MemU:
    """Synchronous client. Low-volume write + occasional query."""

    _embedder = None
    _embedder_lock = threading.Lock()

    def __init__(self):
        self.conn = None
        if not MEMU_ENABLED:
            log.info("memu: disabled via MEMU_ENABLED=false — client is a no-op")
            return

        import psycopg2  # noqa: F401 – imported for module availability
        import psycopg2.extras  # noqa: F401
        self._connect()

    def _connect(self) -> None:
        import psycopg2
        password = os.getenv("DB_PASSWORD")
        if password is None:
            raise RuntimeError(
                "DB_PASSWORD env var is required for MemU; refusing default.")
        self.conn = psycopg2.connect(
            host=os.getenv("DB_HOST", "127.0.0.1"),
            port=int(os.getenv("DB_PORT", "5432")),
            user=os.getenv("DB_USER", "admin"),
            password=password,
            dbname=MEMU_DB,
            connect_timeout=10,
        )
        self.conn.autocommit = True
        with self.conn.cursor() as cur:
            # Pass 2 P2: set a session-level statement_timeout so a
            # zombie connection (or a pathologically slow query) can't
            # wedge a whole worker indefinitely. Override with MEMU_STMT_TIMEOUT_MS.
            stmt_timeout = int(os.getenv("MEMU_STMT_TIMEOUT_MS", "15000"))
            cur.execute(f"SET statement_timeout = {stmt_timeout}")
            cur.execute(_SCHEMA_SQL)

    # ----------------------------------------------------------------
    # Lazy, thread-safe embedder
    # ----------------------------------------------------------------
    def _embed(self, text: str) -> Optional[List[float]]:
        if MemU._embedder is None:
            with MemU._embedder_lock:
                if MemU._embedder is None:
                    try:
                        from sentence_transformers import SentenceTransformer
                        MemU._embedder = SentenceTransformer(MEMU_EMBED_MODEL)
                    except Exception as e:
                        log.warning(
                            "memu: embedder unavailable (%s) — stored text-only", e)
                        MemU._embedder = False
        if MemU._embedder is False:
            return None
        try:
            v = MemU._embedder.encode(text, normalize_embeddings=True)
            return v.tolist()
        except Exception:
            log.exception("memu: embedding failed")
            return None

    # ----------------------------------------------------------------
    # Internal helpers
    # ----------------------------------------------------------------
    def _ensure_connected(self) -> None:
        if self.conn is None:
            return
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT 1")
        except Exception:
            log.warning("memu: stale connection — reconnecting")
            try:
                self.conn.close()
            except Exception:
                pass
            self._connect()

    # ----------------------------------------------------------------
    # Public API
    # ----------------------------------------------------------------
    def write_insight(self, kind: str, content: str,
                      source_agent: Optional[str] = None,
                      metadata: Optional[Dict[str, Any]] = None) -> str:
        """Persist a structured insight; dedup on (kind, content_hash).
        Returns the insight id (existing id if duplicate).
        """
        if self.conn is None:
            return ""
        self._ensure_connected()
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        ins_id = str(uuid.uuid4())
        embedding = self._embed(content)
        meta_json = json.dumps(metadata or {}, default=str)

        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO insights (id, kind, source_agent, content,
                                      content_hash, metadata, embedding)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::vector)
                ON CONFLICT (kind, content_hash) DO NOTHING
                RETURNING id
                """,
                (ins_id, kind, source_agent, content, content_hash, meta_json,
                 embedding if embedding else None),
            )
            row = cur.fetchone()
            if row:
                ins_id = str(row[0])
            else:
                # Duplicate: fetch existing id
                cur.execute(
                    "SELECT id FROM insights WHERE kind = %s AND content_hash = %s",
                    (kind, content_hash),
                )
                r = cur.fetchone()
                if r:
                    ins_id = str(r[0])

            # Broadcast. We do NOT let a notify failure bubble up — the
            # insight is already safely persisted. Clamp payload size.
            try:
                payload = json.dumps({
                    "id": ins_id, "kind": kind,
                    "agent": source_agent,
                    "preview": content[:120],
                }, default=str)
                if len(payload.encode("utf-8")) > _NOTIFY_MAX_BYTES:
                    # truncate preview further
                    payload = json.dumps({
                        "id": ins_id, "kind": kind, "agent": source_agent,
                        "preview": "",
                    })
                cur.execute("SELECT pg_notify('memu_insights', %s)", (payload,))
            except Exception:
                log.exception("memu: pg_notify failed (insight still written)")

        log.info("memu.write_insight: %s kind=%s agent=%s",
                 ins_id, kind, source_agent)
        return ins_id

    def search(self, query: str, *, kind: Optional[str] = None,
               k: int = 10) -> List[Dict[str, Any]]:
        """Semantic NN search; falls back to recency when embedder missing."""
        if self.conn is None:
            return []
        self._ensure_connected()
        k = int(max(1, min(k, 500)))
        embed = self._embed(query)
        if embed is None:
            where = "WHERE kind = %s" if kind else ""
            args = (kind,) if kind else ()
            with self.conn.cursor() as cur:
                cur.execute(
                    f"SELECT id, kind, content, metadata, created_at "
                    f"FROM insights {where} ORDER BY created_at DESC LIMIT %s",
                    args + (k,),
                )
                cols = [c[0] for c in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]

        # Positional-parameter layout for the vector query:
        #   1st %s -> embed   (in SELECT ... AS distance)
        #   2nd %s -> kind    (in optional WHERE)  -- skipped when no kind
        #   3rd %s -> embed   (in ORDER BY)
        #   4th %s -> k       (in LIMIT)
        # Bug (pre-2026-04-20): `args + (embed, embed, k)` placed `kind`
        # before the first embed, which made Postgres try to cast "warning"
        # to vector and raised `malformed vector literal`. Fixed by slotting
        # the kind AFTER the first embed so the tuple aligns with the SQL.
        if kind:
            sql = (
                "SELECT id, kind, content, metadata, created_at, "
                "       embedding <=> %s::vector AS distance "
                "FROM insights WHERE kind = %s "
                "ORDER BY embedding <=> %s::vector LIMIT %s"
            )
            params: tuple = (embed, kind, embed, k)
        else:
            sql = (
                "SELECT id, kind, content, metadata, created_at, "
                "       embedding <=> %s::vector AS distance "
                "FROM insights "
                "ORDER BY embedding <=> %s::vector LIMIT %s"
            )
            params = (embed, embed, k)
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def count(self) -> int:
        if self.conn is None:
            return 0
        self._ensure_connected()
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM insights")
            return cur.fetchone()[0]

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None


# ---------------------------------------------------------------------------
# Module-level singleton helper
# ---------------------------------------------------------------------------
_client: Optional[MemU] = None
_client_lock = threading.Lock()


def get_memu() -> MemU:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = MemU()
    return _client


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    m = get_memu()
    print("count:", m.count())
    m.write_insight(
        kind="smoke_test",
        content=("MemU wrapper wired up successfully. "
                 "This is a self-test insight."),
        source_agent="daemon",
        metadata={"phase": "bootstrap"},
    )
    print("after insert count:", m.count())
    print("search:", m.search("wrapper wired", k=3))
