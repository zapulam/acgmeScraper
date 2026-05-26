"""Shared Rich terminal output helpers for the ACGME command-line scripts."""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich.text import Text
from rich.theme import Theme


"""# --- Constants ---"""

CLI_THEME = Theme(
    {
        "banner": "bold cyan",
        "info": "cyan",
        "success": "green",
        "warning": "yellow",
        "error": "bold red",
        "muted": "dim",
        "key": "bold white",
        "value": "white",
    }
)
CONSOLE = Console(theme=CLI_THEME)
ACGME_BANNER = r"""
  ___   ___  ___ __  __ ___
 / _ \ / __|/ __|  \/  | __|
| __ || (__| (_ | |\/| | _|
|_| |_|\___|\___|_|  |_|___|
"""


# --- Helper functions ---
def active_console(
        console_obj: Console | None = None,
    ) -> Console:
    """Return the provided Rich console or the shared project console."""
    return console_obj or CONSOLE


def print_banner(
        title: str,
        *,
        console_obj: Console | None = None,
    ) -> None:
    """Print a compact ACGME ASCII banner with a script-specific title."""
    console = active_console(console_obj)
    console.print()
    console.print(ACGME_BANNER.rstrip(), style="banner")
    console.print(Text(title.upper(), style="banner"))
    console.print()


def print_run_config(
        title: str,
        items: list[tuple[str, Any]],
        *,
        console_obj: Console | None = None,
    ) -> None:
    """Print a compact two-column run configuration section."""
    console = active_console(console_obj)
    table = Table.grid(padding=(0, 2))
    table.add_column(justify="right", style="key", no_wrap=True)
    table.add_column(style="value")

    for key, value in items:
        table.add_row(Text(str(key), style="key"), Text(str(value), style="value"))

    console.print(Text(title, style="info"))
    console.print(table)
    console.print()


def print_status(
        label: str,
        message: str,
        style: str,
        *,
        console_obj: Console | None = None,
    ) -> None:
    """Print one labeled status line with Rich styling."""
    console = active_console(console_obj)
    if label: line = Text(f"[{label}] ", style=style)
    else: line = Text(f"", style=style)
    line.append(str(message))
    console.print(line)


def print_info(
        message: str,
        *,
        console_obj: Console | None = None,
    ) -> None:
    """Print an informational status line."""
    print_status("", message, "info", console_obj=console_obj)


def print_success(
        message: str,
        *,
        console_obj: Console | None = None,
    ) -> None:
    """Print a successful completion status line."""
    print_status("OK", message, "success", console_obj=console_obj)


def print_warning(
        message: str,
        *,
        console_obj: Console | None = None,
    ) -> None:
    """Print a warning status line."""
    print_status("WARN", message, "warning", console_obj=console_obj)


def print_error(
        message: str,
        *,
        console_obj: Console | None = None,
    ) -> None:
    """Print an error status line."""
    print_status("ERROR", message, "error", console_obj=console_obj)


"""# --- Progress functions ---"""


def make_state_progress(
        *,
        console_obj: Console | None = None,
    ) -> Progress:
    """Create a Rich progress renderer for sequential state scrape progress."""
    return Progress(
        TextColumn("[info]{task.description}"),
        BarColumn(bar_width=None),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(compact=True),
        TextColumn(
            "[muted]contacts=[/]{task.fields[contacts]} "
            "[muted]skipped=[/]{task.fields[skipped]} "
            "[muted]errors=[/]{task.fields[errors]}"
        ),
        console=active_console(console_obj),
        transient=False,
        expand=True,
    )
