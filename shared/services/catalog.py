"""
shared.services.catalog — Phase 24 persistent Services Catalog.

The 21-year-old version
-----------------------
Phase 22 gave us an in-process :data:`SERVICE_REGISTRY` listing every
long-running Tickles service. That lives in Python memory — fine for
``services_cli list`` but useless for a dashboard or for another
process that wants to ask "is the candle-daemon healthy right now?".

Phase 24 mirrors that registry into ``public.services_catalog`` so:

* operators can query state with plain SQL,
* a future Owner Dashboard (Phase 36) has a clean table to render,
* rollups (last systemd state + last auditor heartbeat) live next
  to static registry data.

This module is deliberately thin — it does not *own* anything. The
registry owns descriptors; the auditor owns heartbeats; systemd owns
runtime state. We just upsert a snapshot.

Pool contract
-------------
Anything passed as ``pool`` here must implement the same async API as
:class:`shared.utils.db.DatabasePool`:

    await pool.execute(sql, params) -> int
    await pool.fetch_all(sql, params) -> List[Dict[str, Any]]
    await pool.fetch_one(sql, params) -> Optional[Dict[str, Any]]
    await pool.execute_many(sql, params_list) -> int

Tests use a :class:`_InMemoryPool` fake; production wires a real
asyncpg pool via :func:`shared.utils.db.get_shared_pool`.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from shared.services.registry import (
    SERVICE_REGISTRY,
    ServiceDescriptor,
    ServiceRegistry,
)

logger = logging.getLogger("tickles.services.catalog")

MIGRATION_PATH = str(
    Path(__file__).resolve().parent / "migrations" / "2026_04_19_phase24_services_catalog.sql"
)

# ---------------------------------------------------------------------------
# Small immutable records returned by the catalog.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SystemdState:
    """Parsed output from ``systemctl show``."""

    unit: str
    active_state: Optional[str] = None
    sub_state: Optional[str] = None
    active_enter_ts: Optional[datetime] = None
    load_state: Optional[str] = None

    def as_update_row(self) -> Dict[str, Any]:
        return {
            "last_systemd_state": self.active_state,
            "last_systemd_substate": self.sub_state,
            "last_systemd_active_enter_ts": self.active_enter_ts,
        }


@dataclass
class HeartbeatMark:
    """A digested view of the auditor's last heartbeat for one service."""

    service: str
    ts: datetime
    severity: str
    details: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# systemctl parsing — pure, so it can be unit-tested with fixture text.
# ---------------------------------------------------------------------------

_SYSTEMCTL_PROPERTIES = (
    "ActiveState",
    "SubState",
    "ActiveEnterTimestamp",
    "LoadState",
)


def _parse_systemctl_timestamp(raw: str) -> Optional[datetime]:
    """systemctl prints e.g. 'Sat 2026-04-19 05:27:11 UTC' or '' when unknown."""
    if not raw or raw.strip() in {"", "n/a", "0"}:
        return None
    # Known formats we see in practice
    candidates = (
        "%a %Y-%m-%d %H:%M:%S %Z",
        "%Y-%m-%d %H:%M:%S %Z",
        "%Y-%m-%d %H:%M:%S",
    )
    for fmt in candidates:
        try:
            dt = datetime.strptime(raw.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def parse_systemctl_show(unit: str, text: str) -> SystemdState:
    """Parse the key=value output from ``systemctl show -p <props> <unit>``."""
    props: Dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        props[k.strip()] = v.strip()
    return SystemdState(
        unit=unit,
        active_state=props.get("ActiveState") or None,
        sub_state=props.get("SubState") or None,
        active_enter_ts=_parse_systemctl_timestamp(props.get("ActiveEnterTimestamp", "")),
        load_state=props.get("LoadState") or None,
    )


def default_systemctl_runner(unit: str) -> str:
    """Run ``systemctl show`` and return stdout text.

    Returns an empty string when systemctl isn't available (non-Linux boxes,
    stripped CI images, tests). Callers must treat empty text as "unknown".
    """
    if shutil.which("systemctl") is None:
        return ""
    try:
        completed = subprocess.run(
            [
                "systemctl",
                "show",
                "-p",
                ",".join(_SYSTEMCTL_PROPERTIES),
                unit,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        return completed.stdout or ""
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("systemctl show %s failed: %s", unit, exc)
        return ""


# Injectable for tests.
SystemctlRunner = Callable[[str], str]


# ---------------------------------------------------------------------------
# Core catalog class.
# ---------------------------------------------------------------------------


def _descriptor_to_sync_params(
    desc: ServiceDescriptor,
) -> Tuple[Any, ...]:
    tags_json = json.dumps(dict(desc.tags), sort_keys=True)
    phase = desc.tags.get("phase") if desc.tags else None
    systemd_unit = desc.systemd_unit or f"tickles-{desc.name}.service"
    return (
        desc.name,
        desc.kind,
        desc.module,
        desc.description,
        systemd_unit,
        desc.enabled_on_vps,
        desc.factory is not None,
        phase,
        tags_json,
    )


_UPSERT_SQL = """
INSERT INTO public.services_catalog (
    name, kind, module, description, systemd_unit, enabled_on_vps,
    has_factory, phase, tags, first_registered_at, last_seen_at
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, now(), now()
)
ON CONFLICT (name) DO UPDATE SET
    kind = EXCLUDED.kind,
    module = EXCLUDED.module,
    description = EXCLUDED.description,
    systemd_unit = EXCLUDED.systemd_unit,
    enabled_on_vps = EXCLUDED.enabled_on_vps,
    has_factory = EXCLUDED.has_factory,
    phase = EXCLUDED.phase,
    tags = EXCLUDED.tags,
    last_seen_at = now()
"""


_UPDATE_SYSTEMD_SQL = """
UPDATE public.services_catalog SET
    last_systemd_state = $2,
    last_systemd_substate = $3,
    last_systemd_active_enter_ts = $4,
    last_seen_at = now()
WHERE name = $1
"""


_UPDATE_HEARTBEAT_SQL = """
UPDATE public.services_catalog SET
    last_heartbeat_ts = $2,
    last_heartbeat_severity = $3,
    last_seen_at = now()
WHERE name = $1
"""


_INSERT_SNAPSHOT_SQL = """
INSERT INTO public.services_catalog_snapshots (
    name, ts, systemd_state, systemd_substate,
    systemd_active_enter_ts, last_heartbeat_ts,
    last_heartbeat_severity, metadata
) VALUES (
    $1, now(), $2, $3, $4, $5, $6, $7::jsonb
)
"""


_SELECT_ALL_SQL = """
SELECT * FROM public.services_catalog
ORDER BY kind, name
"""


_SELECT_BY_KIND_SQL = """
SELECT * FROM public.services_catalog
WHERE kind = $1
ORDER BY name
"""


_SELECT_ONE_SQL = """
SELECT * FROM public.services_catalog
WHERE name = $1
"""


class ServicesCatalog:
    """DB-backed catalog mirror of :data:`SERVICE_REGISTRY`.

    Callers pass in any object implementing the
    :class:`shared.utils.db.DatabasePool` async surface.
    """

    def __init__(
        self,
        registry: Optional[ServiceRegistry] = None,
        systemctl_runner: Optional[SystemctlRunner] = None,
    ) -> None:
        self._registry = registry or SERVICE_REGISTRY
        self._systemctl_runner: SystemctlRunner = (
            systemctl_runner or default_systemctl_runner
        )

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def sync_registry(self, pool: Any) -> int:
        """Upsert every descriptor currently in the registry. Returns count."""
        descriptors = list(self._registry.list_services())
        if not descriptors:
            return 0
        rows = [_descriptor_to_sync_params(d) for d in descriptors]
        # asyncpg-compatible pools accept execute_many with $-style placeholders.
        await pool.execute_many(_UPSERT_SQL, rows)
        logger.info("services_catalog: synced %d descriptors", len(rows))
        return len(rows)

    async def snapshot_systemd(
        self,
        pool: Any,
        names: Optional[Sequence[str]] = None,
        also_append_history: bool = True,
    ) -> List[Dict[str, Any]]:
        """Capture systemd state for each named service and update the DB.

        Returns a list of dicts (one per service) describing what we observed
        — useful for the CLI layer and tests.
        """
        if names is None:
            names = [d.name for d in self._registry.list_services()]
        results: List[Dict[str, Any]] = []
        for name in names:
            try:
                desc = self._registry.get(name)
            except KeyError:
                logger.warning("snapshot_systemd: unknown service %s", name)
                continue
            unit = desc.systemd_unit or f"tickles-{desc.name}.service"
            raw = self._systemctl_runner(unit)
            state = parse_systemctl_show(unit, raw)
            await pool.execute(
                _UPDATE_SYSTEMD_SQL,
                (name, state.active_state, state.sub_state, state.active_enter_ts),
            )
            if also_append_history:
                await pool.execute(
                    _INSERT_SNAPSHOT_SQL,
                    (
                        name,
                        state.active_state,
                        state.sub_state,
                        state.active_enter_ts,
                        None,
                        None,
                        json.dumps({"unit": unit, "load_state": state.load_state}),
                    ),
                )
            results.append(
                {
                    "name": name,
                    "unit": unit,
                    "active_state": state.active_state,
                    "sub_state": state.sub_state,
                    "active_enter_ts": (
                        state.active_enter_ts.isoformat()
                        if state.active_enter_ts is not None
                        else None
                    ),
                    "load_state": state.load_state,
                }
            )
        return results

    async def attach_heartbeats(
        self,
        pool: Any,
        heartbeats: Sequence[HeartbeatMark],
        also_append_history: bool = True,
    ) -> int:
        """Update ``last_heartbeat_*`` columns from a pre-digested list.

        The auditor CLI already knows how to read the SQLite store, so we
        don't want to duplicate that logic here — callers pass in parsed
        :class:`HeartbeatMark` instances and we just stamp the DB.
        """
        if not heartbeats:
            return 0
        for hb in heartbeats:
            await pool.execute(
                _UPDATE_HEARTBEAT_SQL,
                (hb.service, hb.ts, hb.severity),
            )
            if also_append_history:
                await pool.execute(
                    _INSERT_SNAPSHOT_SQL,
                    (
                        hb.service,
                        None,
                        None,
                        None,
                        hb.ts,
                        hb.severity,
                        json.dumps(hb.details, sort_keys=True, default=str),
                    ),
                )
        return len(heartbeats)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def list_services(
        self, pool: Any, kind: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        if kind is None:
            rows = await pool.fetch_all(_SELECT_ALL_SQL, ())
        else:
            rows = await pool.fetch_all(_SELECT_BY_KIND_SQL, (kind,))
        return [_normalize_row(r) for r in rows]

    async def describe_service(
        self, pool: Any, name: str
    ) -> Optional[Dict[str, Any]]:
        row = await pool.fetch_one(_SELECT_ONE_SQL, (name,))
        if row is None:
            return None
        return _normalize_row(row)


def _normalize_row(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Convert DB row to JSON-friendly dict (datetimes → iso, jsonb → dict)."""
    out: Dict[str, Any] = {}
    for k, v in raw.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, (bytes, bytearray)):
            out[k] = v.decode("utf-8", errors="replace")
        elif k in ("tags", "metadata") and isinstance(v, str):
            try:
                out[k] = json.loads(v)
            except json.JSONDecodeError:
                out[k] = v
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Heartbeat helper — extracts HeartbeatMark(s) from a Phase 21 AuditStore.
# Kept here rather than inside the auditor so that package has zero new deps.
# ---------------------------------------------------------------------------


def extract_heartbeats_from_audit(
    audit_store: Any,
    service_names: Sequence[str],
    window_seconds: int = 3600,
) -> List[HeartbeatMark]:
    """Pull the most-recent heartbeat row per service from the auditor store.

    ``audit_store`` is duck-typed: any object with a ``list_recent`` method
    matching :class:`shared.auditor.storage.AuditStore.list_recent` works.
    """
    try:
        from shared.auditor.schema import AuditEventType  # lazy import
    except ImportError:
        logger.warning("auditor schema unavailable; skipping heartbeat extract")
        return []

    marks: Dict[str, HeartbeatMark] = {}
    min_ts = None
    import time

    if window_seconds and window_seconds > 0:
        min_ts = time.time() - window_seconds

    records = audit_store.list_recent(
        limit=500,
        event_type=AuditEventType.HEARTBEAT,
        min_ts_unix=min_ts,
    )
    wanted = set(service_names)
    for rec in records:
        subject = getattr(rec, "subject", None) or ""
        if subject not in wanted:
            continue
        if subject in marks:
            continue  # we already have the most recent (records are DESC)
        ts_unix = float(getattr(rec, "ts_unix", 0.0) or 0.0)
        ts = datetime.fromtimestamp(ts_unix, tz=timezone.utc) if ts_unix else datetime.now(timezone.utc)
        severity = getattr(rec, "severity", None)
        sev_val = getattr(severity, "value", None) or str(severity or "ok")
        details = getattr(rec, "details", None) or {}
        marks[subject] = HeartbeatMark(
            service=subject,
            ts=ts,
            severity=sev_val,
            details=dict(details) if isinstance(details, dict) else {},
        )
    return list(marks.values())


# ---------------------------------------------------------------------------
# In-memory pool — production-grade enough for tests, dev loops and the
# `services_catalog_cli snapshot --dry-run` path. Not exported at package
# level to keep the public surface clean.
# ---------------------------------------------------------------------------


class _InMemoryPool:
    """Tiny async pool stub backed by Python dicts.

    Implements just enough of the DatabasePool contract for the catalog
    module's happy paths. Primary key on ``services_catalog.name``; the
    snapshot table is append-only.
    """

    def __init__(self) -> None:
        self.catalog: Dict[str, Dict[str, Any]] = {}
        self.snapshots: List[Dict[str, Any]] = []

    async def execute(self, sql: str, params: Sequence[Any]) -> int:
        sql = sql.strip()
        if sql.startswith("INSERT INTO public.services_catalog_snapshots"):
            (name, systemd_state, substate, active_enter_ts,
             last_hb_ts, last_hb_sev, metadata_json) = params
            self.snapshots.append(
                {
                    "name": name,
                    "ts": datetime.now(timezone.utc),
                    "systemd_state": systemd_state,
                    "systemd_substate": substate,
                    "systemd_active_enter_ts": active_enter_ts,
                    "last_heartbeat_ts": last_hb_ts,
                    "last_heartbeat_severity": last_hb_sev,
                    "metadata": json.loads(metadata_json) if metadata_json else {},
                }
            )
            return 1
        if sql.startswith("UPDATE public.services_catalog SET\n    last_systemd_state"):
            name, state, sub, enter_ts = params
            row = self.catalog.get(name)
            if row is None:
                return 0
            row["last_systemd_state"] = state
            row["last_systemd_substate"] = sub
            row["last_systemd_active_enter_ts"] = enter_ts
            row["last_seen_at"] = datetime.now(timezone.utc)
            return 1
        if sql.startswith("UPDATE public.services_catalog SET\n    last_heartbeat_ts"):
            name, ts, sev = params
            row = self.catalog.get(name)
            if row is None:
                return 0
            row["last_heartbeat_ts"] = ts
            row["last_heartbeat_severity"] = sev
            row["last_seen_at"] = datetime.now(timezone.utc)
            return 1
        raise NotImplementedError(f"_InMemoryPool.execute: unsupported sql:\n{sql}")

    async def execute_many(
        self, sql: str, rows: Sequence[Sequence[Any]]
    ) -> int:
        sql = sql.strip()
        if sql.startswith("INSERT INTO public.services_catalog ("):
            now = datetime.now(timezone.utc)
            for r in rows:
                (name, kind, module, desc, unit, enabled,
                 has_factory, phase, tags_json) = r
                existing = self.catalog.get(name)
                base = existing or {
                    "name": name,
                    "first_registered_at": now,
                    "last_heartbeat_ts": None,
                    "last_heartbeat_severity": None,
                    "last_systemd_state": None,
                    "last_systemd_substate": None,
                    "last_systemd_active_enter_ts": None,
                    "metadata": {},
                }
                base.update(
                    {
                        "kind": kind,
                        "module": module,
                        "description": desc,
                        "systemd_unit": unit,
                        "enabled_on_vps": enabled,
                        "has_factory": has_factory,
                        "phase": phase,
                        "tags": json.loads(tags_json) if tags_json else {},
                        "last_seen_at": now,
                    }
                )
                self.catalog[name] = base
            return len(rows)
        raise NotImplementedError(f"_InMemoryPool.execute_many unsupported sql:\n{sql}")

    async def fetch_all(
        self, sql: str, params: Sequence[Any]
    ) -> List[Dict[str, Any]]:
        sql = sql.strip()
        if sql.startswith("SELECT * FROM public.services_catalog\nORDER BY kind"):
            return sorted(
                (dict(v) for v in self.catalog.values()),
                key=lambda r: (r.get("kind", ""), r.get("name", "")),
            )
        if sql.startswith("SELECT * FROM public.services_catalog\nWHERE kind"):
            (kind,) = params
            return sorted(
                (dict(v) for v in self.catalog.values() if v.get("kind") == kind),
                key=lambda r: r.get("name", ""),
            )
        raise NotImplementedError(f"_InMemoryPool.fetch_all unsupported sql:\n{sql}")

    async def fetch_one(
        self, sql: str, params: Sequence[Any]
    ) -> Optional[Dict[str, Any]]:
        sql = sql.strip()
        if sql.startswith("SELECT * FROM public.services_catalog\nWHERE name"):
            (name,) = params
            row = self.catalog.get(name)
            return dict(row) if row else None
        raise NotImplementedError(f"_InMemoryPool.fetch_one unsupported sql:\n{sql}")


def read_migration_sql() -> str:
    """Read the Phase 24 migration SQL from disk (used by the CLI)."""
    with open(MIGRATION_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


__all__ = [
    "MIGRATION_PATH",
    "SystemdState",
    "HeartbeatMark",
    "ServicesCatalog",
    "SystemctlRunner",
    "parse_systemctl_show",
    "default_systemctl_runner",
    "extract_heartbeats_from_audit",
    "read_migration_sql",
]

# Make sure the migrations directory is importable (for tools that browse
# by path via MIGRATION_PATH).
assert os.path.exists(os.path.dirname(MIGRATION_PATH))
