"""
Client mode CLI menu: discover and use game controllers from remote hosts.
Includes Saved Hosts management for VPN / non-LAN hosts with auto-connect.
"""

import ipaddress
from typing import Optional

from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn
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
from usb_portal.client import ActiveConnection, ClientSession, get_session
from usb_portal.config import CIDR_SCAN_ATTEMPTS, USBIP_PORT
from usb_portal.discovery import CidrScanCancelled, CidrScanProgress, RemoteHost, parse_scan_network, scan_cidr
from usb_portal.saved_hosts import AutoConnectDevice, SavedHost, get_config
from usb_portal.saved_subnets import get_subnets_config
from usb_portal.usbip import UsbipDevice, UsbipError, UsbipNotFound


def run_client_menu() -> None:
    """Entry point for client (use) mode."""
    if not require_root():
        pause()
        return

    session = get_session()

    try:
        session.setup()
    except UsbipError as e:
        print_error(f"Failed to load USB/IP modules: {e}")
        print_info("Ensure usbip kernel modules are installed.")
        pause()
        return

    session.start_monitoring()
    session.start_auto_connect()

    try:
        _client_menu_loop(session)
    finally:
        session.stop_monitoring()


def _client_menu_loop(session: ClientSession) -> None:
    while True:
        clear_screen()
        print_header("Use Mode")

        session.sync_connections()
        connections = [c for c in session.get_connections() if c.is_alive()]

        if connections:
            _print_connections_table(connections)
        else:
            print_info("No active connections.")

        saved_count = len(get_config().get_all())
        saved_label = f"Saved Hosts         — Manage bookmarked hosts [dim]({saved_count} saved)[/dim]"

        console.print()
        options = [
            "Discover hosts on LAN",
            saved_label,
            "Auto-connect settings",
            "Disconnect a device",
            "Refresh status",
            "Return to main menu",
        ]

        choice = prompt_choice(options, "Select option", allow_back=False)
        if choice is None or choice == 5:
            break
        elif choice == 0:
            _discovery_menu(session)
        elif choice == 1:
            _saved_hosts_menu(session)
        elif choice == 2:
            _auto_connect_settings()
        elif choice == 3:
            _disconnect_device(session)
        elif choice == 4:
            session.sync_connections()


def _print_connections_table(connections: list[ActiveConnection]) -> None:
    table = Table(title="Active Connections", border_style="green", show_lines=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("Port", style="cyan", width=6)
    table.add_column("Device", style="white")
    table.add_column("Host", style="yellow")
    table.add_column("Uptime", style="green")
    table.add_column("Auto-Reconnect", style="dim")

    for i, conn in enumerate(connections, 1):
        table.add_row(
            str(i),
            str(conn.port),
            conn.device_name,
            conn.host_name,
            conn.uptime_str(),
            "[green]On[/green]" if conn.reconnect_enabled else "[red]Off[/red]",
        )
    console.print(table)


# ---------------------------------------------------------------------------
# Discovery menu and LAN discovery
# ---------------------------------------------------------------------------

def _discovery_menu(session: ClientSession) -> None:
    while True:
        clear_screen()
        print_header("Discover Hosts")
        options = [
            "Scan Local Area Network",
            "Scan by IP Address",
            "Scan Subnet",
            "Saved Subnets",
            "Back",
        ]
        choice = prompt_choice(options, "Select option", allow_back=False)
        if choice is None or choice == 4:
            return
        if choice == 0:
            _discover_and_connect(session)
        elif choice == 1:
            _connect_by_ip(session)
        elif choice == 2:
            _scan_subnets_menu(session)
        elif choice == 3:
            _saved_subnets_menu()


def _discover_and_connect(session: ClientSession) -> None:
    clear_screen()
    print_header("Discover Hosts")

    console.print("\n[dim]  Scanning LAN for usb|portal hosts...[/dim]")
    hosts = session.discover_hosts(timeout=3.0)

    if not hosts:
        print_warning("No hosts found on LAN.")
        print_info("Ensure the remote machine is running usb|portal in Share Mode.")
        pause()
        return

    _show_hosts_and_connect(session, hosts)


def _show_hosts_and_connect(
    session: ClientSession,
    hosts: list[RemoteHost],
    networks: Optional[dict[tuple[str, int], str]] = None,
) -> None:
    table = Table(title="Discovered Hosts", border_style="cyan", show_lines=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("Hostname", style="white")
    table.add_column("IP Address", style="cyan")
    table.add_column("Shared Devices", style="green")
    table.add_column("ID", style="dim", width=6)

    for i, host in enumerate(hosts, 1):
        count_str = str(host.device_count) if host.device_count >= 0 else "?"
        id_str = "[green]✓[/green]" if host.machine_id else "[dim]—[/dim]"
        table.add_row(str(i), host.hostname, host.ip, count_str, id_str)

    console.print(table)

    options = [f"{h.hostname} [{h.ip}]" for h in hosts]
    choice = prompt_choice(options, "Select host")
    if choice is None:
        return

    selected = hosts[choice]
    network = networks.get((selected.ip, selected.port), "") if networks else ""
    _maybe_save_host(selected.ip, selected.hostname, remote=selected, network=network)
    _browse_host(session, selected.ip, selected.hostname, selected.port)


# ---------------------------------------------------------------------------
# Connect by IP
# ---------------------------------------------------------------------------

def _connect_by_ip(session: ClientSession) -> None:
    clear_screen()
    print_header("Scan by IP Address")
    entry = prompt("Host IP address (leave blank to cancel)")
    if not entry:
        return
    try:
        address = str(ipaddress.ip_address(entry))
        existing = get_config().get(address)
        port = int(prompt("USB/IP port", default=str(existing.port if existing else USBIP_PORT)))
        if not 1 <= port <= 65535:
            raise ValueError("USB/IP port must be between 1 and 65535.")
    except (ValueError, OSError) as error:
        print_error(str(error))
        pause()
        return
    _browse_host(session, address, existing.display_name if existing else address, port, save_if_reachable=True)


def _connect_by_cidr(session: ClientSession, cidr: str) -> None:
    """Single-range entry point using the same temporary results as batch scans."""
    _scan_subnets_and_connect(session, [cidr])


def _scan_subnets_and_connect(session: ClientSession, cidrs: list[str]) -> None:
    if not cidrs:
        return
    try:
        cidrs = list(dict.fromkeys(str(parse_scan_network(cidr)) for cidr in cidrs))
    except ValueError as error:
        print_error(str(error))
        pause()
        return
    print_info("Press Ctrl+C to stop scanning and use any hosts already found.")
    cancelled = False
    found: dict[tuple[str, int], RemoteHost] = {}
    networks: dict[tuple[str, int], str] = {}

    def remember_result(host: RemoteHost, cidr: str) -> None:
        key = (host.ip, host.port)
        found[key] = host
        previous = networks.get(key)
        if previous is None or ipaddress.ip_network(cidr).prefixlen > ipaddress.ip_network(previous).prefixlen:
            networks[key] = cidr

    with Progress(
        SpinnerColumn(), TextColumn("{task.description}"), BarColumn(),
        MofNCompleteColumn(), TextColumn("{task.fields[found]} hosts"),
        TimeRemainingColumn(), console=console,
    ) as progress:
        task = progress.add_task("Scanning", total=None, found=0)
        for index, cidr in enumerate(cidrs, 1):
            progress.reset(task, total=None, completed=0, found=len(found), description=f"{cidr} ({index}/{len(cidrs)})")

            def update(state: CidrScanProgress) -> None:
                progress.update(task, completed=state.completed, total=state.total,
                                found=len(found),
                                description=f"{cidr} ({index}/{len(cidrs)}) · Pass {state.pass_number}/{CIDR_SCAN_ATTEMPTS}")

            try:
                hosts = scan_cidr(cidr, on_progress=update, on_host=lambda host: remember_result(host, cidr))
            except CidrScanCancelled as stopped:
                hosts = stopped.hosts
                cancelled = True
            except (ValueError, OSError) as error:
                print_error(f"Could not scan {cidr}: {error}")
                continue
            for host in hosts:
                remember_result(host, cidr)
            progress.update(task, found=len(found))
            if cancelled:
                break

    hosts = list(found.values())
    if cancelled:
        print_warning(f"Scan stopped; kept {len(hosts)} discovered host(s).")
    if not hosts:
        if not cancelled:
            print_warning("No usb|portal hosts found in the selected subnet(s).")
            print_info("Ensure the remote machine is running usb|portal in Share Mode.")
        pause()
        return

    print_success(f"Found {len(hosts)} host(s). Select a host to connect and add it to Saved Hosts.")
    _show_hosts_and_connect(session, hosts, networks=networks)


# ---------------------------------------------------------------------------
# Saved subnet management and explicit scan selection
# ---------------------------------------------------------------------------

def _print_saved_subnets(subnets: list[str]) -> None:
    table = Table(title="Saved Subnets", border_style="cyan")
    table.add_column("#", style="dim", width=4)
    table.add_column("Network ID/CIDR", style="cyan")
    for index, subnet in enumerate(subnets, 1):
        table.add_row(str(index), subnet)
    console.print(table)


def _saved_subnets_menu() -> None:
    while True:
        clear_screen()
        print_header("Saved Subnets")
        config = get_subnets_config()
        try:
            subnets = config.get_all()
        except OSError as error:
            print_error(str(error))
            pause()
            return
        if subnets:
            _print_saved_subnets(subnets)
        else:
            print_info("No saved subnets. Add the Network ID/CIDR of a reachable LAN, VLAN, or VPN.")
        print_info("Save ranges here, then choose which to scan under Scan Subnet.")
        choice = prompt_choice(["Add a subnet", "Edit a subnet", "Remove a subnet", "Back"],
                               "Select option", allow_back=False)
        if choice is None or choice == 3:
            return
        try:
            if choice == 0:
                entry = prompt("Network ID/CIDR (leave blank to cancel)")
                if not entry:
                    continue
                subnet = config.add(entry)
                print_success(f"Saved subnet: {subnet}")
            else:
                if not subnets:
                    print_info("No saved subnets.")
                    pause()
                    continue
                selected = prompt_choice(subnets, "Select subnet")
                if selected is None:
                    continue
                subnet = subnets[selected]
                if choice == 1:
                    entry = prompt("New Network ID/CIDR (leave blank to cancel)")
                    if not entry:
                        continue
                    print_success(f"Updated subnet: {config.edit(subnet, entry)}")
                elif prompt_yes_no(f"Remove subnet {subnet}?", default=False):
                    if config.remove(subnet):
                        print_success(f"Removed subnet: {subnet}")
                    else:
                        print_warning("That subnet is no longer saved.")
                else:
                    continue
        except (ValueError, OSError) as error:
            print_error(str(error))
        pause()


def _scan_subnets_menu(session: ClientSession) -> None:
    clear_screen()
    print_header("Scan Subnet")
    try:
        subnets = get_subnets_config().get_all()
    except OSError as error:
        print_error(str(error))
        pause()
        return
    if not subnets:
        print_info("No saved subnets. Add a range under Discover Hosts → Saved Subnets first.")
        pause()
        return
    _print_saved_subnets(subnets)
    choice = prompt_choice(["Scan all saved subnets", "Choose subnets to scan", "Back"],
                           "Select option", allow_back=False)
    if choice is None or choice == 2:
        return
    if choice == 0:
        _scan_subnets_and_connect(session, subnets)
        return

    selected: set[str] = set()
    while True:
        clear_screen()
        print_header("Choose Subnets to Scan")
        options = [f"{'[green]✓[/green]' if subnet in selected else '[dim]○[/dim]'} {subnet}" for subnet in subnets]
        options.extend([f"Scan selected subnets ({len(selected)})", "Back"])
        choice = prompt_choice(options, "Select a subnet to toggle, or start scanning", allow_back=False)
        if choice is None or choice == len(subnets) + 1:
            return
        if choice == len(subnets):
            if selected:
                _scan_subnets_and_connect(session, [subnet for subnet in subnets if subnet in selected])
            else:
                print_info("No subnets selected. Nothing scanned.")
                pause()
            return
        subnet = subnets[choice]
        if subnet in selected:
            selected.remove(subnet)
        else:
            selected.add(subnet)


# ---------------------------------------------------------------------------
# Browse a host and attach a device
# ---------------------------------------------------------------------------

def _browse_host(
    session: ClientSession,
    host_ip: str,
    host_name: str,
    port: int = USBIP_PORT,
    *,
    save_if_reachable: bool = False,
) -> None:
    clear_screen()
    print_header(f"Devices on {host_name}")

    console.print(f"\n[dim]  Fetching devices from {host_ip}...[/dim]")

    try:
        classified = session.list_remote_controllers(host_ip, port)
    except UsbipError as e:
        print_error(f"Could not connect to {host_ip}: {e}")
        print_info(f"Ensure the host is running usb|portal on port {port}.")
        pause()
        return

    if save_if_reachable:
        _maybe_save_host(host_ip, port=port)

    if not classified:
        print_warning(f"No shared devices found on {host_name}.")
        print_info("The host may not have any devices bound for export.")
        pause()
        return

    _display_and_attach(session, host_ip, host_name, classified, port)


def _display_and_attach(
    session: ClientSession,
    host_ip: str,
    host_name: str,
    classified: list[tuple[UsbipDevice, bool]],
    port: int = USBIP_PORT,
) -> None:
    table = Table(title=f"Shared Devices on {host_name}", border_style="cyan", show_lines=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("Bus ID", style="cyan")
    table.add_column("Device", style="white")
    table.add_column("VID:PID", style="dim")
    table.add_column("Type", style="yellow")

    for i, (dev, is_ctrl) in enumerate(classified, 1):
        dev_type = "[bold green]Controller[/bold green]" if is_ctrl else "[dim]USB Device[/dim]"
        table.add_row(
            str(i),
            dev.busid,
            f"{dev.vendor_name} {dev.product_name}".strip(),
            dev.vid_pid,
            dev_type,
        )

    console.print(table)

    options = [
        f"{'[bold green][CTRL][/bold green] ' if is_ctrl else ''}{dev.busid} — {dev.vendor_name} {dev.product_name}".strip()
        for dev, is_ctrl in classified
    ]

    choice = prompt_choice(options, "Select device to use")
    if choice is None:
        return

    selected_dev, is_ctrl = classified[choice]
    device_name = f"{selected_dev.vendor_name} {selected_dev.product_name}".strip() or selected_dev.busid
    reconnect = prompt_yes_no("Enable auto-reconnect if connection drops?", default=True)

    try:
        conn = session.connect(
            host_ip=host_ip,
            busid=selected_dev.busid,
            host_name=host_name,
            device_name=device_name,
            remote_port=port,
            vendor_id=selected_dev.vendor_id,
            product_id=selected_dev.product_id,
            reconnect=reconnect,
        )
        print_success(f"Connected: {device_name} from {host_name}")
        print_info(f"Virtual USB port: {conn.port}")
        print_info("The device should appear in /dev/input/ shortly.")

        _offer_auto_connect(host_ip, host_name, selected_dev, device_name)

    except UsbipError as e:
        print_error(f"Failed to attach device: {e}")

    pause()


def _offer_auto_connect(
    host_ip: str,
    host_name: str,
    device: UsbipDevice,
    device_name: str,
) -> None:
    """Ask the user if they want this device auto-connected next time."""
    config = get_config()
    saved = config.get(host_ip)
    if saved and saved.has_auto_connect_device(device.busid):
        return

    console.print()
    if prompt_yes_no(
        f"Auto-connect '{device_name}' from {host_name} on next startup?",
        default=False,
    ):
        config.add_auto_connect_device(
            host_ip,
            AutoConnectDevice(
                busid=device.busid,
                vendor_id=device.vendor_id,
                product_id=device.product_id,
                name=device_name,
            ),
        )
        print_success("Saved. This device will auto-connect on next startup.")
        print_info("Works over VPN — just needs the host IP to be reachable.")


def _maybe_save_host(
    ip: str,
    name: str = "",
    remote: Optional[RemoteHost] = None,
    network: str = "",
    port: int = USBIP_PORT,
) -> None:
    """
    Silently save the host if not already known, or upgrade an existing
    entry with newly discovered hostname/machine_id/network fields.
    """
    config = get_config()
    hostname = remote.hostname if remote else ""
    machine_id = remote.machine_id if remote else ""
    usbip_port = remote.port if remote else port

    existing = (config.get_by_machine_id(machine_id) if machine_id else None) or config.get(ip)

    if not existing:
        config.save_host(SavedHost(
            ip=ip,
            name=name,
            hostname=hostname,
            machine_id=machine_id,
            network=network,
            port=usbip_port,
        ))
    else:
        updated = False
        if not existing.hostname and hostname:
            existing.hostname = hostname
            updated = True
        if not existing.machine_id and machine_id:
            existing.machine_id = machine_id
            updated = True
        if not existing.network and network:
            existing.network = network
            updated = True
        if existing.port != usbip_port:
            existing.port = usbip_port
            updated = True
        if existing.ip != ip:
            existing.ip = ip
            updated = True
        if updated:
            config.save_host(existing)


# ---------------------------------------------------------------------------
# Saved Hosts management menu
# ---------------------------------------------------------------------------

def _saved_hosts_menu(session: ClientSession) -> None:
    while True:
        clear_screen()
        print_header("Saved Hosts")

        hosts = get_config().get_all()

        if hosts:
            _print_saved_hosts_table(hosts)
        else:
            print_info("No saved hosts yet.")
            print_info("Hosts are saved automatically when you connect to them.")
            console.print()

        options = [
            "Connect to a saved host",
            "Edit / rename a host",
            "Remove a host",
            "Back",
        ]
        choice = prompt_choice(options, "Select option", allow_back=False)

        if choice is None or choice == 3:
            break
        elif choice == 0:
            _connect_to_saved_host(session, hosts)
        elif choice == 1:
            _edit_saved_host(hosts)
        elif choice == 2:
            _remove_saved_host(hosts)


def _print_saved_hosts_table(hosts: list[SavedHost]) -> None:
    table = Table(title="Saved Hosts", border_style="cyan", show_lines=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("Display Name", style="white")
    table.add_column("Hostname", style="yellow")
    table.add_column("IP", style="cyan")
    table.add_column("Port", style="cyan")
    table.add_column("Network", style="dim")
    table.add_column("Auto-Connect", style="green")

    for i, host in enumerate(hosts, 1):
        ac_list = ", ".join(str(d) for d in host.auto_connect) if host.auto_connect else "[dim]none[/dim]"
        table.add_row(
            str(i),
            host.name or "[dim]—[/dim]",
            host.hostname or "[dim]—[/dim]",
            host.ip,
            str(host.port),
            host.network or "[dim]—[/dim]",
            ac_list,
        )
    console.print(table)


def _connect_to_saved_host(session: ClientSession, hosts: list[SavedHost]) -> None:
    if not hosts:
        print_info("No saved hosts.")
        pause()
        return

    options = [f"{h.display_name} [{h.ip}]" for h in hosts]
    choice = prompt_choice(options, "Select host to connect to")
    if choice is None:
        return

    host = hosts[choice]
    _browse_host(session, host.ip, host.display_name, host.port)


def _edit_saved_host(hosts: list[SavedHost]) -> None:
    if not hosts:
        return

    options = [f"{h.display_name} [{h.ip}]" for h in hosts]
    choice = prompt_choice(options, "Select host to edit")
    if choice is None:
        return

    host = hosts[choice]
    clear_screen()
    print_header(f"Edit: {host.display_name}")

    new_name = prompt("New name", default=host.name)
    new_network = prompt("Network/CIDR for VPN auto-discovery", default=host.network)
    port_str = prompt("Port", default=str(host.port))
    try:
        new_port = int(port_str)
        if not 1 <= new_port <= 65535:
            raise ValueError("Port outside valid range")
    except ValueError:
        new_port = host.port

    config = get_config()
    host.name = new_name
    host.network = new_network
    host.port = new_port

    if host.auto_connect:
        console.print("\n[dim]  Auto-connect devices:[/dim]")
        for d in host.auto_connect:
            console.print(f"  [cyan]•[/cyan] {d}")
        if prompt_yes_no("Remove any auto-connect devices?", default=False):
            ac_options = [str(d) for d in host.auto_connect]
            ac_choice = prompt_choice(ac_options, "Select device to remove")
            if ac_choice is not None:
                removed = host.auto_connect[ac_choice]
                host.auto_connect.pop(ac_choice)

    if config.save_host(host):
        print_success(f"Updated: {host.display_name}")
    else:
        print_error("Could not save host settings.")
    pause()


def _remove_saved_host(hosts: list[SavedHost]) -> None:
    if not hosts:
        return

    options = [f"{h.display_name} [{h.ip}]" for h in hosts]
    choice = prompt_choice(options, "Select host to remove")
    if choice is None:
        return

    host = hosts[choice]
    if not prompt_yes_no(f"Remove '{host.display_name}'? This also removes its auto-connect devices.", default=False):
        return

    get_config().remove_host(host.ip)
    print_success(f"Removed: {host.display_name}")
    pause()


# ---------------------------------------------------------------------------
# Auto-connect settings
# ---------------------------------------------------------------------------

def _auto_connect_settings() -> None:
    """View and manage all auto-connect devices across all saved hosts."""
    while True:
        clear_screen()
        print_header("Auto-Connect Settings  [dim](Use)[/dim]")

        config = get_config()
        hosts = config.get_all()
        hosts_with_ac = [h for h in hosts if h.auto_connect]

        if hosts_with_ac:
            table = Table(title="Auto-Connect Devices", border_style="cyan", show_lines=True)
            table.add_column("#", style="dim", width=4)
            table.add_column("Host", style="yellow")
            table.add_column("Device", style="white")
            table.add_column("VID:PID", style="dim")
            idx = 1
            entries = []
            for host in hosts_with_ac:
                for dev in host.auto_connect:
                    table.add_row(str(idx), host.display_name, dev.name or dev.busid, dev.vid_pid)
                    entries.append((host.ip, dev))
                    idx += 1
            console.print(table)
            console.print()
            print_info("These devices will auto-connect each time the service starts or the host becomes reachable.")
        else:
            print_info("No auto-connect devices configured.")
            console.print()
            entries = []

        options = [
            "Remove an auto-connect device",
            "Clear all auto-connect entries",
            "Back",
        ]
        choice = prompt_choice(options, "Select option", allow_back=False)

        if choice is None or choice == 2:
            break
        elif choice == 0:
            if not entries:
                print_info("No auto-connect devices to remove.")
                pause()
                continue
            opts = [f"{config.get(ip).display_name if config.get(ip) else ip} — {dev.name or dev.busid}" for ip, dev in entries]
            sel = prompt_choice(opts, "Select device to remove")
            if sel is None:
                continue
            host_ip, dev = entries[sel]
            config.remove_auto_connect_device(host_ip, dev.busid)
            print_success(f"Removed '{dev.name or dev.busid}' from auto-connect. It will not reconnect automatically.")
            pause()
        elif choice == 1:
            if not entries:
                print_info("Nothing to clear.")
                pause()
                continue
            if prompt_yes_no("Clear ALL auto-connect entries? Devices will not reconnect after reboot.", default=False):
                for host in hosts_with_ac:
                    updated = host
                    updated.auto_connect = []
                    config.save_host(updated)
                print_success("All auto-connect entries cleared.")
                pause()


# ---------------------------------------------------------------------------
# Disconnect
# ---------------------------------------------------------------------------

def _disconnect_device(session: ClientSession) -> None:
    clear_screen()
    print_header("Disconnect a Device")

    connections = session.get_connections()
    if not connections:
        print_info("No active connections to disconnect.")
        pause()
        return

    options = [
        f"Port {c.port}: [cyan]{c.device_name}[/cyan] from [yellow]{c.host_name}[/yellow]  [dim](connected {c.uptime_str()})[/dim]"
        for c in connections
    ]

    choice = prompt_choice(options, "Select connection to close")
    if choice is None:
        return

    conn = connections[choice]
    if not prompt_yes_no(f"Disconnect '{conn.device_name}' from {conn.host_name}?"):
        return

    also_remove_ac = False
    config = get_config()
    saved = config.get(conn.host_ip)
    if saved and saved.has_auto_connect_device(conn.busid):
        also_remove_ac = prompt_yes_no(
            "Also remove from auto-connect list?", default=False
        )

    try:
        if also_remove_ac:
            if not config.remove_auto_connect_device(conn.host_ip, conn.busid):
                print_error("Could not save auto-connect removal; connection left active.")
                pause()
                return
            print_info("Removed from auto-connect list.")
        session.disconnect(conn.port)
        print_success(f"Disconnected: {conn.device_name}")
        if saved and not also_remove_ac and saved.has_auto_connect_device(conn.busid):
            print_info("Auto-connect remains enabled; the service will reconnect this device.")
    except UsbipError as e:
        print_error(f"Failed to disconnect: {e}")

    pause()
