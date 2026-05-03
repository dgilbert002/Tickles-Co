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
        if not path.exists():
            # Tracked but already deleted on disk (pending commit). Skip.
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
