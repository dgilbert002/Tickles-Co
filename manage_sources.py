#!/usr/bin/env python3
"""
Module: manage_sources
Purpose: CLI entrypoint for the Collector Catalogue manager.
Location: /opt/tickles/manage_sources.py

Usage:
    python manage_sources.py          # Prints web panel URL (default)
    python manage_sources.py --tui    # Runs legacy interactive TUI over SSH

What it does:
    - By default, prints the Tailscale URL of the served HTML manage panel
      and exits.  This is the preferred way to drive the platform from
      a phone over Tailscale.
    - With --tui, launches the legacy interactive terminal UI for viewing
      and configuring collector_catalog, watched_channels, and watched_users.
    - Mutating actions in the TUI are gated by TICKLES_TUI_READONLY=1
      (default).  Set TICKLES_TUI_READONLY=0 to enable mutations, or use
      the web panel which has full edit-in-place support.
"""

import argparse
import asyncio
import logging
import os
import sys

# Ensure project root is on path
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

logger = logging.getLogger(__name__)


def _panel_url() -> str:
    """Return the public manage-panel URL from env or a sensible default."""
    return os.environ.get(
        "MANAGE_PANEL_PUBLIC_BASE_URL",
        "https://vmi3220412.trout-goblin.ts.net/manage",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collector Catalogue Manager launcher",
    )
    parser.add_argument(
        "--tui",
        action="store_true",
        help="Run the legacy interactive TUI instead of printing the web panel URL.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.tui:
        from shared.catalogue.tui_manager import main as tui_main

        try:
            asyncio.run(tui_main())
        except KeyboardInterrupt:
            print("\nInterrupted.")
            return 130
        return 0

    url = _panel_url()
    print(f"Manage Panel: {url}")
    print("Open this URL in a browser on your Tailscale network.")
    print("Use --tui to launch the legacy SSH terminal UI.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
