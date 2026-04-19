"""SQLite-backed audit store.

One file, one table, no migrations. SQLite is deliberate:

  * The auditor runs on the VPS alongside everything else and we
    don't want yet another Postgres dependency for what is, in
    steady state, a write-heavy low-query-rate log.
  * Dean can `scp rule1.sqlite3 local/` and inspect with any SQLite
    client for forensic analysis.
  * If we later decide to mirror to Postgres, ``AuditStore.replay()``
    gives us a clean iterator.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from shared.auditor.schema import (
    AuditEventType,
    AuditRecord,
    AuditSeverity,
    DivergenceSummary,
)

_DEFAULT_PATH = os.environ.get(
    "TICKLES_AUDIT_DB",
    "/opt/tickles/var/audit/rule1.sqlite3" if os.name != "nt"
    else str(Path.home() / ".tickles" / "audit" / "rule1.sqlite3"),
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_unix        REAL    NOT NULL,
    event_type     TEXT    NOT NULL,
    severity       TEXT    NOT NULL,
    subject        TEXT    NOT NULL,
    strategy_id    TEXT,
    engine         TEXT,
    passed         INTEGER NOT NULL,
    metric         REAL,
    tolerance      REAL,
    details_json   TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_audit_events_ts ON audit_events(ts_unix DESC);
CREATE INDEX IF NOT EXISTS idx_audit_events_type ON audit_events(event_type, ts_unix DESC);
CREATE INDEX IF NOT EXISTS idx_audit_events_severity ON audit_events(severity, ts_unix DESC);
CREATE INDEX IF NOT EXISTS idx_audit_events_subject ON audit_events(subject, ts_unix DESC);
"""


class AuditStore:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = str(path or _DEFAULT_PATH)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "AuditStore":
        return self

    def __exit__(self, *a: Any) -> None:
        self.close()

    # ------------------------ write path ------------------------

    def record(self, rec: AuditRecord) -> AuditRecord:
        row = rec.to_row()
        cur = self._conn.execute(
            "INSERT INTO audit_events "
            "(ts_unix, event_type, severity, subject, strategy_id, engine, "
            " passed, metric, tolerance, details_json) VALUES "
            "(:ts_unix, :event_type, :severity, :subject, :strategy_id, :engine, "
            " :passed, :metric, :tolerance, :details_json)",
            row,
        )
        self._conn.commit()
        rec.id = int(cur.lastrowid) if cur.lastrowid is not None else None
        return rec

    def record_many(self, recs: List[AuditRecord]) -> int:
        rows = [r.to_row() for r in recs]
        self._conn.executemany(
            "INSERT INTO audit_events "
            "(ts_unix, event_type, severity, subject, strategy_id, engine, "
            " passed, metric, tolerance, details_json) VALUES "
            "(:ts_unix, :event_type, :severity, :subject, :strategy_id, :engine, "
            " :passed, :metric, :tolerance, :details_json)",
            rows,
        )
        self._conn.commit()
        return len(rows)

    # ------------------------ read path -------------------------

    def list_recent(
        self,
        limit: int = 50,
        event_type: Optional[AuditEventType] = None,
        severity: Optional[AuditSeverity] = None,
        min_ts_unix: Optional[float] = None,
    ) -> List[AuditRecord]:
        sql = "SELECT * FROM audit_events WHERE 1=1"
        params: List[Any] = []
        if event_type is not None:
            sql += " AND event_type = ?"
            params.append(event_type.value)
        if severity is not None:
            sql += " AND severity = ?"
            params.append(severity.value)
        if min_ts_unix is not None:
            sql += " AND ts_unix >= ?"
            params.append(min_ts_unix)
        sql += " ORDER BY ts_unix DESC LIMIT ?"
        params.append(limit)
        cur = self._conn.execute(sql, params)
        return [AuditRecord.from_row(dict(row)) for row in cur.fetchall()]

    def summary(self, window_seconds: int = 3600) -> DivergenceSummary:
        """Roll up all events in the last ``window_seconds`` seconds."""
        now = time.time()
        min_ts = now - window_seconds

        cur = self._conn.execute(
            "SELECT severity, passed, event_type, engine FROM audit_events "
            "WHERE ts_unix >= ?",
            (min_ts,),
        )
        rows = cur.fetchall()
        total = len(rows)
        passed = sum(1 for r in rows if r["passed"])
        warnings = sum(1 for r in rows if r["severity"] == AuditSeverity.WARNING.value)
        breaches = sum(1 for r in rows if r["severity"] == AuditSeverity.BREACH.value)
        critical = sum(1 for r in rows if r["severity"] == AuditSeverity.CRITICAL.value)
        by_event_type: Dict[str, int] = {}
        by_engine: Dict[str, int] = {}
        for r in rows:
            by_event_type[r["event_type"]] = by_event_type.get(r["event_type"], 0) + 1
            if r["engine"]:
                by_engine[r["engine"]] = by_engine.get(r["engine"], 0) + 1
        pass_rate = (passed / total) if total > 0 else 1.0

        cur2 = self._conn.execute(
            "SELECT MAX(ts_unix) AS last_ts FROM audit_events WHERE ts_unix >= ?",
            (min_ts,),
        )
        last_ts_row = cur2.fetchone()
        last_ts = last_ts_row["last_ts"] if last_ts_row and last_ts_row["last_ts"] else None

        return DivergenceSummary(
            window_seconds=window_seconds,
            total=total,
            passed=passed,
            warnings=warnings,
            breaches=breaches,
            critical=critical,
            pass_rate=pass_rate,
            last_event_ts=last_ts,
            by_event_type=by_event_type,
            by_engine=by_engine,
        )

    def purge_older_than(self, ts_unix: float) -> int:
        cur = self._conn.execute(
            "DELETE FROM audit_events WHERE ts_unix < ?", (ts_unix,)
        )
        self._conn.commit()
        return int(cur.rowcount or 0)

    def replay(self, batch: int = 500) -> Iterator[AuditRecord]:
        """Yield every record in insertion order (for backups / mirrors)."""
        offset = 0
        while True:
            cur = self._conn.execute(
                "SELECT * FROM audit_events ORDER BY id ASC LIMIT ? OFFSET ?",
                (batch, offset),
            )
            rows = cur.fetchall()
            if not rows:
                return
            for row in rows:
                yield AuditRecord.from_row(dict(row))
            offset += batch
