"""
Auto-restarting SSH Tunnel for the Local Runner
================================================

Opens a persistent SSH tunnel from the local machine to the VPS, mapping:
  * localhost:5432  -> vps:5432   (Postgres)
  * localhost:6379  -> vps:6379   (Redis)
  * localhost:9000  -> vps:9000   (ClickHouse native)
  * localhost:8123  -> vps:8123   (ClickHouse HTTP, for debug)

If the tunnel drops, it auto-restarts after a short backoff. This lets the
runner treat the VPS services as if they were local.

Config via env:
  VPS_HOST        (e.g. "vps" — uses your ~/.ssh/config alias)
  VPS_USER        (default "root")
  VPS_IDENTITY    (path to the private key, default ~/.ssh/id_rsa)

Run:
    python ssh_tunnel.py
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time

log = logging.getLogger("tickles.tunnel")

VPS_HOST = os.getenv("VPS_HOST", "vps")
VPS_USER = os.getenv("VPS_USER", "root")
VPS_KEY  = os.getenv("VPS_IDENTITY", os.path.expanduser("~/.ssh/id_rsa"))

PORT_MAP = [
    ("5432", "127.0.0.1:5432"),
    ("6379", "127.0.0.1:6379"),
    ("9000", "127.0.0.1:9000"),
    ("8123", "127.0.0.1:8123"),
]


def build_cmd():
    cmd = [
        "ssh", "-N",  # no remote command
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "StrictHostKeyChecking=accept-new",
    ]
    if VPS_KEY and os.path.exists(VPS_KEY):
        cmd += ["-i", VPS_KEY]
    for local_port, remote in PORT_MAP:
        cmd += ["-L", f"{local_port}:{remote}"]
    cmd.append(f"{VPS_USER}@{VPS_HOST}")
    return cmd


def main():
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    backoff = 2.0
    while True:
        cmd = build_cmd()
        log.info("tunnel: starting %s", " ".join(cmd))
        try:
            p = subprocess.Popen(cmd)
            p.wait()
            log.warning("tunnel: ssh exited with %s, retrying in %.0fs",
                        p.returncode, backoff)
        except KeyboardInterrupt:
            log.info("tunnel: ctrl-C, exiting")
            break
        except Exception as e:
            log.exception("tunnel: error %s, retry in %.0fs", e, backoff)
        time.sleep(backoff)
        backoff = min(backoff * 1.5, 60.0)


if __name__ == "__main__":
    main()
