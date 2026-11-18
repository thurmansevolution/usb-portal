"""
Main CLI menu for usb|portal.
Provides top-level navigation between Share Mode, Use Mode, and settings.
"""

import os
import shutil
import socket
import sys

from rich.table import Table
from rich.text import Text

from usb_portal.cli.client_menu import run_client_menu
from usb_portal.cli.common import (
    clear_screen,
    console,
    pause,
    print_header,
    print_info,
    prompt_choice,
)
from usb_portal.cli.host_menu import run_host_menu
from usb_portal.cli.service_menu import run_service_menu
from usb_portal.config import APP_NAME, APP_VERSION, USBIP_PORT
from usb_portal.discovery import get_local_hostname, get_local_ip
from usb_portal.distro import (
    get_distro_display_name,
    get_modprobe_command,
    get_platform_summary,
    get_usbip_install_command,
    is_raspberry_pi,
)
from usb_portal.usbip import check_prerequisites
from usb_portal import service as svc


def run_main_menu() -> None:
    """Main application entry point — runs the top-level menu loop."""
    while True:
        clear_screen()
        print_header()
        _print_system_status()

        console.print()

        service_installed = svc.has_systemd() and svc.is_installed()
        service_active = service_installed and svc.is_active()

        if service_active:
            share_label = "[bold green]Share Mode[/bold green]  — Manage shared devices"
        else:
            share_label = "[bold green]Share Mode[/bold green]  — Share a USB device from this machine"

        options = [
            share_label,
            "[bold yellow]Use Mode[/bold yellow]   — Use a USB device shared from another machine",
            "Service Management — Install/manage the headless background service",
            "System Check       — Check USB/IP prerequisites",
            "About",
            "Exit",
        ]

        choice = prompt_choice(options, "Select option", allow_back=False)

        if choice is None or choice == 5:
            _exit()
        elif choice == 0:
            if service_active:
                from usb_portal.cli.service_menu import _manage_devices_menu
                _manage_devices_menu()
            else:
                run_host_menu()
        elif choice == 1:
            run_client_menu()
        elif choice == 2:
            run_service_menu()
        elif choice == 3:
            _system_check_menu()
        elif choice == 4:
            _about_menu()


def _print_system_status() -> None:
    hostname = get_local_hostname()
    local_ip = get_local_ip()

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="dim", width=20)
    table.add_column("Value", style="white")
    table.add_row("Hostname", hostname)
    table.add_row("Local IP", local_ip)
    table.add_row("Platform", get_distro_display_name())
    table.add_row("USB/IP Port", str(USBIP_PORT))
    table.add_row("Running as", "[bold red]root[/bold red]" if os.geteuid() == 0 else "[yellow]user (need sudo)[/yellow]")

    if svc.has_systemd():
        if svc.is_installed():
            svc_status = "[bold green]Active[/bold green]" if svc.is_active() else "[red]Stopped[/red]"
        else:
            svc_status = "[dim]Not installed[/dim]"
        table.add_row("Service", svc_status)

    console.print(table)


def _system_check_menu() -> None:
    clear_screen()
    print_header("System Check")

    platform_info = get_platform_summary()
    ptable = Table(title="Platform", border_style="dim", show_lines=False, box=None, padding=(0, 2))
    ptable.add_column("", style="dim", width=18)
    ptable.add_column("", style="white")
    for k, v in platform_info.items():
        ptable.add_row(k, v)
    console.print(ptable)
    console.print()

    checks = check_prerequisites()

    table = Table(title="Prerequisites", border_style="blue", show_lines=True)
    table.add_column("Component", style="white")
    table.add_column("Status", style="white")
    table.add_column("Notes", style="dim")

    status_map = {
        True: "[bold green]  ✓  OK[/bold green]",
        False: "[bold red]  ✗  Missing[/bold red]",
    }

    notes = {
        "usbip": "Required for device listing and attach/detach",
        "usbipd": "Required for Share Mode (exports devices)",
        "root": "Root is required for bind/attach operations",
        "module_usbip_core": "Core USB/IP kernel module",
        "module_usbip_host": "Required for Share Mode",
        "module_vhci_hcd": "Required for Use Mode (virtual HCI)",
    }

    labels = {
        "usbip": "usbip command",
        "usbipd": "usbipd daemon",
        "root": "Root privileges",
        "module_usbip_core": "Module: usbip_core",
        "module_usbip_host": "Module: usbip_host",
        "module_vhci_hcd": "Module: vhci_hcd",
    }

    for key, ok in checks.items():
        table.add_row(labels.get(key, key), status_map[ok], notes.get(key, ""))

    console.print(table)

    install_cmd = get_usbip_install_command()
    modprobe_cmd = get_modprobe_command()

    console.print(f"\n[dim]  Install missing tools ({get_distro_display_name()}):[/dim]")
    console.print(f"  [cyan]{install_cmd}[/cyan]")
    console.print("\n[dim]  Load kernel modules manually:[/dim]")
    console.print(f"  [cyan]{modprobe_cmd}[/cyan]")

    if is_raspberry_pi():
        console.print("\n[dim]  Raspberry Pi tip: modules are usually pre-built; try modprobe first.[/dim]")

    pause()


def _about_menu() -> None:
    clear_screen()
    print_header("About")

    console.print(f"""
  [bold]{APP_NAME}[/bold] [dim]v{APP_VERSION}[/dim]

  Share USB game controllers over your local network using USB/IP.
  Built on the Linux USB/IP kernel subsystem with improvements:

  [green]•[/green] Automatic LAN discovery via UDP broadcast
  [green]•[/green] Smart game controller detection (HID class + known VIDs)
  [green]•[/green] Auto-reconnect on network drop
  [green]•[/green] Auto-rebind when controller is physically unplugged and replugged
  [green]•[/green] Headless service mode (systemd) — survives reboots
  [green]•[/green] Unified CLI — no manual usbipd/usbip juggling
  [green]•[/green] Works on x86, ARM, and Raspberry Pi

  [dim]USB/IP Protocol Port:[/dim] {USBIP_PORT}
  [dim]Discovery Port:[/dim]      3241

  [dim]Requires Linux with USB/IP kernel support (kernel ≥ 3.17)[/dim]
  [dim]Running on:[/dim] {get_distro_display_name()}
""")
    pause()


def _exit() -> None:
    clear_screen()
    console.print(f"\n  [dim]Goodbye from {APP_NAME}![/dim]\n")
    sys.exit(0)
