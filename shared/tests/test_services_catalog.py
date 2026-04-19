"""Phase 24 — tests for the Services Catalog."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict

from shared.services import SERVICE_REGISTRY, ServicesCatalog, SystemdState
from shared.services.catalog import (
    HeartbeatMark,
    MIGRATION_PATH,
    _InMemoryPool,
    _normalize_row,
    extract_heartbeats_from_audit,
    parse_systemctl_show,
    read_migration_sql,
)


# ---------------------------------------------------------------------------
# Pure parsing
# ---------------------------------------------------------------------------


def test_parse_systemctl_show_happy_path() -> None:
    text = (
        "ActiveState=active\n"
        "SubState=running\n"
        "ActiveEnterTimestamp=Sat 2026-04-19 05:27:11 UTC\n"
        "LoadState=loaded\n"
    )
    s = parse_systemctl_show("tickles-md-gateway.service", text)
    assert s.active_state == "active"
    assert s.sub_state == "running"
    assert s.load_state == "loaded"
    assert s.active_enter_ts is not None
    assert s.active_enter_ts.tzinfo is not None


def test_parse_systemctl_show_missing_timestamp() -> None:
    text = "ActiveState=inactive\nSubState=dead\nActiveEnterTimestamp=\nLoadState=loaded\n"
    s = parse_systemctl_show("tickles-x.service", text)
    assert s.active_state == "inactive"
    assert s.active_enter_ts is None


def test_parse_systemctl_show_empty_text() -> None:
    s = parse_systemctl_show("tickles-x.service", "")
    assert s.active_state is None
    assert s.sub_state is None
    assert s.active_enter_ts is None


def test_systemd_state_update_row_shape() -> None:
    state = SystemdState(
        unit="tickles-x.service",
        active_state="active",
        sub_state="running",
        active_enter_ts=datetime(2026, 4, 19, 5, 27, 11, tzinfo=timezone.utc),
    )
    row = state.as_update_row()
    assert row["last_systemd_state"] == "active"
    assert row["last_systemd_substate"] == "running"
    assert row["last_systemd_active_enter_ts"].tzinfo is not None


def test_migration_file_exists_and_has_expected_objects() -> None:
    assert os.path.exists(MIGRATION_PATH)
    sql = read_migration_sql()
    assert "CREATE TABLE IF NOT EXISTS public.services_catalog" in sql
    assert "CREATE TABLE IF NOT EXISTS public.services_catalog_snapshots" in sql
    assert "CREATE OR REPLACE VIEW public.services_catalog_current" in sql


def test_normalize_row_converts_datetimes_and_jsonb() -> None:
    raw = {
        "name": "x",
        "first_registered_at": datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc),
        "tags": '{"phase": "24"}',
        "metadata": '{"foo": 1}',
    }
    out = _normalize_row(raw)
    assert out["first_registered_at"].endswith("+00:00")
    assert out["tags"] == {"phase": "24"}
    assert out["metadata"] == {"foo": 1}


# ---------------------------------------------------------------------------
# ServicesCatalog round-trip via the in-memory pool
# ---------------------------------------------------------------------------


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_sync_registry_upserts_all_descriptors() -> None:
    pool = _InMemoryPool()
    cat = ServicesCatalog()
    synced = _run(cat.sync_registry(pool))
    assert synced == len(SERVICE_REGISTRY.list_services())
    assert synced >= 9
    # Every descriptor shows up with the expected columns.
    for desc in SERVICE_REGISTRY.list_services():
        row = pool.catalog[desc.name]
        assert row["kind"] == desc.kind
        assert row["module"] == desc.module
        assert row["systemd_unit"]
        assert row["enabled_on_vps"] == desc.enabled_on_vps
        assert row["has_factory"] == (desc.factory is not None)
        assert isinstance(row["tags"], dict)


def test_sync_registry_is_idempotent() -> None:
    pool = _InMemoryPool()
    cat = ServicesCatalog()
    _run(cat.sync_registry(pool))
    initial_names = set(pool.catalog.keys())
    _run(cat.sync_registry(pool))
    assert set(pool.catalog.keys()) == initial_names


def test_snapshot_systemd_with_fake_runner() -> None:
    pool = _InMemoryPool()
    cat = ServicesCatalog(
        systemctl_runner=lambda unit: (
            "ActiveState=active\nSubState=running\n"
            "ActiveEnterTimestamp=Sat 2026-04-19 05:27:11 UTC\nLoadState=loaded\n"
        ),
    )
    _run(cat.sync_registry(pool))
    results = _run(cat.snapshot_systemd(pool, names=["md-gateway", "candle-daemon"]))
    assert len(results) == 2
    for r in results:
        assert r["active_state"] == "active"
        assert r["sub_state"] == "running"
    # Catalog rows updated
    for name in ("md-gateway", "candle-daemon"):
        row = pool.catalog[name]
        assert row["last_systemd_state"] == "active"
        assert row["last_systemd_substate"] == "running"
    # History appended
    assert len(pool.snapshots) == 2


def test_snapshot_systemd_unknown_service_skipped() -> None:
    pool = _InMemoryPool()
    cat = ServicesCatalog(systemctl_runner=lambda unit: "")
    _run(cat.sync_registry(pool))
    results = _run(cat.snapshot_systemd(pool, names=["definitely-not-registered"]))
    assert results == []


def test_snapshot_systemd_no_history_flag() -> None:
    pool = _InMemoryPool()
    cat = ServicesCatalog(systemctl_runner=lambda unit: "ActiveState=active\nSubState=running\n")
    _run(cat.sync_registry(pool))
    _run(cat.snapshot_systemd(pool, names=["md-gateway"], also_append_history=False))
    assert pool.snapshots == []
    assert pool.catalog["md-gateway"]["last_systemd_state"] == "active"


def test_attach_heartbeats_updates_and_appends() -> None:
    pool = _InMemoryPool()
    cat = ServicesCatalog()
    _run(cat.sync_registry(pool))
    now = datetime.now(timezone.utc)
    marks = [
        HeartbeatMark(service="md-gateway", ts=now, severity="ok"),
        HeartbeatMark(service="candle-daemon", ts=now, severity="warning",
                      details={"misses": 3}),
    ]
    updated = _run(cat.attach_heartbeats(pool, marks))
    assert updated == 2
    assert pool.catalog["md-gateway"]["last_heartbeat_severity"] == "ok"
    assert pool.catalog["candle-daemon"]["last_heartbeat_severity"] == "warning"
    assert len(pool.snapshots) == 2
    assert pool.snapshots[1]["metadata"] == {"misses": 3}


def test_attach_heartbeats_empty_list_noop() -> None:
    pool = _InMemoryPool()
    cat = ServicesCatalog()
    _run(cat.sync_registry(pool))
    assert _run(cat.attach_heartbeats(pool, [])) == 0
    assert pool.snapshots == []


def test_list_services_and_by_kind() -> None:
    pool = _InMemoryPool()
    cat = ServicesCatalog()
    _run(cat.sync_registry(pool))
    everyone = _run(cat.list_services(pool))
    assert len(everyone) == len(SERVICE_REGISTRY.list_services())
    collectors = _run(cat.list_services(pool, kind="collector"))
    assert len(collectors) >= 1
    for row in collectors:
        assert row["kind"] == "collector"


def test_describe_service_hit_and_miss() -> None:
    pool = _InMemoryPool()
    cat = ServicesCatalog()
    _run(cat.sync_registry(pool))
    row = _run(cat.describe_service(pool, "md-gateway"))
    assert row is not None
    assert row["name"] == "md-gateway"
    assert _run(cat.describe_service(pool, "nope")) is None


# ---------------------------------------------------------------------------
# extract_heartbeats_from_audit uses the real AuditStore (SQLite, lightweight)
# ---------------------------------------------------------------------------


def test_extract_heartbeats_from_audit_returns_latest_per_service(tmp_path) -> None:
    os.environ["TICKLES_AUDIT_DB"] = str(tmp_path / "rule1.sqlite3")
    from shared.auditor import AuditStore
    from shared.auditor.schema import AuditEventType, AuditRecord, AuditSeverity

    with AuditStore() as store:
        # Older heartbeat
        store.record(
            AuditRecord(
                ts_unix=time.time() - 120,
                event_type=AuditEventType.HEARTBEAT,
                severity=AuditSeverity.OK,
                subject="service:md-gateway",
                passed=True,
            )
        )
        # Newer heartbeat (should be the one we pick up)
        store.record(
            AuditRecord(
                ts_unix=time.time(),
                event_type=AuditEventType.HEARTBEAT,
                severity=AuditSeverity.WARNING,
                subject="service:md-gateway",
                passed=False,
                details={"misses": 5},
            )
        )
        # Unrelated service, should be ignored
        store.record(
            AuditRecord(
                ts_unix=time.time(),
                event_type=AuditEventType.HEARTBEAT,
                severity=AuditSeverity.OK,
                subject="service:something-else",
                passed=True,
            )
        )
        marks = extract_heartbeats_from_audit(
            store,
            ["service:md-gateway", "service:candle-daemon"],
            window_seconds=3600,
        )
    assert len(marks) == 1
    mark = marks[0]
    assert mark.service == "service:md-gateway"
    assert mark.severity == "warning"
    assert mark.details.get("misses") == 5


# ---------------------------------------------------------------------------
# CLI smoke — apply-migration and migration-sql should work without a DB.
# ---------------------------------------------------------------------------


def _run_cli(*args: str, env: Dict[str, str] | None = None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "shared.cli.services_catalog_cli", *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        timeout=30,
    )


def test_cli_apply_migration_prints_path_and_example() -> None:
    cp = _run_cli("apply-migration")
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout.strip())
    assert payload["ok"] is True
    assert payload["migration_path"].endswith("2026_04_19_phase24_services_catalog.sql")
    assert "psql" in payload["apply_example"]


def test_cli_migration_sql_emits_full_file() -> None:
    cp = _run_cli("migration-sql")
    assert cp.returncode == 0, cp.stderr
    assert "public.services_catalog" in cp.stdout
    assert "BEGIN" in cp.stdout and "COMMIT" in cp.stdout


def test_cli_help_lists_subcommands() -> None:
    cp = _run_cli("--help")
    assert cp.returncode == 0, cp.stderr
    out = cp.stdout
    for sub in ("apply-migration", "sync", "snapshot", "attach-heartbeats",
                "list", "describe", "refresh", "migration-sql"):
        assert sub in out, f"missing {sub} in --help output"


def test_cli_describe_no_db_fails_cleanly() -> None:
    # Force use of the DSN branch so we don't hit the real pool. Point it
    # at an unreachable DSN — we expect a single-line JSON error.
    env = {"TICKLES_SHARED_DSN": "postgres://nouser:nopw@127.0.0.1:1/nodb"}
    cp = _run_cli("describe", "md-gateway", env=env)
    assert cp.returncode == 1
    # The CLI wraps unexpected exceptions as JSON; parse first line.
    first = cp.stdout.strip().splitlines()[0]
    payload = json.loads(first)
    assert payload["ok"] is False
