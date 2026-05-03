"""
Module: test_migrate_md_to_mem0
Purpose: Unit tests for shared/scripts/migrate_md_to_mem0.py.
Location: /opt/tickles/shared/tests/test_migrate_md_to_mem0.py

Run with:
    python -m pytest shared/tests/test_migrate_md_to_mem0.py -v
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Ensure /opt/tickles is on sys.path for the `shared` package import.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared.scripts import migrate_md_to_mem0 as mig  # noqa: E402

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
SAMPLE_LOG_TWO_ENTRIES = (
    "Trade #1 -- BTCUSDT\n"
    "- Time: 2026-04-30T11:23:45.123456+00:00\n"
    "- Action: OPEN\n"
    "- Side: long\n"
    "- Price: 60000\n"
    "\n"
    "Trade #2 -- ETHUSDT\n"
    "- Time: 2026-04-30T12:00:00+00:00\n"
    "- Action: CLOSE\n"
    "- PnL: 12.34\n"
)

SAMPLE_STATE = (
    "# TRADE_STATE\n"
    "Balance: 10123.45\n"
    "Open positions: 0\n"
)


@pytest.fixture
def fake_mem(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch shared.utils.mem0_config.get_memory to a MagicMock."""
    mock_mem = MagicMock()
    mock_mem.add = MagicMock(return_value={"id": "fake"})

    def _factory(_company: str, _agent: str) -> tuple[MagicMock, str]:
        return mock_mem, f"{_company}-{_agent}"

    # Patch at the import site that the migration script uses.
    fake_module = MagicMock()
    fake_module.get_memory = _factory
    monkeypatch.setitem(sys.modules, "shared.utils.mem0_config", fake_module)
    return mock_mem


# ---------------------------------------------------------------------------
# Test 1 — discover() finds nested files and excludes blacklisted dirs
# ---------------------------------------------------------------------------
def test_discover_finds_nested_files_and_excludes(tmp_path: Path) -> None:
    """discover() walks recursively but skips template/test/handoff dirs."""
    (tmp_path / "ws_a").mkdir()
    (tmp_path / "ws_a" / "TRADE_STATE.md").write_text("a")
    (tmp_path / "ws_a" / "TRADE_LOG.md").write_text("b")
    (tmp_path / "ws_b" / "deep").mkdir(parents=True)
    (tmp_path / "ws_b" / "deep" / "TRADE_LOG.md").write_text("c")
    # Excluded
    excluded = tmp_path / "shared" / "templates"
    excluded.mkdir(parents=True)
    (excluded / "TRADE_STATE.md").write_text("ignored")

    found = sorted(str(p) for p in mig.discover([tmp_path]))
    assert any("ws_a/TRADE_STATE.md" in p for p in found)
    assert any("ws_a/TRADE_LOG.md" in p for p in found)
    assert any("ws_b/deep/TRADE_LOG.md" in p for p in found)
    assert not any("shared/templates" in p for p in found)


# ---------------------------------------------------------------------------
# Test 2 — discover() handles a non-existent root gracefully
# ---------------------------------------------------------------------------
def test_discover_handles_missing_root(tmp_path: Path) -> None:
    """A non-existent root is logged and skipped, not raised."""
    missing = tmp_path / "does-not-exist"
    out = list(mig.discover([missing]))
    assert out == []


# ---------------------------------------------------------------------------
# Test 3 — resolve_owner reads meta.json companyId
# ---------------------------------------------------------------------------
def test_resolve_owner_uses_meta_json(tmp_path: Path) -> None:
    """resolve_owner walks parents and returns companyId/agentId from meta.json."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "meta.json").write_text(
        json.dumps({"companyId": "rubicon", "agentId": "surgeon1"})
    )
    f = ws / "TRADE_LOG.md"
    f.write_text("x")
    company, agent = mig.resolve_owner(f, allow_fallback=False)
    assert company == "rubicon"
    assert agent == "surgeon1"


# ---------------------------------------------------------------------------
# Test 4 — resolve_owner raises without meta.json unless --allow-fallback-owner
# ---------------------------------------------------------------------------
def test_resolve_owner_raises_without_fallback(tmp_path: Path) -> None:
    """No meta.json + allow_fallback=False raises FileNotFoundError."""
    f = tmp_path / "stray" / "TRADE_LOG.md"
    f.parent.mkdir()
    f.write_text("x")
    with pytest.raises(FileNotFoundError):
        mig.resolve_owner(f, allow_fallback=False)
    # With fallback enabled, dir-name slug is used.
    company, agent = mig.resolve_owner(f, allow_fallback=True)
    assert company == "stray"
    assert agent == "surgeon"


# ---------------------------------------------------------------------------
# Test 5 — replay_trade_log parses two clean entries with timestamps
# ---------------------------------------------------------------------------
def test_replay_trade_log_parses_two_entries(fake_mem: MagicMock) -> None:
    """replay_trade_log writes one mem0 entry per Twilly trade entry."""
    n = mig.replay_trade_log(
        SAMPLE_LOG_TWO_ENTRIES,
        fallback_iso="2026-05-01T00:00:00+00:00",
        company="rubicon",
        agent="surgeon",
        dry_run=False,
    )
    assert n == 2
    assert fake_mem.add.call_count == 2
    # Inspect first call: action+symbol+trade_id+original_timestamp must be parsed.
    first_kwargs = fake_mem.add.call_args_list[0].kwargs
    md = first_kwargs["metadata"]
    assert md["type"] == "trade_decision"
    assert md["kind"] == "historical_migration"
    assert md["action"] == "open"
    assert md["trade_id"] == 1
    assert md["symbol"] == "BTCUSDT"
    assert md["original_timestamp"].startswith("2026-04-30T11:23:45")
    assert md["source_file"] == "TRADE_LOG.md"


# ---------------------------------------------------------------------------
# Test 6 — replay_trade_log R2 fallback for malformed log writes ONE raw_dump
# ---------------------------------------------------------------------------
def test_replay_trade_log_raw_dump_fallback(fake_mem: MagicMock) -> None:
    """If the regex finds zero entries, write one entry tagged kind=raw_dump."""
    junk = "this is not a Twilly trade log at all\nlol random text\n"
    n = mig.replay_trade_log(
        junk,
        fallback_iso="2026-05-01T00:00:00+00:00",
        company="rubicon",
        agent="surgeon",
        dry_run=False,
    )
    assert n == 1
    md = fake_mem.add.call_args.kwargs["metadata"]
    assert md["kind"] == "raw_dump"
    assert md["source_file"] == "TRADE_LOG.md"
    assert md["original_timestamp"] == "2026-05-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Test 7 — replay_trade_log on empty file writes nothing
# ---------------------------------------------------------------------------
def test_replay_trade_log_empty_writes_nothing(fake_mem: MagicMock) -> None:
    """Empty TRADE_LOG.md is not migrated."""
    n = mig.replay_trade_log(
        "   \n\n",
        fallback_iso="2026-05-01T00:00:00+00:00",
        company="rubicon",
        agent="surgeon",
        dry_run=False,
    )
    assert n == 0
    fake_mem.add.assert_not_called()


# ---------------------------------------------------------------------------
# Test 8 — dry-run writes nothing to mem0 but populates manifest
# ---------------------------------------------------------------------------
def test_dry_run_writes_no_mem0_but_writes_manifest(
    tmp_path: Path, fake_mem: MagicMock, capsys: pytest.CaptureFixture
) -> None:
    """--dry-run produces a manifest without calling mem.add."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "meta.json").write_text(json.dumps({"companyId": "testco"}))
    (ws / "TRADE_STATE.md").write_text(SAMPLE_STATE)
    (ws / "TRADE_LOG.md").write_text(SAMPLE_LOG_TWO_ENTRIES)
    manifest_path = tmp_path / "manifest.json"

    rc = mig.main(
        [
            "--dry-run",
            "--roots",
            str(tmp_path),
            "--manifest",
            str(manifest_path),
        ]
    )
    assert rc == 0
    fake_mem.add.assert_not_called()
    data = json.loads(manifest_path.read_text())
    assert len(data) == 2
    statuses = {e["status"] for e in data}
    assert statuses == {"dry_run_planned"}
    # The .md files must remain on disk after dry-run (no rename).
    assert (ws / "TRADE_STATE.md").exists()
    assert (ws / "TRADE_LOG.md").exists()


# ---------------------------------------------------------------------------
# Test 9 — --apply writes to mem0 and renames the file to .md.migrated
# ---------------------------------------------------------------------------
def test_apply_writes_mem0_and_renames(
    tmp_path: Path, fake_mem: MagicMock
) -> None:
    """--apply renames TRADE_LOG.md → TRADE_LOG.md.migrated and writes mem0."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "meta.json").write_text(json.dumps({"companyId": "testco"}))
    (ws / "TRADE_LOG.md").write_text(SAMPLE_LOG_TWO_ENTRIES)
    manifest_path = tmp_path / "manifest.json"

    rc = mig.main(
        [
            "--apply",
            "--roots",
            str(tmp_path),
            "--manifest",
            str(manifest_path),
        ]
    )
    assert rc == 0
    assert fake_mem.add.call_count == 2
    assert not (ws / "TRADE_LOG.md").exists()
    assert (ws / "TRADE_LOG.md.migrated").exists()
    data = json.loads(manifest_path.read_text())
    assert data[0]["status"] == "applied"
    assert data[0]["entries_written"] == 2
    assert data[0]["renamed_to"].endswith("TRADE_LOG.md.migrated")


# ---------------------------------------------------------------------------
# Test 10 — --company filter and exclusivity of --dry-run/--apply
# ---------------------------------------------------------------------------
def test_company_filter_and_mutual_exclusivity(
    tmp_path: Path, fake_mem: MagicMock
) -> None:
    """--company filters; passing both/neither --dry-run/--apply errors out."""
    # Setup two workspaces in different companies.
    a = tmp_path / "a"
    a.mkdir()
    (a / "meta.json").write_text(json.dumps({"companyId": "alpha"}))
    (a / "TRADE_LOG.md").write_text(SAMPLE_LOG_TWO_ENTRIES)
    b = tmp_path / "b"
    b.mkdir()
    (b / "meta.json").write_text(json.dumps({"companyId": "beta"}))
    (b / "TRADE_LOG.md").write_text(SAMPLE_LOG_TWO_ENTRIES)
    manifest_path = tmp_path / "m.json"

    # Filter to alpha only — beta must be skipped.
    rc = mig.main(
        [
            "--dry-run",
            "--company",
            "alpha",
            "--roots",
            str(tmp_path),
            "--manifest",
            str(manifest_path),
        ]
    )
    assert rc == 0
    data = json.loads(manifest_path.read_text())
    by_company: dict[str, list[dict[str, Any]]] = {}
    for e in data:
        by_company.setdefault(str(e.get("company")), []).append(e)
    assert "alpha" in by_company
    assert any(e["status"] == "dry_run_planned" for e in by_company["alpha"])
    assert all(e["status"] == "skipped_filtered" for e in by_company["beta"])

    # Mutual exclusivity: passing both flags must trigger argparse SystemExit.
    with pytest.raises(SystemExit):
        mig.main(
            [
                "--dry-run",
                "--apply",
                "--roots",
                str(tmp_path),
                "--manifest",
                str(manifest_path),
            ]
        )
    # Passing neither must also fail.
    with pytest.raises(SystemExit):
        mig.main(
            [
                "--roots",
                str(tmp_path),
                "--manifest",
                str(manifest_path),
            ]
        )


# ---------------------------------------------------------------------------
# Smoke test: build_manifest with allow_fallback=False marks orphans
# ---------------------------------------------------------------------------
def test_build_manifest_marks_orphans_when_no_fallback(tmp_path: Path) -> None:
    """A file with no meta.json ancestor is flagged status=skipped_no_owner."""
    f = tmp_path / "stray" / "TRADE_LOG.md"
    f.parent.mkdir()
    f.write_text(SAMPLE_LOG_TWO_ENTRIES)
    manifest = mig.build_manifest(
        [f], company_filter=None, allow_fallback=False
    )
    assert len(manifest) == 1
    assert manifest[0]["status"] == "skipped_no_owner"
    assert "error" in manifest[0]


# ---------------------------------------------------------------------------
# Regression 1 — replay_trade_state forwards user_id + agent_id to mem.add()
# ---------------------------------------------------------------------------
def test_replay_trade_state_forwards_user_id_and_agent_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: shipped script must forward user_id=company,
    agent_id=agent_id to mem.add(). Previous bug discarded agent_id and
    omitted user_id, causing mem0 ValidationError on every call.
    See .roo/handoffs/2026-05-03-md-vs-mem0-apply-FAILED.md §3.3."""
    captured: list[dict[str, Any]] = []

    class _FakeMem:
        def add(self, text: str, **kwargs: Any) -> dict[str, Any]:
            captured.append({"text": text, "kwargs": kwargs})
            return {"results": [{"id": "fake"}]}

    def _fake_get_memory(company: str, agent: str) -> tuple[_FakeMem, str]:
        return _FakeMem(), f"{company}_{agent}_uuid"

    fake_module = MagicMock()
    fake_module.get_memory = _fake_get_memory
    monkeypatch.setitem(sys.modules, "shared.utils.mem0_config", fake_module)

    n = mig.replay_trade_state(
        SAMPLE_STATE,
        mtime_iso="2026-05-03T00:00:00+00:00",
        company="rubicon_test",
        agent="surgeon",
        dry_run=False,
    )
    assert n == 1
    assert len(captured) == 1, "replay_trade_state did not call mem.add exactly once"
    kw = captured[0]["kwargs"]
    assert kw.get("user_id") == "rubicon_test", (
        f"missing user_id on mem.add; got kwargs={kw}"
    )
    assert kw.get("agent_id") == "rubicon_test_surgeon_uuid", (
        f"missing/wrong agent_id on mem.add; got kwargs={kw}"
    )
    assert kw["metadata"]["kind"] == "historical_migration"
    assert kw["metadata"]["source_file"] == "TRADE_STATE.md"


# ---------------------------------------------------------------------------
# Regression 2 — replay_trade_log per-entry loop forwards user_id + agent_id
# ---------------------------------------------------------------------------
def test_replay_trade_log_forwards_user_id_and_agent_id_per_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: per-entry mem.add() in replay_trade_log must forward
    user_id=company, agent_id=agent_id from get_memory()."""
    captured: list[dict[str, Any]] = []

    class _FakeMem:
        def add(self, text: str, **kwargs: Any) -> dict[str, Any]:
            captured.append({"text": text, "kwargs": kwargs})
            return {"results": [{"id": "fake"}]}

    def _fake_get_memory(company: str, agent: str) -> tuple[_FakeMem, str]:
        return _FakeMem(), f"{company}_{agent}_uuid"

    fake_module = MagicMock()
    fake_module.get_memory = _fake_get_memory
    monkeypatch.setitem(sys.modules, "shared.utils.mem0_config", fake_module)

    n = mig.replay_trade_log(
        SAMPLE_LOG_TWO_ENTRIES,
        fallback_iso="2026-05-03T00:00:00+00:00",
        company="rubicon_test",
        agent="surgeon",
        dry_run=False,
    )
    assert n == 2
    assert len(captured) == 2
    for call in captured:
        kw = call["kwargs"]
        assert kw.get("user_id") == "rubicon_test", (
            f"missing user_id on mem.add; got kwargs={kw}"
        )
        assert kw.get("agent_id") == "rubicon_test_surgeon_uuid", (
            f"missing/wrong agent_id on mem.add; got kwargs={kw}"
        )
        assert kw["metadata"]["kind"] == "historical_migration"


# ---------------------------------------------------------------------------
# Regression 3 — process_file refuses rename when entries_written=0
# ---------------------------------------------------------------------------
def test_apply_refuses_rename_when_zero_entries_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: process_file must NOT rename source to .md.migrated when
    entries_written=0 on a non-empty source file. Previously a swallowed
    per-entry exception left status=applied with entries_written=0 and the
    source file was renamed away — silent data loss risk."""

    class _FailingMem:
        def add(self, text: str, **kwargs: Any) -> dict[str, Any]:
            # Simulate every per-entry mem.add failing — the script's loop
            # swallows the exception and continues, so n stays at 0.
            raise RuntimeError("simulated mem0 ValidationError")

    def _fake_get_memory(company: str, agent: str) -> tuple[_FailingMem, str]:
        return _FailingMem(), f"{company}_{agent}_uuid"

    fake_module = MagicMock()
    fake_module.get_memory = _fake_get_memory
    monkeypatch.setitem(sys.modules, "shared.utils.mem0_config", fake_module)

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "meta.json").write_text(json.dumps({"companyId": "testco"}))
    log_path = ws / "TRADE_LOG.md"
    log_path.write_text(SAMPLE_LOG_TWO_ENTRIES)

    entry: dict[str, Any] = {
        "path": str(log_path),
        "company": "testco",
        "agent": "surgeon",
        "size_bytes": log_path.stat().st_size,
        "mtime_utc": "2026-05-03T00:00:00+00:00",
        "kind": "TRADE_LOG.md",
        "status": "pending",
    }

    result = mig.process_file(log_path, entry, dry_run=False)

    # Source must still exist as .md (NOT renamed to .md.migrated).
    assert log_path.exists(), (
        "FAIL: source .md was renamed despite zero entries written"
    )
    assert not (ws / "TRADE_LOG.md.migrated").exists(), (
        "FAIL: .md.migrated was created despite zero entries written"
    )
    # Status must reflect the guard.
    assert result["status"] == "error_zero_entries", (
        f"expected status=error_zero_entries, got {result['status']}"
    )
    assert result["entries_written"] == 0
    assert "renamed_to" not in result
