"""
Service management CLI menu.
Allows installing/uninstalling the systemd service and managing it when active.
When the service is running, Share Mode uses this as its management interface.
"""

import logging

from rich.table import Table
from rich.text import Text

from usb_portal.cli.common import (
    clear_screen,
    console,
    pause,
    print_error,
    print_header,
    print_info,
    print_success,
    print_warning,
    prompt_choice,
    prompt_yes_no,
    require_root,
)
from usb_portal import service
from usb_portal.service import ServiceError
from usb_portal.usbip import UsbipError, get_bound_devices, bind, unbind, list_exportable, get_local_device
from usb_portal.daemon import BoundDevice, DaemonState
from usb_portal.saved_hosts import get_config as get_hosts_config
from usb_portal.logging_setup import get_log_path

log = logging.getLogger("usb-portal-daemon")


def run_service_menu() -> None:
    """Entry point for the service management menu."""
    if not service.has_systemd():
        print_error("systemd is not available on this system.")
        print_info("Service mode requires a Linux system with systemd.")
        pause()
        return

    while True:
        clear_screen()
        print_header("Service Management")
        _print_service_status_bar()
        console.print()

        if not service.is_installed():
            _not_installed_menu()
            break
        else:
            if not _installed_menu():
                break


def _print_service_status_bar() -> None:
    installed = service.is_installed()
    active = service.is_active() if installed else False
    enabled = service.is_enabled() if installed else False

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="dim", width=18)
    table.add_column("Value")

    table.add_row("Service installed", "[green]Yes[/green]" if installed else "[red]No[/red]")
    if installed:
        table.add_row("Running", "[bold green]Active[/bold green]" if active else "[red]Stopped[/red]")
        table.add_row("Start at boot", "[green]Enabled[/green]" if enabled else "[yellow]Disabled[/yellow]")

    console.print(table)


def _not_installed_menu() -> None:
    """Menu shown when service is not yet installed."""
    print_info("The usb|portal systemd service is not installed.")
    console.print()
    console.print(
        "  Installing the service allows [bold]headless operation[/bold] — the daemon\n"
        "  runs in the background, surviving terminal closes and reboots.\n"
        "  The CLI becomes a management tool when the service is active.\n"
    )

    options = [
        "Install and start service",
        "Return to main menu",
    ]
    choice = prompt_choice(options, "Select option", allow_back=False)

    if choice == 0:
        _install_service()


def _installed_menu() -> bool:
    """Menu shown when service is installed. Returns False to exit."""
    active = service.is_active()

    options = []
    if active:
        options += [
            "Stop service",
            "Restart service",
        ]
    else:
        options += [
            "Start service",
        ]

    options += [
        "View service status",
        "View logs",
        "Manage auto-connect hosts",
        "Uninstall service",
        "Return to main menu",
    ]

    choice = prompt_choice(options, "Select option", allow_back=False)

    if choice is None:
        return False

    if active:
        if choice == 0:
            _stop_service()
        elif choice == 1:
            _restart_service()
        elif choice == 2:
            _view_status()
        elif choice == 3:
            _view_logs()
        elif choice == 4:
            _manage_auto_connect_hosts()
        elif choice == 5:
            _uninstall_service()
            return False
        elif choice == 6:
            return False
    else:
        if choice == 0:
            _start_service()
        elif choice == 1:
            _view_status()
        elif choice == 2:
            _view_logs()
        elif choice == 3:
            _manage_auto_connect_hosts()
        elif choice == 4:
            _uninstall_service()
            return False
        elif choice == 5:
            return False

    return True


def _install_service() -> None:
    if not require_root():
        pause()
        return

    console.print("\n[dim]  Locating daemon executable...[/dim]")
    try:
        service.install()
        print_success("Service installed and enabled.")
    except ServiceError as e:
        print_error(f"Install failed: {e}")
        print_info("Make sure the package is installed: pip install -e .")
        pause()
        return

    if prompt_yes_no("Start the service now?", default=True):
        try:
            service.start()
            print_success("Service started. It will now run in the background.")
            print_info("The CLI is now in management mode.")
        except ServiceError as e:
            print_error(f"Failed to start service: {e}")

    pause()


def _start_service() -> None:
    if not require_root():
        pause()
        return
    try:
        service.start()
        print_success("Service started.")
    except ServiceError as e:
        print_error(f"Failed to start: {e}")
    pause()


def _stop_service() -> None:
    if not require_root():
        pause()
        return
    if not prompt_yes_no("Stop the usb|portal service? Remote users will lose access.", default=False):
        return
    try:
        service.stop()
        print_success("Service stopped.")
    except ServiceError as e:
        print_error(f"Failed to stop: {e}")
    pause()


def _restart_service() -> None:
    if not require_root():
        pause()
        return
    try:
        service.restart()
        print_success("Service restarted.")
    except ServiceError as e:
        print_error(f"Failed to restart: {e}")
    pause()


def _view_status() -> None:
    clear_screen()
    print_header("Service Status")
    status_text = service.get_status()
    console.print(status_text)
    pause()


def _view_logs() -> None:
    clear_screen()
    print_header("Service Logs")
    console.print(f"Log file: {get_log_path()}", markup=False)
    logs = service.get_logs(lines=40)
    console.print(logs, markup=False, highlight=False)
    pause()


def _manage_auto_connect_hosts() -> None:
    """View and manage saved Use-side hosts and their auto-connect devices."""
    while True:
        clear_screen()
        print_header("Auto-Connect Hosts  [dim](Use Side)[/dim]")

        config = get_hosts_config()
        hosts = config.get_all()

        if hosts:
            table = Table(title="Saved Hosts", border_style="cyan", show_lines=True)
            table.add_column("#", style="dim", width=4)
            table.add_column("Display Name", style="yellow")
            table.add_column("Hostname", style="white")
            table.add_column("IP", style="cyan")
            table.add_column("Network", style="dim")
            table.add_column("Auto-Connect Devices", style="green")
            for i, h in enumerate(hosts, 1):
                ac = ", ".join(d.name or d.busid for d in h.auto_connect) if h.auto_connect else "[dim]none[/dim]"
                table.add_row(
                    str(i),
                    h.display_name,
                    h.hostname or "[dim]—[/dim]",
                    h.ip,
                    h.network or "[dim]—[/dim]",
                    ac,
                )
            console.print(table)
        else:
            print_info("No saved hosts.")
            console.print()

        options = [
            "Remove auto-connect device from a host",
            "Remove a host entirely",
            "Back",
        ]
        choice = prompt_choice(options, "Select option", allow_back=False)

        if choice is None or choice == 2:
            break
        elif choice == 0:
            entries = [(h.ip, dev) for h in hosts for dev in h.auto_connect]
            if not entries:
                print_info("No auto-connect devices configured.")
                pause()
                continue
            opts = [f"{config.get(ip).display_name if config.get(ip) else ip} — {dev.name or dev.busid}" for ip, dev in entries]
            sel = prompt_choice(opts, "Select device to remove")
            if sel is None:
                continue
            host_ip, dev = entries[sel]
            config.remove_auto_connect_device(host_ip, dev.busid)
            print_success(f"Removed '{dev.name or dev.busid}' from auto-connect.")
            pause()
        elif choice == 1:
            if not hosts:
                print_info("No hosts to remove.")
                pause()
                continue
            opts = [f"{h.display_name} [{h.ip}]" for h in hosts]
            sel = prompt_choice(opts, "Select host to remove")
            if sel is None:
                continue
            host = hosts[sel]
            if prompt_yes_no(f"Remove '{host.display_name}'? This also clears its auto-connect devices.", default=False):
                config.remove_host(host.ip)
                print_success(f"Removed: {host.display_name}")
                pause()


def _uninstall_service() -> None:
    if not require_root():
        pause()
        return

    console.print()
    print_warning("This will stop the usb|portal service and remove it from systemd.")
    if not prompt_yes_no("Are you sure you want to uninstall the service?", default=False):
        return

    try:
        service.uninstall()
        print_success("Service uninstalled. The app will return to standard CLI mode.")
    except ServiceError as e:
        print_error(f"Uninstall failed: {e}")
    pause()


def _manage_devices_menu() -> None:
    """
    When the service is active, this replaces the normal Share Mode menu.
    Allows binding/unbinding devices directly via usbip commands,
    with the daemon detecting and persisting the changes.
    """
    while True:
        clear_screen()
        print_header("Manage Shared Devices  [dim](Service Mode)[/dim]")

        bound = get_bound_devices()
        if bound:
            _print_bound_table(bound)
        else:
            print_info("No devices currently bound for sharing.")

        console.print()
        options = [
            "Share a USB device",
            "Unshare a USB device",
            "Auto-rebind settings",
            "Refresh",
            "Back",
        ]
        choice = prompt_choice(options, "Select option", allow_back=False)

        if choice is None or choice == 4:
            break
        elif choice == 0:
            _bind_any_device()
        elif choice == 1:
            _unbind_device_menu(bound)
        elif choice == 2:
            _auto_rebind_settings()
        elif choice == 3:
            continue


def _print_bound_table(bound: list[str]) -> None:
    table = Table(title="Bound Devices", border_style="green", show_lines=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("Bus ID", style="cyan")
    table.add_column("Device", style="white")
    table.add_column("VID:PID", style="dim")

    for i, busid in enumerate(bound, 1):
        device = _bound_device_info(busid)
        table.add_row(
            str(i),
            busid,
            device.name,
            device.vid_pid,
        )

    console.print(table)


def _bound_device_info(busid: str) -> BoundDevice:
    device = get_local_device(busid)
    if device:
        name = f"{device.vendor_name} {device.product_name}".strip() or "Unknown USB device"
        return BoundDevice(
            busid=busid,
            vendor_id=device.vendor_id,
            product_id=device.product_id,
            name=name,
            auto_rebind=False,
        )

    state_match = next((d for d in DaemonState().get_bindings() if d.busid == busid), None)
    if state_match:
        return state_match

    return BoundDevice(
        busid=busid,
        vendor_id="",
        product_id="",
        name="Unknown USB device",
        auto_rebind=False,
    )


def _bind_any_device() -> None:
    if not require_root():
        pause()
        return
    console.print("\n[dim]  Fetching exportable devices...[/dim]")
    try:
        devices = list_exportable()
    except UsbipError as e:
        print_error(f"Could not list devices: {e}")
        pause()
        return

    if not devices:
        print_warning("No exportable devices found.")
        pause()
        return

    already_bound = set(get_bound_devices())
    available = [d for d in devices if d.busid not in already_bound]

    if not available:
        print_info("All USB devices are already bound.")
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

    options = [f"{d.busid} — {d.vendor_name} {d.product_name}" for d in available]
    choice = prompt_choice(options, "Select device to share")
    if choice is None:
        return

    dev = available[choice]
    console.print()
    auto_rebind = prompt_yes_no(
        "Enable auto-rebind? (reconnect this device automatically after reboot/unplug)",
        default=True,
    )
    try:
        bind(dev.busid)
        device_name = f"{dev.vendor_name} {dev.product_name}".strip() or dev.busid
        state_saved = _update_daemon_state(BoundDevice(
            busid=dev.busid,
            vendor_id=dev.vendor_id,
            product_id=dev.product_id,
            name=device_name,
            auto_rebind=auto_rebind,
        ))
        print_success(f"Bound: {device_name} ({dev.busid})")
        if auto_rebind:
            if state_saved:
                print_info("Auto-rebind enabled — device will reconnect automatically after reboot.")
            else:
                print_warning("Bound for this session, but auto-rebind could not be saved.")
        else:
            if state_saved:
                print_info("Single-session binding. Use 'Share' again after a reboot to re-share.")
            else:
                print_warning("Bound for this session, but auto-rebind settings could not be updated.")
    except UsbipError as e:
        print_error(f"Bind failed: {e}")
    pause()


def _update_daemon_state(device: BoundDevice) -> bool:
    """Add a device to the daemon's state file so it auto-rebinds on reconnect."""
    try:
        state = DaemonState()
        unique = _has_unique_active_identity(device)
        def update(bindings):
            remaining = _bindings_without_device(bindings, device, remove_unique_identity=unique)
            if device.auto_rebind:
                remaining.append(device)
            return remaining
        if not state.update_bindings(update):
            print_error(f"Could not save auto-rebind settings: {state.last_error or 'state update failed'}")
            return False
        log.info("Auto-rebind %s for %s [%s] %s",
                 "enabled" if device.auto_rebind else "disabled",
                 device.busid, device.vid_pid, device.name)
        return True
    except Exception as error:
        log.exception("Could not update auto-rebind for %s", device.busid)
        print_error(f"Could not save auto-rebind settings: {error}")
        return False


def _remove_from_daemon_state(
    busid: str,
    vendor_id: str = "",
    product_id: str = "",
) -> bool:
    """Remove a device from the daemon's state file."""
    try:
        state = DaemonState()
        target = BoundDevice(
            busid=busid,
            vendor_id=vendor_id,
            product_id=product_id,
        )
        saved = state.update_bindings(
            lambda bindings: _bindings_without_device(bindings, target, remove_unique_identity=True)
        )
        if not saved:
            print_error(f"Could not save auto-rebind settings: {state.last_error or 'state update failed'}")
        return saved
    except Exception as error:
        log.exception("Could not remove auto-rebind for %s", busid)
        print_error(f"Could not save auto-rebind settings: {error}")
        return False


def _bindings_without_device(
    bindings: list[BoundDevice],
    target: BoundDevice,
    remove_unique_identity: bool = False,
) -> list[BoundDevice]:
    """
    Remove the binding entry represented by target.

    Bus IDs are the live USB/IP handle, but a unique VID:PID lets us update
    stale memory when the same device moved to another USB port. Duplicate
    serial-less devices intentionally stay busid-scoped.
    """
    same_identity = [
        b for b in bindings
        if _same_state_identity(b, target)
    ]
    remove_by_identity = remove_unique_identity and len(same_identity) == 1

    remaining = []
    for binding in bindings:
        if binding.busid == target.busid and _compatible_state_identity(binding, target):
            continue
        if remove_by_identity and _same_state_identity(binding, target):
            continue
        remaining.append(binding)
    return remaining


def _has_unique_active_identity(target: BoundDevice) -> bool:
    """Return True when exactly one currently bound device has target's VID:PID."""
    if not target.vendor_id or not target.product_id:
        return False

    matches = 0
    for busid in get_bound_devices():
        device = get_local_device(busid)
        if not device:
            continue
        if device.vendor_id == target.vendor_id and device.product_id == target.product_id:
            matches += 1
            if matches > 1:
                return False
    return matches == 1


def _same_state_identity(left: BoundDevice, right: BoundDevice) -> bool:
    return (
        bool(left.vendor_id)
        and bool(left.product_id)
        and left.vendor_id == right.vendor_id
        and left.product_id == right.product_id
    )


def _compatible_state_identity(existing: BoundDevice, target: BoundDevice) -> bool:
    if not existing.vendor_id or not existing.product_id:
        return True
    if not target.vendor_id or not target.product_id:
        return True
    return _same_state_identity(existing, target)


def _auto_rebind_settings() -> None:
    """View and manage devices saved for auto-rebind on restart."""
    if not require_root():
        pause()
        return
    while True:
        clear_screen()
        print_header("Auto-Rebind Settings  [dim](Share)[/dim]")

        state = DaemonState()
        try:
            bindings = state.get_bindings()
        except OSError as error:
            log.exception("Could not read auto-rebind settings")
            print_error(str(error))
            pause()
            return
        auto_rebind = [b for b in bindings if b.auto_rebind]

        if auto_rebind:
            table = Table(title="Auto-Rebind Devices", border_style="green", show_lines=True)
            table.add_column("#", style="dim", width=4)
            table.add_column("Bus ID", style="cyan")
            table.add_column("Device", style="white")
            table.add_column("VID:PID", style="dim")
            for i, b in enumerate(auto_rebind, 1):
                table.add_row(str(i), b.busid, b.name or "—", b.vid_pid if b.vendor_id else "—")
            console.print(table)
            console.print()
            print_info("These devices will be automatically re-shared after a reboot or unplug.")
        else:
            print_info("No devices are configured for auto-rebind.")
            console.print()

        options = [
            "Enable auto-rebind for an already shared device",
            "Remove a device from auto-rebind",
            "Clear all auto-rebind entries",
            "Back",
        ]
        choice = prompt_choice(options, "Select option", allow_back=False)

        if choice is None or choice == 3:
            break
        elif choice == 0:
            _enable_auto_rebind_menu()
        elif choice == 1:
            if not auto_rebind:
                print_info("No auto-rebind devices to remove.")
                pause()
                continue
            opts = [f"{b.busid} — {b.name or b.vid_pid}" for b in auto_rebind]
            sel = prompt_choice(opts, "Select device to remove from auto-rebind")
            if sel is None:
                continue
            device = auto_rebind[sel]
            if _remove_from_daemon_state(device.busid, device.vendor_id, device.product_id):
                print_success(f"Removed '{device.name or device.busid}' from auto-rebind. It will not be re-shared on next reboot.")
            pause()
        elif choice == 2:
            if not auto_rebind:
                print_info("Nothing to clear.")
                pause()
                continue
            if prompt_yes_no("Clear ALL auto-rebind entries? Devices will not reconnect after reboot.", default=False):
                try:
                    if state.set_bindings([]):
                        log.info("Auto-rebind list cleared")
                        print_success("Auto-rebind list cleared.")
                    else:
                        print_error(f"Could not clear auto-rebind settings: {state.last_error or 'state update failed'}")
                except OSError as error:
                    log.exception("Could not clear auto-rebind settings")
                    print_error(str(error))
                pause()


def _enable_auto_rebind_menu() -> None:
    """Save intent without binding again or interrupting the connected client."""
    if not require_root():
        pause()
        return
    devices = [_bound_device_info(busid) for busid in get_bound_devices()]
    if not devices:
        print_info("Share the device first, then enable auto-rebind here.")
        pause()
        return
    choice = prompt_choice(
        [f"{d.busid} — {d.name} [{d.vid_pid}]" for d in devices],
        "Select shared device for auto-rebind",
    )
    if choice is None:
        return
    device = devices[choice]
    if not device.vendor_id or not device.product_id:
        print_error("Could not read the device identity. Check that it is still plugged in and try again.")
    else:
        device.auto_rebind = True
        if _update_daemon_state(device):
            print_success(f"Auto-rebind enabled for {device.name} ({device.busid}).")
            print_info("The background service will re-share it after reboot or unplug/replug.")
    pause()


def _unbind_device_menu(bound: list[str]) -> None:
    if not require_root():
        pause()
        return
    if not bound:
        print_info("No bound devices to unshare.")
        pause()
        return

    infos = [_bound_device_info(busid) for busid in bound]
    options = [
        f"[cyan]{device.busid}[/cyan]  {device.name}  [dim][{device.vid_pid if device.vendor_id else 'unknown'}][/dim]"
        for device in infos
    ]
    choice = prompt_choice(options, "Select device to unshare")
    if choice is None:
        return

    device = infos[choice]
    busid = device.busid
    if not prompt_yes_no(f"Unshare '{device.name}'? (will also remove from auto-rebind list)"):
        return

    try:
        unbind(busid)
        print_success(f"Unbound: {device.name}")
        if not _remove_from_daemon_state(busid, device.vendor_id, device.product_id):
            print_warning("Device was unbound, but auto-rebind settings could not be updated.")
    except UsbipError as e:
        print_error(f"Unbind failed: {e}")
    pause()
