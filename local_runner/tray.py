"""
Tickles Runner Tray App (Windows)
===================================

Minimal system-tray icon for Dean's desktop. Lets him:
  * See runner status (idle / running / paused / offline)
  * Pause / resume the runner
  * See queue depth from VPS
  * Open the log folder
  * Quit cleanly

Written with pystray (cross-platform tray library) + Pillow for the icon.
Uses a small IPC file (~/.tickles_runner/state.json) to communicate with
the runner process.

Run:
    python tray.py

When bundled with PyInstaller (`pyinstaller --windowed tray.py`) this
becomes a single-file exe that can live in the Windows startup folder.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    from PIL import Image, ImageDraw  # type: ignore
    import pystray  # type: ignore
except ImportError:
    print("pystray / Pillow not installed. pip install pystray pillow", file=sys.stderr)
    sys.exit(1)

STATE_FILE = Path.home() / ".tickles_runner" / "state.json"
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

LOG_DIR = Path.home() / ".tickles_runner" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    except Exception:
        return {}


def _save(d: dict):
    STATE_FILE.write_text(json.dumps(d, default=str))


def _make_icon(color: tuple) -> "Image.Image":
    img = Image.new("RGB", (64, 64), color=(16, 16, 16))
    draw = ImageDraw.Draw(img)
    draw.ellipse((8, 8, 56, 56), fill=color)
    return img


def _pulse_icon(icon: "pystray.Icon"):
    """Swap icon colour to indicate live activity."""
    paused_colour = (230, 150, 20)
    idle_colour   = (60, 140, 230)
    running_colour= (60, 200, 80)
    while True:
        s = _state()
        if s.get("paused"):
            icon.icon = _make_icon(paused_colour)
            icon.title = "Tickles Runner — PAUSED"
        elif (time.time() - s.get("last_job_at", 0)) < 10:
            icon.icon = _make_icon(running_colour)
            icon.title = (
                f"Tickles Runner — running\n"
                f"jobs done: {s.get('jobs_done', 0)}"
            )
        else:
            icon.icon = _make_icon(idle_colour)
            icon.title = (
                f"Tickles Runner — idle\n"
                f"jobs done: {s.get('jobs_done', 0)}"
            )
        time.sleep(3.0)


def toggle_pause(icon, item):
    s = _state()
    s["paused"] = not s.get("paused", False)
    _save(s)


def open_logs(icon, item):
    os.startfile(str(LOG_DIR))


def quit_app(icon, item):
    icon.stop()


def main():
    menu = pystray.Menu(
        pystray.MenuItem(
            "Pause / Resume",
            toggle_pause,
            checked=lambda item: _state().get("paused", False),
        ),
        pystray.MenuItem("Open Logs Folder", open_logs),
        pystray.MenuItem("Quit", quit_app),
    )
    icon = pystray.Icon(
        "tickles",
        icon=_make_icon((60, 140, 230)),
        title="Tickles Runner",
        menu=menu,
    )
    t = threading.Thread(target=_pulse_icon, args=(icon,), daemon=True)
    t.start()
    icon.run()


if __name__ == "__main__":
    main()
