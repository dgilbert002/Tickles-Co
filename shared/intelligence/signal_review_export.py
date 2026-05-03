"""
Module: signal_review_export
Purpose: Daemon that exports signal_interpretations to CSV + HTML every 60s.
Location: /opt/tickles/shared/intelligence/signal_review_export.py
"""

import asyncio
import base64
import csv
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import asyncpg
from jinja2 import Environment, FileSystemLoader, select_autoescape

from shared.utils.companies import get_company_dsn, list_active_companies

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CSV column contract — frozen; downstream Phase L re-reads this.
# ---------------------------------------------------------------------------
CSV_COLUMNS: Tuple[str, ...] = (
    "created_at",
    "news_item_id",
    "media_item_id",
    "platform",
    "trader_handle",
    "symbol",
    "instrument_symbol_normalised",
    "instrument_exchange",
    "prefilter_result",
    "vision_provider",
    "vision_model_resolved",
    "prompt_version",
    "llm_direction",
    "llm_confidence",
    "quant_direction",
    "quant_confidence",
    "consensus_direction",
    "consensus_confidence",
    "llm_levels_json",
    "discord_url",
    "media_local_path",
    "llm_raw_request_path",
    "llm_raw_response_path",
    "correlation_id",
    "total_cost_usd",
)

# ---------------------------------------------------------------------------
# Thumbnail size-cap policy [AM]
# ---------------------------------------------------------------------------
THUMB_INLINE_MAX_BYTES = 200 * 1024
THUMB_LINK_MAX_BYTES = 5 * 1024 * 1024
HTML_AGGREGATE_MAX_BYTES = 50 * 1024 * 1024

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------
_LOOKBACK_H = int(os.environ.get("SIGNAL_REVIEW_LOOKBACK_H", "24"))
_REPORT_DIR = Path(os.environ.get("SIGNAL_REVIEW_REPORT_DIR", "shared/reports/signal_review"))
if not _REPORT_DIR.is_absolute():
    _REPORT_DIR = Path("/opt/tickles") / _REPORT_DIR
_PUBLIC_BASE_URL = os.environ.get(
    "SIGNAL_REVIEW_PUBLIC_BASE_URL",
    "https://vmi3220412.trout-goblin.ts.net/opticals/signal_review",
)

# ---------------------------------------------------------------------------
# Jinja2 environment with autoescape [AN]
# ---------------------------------------------------------------------------
_TEMPLATE_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(enabled_extensions=("html", "jinja2"), default_for_string=True),
    trim_blocks=True,
    lstrip_blocks=True,
    cache_size=0,
)
_env.globals["len"] = len


def _image_mime_type(path: str) -> str:
    """Return MIME type from file extension."""
    ext = Path(path).suffix.lower()
    mapping = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }
    return mapping.get(ext, "image/png")


def render_thumb(media_local_path: Optional[str]) -> str:
    """Return safe HTML for a chart thumbnail per [AM] size-cap policy.

    Parameters
    ----------
    media_local_path:
        Absolute or relative path to the image file on disk.

    Returns
    -------
    str:
        HTML snippet (safe to mark |safe in Jinja).
    """
    if not media_local_path:
        return '<span class="thumb-missing">(missing)</span>'
    p = Path(media_local_path)
    try:
        size = p.stat().st_size
    except OSError:
        return '<span class="thumb-missing">(missing)</span>'
    if size > THUMB_LINK_MAX_BYTES:
        logger.warning(
            "oversized_thumbnail",
            extra={"path": str(p), "bytes": size},
        )
        return (
            f'<a href="file://{p}" class="thumb-oversized">'
            f"oversized {size // 1024} KB</a>"
        )
    if size > THUMB_INLINE_MAX_BYTES:
        return f'<img src="file://{p}" loading="lazy" class="thumb-link">'
    try:
        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    except OSError:
        return '<span class="thumb-missing">(missing)</span>'
    mime = _image_mime_type(str(p))
    return f'<img src="data:{mime};base64,{b64}" class="thumb-inline">'


_env.globals["render_thumb"] = render_thumb


# ---------------------------------------------------------------------------
# Symlink integrity check
# ---------------------------------------------------------------------------
def _assert_symlink_integrity() -> None:
    """Raise RuntimeError if the opticals symlink is broken or missing."""
    opticals = Path("/opt/tickles/opticals/signal_review")
    if not opticals.is_symlink():
        raise RuntimeError(
            f"Expected {opticals} to be a symlink to shared/reports/signal_review"
        )
    real = opticals.resolve()
    expected = Path("/opt/tickles/shared/reports/signal_review").resolve()
    if real != expected:
        raise RuntimeError(
            f"Symlink {opticals} resolves to {real}, expected {expected}"
        )


# ---------------------------------------------------------------------------
# Database fetch
# ---------------------------------------------------------------------------
async def _fetch_rows_for_company(company: str, lookback_h: int) -> List[Dict[str, Any]]:
    """Fetch signal_interpretations rows for a single company.

    Parameters
    ----------
    company:
        Company short-name (e.g. 'rubicon').
    lookback_h:
        Hours to look back.

    Returns
    -------
    List[Dict[str, Any]]:
        Row dicts matching CSV_COLUMNS.
    """
    dsn = await get_company_dsn(company)
    try:
        conn = await asyncpg.connect(dsn)
    except Exception as exc:
        logger.error("Cannot connect to %s: %s", company, exc)
        return []
    try:
        rows = await conn.fetch(
            f"""
            SELECT
                si.created_at,
                si.news_item_id,
                si.media_item_id,
                si.platform,
                tp.trader_handle,
                si.symbol,
                si.instrument_symbol_normalised,
                si.instrument_exchange,
                si.prefilter_result,
                si.prefilter_provider AS vision_provider,
                si.vision_model_resolved,
                si.prompt_version,
                si.llm_direction,
                si.llm_confidence,
                si.quant_direction,
                si.quant_confidence,
                si.consensus_direction,
                si.consensus_confidence,
                si.llm_levels_json,
                ni.discord_url,
                mi.local_path AS media_local_path,
                si.llm_raw_request_path,
                si.llm_raw_response_path,
                si.correlation_id,
                si.total_cost_usd
            FROM signal_interpretations si
            LEFT JOIN news_items ni ON si.news_item_id = ni.id
            LEFT JOIN media_items mi ON si.media_item_id = mi.id
            LEFT JOIN trader_profiles tp ON si.trader_profile_id = tp.id
            WHERE si.created_at >= NOW() - INTERVAL '{lookback_h} hours'
            ORDER BY si.created_at DESC
            LIMIT 500
            """,
        )
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.error("Query failed for %s: %s", company, exc)
        return []
    finally:
        await conn.close()


async def _fetch_all_rows(lookback_h: int) -> List[Dict[str, Any]]:
    """Fetch rows across all active companies, sorted by created_at DESC."""
    companies = await list_active_companies()
    all_rows: List[Dict[str, Any]] = []
    for company in companies:
        rows = await _fetch_rows_for_company(company, lookback_h)
        for r in rows:
            r["_company"] = company
        all_rows.extend(rows)
    all_rows.sort(key=lambda r: r.get("created_at") or datetime.min, reverse=True)
    return all_rows[:500]


# ---------------------------------------------------------------------------
# CSV writer
# ---------------------------------------------------------------------------
def _write_csv(rows: List[Dict[str, Any]], report_dir: Path) -> Path:
    """Write timestamped CSV + atomic latest.csv symlink.

    Parameters
    ----------
    rows:
        Row dicts.
    report_dir:
        Output directory.

    Returns
    -------
    Path:
        Path to the timestamped CSV file.
    """
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    csv_path = report_dir / f"signal_review_{ts}.csv"
    tmp_path = report_dir / f"signal_review_{ts}.csv.tmp"
    latest_tmp = report_dir / "latest.csv.tmp"
    latest_final = report_dir / "latest.csv"

    with tmp_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    os.replace(tmp_path, csv_path)

    # Symlink latest.csv → timestamped file
    if latest_final.exists() or latest_final.is_symlink():
        latest_final.unlink()
    latest_final.symlink_to(csv_path.name)

    return csv_path


# ---------------------------------------------------------------------------
# HTML writer
# ---------------------------------------------------------------------------
def _render_html(rows: List[Dict[str, Any]], lookback_h: int, truncated: bool = False, banner: str = "") -> str:
    """Render the Jinja2 template to a string.

    Parameters
    ----------
    rows:
        Row dicts.
    lookback_h:
        Lookback window in hours.
    truncated:
        Whether the report was truncated due to size.
    banner:
        Truncation banner text.

    Returns
    -------
    str:
        Rendered HTML.
    """
    # Fresh environment on every render so template edits are picked up
    # immediately (important in tests and during development).
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(enabled_extensions=("html", "jinja2"), default_for_string=True),
        trim_blocks=True,
        lstrip_blocks=True,
        cache_size=0,
    )
    env.globals["len"] = len
    env.globals["render_thumb"] = render_thumb
    template = env.get_template("signal_review.html.jinja2")
    return template.render(
        rows=rows,
        generated_at=datetime.now(timezone.utc).isoformat(),
        lookback_h=lookback_h,
        truncated=truncated,
        truncation_banner=banner,
    )


def _write_html(rows: List[Dict[str, Any]], report_dir: Path, lookback_h: int) -> Tuple[Path, bool, str]:
    """Write timestamped HTML + atomic latest.html, respecting aggregate cap.

    Returns
    -------
    Tuple[Path, bool, str]:
        (timestamped_path, was_truncated, banner_text)
    """
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    html_path = report_dir / f"signal_review_{ts}.html"
    tmp_path = report_dir / f"signal_review_{ts}.html.tmp"
    latest_tmp = report_dir / "latest.html.tmp"
    latest_final = report_dir / "latest.html"

    html = _render_html(rows, lookback_h)
    encoded = html.encode("utf-8")

    truncated = False
    banner = ""
    if len(encoded) > HTML_AGGREGATE_MAX_BYTES:
        logger.warning(
            "report_truncated_due_to_size",
            extra={
                "bytes": len(encoded),
                "cap": HTML_AGGREGATE_MAX_BYTES,
                "rows_before": len(rows),
            },
        )
        # Drop oldest rows until we fit
        while len(encoded) > HTML_AGGREGATE_MAX_BYTES and len(rows) > 1:
            rows.pop()
            html = _render_html(rows, lookback_h, truncated=True)
            encoded = html.encode("utf-8")
        truncated = True
        banner = (
            f"Report truncated from {len(encoded)} bytes to fit under "
            f"{HTML_AGGREGATE_MAX_BYTES // (1024 * 1024)} MB cap. "
            f"Showing {len(rows)} most recent rows."
        )
        html = _render_html(rows, lookback_h, truncated=True, banner=banner)
        encoded = html.encode("utf-8")

    tmp_path.write_bytes(encoded)
    os.replace(tmp_path, html_path)

    # Atomic latest.html swap
    latest_tmp.write_bytes(encoded)
    os.replace(latest_tmp, latest_final)

    return html_path, truncated, banner


# ---------------------------------------------------------------------------
# Tick function
# ---------------------------------------------------------------------------
async def _tick(report_dir: Path, lookback_h: int) -> Dict[str, Any]:
    """One export tick: fetch, write CSV + HTML.

    Parameters
    ----------
    report_dir:
        Output directory.
    lookback_h:
        Hours to look back.

    Returns
    -------
    Dict[str, Any]:
        Summary dict for daemon stats.
    """
    start = datetime.now(timezone.utc)
    rows = await _fetch_all_rows(lookback_h)
    if not rows:
        return {"ok": True, "rows": 0, "csv": None, "html": None, "elapsed_ms": 0}

    csv_path = _write_csv(rows, report_dir)
    html_path, truncated, banner = _write_html(rows, report_dir, lookback_h)

    elapsed = (datetime.now(timezone.utc) - start).total_seconds() * 1000
    return {
        "ok": True,
        "rows": len(rows),
        "csv": str(csv_path),
        "html": str(html_path),
        "truncated": truncated,
        "banner": banner,
        "elapsed_ms": round(elapsed, 1),
    }


# ---------------------------------------------------------------------------
# Daemon entry points
# ---------------------------------------------------------------------------
class SignalReviewExporter:
    """Thin wrapper around _tick for ServiceDaemon integration."""

    def __init__(self, report_dir: Optional[Path] = None, lookback_h: Optional[int] = None) -> None:
        self.report_dir = report_dir or _REPORT_DIR
        self.lookback_h = lookback_h if lookback_h is not None else _LOOKBACK_H

    async def run_once(self) -> Dict[str, Any]:
        """Execute one export tick."""
        _assert_symlink_integrity()
        return await _tick(self.report_dir, self.lookback_h)

    async def run_forever(self) -> None:
        """Blocking supervisor loop via ServiceDaemon."""
        from shared.services.daemon import DaemonConfig, ServiceDaemon

        _assert_symlink_integrity()
        daemon = ServiceDaemon(
            config=DaemonConfig(
                name="signal_review_exporter",
                interval_seconds=60.0,
                jitter_seconds=2.0,
            ),
            tick=lambda: _tick(self.report_dir, self.lookback_h),
        )
        await daemon.run_forever()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
async def main() -> None:
    """CLI entry point: run one tick or start daemon."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    exporter = SignalReviewExporter()
    if len(sys.argv) > 1 and sys.argv[1] == "daemon":
        await exporter.run_forever()
    else:
        result = await exporter.run_once()
        print(result)


if __name__ == "__main__":
    asyncio.run(main())
