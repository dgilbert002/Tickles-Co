"""
Backtest Runner — Tickles & Co V2.0 (hardened 2026-04-17)
==========================================================

Spawns and manages a pool of `backtest.worker` processes. Reaps stuck jobs
on the Redis queue and respawns dead workers.

HARDENING (audit 2026-04-17):
  * Dead processes are explicitly p.join()'d before the slot is overwritten —
    no more zombie entries on Linux.
  * Respawn uses a monotonic generation counter (w00-gen1, w00-gen2…) rather
    than appending 'r' infinitely.
  * SIGTERM handler is only installed on POSIX; Windows uses SIGINT only.
  * Queue client is explicitly closed on shutdown.
  * reap_stuck() uses new age-based + orphan-list API (not heartbeat-based).
"""
from __future__ import annotations

import argparse
import logging
import multiprocessing as mp
import os
import signal
import sys
import time
from typing import List, Dict

from backtest.queue import BacktestQueue
from backtest.worker import main as worker_main

log = logging.getLogger("tickles.runner")


def _run_worker(worker_id: str) -> None:
    """Child-process entry point."""
    sys.argv = ["worker", "--id", worker_id]
    try:
        worker_main()
    except SystemExit:
        pass


class Runner:
    def __init__(self, num_workers: int):
        self.num_workers = num_workers
        self.procs: List[mp.Process] = []
        # Track generation per logical slot so respawns don't stack "r" forever.
        self._gens: Dict[int, int] = {}
        self.queue = BacktestQueue()
        self._stop = False

    def _slot_name(self, idx: int, gen: int) -> str:
        return f"w{idx:02d}-{os.getpid()}-g{gen}"

    def start(self) -> None:
        log.info("runner: spawning %d workers", self.num_workers)
        ctx = mp.get_context("spawn")
        for i in range(self.num_workers):
            self._gens[i] = 1
            wid = self._slot_name(i, 1)
            p = ctx.Process(target=_run_worker, args=(wid,), name=wid)
            p.start()
            self.procs.append(p)
            log.info("runner: worker %s pid=%s up", wid, p.pid)

    def supervise(self) -> None:
        last_reap = 0.0
        last_stats = 0.0
        ctx = mp.get_context("spawn")
        while not self._stop:
            now = time.time()

            # Respawn dead children. Join + discard the dead Process object.
            for idx, p in enumerate(list(self.procs)):
                if not p.is_alive():
                    log.warning("runner: worker %s died (exit=%s), respawning",
                                p.name, p.exitcode)
                    try:
                        p.join(timeout=2)
                    except Exception:
                        pass
                    try:
                        p.close()
                    except Exception:
                        pass
                    self._gens[idx] = self._gens.get(idx, 1) + 1
                    new_name = self._slot_name(idx, self._gens[idx])
                    new = ctx.Process(target=_run_worker,
                                      args=(new_name,), name=new_name)
                    new.start()
                    self.procs[idx] = new
                    log.info("runner: worker %s pid=%s up (respawn)",
                             new_name, new.pid)

            # Periodic stuck-job reaper.
            if now - last_reap > 30.0:
                try:
                    reaped = self.queue.reap_stuck()
                    if reaped:
                        log.warning("runner: re-queued %d stuck jobs", reaped)
                except Exception:
                    log.exception("runner: reap_stuck failed")
                last_reap = now

            if now - last_stats > 60.0:
                try:
                    stats = self.queue.stats()
                    log.info("runner stats: %s", stats)
                except Exception:
                    log.exception("runner: stats failed")
                last_stats = now

            time.sleep(2.0)

    def shutdown(self) -> None:
        log.info("runner: shutting down %d workers", len(self.procs))
        for p in self.procs:
            if p.is_alive():
                try:
                    if os.name == "nt":
                        p.terminate()  # Windows: no SIGTERM
                    else:
                        os.kill(p.pid, signal.SIGTERM)
                except Exception:
                    log.debug("kill SIGTERM failed for %s", p.name, exc_info=True)
        deadline = time.time() + 30
        for p in self.procs:
            timeout = max(1, int(deadline - time.time()))
            p.join(timeout=timeout)
            if p.is_alive():
                log.warning("runner: worker %s refused SIGTERM, killing", p.name)
                p.terminate()
                p.join(timeout=5)
            try:
                p.close()
            except Exception:
                pass
        try:
            self.queue.close()
        except Exception:
            pass
        log.info("runner: shutdown complete")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--workers", type=int, default=0,
        help="0 = auto (cpu_count-1), capped by WORKERS env var",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cores = mp.cpu_count()
    env_cap = int(os.getenv("WORKERS", "0"))
    if args.workers > 0:
        n = args.workers
    elif env_cap > 0:
        n = env_cap
    else:
        n = max(1, cores - 1)
    n = max(1, min(n, cores))

    runner = Runner(num_workers=n)

    def _on_sig(signum, frame):
        log.info("runner: signal %s received, draining", signum)
        runner._stop = True

    if os.name != "nt":
        try:
            signal.signal(signal.SIGTERM, _on_sig)
        except (OSError, ValueError):
            pass
    signal.signal(signal.SIGINT, _on_sig)

    runner.start()
    try:
        runner.supervise()
    finally:
        runner.shutdown()


if __name__ == "__main__":
    main()
