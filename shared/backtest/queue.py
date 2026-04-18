"""
Backtest Queue — Tickles & Co V2.0 (hardened 2026-04-17)
=========================================================

Redis-backed reliable task queue for backtest jobs.

Keyspace layout:
  tickles:bt:queue:pending          (list)   jobs waiting to be claimed
  tickles:bt:queue:running          (hash)   claimed jobs in flight, field=jobid, val=json
  tickles:bt:queue:proc:<worker>    (list)   per-worker reliable-queue list (BLMOVE target)
  tickles:bt:queue:done             (list)   completed jobs (trimmed)
  tickles:bt:queue:failed           (list)   failed jobs (trimmed)
  tickles:bt:queue:hashseen         (set)    param_hash set for fast dedup
  tickles:bt:queue:workers          (set)    registered worker IDs (for scan)
  tickles:bt:worker:<id>:hb         (string) worker liveness heartbeat, TTL configurable

HARDENING (this revision, audit 2026-04-17):
  * Atomic claim uses BLMOVE pending → proc:<worker> — if a worker dies between
    claim and hset, the envelope is still in the worker's per-worker list and
    is reclaimed on reaper scan (`reclaim_orphans()`).
  * Enqueue dedup uses SADD's return-value as the atomic gate (not sismember→sadd).
  * `fail(retry=False)` removes the param_hash from `hashseen` so operators can
    re-submit after fixing the underlying bug.
  * Retry cap: envelope tracks `retry_count`; jobs that fail > MAX_RETRIES are
    permanently failed regardless of `retry=True`.
  * Reaper uses `claimed_at` (age-based) not heartbeat — stops duplicate
    execution when a long-running job merely exceeds HB TTL.
  * Reap + reclaim use atomic Lua scripts so a reaper crash cannot lose jobs.
  * Redis client has socket_timeout + socket_connect_timeout so the caller
    never hangs forever on a dropped tunnel.
  * alive_workers() uses SMEMBERS of a tracking set (not KEYS scan).

Usage (producer):
    q = BacktestQueue()
    job_id = q.enqueue({ ... })

Usage (consumer):
    q = BacktestQueue()
    q.register_worker("w00")
    q.reclaim_orphans("w00")           # at start-up, reclaim anything we left behind
    while not stop:
        env = q.claim("w00", block_s=5)
        if env is None: continue
        try:
            out = run(env)
            q.complete(env["id"], out)
        except Exception as e:
            q.fail(env["id"], str(e), retry=is_transient(e))
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional

import redis

log = logging.getLogger("tickles.queue")

# Maximum automatic retries before a job is moved to the failed list regardless.
MAX_RETRIES = int(os.getenv("BT_QUEUE_MAX_RETRIES", "3"))

# Default TTL on worker heartbeat key. This is a liveness signal ONLY — the
# reaper uses claimed_at age, not HB presence, to decide whether to reap.
HEARTBEAT_TTL_S = int(os.getenv("BT_QUEUE_HB_TTL", "120"))

# Default Redis socket timeout (s). If the tunnel / server is unreachable, we
# raise quickly instead of hanging forever.
REDIS_SOCKET_TIMEOUT = float(os.getenv("BT_REDIS_SOCKET_TIMEOUT", "15"))
REDIS_CONNECT_TIMEOUT = float(os.getenv("BT_REDIS_CONNECT_TIMEOUT", "10"))

# Per-enqueue back-pressure guard. 0 = unbounded.
MAX_PENDING = int(os.getenv("BT_QUEUE_MAX_PENDING", "0"))

# Default reaper thresholds. Re-used by the runner.
DEFAULT_REAP_AGE_S = int(os.getenv("BT_QUEUE_REAP_AGE_S", "1800"))  # 30 min
DEFAULT_ORPHAN_AGE_S = int(os.getenv("BT_QUEUE_ORPHAN_AGE_S", "60"))

# --- Lua scripts (atomic reclaims) ---

# Atomically move an envelope from proc:<worker> back to pending, rotating any
# items older than `max_age_s` (measured by enqueued_at in the JSON).
# KEYS[1] = proc_list, KEYS[2] = pending
# ARGV[1] = max_items (int), ARGV[2] = marker_tag (string appended to envelope
#          so we don't reclaim the same item infinitely — actually we just pop
#          everything, since a crashed worker has nothing else to do).
_RECLAIM_LUA = """
local proc = KEYS[1]
local pending = KEYS[2]
local count = 0
while true do
  local item = redis.call('RPOP', proc)
  if not item then break end
  redis.call('LPUSH', pending, item)
  count = count + 1
end
return count
"""

# Atomic reap-from-running: check current entry matches what we expect,
# then hdel + push to pending. Prevents TOCTOU.
# KEYS[1] = running_hash, KEYS[2] = pending
# ARGV[1] = job_id, ARGV[2] = envelope_json_to_push
_REAP_LUA = """
local running = KEYS[1]
local pending = KEYS[2]
local job_id = ARGV[1]
local env = ARGV[2]
local exists = redis.call('HEXISTS', running, job_id)
if exists == 0 then return 0 end
redis.call('HDEL', running, job_id)
redis.call('LPUSH', pending, env)
return 1
"""


class BacktestQueue:
    KEY_PENDING   = "tickles:bt:queue:pending"
    KEY_RUNNING   = "tickles:bt:queue:running"
    KEY_DONE      = "tickles:bt:queue:done"
    KEY_FAILED    = "tickles:bt:queue:failed"
    KEY_HASHSEEN  = "tickles:bt:queue:hashseen"
    KEY_WORKERS   = "tickles:bt:queue:workers"
    KEY_HB_PREFIX = "tickles:bt:worker:"
    KEY_PROC_PREFIX = "tickles:bt:queue:proc:"

    def __init__(self, client: Optional[redis.Redis] = None):
        if client is None:
            host = os.getenv("REDIS_HOST", "127.0.0.1")
            port = int(os.getenv("REDIS_PORT", "6379"))
            db   = int(os.getenv("REDIS_DB", "0"))
            client = redis.Redis(
                host=host, port=port, db=db,
                decode_responses=True,
                socket_timeout=REDIS_SOCKET_TIMEOUT,
                socket_connect_timeout=REDIS_CONNECT_TIMEOUT,
                health_check_interval=30,
                retry_on_timeout=False,
            )
        self.r = client
        self._reclaim_sha: Optional[str] = None
        self._reap_sha: Optional[str] = None

    # ------------------------------------------------------------------
    # Script loading (lazy)
    # ------------------------------------------------------------------
    def _reclaim(self) -> str:
        if self._reclaim_sha is None:
            self._reclaim_sha = self.r.script_load(_RECLAIM_LUA)
        return self._reclaim_sha

    def _reap(self) -> str:
        if self._reap_sha is None:
            self._reap_sha = self.r.script_load(_REAP_LUA)
        return self._reap_sha

    def close(self) -> None:
        try:
            self.r.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Producer
    # ------------------------------------------------------------------
    def enqueue(self, job: Dict[str, Any], *, dedup: bool = True) -> Optional[str]:
        """Push a job. Returns job_id, None if deduped, raises if queue is full."""
        payload = json.dumps(job, sort_keys=True, separators=(",", ":"), default=str)
        p_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()

        # Atomic dedup gate: SADD returns 0 if element already present.
        if dedup:
            added = self.r.sadd(self.KEY_HASHSEEN, p_hash)
            if added == 0:
                log.debug("enqueue: dedup hit %s", p_hash[:12])
                return None

        # Back-pressure guard.
        if MAX_PENDING > 0:
            depth = int(self.r.llen(self.KEY_PENDING))
            if depth >= MAX_PENDING:
                # Roll back the dedup-set entry so this job can be tried again.
                if dedup:
                    self.r.srem(self.KEY_HASHSEEN, p_hash)
                raise RuntimeError(
                    f"enqueue: queue full ({depth} >= {MAX_PENDING})")

        job_id = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
        envelope = {
            "id": job_id,
            "hash": p_hash,
            "enqueued_at": time.time(),
            "payload": job,
            "retry_count": 0,
        }
        self.r.lpush(self.KEY_PENDING, json.dumps(envelope, default=str))
        log.debug("enqueue: %s hash=%s strategy=%s",
                  job_id, p_hash[:8], job.get("strategy", "?"))
        return job_id

    # ------------------------------------------------------------------
    # Consumer
    # ------------------------------------------------------------------
    def register_worker(self, worker_id: str) -> None:
        """Record this worker in the tracking set. Idempotent."""
        self.r.sadd(self.KEY_WORKERS, worker_id)
        self.heartbeat(worker_id)

    def _proc_key(self, worker_id: str) -> str:
        return self.KEY_PROC_PREFIX + worker_id

    def claim(self, worker_id: str, block_s: int = 5) -> Optional[Dict[str, Any]]:
        """Atomically claim a job.

        Uses BLMOVE pending → proc:<worker>. If the worker crashes between
        BLMOVE and HSET, the envelope is still in proc:<worker> and is
        returned to pending by reclaim_orphans() on next startup or by the
        reaper.
        """
        try:
            env_json = self.r.blmove(
                self.KEY_PENDING, self._proc_key(worker_id),
                timeout=block_s, src="RIGHT", dest="LEFT",
            )
        except redis.exceptions.ResponseError:
            # Server pre-Redis 6.2 or rename — fall back to rpoplpush+sleep loop.
            # Rare in our stack (Redis 7+) but keep a guard.
            env_json = self.r.rpoplpush(self.KEY_PENDING, self._proc_key(worker_id))
            if env_json is None and block_s > 0:
                time.sleep(min(0.1, block_s))
                env_json = self.r.rpoplpush(self.KEY_PENDING, self._proc_key(worker_id))
        if env_json is None:
            return None

        try:
            envelope = json.loads(env_json)
        except Exception:
            log.exception("claim: corrupt envelope, discarding: %r", env_json[:200])
            # Remove the bad entry from the proc list.
            self.r.lrem(self._proc_key(worker_id), 1, env_json)
            return None

        envelope["claimed_at"] = time.time()
        envelope["worker"] = {
            "id": worker_id, "pid": os.getpid(), "host": socket.gethostname(),
        }
        pipe = self.r.pipeline()
        pipe.hset(self.KEY_RUNNING, envelope["id"], json.dumps(envelope, default=str))
        pipe.setex(self.KEY_HB_PREFIX + worker_id, HEARTBEAT_TTL_S, str(time.time()))
        pipe.sadd(self.KEY_WORKERS, worker_id)
        pipe.execute()
        log.debug("claim: %s by %s", envelope["id"], worker_id)
        return envelope

    def complete(self, job_id: str, result_summary: Dict[str, Any],
                 worker_id: Optional[str] = None) -> None:
        """Mark job complete. Removes from running hash and worker's proc list."""
        pipe = self.r.pipeline()
        pipe.hdel(self.KEY_RUNNING, job_id)
        if worker_id:
            # Best-effort: remove envelope from this worker's proc list. We don't
            # store the exact payload here (Redis LREM requires value), so we pop
            # the envelope we just wrote to pending in claim() — but the cleanest
            # way is to let reclaim_orphans handle stale items. Instead, we use
            # the stored envelope from running_hash BEFORE we hdel.
            pass  # handled below
        done_entry = {"id": job_id, "ended_at": time.time(), "summary": result_summary}
        pipe.lpush(self.KEY_DONE, json.dumps(done_entry, default=str))
        pipe.ltrim(self.KEY_DONE, 0, 10_000 - 1)
        pipe.execute()
        if worker_id:
            # Drain anything in this worker's proc list matching the job id.
            # Simple linear scan; proc list should be tiny (0-1 items usually).
            self._drain_proc_for_job(worker_id, job_id)
        log.debug("complete: %s", job_id)

    def _drain_proc_for_job(self, worker_id: str, job_id: str) -> None:
        """Remove the envelope with this job_id from the worker's proc list."""
        proc_key = self._proc_key(worker_id)
        # Get all items (tiny list), LREM the one that matches.
        items = self.r.lrange(proc_key, 0, -1) or []
        for it in items:
            try:
                env = json.loads(it)
                if env.get("id") == job_id:
                    self.r.lrem(proc_key, 1, it)
                    return
            except Exception:
                continue

    def fail(self, job_id: str, error: str, retry: bool = False,
             worker_id: Optional[str] = None) -> None:
        """Mark job failed. If retry=True and retry_count < MAX_RETRIES, re-enqueue."""
        raw = self.r.hget(self.KEY_RUNNING, job_id)
        env: Optional[Dict[str, Any]] = None
        if raw:
            try:
                env = json.loads(raw)
            except Exception:
                env = None

        do_retry = False
        if retry and env is not None:
            env["retry_count"] = int(env.get("retry_count", 0)) + 1
            do_retry = env["retry_count"] <= MAX_RETRIES
            env["last_error"] = error[:1024]

        pipe = self.r.pipeline()
        pipe.hdel(self.KEY_RUNNING, job_id)
        if do_retry and env is not None:
            env.pop("worker", None)
            env.pop("claimed_at", None)
            pipe.lpush(self.KEY_PENDING, json.dumps(env, default=str))
        else:
            fail_entry = {
                "id": job_id, "failed_at": time.time(),
                "error": error[:2048],
                "retry_count": int((env or {}).get("retry_count", 0)),
            }
            pipe.lpush(self.KEY_FAILED, json.dumps(fail_entry, default=str))
            pipe.ltrim(self.KEY_FAILED, 0, 10_000 - 1)
            # Release the hashseen entry so this param_hash can be re-submitted
            # after the operator fixes the root cause.
            if env and env.get("hash"):
                pipe.srem(self.KEY_HASHSEEN, env["hash"])
        pipe.execute()

        if worker_id:
            self._drain_proc_for_job(worker_id, job_id)

        log.warning("fail: %s err=%s retry=%s (attempt=%d)",
                    job_id, (error or "")[:80], do_retry,
                    int((env or {}).get("retry_count", 0)))

    # ------------------------------------------------------------------
    # Worker heartbeat
    # ------------------------------------------------------------------
    def heartbeat(self, worker_id: str, ttl_s: int = HEARTBEAT_TTL_S) -> None:
        self.r.setex(self.KEY_HB_PREFIX + worker_id, ttl_s, str(time.time()))

    def alive_workers(self) -> Dict[str, float]:
        """Workers that heartbeat within TTL. Uses the tracking SET (no KEYS)."""
        ids = self.r.smembers(self.KEY_WORKERS) or set()
        out: Dict[str, float] = {}
        for wid in ids:
            v = self.r.get(self.KEY_HB_PREFIX + wid)
            if v:
                try:
                    out[wid] = float(v)
                except ValueError:
                    pass
            else:
                # Heartbeat expired — remove from tracking set.
                self.r.srem(self.KEY_WORKERS, wid)
        return out

    # ------------------------------------------------------------------
    # Ops / admin
    # ------------------------------------------------------------------
    def stats(self) -> Dict[str, int]:
        return {
            "pending":  int(self.r.llen(self.KEY_PENDING)),
            "running":  int(self.r.hlen(self.KEY_RUNNING)),
            "done":     int(self.r.llen(self.KEY_DONE)),
            "failed":   int(self.r.llen(self.KEY_FAILED)),
            "hashseen": int(self.r.scard(self.KEY_HASHSEEN)),
            "workers":  len(self.alive_workers()),
        }

    def reclaim_orphans(self, worker_id: str) -> int:
        """On worker startup, sweep our own proc list back into pending.

        Run this IMMEDIATELY after register_worker() to recover any envelope
        that was BLMOVE'd before a prior crash but never HSET into running.
        """
        proc_key = self._proc_key(worker_id)
        n = int(self.r.evalsha(self._reclaim(), 2, proc_key, self.KEY_PENDING, 0))
        if n:
            log.warning("reclaim_orphans(%s): %d envelopes returned to pending",
                        worker_id, n)
        return n

    def reap_stuck(self, max_age_s: int = DEFAULT_REAP_AGE_S,
                   orphan_age_s: int = DEFAULT_ORPHAN_AGE_S) -> int:
        """Re-queue jobs whose claim is older than max_age_s, and sweep proc
        lists of workers that have been silent > orphan_age_s.

        Returns total count moved back to pending.
        """
        reaped = 0
        now = time.time()

        # (1) Age-based reap of the running hash. Uses claimed_at — independent
        #     of heartbeat TTL so a long-running job won't get double-executed
        #     while the worker is still healthy.
        running = self.r.hgetall(self.KEY_RUNNING)
        for job_id, envelope_json in running.items():
            try:
                env = json.loads(envelope_json)
            except Exception:
                # Corrupt entry — remove it so it doesn't block reap.
                self.r.hdel(self.KEY_RUNNING, job_id)
                continue
            claimed_at = float(env.get("claimed_at", 0) or 0)
            if claimed_at == 0 or (now - claimed_at) <= max_age_s:
                continue
            # Sanity: increment retry_count and decide whether to re-queue.
            env["retry_count"] = int(env.get("retry_count", 0)) + 1
            worker_id = (env.get("worker") or {}).get("id", "?")
            log.warning("reap_stuck: job %s (worker %s) age=%ds, retry=%d",
                        job_id, worker_id, int(now - claimed_at), env["retry_count"])
            env.pop("worker", None)
            env.pop("claimed_at", None)
            if env["retry_count"] > MAX_RETRIES:
                # Permanent fail.
                self.r.hdel(self.KEY_RUNNING, job_id)
                self.r.lpush(self.KEY_FAILED, json.dumps({
                    "id": job_id, "failed_at": now,
                    "error": f"reap_stuck: exceeded max retries ({MAX_RETRIES})",
                    "retry_count": env["retry_count"],
                }, default=str))
                self.r.ltrim(self.KEY_FAILED, 0, 10_000 - 1)
                if env.get("hash"):
                    self.r.srem(self.KEY_HASHSEEN, env["hash"])
                reaped += 1
                continue
            # Atomic reap-and-requeue. If another process already completed it
            # (TOCTOU), our HDEL returns 0 and the Lua script skips the push.
            moved = int(self.r.evalsha(
                self._reap(), 2, self.KEY_RUNNING, self.KEY_PENDING,
                job_id, json.dumps(env, default=str),
            ))
            reaped += moved

        # (2) Orphan sweep: for workers silent > orphan_age_s, reclaim their
        #     proc lists (envelopes that were BLMOVE'd but never HSET'd).
        alive = self.alive_workers()
        # Find proc:<worker> keys by scanning (bounded by worker count).
        for key in self.r.scan_iter(self.KEY_PROC_PREFIX + "*"):
            wid = key.replace(self.KEY_PROC_PREFIX, "")
            last_hb = alive.get(wid, 0)
            if last_hb and (now - last_hb) <= orphan_age_s:
                continue
            moved = int(self.r.evalsha(self._reclaim(), 2, key, self.KEY_PENDING, 0))
            if moved:
                log.warning("reap_stuck: reclaimed %d orphan envelopes from %s",
                            moved, wid)
                reaped += moved
        return reaped

    def flush_all(self) -> None:
        """DANGER: wipe every tickles:bt:* key. Dev use only."""
        for k in self.r.scan_iter("tickles:bt:*"):
            self.r.delete(k)
        log.warning("flush_all: queue wiped")
