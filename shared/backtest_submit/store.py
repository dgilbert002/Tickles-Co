"""shared.backtest_submit.store — async wrappers for Phase 35 tables."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from shared.backtest_submit.protocol import (
    BacktestSpec,
    BacktestSubmission,
    STATUS_QUEUED,
)


class BacktestSubmissionStore:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def create(self, sub: BacktestSubmission) -> int:
        sql = (
            "INSERT INTO public.backtest_submissions "
            "(company_id, client_id, spec, spec_hash, status, "
            " queue_job_id, result_summary, artefacts, error, "
            " metadata, submitted_at, queued_at, started_at, "
            " completed_at, updated_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,"
            "        $14, NOW()) "
            "ON CONFLICT (spec_hash) "
            "WHERE status IN ('submitted','queued','running','completed') "
            "DO NOTHING RETURNING id"
        )
        row = await self._pool.fetch_one(
            sql,
            (
                sub.company_id, sub.client_id,
                json.dumps(sub.spec.to_dict(), default=str),
                sub.spec_hash, sub.status, sub.queue_job_id,
                None if sub.result_summary is None
                else json.dumps(sub.result_summary, default=str),
                json.dumps(sub.artefacts or {}, default=str),
                sub.error, json.dumps(sub.metadata or {}, default=str),
                sub.submitted_at, sub.queued_at, sub.started_at,
                sub.completed_at,
            ),
        )
        return int(row["id"]) if row and "id" in row else 0

    async def get(self, submission_id: int) -> Optional[BacktestSubmission]:
        sql = "SELECT * FROM public.backtest_submissions WHERE id = $1"
        row = await self._pool.fetch_one(sql, (int(submission_id),))
        return _row(row) if row else None

    async def get_by_hash(
        self, spec_hash: str,
    ) -> Optional[BacktestSubmission]:
        sql = (
            "SELECT * FROM public.backtest_submissions "
            "WHERE spec_hash = $1 "
            "ORDER BY submitted_at DESC LIMIT 1"
        )
        row = await self._pool.fetch_one(sql, (spec_hash,))
        return _row(row) if row else None

    async def mark_queued(
        self, submission_id: int, queue_job_id: Optional[str],
    ) -> None:
        sql = (
            "UPDATE public.backtest_submissions SET "
            "  status = $2, queue_job_id = COALESCE($3, queue_job_id), "
            "  queued_at = COALESCE(queued_at, NOW()), "
            "  updated_at = NOW() "
            "WHERE id = $1"
        )
        await self._pool.execute(
            sql, (int(submission_id), STATUS_QUEUED, queue_job_id),
        )

    async def mark_running(self, submission_id: int) -> None:
        sql = (
            "UPDATE public.backtest_submissions SET "
            "  status = 'running', "
            "  started_at = COALESCE(started_at, NOW()), "
            "  updated_at = NOW() "
            "WHERE id = $1"
        )
        await self._pool.execute(sql, (int(submission_id),))

    async def mark_completed(
        self,
        submission_id: int,
        *,
        result_summary: Dict[str, Any],
        artefacts: Optional[Dict[str, Any]] = None,
    ) -> None:
        sql = (
            "UPDATE public.backtest_submissions SET "
            "  status = 'completed', "
            "  result_summary = $2, "
            "  artefacts = COALESCE($3, artefacts), "
            "  completed_at = NOW(), "
            "  updated_at = NOW() "
            "WHERE id = $1"
        )
        await self._pool.execute(
            sql,
            (
                int(submission_id),
                json.dumps(result_summary, default=str),
                None if artefacts is None
                else json.dumps(artefacts, default=str),
            ),
        )

    async def mark_failed(
        self, submission_id: int, *, error: str,
    ) -> None:
        sql = (
            "UPDATE public.backtest_submissions SET "
            "  status = 'failed', "
            "  error = $2, "
            "  completed_at = NOW(), "
            "  updated_at = NOW() "
            "WHERE id = $1"
        )
        await self._pool.execute(
            sql, (int(submission_id), error[:4096]),
        )

    async def mark_cancelled(
        self, submission_id: int, *, reason: Optional[str] = None,
    ) -> None:
        sql = (
            "UPDATE public.backtest_submissions SET "
            "  status = 'cancelled', "
            "  error = COALESCE($2, error), "
            "  completed_at = NOW(), "
            "  updated_at = NOW() "
            "WHERE id = $1"
        )
        await self._pool.execute(
            sql, (int(submission_id), reason),
        )

    async def list(
        self,
        *,
        status: Optional[str] = None,
        client_id: Optional[str] = None,
        active_only: bool = False,
        limit: int = 100,
    ) -> List[BacktestSubmission]:
        if active_only:
            sql = "SELECT * FROM public.backtest_submissions_active"
            params: List[Any] = []
            idx = 1
            extras = ""
            if client_id is not None:
                extras += f" WHERE client_id = ${idx}"
                params.append(client_id)
                idx += 1
            sql += extras + f" ORDER BY submitted_at DESC LIMIT ${idx}"
            params.append(int(limit))
        else:
            sql = "SELECT * FROM public.backtest_submissions WHERE 1=1"
            params = []
            idx = 1
            if status is not None:
                sql += f" AND status = ${idx}"
                params.append(status)
                idx += 1
            if client_id is not None:
                sql += f" AND client_id = ${idx}"
                params.append(client_id)
                idx += 1
            sql += f" ORDER BY submitted_at DESC LIMIT ${idx}"
            params.append(int(limit))
        rows = await self._pool.fetch_all(sql, tuple(params))
        return [_row(r) for r in rows]


def _row(row: Dict[str, Any]) -> BacktestSubmission:
    spec_raw = row.get("spec")
    if isinstance(spec_raw, str):
        try:
            spec_raw = json.loads(spec_raw)
        except Exception:
            spec_raw = {}
    if not isinstance(spec_raw, dict):
        spec_raw = {}
    spec = BacktestSpec(
        strategy=spec_raw.get("strategy", ""),
        symbols=list(spec_raw.get("symbols") or []),
        timeframe=spec_raw.get("timeframe", "1m"),
        start=spec_raw.get("start"),
        end=spec_raw.get("end"),
        engine=spec_raw.get("engine", "classic"),
        params=dict(spec_raw.get("params") or {}),
        capital_usd=float(spec_raw.get("capital_usd") or 0.0),
        metadata=dict(spec_raw.get("metadata") or {}),
    )
    return BacktestSubmission(
        id=int(row["id"]),
        spec=spec, spec_hash=row["spec_hash"],
        status=row.get("status") or "submitted",
        client_id=row.get("client_id"),
        queue_job_id=row.get("queue_job_id"),
        result_summary=_maybe_json(row.get("result_summary")),
        artefacts=_maybe_json(row.get("artefacts")) or {},
        error=row.get("error"),
        company_id=row.get("company_id"),
        metadata=_maybe_json(row.get("metadata")) or {},
        submitted_at=_maybe_dt(row.get("submitted_at")),
        queued_at=_maybe_dt(row.get("queued_at")),
        started_at=_maybe_dt(row.get("started_at")),
        completed_at=_maybe_dt(row.get("completed_at")),
    )


def _maybe_json(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return None


def _maybe_dt(v: Any) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v))
    except Exception:
        return None


__all__ = ["BacktestSubmissionStore"]
