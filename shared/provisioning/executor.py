"""Company provisioning executor — runs the nine atomic steps.

Public surface:

* ``run(company_id, slug, template_id, *, options)`` → ``ProvisionResult``
  Drives the nine steps end-to-end. Emits progress events to Paperclip.
  Automatically rolls back completed steps if a required step fails.

* ``rollback(company_id, slug, completed)`` → ``List[StepResult]``
  Reverses the given list of completed step results in reverse order.

The nine steps are (see ``templates/companies/README.md``):

  1. paperclip_row          — verify Paperclip row exists (created before us)
  2. postgres_db            — CREATE DATABASE tickles_<slug> + apply schema
  3. qdrant_collection      — PUT /collections/tickles_<slug>
  4. mem0_scopes            — write a bootstrap memory to confirm wiring
  5. memu_subscriptions     — stash subscription list in company metadata
  6. treasury_registration  — register venues in company metadata (Layer 2)
  7. install_skills         — Phase-4: install ClawHub skills (stub today)
  8. hire_agents            — POST /companies/:id/agents + OpenClaw dir clone
  9. register_routines      — Phase-6: wire autopsy/postmortem routines (stub)

Each step:
* emits a ``running`` event at start,
* performs idempotent work (safe to re-run on any existing company),
* emits ``ok`` / ``skipped`` / ``failed`` at end,
* returns a ``StepResult`` carrying enough info to reverse it.

Step failure policy:
* **Required** steps (2, 3, 4) cause the whole run to abort and roll back.
* **Best-effort** steps (5, 6, 7, 8, 9) log the error but continue — the
  company is left in a partially-provisioned state with its DB+Qdrant+
  memory intact so you can re-run provisioning or hire agents manually.

All HTTP is via stdlib urllib (no aiohttp dep). DB work uses psycopg2 when
available, otherwise falls back to ``psql`` via subprocess. Qdrant uses raw
HTTP. This keeps the executor runnable from inside the Tickles MCP daemon
without adding new Python dependencies.
"""

from __future__ import annotations

import asyncio
import contextvars
import json as _json
import logging
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .jobs import (
    JobEvent,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    emit,
    new_event,
    _utc_now_iso,
)
from .templates import Template, TemplateAgent, load as load_template

# Phase-3: thread job_id through the executor without having to change every
# step signature. Set by `run()` at the start of a provisioning run, read by
# `_emit()` so every step's progress events are posted to the right job row.
_CURRENT_JOB_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "tickles_provisioning_job_id",
    default=None,
)

LOG = logging.getLogger("tickles.provisioning.executor")

# ---- config / env knobs -----------------------------------------------------

PAPERCLIP_URL = os.environ.get("PAPERCLIP_URL", "http://127.0.0.1:3100").rstrip("/")
PAPERCLIP_TOKEN = os.environ.get("PAPERCLIP_API_TOKEN") or None

QDRANT_URL = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333").rstrip("/")
QDRANT_VECTOR_SIZE = int(os.environ.get("QDRANT_VECTOR_SIZE", "384"))

# Default system Postgres superuser + admin DB (for CREATE DATABASE).
# Matches openmemory.md: system Postgres on 127.0.0.1:5432 with peer auth.
POSTGRES_ADMIN_DSN = os.environ.get(
    "POSTGRES_ADMIN_DSN",
    "postgresql://postgres@127.0.0.1:5432/postgres",
)
POSTGRES_TEMPLATE_SQL = Path(
    os.environ.get(
        "TICKLES_COMPANY_SCHEMA_SQL",
        "/opt/tickles/shared/migration/tickles_company_pg.sql",
    )
)

OPENCLAW_AGENTS_DIR = Path(os.environ.get("OPENCLAW_AGENTS_DIR", "/root/.openclaw/agents"))
OPENCLAW_CONFIG_PATH = Path(os.environ.get("OPENCLAW_CONFIG_PATH", "/root/.openclaw/openclaw.json"))

# Overlay-file template tags — every generated file gets this header so humans
# can tell regenerable files apart from hand-edited ones. Files WITH this
# header are considered "owned" by the executor and may be overwritten on
# backfill. Files WITHOUT it are considered hand-edited and left alone unless
# the caller passes force_overwrite=True.
_GENERATED_HEADER = "<!-- generated-by: shared/provisioning/executor.py / phase5 -->"

# Paperclip's AGENT_ROLES enum is fixed:
#   ceo | cto | cmo | cfo | engineer | designer | pm | qa | devops
#   | researcher | general
# Historical templates (SurgeonCo, Polydesk, etc.) used "analyst", "observer",
# "member" which are NOT in the enum and cause Zod validation failures. This
# map keeps those older templates working and lets future templates use
# friendlier role names — original value is always preserved under
# metadata.templateRole so UI/analytics can surface it.
_ROLE_MAP: Dict[str, str] = {
    "analyst":   "researcher",
    "observer":  "general",
    "member":    "general",
    "quant":     "researcher",
    "ledger":    "general",
}


def _map_role_for_paperclip(raw: str) -> str:
    """Return a Paperclip-valid role for ``raw`` (passthrough if already valid)."""
    normalized = (raw or "").strip().lower()
    return _ROLE_MAP.get(normalized, normalized or "general")


# Optional belt-and-braces: even though Paperclip's own
# applyCreateDefaultsByAdapterType (Phase A) now auto-fills these from env vars
# /etc/paperclip/openclaw-gateway.env, the executor will *also* include them
# in the POST body when it has them. This way the executor keeps working if
# ever pointed at a Paperclip without the Phase A patch.
_GW_URL_ENV = os.environ.get("OPENCLAW_GATEWAY_URL")
_GW_TOKEN_ENV = os.environ.get("OPENCLAW_GATEWAY_TOKEN")


# ---- result shapes ----------------------------------------------------------


@dataclass
class StepResult:
    """Outcome of a single step, with enough info to reverse it."""

    step: str
    step_index: int
    status: str                                # ok | skipped | failed
    detail: Optional[str] = None
    error: Optional[str] = None
    undo: Dict[str, Any] = field(default_factory=dict)  # payload for rollback
    started_at: Optional[str] = None
    finished_at: Optional[str] = None

    def to_payload(self) -> Dict[str, Any]:
        return {
            "step": self.step,
            "stepIndex": self.step_index,
            "status": self.status,
            "detail": self.detail,
            "error": self.error,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
        }


@dataclass
class ProvisionResult:
    company_id: str
    slug: str
    template_id: str
    overall_status: str                        # ok | partial | failed
    steps: List[StepResult]
    started_at: str
    finished_at: str

    def to_payload(self) -> Dict[str, Any]:
        return {
            "companyId": self.company_id,
            "slug": self.slug,
            "templateId": self.template_id,
            "overallStatus": self.overall_status,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "steps": [s.to_payload() for s in self.steps],
        }


# ---- tiny HTTP helpers ------------------------------------------------------


def _http(method: str, url: str, *, body: Any = None, headers: Optional[Dict[str, str]] = None,
          timeout: float = 30.0) -> Tuple[int, bytes]:
    """Minimal sync HTTP. Returns (status_code, raw_body). Never raises."""
    h = {"content-type": "application/json"}
    if headers:
        h.update(headers)
    data = None if body is None else _json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read() if hasattr(err, "read") else b""
    except Exception as err:  # pragma: no cover
        return 0, str(err).encode("utf-8")


def _paperclip(method: str, path: str, *, body: Any = None) -> Tuple[int, Any]:
    headers = {}
    if PAPERCLIP_TOKEN:
        headers["authorization"] = f"Bearer {PAPERCLIP_TOKEN}"
    status, raw = _http(method, f"{PAPERCLIP_URL}{path}", body=body, headers=headers)
    try:
        parsed = _json.loads(raw) if raw else None
    except Exception:
        parsed = {"raw": raw.decode("utf-8", "replace")}
    return status, parsed


# ---- postgres admin helpers -------------------------------------------------


def _psql(sql: str, *, db: str = "postgres") -> Tuple[int, str]:
    """Run one SQL statement via `sudo -u postgres psql`. Returns (rc, stdout).

    We shell out instead of using psycopg2 so the executor has zero new deps.
    peer auth on the VPS lets `sudo -u postgres` connect without a password.
    """
    cmd = ["sudo", "-u", "postgres", "psql", "-d", db, "-tAc", sql]
    LOG.debug("[_psql] db=%s sql=%r", db, sql[:200])
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "psql timeout"
    except FileNotFoundError:
        return 127, "psql not found on PATH"


def _psql_file(path: Path, *, db: str) -> Tuple[int, str]:
    """Run a .sql file via psql -f."""
    cmd = ["sudo", "-u", "postgres", "psql", "-d", db, "-f", str(path)]
    LOG.debug("[_psql_file] db=%s file=%s", db, path)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "psql timeout"


def _database_exists(dbname: str) -> bool:
    rc, out = _psql(f"SELECT 1 FROM pg_database WHERE datname = '{dbname}'")
    return rc == 0 and out.strip() == "1"


# ---- helpers ----------------------------------------------------------------


def _slugify(name: str) -> str:
    """Coerce a company name to a safe slug usable as a Postgres db name and
    Qdrant collection suffix. Keeps alphanumerics + underscore."""
    s = "".join(c if c.isalnum() else "_" for c in name.lower()).strip("_")
    while "__" in s:
        s = s.replace("__", "_")
    return s or "company"


def _emit(company_id: str, step: str, idx: int, status: str,
          *, detail: Optional[str] = None, error: Optional[str] = None,
          started_at: Optional[str] = None,
          overall_status: Optional[str] = None,
          metadata_merge: Optional[Dict[str, Any]] = None) -> None:
    ev = new_event(
        company_id=company_id,
        step=step,
        step_index=idx,
        status=status,
        detail=detail,
        error=error,
        started_at=started_at,
        job_id=_CURRENT_JOB_ID.get(),
        overall_status=overall_status,
        metadata_merge=metadata_merge,
    )
    emit(ev)


# =============================================================================
# Step 1 — Paperclip row (verify)
# =============================================================================


def _step1_paperclip_row(company_id: str) -> StepResult:
    started = _utc_now_iso()
    _emit(company_id, "paperclip_row", 1, STATUS_RUNNING, started_at=started)
    status, body = _paperclip("GET", f"/api/companies/{company_id}")
    if status == 200 and body and body.get("id") == company_id:
        LOG.info("[step1] paperclip row verified id=%s", company_id)
        res = StepResult(
            step="paperclip_row", step_index=1, status=STATUS_OK,
            detail=f"company {body.get('name')!r} row present",
            undo={}, started_at=started, finished_at=_utc_now_iso(),
        )
    else:
        res = StepResult(
            step="paperclip_row", step_index=1, status=STATUS_FAILED,
            error=f"Paperclip returned http {status}",
            started_at=started, finished_at=_utc_now_iso(),
        )
    _emit(company_id, "paperclip_row", 1, res.status, detail=res.detail,
          error=res.error, started_at=started)
    return res


# =============================================================================
# Step 2 — Postgres DB
# =============================================================================


def _step2_postgres_db(company_id: str, slug: str) -> StepResult:
    started = _utc_now_iso()
    _emit(company_id, "postgres_db", 2, STATUS_RUNNING, started_at=started)
    dbname = f"tickles_{slug}"

    try:
        if _database_exists(dbname):
            LOG.info("[step2] db already exists: %s", dbname)
            res = StepResult(
                step="postgres_db", step_index=2, status=STATUS_OK,
                detail=f"db {dbname!r} already exists",
                undo={"dbname": dbname, "created_by_us": False},
                started_at=started, finished_at=_utc_now_iso(),
            )
        else:
            rc, out = _psql(f'CREATE DATABASE "{dbname}"')
            if rc != 0:
                raise RuntimeError(f"CREATE DATABASE failed: {out.strip()[:300]}")

            if not POSTGRES_TEMPLATE_SQL.exists():
                raise RuntimeError(f"schema template missing: {POSTGRES_TEMPLATE_SQL}")

            # The template uses `COMPANY_NAME` placeholder in `\c tickles_COMPANY_NAME`
            # and elsewhere. Materialise a rendered copy.
            rendered = POSTGRES_TEMPLATE_SQL.read_text(encoding="utf-8").replace(
                "COMPANY_NAME", slug
            )
            rendered_path = Path(f"/tmp/tickles_company_{slug}.sql")
            rendered_path.write_text(rendered, encoding="utf-8")
            rc, out = _psql_file(rendered_path, db=dbname)
            # don't fail on minor notices; check for real errors
            if rc != 0 and "ERROR" in out.upper():
                raise RuntimeError(f"schema apply failed: {out.strip()[:400]}")

            LOG.info("[step2] created db=%s + applied schema", dbname)
            res = StepResult(
                step="postgres_db", step_index=2, status=STATUS_OK,
                detail=f"db {dbname!r} created + schema applied",
                undo={"dbname": dbname, "created_by_us": True},
                started_at=started, finished_at=_utc_now_iso(),
            )
    except Exception as err:
        LOG.exception("[step2] failed slug=%s", slug)
        res = StepResult(
            step="postgres_db", step_index=2, status=STATUS_FAILED,
            error=str(err), started_at=started, finished_at=_utc_now_iso(),
        )
    _emit(company_id, "postgres_db", 2, res.status, detail=res.detail,
          error=res.error, started_at=started)
    return res


def _undo_step2(undo: Dict[str, Any]) -> None:
    if not undo.get("created_by_us"):
        return
    dbname = undo.get("dbname")
    if not dbname:
        return
    LOG.warning("[rollback.step2] dropping db=%s", dbname)
    _psql(f'DROP DATABASE IF EXISTS "{dbname}"')


# =============================================================================
# Step 3 — Qdrant collection
# =============================================================================


def _step3_qdrant_collection(company_id: str, slug: str) -> StepResult:
    started = _utc_now_iso()
    _emit(company_id, "qdrant_collection", 3, STATUS_RUNNING, started_at=started)
    collection = f"tickles_{slug}"
    url = f"{QDRANT_URL}/collections/{collection}"
    try:
        # Does it exist already?
        status, _ = _http("GET", url)
        if status == 200:
            LOG.info("[step3] qdrant collection already exists: %s", collection)
            res = StepResult(
                step="qdrant_collection", step_index=3, status=STATUS_OK,
                detail=f"collection {collection!r} already present",
                undo={"collection": collection, "created_by_us": False},
                started_at=started, finished_at=_utc_now_iso(),
            )
        else:
            body = {
                "vectors": {"size": QDRANT_VECTOR_SIZE, "distance": "Cosine"},
            }
            status, raw = _http("PUT", url, body=body)
            if not (200 <= status < 300):
                raise RuntimeError(
                    f"qdrant PUT failed http={status} body={raw[:200]!r}"
                )
            LOG.info("[step3] qdrant collection created: %s", collection)
            res = StepResult(
                step="qdrant_collection", step_index=3, status=STATUS_OK,
                detail=f"collection {collection!r} created size={QDRANT_VECTOR_SIZE}",
                undo={"collection": collection, "created_by_us": True},
                started_at=started, finished_at=_utc_now_iso(),
            )
    except Exception as err:
        LOG.exception("[step3] failed slug=%s", slug)
        res = StepResult(
            step="qdrant_collection", step_index=3, status=STATUS_FAILED,
            error=str(err), started_at=started, finished_at=_utc_now_iso(),
        )
    _emit(company_id, "qdrant_collection", 3, res.status, detail=res.detail,
          error=res.error, started_at=started)
    return res


def _undo_step3(undo: Dict[str, Any]) -> None:
    if not undo.get("created_by_us"):
        return
    col = undo.get("collection")
    if not col:
        return
    LOG.warning("[rollback.step3] deleting qdrant collection=%s", col)
    _http("DELETE", f"{QDRANT_URL}/collections/{col}")


# =============================================================================
# Step 4 — mem0 scopes bootstrap
# =============================================================================


def _step4_mem0_scopes(company_id: str, slug: str) -> StepResult:
    """Register the three-tier mem0 scopes for this company.

    mem0 doesn't require pre-registration; collections are created on first
    write and user/agent IDs are just strings. So this step is a *contract
    recorder* — it stashes the canonical scope identifiers in Paperclip's
    company metadata so other services know how to address this tenant's
    memory. Actual writes happen via the `user-mem0::add-memory` MCP tool
    called by agents.
    """
    started = _utc_now_iso()
    _emit(company_id, "mem0_scopes", 4, STATUS_RUNNING, started_at=started)
    try:
        scopes = {
            "collection": f"tickles_{slug}",
            "tier1AgentIdPrefix": f"{slug}_",      # tier-1 (agent-private)
            "tier2SharedAgentId": "shared",         # tier-2 (company-shared)
            "tier3Broadcast": "memu.insights",      # tier-3 (cross-company)
        }
        status, body = _paperclip(
            "PATCH",
            f"/api/companies/{company_id}",
            body={"metadata": {"mem0Scopes": scopes}},
        )
        if not (200 <= (status or 0) < 300):
            raise RuntimeError(f"paperclip PATCH metadata failed http={status}")
        LOG.info("[step4] mem0 scopes registered slug=%s", slug)
        res = StepResult(
            step="mem0_scopes", step_index=4, status=STATUS_OK,
            detail=f"registered scopes collection=tickles_{slug}",
            undo={"company_id": company_id, "metadata_key": "mem0Scopes"},
            started_at=started, finished_at=_utc_now_iso(),
        )
    except Exception as err:
        LOG.exception("[step4] failed slug=%s", slug)
        res = StepResult(
            step="mem0_scopes", step_index=4, status=STATUS_FAILED,
            error=str(err), started_at=started, finished_at=_utc_now_iso(),
        )
    _emit(company_id, "mem0_scopes", 4, res.status, detail=res.detail,
          error=res.error, started_at=started)
    return res


# =============================================================================
# Step 5 — MemU subscriptions
# =============================================================================


def _step5_memu_subscriptions(company_id: str, tpl: Template) -> StepResult:
    started = _utc_now_iso()
    _emit(company_id, "memu_subscriptions", 5, STATUS_RUNNING, started_at=started)
    if not tpl.memu_subscriptions:
        res = StepResult(
            step="memu_subscriptions", step_index=5, status=STATUS_SKIPPED,
            detail="template subscribes to nothing",
            started_at=started, finished_at=_utc_now_iso(),
        )
        _emit(company_id, "memu_subscriptions", 5, res.status,
              detail=res.detail, started_at=started)
        return res
    try:
        status, _ = _paperclip(
            "PATCH",
            f"/api/companies/{company_id}",
            body={"metadata": {"memuSubscriptions": list(tpl.memu_subscriptions)}},
        )
        if not (200 <= (status or 0) < 300):
            raise RuntimeError(f"paperclip PATCH failed http={status}")
        res = StepResult(
            step="memu_subscriptions", step_index=5, status=STATUS_OK,
            detail=f"subscribed to {len(tpl.memu_subscriptions)} topic(s)",
            undo={"company_id": company_id, "metadata_key": "memuSubscriptions"},
            started_at=started, finished_at=_utc_now_iso(),
        )
    except Exception as err:
        LOG.exception("[step5] failed company=%s", company_id)
        res = StepResult(
            step="memu_subscriptions", step_index=5, status=STATUS_FAILED,
            error=str(err), started_at=started, finished_at=_utc_now_iso(),
        )
    _emit(company_id, "memu_subscriptions", 5, res.status,
          detail=res.detail, error=res.error, started_at=started)
    return res


# =============================================================================
# Step 6 — Treasury registration (Layer 2)
# =============================================================================


def _step6_treasury_registration(company_id: str, tpl: Template) -> StepResult:
    started = _utc_now_iso()
    _emit(company_id, "treasury_registration", 6, STATUS_RUNNING, started_at=started)
    if not tpl.layer2_trading:
        res = StepResult(
            step="treasury_registration", step_index=6, status=STATUS_SKIPPED,
            detail="non-trading template (Layer-2 off)",
            started_at=started, finished_at=_utc_now_iso(),
        )
        _emit(company_id, "treasury_registration", 6, res.status,
              detail=res.detail, started_at=started)
        return res
    try:
        treasury_cfg = {
            "enabled": True,
            "ruleOneMode": tpl.rule_one_mode,
            "venues": list(tpl.venues),
            "registeredAt": _utc_now_iso(),
        }
        status, _ = _paperclip(
            "PATCH",
            f"/api/companies/{company_id}",
            body={"metadata": {"treasury": treasury_cfg}},
        )
        if not (200 <= (status or 0) < 300):
            raise RuntimeError(f"paperclip PATCH failed http={status}")
        res = StepResult(
            step="treasury_registration", step_index=6, status=STATUS_OK,
            detail=f"rule1={tpl.rule_one_mode} venues={tpl.venues}",
            undo={"company_id": company_id, "metadata_key": "treasury"},
            started_at=started, finished_at=_utc_now_iso(),
        )
    except Exception as err:
        LOG.exception("[step6] failed company=%s", company_id)
        res = StepResult(
            step="treasury_registration", step_index=6, status=STATUS_FAILED,
            error=str(err), started_at=started, finished_at=_utc_now_iso(),
        )
    _emit(company_id, "treasury_registration", 6, res.status,
          detail=res.detail, error=res.error, started_at=started)
    return res


# =============================================================================
# Step 7 — Install skills (Phase 4 stub)
# =============================================================================


def _step7_install_skills(company_id: str, tpl: Template) -> StepResult:
    started = _utc_now_iso()
    _emit(company_id, "install_skills", 7, STATUS_RUNNING, started_at=started)
    if not tpl.skills:
        res = StepResult(
            step="install_skills", step_index=7, status=STATUS_SKIPPED,
            detail="template declares no skills",
            started_at=started, finished_at=_utc_now_iso(),
        )
        _emit(company_id, "install_skills", 7, res.status,
              detail=res.detail, started_at=started)
        return res
    # For now we only record the intent. Phase 4 will call ClawHub to actually
    # install each skill via OpenClaw's skills API, then surface them in the
    # Paperclip company-skills page.
    try:
        status, _ = _paperclip(
            "PATCH",
            f"/api/companies/{company_id}",
            body={"metadata": {"pendingSkillInstalls": list(tpl.skills)}},
        )
        res = StepResult(
            step="install_skills", step_index=7, status=STATUS_SKIPPED,
            detail=f"Phase 4 — recorded {len(tpl.skills)} skill(s) for later install",
            undo={"company_id": company_id, "metadata_key": "pendingSkillInstalls"},
            started_at=started, finished_at=_utc_now_iso(),
        )
    except Exception as err:
        LOG.warning("[step7] metadata stash failed company=%s err=%s", company_id, err)
        res = StepResult(
            step="install_skills", step_index=7, status=STATUS_SKIPPED,
            detail="Phase 4 — could not stash pending skills",
            error=str(err),
            started_at=started, finished_at=_utc_now_iso(),
        )
    _emit(company_id, "install_skills", 7, res.status,
          detail=res.detail, error=res.error, started_at=started)
    return res


# =============================================================================
# Step 8 — Hire agents
# =============================================================================


def _openclaw_clone(from_key: str, to_key: str) -> Optional[str]:
    """Copy an OpenClaw agent directory template to a new urlKey.

    Returns the new directory path, or None when the source is missing (we
    skip OpenClaw provisioning rather than fail). Idempotent: if target dir
    already exists, we leave it alone.
    """
    src = OPENCLAW_AGENTS_DIR / from_key
    dst = OPENCLAW_AGENTS_DIR / to_key
    if not src.exists():
        LOG.warning("[openclaw_clone] source missing: %s (skipping)", src)
        return None
    if dst.exists():
        LOG.info("[openclaw_clone] target already exists: %s", dst)
        return str(dst)
    try:
        shutil.copytree(
            src, dst,
            # Don't copy runtime-only dirs (sessions is per-agent state)
            ignore=shutil.ignore_patterns("sessions", "*.lock", "*.jsonl"),
        )
        LOG.info("[openclaw_clone] %s -> %s", src, dst)
        return str(dst)
    except Exception as err:
        LOG.warning("[openclaw_clone] copy failed %s -> %s err=%s", src, dst, err)
        return None


def _write_overlay_if_allowed(
    path: Path,
    content: str,
    *,
    force_overwrite: bool,
) -> bool:
    """Write a markdown overlay file unless a hand-edited version already exists.

    A file is considered "hand-edited" if it exists and does NOT contain our
    `_GENERATED_HEADER` marker. We never clobber hand-edited files unless the
    caller passes force_overwrite=True.

    Returns True if the file was written, False if it was preserved.
    """
    try:
        if path.exists() and not force_overwrite:
            existing = path.read_text(encoding="utf-8", errors="ignore")
            if _GENERATED_HEADER not in existing:
                LOG.info(
                    "[overlay] preserving hand-edited file %s (no generated header)",
                    path,
                )
                return False
        path.write_text(content, encoding="utf-8")
        return True
    except Exception as err:
        LOG.warning("[overlay] write failed path=%s err=%s", path, err)
        return False


def _openclaw_customize(
    dst_dir: str,
    *,
    agent: TemplateAgent,
    slug: str,
    company_id: str,
    global_url_key: str,
    force_overwrite: bool = True,
) -> Dict[str, Any]:
    """Write the full 8-file OpenClaw overlay set + meta.json for an agent.

    Files written (each with a `<!-- generated-by -->` header so re-runs can
    tell regenerable files apart from hand-edited ones):

        AGENT.md       — high-level identity + workspace wiring
        SOUL.md        — persona/soul prompt (drives the agent's voice)
        IDENTITY.md    — who am I, who do I report to, company id/slug
        TOOLS.md       — MCP tool catalogue + skills available
        USER.md        — who the human user is + how to address them
        HEARTBEAT.md   — what to do on each heartbeat tick
        BOOTSTRAP.md   — first-run checklist (read AGENT.md, ping MCP, etc.)
        MEMORY.md      — three-tier mem0 contract + per-scope examples
        meta.json      — machine-readable wiring metadata

    Behaviour: best-effort. Returns a dict summarising which files were
    written vs preserved. Never raises — on any error we log and return
    {"error": str} so the caller (hire_one_agent) still succeeds.

    force_overwrite=True means "regenerate all generated files". Hand-edited
    files (those without our header) are still preserved either way — use a
    separate one-off script if you truly want to blow away hand edits.
    """
    result: Dict[str, Any] = {"written": [], "preserved": [], "errors": []}
    try:
        dst = Path(dst_dir)
        hdr = _GENERATED_HEADER
        skills_md = (
            "\n".join(f"- `{s}`" for s in agent.skills)
            if agent.skills
            else "- (none declared in template)"
        )

        # ---------- AGENT.md ----------
        agent_md = (
            f"{hdr}\n"
            f"# {agent.name} — {agent.role}\n\n"
            f"You are **{agent.name}**, the {agent.role} of company `{slug}` "
            f"(paperclip company_id=`{company_id}`). Your OpenClaw agent id is "
            f"`{global_url_key}`.\n\n"
            "## Who to read first\n\n"
            "1. `SOUL.md` — your persona and voice\n"
            "2. `IDENTITY.md` — where you sit in the org\n"
            "3. `TOOLS.md` — what you can call on the MCP\n"
            "4. `MEMORY.md` — how to read/write the three-tier memory\n"
            "5. `HEARTBEAT.md` — what to do each tick\n"
            "6. `BOOTSTRAP.md` — what to do on very first run\n\n"
            f"## Model\n\n`{agent.model}` (set via `adapterConfig.model`).\n\n"
            f"## Workspace\n\n"
            f"- Paperclip company DB: `tickles_{slug}` on 127.0.0.1:5432\n"
            f"- Qdrant collection: `tickles_{slug}`\n"
            f"- MCP control-plane: http://127.0.0.1:7777/mcp (JSON-RPC 2.0)\n"
            f"- Workspace dir: /root/.openclaw/workspace\n"
        )

        # ---------- SOUL.md ----------
        soul_body = (agent.soul or "").strip() or (
            "You are a competent, measured {role} who ships careful, reversible "
            "decisions. Prefer small steps, always check the last 3 learnings "
            "before acting, and never silently remove functionality.".format(
                role=agent.role,
            )
        )
        soul_md = (
            f"{hdr}\n"
            f"# {agent.name} — Soul\n\n"
            f"{soul_body}\n\n"
            "## Voice rules\n\n"
            "- First person, concise, 3-5 bullets when summarising.\n"
            "- Show your reasoning only when it changes the decision.\n"
            "- Never fabricate prices, positions, or policies — call the MCP tool.\n"
        )

        # ---------- IDENTITY.md ----------
        identity_md = (
            f"{hdr}\n"
            f"# {agent.name} — Identity\n\n"
            f"| field | value |\n"
            f"|---|---|\n"
            f"| name | `{agent.name}` |\n"
            f"| role | `{agent.role}` |\n"
            f"| openclaw agentId | `{global_url_key}` |\n"
            f"| paperclip companyId | `{company_id}` |\n"
            f"| company slug | `{slug}` |\n"
            f"| model (primary) | `{agent.model}` |\n"
            f"| budget (cents/mo) | `{agent.budget_monthly_cents}` |\n\n"
            "## Reports to\n\n"
            "Paperclip `agents.reportsTo` is the source of truth. Check it with "
            "the `agent.get` MCP tool, or with `GET /api/companies/{companyId}/"
            "agents/{agentId}` on Paperclip.\n"
        )

        # ---------- TOOLS.md ----------
        tools_md = (
            f"{hdr}\n"
            f"# {agent.name} — Tools\n\n"
            "## MCP control-plane\n\n"
            "Transport: JSON-RPC 2.0 over HTTP at `http://127.0.0.1:7777/mcp`.\n"
            "All arguments are **camelCase** (`companyId`, `agentId`, `jobId`).\n\n"
            "### Tool groups (live)\n\n"
            "- **Company/Agent lifecycle:** `company.create/get/list/pause/"
            "resume/delete/templates/provision`, `agent.create/get/list/pause/"
            "resume/delete`.\n"
            "- **Market/Data:** `catalog.list/get`, `md.quote`, `md.candles`, "
            "`altdata.search` (md.* are Phase-2.5 stubs until market-data "
            "gateway is mounted).\n"
            "- **Memory:** `memory.add`, `memory.search`, `memu.broadcast`, "
            "`memu.search`, `learnings.read_last_3`. See `MEMORY.md`.\n"
            "- **Trading:** `banker.snapshot` (real), `banker.positions`, "
            "`treasury.evaluate`, `execution.submit/cancel/status` (Phase-2.5 "
            "stubs until `shared/trading/*` is mounted).\n"
            "- **Learning loop:** `autopsy.run`, `postmortem.run`, "
            "`feedback.loop`, `feedback.prompts` (Twilly Templates 01/02/03).\n"
            "- **Ops:** `ping`.\n\n"
            "## Skills the template declared\n\n"
            f"{skills_md}\n\n"
            "Skills are advisory labels — the authoritative surface is the MCP "
            "tool list above. Call `tools/list` to discover live tools.\n"
        )

        # ---------- USER.md ----------
        user_md = (
            f"{hdr}\n"
            f"# {agent.name} — User Context\n\n"
            "The human user of this platform is the CEO of the holding "
            "company `Tickles n Co`. Address them as 'CEO' or 'boss' (their "
            "preference in openmemory). They value:\n\n"
            "- Plain-English explanations, no jargon.\n"
            "- Phased plans with explicit rollback steps before any change.\n"
            "- Less files, not long files — grouped by feature/functionality.\n"
            "- Logs of the form `[module.function] params=... -> result`.\n"
            "- Never silently remove code; comment-out with ROLLBACK note + "
            "roadmap pointer.\n\n"
            "When asked to do something destructive or ambiguous: **stop and "
            "ask first.**\n"
        )

        # ---------- HEARTBEAT.md ----------
        heartbeat_md = (
            f"{hdr}\n"
            f"# {agent.name} — Heartbeat\n\n"
            "On every heartbeat tick:\n\n"
            "1. Read `AGENT.md`, `SOUL.md`, `IDENTITY.md`, `MEMORY.md`.\n"
            "2. Call `learnings.read_last_3` (tier-1) — never skip this.\n"
            "3. List open Paperclip issues assigned to you (`agent.get` +"
            " Paperclip issues endpoint).\n"
            "4. If nothing changed and no issues are open: respond `nothing to "
            "do` and exit cleanly (don't burn tokens).\n"
            "5. If an issue is open: work exactly one step, write a tier-1 "
            "learning, and either close the issue or leave a progress note.\n"
            "6. For closed trades: run `autopsy.run`; for closed sessions: "
            "`postmortem.run`; each session ends with `feedback.loop`.\n"
            f"7. Stay within your monthly budget "
            f"({agent.budget_monthly_cents} cents).\n"
        )

        # ---------- BOOTSTRAP.md ----------
        bootstrap_md = (
            f"{hdr}\n"
            f"# {agent.name} — Bootstrap\n\n"
            "Very first run — before you do anything else:\n\n"
            "1. Call MCP `ping`. If it fails, halt and report.\n"
            "2. Call `banker.snapshot` with `companyId` from `IDENTITY.md` to "
            "confirm DB reachability.\n"
            "3. Call `memory.add` with `scope=\"agent\"`, a one-sentence "
            "\"hello, I am online\" content, and your ids. Confirm the "
            "returned `forward_to` payload.\n"
            "4. Call `feedback.prompts` to cache the Twilly templates 01/02/"
            "03 (autopsy / postmortem / feedback).\n"
            "5. Read any open issues assigned to you on Paperclip. If there's "
            "no open issue, idle politely and wait for the next heartbeat.\n"
        )

        # ---------- MEMORY.md ----------
        memory_md = (
            f"{hdr}\n"
            f"# {agent.name} — Memory Contract\n\n"
            "The MCP `memory.*` tools take a **tier literal** — NOT a "
            "namespace name. Always pass your resolved Paperclip ids as "
            "separate camelCase arguments.\n\n"
            "## Three tiers\n\n"
            "| Tier | Scope literal | Who can read | Who can write | Backing store |\n"
            "|---|---|---|---|---|\n"
            "| 1 | `agent` | me only | me only | mem0 over Qdrant `tickles_"
            f"{slug}` |\n"
            "| 2 | `company` | my company-mates | my company-mates | mem0 over "
            f"Qdrant `tickles_{slug}`, `agent_id='shared'` |\n"
            "| 3 | `building` | all companies | Strategy Council only | MemU "
            "(Postgres + pgvector) with `pg_notify('memu_broadcast', ...)` |\n\n"
            "## Examples\n\n"
            "```jsonc\n"
            "// Tier-1 write (my private learning)\n"
            "memory.add {\n"
            f"  \"scope\": \"agent\",\n"
            f"  \"companyId\": \"{company_id}\",\n"
            f"  \"agentId\": \"{global_url_key}\",\n"
            "  \"content\": \"Observation ...\",\n"
            "  \"metadata\": { \"topic\": \"regime\" }\n"
            "}\n\n"
            "// Tier-2 search (company-shared knowledge)\n"
            "memory.search {\n"
            "  \"scope\": \"company\",\n"
            f"  \"companyId\": \"{company_id}\",\n"
            "  \"query\": \"recent BTC regime shifts\"\n"
            "}\n\n"
            "// Tier-3 search (building-wide lessons)\n"
            "memory.search { \"scope\": \"building\", "
            "\"query\": \"crash guardrails that worked\" }\n"
            "```\n\n"
            f"## My ids\n\n"
            f"- `companyId` = `{company_id}`\n"
            f"- `agentId`   = `{global_url_key}`\n"
            f"- Qdrant collection = `tickles_{slug}`\n"
        )

        overlays: Dict[str, str] = {
            "AGENT.md": agent_md,
            "SOUL.md": soul_md,
            "IDENTITY.md": identity_md,
            "TOOLS.md": tools_md,
            "USER.md": user_md,
            "HEARTBEAT.md": heartbeat_md,
            "BOOTSTRAP.md": bootstrap_md,
            "MEMORY.md": memory_md,
        }
        for filename, body in overlays.items():
            if _write_overlay_if_allowed(
                dst / filename, body, force_overwrite=force_overwrite
            ):
                result["written"].append(filename)
            else:
                result["preserved"].append(filename)

        meta = {
            "agentId": global_url_key,
            "agentName": agent.name,
            "role": agent.role,
            "companyId": company_id,
            "companySlug": slug,
            "model": agent.model,
            "skills": list(agent.skills),
            "budgetMonthlyCents": agent.budget_monthly_cents,
            "createdByExecutor": True,
            "overlaySchema": "phase5",
        }
        (dst / "meta.json").write_text(
            _json.dumps(meta, indent=2), encoding="utf-8"
        )
        result["written"].append("meta.json")

        LOG.info(
            "[openclaw_customize] dir=%s written=%s preserved=%s",
            dst_dir, result["written"], result["preserved"],
        )
        return result
    except Exception as err:
        LOG.warning("[openclaw_customize] failed dir=%s err=%s", dst_dir, err)
        result["errors"].append(str(err))
        return result


def _openclaw_register_in_registry(
    *,
    global_url_key: str,
    agent: TemplateAgent,
    slug: str,
    heartbeat_every: Optional[str] = None,
) -> Dict[str, Any]:
    """Upsert an entry into `/root/.openclaw/openclaw.json` `agents.list[]` so
    the OpenClaw control-UI dropdown can see the agent.

    Entry shape (matches the 4 existing agents `main/cody/schemy/audrey`):

        { "id": "<global_url_key>",
          "model": {"primary": "<model>", "fallbacks": [...]},
          "heartbeat": {"every": "30m"},
          "tools": {"alsoAllow": ["lcm_describe","lcm_expand","lcm_grep",
                                  "agents_list"]},
          "paperclip": {"companySlug": "<slug>", "role": "<role>"}  # our tag
        }

    Behaviour:
      - If the agent id already exists in `agents.list[]`, we update in place
        (no duplicates).
      - A timestamped backup is written FIRST to
        `openclaw.json.bak.phase5-<ISO>` before any mutation, so rollback is
        `cp openclaw.json.bak.phase5-<ISO> openclaw.json`.
      - `heartbeat_every=None` means "do not set a heartbeat key" — matches
        Paperclip's default `runtimeConfig.heartbeat.enabled=false`. The
        CEO flips it on per-agent via the UI when they want periodic runs.
      - Best-effort: on any failure we log + return {"ok": False, "error":...}
        so the hire still counts.
    """
    cfg_path = OPENCLAW_CONFIG_PATH
    if not cfg_path.exists():
        LOG.warning(
            "[openclaw_register] config not found at %s — skipping registry",
            cfg_path,
        )
        return {"ok": False, "error": f"config missing: {cfg_path}"}

    try:
        raw = cfg_path.read_text(encoding="utf-8")
        cfg = _json.loads(raw)
    except Exception as err:
        LOG.warning("[openclaw_register] parse failed path=%s err=%s", cfg_path, err)
        return {"ok": False, "error": f"parse: {err}"}

    backup_path = cfg_path.with_name(
        f"openclaw.json.bak.phase5-{_utc_now_iso().replace(':', '-')}"
    )
    try:
        backup_path.write_text(raw, encoding="utf-8")
    except Exception as err:
        LOG.warning("[openclaw_register] backup failed path=%s err=%s", backup_path, err)
        return {"ok": False, "error": f"backup: {err}"}

    agents_block = cfg.setdefault("agents", {})
    agent_list: List[Dict[str, Any]] = agents_block.setdefault("list", [])

    # NOTE: OpenClaw's openclaw.json has a STRICT zod schema. We MUST only
    # use keys it recognises (`id`, `model`, `heartbeat`, `tools`). Adding an
    # unknown key (e.g. our own `paperclip` for slug/role tracking) crashes
    # the gateway with `Unrecognized key`. The companySlug + role + urlKey
    # mapping lives in a side-file we own at
    # `/root/.openclaw/tickles-meta-map.json` — updated below.
    entry: Dict[str, Any] = {
        "id": global_url_key,
        "model": {"primary": agent.model, "fallbacks": []},
        "tools": {
            "alsoAllow": [
                "lcm_describe",
                "lcm_expand",
                "lcm_expand_query",
                "lcm_grep",
                "agents_list",
            ]
        },
    }
    if heartbeat_every:
        entry["heartbeat"] = {"every": heartbeat_every}

    # Only these keys are allowed by OpenClaw's strict schema. We MUST filter
    # the existing entry through this allow-list so that any leftover junk
    # (e.g. a stale `paperclip` key from a pre-fix run, or something somebody
    # hand-added via the UI that we don't understand) gets stripped on the
    # next upsert — otherwise the gateway crash-loops again.
    _SCHEMA_ALLOWED_KEYS = {"id", "model", "heartbeat", "tools"}

    replaced = False
    for i, existing in enumerate(agent_list):
        if isinstance(existing, dict) and existing.get("id") == global_url_key:
            # Carry forward only the schema-safe keys from the existing
            # entry, then overlay our freshly-built entry.
            merged: Dict[str, Any] = {
                k: v for k, v in existing.items() if k in _SCHEMA_ALLOWED_KEYS
            }
            merged.update(entry)
            # Keep an existing heartbeat unless we were explicitly asked to set one.
            if not heartbeat_every and "heartbeat" in existing:
                merged["heartbeat"] = existing["heartbeat"]
            agent_list[i] = merged
            replaced = True
            break
    if not replaced:
        agent_list.append(entry)

    try:
        tmp_path = cfg_path.with_suffix(cfg_path.suffix + ".tmp")
        tmp_path.write_text(
            _json.dumps(cfg, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(tmp_path, cfg_path)
    except Exception as err:
        LOG.warning("[openclaw_register] write failed path=%s err=%s", cfg_path, err)
        return {"ok": False, "error": f"write: {err}", "backup": str(backup_path)}

    # Side-file: record slug/role/urlKey mapping. OpenClaw's schema rejects
    # unknown keys so we cannot embed this on the entry itself. Any tooling
    # that needs "which Paperclip company/role does OpenClaw id X belong to"
    # reads this file instead.
    side_map_path = cfg_path.with_name("tickles-meta-map.json")
    side_map: Dict[str, Any] = {}
    if side_map_path.exists():
        # Symmetric safety net: the main config gets a phase5 backup, the
        # side-file should too, so a bad write can be rolled back.
        try:
            side_bak = side_map_path.with_name(
                f"tickles-meta-map.json.bak.phase5-{_utc_now_iso().replace(':', '-')}"
            )
            side_bak.write_text(
                side_map_path.read_text(encoding="utf-8"), encoding="utf-8"
            )
        except Exception as err:
            LOG.warning(
                "[openclaw_register] side-map backup failed path=%s err=%s",
                side_map_path,
                err,
            )
        try:
            side_map = _json.loads(side_map_path.read_text(encoding="utf-8")) or {}
        except Exception as err:
            LOG.warning(
                "[openclaw_register] side-map parse failed path=%s err=%s",
                side_map_path,
                err,
            )
            side_map = {}
    side_map[global_url_key] = {
        "companySlug": slug,
        "role": agent.role,
        "urlKey": agent.url_key,
        "updatedAt": _utc_now_iso(),
    }
    try:
        tmp_side = side_map_path.with_suffix(side_map_path.suffix + ".tmp")
        tmp_side.write_text(_json.dumps(side_map, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp_side, side_map_path)
    except Exception as err:
        LOG.warning(
            "[openclaw_register] side-map write failed path=%s err=%s",
            side_map_path,
            err,
        )

    LOG.info(
        "[openclaw_register] upserted id=%s (%s) backup=%s side_map=%s",
        global_url_key,
        "replaced" if replaced else "appended",
        backup_path.name,
        side_map_path.name,
    )
    return {
        "ok": True,
        "replaced": replaced,
        "backup": str(backup_path),
        "id": global_url_key,
        "side_map": str(side_map_path),
    }


def _hire_one_agent(company_id: str, slug: str, agent: TemplateAgent) -> Tuple[bool, Dict[str, Any]]:
    """Create one agent in Paperclip + optionally clone an OpenClaw dir.

    Returns (success, detail_dict).
    """
    # Paperclip's adapter types: the canonical enum value is `openclaw_gateway`
    # (underscore, not hyphen) — see Paperclip's packages/shared/src/constants.ts
    # AGENT_ADAPTER_TYPES. Sending a hyphenated value results in a 422 with
    # "Unknown adapter type: openclaw-gateway".
    global_url_key = f"{slug}_{agent.url_key}"  # globally unique in OpenClaw

    paperclip_role = _map_role_for_paperclip(agent.role)
    adapter_config: Dict[str, Any] = {
        # `agentId` is the field Paperclip's openclaw-gateway adapter sends in
        # the wake payload (see paperclip/packages/adapters/openclaw-gateway/
        # src/server/execute.ts line ~1140: `agentParams.agentId = ctx.config.
        # agentId`). OpenClaw's gateway then uses it to locate the agent
        # directory at /root/.openclaw/agents/<agentId>/. We ALSO keep
        # `agentKey` for any tooling that reads it directly.
        "agentId": global_url_key,
        "agentKey": global_url_key,
        "model": agent.model,
    }
    # Belt-and-braces — Paperclip's Phase-A auto-defaults already fill these,
    # but including them here makes the executor portable to a Paperclip
    # without the Phase-A patch.
    if _GW_URL_ENV:
        adapter_config["url"] = _GW_URL_ENV
    if _GW_TOKEN_ENV:
        adapter_config["headers"] = {"x-openclaw-token": _GW_TOKEN_ENV}

    body = {
        "name": agent.name,
        "role": paperclip_role,
        "urlKey": agent.url_key,
        "adapterType": "openclaw_gateway",
        "adapterConfig": adapter_config,
        "runtimeConfig": {
            "soul": agent.soul,
        },
        "budgetMonthlyCents": agent.budget_monthly_cents,
        "metadata": {
            "templateAgent": True,
            "templateUrlKey": agent.url_key,
            "templateRole": agent.role,
            "skills": agent.skills,
        },
    }

    status, resp = _paperclip(
        "POST", f"/api/companies/{company_id}/agents", body=body,
    )
    if not (200 <= (status or 0) < 300):
        return False, {
            "urlKey": agent.url_key,
            "error": f"paperclip POST /agents http={status} body={resp}",
        }

    openclaw_dir = _openclaw_clone(agent.clone_openclaw_from, global_url_key)
    customize_result: Dict[str, Any] = {}
    register_result: Dict[str, Any] = {}
    if openclaw_dir:
        customize_result = _openclaw_customize(
            openclaw_dir,
            agent=agent,
            slug=slug,
            company_id=company_id,
            global_url_key=global_url_key,
        )
        # Register in openclaw.json so the GUI dropdown can see the agent.
        # heartbeat_every=None means "agent is event/manual-run driven" — the
        # CEO flips it on in the UI only if they want periodic runs. See the
        # Services vs Agents doc in ROADMAP_V3.md.
        register_result = _openclaw_register_in_registry(
            global_url_key=global_url_key,
            agent=agent,
            slug=slug,
            heartbeat_every=None,
        )
    return True, {
        "urlKey": agent.url_key,
        "globalUrlKey": global_url_key,
        "paperclipAgentId": (resp or {}).get("id"),
        "openclawDir": openclaw_dir,
        "overlay": customize_result,
        "openclawRegistry": register_result,
    }


def _step8_hire_agents(company_id: str, slug: str, tpl: Template) -> StepResult:
    started = _utc_now_iso()
    _emit(company_id, "hire_agents", 8, STATUS_RUNNING, started_at=started)
    if not tpl.agents:
        res = StepResult(
            step="hire_agents", step_index=8, status=STATUS_SKIPPED,
            detail="template hires no agents",
            started_at=started, finished_at=_utc_now_iso(),
        )
        _emit(company_id, "hire_agents", 8, res.status,
              detail=res.detail, started_at=started)
        return res

    hired: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    for a in tpl.agents:
        ok, info = _hire_one_agent(company_id, slug, a)
        (hired if ok else failed).append(info)

    status = STATUS_OK if not failed else (STATUS_FAILED if not hired else STATUS_OK)
    detail = f"hired {len(hired)}/{len(tpl.agents)} agents"
    if failed:
        detail += f" — failed: {[f['urlKey'] for f in failed]}"
    res = StepResult(
        step="hire_agents", step_index=8, status=status, detail=detail,
        error=_json.dumps(failed) if failed else None,
        undo={"hired": hired},
        started_at=started, finished_at=_utc_now_iso(),
    )
    _emit(company_id, "hire_agents", 8, res.status,
          detail=res.detail, error=res.error, started_at=started)
    return res


def _undo_step8(undo: Dict[str, Any]) -> None:
    for info in undo.get("hired", []) or []:
        agent_id = info.get("paperclipAgentId")
        if agent_id:
            LOG.warning("[rollback.step8] deleting paperclip agent=%s", agent_id)
            _paperclip("DELETE", f"/api/agents/{agent_id}")
        oc_dir = info.get("openclawDir")
        if oc_dir and Path(oc_dir).exists():
            LOG.warning("[rollback.step8] removing openclaw dir=%s", oc_dir)
            shutil.rmtree(oc_dir, ignore_errors=True)


# =============================================================================
# Step 9 — Register routines (Phase 6 stub)
# =============================================================================


def _step9_register_routines(company_id: str, tpl: Template) -> StepResult:
    started = _utc_now_iso()
    _emit(company_id, "register_routines", 9, STATUS_RUNNING, started_at=started)
    if not tpl.routines:
        res = StepResult(
            step="register_routines", step_index=9, status=STATUS_SKIPPED,
            detail="template declares no routines",
            started_at=started, finished_at=_utc_now_iso(),
        )
        _emit(company_id, "register_routines", 9, res.status,
              detail=res.detail, started_at=started)
        return res
    # Phase-6 will actually wire up the autopsy/postmortem/feedback_loop
    # routines against Paperclip's routines API. For now we record intent.
    try:
        status, _ = _paperclip(
            "PATCH",
            f"/api/companies/{company_id}",
            body={"metadata": {
                "pendingRoutines": [
                    {"kind": r.kind, "trigger": r.trigger} for r in tpl.routines
                ],
            }},
        )
        res = StepResult(
            step="register_routines", step_index=9, status=STATUS_SKIPPED,
            detail=f"Phase 6 — recorded {len(tpl.routines)} routine(s) for later",
            undo={"company_id": company_id, "metadata_key": "pendingRoutines"},
            started_at=started, finished_at=_utc_now_iso(),
        )
    except Exception as err:
        res = StepResult(
            step="register_routines", step_index=9, status=STATUS_SKIPPED,
            detail="Phase 6 — could not stash pending routines",
            error=str(err),
            started_at=started, finished_at=_utc_now_iso(),
        )
    _emit(company_id, "register_routines", 9, res.status,
          detail=res.detail, error=res.error, started_at=started)
    return res


# =============================================================================
# Orchestrator
# =============================================================================


REQUIRED_STEPS = {"postgres_db", "qdrant_collection", "mem0_scopes"}


def _compute_overall(results: List[StepResult]) -> str:
    if any(r.step in REQUIRED_STEPS and r.status == STATUS_FAILED for r in results):
        return "failed"
    if any(r.status == STATUS_FAILED for r in results):
        return "partial"
    return "ok"


async def run(
    *,
    company_id: str,
    slug: str,
    template_id: str,
    job_id: Optional[str] = None,
) -> ProvisionResult:
    """End-to-end provisioning. Runs sync step functions inside a thread so
    the MCP daemon's event loop is not blocked by psql/HTTP calls.

    If ``job_id`` is provided (normal path — seeded by Paperclip's POST
    handler) every step event is posted to
    ``/api/companies/<company_id>/provisioning-jobs/<job_id>/events`` so the
    UI poller picks it up. Without a job_id the run still executes (CLI
    mode), but progress is not visible in Paperclip.
    """
    # Set the contextvar FIRST so that even if template loading fails we can
    # still emit a failed-run terminal event to the correct job row.
    token = _CURRENT_JOB_ID.set(job_id)
    overall_started = _utc_now_iso()
    try:
        tpl = load_template(template_id)
    except Exception as err:
        LOG.exception("[run] template load failed template_id=%s", template_id)
        # Emit a best-effort terminal event so Paperclip's UI doesn't spin on
        # "running" forever. Use step=paperclip_row/stepIndex=1 as the carrier
        # since no real step has started.
        try:
            _emit(
                company_id, "paperclip_row", 1, STATUS_FAILED,
                detail=f"template {template_id!r} load failed",
                error=str(err), overall_status="failed",
            )
        finally:
            _CURRENT_JOB_ID.reset(token)
        return ProvisionResult(
            company_id=company_id, slug=slug, template_id=template_id,
            overall_status="failed", steps=[],
            started_at=overall_started, finished_at=_utc_now_iso(),
        )
    slug = _slugify(slug) if slug else _slugify(tpl.id)
    LOG.info(
        "[run] starting company_id=%s slug=%s template=%s category=%s layer2=%s job_id=%s",
        company_id, slug, template_id, tpl.category, tpl.layer2_trading, job_id,
    )

    results: List[StepResult] = []

    # NOTE: asyncio.to_thread() copies the current contextvars into the
    # worker thread, so _CURRENT_JOB_ID (set a few lines up via `token`)
    # propagates into every step's `_emit()` calls. `loop.run_in_executor`
    # does NOT copy context, which is why the previous implementation was
    # dropping job_id on step events and only the terminal event (which
    # ran on the event-loop thread) was reaching the right URL.
    async def _call(fn, *args):
        return await asyncio.to_thread(fn, *args)

    def _add(r: StepResult) -> StepResult:
        results.append(r)
        return r

    def _emit_terminal(overall: str, last_step: str, last_idx: int) -> None:
        """Emit a final marker event so Paperclip flips the job's
        overall_status + finished_at. Uses the last step as the carrier so
        the UI shows a sane "last action" label."""
        try:
            _emit(
                company_id,
                last_step,
                last_idx,
                STATUS_OK if overall == "ok" else (
                    STATUS_SKIPPED if overall == "partial" else STATUS_FAILED
                ),
                detail=f"provisioning {overall}",
                overall_status=overall,
            )
        except Exception as err:  # pragma: no cover
            LOG.warning("[run] terminal emit failed: %s", err)

    try:
        # Step 1
        _add(await _call(_step1_paperclip_row, company_id))
        if results[-1].status == STATUS_FAILED:
            await _call(_rollback_sync, company_id, slug, results)
            _emit_terminal("failed", "paperclip_row", 1)
            return ProvisionResult(
                company_id=company_id, slug=slug, template_id=template_id,
                overall_status="failed", steps=results,
                started_at=overall_started, finished_at=_utc_now_iso(),
            )

        # Step 2 (required)
        r2 = _add(await _call(_step2_postgres_db, company_id, slug))
        if r2.status == STATUS_FAILED:
            await _call(_rollback_sync, company_id, slug, results)
            _emit_terminal("failed", "postgres_db", 2)
            return ProvisionResult(
                company_id=company_id, slug=slug, template_id=template_id,
                overall_status="failed", steps=results,
                started_at=overall_started, finished_at=_utc_now_iso(),
            )

        # Step 3 (required)
        r3 = _add(await _call(_step3_qdrant_collection, company_id, slug))
        if r3.status == STATUS_FAILED:
            await _call(_rollback_sync, company_id, slug, results)
            _emit_terminal("failed", "qdrant_collection", 3)
            return ProvisionResult(
                company_id=company_id, slug=slug, template_id=template_id,
                overall_status="failed", steps=results,
                started_at=overall_started, finished_at=_utc_now_iso(),
            )

        # Step 4 (required)
        r4 = _add(await _call(_step4_mem0_scopes, company_id, slug))
        if r4.status == STATUS_FAILED:
            await _call(_rollback_sync, company_id, slug, results)
            _emit_terminal("failed", "mem0_scopes", 4)
            return ProvisionResult(
                company_id=company_id, slug=slug, template_id=template_id,
                overall_status="failed", steps=results,
                started_at=overall_started, finished_at=_utc_now_iso(),
            )

        # Step 5 — best-effort
        _add(await _call(_step5_memu_subscriptions, company_id, tpl))

        # Step 6 — best-effort
        _add(await _call(_step6_treasury_registration, company_id, tpl))

        # Step 7 — best-effort (stub)
        _add(await _call(_step7_install_skills, company_id, tpl))

        # Step 8 — best-effort
        _add(await _call(_step8_hire_agents, company_id, slug, tpl))

        # Step 9 — best-effort (stub)
        _add(await _call(_step9_register_routines, company_id, tpl))

        overall = _compute_overall(results)
        LOG.info("[run] finished company_id=%s overall=%s", company_id, overall)
        _emit_terminal(overall, "register_routines", 9)
        return ProvisionResult(
            company_id=company_id, slug=slug, template_id=template_id,
            overall_status=overall, steps=results,
            started_at=overall_started, finished_at=_utc_now_iso(),
        )
    finally:
        _CURRENT_JOB_ID.reset(token)


# =============================================================================
# Rollback
# =============================================================================


UNDO_MAP = {
    "postgres_db": _undo_step2,
    "qdrant_collection": _undo_step3,
    "hire_agents": _undo_step8,
    # mem0_scopes / memu_subscriptions / treasury_registration are metadata
    # writes — rolling them back is not critical and would fight with a
    # retry. Skip in rollback; a full `company.delete` drops the row.
}


def _rollback_sync(company_id: str, slug: str, results: List[StepResult]) -> None:
    """Reverse completed steps in reverse order. Best-effort."""
    LOG.warning("[rollback] starting company_id=%s slug=%s steps=%d",
                company_id, slug, len(results))
    for r in reversed(results):
        if r.status != STATUS_OK:
            continue
        fn = UNDO_MAP.get(r.step)
        if not fn:
            continue
        try:
            fn(r.undo)
        except Exception as err:  # pragma: no cover
            LOG.error("[rollback] step=%s err=%s", r.step, err)


async def rollback(company_id: str, slug: str, completed: List[StepResult]) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _rollback_sync, company_id, slug, completed)
