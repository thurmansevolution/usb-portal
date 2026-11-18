"""
Shared CLI utilities: prompting, display helpers, error formatting.
"""

import os
import sys
from typing import Optional, TypeVar

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from usb_portal.config import APP_NAME, APP_VERSION

console = Console()
error_console = Console(stderr=True, style="bold red")

T = TypeVar("T")


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def print_header(subtitle: str = "") -> None:
    title = Text(APP_NAME, style="bold cyan")
    if subtitle:
        title.append(f"  —  {subtitle}", style="dim white")
    version_line = Text(f"v{APP_VERSION}", style="dim")
    console.print(Panel(title, subtitle=f"v{APP_VERSION}", border_style="cyan"))


def print_error(msg: str) -> None:
    console.print(f"[bold red]  ✗  {msg}[/bold red]")


def print_success(msg: str) -> None:
    console.print(f"[bold green]  ✓  {msg}[/bold green]")


def print_warning(msg: str) -> None:
    console.print(f"[bold yellow]  ⚠  {msg}[/bold yellow]")


def print_info(msg: str) -> None:
    console.print(f"[dim]  ℹ  {msg}[/dim]")


def prompt(text: str, default: str = "") -> str:
    """Simple text prompt with optional default."""
    suffix = f" [{default}]" if default else ""
    try:
        value = console.input(f"[bold cyan]  > {text}{suffix}:[/bold cyan] ")
    except (EOFError, KeyboardInterrupt):
        return default
    return value.strip() or default


def prompt_choice(
    options: list[str],
    prompt_text: str = "Select",
    allow_back: bool = True,
    back_label: str = "Back",
) -> Optional[int]:
    """
    Display a numbered menu and return the 0-based index of the chosen option.
    Returns None if the user chose to go back.
    """
    console.print()
    for i, option in enumerate(options, start=1):
        console.print(f"  [bold cyan]{i}[/bold cyan]  {option}")

    if allow_back:
        console.print(f"  [bold cyan]0[/bold cyan]  {back_label}")

    console.print()

    while True:
        try:
            raw = console.input(f"[bold cyan]  > {prompt_text}: [/bold cyan]").strip()
        except (EOFError, KeyboardInterrupt):
            return None

        if raw == "0" and allow_back:
            return None

        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return idx

        print_error(f"Invalid choice. Enter 1–{len(options)}" + (" or 0 to go back." if allow_back else "."))


def prompt_yes_no(question: str, default: bool = True) -> bool:
    """Prompt for yes/no confirmation."""
    hint = "[Y/n]" if default else "[y/N]"
    try:
        raw = console.input(f"[bold cyan]  > {question} {hint}: [/bold cyan]").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False

    if not raw:
        return default
    return raw.startswith("y")


def pause(msg: str = "Press Enter to continue...") -> None:
    try:
        console.input(f"\n[dim]  {msg}[/dim]")
    except (EOFError, KeyboardInterrupt):
        pass


def require_root() -> bool:
    """Check if running as root; print error and return False if not."""
    if os.geteuid() != 0:
        print_error("This operation requires root privileges. Run with sudo.")
        return False
    return True
