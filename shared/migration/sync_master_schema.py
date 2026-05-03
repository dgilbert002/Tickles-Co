"""Module: sync_master_schema
Purpose: pg_dump-and-diff CI tool that fails if master templates drift from live schema.
Location: /opt/tickles/shared/migration/sync_master_schema.py
"""

import argparse
import asyncio
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)

MIGRATION_DIR = Path(__file__).parent
SHARED_TEMPLATE = MIGRATION_DIR / "tickles_shared_pg.sql"
COMPANY_TEMPLATE = MIGRATION_DIR / "tickles_company_pg.sql"


async def _get_live_dsn(db_name: str) -> str:
    """Build DSN for a live database from env vars."""
    template = os.environ.get(
        "TICKLES_DB_DSN_TEMPLATE",
        "postgresql://{user}:{password}@{host}:{port}/{db}",
    )
    if "{user}" in template:
        template = template.format(
            user=os.getenv("DB_USER", "tickles"),
            password=os.getenv("DB_PASSWORD", ""),
            host=os.getenv("DB_HOST", "localhost"),
            port=os.getenv("DB_PORT", "5432"),
            db="{db}",
        )
    return template.format(db=db_name)


def _normalize_dump(sql: str) -> str:
    """Normalize pg_dump output for comparison.

    Strips:
      - Leading/trailing whitespace per line
      - Empty lines
      - Comments (lines starting with --)
      - SET statements (pg_dump noise)
      - SELECT pg_catalog.set_config noise
    """
    lines = []
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("--"):
            continue
        if stripped.startswith("SET "):
            continue
        if "pg_catalog.set_config" in stripped:
            continue
        lines.append(stripped)
    return "\n".join(lines)


def _run_pg_dump(db_name: str) -> str:
    """Run pg_dump --schema-only and return normalized SQL."""
    dsn = asyncio.run(_get_live_dsn(db_name))
    # Parse DSN for pg_dump connection params
    # Simple parsing: postgresql://user:pass@host:port/db
    # pg_dump uses -h host -U user -d db
    # We need to extract components
    # For simplicity, use PGPASSWORD env and pg_dump with URI
    env = os.environ.copy()
    cmd = [
        "pg_dump",
        "--schema-only",
        "--no-owner",
        "--no-privileges",
        "--no-comments",
        dsn,
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return _normalize_dump(result.stdout)


def _read_template(path: Path) -> str:
    """Read and normalize a template SQL file."""
    if not path.exists():
        raise FileNotFoundError(f"Template not found: {path}")
    return _normalize_dump(path.read_text())


def _diff(a: str, b: str, label_a: str, label_b: str) -> str:
    """Return unified diff between two normalized SQL strings."""
    import difflib

    a_lines = a.splitlines(keepends=True)
    b_lines = b.splitlines(keepends=True)
    diff = difflib.unified_diff(
        a_lines,
        b_lines,
        fromfile=label_a,
        tofile=label_b,
        lineterm="",
    )
    return "".join(diff)


async def check_shared_schema() -> tuple[bool, str]:
    """Check if tickles_shared live schema matches the template.

    Returns:
        (is_match, diff_or_message)
    """
    try:
        live = _run_pg_dump("tickles_shared")
        template = _read_template(SHARED_TEMPLATE)
    except Exception as exc:
        return False, f"Error generating dump: {exc}"

    if live == template:
        return True, "Schemas match"

    diff = _diff(template, live, str(SHARED_TEMPLATE), "tickles_shared_live")
    return False, diff


async def check_company_schema(company: str = "rubicon") -> tuple[bool, str]:
    """Check if a company DB live schema matches the template.

    Returns:
        (is_match, diff_or_message)
    """
    db_name = f"tickles_{company}"
    try:
        live = _run_pg_dump(db_name)
        template = _read_template(COMPANY_TEMPLATE)
    except Exception as exc:
        return False, f"Error generating dump for {db_name}: {exc}"

    if live == template:
        return True, "Schemas match"

    diff = _diff(template, live, str(COMPANY_TEMPLATE), f"{db_name}_live")
    return False, diff


async def run_check(
    shared: bool = True,
    company: Optional[str] = "rubicon",
) -> bool:
    """Run schema drift checks.

    Returns:
        True if all checks pass, False if any drift detected.
    """
    all_pass = True

    if shared:
        match, msg = await check_shared_schema()
        if match:
            logger.info("[PASS] tickles_shared matches template")
        else:
            all_pass = False
            logger.error("[FAIL] tickles_shared drift detected:\n%s", msg)

    if company:
        match, msg = await check_company_schema(company)
        if match:
            logger.info("[PASS] %s matches template", f"tickles_{company}")
        else:
            all_pass = False
            logger.error("[FAIL] %s drift detected:\n%s", f"tickles_{company}", msg)

    return all_pass


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Check live schema against master templates. Exit 1 on drift."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run the drift check (default behaviour)",
    )
    parser.add_argument(
        "--no-shared",
        action="store_true",
        help="Skip shared schema check",
    )
    parser.add_argument(
        "--company",
        default="rubicon",
        help="Company DB to check against company template (default: rubicon)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable debug logging"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    all_pass = asyncio.run(
        run_check(
            shared=not args.no_shared,
            company=args.company if not args.no_shared else None,
        )
    )

    if all_pass:
        print("OK: No schema drift detected.")
        sys.exit(0)
    else:
        print("FAIL: Schema drift detected — see logs above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
