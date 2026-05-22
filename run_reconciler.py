"""
Module: run_reconciler
Purpose: Thin wrapper to invoke the surgeon_position_reconciler in --once mode.
         Designed to be called from cron every 5 minutes.
Location: /opt/tickles/run_reconciler.py
"""
from __future__ import annotations

import logging
import os
import sys

# Ensure /opt/tickles is on sys.path before importing shared modules.
sys.path.insert(0, "/opt/tickles")

from shared.utils.config import load_env

load_env()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s run_reconciler :: %(message)s",
)
logger = logging.getLogger("run_reconciler")


def main() -> None:
    """Run a single reconciler pass for all configured companies."""
    logger.info("starting reconciler pass")
    # The reconciler reads sys.argv directly; inject --once, a generous
    # --since window, and the correct surgeon-v1 state file path so we
    # catch trades even if the forward-bridge was broken for a while.
    saved_argv = sys.argv[:]
    surgeon1_state = os.environ.get(
        "SURGEON1_STATE_FILE",
        "/root/.openclaw/workspace/rubicon_surgeon/.surgeon_state.json",
    )
    since = os.environ.get("SURGEON_RECON_SINCE", "30d")
    sys.argv = [
        sys.argv[0],
        "--once",
        "--since", since,
        "--surgeon1-state-file", surgeon1_state,
    ]
    try:
        from shared.intelligence.surgeon_position_reconciler import main as reconciler_main
        reconciler_main()
    except SystemExit as exc:
        if exc.code and exc.code != 0:
            logger.error("reconciler exited with code %s", exc.code)
            sys.exit(exc.code)
        logger.info("reconciler completed successfully")
    except Exception:
        logger.exception("reconciler pass crashed")
        sys.exit(1)
    finally:
        sys.argv = saved_argv
    logger.info("reconciler pass finished")


if __name__ == "__main__":
    main()
