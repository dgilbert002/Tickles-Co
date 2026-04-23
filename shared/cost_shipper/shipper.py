"""Tickles-Co cost shipper: stream LLM usage from OpenClaw session jsonls into
Paperclip `cost_events`.

Why
---
OpenClaw writes a full ``usage`` + ``cost`` block into every assistant message
it appends to ``~/.openclaw/agents/<urlKey>/sessions/<sessionId>.jsonl``. That
block is the cheapest, most reliable source of per-call OpenRouter spend we
have: it already contains provider, model, token counts, cacheRead/cacheWrite,
cost-in-USD **and** the OpenRouter ``responseId`` (which we use as an idempotency
key so restarts never double-bill).

Paperclip already has a complete cost stack (``cost_events`` table, budget
evaluator, 8 aggregations, ``/api/companies/:id/cost-events`` endpoint). What
was missing was a producer. This daemon is that producer. It never touches the
Paperclip DB directly; it POSTs over the local HTTP API, which in
``local_trusted`` mode (default) accepts localhost requests as the board actor.

Mapping
-------
OpenClaw agent directory name (``main``, ``cody``, ``audrey``, ``schemy`` ...)
equals Paperclip ``agents.urlKey``. We refresh the urlKey -> (companyId, agentId)
map every five minutes by hitting Paperclip's HTTP API.

Durability
----------
* Cursor per session file: ``<state>/cursors.json`` maps session path -> byte
  offset. Advanced on every successful POST (or on skip).
* Dedup set: ``<state>/shipped.sqlite`` stores seen ``responseId`` values with
  TTL. The Paperclip endpoint is not idempotent, so we must not re-POST the
  same responseId after a restart.

Deployment
----------
Install to ``/opt/tickles/shared/cost_shipper/`` on the VPS; enable the
``tickles-cost-shipper.service`` systemd unit. Run ``python shipper.py
--backfill-days 7`` once to seed recent history before enabling the watcher.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

try:
    from .pricing import estimate_cost_cents
except ImportError:  # pragma: no cover - allow direct script execution
    from pricing import estimate_cost_cents  # type: ignore[no-redef]

LOG = logging.getLogger("tickles.cost_shipper")

DEFAULT_PAPERCLIP_URL = os.environ.get("PAPERCLIP_URL", "http://127.0.0.1:3100")
DEFAULT_OPENCLAW_ROOT = Path(os.environ.get("OPENCLAW_ROOT", "/root/.openclaw"))
DEFAULT_STATE_DIR = Path(os.environ.get("TICKLES_COST_STATE", "/var/lib/tickles/cost-shipper"))
POLL_SECONDS = float(os.environ.get("TICKLES_COST_POLL_SEC", "5"))
AGENT_MAP_REFRESH_SECONDS = 300.0
SHIPPED_TTL_DAYS = 30


@dataclass
class AgentBinding:
    """Resolved mapping for a single OpenClaw agent url-key."""

    url_key: str
    agent_id: str
    company_id: str


@dataclass
class CursorStore:
    """Persistent (file, offset) cursor for every session jsonl we tail."""

    path: Path
    _data: dict[str, int] = field(default_factory=dict)

    def load(self) -> None:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
            except json.JSONDecodeError:
                LOG.warning("[cursor_store.load] corrupt cursor file, resetting")
                self._data = {}

    def get(self, key: str) -> int:
        return int(self._data.get(key, 0))

    def set(self, key: str, offset: int) -> None:
        self._data[key] = offset

    def flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data))
        tmp.replace(self.path)


class DedupStore:
    """SQLite-backed set of OpenRouter responseIds we've already shipped."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS shipped (
                response_id TEXT PRIMARY KEY,
                shipped_at TEXT NOT NULL
            )"""
        )
        self.conn.commit()

    def has(self, response_id: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM shipped WHERE response_id = ?", (response_id,))
        return cur.fetchone() is not None

    def add(self, response_id: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO shipped (response_id, shipped_at) VALUES (?, ?)",
            (response_id, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    def prune(self) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=SHIPPED_TTL_DAYS)).isoformat()
        cur = self.conn.execute("DELETE FROM shipped WHERE shipped_at < ?", (cutoff,))
        self.conn.commit()
        return cur.rowcount


class AgentResolver:
    """Maps OpenClaw urlKey (agent dir name) to Paperclip (company, agent) UUIDs."""

    def __init__(self, paperclip_url: str) -> None:
        self.paperclip_url = paperclip_url.rstrip("/")
        self._bindings: dict[str, AgentBinding] = {}
        self._last_refresh: float = 0.0

    def resolve(self, url_key: str) -> AgentBinding | None:
        if time.monotonic() - self._last_refresh > AGENT_MAP_REFRESH_SECONDS:
            self._refresh()
        return self._bindings.get(url_key.lower())

    def _refresh(self) -> None:
        LOG.info("[agent_resolver._refresh] refreshing paperclip agent map")
        try:
            companies = _http_json(f"{self.paperclip_url}/api/companies")
        except Exception as exc:  # pragma: no cover - network error path
            LOG.error("[agent_resolver._refresh] cannot list companies: %s", exc)
            return
        new_map: dict[str, AgentBinding] = {}
        for company in companies or []:
            cid = company.get("id")
            try:
                agents = _http_json(f"{self.paperclip_url}/api/companies/{cid}/agents")
            except Exception as exc:  # pragma: no cover
                LOG.error("[agent_resolver._refresh] agents fetch for %s: %s", cid, exc)
                continue
            for agent in agents or []:
                url_key = (agent.get("urlKey") or "").lower()
                if not url_key:
                    continue
                new_map[url_key] = AgentBinding(
                    url_key=url_key,
                    agent_id=agent["id"],
                    company_id=cid,
                )
        self._bindings = new_map
        self._last_refresh = time.monotonic()
        LOG.info("[agent_resolver._refresh] bindings=%d", len(new_map))


def _http_json(url: str, data: bytes | None = None, headers: dict[str, str] | None = None) -> object:
    req = urllib.request.Request(url, data=data, headers=headers or {"content-type": "application/json"},
                                 method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - trusted local host
        body = resp.read()
    return json.loads(body) if body else None


def _iter_session_files(root: Path) -> Iterator[tuple[str, Path]]:
    """Yield (urlKey, session.jsonl) for every active session log."""
    agents_dir = root / "agents"
    if not agents_dir.exists():
        return
    for agent_dir in agents_dir.iterdir():
        sess_dir = agent_dir / "sessions"
        if not sess_dir.is_dir():
            continue
        for entry in sess_dir.iterdir():
            if entry.is_file() and entry.suffix == ".jsonl":
                yield agent_dir.name, entry


def _read_new_lines(path: Path, offset: int) -> tuple[list[str], int]:
    """Return any lines newer than offset, plus the new offset."""
    size = path.stat().st_size
    if size < offset:
        offset = 0
    if size == offset:
        return [], offset
    with path.open("r", encoding="utf-8", errors="ignore") as fh:
        fh.seek(offset)
        chunk = fh.read()
        new_offset = fh.tell()
    lines = [ln for ln in chunk.splitlines() if ln.strip()]
    return lines, new_offset


def _extract_cost_payload(line: str) -> dict | None:
    """Parse one jsonl line and return a cost-events payload if relevant."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    if event.get("type") != "message":
        return None
    msg = event.get("message")
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return None
    usage = msg.get("usage")
    response_id = msg.get("responseId")
    model = msg.get("model")
    if not (isinstance(usage, dict) and response_id and model):
        return None
    timestamp = event.get("timestamp") or msg.get("timestamp")
    occurred = _coerce_iso_ts(timestamp)
    provider = msg.get("provider") or "openclaw"

    input_tokens = int(usage.get("input", 0) or 0)
    output_tokens = int(usage.get("output", 0) or 0)
    cache_read = int(usage.get("cacheRead", 0) or 0)
    cost_block = usage.get("cost") if isinstance(usage.get("cost"), dict) else None
    if cost_block and isinstance(cost_block.get("total"), (int, float)):
        cost_usd = float(cost_block["total"])
    else:
        cost_usd = estimate_cost_cents(model, input_tokens, output_tokens, cache_read) / 100.0
    cost_cents = max(0, int(round(cost_usd * 100.0)))
    return {
        "response_id": response_id,
        "payload": {
            "provider": provider,
            "biller": provider,
            "billingType": "metered_api",
            "model": model,
            "inputTokens": input_tokens,
            "cachedInputTokens": cache_read,
            "outputTokens": output_tokens,
            "costCents": cost_cents,
            "occurredAt": occurred,
        },
    }


def _coerce_iso_ts(value: object) -> str:
    """Return a Zod-compatible ISO8601 timestamp ending in ``Z`` (UTC).

    Paperclip validates ``occurredAt`` with ``z.string().datetime()`` which by
    default only accepts UTC with a trailing ``Z`` (not ``+00:00``), so we
    normalize aggressively here.
    """
    if isinstance(value, str):
        if value.endswith("Z"):
            return value
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        dt = dt.astimezone(timezone.utc)
    elif isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
    else:
        dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _post_cost_event(paperclip_url: str, company_id: str, agent_id: str, payload: dict) -> bool:
    body = dict(payload)
    body["agentId"] = agent_id
    data = json.dumps(body).encode("utf-8")
    url = f"{paperclip_url}/api/companies/{company_id}/cost-events"
    try:
        _http_json(url, data=data, headers={"content-type": "application/json"})
        return True
    except urllib.error.HTTPError as err:
        LOG.error(
            "[post_cost_event] HTTP %s agent=%s model=%s body=%s",
            err.code, agent_id, payload.get("model"), err.read()[:300],
        )
    except Exception as err:  # pragma: no cover
        LOG.error("[post_cost_event] failed: %s", err)
    return False


def run_once(
    resolver: AgentResolver,
    cursors: CursorStore,
    dedup: DedupStore,
    openclaw_root: Path,
    paperclip_url: str,
    max_age_days: float | None = None,
) -> tuple[int, int]:
    """One scan of all session files. Returns (shipped, skipped)."""
    shipped = 0
    skipped = 0
    cutoff_ts = None
    if max_age_days is not None:
        cutoff_ts = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    for url_key, session_path in _iter_session_files(openclaw_root):
        cursor_key = str(session_path)
        offset = cursors.get(cursor_key)
        lines, new_offset = _read_new_lines(session_path, offset)
        if not lines:
            continue
        binding = resolver.resolve(url_key)
        if binding is None:
            LOG.warning("[run_once] no paperclip binding for urlKey=%s", url_key)
            cursors.set(cursor_key, new_offset)
            cursors.flush()
            continue
        for line in lines:
            cost = _extract_cost_payload(line)
            if cost is None:
                continue
            rid = cost["response_id"]
            if dedup.has(rid):
                skipped += 1
                continue
            if cutoff_ts:
                try:
                    occurred = datetime.fromisoformat(cost["payload"]["occurredAt"].replace("Z", "+00:00"))
                    if occurred < cutoff_ts:
                        dedup.add(rid)
                        skipped += 1
                        continue
                except Exception:
                    pass
            if _post_cost_event(paperclip_url, binding.company_id, binding.agent_id, cost["payload"]):
                dedup.add(rid)
                shipped += 1
            else:
                return shipped, skipped  # bail on error; cursor not advanced
        cursors.set(cursor_key, new_offset)
        cursors.flush()
    return shipped, skipped


def run_forever(openclaw_root: Path, paperclip_url: str, state_dir: Path) -> None:
    resolver = AgentResolver(paperclip_url)
    cursors = CursorStore(state_dir / "cursors.json")
    cursors.load()
    dedup = DedupStore(state_dir / "shipped.sqlite")
    stopping = {"flag": False}

    def _stop(*_args):
        LOG.info("[run_forever] shutdown signal received")
        stopping["flag"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    last_prune = 0.0
    LOG.info("[run_forever] start poll_sec=%s root=%s url=%s", POLL_SECONDS, openclaw_root, paperclip_url)
    while not stopping["flag"]:
        try:
            shipped, skipped = run_once(resolver, cursors, dedup, openclaw_root, paperclip_url)
            if shipped or skipped:
                LOG.info("[run_forever] shipped=%d skipped=%d", shipped, skipped)
        except Exception as err:  # pragma: no cover - defensive
            LOG.exception("[run_forever] iteration error: %s", err)
        now = time.monotonic()
        if now - last_prune > 86400:
            dedup.prune()
            last_prune = now
        time.sleep(POLL_SECONDS)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tickles cost shipper")
    parser.add_argument("--paperclip-url", default=DEFAULT_PAPERCLIP_URL)
    parser.add_argument("--openclaw-root", default=str(DEFAULT_OPENCLAW_ROOT))
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    parser.add_argument("--backfill-days", type=float, default=None,
                        help="one-shot: ship events newer than N days then exit")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)

    if args.backfill_days is not None:
        resolver = AgentResolver(args.paperclip_url)
        cursors = CursorStore(state_dir / "cursors.json")
        cursors.load()
        dedup = DedupStore(state_dir / "shipped.sqlite")
        shipped, skipped = run_once(
            resolver, cursors, dedup,
            Path(args.openclaw_root), args.paperclip_url,
            max_age_days=args.backfill_days,
        )
        LOG.info("[main.backfill] shipped=%d skipped=%d", shipped, skipped)
        return 0

    run_forever(Path(args.openclaw_root), args.paperclip_url, state_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
