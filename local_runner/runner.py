"""
Tickles Local Runner — Desktop Agent
======================================

Runs on Dean's Windows machine. Pulls backtest jobs from the VPS Redis queue
over an SSH tunnel, runs them locally using the same engine as the VPS
(so results are identical — Rule #1), pushes results back to ClickHouse
over the same tunnel.

Why?
  * Dean's desktop has many more cores than the VPS. Heavy backtests run
    faster there.
  * It gives Dean a "there's an agent running on my machine" tangible
    artifact — a tray icon, a small UI, a button to pause.
  * Zero code duplication: we reuse shared/backtest/* by shipping it
    alongside this runner (or bundling into a PyInstaller exe).

Architecture:

    [Dean's Windows]                        [VPS]
    +-----------------+                     +-----------------+
    | tray.py         |                     |                 |
    |   system-tray   |                     | Redis queue     |
    |   menu/GUI      |                     |                 |
    +--------+--------+                     | Postgres        |
             |                              |                 |
             v                              | ClickHouse      |
    +-----------------+    SSH tunnel      +-----------------+
    | runner.py       |<-------------------| :6379, :5432,   |
    |   claim → run   |                    |  :9000          |
    |   → push result |                    |                 |
    +-----------------+                     +-----------------+

Tunnel is opened by `ssh_tunnel.py` (auto-restarting) on startup. All DB
config pointing at 127.0.0.1 "just works" because the tunnel maps the
VPS ports to the local loopback.

This file = the runner loop. Tray is separate (tray.py) and talks to
this process via a small IPC file.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Dict, Any

# When packaged, shared/ is sibling to local_runner/. When running from
# source, shared/ is at /opt/tickles/shared/ or nearby. Try both.
_HERE = Path(__file__).parent
for candidate in (_HERE.parent, _HERE.parent / "shared"):
    if (candidate / "backtest").is_dir():
        sys.path.insert(0, str(candidate))
        sys.path.insert(0, str(candidate.parent))
        break

from backtest.worker import process  # noqa: E402
from backtest.queue import BacktestQueue  # noqa: E402
from backtest.ch_writer import ClickHouseWriter  # noqa: E402

log = logging.getLogger("tickles.local_runner")

STATE_FILE = Path.home() / ".tickles_runner" / "state.json"
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

_STOP = False
_PAUSED = False


def _on_sig(signum, frame):
    global _STOP
    log.info("local_runner: signal %s received, draining", signum)
    _STOP = True


def _read_state() -> Dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def _write_state(d: Dict[str, Any]):
    STATE_FILE.write_text(json.dumps(d, default=str))


def run(worker_id: str, max_jobs: int = 0):
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    signal.signal(signal.SIGTERM, _on_sig)
    signal.signal(signal.SIGINT, _on_sig)

    q = BacktestQueue()   # env-driven (REDIS_HOST=127.0.0.1 via tunnel)
    ch = ClickHouseWriter()

    log.info("local_runner %s: online, polling queue on %s",
             worker_id, os.getenv("REDIS_HOST", "127.0.0.1"))

    done = 0
    while not _STOP:
        q.heartbeat(worker_id)
        state = _read_state()
        if state.get("paused"):
            time.sleep(2.0)
            continue
        job = q.claim(worker_id, block_s=5)
        if job is None:
            continue
        try:
            summary = process(job, ch)
            q.complete(job["id"], summary)
            log.info("local_runner: job %s done pnl=%.2f%% sharpe=%.2f",
                     job["id"],
                     summary.get("pnl_pct", 0),
                     summary.get("sharpe", 0))
        except Exception as e:
            log.exception("local_runner: job %s failed: %s", job["id"], e)
            q.fail(job["id"], str(e), retry=False)

        done += 1
        _write_state({
            "paused":       state.get("paused", False),
            "jobs_done":    state.get("jobs_done", 0) + 1,
            "last_job_at":  time.time(),
            "worker_id":    worker_id,
        })
        if max_jobs and done >= max_jobs:
            log.info("local_runner: hit max_jobs=%d, exiting", max_jobs)
            break

    log.info("local_runner %s: drained, exiting", worker_id)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", default=f"local-{os.getpid()}")
    ap.add_argument("--max-jobs", type=int, default=0,
                    help="Exit after N jobs (0 = forever).")
    args = ap.parse_args()
    run(args.id, max_jobs=args.max_jobs)


if __name__ == "__main__":
    main()
