"""
Module: tui_manager
Purpose: Interactive terminal UI for managing collector catalogue sources, channels, and users.
Location: /opt/tickles/shared/catalogue/tui_manager.py
"""

import asyncio
import logging
import os
import sys
from typing import Any, Dict, List, Optional

from rich.align import Align
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table
from rich.text import Text

# Ensure project root is on path
_HERE = os.path.dirname(os.path.abspath(__file__))
_SHARED = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_SHARED)
for p in (_ROOT, _SHARED):
    if p not in sys.path:
        sys.path.insert(0, p)

from shared.catalogue import db

logger = logging.getLogger(__name__)
console = Console()


# ---------------------------------------------------------------------------
# Read-only gate
# ---------------------------------------------------------------------------
_TUI_READONLY: bool = os.environ.get("TICKLES_TUI_READONLY", "1").strip().lower() in ("1", "true", "yes", "on")


def _readonly_guard(action: str) -> bool:
    """Return True if the action is allowed; print warning and return False in read-only mode."""
    if _TUI_READONLY:
        console.print(
            f"[yellow]⚠ Read-only mode — {action} is disabled.[/yellow]\n"
            "[dim]Set TICKLES_TUI_READONLY=0 to enable mutations, or use the web panel.[/dim]"
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _bool_emoji(val: bool) -> str:
    return "[green]✓[/green]" if val else "[red]✗[/red]"


def _trunc(s: Optional[str], max_len: int = 40) -> str:
    if not s:
        return ""
    return s if len(s) <= max_len else s[: max_len - 3] + "..."


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------
async def view_dashboard() -> None:
    """Show high-level statistics."""
    stats = await db.get_stats()
    grid = Table.grid(expand=True)
    grid.add_column(justify="center")
    grid.add_column(justify="center")
    grid.add_column(justify="center")
    grid.add_column(justify="center")
    grid.add_row(
        Panel(f"[bold cyan]{stats['active_sources']}[/bold cyan]\nSources", border_style="blue"),
        Panel(f"[bold green]{stats['active_channels']}[/bold green]\nChannels", border_style="green"),
        Panel(f"[bold magenta]{stats['active_users']}[/bold magenta]\nUsers", border_style="magenta"),
        Panel(f"[bold yellow]{stats['open_positions']}[/bold yellow]\nOpen Pos", border_style="yellow"),
    )
    console.print(grid)


async def view_sources() -> None:
    """List all sources with enable/disable toggle."""
    sources = await db.list_sources()
    if not sources:
        console.print("[yellow]No sources configured.[/yellow]")
        return

    table = Table(title="Collector Sources", expand=True)
    table.add_column("ID", style="dim", width=6)
    table.add_column("Type", style="cyan")
    table.add_column("Name", style="bold")
    table.add_column("Slug", style="dim")
    table.add_column("Enabled", justify="center")
    table.add_column("Description")

    for s in sources:
        table.add_row(
            str(s["id"]),
            s["source_type"],
            s["source_name"],
            s["source_slug"],
            _bool_emoji(s["is_enabled"]),
            _trunc(s.get("description"), 35),
        )
    console.print(table)

    # Interactive toggle
    choice = Prompt.ask(
        "Enter ID to toggle, 'a' for all, or Enter to skip",
        default="",
        show_default=False,
    )
    if not choice:
        return
    if not _readonly_guard("source toggle"):
        return
    if choice.lower() == "a":
        for s in sources:
            await db.toggle_source(s["id"], not s["is_enabled"])
        console.print("[green]Toggled all sources.[/green]")
    elif choice.isdigit():
        sid = int(choice)
        for s in sources:
            if s["id"] == sid:
                new_state = not s["is_enabled"]
                await db.toggle_source(sid, new_state)
                console.print(
                    f"[green]Source '{s['source_name']}' now {'enabled' if new_state else 'disabled'}.[/green]"
                )
                break
        else:
            console.print("[red]ID not found.[/red]")


async def view_channels(source_id: Optional[int] = None) -> None:
    """List channels, optionally filtered by source."""
    channels = await db.list_channels(catalog_id=source_id)
    if not channels:
        console.print("[yellow]No channels configured.[/yellow]")
        return

    table = Table(title="Watched Channels", expand=True)
    table.add_column("ID", style="dim", width=6)
    table.add_column("Source ID", style="dim")
    table.add_column("Name", style="bold")
    table.add_column("Slug", style="dim")
    table.add_column("Enabled", justify="center")
    table.add_column("Text", justify="center")
    table.add_column("Images", justify="center")
    table.add_column("Charts", justify="center")
    table.add_column("Videos", justify="center")
    table.add_column("Poll(s)", justify="right")

    for c in channels:
        table.add_row(
            str(c["id"]),
            str(c["catalog_id"]),
            c["channel_name"],
            c["channel_slug"],
            _bool_emoji(c["is_enabled"]),
            _bool_emoji(c["collect_text"]),
            _bool_emoji(c["collect_images"]),
            _bool_emoji(c["collect_charts"]),
            _bool_emoji(c["collect_videos"]),
            str(c["poll_interval_seconds"]),
        )
    console.print(table)

    choice = Prompt.ask(
        "Enter ID to toggle, 'c' to change collection flags, or Enter to skip",
        default="",
        show_default=False,
    )
    if not choice:
        return
    if choice.lower() == "c":
        if not _readonly_guard("channel collection flags"):
            return
        cid_str = Prompt.ask("Channel ID (or 'x' to cancel)", default="")
        if cid_str.lower() in ("x", "q", "cancel", ""):
            console.print("[dim]Cancelled.[/dim]")
            return
        try:
            cid = int(cid_str)
        except ValueError:
            console.print("[red]Invalid ID — must be a number.[/red]")
            return
        flags = {
            "collect_text": Confirm.ask("Collect text?", default=True),
            "collect_images": Confirm.ask("Collect images?", default=True),
            "collect_charts": Confirm.ask("Collect charts?", default=True),
            "collect_videos": Confirm.ask("Collect videos?", default=False),
        }
        await db.update_channel_collection(cid, **flags)
        console.print("[green]Collection flags updated.[/green]")
    elif choice.isdigit():
        if not _readonly_guard("channel toggle"):
            return
        cid = int(choice)
        for c in channels:
            if c["id"] == cid:
                new_state = not c["is_enabled"]
                await db.toggle_channel(cid, new_state)
                console.print(
                    f"[green]Channel '{c['channel_name']}' now {'enabled' if new_state else 'disabled'}.[/green]"
                )
                break
        else:
            console.print("[red]ID not found.[/red]")


async def view_users(channel_id: Optional[int] = None) -> None:
    """List watched users, optionally filtered by channel."""
    users = await db.list_users(channel_id=channel_id)
    if not users:
        console.print("[yellow]No users configured.[/yellow]")
        return

    table = Table(title="Watched Users / Traders", expand=True)
    table.add_column("ID", style="dim", width=6)
    table.add_column("Ch ID", style="dim")
    table.add_column("Handle", style="cyan")
    table.add_column("Display Name", style="bold")
    table.add_column("Enabled", justify="center")
    table.add_column("Trades", justify="center")
    table.add_column("Charts", justify="center")
    table.add_column("Commentary", justify="center")
    table.add_column("Advice", justify="center")

    for u in users:
        table.add_row(
            str(u["id"]),
            str(u["channel_id"]),
            u["platform_handle"],
            u["display_name"] or "",
            _bool_emoji(u["is_enabled"]),
            _bool_emoji(u["track_trades"]),
            _bool_emoji(u["track_charts"]),
            _bool_emoji(u["track_commentary"]),
            _bool_emoji(u["track_advice"]),
        )
    console.print(table)

    choice = Prompt.ask(
        "Enter ID to toggle, 't' to change tracking flags, or Enter to skip",
        default="",
        show_default=False,
    )
    if not choice:
        return
    if choice.lower() == "t":
        if not _readonly_guard("user tracking flags"):
            return
        uid_str = Prompt.ask("User ID (or 'x' to cancel)", default="")
        if uid_str.lower() in ("x", "q", "cancel", ""):
            console.print("[dim]Cancelled.[/dim]")
            return
        try:
            uid = int(uid_str)
        except ValueError:
            console.print("[red]Invalid ID — must be a number.[/red]")
            return
        flags = {
            "track_trades": Confirm.ask("Track trades?", default=True),
            "track_charts": Confirm.ask("Track charts?", default=True),
            "track_commentary": Confirm.ask("Track commentary?", default=True),
            "track_advice": Confirm.ask("Track advice?", default=True),
            "track_media": Confirm.ask("Track media?", default=True),
            "auto_detect_entries": Confirm.ask("Auto-detect entries?", default=True),
            "auto_detect_sl_tp": Confirm.ask("Auto-detect SL/TP?", default=True),
        }
        await db.update_user_tracking(uid, **flags)
        console.print("[green]Tracking flags updated.[/green]")
    elif choice.isdigit():
        if not _readonly_guard("user toggle"):
            return
        uid = int(choice)
        for u in users:
            if u["id"] == uid:
                new_state = not u["is_enabled"]
                await db.toggle_user(uid, new_state)
                console.print(
                    f"[green]User '{u['display_name'] or u['platform_handle']}' now {'enabled' if new_state else 'disabled'}.[/green]"
                )
                break
        else:
            console.print("[red]ID not found.[/red]")


async def view_leaderboard() -> None:
    """Show trader leaderboard with stats, R:R, directional bias, win/loss."""
    days = IntPrompt.ask("Lookback period (days)", default=30)
    rows = await db.get_leaderboard(days=days)
    if not rows:
        console.print("[yellow]No trades recorded in the lookback period.[/yellow]")
        return

    table = Table(title=f"Trader Leaderboard (last {days} days)", expand=True)
    table.add_column("Rank", style="dim", width=4, justify="center")
    table.add_column("Trader", style="bold")
    table.add_column("Trades", justify="right")
    table.add_column("Wins", style="green", justify="right")
    table.add_column("Losses", style="red", justify="right")
    table.add_column("Win Rate", justify="right")
    table.add_column("Total P&L", justify="right")
    table.add_column("Avg R:R", justify="right")
    table.add_column("Bias", justify="center")
    table.add_column("Most Traded", style="dim")

    for i, r in enumerate(rows, 1):
        total = r.get("total_trades", 0)
        wins = r.get("wins", 0)
        losses = r.get("losses", 0)
        win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0.0
        pnl = r.get("total_pnl") or 0.0
        rr = r.get("avg_rr") or 0.0
        long_c = r.get("long_count", 0)
        short_c = r.get("short_count", 0)
        if long_c > short_c * 1.5:
            bias = "[green]LONG[/green]"
        elif short_c > long_c * 1.5:
            bias = "[red]SHORT[/red]"
        else:
            bias = "[yellow]MIXED[/yellow]"
        pnl_color = "green" if pnl >= 0 else "red"

        table.add_row(
            str(i),
            r.get("display_name") or r.get("handle_normalized") or "unknown",
            str(total),
            str(wins),
            str(losses),
            f"{win_rate:.1f}%",
            f"[{pnl_color}]${pnl:,.2f}[/{pnl_color}]",
            f"{rr:.2f}",
            bias,
            r.get("most_traded_symbol") or "",
        )
    console.print(table)


async def view_hierarchy() -> None:
    """Show nested source -> channel -> user tree."""
    tree = await db.get_hierarchy()
    if not tree:
        console.print("[yellow]No hierarchy configured.[/yellow]")
        return

    for src in tree:
        src_style = "green" if src["is_enabled"] else "red"
        src_emoji = "✓" if src["is_enabled"] else "✗"
        console.print(
            f"\n[{src_style}]{src_emoji} [{src['source_type'].upper()}] {src['source_name']}[/] "
            f"[dim]({src['source_slug']})[/dim]"
        )
        for ch in src.get("channels", []):
            ch_style = "green" if ch["is_enabled"] else "red"
            ch_emoji = "✓" if ch["is_enabled"] else "✗"
            console.print(
                f"  [{ch_style}]{ch_emoji} #{ch['channel_name']}[/] "
                f"[dim]({ch['channel_slug']})[/dim]"
            )
            for u in ch.get("users", []):
                u_style = "green" if u["is_enabled"] else "red"
                u_emoji = "✓" if u["is_enabled"] else "✗"
                tags = []
                if u["track_trades"]:
                    tags.append("trades")
                if u["track_charts"]:
                    tags.append("charts")
                if u["track_commentary"]:
                    tags.append("chat")
                if u["track_advice"]:
                    tags.append("advice")
                tag_str = f" [{', '.join(tags)}]" if tags else ""
                console.print(
                    f"    [{u_style}]{u_emoji} {u['display_name'] or u['platform_handle']}[/]"
                    f"[dim]{tag_str}[/dim]"
                )


# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------
MENU_ITEMS: List[tuple[str, str, Any]] = [
    ("1", "Dashboard", view_dashboard),
    ("2", "Sources", view_sources),
    ("3", "Channels", view_channels),
    ("4", "Users", view_users),
    ("5", "Leaderboard", view_leaderboard),
    ("6", "Hierarchy (tree view)", view_hierarchy),
    ("q", "Quit", None),
]


def _render_menu() -> Panel:
    table = Table(show_header=False, box=None, expand=True)
    table.add_column(style="bold cyan", width=4)
    table.add_column(style="white")
    for key, label, _ in MENU_ITEMS:
        table.add_row(f"[{key}]", label)
    return Panel(
        Align.center(table, vertical="middle"),
        title="[bold]Collector Catalogue Manager[/bold]",
        subtitle="[dim]v2.0 — Tickles & Co[/dim]",
        border_style="blue",
    )


async def main() -> None:
    """Run the interactive TUI loop."""
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    console.clear()
    console.print(_render_menu())

    while True:
        choice = Prompt.ask(
            "\nSelect option",
            choices=[m[0] for m in MENU_ITEMS],
            default="1",
        )
        if choice == "q":
            console.print("[dim]Goodbye.[/dim]")
            break

        for key, _, handler in MENU_ITEMS:
            if key == choice and handler is not None:
                console.clear()
                try:
                    await handler()
                except Exception as exc:
                    logger.exception("Menu handler failed")
                    console.print(f"[red]Error: {exc}[/red]")
                console.print("\n" + "─" * console.width)
                break


if __name__ == "__main__":
    asyncio.run(main())
