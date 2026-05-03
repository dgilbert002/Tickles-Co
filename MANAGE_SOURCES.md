"""
Module: manage_sources
Purpose: Interactive TUI + Web Panel for managing Discord/Telegram/RSS sources, channels, and watched traders.
Location: /opt/tickles/manage_sources.py

Usage — TUI (read-only when TICKLES_TUI_READONLY=1):
    python manage_sources.py

Or use the launcher scripts:
    ./manage_sources.sh      (Linux/macOS)
    manage_sources.bat       (Windows)

Usage — Web Panel (default, requires auth):
    Open https://<tailscale-host>/manage in your browser.
    The panel is served by the dashboard server (shared/dashboard/server.py).

What it does:
    1. Connects to Postgres (tickles_shared) and reads collector_catalog,
       watched_channels, and watched_users tables.
    2. Presents either a Rich-based interactive TUI or a served HTML panel.

Menu Options (TUI)
------------------

[1] Dashboard
    Shows a summary: how many sources, channels, users, and enabled counts.

[2] Sources
    Lists all collector sources (Discord servers, Telegram, RSS feeds).
    You can toggle a source on/off by entering its ID.

[3] Channels
    Lists all watched channels for a selected source.
    Shows: channel name, platform ID, enabled status, what is collected
           (text, images, charts, videos), and poll interval.
    Actions:
      - Enter an ID to toggle enabled/disabled.
      - Type 'c' to change collection flags (text, images, charts, videos).

[4] Users / Traders
    Lists all watched users/traders for a selected channel.
    Shows: platform handle, display name, enabled status, and what we track:
           trades, charts, commentary, advice.
    Actions:
      - Enter an ID to toggle enabled/disabled.
      - Type 't' to change tracking flags (trades, charts, commentary, advice).

[5] Hierarchy
    Displays the full tree: Source → Channels → Users.
    Useful for verifying your configuration is wired correctly.

Web Panel (/manage/*)
---------------------
The web panel is the preferred interface for mutations. It is mounted on the
existing dashboard aiohttp app via attach_routes() and protected by:

  - Default-deny auth middleware (explicit allow-list, no public fallback).
  - CSRF tokens: __Host-csrf cookie + X-CSRF-Token header on every POST.
  - Token-bucket rate limiter: 600 reads/min, 30 writes/min per session.
  - TUI coexistence: set TICKLES_TUI_READONLY=1 to block mutating TUI menus.

Pages:
  /manage/sources      — Sources table with enable/disable + add new source.
  /manage/channels     — Channels table with enable/disable.
  /manage/users        — Users table with enable/disable.
  /manage/signals      — Recent signal interpretations (read-only).
  /manage/positions    — Open + closed positions (read-only).
  /manage/leaderboard  — Trader performance leaderboard (read-only).
  /manage/trader/{id}  — Drill-down for a single trader (read-only).

API (POST, CSRF + rate-limit protected):
  /manage/api/sources/add
  /manage/api/sources/disable
  /manage/api/sources/enable
  /manage/api/channels/disable
  /manage/api/channels/enable
  /manage/api/users/disable
  /manage/api/users/enable

All mutations are logged to api_cost_log for audit (provider="manage_panel").

Current ChartHackers Configuration (seeded by migration)
----------------------------------------------------------
Source: ChartHackers Discord (id=1)
  Channels (12 watched):
    - daily-market-updates
    - chats-and-setups
    - panda-trades
    - nagel-trades
    - trader-j-trades
    - stonks-setups (CFD — uses Capital.com epic resolution)
    - alt-coin-trading-setups
    - wen-n-tree
    - metals-comodities (CFD)
    - live-show-charts
    - zoom-charts
    - blofin-trading-comp

  Watched Traders (6):
    - emutrading      (Trader J)
    - degendavidd     (DegenDavidD)
    - its.chaos       (Chaoss)
    - thelordofentry  (Dylan)
    - arabian.panda   (ArabianPanda)
    - thenagel        (TheNagel)

All channels have download_media=true (images collected for LLM analysis).
All traders have track_trades=true (positions will be tracked).

How to Run
----------
1. Ensure Postgres is running and migrations are applied:
   psql -U admin -d tickles_shared -c "SELECT COUNT(*) FROM collector_catalog;"

2. Run the TUI:
   cd /opt/tickles
   python manage_sources.py

3. Navigate with number keys (1-5) or press 'q' to quit.

Environment Variables
-------------------
DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME_SHARED
  — Postgres connection (read from .env)

Files
-----
shared/catalogue/tui_manager.py              — TUI implementation (Rich)
shared/catalogue/db.py                       — Async DB queries
shared/catalogue/models.py                   — Dataclasses
shared/intelligence/manage_panel/server_routes.py  — aiohttp route handlers (/manage/*)
shared/intelligence/manage_panel/db_views.py       — Panel DB queries
shared/intelligence/manage_panel/templates/      — Jinja2 HTML templates
shared/intelligence/manage_panel/static/         — CSS + JS assets
shared/dashboard/csrf.py                     — CSRF token issuance + validation
shared/dashboard/rate_limit.py               — Token-bucket rate limiter
shared/dashboard/server.py                     — Dashboard app (mounts /manage/*)
manage_sources.py                              — Entrypoint (redirects to web panel)
manage_sources.sh / .bat                       — Launcher scripts

Environment Variables
---------------------
DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME_SHARED
  — Postgres connection (read from .env)

TICKLES_TUI_READONLY
  — Set to 1 to block mutating TUI menus (forces web panel for changes).

DASHBOARD_SECRET
  — Secret key for session cookie signing.

TICKLES_MANAGE_PANEL_HOST / TICKLES_MANAGE_PANEL_PORT
  — Bind address for the dashboard server (default 0.0.0.0:8080).
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

# Ensure shared imports resolve
_HERE = Path(__file__).resolve().parent
_SHARED = _HERE / "shared"
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

from shared.catalogue.tui_manager import main as tui_main
from shared.utils.config import load_env

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    """Configure basic logging for the TUI."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def _async_main() -> None:
    """Async entrypoint: load env then launch TUI."""
    load_env()
    await tui_main()


def main() -> None:
    """CLI entrypoint for the source manager TUI."""
    _setup_logging()
    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        print("\nExiting.")
        sys.exit(0)


if __name__ == "__main__":
    main()
