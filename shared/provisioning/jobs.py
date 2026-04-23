"""Provisioning job progress events.

Each provisioning step emits one or more ``JobEvent``s that get POSTed to
Paperclip's ``POST /api/companies/:id/provisioning-events`` endpoint (added
by the Phase-3 server patch).

Design goals:
* **Fire-and-forget** — failures to record progress must NEVER break the
  underlying provisioning step. We log and move on.
* **Stateless** — no queues, no background worker. The HTTP call is made
  inline from the executor.
* **Cheap** — the server endpoint only accepts append-only events, so even
  under duplicate events it stays idempotent.

The Paperclip side aggregates events into the
``company_provisioning_jobs`` table (see Drizzle schema added in Phase-3).
"""

from __future__ import annotations

import json as _json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

LOG = logging.getLogger("tickles.provisioning.jobs")

PAPERCLIP_URL_DEFAULT = os.environ.get("PAPERCLIP_URL", "http://127.0.0.1:3100")
PAPERCLIP_TOKEN = os.environ.get("PAPERCLIP_API_TOKEN") or None


STATUS_RUNNING = "running"
STATUS_OK = "ok"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

ALL_STATUSES = (STATUS_RUNNING, STATUS_OK, STATUS_SKIPPED, STATUS_FAILED)


@dataclass
class JobEvent:
    """A single step-progress event."""

    company_id: str
    step: str                 # e.g. "paperclip_row", "postgres_db"
    step_index: int           # 1..9
    status: str               # running | ok | skipped | failed
    detail: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    job_id: Optional[str] = None
    overall_status: Optional[str] = None   # set on the terminal event
    metadata_merge: Optional[Dict[str, Any]] = None

    def to_payload(self) -> Dict[str, Any]:
        # Paperclip expects camelCase.
        payload: Dict[str, Any] = {
            "step": self.step,
            "stepIndex": self.step_index,
            "status": self.status,
            "detail": self.detail,
            "error": self.error,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
        }
        if self.overall_status is not None:
            payload["overallStatus"] = self.overall_status
        if self.metadata_merge is not None:
            payload["metadataMerge"] = self.metadata_merge
        return payload


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def emit(
    event: JobEvent,
    *,
    paperclip_url: Optional[str] = None,
    bearer: Optional[str] = None,
) -> bool:
    """POST the event to Paperclip. Swallows errors + returns False on failure.

    Returns True when the HTTP call succeeded (2xx); False otherwise.
    Progress events are best-effort — the caller must NOT raise when this
    returns False, because the step itself may have succeeded.
    """
    url = (paperclip_url or PAPERCLIP_URL_DEFAULT).rstrip("/")
    # When we have a job id we post to the Phase-3 per-job events endpoint so
    # Paperclip can aggregate steps/metadata on the right row. When we don't
    # (e.g. manual CLI invocations without a pre-seeded job), we fall back
    # to a best-effort call against a legacy `/provisioning-events` endpoint
    # which is expected to 404 quietly — the executor still completes its
    # real work, it just won't show up in the UI.
    if event.job_id:
        target = (
            f"{url}/api/companies/{event.company_id}"
            f"/provisioning-jobs/{event.job_id}/events"
        )
    else:
        target = f"{url}/api/companies/{event.company_id}/provisioning-events"
    body = _json.dumps(event.to_payload()).encode("utf-8")
    headers = {"content-type": "application/json"}
    if bearer or PAPERCLIP_TOKEN:
        headers["authorization"] = f"Bearer {bearer or PAPERCLIP_TOKEN}"

    req = urllib.request.Request(target, data=body, headers=headers, method="POST")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - trusted localhost
            ms = int((time.perf_counter() - t0) * 1000)
            LOG.debug(
                "[jobs.emit] step=%s status=%s http=%d ms=%d",
                event.step, event.status, resp.status, ms,
            )
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as err:
        # Endpoint not yet deployed (404) or job row missing — log at DEBUG
        # so we don't spam the journal during Phase-3 rollout.
        LOG.warning(
            "[jobs.emit] step=%s http_err=%s body=%s",
            event.step, err.code, err.read()[:200] if hasattr(err, "read") else "",
        )
        return False
    except Exception as err:  # pragma: no cover
        LOG.warning("[jobs.emit] step=%s err=%s", event.step, err)
        return False


def new_event(
    *,
    company_id: str,
    step: str,
    step_index: int,
    status: str,
    detail: Optional[str] = None,
    error: Optional[str] = None,
    started_at: Optional[str] = None,
    job_id: Optional[str] = None,
    overall_status: Optional[str] = None,
    metadata_merge: Optional[Dict[str, Any]] = None,
) -> JobEvent:
    """Helper to build a JobEvent with sensible defaults (`finished_at=now`
    when status != running).
    """
    if status not in ALL_STATUSES:
        raise ValueError(f"invalid status {status!r}")
    finished = None if status == STATUS_RUNNING else _utc_now_iso()
    return JobEvent(
        company_id=company_id,
        step=step,
        step_index=step_index,
        status=status,
        detail=detail,
        error=error,
        started_at=started_at,
        finished_at=finished,
        job_id=job_id,
        overall_status=overall_status,
        metadata_merge=metadata_merge,
    )


__all__ = [
    "JobEvent",
    "emit",
    "new_event",
    "STATUS_RUNNING",
    "STATUS_OK",
    "STATUS_SKIPPED",
    "STATUS_FAILED",
    "ALL_STATUSES",
]
