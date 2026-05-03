"""
Module: migrate_md_to_mem0
Purpose: One-shot migration. Replays every TRADE_STATE.md / TRADE_LOG.md
         on disk into mem0, preserves original timestamps, then renames
         the source files to `.md.migrated` so the migration is idempotent.
Location: /opt/tickles/shared/scripts/migrate_md_to_mem0.py

Usage:
    python -m shared.scripts.migrate_md_to_mem0 --dry-run
    python -m shared.scripts.migrate_md_to_mem0 --apply --company rubicon
    python -m shared.scripts.migrate_md_to_mem0 --dry-run --allow-fallback-owner

Implements §4.2 of .roo/handoffs/2026-05-03-md-vs-mem0-audit.md.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

# Constants
DEFAULT_ROOTS: list[str] = ["/opt/tickles", "/root/.openclaw"]
TARGET_NAMES: tuple[str, ...] = ("TRADE_STATE.md", "TRADE_LOG.md")
MANIFEST_PATH: Path = Path("/opt/tickles/shared/scripts/migration_manifest.json")

# Skip directories that contain template files, dev docs, handoffs, or already-migrated content.
EXCLUDE_DIR_PARTS: tuple[str, ...] = (
    ".roo",
    "shared/docs",
    "shared/templates",
    "shared/tests",
    ".git",
    "node_modules",
    "_archive",
)

# Per-entry parser for TRADE_LOG.md. Twilly format:
#   "Trade #123 -- BTCUSDT
#    - Time: 2026-04-30T11:23:45.123456+00:00
#    - Action: OPEN
#    ..."
_LOG_ENTRY_RE = re.compile(
    r"(?P<header>Trade #\d+ -- .+?)(?=\n\nTrade #|\Z)", re.DOTALL
)


# ---------------------------------------------------------------------------
# 1. Discovery
# ---------------------------------------------------------------------------
def _is_excluded(path: Path) -> bool:
    """Check whether a path lies under an excluded directory.

    Args:
        path: Absolute path to inspect.

    Returns:
        True if the path should be skipped, False otherwise.
    """
    try:
        s = str(path)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("could not stringify %r: %s", path, exc)
        return True
    return any(part in s for part in EXCLUDE_DIR_PARTS)


def discover(roots: list[Path]) -> Iterable[Path]:
    """Locate every TRADE_STATE.md / TRADE_LOG.md under the given roots.

    Args:
        roots: List of root directories to search recursively.

    Yields:
        Absolute paths to candidate files (excluded dirs filtered out).
    """
    for root in roots:
        if not root.exists():
            logger.info("root does not exist, skipping: %s", root)
            continue
        for name in TARGET_NAMES:
            try:
                for p in root.rglob(name):
                    if _is_excluded(p):
                        continue
                    yield p
            except (PermissionError, OSError) as exc:
                logger.warning("rglob failed under %s: %s", root, exc)


# ---------------------------------------------------------------------------
# 2. Owner resolution
# ---------------------------------------------------------------------------
def resolve_owner(
    file_path: Path, *, allow_fallback: bool = False
) -> tuple[str, str]:
    """Map a workspace path to (company, agent_id).

    Walks parent directories looking for `meta.json` containing `companyId`.
    Falls back to the workspace dir name + agent_id="surgeon" only if
    `allow_fallback=True`.

    Args:
        file_path: Path to the .md file being migrated.
        allow_fallback: If True, use dir-name slug when no meta.json is found.

    Returns:
        Tuple of (company_slug, agent_id).

    Raises:
        FileNotFoundError: If no meta.json ancestor and allow_fallback=False.
        ValueError: If meta.json is malformed.
    """
    for ancestor in file_path.parents:
        meta = ancestor / "meta.json"
        if not meta.exists():
            continue
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"meta.json at {meta} is not valid JSON: {exc}"
            ) from exc
        company = data.get("companyId")
        if not company:
            raise ValueError(
                f"meta.json at {meta} missing required 'companyId' field"
            )
        agent = data.get("agentId", "surgeon")
        return str(company), str(agent)
    if allow_fallback:
        return file_path.parent.name, "surgeon"
    raise FileNotFoundError(
        f"no meta.json ancestor for {file_path}; "
        "pass --allow-fallback-owner to use the dir-name slug"
    )


# ---------------------------------------------------------------------------
# 3. Per-file replay logic
# ---------------------------------------------------------------------------
def replay_trade_state(
    text: str, mtime_iso: str, company: str, agent: str, *, dry_run: bool
) -> int:
    """Replay a single TRADE_STATE.md snapshot into mem0.

    TRADE_STATE.md is overwrite-latest. Store the most recent snapshot only
    as ONE mem0 entry tagged `kind=historical_migration`.

    Args:
        text: Full file text.
        mtime_iso: File mtime as ISO-8601 UTC string.
        company: Company slug (mem0 collection scope).
        agent: Agent id (mem0 user scope).
        dry_run: If True, do not write to mem0; return planned count only.

    Returns:
        Number of mem0 entries written (1 on success, 0 on dry-run).
    """
    if dry_run:
        return 0
    try:
        from shared.utils.mem0_config import get_memory
    except ImportError as exc:
        logger.error("mem0 import failed; cannot apply: %s", exc)
        raise
    try:
        mem, agent_id = get_memory(company, agent)
        mem.add(
            text,
            user_id=company,
            agent_id=agent_id,
            metadata={
                "type": "trade_state_snapshot",
                "kind": "historical_migration",
                "original_timestamp": mtime_iso,
                "source_file": "TRADE_STATE.md",
            },
        )
        return 1
    except Exception as exc:
        logger.error(
            "replay_trade_state failed for company=%s agent=%s: %s",
            company,
            agent,
            exc,
        )
        raise


def _parse_log_entry(entry: str, fallback_iso: str) -> dict[str, Any]:
    """Extract structured metadata from a Twilly trade-log entry.

    Args:
        entry: Single trade-log entry text (Twilly format).
        fallback_iso: ISO timestamp to use when the entry has no Time: line.

    Returns:
        Dict with keys: original_timestamp, action, trade_id, symbol.
    """
    ts_match = re.search(r"Time:\s*([0-9T:.\-+ ]+)", entry)
    original_ts = ts_match.group(1).strip() if ts_match else fallback_iso
    action_match = re.search(r"Action:\s*(\w+)", entry)
    action = action_match.group(1).lower() if action_match else "unknown"
    trade_match = re.search(r"Trade #(\d+)", entry)
    trade_id: Optional[int] = int(trade_match.group(1)) if trade_match else None
    symbol_match = re.search(r"Trade #\d+ -- (\S+)", entry)
    symbol: Optional[str] = symbol_match.group(1) if symbol_match else None
    return {
        "original_timestamp": original_ts,
        "action": action,
        "trade_id": trade_id,
        "symbol": symbol,
    }


def replay_trade_log(
    text: str, fallback_iso: str, company: str, agent: str, *, dry_run: bool
) -> int:
    """Replay a TRADE_LOG.md file into mem0, one entry per trade.

    TRADE_LOG.md is append-only. Parse each entry, extract its embedded
    timestamp, and write one mem0 entry per parsed entry. If the regex
    finds zero entries (R2 fallback per audit §7), write the entire file
    as ONE mem0 entry tagged `kind="raw_dump"` so we never lose data.

    Args:
        text: Full file text.
        fallback_iso: ISO-8601 UTC timestamp (file mtime) used when an
                      entry lacks a `Time:` line.
        company: Company slug.
        agent: Agent id.
        dry_run: If True, do not write; return planned count only.

    Returns:
        Number of mem0 entries that were (or would have been) written.
    """
    matches = list(_LOG_ENTRY_RE.finditer(text))
    # R2 fallback: malformed/empty/non-Twilly file → one raw_dump entry.
    if not matches:
        if not text.strip():
            logger.info(
                "empty TRADE_LOG.md for company=%s agent=%s; skipping",
                company,
                agent,
            )
            return 0
        if dry_run:
            return 1
        try:
            from shared.utils.mem0_config import get_memory
        except ImportError as exc:
            logger.error("mem0 import failed; cannot apply: %s", exc)
            raise
        try:
            mem, agent_id = get_memory(company, agent)
            mem.add(
                text,
                user_id=company,
                agent_id=agent_id,
                metadata={
                    "type": "trade_decision",
                    "kind": "raw_dump",
                    "original_timestamp": fallback_iso,
                    "source_file": "TRADE_LOG.md",
                },
            )
            return 1
        except Exception as exc:
            logger.error(
                "replay_trade_log raw_dump failed for company=%s: %s",
                company,
                exc,
            )
            raise

    if dry_run:
        return len(matches)

    try:
        from shared.utils.mem0_config import get_memory
    except ImportError as exc:
        logger.error("mem0 import failed; cannot apply: %s", exc)
        raise

    try:
        mem, agent_id = get_memory(company, agent)
    except Exception as exc:
        logger.error("get_memory failed for company=%s: %s", company, exc)
        raise

    count = 0
    for m in matches:
        entry = m.group("header").strip()
        parsed = _parse_log_entry(entry, fallback_iso)
        try:
            mem.add(
                entry,
                user_id=company,
                agent_id=agent_id,
                metadata={
                    "type": "trade_decision",
                    "kind": "historical_migration",
                    "action": parsed["action"],
                    "trade_id": parsed["trade_id"],
                    "symbol": parsed["symbol"],
                    "original_timestamp": parsed["original_timestamp"],
                    "source_file": "TRADE_LOG.md",
                },
            )
            count += 1
        except Exception as exc:
            logger.error(
                "mem.add failed for trade_id=%s in company=%s: %s",
                parsed.get("trade_id"),
                company,
                exc,
            )
            # Continue with remaining entries; do not lose partial progress.
            continue
    return count


# ---------------------------------------------------------------------------
# 4. Manifest + idempotence orchestration
# ---------------------------------------------------------------------------
def _file_mtime_iso(p: Path) -> str:
    """Return the file's mtime as an ISO-8601 UTC string.

    Args:
        p: Path to inspect.

    Returns:
        ISO-8601 UTC timestamp string.
    """
    try:
        return datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError as exc:
        logger.warning("stat failed for %s: %s", p, exc)
        return datetime.now(tz=timezone.utc).isoformat()


def build_manifest(
    files: list[Path], *, company_filter: Optional[str], allow_fallback: bool
) -> list[dict[str, Any]]:
    """Build a manifest entry per discovered file (no mem0 writes).

    Args:
        files: List of discovered .md paths.
        company_filter: If set, only files owned by this company are kept.
        allow_fallback: Pass-through to resolve_owner.

    Returns:
        Manifest entries (each: path, company, agent, size_bytes, mtime_utc,
        kind, status). Files with unresolved owners are flagged with
        status="skipped_no_owner".
    """
    manifest: list[dict[str, Any]] = []
    for f in files:
        entry: dict[str, Any] = {
            "path": str(f),
            "company": None,
            "agent": None,
            "size_bytes": None,
            "mtime_utc": None,
            "kind": f.name,
            "status": "pending",
        }
        try:
            company, agent = resolve_owner(f, allow_fallback=allow_fallback)
        except (FileNotFoundError, ValueError) as exc:
            logger.warning("owner resolution failed for %s: %s", f, exc)
            entry["status"] = "skipped_no_owner"
            entry["error"] = str(exc)
            manifest.append(entry)
            continue
        if company_filter and company != company_filter:
            entry["status"] = "skipped_filtered"
            entry["company"] = company
            entry["agent"] = agent
            manifest.append(entry)
            continue
        try:
            size_bytes = f.stat().st_size
        except OSError as exc:
            logger.warning("stat failed for %s: %s", f, exc)
            size_bytes = -1
        entry.update(
            {
                "company": company,
                "agent": agent,
                "size_bytes": size_bytes,
                "mtime_utc": _file_mtime_iso(f),
            }
        )
        manifest.append(entry)
    return manifest


def write_manifest(manifest: list[dict[str, Any]], out: Path) -> None:
    """Persist the manifest to disk BEFORE any mem0 writes (crash-safety).

    Args:
        manifest: List of manifest entries.
        out: Destination path.

    Raises:
        OSError: If the manifest cannot be written.
    """
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.error("could not write manifest to %s: %s", out, exc)
        raise


def process_file(
    f: Path,
    entry: dict[str, Any],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Replay a single file into mem0 and (if --apply) rename it.

    Args:
        f: Path to the .md file.
        entry: Manifest entry (mutated in-place with `entries_written` and
               final `status`).
        dry_run: If True, no mem0 writes and no rename.

    Returns:
        The mutated manifest entry.
    """
    if entry["status"] != "pending":
        return entry
    company = entry["company"]
    agent = entry["agent"]
    mtime_iso = entry["mtime_utc"]
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.error("read failed for %s: %s", f, exc)
        entry["status"] = "error_read"
        entry["error"] = str(exc)
        return entry

    # Detect whether the source had migratable content BEFORE we replay.
    # Used by the fail-fast guard below to distinguish between "legitimate
    # nothing-to-do" (empty/whitespace file, or a TRADE_LOG.md with zero
    # parseable entries) and "expected entries but mem.add silently failed".
    stripped = text.strip()
    if not stripped:
        source_had_content = False
    elif f.name == "TRADE_STATE.md":
        # Any non-empty state file is migratable content.
        source_had_content = True
    else:
        # TRADE_LOG.md: content is migratable if either the regex matches at
        # least one entry, OR (R2 fallback) the file is non-empty (raw_dump
        # path will write exactly 1 entry).
        source_had_content = True

    try:
        if f.name == "TRADE_STATE.md":
            n = replay_trade_state(
                text, mtime_iso, company, agent, dry_run=dry_run
            )
        else:
            n = replay_trade_log(
                text, mtime_iso, company, agent, dry_run=dry_run
            )
    except Exception as exc:
        logger.error("replay failed for %s: %s", f, exc)
        entry["status"] = "error_replay"
        entry["error"] = str(exc)
        entry["entries_written"] = 0
        return entry

    entry["entries_written"] = n
    if dry_run:
        entry["status"] = "dry_run_planned"
        return entry

    # Guard: refuse to rename source if zero entries actually landed in mem0.
    # Without this, a silent failure inside the per-entry try/except can mark
    # status='applied' with entries_written=0 and rename the source — losing
    # the .md content. See .roo/handoffs/2026-05-03-md-vs-mem0-apply-FAILED.md
    # §3.3.
    if n == 0 and source_had_content:
        entry["status"] = "error_zero_entries"
        entry["error"] = (
            "refusing to rename: source had migratable content but "
            "entries_written=0 (per-entry mem.add failures swallowed?)"
        )
        logger.error(
            "refusing to rename %s: entries_written=0 on non-empty source",
            f,
        )
        return entry

    # Idempotence: rename .md → .md.migrated only after successful replay.
    try:
        target = f.with_suffix(".md.migrated")
        f.rename(target)
        entry["renamed_to"] = str(target)
        entry["status"] = "applied"
    except OSError as exc:
        logger.error("rename failed for %s: %s", f, exc)
        entry["status"] = "applied_no_rename"
        entry["error"] = str(exc)
    return entry


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser.

    Returns:
        Configured ArgumentParser.
    """
    ap = argparse.ArgumentParser(
        description=(
            "Migrate TRADE_STATE.md / TRADE_LOG.md files into mem0. "
            "One of --dry-run or --apply is required."
        )
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover and plan, but do not write to mem0 or rename files.",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Actually write to mem0 and rename source files to .md.migrated.",
    )
    ap.add_argument(
        "--company",
        default=None,
        help="Restrict the migration to a single company slug.",
    )
    ap.add_argument(
        "--roots",
        nargs="+",
        default=DEFAULT_ROOTS,
        help=f"Roots to scan (default: {DEFAULT_ROOTS}).",
    )
    ap.add_argument(
        "--allow-fallback-owner",
        action="store_true",
        help=(
            "If no meta.json ancestor is found, fall back to the parent dir "
            "name + agent_id='surgeon'. Without this flag, such files are "
            "logged and skipped."
        ),
    )
    ap.add_argument(
        "--manifest",
        default=str(MANIFEST_PATH),
        help=f"Where to write the manifest (default: {MANIFEST_PATH}).",
    )
    ap.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argv override (used by tests).

    Returns:
        Process exit code (0 on success, 2 on argparse error).
    """
    ap = _build_arg_parser()
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.dry_run == args.apply:
        ap.error("must pass exactly one of --dry-run or --apply")

    roots = [Path(r) for r in args.roots]
    files = sorted(set(discover(roots)))
    logger.info("discovered %d candidate files under roots=%s", len(files), roots)

    manifest = build_manifest(
        files,
        company_filter=args.company,
        allow_fallback=args.allow_fallback_owner,
    )

    # Crash-safety: persist the manifest BEFORE any mem0 writes.
    manifest_path = Path(args.manifest)
    write_manifest(manifest, manifest_path)
    logger.info("manifest written: %s (%d entries)", manifest_path, len(manifest))

    # Now process each file. Manifest entries are mutated in-place.
    for entry in manifest:
        f = Path(entry["path"])
        process_file(f, entry, dry_run=args.dry_run)

    # Re-persist the now-completed manifest.
    write_manifest(manifest, manifest_path)

    # Summary print (stdout, not logging — operator-friendly).
    summary = {
        "total": len(manifest),
        "applied": sum(1 for e in manifest if e["status"] == "applied"),
        "applied_no_rename": sum(
            1 for e in manifest if e["status"] == "applied_no_rename"
        ),
        "dry_run_planned": sum(
            1 for e in manifest if e["status"] == "dry_run_planned"
        ),
        "skipped_no_owner": sum(
            1 for e in manifest if e["status"] == "skipped_no_owner"
        ),
        "skipped_filtered": sum(
            1 for e in manifest if e["status"] == "skipped_filtered"
        ),
        "errors": sum(
            1
            for e in manifest
            if e["status"] in ("error_read", "error_replay")
        ),
        "entries_written_total": sum(
            int(e.get("entries_written") or 0) for e in manifest
        ),
    }
    sys.stdout.write(
        f"manifest: {manifest_path}\n"
        f"summary: {json.dumps(summary, indent=2)}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
