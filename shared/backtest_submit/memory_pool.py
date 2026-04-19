"""In-memory pool for Phase 35 backtest-submit tests."""
from __future__ import annotations

import itertools
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence


ACTIVE_AND_COMPLETED = {"submitted", "queued", "running", "completed"}


class InMemoryBacktestSubmitPool:
    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []
        self._seq = itertools.count(1)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _loads(val: Any) -> Any:
        if val is None:
            return None
        if isinstance(val, (dict, list)):
            return val
        try:
            return json.loads(val)
        except Exception:
            return None

    async def execute(self, sql: str, params: Sequence[Any]) -> int:
        sql = sql.strip()
        if sql.startswith("UPDATE public.backtest_submissions"):
            # Dispatch on the particular update we expect.
            sid = int(params[0])
            row = next((r for r in self.rows if r["id"] == sid), None)
            if row is None:
                return 0

            if "status = $2" in sql and "queue_job_id" in sql:
                # mark_queued: status, queue_job_id
                status = params[1]
                queue_job_id = params[2]
                row["status"] = status
                if queue_job_id is not None:
                    row["queue_job_id"] = queue_job_id
                if row.get("queued_at") is None:
                    row["queued_at"] = self._now()
                row["updated_at"] = self._now()
                return 1
            if "status = 'running'" in sql:
                row["status"] = "running"
                if row.get("started_at") is None:
                    row["started_at"] = self._now()
                row["updated_at"] = self._now()
                return 1
            if "status = 'completed'" in sql:
                row["status"] = "completed"
                row["result_summary"] = self._loads(params[1])
                artefacts = self._loads(params[2])
                if artefacts is not None:
                    row["artefacts"] = artefacts
                row["completed_at"] = self._now()
                row["updated_at"] = self._now()
                return 1
            if "status = 'failed'" in sql:
                row["status"] = "failed"
                row["error"] = params[1]
                row["completed_at"] = self._now()
                row["updated_at"] = self._now()
                return 1
            if "status = 'cancelled'" in sql:
                row["status"] = "cancelled"
                if params[1] is not None:
                    row["error"] = params[1]
                row["completed_at"] = self._now()
                row["updated_at"] = self._now()
                return 1
            raise NotImplementedError(
                f"unknown update shape: {sql!r}")
        raise NotImplementedError(
            f"InMemoryBacktestSubmitPool.execute: {sql!r}")

    async def fetch_one(
        self, sql: str, params: Sequence[Any],
    ) -> Optional[Dict[str, Any]]:
        sql = sql.strip()
        if sql.startswith("INSERT INTO public.backtest_submissions"):
            (
                company_id, client_id, spec_json, spec_hash, status,
                queue_job_id, result_summary, artefacts, error,
                metadata, submitted_at, queued_at, started_at,
                completed_at,
            ) = params
            # Partial unique index on spec_hash where status in
            # (submitted, queued, running, completed).
            for r in self.rows:
                if (
                    r["spec_hash"] == spec_hash
                    and r["status"] in ACTIVE_AND_COMPLETED
                ):
                    return None
            rid = next(self._seq)
            self.rows.append({
                "id": rid,
                "company_id": company_id, "client_id": client_id,
                "spec": self._loads(spec_json) or {},
                "spec_hash": spec_hash,
                "status": status or "submitted",
                "queue_job_id": queue_job_id,
                "result_summary": self._loads(result_summary),
                "artefacts": self._loads(artefacts) or {},
                "error": error,
                "metadata": self._loads(metadata) or {},
                "submitted_at": submitted_at or self._now(),
                "queued_at": queued_at,
                "started_at": started_at,
                "completed_at": completed_at,
                "updated_at": self._now(),
            })
            return {"id": rid}

        if sql.startswith("SELECT * FROM public.backtest_submissions WHERE id"):
            sid = int(params[0])
            row = next((r for r in self.rows if r["id"] == sid), None)
            return dict(row) if row else None

        if sql.startswith("SELECT * FROM public.backtest_submissions "
                          "WHERE spec_hash"):
            h = params[0]
            rows = [r for r in self.rows if r["spec_hash"] == h]
            rows.sort(
                key=lambda r: r.get("submitted_at") or self._now(),
                reverse=True,
            )
            return dict(rows[0]) if rows else None

        raise NotImplementedError(
            f"InMemoryBacktestSubmitPool.fetch_one: {sql!r}")

    async def fetch_all(
        self, sql: str, params: Sequence[Any],
    ) -> List[Dict[str, Any]]:
        sql = sql.strip()

        if sql.startswith("SELECT * FROM public.backtest_submissions_active"):
            rows = [
                r for r in self.rows
                if r["status"] in ("submitted", "queued", "running")
            ]
            p_idx = 0
            if "WHERE client_id = $" in sql:
                rows = [r for r in rows if r["client_id"] == params[p_idx]]
                p_idx += 1
            limit = int(params[p_idx])
            rows.sort(
                key=lambda r: r.get("submitted_at") or self._now(),
                reverse=True,
            )
            return [dict(r) for r in rows[:limit]]

        if sql.startswith("SELECT * FROM public.backtest_submissions"):
            rows = list(self.rows)
            p_idx = 0
            if " AND status = $" in sql:
                rows = [r for r in rows if r["status"] == params[p_idx]]
                p_idx += 1
            if " AND client_id = $" in sql:
                rows = [r for r in rows if r["client_id"] == params[p_idx]]
                p_idx += 1
            limit = int(params[p_idx])
            rows.sort(
                key=lambda r: r.get("submitted_at") or self._now(),
                reverse=True,
            )
            return [dict(r) for r in rows[:limit]]

        raise NotImplementedError(
            f"InMemoryBacktestSubmitPool.fetch_all: {sql!r}")


__all__ = ["InMemoryBacktestSubmitPool"]
