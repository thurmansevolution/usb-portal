"""
Host mode CLI menu: share local game controllers over LAN.
"""

import os
from typing import Optional

from rich.table import Table

from usb_portal.cli.common import (
    clear_screen,
    console,
    pause,
    print_error,
    print_header,
    print_info,
    print_success,
    print_warning,
    prompt,
    prompt_choice,
    prompt_yes_no,
    require_root,
)
from usb_portal.host import HostSession, SharedDevice, get_session
from usb_portal.usbip import UsbipDevice, UsbipError, UsbipNotFound, list_exportable


def run_host_menu() -> None:
    """Entry point for host mode."""
    if not require_root():
        pause()
        return

    session = get_session()

    try:
        session.setup()
    except UsbipError as e:
        print_error(f"Failed to load USB/IP modules: {e}")
        print_info("Ensure usbip kernel modules are installed: sudo apt install linux-tools-generic")
        pause()
        return

    try:
        if not session.is_daemon_running():
            session.start_daemon()
            print_success("USB/IP daemon started.")
        session.start_advertising()
    except UsbipNotFound as e:
        print_error(str(e))
        print_info("Install usbip tools: sudo apt install usbip")
        pause()
        return
    except UsbipError as e:
        print_error(f"Failed to start daemon: {e}")
        pause()
        return

    try:
        _host_menu_loop(session)
    finally:
        if prompt_yes_no("\nUnshare all devices and stop daemon?", default=True):
            session.shutdown()
            print_success("All devices unshared. Daemon stopped.")
        else:
            print_info("Devices remain shared while this process is running.")


def _host_menu_loop(session: HostSession) -> None:
    while True:
        clear_screen()
        print_header("Share Mode")

        session.sync_bound_devices()
        shared = session.get_shared_devices()

        if shared:
            _print_shared_table(shared)
        else:
            print_info("No devices currently shared.")

        console.print()
        options = [
            "Share a USB device",
            "Unshare a USB device",
            "Refresh status",
            "Return to main menu",
        ]

        choice = prompt_choice(options, "Select option", allow_back=False)
        if choice is None or choice == 3:
            break
        elif choice == 0:
            _share_any_device(session)
        elif choice == 1:
            _unshare_device(session)
        elif choice == 2:
            session.sync_bound_devices()


def _print_shared_table(shared: list[SharedDevice]) -> None:
    table = Table(title="Shared Devices", border_style="green", show_lines=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("Bus ID", style="cyan")
    table.add_column("Device", style="white")
    table.add_column("VID:PID", style="dim")
    table.add_column("Uptime", style="green")

    for i, dev in enumerate(shared, 1):
        table.add_row(
            str(i),
            dev.busid,
            dev.name,
            f"{dev.vendor_id}:{dev.product_id}" if dev.vendor_id else "—",
            dev.uptime_str(),
        )
    console.print(table)


def _share_any_device(session: HostSession) -> None:
    clear_screen()
    print_header("Share Any USB Device")

    console.print("\n[dim]  Fetching exportable devices...[/dim]")

    try:
        all_devices = list_exportable()
    except UsbipError as e:
        print_error(f"Could not list devices: {e}")
        pause()
        return

    if not all_devices:
        print_warning("No exportable USB devices found.")
        pause()
        return

    already_shared = {d.busid for d in session.get_shared_devices()}
    available = [d for d in all_devices if d.busid not in already_shared]

    if not available:
        print_info("All USB devices are already being shared.")
        pause()
        return

    table = Table(title="Available USB Devices", border_style="cyan", show_lines=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("Bus ID", style="cyan")
    table.add_column("Vendor", style="white")
    table.add_column("Product", style="white")
    table.add_column("VID:PID", style="dim")

    for i, dev in enumerate(available, 1):
        table.add_row(str(i), dev.busid, dev.vendor_name, dev.product_name, dev.vid_pid)

    console.print(table)

    choice = prompt_choice(
        [f"{d.busid} — {d.vendor_name} {d.product_name}" for d in available],
        "Select device to share",
    )
    if choice is None:
        return

    dev = available[choice]
    _do_share(session, dev.busid, dev)


def _do_share(session: HostSession, busid: str, device: Optional[UsbipDevice] = None) -> None:
    try:
        shared = session.share_device(busid, device)
        print_success(f"Now sharing: {shared.name}  (Bus ID: {busid})")
        print_info("Remote machines can now import this device.")
    except UsbipError as e:
        print_error(f"Failed to share device: {e}")
    pause()


def _unshare_device(session: HostSession) -> None:
    clear_screen()
    print_header("Unshare a Device")

    shared = session.get_shared_devices()
    if not shared:
        print_info("No devices are currently being shared.")
        pause()
        return

    options = [f"[cyan]{d.busid}[/cyan]  {d.name}  [dim](shared {d.uptime_str()})[/dim]" for d in shared]
    choice = prompt_choice(options, "Select device to unshare")
    if choice is None:
        return

    dev = shared[choice]
    if not prompt_yes_no(f"Unshare '{dev.name}'?"):
        return

    try:
        session.unshare_device(dev.busid)
        print_success(f"Unshared: {dev.name}")
    except UsbipError as e:
        print_error(f"Failed to unshare: {e}")
    pause()
