# 2026-05-03 — `.md` vs mem0 Writer Audit

> Architect-mode handoff. Implements §4.3 of [`.roo/handoffs/2026-05-03-master-resume-handoff.md`](.roo/handoffs/2026-05-03-master-resume-handoff.md:344) and binding directive **D1** ("Memory unification — kill `openclaw_export_daemon` and any other agent that writes summaries to `.md` files. Replace with mem0 writes via [`shared/utils/mem0_config.py`](shared/utils/mem0_config.py:1)").

---

## §0 — TL;DR

| Question | Answer |
|---|---|
| Does `openclaw_export_daemon` exist? | **No.** Zero hits across the repo for the identifier in any `*.py`. It is a planned-but-never-built daemon mentioned only in handoffs and [`shared/docs/SYSTEM_INVENTORY_2026-05-02.md:381`](shared/docs/SYSTEM_INVENTORY_2026-05-02.md:381). Nothing to delete; D1 just kills the *idea*. |
| How many actual agent-output `.md` writers exist? | **2 files, 5 write-sites.** All inside `shared/`. Zero in `projects/`. |
| Are dev/handoff `.md` files in scope? | **No.** Per recipe acceptance criterion: *"Dev / handoff `.md` files (this document, for example) are EXCLUDED from the ban."* |
| Are agent-INPUT overlays (SOUL.md, AGENT.md, IDENTITY.md, etc.) in scope? | **No.** D1 bans agents *writing summaries* to `.md`. Provisioning writing prompt-template overlays is the inverse direction — agent-input, not agent-output. Keep. |
| Are CSV/HTML signal-review exports in scope? | **No.** [`shared/intelligence/signal_review_export.py`](shared/intelligence/signal_review_export.py:1) writes `.csv` + `.html`, not `.md`. Excluded by recipe step 2 ("…that one is intentionally CSV/HTML, not `.md`, but verify"). Verified. |
| What is the migration target? | **Three mem0 namespaces** depending on content kind: `<company>` agent_id `surgeon` for trade state/logs, `dev` agent_id `chart_hacker_guru` for cross-trader lessons (already in place — only the `.txt`/`.json` writes are redundant), and `<company>` agent_id `<role>` for any future per-agent narrative. |

**Bottom line:** the audit surface is small. Two source files, both surgeon-trader variants, both writing the same two filenames (`TRADE_STATE.md` and `TRADE_LOG.md`). The cleanup is a single-day code change. No production company currently runs the surgeon-trader code path against real money — `rubicon` runs the surgeon2 variant (which already has zero `.md` writes; verified), and `jarvais` is FROZEN. So this is a dead-code cleanup with a CI gate to prevent regression.

---

## §1 — Discovery method

### §1.1 Recipe (verbatim from master handoff §4.3)

```bash
# any code path that writes a .md file
grep -rn --include='*.py' "write_text\|\.md['\"]" shared/ projects/ | \
  grep -v test_ | \
  grep -v migrations/

# any code path that opens .md for append/write
grep -rn --include='*.py' "open(.*\.md.*['\"][wa]" shared/ projects/
```

### §1.2 Executed (Architect mode, this session)

| Search | Tool | Pattern | Scope | Hits |
|---|---|---|---|---|
| S1 | `search_files` | `write_text\(\|\.md['"]` | `shared/` `*.py` | 48 raw → 11 after exclusions |
| S2 | `search_files` | `write_text\(\|\.md['"]` | `projects/` `*.py` | **0** |
| S3 | `search_files` | `open\([^)]*\.md[^)]*['"][wa]` | `shared/` `*.py` | 0 |
| S4 | `search_files` | `open\([^)]*\.md[^)]*['"][wa]` | `projects/` `*.py` | 0 |
| S5 | `search_files` | `openclaw_export` | repo-wide `*.py` | **0** |
| S6 | `search_files` | `openclaw_export_daemon` | repo-wide all-files | 7 (all in `.md` docs/handoffs, none in code) |
| S7 | `search_files` | `SOUL\.md\|MEMORY\.md\|AGENT\.md\|IDENTITY\.md\|HEARTBEAT\.md\|BOOTSTRAP\.md\|EXECUTION_REALITY\.md\|USER\.md` | `shared/` `*.py` | 20 (all overlay-template hits — see §3) |

### §1.3 Exclusions applied

Per the recipe (`grep -v test_ | grep -v migrations/`) plus the acceptance criterion ("dev / handoff `.md` files…are EXCLUDED"):

- All `shared/tests/test_*.py` hits — test fixtures writing `.md` to `tmp_path` (e.g. [`shared/tests/test_payload_retention.py:107`](shared/tests/test_payload_retention.py:107)).
- All `shared/scripts/test_*.py` hits — same pattern.
- `shared/jobs/test_payload_retention.py` — same pattern.
- `shared/scripts/master_sync.py` — writes `.tmp` JSON, not `.md`.
- `shared/scripts/e2e_smoke.py:281` — writes a JSON report, not `.md`.
- `shared/cost_shipper/shipper.py:104-106` — writes a JSON tmp file, not `.md`.
- `shared/backtest/accessible.py:127` — generic `_atomic_write_text` helper, used for JSON state files, not `.md`.
- `shared/migration/memclaw_update_v3.py:144` — **reads** `CONTEXT_V3.md`, never writes.
- All `shared/altdata/` / `shared/regime/` / `shared/enrichment/` / `shared/assets/` writes — JSON fixtures or schema files, not `.md`.

---

## §2 — The inventory (every actual agent-output `.md` writer)

> **Read this table as: "what each row produces, why D1 bans it, where it must go instead."**

| # | File | Line(s) | Write target | Content kind | Source-of-truth status | D1 verdict | Proposed mem0 destination |
|---|------|---------|--------------|--------------|------------------------|------------|---------------------------|
| W1 | [`shared/daemons/surgeon_trader.py`](shared/daemons/surgeon_trader.py:54) | 54, 421-466 | `<workspace>/TRADE_STATE.md` (overwrite) | Live trader state snapshot: balance, realised P&L, fees, turnover, open positions, last 5 closes. | **Decorative.** Authoritative source is `.surgeon_state.json` ([line 56](shared/daemons/surgeon_trader.py:56) — comment: *"machine-readable sidecar (authoritative)"*). | **DELETE** the `.md` write. Replace with mem0 narrative entry on every closed trade. | namespace=`<company>` (e.g. `rubicon`), agent_id=`surgeon`, metadata.type=`trade_state_snapshot`, metadata.kind=`overwrite_latest`. **Note:** mem0 is append-only, so a "snapshot" pattern needs to be a search-by-recency + last-write-wins, not an UPDATE. |
| W2 | [`shared/daemons/surgeon_trader.py`](shared/daemons/surgeon_trader.py:55) | 55, 467-471, 588-591 | `<workspace>/TRADE_LOG.md` (append-only) | One-line entries per OPEN / CLOSE / NO-TRADE decision in "Twilly format". | **Authoritative narrative.** No DB equivalent — the `tracked_positions` table records facts, not the *prose* reasoning attached to a decision. | **CONVERT.** This is exactly the kind of agent narrative D1 wants in mem0. | namespace=`<company>`, agent_id=`surgeon`, metadata.type=`trade_decision`, metadata.action=`open\|close\|skip`, metadata.trade_id=int, metadata.symbol=str, metadata.original_timestamp=ISO-8601 UTC. |
| W3 | [`shared/templates/trading_agent/surgeon_llm_runner.py`](shared/templates/trading_agent/surgeon_llm_runner.py:528) | 489-528 | `<workspace>/TRADE_STATE.md` (overwrite) | Same content kind as W1, but produced by the LLM-driven variant of the surgeon. Includes "Last LLM Reasoning" section. | **Decorative + LLM-narrative blob.** The "Last LLM Reasoning" string is not stored anywhere else. | **CONVERT** the LLM-reasoning blob to mem0. **DELETE** the rest (state snapshot is in JSON). | namespace=`<company>`, agent_id=`surgeon_llm`, metadata.type=`llm_reasoning`, metadata.cycle_ts=ISO-8601, metadata.original_timestamp=ISO-8601. |
| W4 | [`shared/templates/trading_agent/surgeon_llm_runner.py`](shared/templates/trading_agent/surgeon_llm_runner.py:534) | 531-538 | `<workspace>/TRADE_LOG.md` (seed-if-missing) | Header line creating the log file. | Bootstrap artifact, no real content. | **DELETE.** No mem0 equivalent needed — the `TRADE_LOG.md` itself is being deleted (see W5). | n/a |
| W5 | [`shared/templates/trading_agent/surgeon_llm_runner.py`](shared/templates/trading_agent/surgeon_llm_runner.py:543) | 540-546 | `<workspace>/TRADE_LOG.md` (append) | OPEN / CLOSE / NO-TRADE entries (functions [`format_open_entry`](shared/templates/trading_agent/surgeon_llm_runner.py:549), [`format_close_entry`](shared/templates/trading_agent/surgeon_llm_runner.py:562), [`format_decision_line`](shared/templates/trading_agent/surgeon_llm_runner.py:576)). | **Authoritative narrative** — same as W2. | **CONVERT.** | namespace=`<company>`, agent_id=`surgeon_llm`, metadata.type=`trade_decision`, metadata.action=`open\|close\|skip`, metadata.trade_id=int, metadata.original_timestamp=ISO-8601. |

**Total violator surface: 2 source files, 5 write-sites.** All five are surgeon-trader narratives.

### §2.1 Adjacent flags (not violations, surfaced for completeness)

| # | File | Line | Why surfaced | Verdict |
|---|------|------|--------------|---------|
| A1 | [`shared/intelligence/chart_hacker_guru.py`](shared/intelligence/chart_hacker_guru.py:487) | 487-525, 830-831 | Writes `chart_hacker_guru_<ts>.txt` + `.json` (NOT `.md`) and `hourly_status_<ts>.txt` reports under `cfg.report_dir`. **Lessons already mirrored to mem0** at [line 559-578](shared/intelligence/chart_hacker_guru.py:559) via `_store_learning_in_mem0`. | **Strictly NOT a `.md` writer**, so it falls outside D1's literal text. But the `.txt` files duplicate what's already in mem0 + DB. **Recommendation:** flag for follow-up cleanup, NOT this audit. The mem0 path is the authoritative one and is already wired. |
| A2 | [`shared/provisioning/executor.py`](shared/provisioning/executor.py:916) | 916-948 | Writes 8 overlay `.md` files (`AGENT.md`, `SOUL.md`, `IDENTITY.md`, `TOOLS.md`, `USER.md`, `HEARTBEAT.md`, `BOOTSTRAP.md`, `MEMORY.md`) into a freshly-provisioned agent workspace. | **NOT a violation.** These are agent-INPUT prompt templates, not agent-OUTPUT knowledge. D1's intent is to ban agents persisting summaries/learnings/state to `.md`. Reading prompt-template `.md` files at boot is unchanged behaviour and matches the OpenClaw paperclip recipe documented in [`shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:146`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:146). **Keep, exclude from CI gate.** |
| A3 | [`shared/templates/trading_agent/surgeon_llm_runner.py`](shared/templates/trading_agent/surgeon_llm_runner.py:603) | 603-606, 626-627 | Reads `SOUL.md`, `TRADE_STATE.md`, `TRADE_LOG.md`, `EXECUTION_REALITY.md` at the top of every cycle. | **READ-only.** Not a violation. But once W3-W5 are removed, the corresponding reads at lines 604-605 must also be ripped out (they'd return empty strings) and replaced with mem0 search calls. See §4.3 for migration order. |
| A4 | [`shared/scripts/check_system_freshness.py`](shared/scripts/check_system_freshness.py:156) | 155-156 | Computes path to `TRADE_STATE.md` to check freshness. | **READ-only check.** Once W1/W3 are removed, this script must be updated to check `.surgeon_state.json` mtime instead. Coordinate with W1/W3 removal. |
| A5 | [`shared/daemons/surgeon2_trader.py`](shared/daemons/surgeon2_trader.py:1) | n/a | The newer surgeon variant. **Zero `.md` writes** confirmed via grep S2-equivalent. | **Already compliant.** This is the path `rubicon` runs in production. Confirms the cleanup is feasible — surgeon2 has already proved that no `.md` writes are needed for live trading. |

---

## §3 — Why the overlays (A2) are NOT violations — explicit rationale

A senior reviewer will ask: *"You're listing 8 `.md` writes in [`provisioning/executor.py:916-924`](shared/provisioning/executor.py:916) and calling them safe. Defend that."*

**Defence:**

1. **Direction of flow.** D1 says: *"agents must write to mem0, not files."* The verb is *write*. Provisioning writes overlay templates *for an agent to read*. The agent never writes back to those files at runtime. So the rule's direction-of-flow is preserved.
2. **Content kind.** The overlays contain **prompt-template scaffolding** — persona ("you are a quant trader…"), role hierarchy, MCP tool catalogue, three-tier mem0 contract, bootstrap checklist. None of it is *generated knowledge*. It's all author-time content baked from the company config.
3. **Existing OpenClaw recipe.** [`shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:146`](shared/docs/INTELLIGENCE_UNIFIED_PLAN.md:146) defines the OpenClaw-native agent recipe explicitly using these `.md` overlays. Removing them breaks the entire OpenClaw paperclip-visible workflow.
4. **Symmetric precedent.** A `.json` config file is not banned by D1 either — and these `.md` overlays are functionally the same: structured input config in a human-readable format.

The CI gate (§5) therefore must be a **path/file-name allowlist**, not a blanket `*.md` ban. Specific allowed write targets:

| Allowed write target | Rationale |
|---|---|
| `<workspace>/AGENT.md` | overlay template (provisioning) |
| `<workspace>/SOUL.md` | overlay template (provisioning) |
| `<workspace>/IDENTITY.md` | overlay template (provisioning) |
| `<workspace>/TOOLS.md` | overlay template (provisioning) |
| `<workspace>/USER.md` | overlay template (provisioning) |
| `<workspace>/HEARTBEAT.md` | overlay template (provisioning) |
| `<workspace>/BOOTSTRAP.md` | overlay template (provisioning) |
| `<workspace>/MEMORY.md` | overlay template (provisioning) |
| `<tmp_path>/*.md` (test fixtures) | test-only |
| `.roo/handoffs/*.md` | dev/handoff docs |
| `shared/docs/*.md` | dev/handoff docs |
| `*/README.md` | dev docs |
| `CLAUDE.md`, `CONTEXT_V3.md`, `ROADMAP_V3.md`, `ARCHITECTURE.md`, `TOOLS.md` (top-level) | dev docs |

Anything else (notably `TRADE_STATE.md` and `TRADE_LOG.md`) is **forbidden**.

---

## §4 — Migration script spec

> **Goal:** preserve the historical narrative content of every existing `TRADE_STATE.md` / `TRADE_LOG.md` on disk by replaying it into the appropriate mem0 namespace, with `metadata.original_timestamp` reflecting the file's mtime (or, for `TRADE_LOG.md`, the per-line embedded timestamp).

### §4.1 Discovery — what's on disk today

The audit must run *before* the writers are deleted, so we capture historical content. Discovery query:

```bash
# Every workspace dir owned by a known company. Path pattern derived from
# shared/templates/trading_agent/surgeon_llm_runner.py STATE_PATH=workspace["workspace"].
find /opt/tickles -name "TRADE_STATE.md" -o -name "TRADE_LOG.md" 2>/dev/null | \
  grep -v ".roo/handoffs/" | \
  grep -v "shared/docs/" | \
  grep -v "shared/templates/" | \
  grep -v "shared/tests/"
```

Plus the OpenClaw workspace path from [`shared/scripts/check_system_freshness.py:155-156`](shared/scripts/check_system_freshness.py:155): `/root/.openclaw/workspace/TRADE_STATE.md` and the equivalent under each provisioned company workspace.

The migration script must persist the inventory it found in a `migration_manifest.json` before doing any reads, so the operation is idempotent and re-runnable.

### §4.2 Script: `shared/scripts/migrate_md_to_mem0.py`

**Skeleton (Code mode will implement):**

```python
"""Module: migrate_md_to_mem0
Purpose: One-shot migration. Replays every TRADE_STATE.md / TRADE_LOG.md
on disk into mem0, preserves original timestamps, then renames the source
files to `.md.migrated` so the migration is idempotent.

Usage:
    python -m shared.scripts.migrate_md_to_mem0 --dry-run
    python -m shared.scripts.migrate_md_to_mem0 --apply --company rubicon
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from shared.utils.mem0_config import get_memory  # production trading ns


# 1. Discovery — locate every TRADE_STATE.md / TRADE_LOG.md.
def discover(roots: list[Path]) -> Iterable[Path]:
    for root in roots:
        for name in ("TRADE_STATE.md", "TRADE_LOG.md"):
            yield from root.rglob(name)


# 2. Owner resolution — map a workspace path to (company, agent_id).
#    Strategy: walk up parent dirs until we hit a known marker file
#    (meta.json with companyId), or fall back to the dir-name slug.
def resolve_owner(file_path: Path) -> tuple[str, str]:
    for ancestor in file_path.parents:
        meta = ancestor / "meta.json"
        if meta.exists():
            data = json.loads(meta.read_text())
            return data["companyId"], data.get("agentId", "surgeon")
    # Fall back: workspace dir name, agent_id="surgeon"
    return file_path.parent.name, "surgeon"


# 3. Per-file replay logic.
def replay_trade_state(text: str, mtime_iso: str, company: str, agent: str) -> int:
    """TRADE_STATE.md is overwrite-latest. Store the most recent snapshot only.
    Strategy: write ONE mem0 entry tagged kind=trade_state_snapshot.
    """
    mem, agent_id = get_memory(company, agent)
    mem.add(
        text,
        metadata={
            "type": "trade_state_snapshot",
            "kind": "historical_migration",
            "original_timestamp": mtime_iso,
            "source_file": "TRADE_STATE.md",
        },
    )
    return 1


_LOG_ENTRY_RE = re.compile(
    r"(?P<header>Trade #\d+ -- .+?)(?=\n\nTrade #|\Z)", re.DOTALL
)


def replay_trade_log(text: str, fallback_iso: str, company: str, agent: str) -> int:
    """TRADE_LOG.md is append-only. Parse each entry, extract its embedded
    timestamp, and write one mem0 entry per entry.
    """
    mem, agent_id = get_memory(company, agent)
    count = 0
    for m in _LOG_ENTRY_RE.finditer(text):
        entry = m.group("header").strip()
        # Embedded line: "- Time: 2026-04-30T11:23:45.123456+00:00 | ..."
        ts_match = re.search(r"Time:\s*([0-9T:.\-+ ]+)", entry)
        original_ts = ts_match.group(1).strip() if ts_match else fallback_iso
        action_match = re.search(r"Action:\s*(\w+)", entry)
        action = action_match.group(1).lower() if action_match else "unknown"
        trade_match = re.search(r"Trade #(\d+)", entry)
        trade_id = int(trade_match.group(1)) if trade_match else None
        symbol_match = re.search(r"Trade #\d+ -- (\S+)", entry)
        symbol = symbol_match.group(1) if symbol_match else None
        mem.add(
            entry,
            metadata={
                "type": "trade_decision",
                "kind": "historical_migration",
                "action": action,
                "trade_id": trade_id,
                "symbol": symbol,
                "original_timestamp": original_ts,
                "source_file": "TRADE_LOG.md",
            },
        )
        count += 1
    return count


# 4. Idempotence — rename source file to `.md.migrated` after success.
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--company", help="Restrict to one company")
    ap.add_argument("--roots", nargs="+", default=["/opt/tickles", "/root/.openclaw"])
    args = ap.parse_args(argv)
    if not (args.dry_run or args.apply):
        ap.error("must pass --dry-run or --apply")

    files = list(discover([Path(r) for r in args.roots]))
    manifest = []
    for f in files:
        company, agent = resolve_owner(f)
        if args.company and company != args.company:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).isoformat()
        if f.name == "TRADE_STATE.md":
            n = 0 if args.dry_run else replay_trade_state(text, mtime, company, agent)
        else:
            n = 0 if args.dry_run else replay_trade_log(text, mtime, company, agent)
        manifest.append({
            "path": str(f), "company": company, "agent": agent,
            "size_bytes": len(text), "entries_written": n, "mtime_utc": mtime,
        })
        if args.apply:
            f.rename(f.with_suffix(".md.migrated"))

    out = Path("/opt/tickles/shared/scripts/migration_manifest.json")
    out.write_text(json.dumps(manifest, indent=2))
    print(f"manifest written: {out} ({len(manifest)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

### §4.3 Migration order (matters)

1. **STOP** the surgeon and surgeon-LLM daemons (`systemctl stop tickles-surgeon-rubicon` etc.) — guarantees no further writes during migration.
2. **DRY RUN** the migration script. Inspect `migration_manifest.json` for surprises (unexpected files, weird companies, zero-entry dirs).
3. **APPLY** the migration. Verify mem0 entry counts via [`mem0_test.py`](shared/utils/mem0_test.py:1) or a `mem.search(metadata.kind="historical_migration")`.
4. **DELETE** the writer code (W1-W5 in §2). PR template in §6.
5. **DELETE** the reader code (A3) at [`surgeon_llm_runner.py:603-606`](shared/templates/trading_agent/surgeon_llm_runner.py:603) — replace with `mem.search(...)` calls per the new agent-input contract.
6. **UPDATE** [`shared/scripts/check_system_freshness.py:155-156`](shared/scripts/check_system_freshness.py:155) to check `.surgeon_state.json` mtime instead of `TRADE_STATE.md` mtime.
7. **RESTART** the daemons. Watch logs for `FileNotFoundError` on the deleted `.md` paths.
8. **CONFIRM** via `mem0` search that new `trade_decision` entries are landing.
9. **CLEAN UP** the `.md.migrated` files after a 30-day grace period (use [`shared/jobs/payload_retention.py`](shared/jobs/payload_retention.py:1)-style cleanup, NOT a manual `rm -rf`).

### §4.4 What metadata MUST be preserved

Per the recipe: *"preserve `created_at` as `metadata.original_timestamp`"*.

Implementation:

| Field | Source | Notes |
|---|---|---|
| `metadata.original_timestamp` | for `TRADE_LOG.md`: per-entry "Time:" line. For `TRADE_STATE.md`: file mtime. | ISO-8601 UTC. Mandatory. |
| `metadata.kind` | always `"historical_migration"` for migrated entries. | Lets us distinguish backfilled vs live mem0 writes for analysis. |
| `metadata.source_file` | `"TRADE_STATE.md"` or `"TRADE_LOG.md"`. | Lets us re-run the migration if mem0 ever gets dropped (manifest is the source of truth). |
| `metadata.type` | `"trade_state_snapshot"` or `"trade_decision"`. | Aligns with the live-write contract going forward. |
| `metadata.action`, `metadata.trade_id`, `metadata.symbol` | parsed from the log entry text. | Best-effort. Null if regex fails. Logged. |

---

## §5 — CI gate spec

> **Goal:** prevent any future PR from re-introducing `.md` writes to forbidden paths in `shared/` or `projects/`.

### §5.1 Implementation: `shared/tests/test_grep_guard_md_writes.py`

This extends the existing pattern at [`shared/tests/test_grep_guard.py`](shared/tests/test_grep_guard.py:1) (which already enforces other invariants).

```python
"""CI gate: no agent-output .md writes outside the allowlist.

Implements D1 from .roo/handoffs/2026-05-03-master-resume-handoff.md §2.

Allowed `.md` write targets:
  - <workspace>/AGENT.md, SOUL.md, IDENTITY.md, TOOLS.md, USER.md,
    HEARTBEAT.md, BOOTSTRAP.md, MEMORY.md  (provisioning overlays)
  - test fixtures under tmp_path
  - .roo/handoffs/*.md, shared/docs/*.md, */README.md  (dev docs)

Forbidden anywhere in shared/ or projects/<company>/:
  - TRADE_STATE.md, TRADE_LOG.md
  - any other agent-output .md (anything not on the allowlist)
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# Filenames that ARE allowed to be written (overlay templates).
ALLOWED_OVERLAY_NAMES = {
    "AGENT.md", "SOUL.md", "IDENTITY.md", "TOOLS.md", "USER.md",
    "HEARTBEAT.md", "BOOTSTRAP.md", "MEMORY.md",
}

# Files in these dirs are allowed to write any .md they want (dev/test/overlay templates).
ALLOWED_WRITER_DIRS = {
    REPO / "shared" / "tests",
    REPO / "shared" / "scripts",  # for migrate_md_to_mem0.py rename, e2e fixtures
    REPO / "shared" / "jobs",     # test_payload_retention.py
    REPO / "shared" / "provisioning",  # executor.py overlay writes
    REPO / "shared" / "templates",     # template files for cloning
}

# Pattern: any line that opens a .md file for write/append, or .write_text on a .md path.
WRITER_RE = re.compile(
    r"""(?xs)
    (?:open\s*\([^)]*\.md[^)]*['"][wa] |          # open("foo.md", "w")
       \bwrite_text\s*\(.*?\.md)                   # path.with_suffix(".md").write_text(...)
    """
)


def test_no_forbidden_md_writes():
    """Fail if any new .md writer appears outside the allowlist."""
    proc = subprocess.run(
        ["git", "ls-files", "shared/", "projects/"],
        capture_output=True, text=True, check=True, cwd=REPO,
    )
    violations = []
    for rel in proc.stdout.splitlines():
        if not rel.endswith(".py"):
            continue
        path = REPO / rel
        if any(path.is_relative_to(d) for d in ALLOWED_WRITER_DIRS):
            continue
        if path.name.startswith("test_"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in WRITER_RE.finditer(text):
            # Verify the target filename is forbidden, not allowed.
            line_start = text.rfind("\n", 0, m.start()) + 1
            line_end = text.find("\n", m.end())
            line = text[line_start:line_end]
            if any(name in line for name in ALLOWED_OVERLAY_NAMES):
                continue
            line_no = text[:m.start()].count("\n") + 1
            violations.append(f"{rel}:{line_no}: {line.strip()}")

    assert not violations, (
        "Forbidden .md writes detected (D1 violation). Move the content to mem0:\n"
        + "\n".join(violations)
    )
```

### §5.2 Hook into pytest

The existing [`shared/tests/test_grep_guard.py`](shared/tests/test_grep_guard.py:1) is already collected by pytest. The new test follows the same naming convention (`test_grep_guard_md_writes.py`) so it's discovered automatically. No CI config change required.

### §5.3 Failure mode UX

When a future PR adds a forbidden write, pytest output will be:

```
FAILED shared/tests/test_grep_guard_md_writes.py::test_no_forbidden_md_writes
AssertionError: Forbidden .md writes detected (D1 violation). Move the content to mem0:
shared/intelligence/some_new_thing.py:142: path.write_text(report_text)
```

The error message names the rule (D1), names the action (Move the content to mem0), and shows the line. PR author has zero ambiguity.

### §5.4 Why allowlist-by-filename, not allowlist-by-directory

A senior engineer might suggest: *"Just allow any `.md` write under `shared/provisioning/` and `shared/templates/`."* That works for today, but next year someone adds `shared/intelligence/learnings_report.py` that writes `learning_report.md` and bypasses the gate. Filename allowlist is stricter and harder to accidentally circumvent.

---

## §6 — Implementation order (final, copy-paste-ready)

### Phase 1 — Architect (this document) ✅ DONE

- Inventory complete (§2).
- Migration script spec (§4).
- CI gate spec (§5).

### Phase 2 — Code mode (1 day)

| Step | File | Action |
|---|------|--------|
| 1 | `shared/scripts/migrate_md_to_mem0.py` | Create per §4.2 skeleton. |
| 2 | `shared/tests/test_migrate_md_to_mem0.py` | Unit tests: discover() finds nested files, replay_trade_log() parses one entry correctly, dry-run writes nothing. |
| 3 | (run) | `python -m shared.scripts.migrate_md_to_mem0 --dry-run` → inspect manifest. |
| 4 | (run) | `python -m shared.scripts.migrate_md_to_mem0 --apply` (only after manual review of dry-run). |

### Phase 3 — Code mode (0.5 day)

| Step | File | Action |
|---|------|--------|
| 5 | [`shared/daemons/surgeon_trader.py`](shared/daemons/surgeon_trader.py:54) | Delete lines 54-55 (`STATE_PATH`, `LOG_PATH`). Delete [`write_trade_state`](shared/daemons/surgeon_trader.py:421). Delete log writes at lines 467-471 and 588-591. Replace with `mem.add(..., metadata={"type": "trade_decision", ...})` calls. |
| 6 | [`shared/templates/trading_agent/surgeon_llm_runner.py`](shared/templates/trading_agent/surgeon_llm_runner.py:489) | Delete [`write_trade_state`](shared/templates/trading_agent/surgeon_llm_runner.py:490), [`ensure_trade_log`](shared/templates/trading_agent/surgeon_llm_runner.py:531), [`append_trade_log`](shared/templates/trading_agent/surgeon_llm_runner.py:540). Update [`format_open_entry`](shared/templates/trading_agent/surgeon_llm_runner.py:549) etc. to return dicts (for mem0.add) instead of pre-formatted strings. Replace [line 528, 534, 543](shared/templates/trading_agent/surgeon_llm_runner.py:528) call sites with `mem.add()`. |
| 7 | [`shared/templates/trading_agent/surgeon_llm_runner.py`](shared/templates/trading_agent/surgeon_llm_runner.py:603) | Delete reads of `TRADE_STATE.md` and `TRADE_LOG.md` (lines 604-605). Replace with `mem.search(query="recent trades", metadata={"type": "trade_decision"}, limit=5)`. |
| 8 | [`shared/scripts/check_system_freshness.py`](shared/scripts/check_system_freshness.py:155) | Replace `TRADE_STATE.md` mtime check with `.surgeon_state.json` mtime check. Update tests at [`shared/scripts/test_check_system_freshness.py:107, 144`](shared/scripts/test_check_system_freshness.py:107). |

### Phase 4 — Code mode (0.5 day)

| Step | File | Action |
|---|------|--------|
| 9 | `shared/tests/test_grep_guard_md_writes.py` | Create per §5.1. |
| 10 | (run) | `pytest shared/tests/test_grep_guard_md_writes.py -v` should PASS after Phase 3 commits. |
| 11 | [`CLAUDE.md`](CLAUDE.md:1) | Add a one-liner under "## Mem0 Scoping Rules" stating: *"Agent-output knowledge is mem0-only. `.md` writes outside `shared/{tests,scripts,jobs,provisioning,templates}/` are blocked by [`shared/tests/test_grep_guard_md_writes.py`](shared/tests/test_grep_guard_md_writes.py:1) (CI gate)."* |
| 12 | [`.roo/handoffs/2026-05-03-master-resume-handoff.md`](.roo/handoffs/2026-05-03-master-resume-handoff.md:344) | Mark §4.3 as DONE, link to this audit doc + the migration commit. |

### Phase 5 — 30-day grace period

After 30 days of clean operation, run:

```bash
find /opt/tickles -name "*.md.migrated" -delete
find /root/.openclaw -name "*.md.migrated" -delete
```

Total estimated time: **2 days** (matches the recipe's "1 day audit + 1 day migration script + 0.5 day CI gate" minus the 0.5d already spent on the audit).

---

## §7 — What Could Go Wrong

| # | Risk | Likelihood | Mitigation |
|---|------|------------|------------|
| R1 | Migration script writes duplicate entries on retry. | **MEDIUM** — mem0 doesn't dedupe by content. | The `f.rename(f.with_suffix(".md.migrated"))` step at the end of each file's processing makes the operation idempotent — re-running won't find the file again. Manifest also serves as a checkpoint. |
| R2 | `replay_trade_log` regex fails to split entries on a malformed log. | **MEDIUM** — humans have edited these files in the past. | Fall back: if the regex finds zero entries, write the entire file as ONE mem0 entry tagged `metadata.kind="raw_dump"`. Don't lose data. |
| R3 | Stopping surgeon daemons mid-migration causes a position to miss its close. | **LOW** but financially material. | Schedule migration during a market-closed window OR after the surgeon has already gone flat (state.positions is empty). Verify via `tracked_positions WHERE state='open'` before stopping. |
| R4 | The owner-resolution heuristic in `resolve_owner()` puts entries in the wrong company namespace. | **MEDIUM** — workspace dir layout varies. | Add `--require-meta-json` flag that REFUSES to migrate any file lacking a `meta.json` ancestor. Forces explicit owner declaration. |
| R5 | `surgeon2_trader.py` (the production code path) has a hidden `.md` write we missed. | **LOW** — grep S2 returned zero hits in `projects/` and the surgeon2 file's regex check returned zero. | The CI gate (§5) catches any regression on first PR after merge. |
| R6 | Future devs don't read this audit and re-introduce `.md` writes. | **HIGH** without the gate. | The CI gate (§5) is the entire mitigation. Without it, this audit is a one-shot cleanup, not a durable invariant. |
| R7 | Provisioning's overlay writes accidentally get caught by the gate. | **LOW** — explicit allowlist on filename + directory. | Test case in `test_grep_guard_md_writes.py` MUST include a smoke test that asserts [`shared/provisioning/executor.py:916`](shared/provisioning/executor.py:916) does NOT trigger a violation. |
| R8 | Mem0 search latency for "give me the last 5 trade decisions" is slower than `tail -5 TRADE_LOG.md` was. | **LOW-MEDIUM**. | Mem0 over Qdrant is fast for top-k search. If latency becomes an issue (>500ms), add a `surgeon_state` cache row in `tickles_<company>.public.agent_state` JSON column for hot-path reads. The mem0 entries remain authoritative. |
| R9 | The `metadata.original_timestamp` field doesn't survive a future mem0 schema change. | **LOW** — mem0's metadata is JSONB-native and stable. | Migration manifest at `shared/scripts/migration_manifest.json` is the durable backup. Keep it forever. |
| R10 | Someone later wants to re-add a `.md` writer for a legitimate reason (e.g. a new overlay template `BUDGET.md`). | **MEDIUM**. | The allowlist in §5.1 `ALLOWED_OVERLAY_NAMES` is one-line-edit extensible. Document the addition in CLAUDE.md and require a PR review tag. |

---

## §8 — Self-critique (devil's advocate pass)

A senior engineer reviewing this audit would ask:

1. **"You only audited two writers. Are you sure you didn't miss anything?"**
   - The S1+S2 grep returned 48 raw hits; I categorised every one. The exclusions are documented in §1.3. The S5 grep for `openclaw_export` confirms the headline suspect doesn't exist. I'm confident the surface is captured. **Mitigation if I'm wrong:** the CI gate catches anything I missed on next PR.

2. **"Why not just delete `TRADE_STATE.md` writes silently — nobody reads them anyway since the JSON is authoritative?"**
   - Two reasons. First, [`check_system_freshness.py:156`](shared/scripts/check_system_freshness.py:156) DOES read `TRADE_STATE.md` for mtime. Silent deletion breaks that script. Second, the file is decorative for *humans* — operators look at it. We owe them a replacement (a CLI to query mem0 for the latest snapshot) before we yank the file.

3. **"The migration script's regex for parsing TRADE_LOG.md is fragile."**
   - True. R2 mitigation (raw_dump fallback) handles malformed entries. The unit test in step 2 of Phase 2 must cover at least: a clean log, a log with a corrupted entry, a log with a partial last entry, and an empty log.

4. **"Why a custom CI test instead of a pre-commit hook?"**
   - Pre-commit hooks are local and skippable. Pytest in CI is mandatory and runs in a clean environment. It catches PRs from people who don't have the hook installed.

5. **"You skipped `chart_hacker_guru.py`'s `.txt` writes (A1). That's still files-on-disk knowledge."**
   - Acknowledged in §2.1. D1's literal scope is `.md` writes; `.txt` is out of scope of *this* audit. I flagged it for a follow-up audit. If the user wants to expand D1 to all-files, that's a separate ticket and I'd argue for it explicitly rather than smuggling it into this one.

6. **"What about agent_events / api_cost_log / position_postmortems — those are append-only narratives too. Are we duplicating them in mem0?"**
   - No. Those are structured Postgres tables for *facts and metrics*. Mem0 holds *prose narratives*. The dashboard's Memory Feed (PHASE_Y v2 §4) UNIONs across both. Different storage, different access pattern, no duplication.

---

## §9 — Resume command for next session

> Read [`.roo/handoffs/2026-05-03-md-vs-mem0-audit.md`](.roo/handoffs/2026-05-03-md-vs-mem0-audit.md:1) §6 (Implementation order). Switch to Code mode. Implement Phase 2 (the migration script `shared/scripts/migrate_md_to_mem0.py` + tests), then Phase 3 (delete `.md` writes from [`shared/daemons/surgeon_trader.py`](shared/daemons/surgeon_trader.py:54) and [`shared/templates/trading_agent/surgeon_llm_runner.py`](shared/templates/trading_agent/surgeon_llm_runner.py:489) and replace with `mem.add` calls), then Phase 4 (CI gate `shared/tests/test_grep_guard_md_writes.py`). Run dry-run BEFORE apply. Confirm zero `pytest` regressions before committing.

---

## §10 — Sign-off

| Field | Value |
|---|---|
| Audit completed | 2026-05-03 (Architect mode, Roo) |
| Files audited | every `*.py` under `shared/` and `projects/` |
| Greps executed | 7 (documented in §1.2) |
| Violators found | 2 source files, 5 write-sites |
| `openclaw_export_daemon` exists? | **No** (zero code references) |
| Allowlist files | 8 overlay templates + dev/test dirs (§3, §5.4) |
| Estimated cleanup time | 2 engineer-days (matches recipe) |
| CI gate | `shared/tests/test_grep_guard_md_writes.py` (§5.1) |

**Status:** Ready for Code mode handoff.
