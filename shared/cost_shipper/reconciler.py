"""Daily OpenRouter reconciler.

Compares OpenRouter's authoritative spend total (``/api/v1/auth/key``) against
the sum of Paperclip ``cost_events`` for the current UTC day. If drift > 5%
we log a warning; systemd's ``journalctl`` plus the budget incident pipeline
pick it up from there.

Run as a daily systemd timer or cron at 00:05 UTC for prior day.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Iterable

LOG = logging.getLogger("tickles.reconciler")

DEFAULT_PAPERCLIP_URL = os.environ.get("PAPERCLIP_URL", "http://127.0.0.1:3100")
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/auth/key"
DRIFT_THRESHOLD_PCT = float(os.environ.get("TICKLES_COST_DRIFT_PCT", "5.0"))


def _http_json(url: str, headers: dict[str, str] | None = None) -> dict:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - trusted
        return json.loads(resp.read() or "{}")


def _openrouter_total_usd() -> float:
    if not OPENROUTER_KEY:
        raise RuntimeError("OPENROUTER_API_KEY env var not set")
    data = _http_json(OPENROUTER_URL, headers={"authorization": f"Bearer {OPENROUTER_KEY}"})
    # OpenRouter returns { data: { usage: { total: float_usd, ... } } }
    usage = (data.get("data") or {}).get("usage", {}) if isinstance(data, dict) else {}
    total = usage.get("total") or usage.get("total_usd") or 0.0
    return float(total)


def _paperclip_spend_cents(paperclip_url: str, from_utc: datetime, to_utc: datetime) -> int:
    """Sum cost_events.costCents across every company for the given range."""
    companies = _http_json(f"{paperclip_url}/api/companies")
    total = 0
    for company in companies or []:
        cid = company.get("id")
        qs = f"from={from_utc.isoformat()}&to={to_utc.isoformat()}"
        url = f"{paperclip_url}/api/companies/{cid}/costs/summary?{qs}"
        try:
            summary = _http_json(url)
            total += int(summary.get("spendCents", 0) or 0)
        except Exception as err:  # pragma: no cover
            LOG.warning("[paperclip_spend] company=%s err=%s", cid, err)
    return total


def reconcile(paperclip_url: str, day_back: int = 1) -> dict:
    now = datetime.now(timezone.utc)
    to_utc = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=day_back - 1)
    from_utc = to_utc - timedelta(days=1)
    openrouter_usd = _openrouter_total_usd()
    spend_cents = _paperclip_spend_cents(paperclip_url, from_utc, to_utc)
    spend_usd = spend_cents / 100.0
    drift_pct = 0.0 if openrouter_usd == 0 else abs(openrouter_usd - spend_usd) / openrouter_usd * 100.0
    payload = {
        "from": from_utc.isoformat(),
        "to": to_utc.isoformat(),
        "openrouter_total_usd_running": openrouter_usd,
        "paperclip_spend_usd": spend_usd,
        "drift_pct": round(drift_pct, 2),
        "threshold_pct": DRIFT_THRESHOLD_PCT,
    }
    if drift_pct > DRIFT_THRESHOLD_PCT and openrouter_usd > 0:
        LOG.error("[reconcile] DRIFT %.2f%% > %.2f%% (%s)", drift_pct, DRIFT_THRESHOLD_PCT, payload)
    else:
        LOG.info("[reconcile] drift=%.2f%% %s", drift_pct, payload)
    return payload


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Daily OpenRouter cost reconciler")
    parser.add_argument("--paperclip-url", default=DEFAULT_PAPERCLIP_URL)
    parser.add_argument("--day-back", type=int, default=1,
                        help="reconcile N days back (1 = yesterday UTC)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    result = reconcile(args.paperclip_url, day_back=args.day_back)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
