"""Unit tests for Phase 22 shared.services."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import pytest

from shared.services import (
    SERVICE_REGISTRY,
    CollectorServiceAdapter,
    DaemonConfig,
    DaemonStats,
    ServiceDaemon,
    ServiceDescriptor,
    ServiceRegistry,
    register_builtin_services,
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_builtin_services_registered() -> None:
    register_builtin_services()
    assert len(SERVICE_REGISTRY) >= 5
    names = {s.name for s in SERVICE_REGISTRY.list_services()}
    for expected in (
        "md-gateway",
        "candle-daemon",
        "catalog",
        "bt-workers",
        "auditor",
    ):
        assert expected in names


def test_service_descriptor_to_dict_is_json() -> None:
    desc = ServiceDescriptor(
        name="demo",
        kind="collector",
        module="shared.demo",
        description="x",
        enabled_on_vps=False,
    )
    d = desc.to_dict()
    assert d["systemd_unit"] == "tickles-demo.service"
    assert d["has_factory"] is False
    json.dumps(d)


def test_register_custom_service() -> None:
    reg = ServiceRegistry()
    reg.register(
        ServiceDescriptor(name="x", kind="custom", module="m", description="d")
    )
    assert "x" in reg
    assert reg.get("x").kind == "custom"
    with pytest.raises(KeyError):
        reg.get("missing")


def test_registry_by_kind() -> None:
    collectors = SERVICE_REGISTRY.by_kind("collector")
    assert len(collectors) >= 1
    assert all(s.kind == "collector" for s in collectors)


# ---------------------------------------------------------------------------
# ServiceDaemon
# ---------------------------------------------------------------------------


def test_daemon_config_validates() -> None:
    with pytest.raises(ValueError):
        DaemonConfig(name="x", interval_seconds=0)
    with pytest.raises(ValueError):
        DaemonConfig(name="x", jitter_seconds=-1)

    cfg = DaemonConfig(name="x", interval_seconds=1.0, max_backoff_seconds=0.5)
    # max_backoff is auto-adjusted
    assert cfg.max_backoff_seconds >= cfg.interval_seconds


def test_daemon_run_once_happy_path() -> None:
    state = {"count": 0}

    async def tick() -> dict:
        state["count"] += 1
        return {"ok": True, "n": state["count"]}

    daemon = ServiceDaemon(
        DaemonConfig(
            name="t",
            interval_seconds=0.1,
            heartbeat_every_seconds=0.0,
            emit_heartbeats_to_auditor=False,
        ),
        tick,
    )
    result = asyncio.run(daemon.run_once())
    assert result == {"ok": True, "n": 1}
    assert daemon.stats.total_ticks == 1
    assert daemon.stats.last_tick_ok is True
    assert daemon.stats.consecutive_failures == 0


def test_daemon_run_once_handles_exception() -> None:
    async def tick() -> dict:
        raise RuntimeError("boom")

    daemon = ServiceDaemon(
        DaemonConfig(
            name="t",
            interval_seconds=0.1,
            emit_heartbeats_to_auditor=False,
        ),
        tick,
    )
    result = asyncio.run(daemon.run_once())
    assert result["ok"] is False
    assert "boom" in result["error"]
    assert daemon.stats.total_failures == 1
    assert daemon.stats.consecutive_failures == 1


def test_daemon_run_forever_stops_on_event() -> None:
    state = {"count": 0}

    async def tick() -> dict:
        state["count"] += 1
        return {"n": state["count"]}

    daemon = ServiceDaemon(
        DaemonConfig(
            name="t",
            interval_seconds=0.05,
            jitter_seconds=0.0,
            heartbeat_every_seconds=0.0,
            emit_heartbeats_to_auditor=False,
        ),
        tick,
    )

    async def main() -> None:
        task = asyncio.create_task(daemon.run_forever())
        await asyncio.sleep(0.2)
        assert daemon._stop_event is not None
        daemon._stop_event.set()
        await task

    asyncio.run(main())
    assert state["count"] >= 2
    assert daemon.stats.alive is False


def test_daemon_next_wait_applies_backoff() -> None:
    async def tick() -> dict:
        return {}

    cfg = DaemonConfig(
        name="t",
        interval_seconds=1.0,
        jitter_seconds=0.0,
        heartbeat_every_seconds=0.0,
        emit_heartbeats_to_auditor=False,
    )
    daemon = ServiceDaemon(cfg, tick)
    daemon.stats.consecutive_failures = 3
    wait = daemon._next_wait_seconds()
    assert wait >= 1.0 * (2 ** 3)


def test_daemon_stats_to_dict_is_json() -> None:
    stats = DaemonStats(name="t", started_at=1.0, last_tick_at=2.0, last_tick_ok=True)
    json.dumps(stats.to_dict())


# ---------------------------------------------------------------------------
# CollectorServiceAdapter
# ---------------------------------------------------------------------------


class _FakeCollector:
    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self.errors = 0
        self.collected = 0

        class _Cfg:
            collection_interval_seconds = 7

        self.config = _Cfg()

    async def collect(self) -> list:
        self.collected += 1
        return [{"id": self.collected}]

    async def write_to_db(self, items: list, db_pool) -> int:  # noqa: ANN001
        return len(items)


def test_collector_adapter_run_once_no_pool() -> None:
    adapter = CollectorServiceAdapter(_FakeCollector("c1"))
    summary = asyncio.run(adapter.run_once())
    assert summary["ok"] is True
    assert summary["items_fetched"] == 1
    assert summary["items_inserted"] == 0


def test_collector_adapter_run_once_with_pool() -> None:
    async def factory():
        return object()

    adapter = CollectorServiceAdapter(
        _FakeCollector("c2"),
        db_pool_factory=factory,
    )
    summary = asyncio.run(adapter.run_once())
    assert summary["items_inserted"] == 1


def test_collector_adapter_inherits_interval() -> None:
    adapter = CollectorServiceAdapter(_FakeCollector("c3"))
    assert adapter.daemon.config.interval_seconds == 7.0


# ---------------------------------------------------------------------------
# services_cli
# ---------------------------------------------------------------------------


def _run_cli(args: list[str]) -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "shared.cli.services_cli", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode in (0, 1), proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_services_cli_list() -> None:
    payload = _run_cli(["list"])
    assert payload["ok"] is True
    assert payload["count"] >= 5
    names = {s["name"] for s in payload["services"]}
    assert "md-gateway" in names
    assert "auditor" in names


def test_services_cli_list_kind_filter() -> None:
    payload = _run_cli(["list", "--kind", "collector"])
    assert payload["ok"] is True
    assert all(s["kind"] == "collector" for s in payload["services"])


def test_services_cli_describe_known() -> None:
    payload = _run_cli(["describe", "md-gateway"])
    assert payload["ok"] is True
    assert payload["service"]["systemd_unit"] == "tickles-md-gateway.service"


def test_services_cli_describe_unknown() -> None:
    payload = _run_cli(["describe", "does-not-exist"])
    assert payload["ok"] is False


def test_services_cli_heartbeats_empty() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = os.environ.copy()
        env["TICKLES_AUDIT_DB"] = os.path.join(tmp, "rule1.sqlite3")
        proc = subprocess.run(
            [sys.executable, "-m", "shared.cli.services_cli", "heartbeats"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        assert payload["ok"] is True
        assert payload["count"] == 0


def test_services_cli_run_once_no_factory() -> None:
    payload = _run_cli(["run-once", "--name", "md-gateway"])
    assert payload["ok"] is False
    assert "factory" in payload["error"]
