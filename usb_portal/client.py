"""
Client mode: connect to a remote host and use their game controllers.
Manages module loading, device attachment, auto-reconnect, and
auto-connect to saved hosts (including VPN hosts) on startup.
"""

import copy
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from usb_portal import usbip
from usb_portal.config import RECONNECT_INTERVAL, RECONNECT_MAX_ATTEMPTS, USBIP_PORT
from usb_portal.devices import classify_usbip_devices
from usb_portal.discovery import CidrScanCancelled, DiscoveryScanner, RemoteHost, parse_scan_network, scan_cidr
from usb_portal.routed_discovery import RoutedDiscovery
from usb_portal.usbip import AttachedPort, UsbipDevice, UsbipError

log = logging.getLogger("usb-portal-daemon")


@dataclass
class ActiveConnection:
    host_ip: str
    host_name: str
    busid: str
    port: int
    device_name: str
    remote_port: int = USBIP_PORT
    vendor_id: str = ""
    product_id: str = ""
    connected_at: float = field(default_factory=time.time)
    reconnect_enabled: bool = True
    _reconnect_thread: Optional[threading.Thread] = field(default=None, repr=False, compare=False)
    _alive: bool = field(default=True, repr=False, compare=False)
    _attempts: int = field(default=0, repr=False, compare=False)
    _saved_intent: bool = field(default=False, repr=False, compare=False)

    def uptime_str(self) -> str:
        elapsed = int(time.time() - self.connected_at)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}h {m}m"
        if m:
            return f"{m}m {s}s"
        return f"{s}s"

    def is_alive(self) -> bool:
        """Check if the virtual USB port is still active."""
        try:
            ports = usbip.list_attached_ports()
            return any(self.matches(p) for p in ports)
        except Exception:
            return False

    def matches(self, port: AttachedPort) -> bool:
        return (
            port.port == self.port and port.remote_host == self.host_ip
            and port.busid == self.busid
            and (port.remote_port is None or port.remote_port == self.remote_port)
            and _compatible_connection_identity(self, port.vendor_id, port.product_id)
        )


class ClientSession:
    """
    Manages client-side USB/IP operations:
    - Loads kernel modules
    - Discovers remote hosts (LAN broadcast)
    - Auto-connects to saved hosts (including VPN hosts) on startup
    - Attaches/detaches remote devices
    - Monitors connections and auto-reconnects on drop
    """

    def __init__(self):
        self._connections: dict[int, ActiveConnection] = {}
        self._lock = threading.Lock()
        self._connect_lock = threading.RLock()
        self._stop = threading.Event()
        self._routed_discovery = RoutedDiscovery(self._stop)
        self._scanner = DiscoveryScanner()
        self._monitor_thread: Optional[threading.Thread] = None
        self._auto_connect_thread: Optional[threading.Thread] = None
        self._running = False

    def setup(self) -> None:
        """Load required kernel modules for client mode."""
        usbip.load_client_modules()

    def start_monitoring(self) -> None:
        """Start background threads: connection monitor + LAN discovery listener."""
        if self._running:
            return
        if (self._routed_discovery.is_alive()
                or any(t and t.is_alive() for t in (self._monitor_thread, self._auto_connect_thread))):
            raise UsbipError("Previous connection worker is still stopping; retry shortly")
        self._running = True
        self._stop.clear()
        self._discover_existing_connections()
        self._scanner.start_listening()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, daemon=True, name="conn-monitor"
        )
        self._monitor_thread.start()

    def _discover_existing_connections(self) -> None:
        """
        Scan existing kernel vhci ports and register them in _connections.
        This handles daemon restarts where kernel connections survive.
        Also updates hosts.json if a device is now at a different busid (port change).
        """
        from usb_portal.saved_hosts import get_config
        try:
            attached = usbip.list_attached_ports()
        except Exception:
            return

        if not attached:
            return

        config = get_config()
        saved_matches = {}
        for ip in {ap.remote_host for ap in attached}:
            saved = config.get(ip)
            if not saved:
                continue
            remote_devices = [UsbipDevice(
                ap.busid, ap.vendor_id, ap.product_id, ap.vendor_name, ap.product_name,
            ) for ap in attached if ap.remote_host == ip]
            saved_matches[ip] = {
                device.busid: ac for ac, device in _resolve_auto_connect_devices(
                    saved.auto_connect, remote_devices, set(),
                )
            }
        for ap in attached:
            if ap.port in self._connections:
                continue
            saved_host = config.get(ap.remote_host) if ap.remote_host else None
            host_name = saved_host.display_name if saved_host else (ap.remote_host or "")
            device_name = f"{ap.vendor_name} {ap.product_name}".strip() or ap.busid
            ac = saved_matches.get(ap.remote_host, {}).get(ap.busid)

            if saved_host:
                if ac and ac.busid != ap.busid:
                    log.info(
                        "Startup: device [%s:%s] is at %s (was %s). Updating hosts.json.",
                        ap.vendor_id, ap.product_id, ap.busid, ac.busid,
                    )
                    self._update_saved_busid(
                        ap.remote_host,
                        ac.busid,
                        ap.busid,
                        ap.vendor_id,
                        ap.product_id,
                    )
                if ac:
                    device_name = ac.name or device_name

            conn = ActiveConnection(
                host_ip=ap.remote_host,
                host_name=host_name,
                busid=ap.busid,
                port=ap.port,
                device_name=device_name,
                remote_port=ap.remote_port or (saved_host.port if saved_host else USBIP_PORT),
                vendor_id=ap.vendor_id,
                product_id=ap.product_id,
                reconnect_enabled=ac is not None,
                _saved_intent=ac is not None,
            )
            with self._lock:
                self._connections[ap.port] = conn
            log.info(
                "Discovered existing connection: port %d → %s/%s (%s).",
                ap.port, ap.remote_host, ap.busid, device_name,
            )

    def start_auto_connect(self) -> None:
        """
        Attempt to auto-connect to all saved hosts that have auto-connect devices.
        Runs in a background thread so it doesn't block the CLI from appearing.
        Will keep retrying hosts that aren't reachable yet (e.g. VPN not up yet).
        """
        if self._auto_connect_thread and self._auto_connect_thread.is_alive():
            return
        if not self._running:
            self.start_monitoring()
        self._auto_connect_thread = threading.Thread(
            target=self._auto_connect_loop, daemon=True, name="auto-connect"
        )
        self._auto_connect_thread.start()

    def stop_monitoring(self) -> None:
        self._running = False
        self._stop.set()
        self._routed_discovery.join()
        self._scanner.stop_listening()
        current_thread = threading.current_thread()
        if self._monitor_thread and self._monitor_thread is not current_thread:
            self._monitor_thread.join(timeout=20)
            if not self._monitor_thread.is_alive():
                self._monitor_thread = None
        if self._auto_connect_thread and self._auto_connect_thread is not current_thread:
            self._auto_connect_thread.join(timeout=20)
            if not self._auto_connect_thread.is_alive():
                self._auto_connect_thread = None

    # ------------------------------------------------------------------
    # Connection monitor + auto-reconnect
    # ------------------------------------------------------------------

    def _monitor_loop(self) -> None:
        while self._running:
            try:
                self._check_connections()
            except Exception:
                log.exception("Connection monitor failed; retrying")
            if self._stop.wait(RECONNECT_INTERVAL):
                return

    def _check_connections(self) -> None:
        from usb_portal.saved_hosts import get_config
        ports = usbip.list_attached_ports(strict=True)
        saved_hosts = {h.ip: h for h in get_config().get_all()}
        with self._lock:
            for conn in self._connections.values():
                if conn._saved_intent:
                    saved = saved_hosts.get(conn.host_ip)
                    if not saved or not _find_saved_auto_connect_device(
                        saved.auto_connect, conn.busid, conn.vendor_id, conn.product_id,
                    ):
                        conn.reconnect_enabled = False
            dead = [
                conn for conn in self._connections.values()
                if not any(conn.matches(p) for p in ports) and conn.reconnect_enabled
            ]

        for conn in dead:
            saved = saved_hosts.get(conn.host_ip)
            if saved and _find_saved_auto_connect_device(
                saved.auto_connect, conn.busid, conn.vendor_id, conn.product_id,
            ):
                # The saved-host loop resolves all identical devices together.
                self._drop_missing_remote_connection(conn)
            else:
                self._attempt_reconnect(conn)

    def _attempt_reconnect(self, conn: ActiveConnection, max_attempts: int = RECONNECT_MAX_ATTEMPTS) -> None:
        """Make one bounded attempt; never hold up recovery of the other devices."""
        from usb_portal.saved_hosts import get_config
        if not self._running or not conn.reconnect_enabled:
            return
        conn._attempts += 1
        if conn._attempts > max_attempts:
            conn.reconnect_enabled = False
            return
        try:
            target_busid = self._resolve_remote_busid(conn)
            if target_busid is None:
                self._drop_missing_remote_connection(conn)
                return
            with self._connect_lock:
                with self._lock:
                    if (not self._running or not conn.reconnect_enabled
                            or self._connections.get(conn.port) is not conn):
                        return
                # Keep the original record visible throughout the blocking attach,
                # so disconnect can cancel it even if the kernel port changes.
                old_port, old_busid = conn.port, conn.busid
                new_port = self._attach_remote(conn.host_ip, target_busid, conn.remote_port)
                with self._lock:
                    cancelled = (not self._running or not conn.reconnect_enabled
                                 or self._connections.get(old_port) is not conn)
                    if not cancelled:
                        self._connections.pop(old_port)
                        conn.port, conn.busid = new_port, target_busid
                        conn.connected_at = time.time()
                        conn._attempts = 0
                        self._connections[new_port] = conn
                if cancelled:
                    target = next((p for p in usbip.list_attached_ports(strict=True)
                                   if p.port == new_port and p.remote_host == conn.host_ip
                                   and p.busid == target_busid), None)
                    if target:
                        usbip.detach(new_port, expected=target)
                    return
                if target_busid != old_busid:
                    self._update_saved_busid(
                        conn.host_ip, old_busid, target_busid, conn.vendor_id, conn.product_id,
                    )
        except UsbipError as error:
            log.debug("Reconnect failed: %s", error)
            saved = get_config().get(conn.host_ip)
            remote = self._resolve_host_via_cidr(saved)
            if remote and conn.reconnect_enabled and self._running:
                self._update_saved_host_from_remote(saved, remote)
                conn.host_ip, conn.remote_port = remote.ip, remote.port

    def _drop_missing_remote_connection(self, conn: ActiveConnection) -> None:
        """
        Forget a dead connection when the host is reachable but the remote device
        is no longer exported. The saved auto-connect entry remains, so the normal
        auto-connect loop will attach it again when it reappears.
        """
        with self._lock:
            if self._connections.get(conn.port) is conn:
                self._connections.pop(conn.port)
            conn.reconnect_enabled = False
        log.info(
            "Remote device %s/%s is not currently shared; removed stale local connection.",
            conn.host_ip,
            conn.busid,
        )

    def _resolve_remote_busid(self, conn: ActiveConnection) -> Optional[str]:
        """
        Try to find the current busid for a device on the remote host.
        First tries the saved busid; if not available, falls back to VID:PID matching
        against the list of exportable devices. Returns None if not found.
        """
        from usb_portal.saved_hosts import get_config
        try:
            remote_devices = usbip.list_remote(conn.host_ip, conn.remote_port)
        except UsbipError:
            return conn.busid

        remote_by_busid = {d.busid: d for d in remote_devices}
        remote = remote_by_busid.get(conn.busid)
        if remote and _same_connection_identity(conn, remote):
            return conn.busid

        saved = get_config().get(conn.host_ip)
        if saved:
            ac = _find_saved_auto_connect_device(saved.auto_connect, conn.busid, conn.vendor_id, conn.product_id)
            if ac and ac.vendor_id and ac.product_id:
                with self._lock:
                    claimed = {c.busid for c in self._connections.values()
                               if c is not conn and c.host_ip == conn.host_ip}
                available = [d for d in remote_devices if d.busid not in claimed
                             and d.vendor_id == ac.vendor_id and d.product_id == ac.product_id]
                if len(available) == 1:
                    return available[0].busid

        return None

    def _update_saved_busid(
        self,
        host_ip: str,
        old_busid: str,
        new_busid: str,
        vendor_id: str = "",
        product_id: str = "",
    ) -> None:
        """Update hosts.json when a device moves to a different busid."""
        from usb_portal.saved_hosts import get_config
        try:
            config = get_config()
            saved = config.get(host_ip)
            if not saved:
                return
            updated_host = copy.deepcopy(saved)
            dev = _find_saved_auto_connect_device(
                updated_host.auto_connect,
                old_busid,
                vendor_id,
                product_id,
            )
            if dev:
                dev.busid = new_busid
                if config.save_host(updated_host):
                    live_dev = _find_saved_auto_connect_device(
                        saved.auto_connect,
                        old_busid,
                        vendor_id,
                        product_id,
                    )
                    if live_dev:
                        live_dev.busid = new_busid
                    log.info("Updated hosts.json: %s/%s → %s", host_ip, old_busid, new_busid)
                else:
                    log.warning("Could not update saved busid: hosts.json write failed.")
        except Exception as e:
            log.warning("Could not update saved busid: %s", e)

    def _resolve_host_via_cidr(self, saved_host) -> Optional[RemoteHost]:
        """
        Discover the saved host's current address without waiting on a large scan.
        Matches by machine_id (preferred) or hostname (fallback).
        Returns the discovered RemoteHost if found, or None.
        """
        if saved_host is None or not saved_host.network:
            return None
        log.info(
            "Scanning %s for host %s...",
            saved_host.network,
            saved_host.hostname or saved_host.machine_id or saved_host.ip,
        )
        try:
            network = parse_scan_network(saved_host.network)
            if network.num_addresses > 1024:
                found = self._routed_discovery.request(str(network))
            else:
                found = scan_cidr(str(network), cancel_event=self._stop)
        except CidrScanCancelled:
            return None
        except Exception as e:
            log.warning("CIDR scan failed: %s", e)
            return None

        for host in found:
            if saved_host.machine_id and host.machine_id == saved_host.machine_id:
                return host

        for host in found:
            if (
                host.ip == saved_host.ip
                and (
                    not saved_host.machine_id
                    or not host.machine_id
                    or host.machine_id == saved_host.machine_id
                )
            ):
                return host

        # A partial enterprise scan cannot establish hostname uniqueness.
        if network.num_addresses <= 1024 and not saved_host.machine_id and saved_host.hostname:
            hostname_matches = [host for host in found if host.hostname == saved_host.hostname]
            if len(hostname_matches) == 1:
                return hostname_matches[0]

        return None

    def _resolve_host_ip_via_cidr(self, saved_host) -> Optional[str]:
        """Compatibility wrapper for tests/callers that only need the refreshed IP."""
        remote = self._resolve_host_via_cidr(saved_host)
        return remote.ip if remote else None

    def _update_saved_host_from_remote(self, saved_host, remote: RemoteHost) -> None:
        """Refresh saved host identity/address fields from a discovery announcement."""
        from usb_portal.saved_hosts import get_config
        try:
            updated = copy.deepcopy(saved_host)
            changed = False
            if updated.ip != remote.ip:
                updated.ip = remote.ip
                changed = True
            if remote.hostname and updated.hostname != remote.hostname:
                updated.hostname = remote.hostname
                changed = True
            if remote.machine_id and updated.machine_id != remote.machine_id:
                updated.machine_id = remote.machine_id
                changed = True
            if updated.port != remote.port:
                updated.port = remote.port
                changed = True
            if changed and get_config().save_host(updated):
                saved_host.ip = updated.ip
                saved_host.hostname = updated.hostname
                saved_host.machine_id = updated.machine_id
                saved_host.port = updated.port
                log.info(
                    "Updated hosts.json: %s now at %s:%d.",
                    saved_host.display_name, saved_host.ip, saved_host.port,
                )
            elif changed:
                log.warning("Could not update saved host from discovery: hosts.json write failed.")
        except Exception as e:
            log.warning("Could not update saved host from discovery: %s", e)

    def _update_saved_ip(self, saved_host, new_ip: str) -> None:
        """Update the cached IP for a saved host and persist it."""
        from usb_portal.saved_hosts import get_config
        try:
            config = get_config()
            updated = copy.deepcopy(saved_host)
            updated.ip = new_ip
            if config.save_host(updated):
                saved_host.ip = new_ip
                log.info("Updated hosts.json: %s new IP → %s", saved_host.display_name, new_ip)
            else:
                log.warning("Could not update saved IP: hosts.json write failed.")
        except Exception as e:
            log.warning("Could not update saved IP: %s", e)

    # ------------------------------------------------------------------
    # Auto-connect to saved hosts
    # ------------------------------------------------------------------

    def _auto_connect_loop(self) -> None:
        """
        Continuously attempt auto-connect for every saved host that has
        auto-connect devices configured. Runs indefinitely so that hosts
        that come back online after a reboot are automatically connected.
        """
        from usb_portal.saved_hosts import get_config

        if self._stop.wait(1):
            return

        while self._running:
            try:
                hosts = [h for h in get_config().get_all() if h.auto_connect]
            except Exception:
                log.exception("Could not read auto-connect settings; retrying")
                self._stop.wait(RECONNECT_INTERVAL)
                continue

            for saved_host in hosts:
                if not self._running:
                    return
                try:
                    self._auto_connect_host(saved_host)
                except Exception:
                    log.exception("Auto-connect to %s failed; retrying", saved_host.ip)

            if self._stop.wait(RECONNECT_INTERVAL):
                return

    def _auto_connect_host(self, saved_host) -> None:
        """Try to connect to a single saved host and attach its auto-connect devices."""
        already_connected = self._connected_busids_for(saved_host.ip)

        try:
            remote_devices = usbip.list_remote(saved_host.ip, saved_host.port)
        except UsbipError:
            remote = self._resolve_host_via_cidr(saved_host)
            if not remote:
                return
            log.info(
                "Host %s found at %s:%d (was %s:%d). Updating.",
                saved_host.display_name, remote.ip, remote.port, saved_host.ip, saved_host.port,
            )
            self._update_saved_host_from_remote(saved_host, remote)
            already_connected = self._connected_busids_for(saved_host.ip)
            try:
                remote_devices = usbip.list_remote(saved_host.ip, saved_host.port)
            except UsbipError:
                return

        assignments = _resolve_auto_connect_devices(
            saved_host.auto_connect,
            remote_devices,
            already_connected,
        )

        for ac_device, remote in assignments:
            if not self._running:
                return

            try:
                device_name = _preferred_device_name(ac_device.name, remote)
                log.info("Auto-connecting to %s/%s (%s)...", saved_host.ip, remote.busid, device_name)
                connection = self.connect(
                    host_ip=saved_host.ip,
                    busid=remote.busid,
                    host_name=saved_host.display_name,
                    device_name=device_name,
                    remote_port=saved_host.port,
                    vendor_id=remote.vendor_id,
                    product_id=remote.product_id,
                    reconnect=True,
                )
                connection._saved_intent = True
                already_connected.add(remote.busid)
                self._update_saved_auto_connect_metadata(
                    saved_host,
                    ac_device,
                    remote,
                    device_name,
                )
                log.info("Auto-connected: %s/%s (%s).", saved_host.ip, remote.busid, device_name)
            except UsbipError as e:
                log.warning("Auto-connect to %s/%s failed: %s", saved_host.ip, remote.busid, e)

    def _update_saved_auto_connect_metadata(
        self,
        saved_host,
        ac_device,
        remote: UsbipDevice,
        device_name: str,
    ) -> None:
        """Persist busid/name refreshes for an auto-connected remote device."""
        from usb_portal.saved_hosts import get_config
        updated_host = copy.deepcopy(saved_host)
        updated_device = _find_saved_auto_connect_device(
            updated_host.auto_connect,
            ac_device.busid,
            ac_device.vendor_id,
            ac_device.product_id,
        )
        if updated_device is None:
            return

        changed = False
        old_busid = ac_device.busid

        if remote.busid != updated_device.busid:
            updated_device.busid = remote.busid
            changed = True
        if device_name and device_name != updated_device.name:
            updated_device.name = device_name
            changed = True

        if not changed:
            return

        try:
            if get_config().save_host(updated_host):
                ac_device.busid = updated_device.busid
                ac_device.name = updated_device.name
                if old_busid != remote.busid:
                    log.info("Updated hosts.json: %s/%s → %s", saved_host.ip, old_busid, remote.busid)
            else:
                log.warning("Could not update saved auto-connect metadata: hosts.json write failed.")
        except Exception as e:
            log.warning("Could not update saved auto-connect metadata: %s", e)

    def _connected_busids_for(self, host_ip: str) -> set[str]:
        ports = usbip.list_attached_ports(strict=True)
        with self._lock:
            return {c.busid for c in self._connections.values()
                    if c.host_ip == host_ip and any(c.matches(p) for p in ports)}

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_hosts(self, timeout: float = 3.0) -> list[RemoteHost]:
        """Actively scan for usb|portal hosts on the LAN."""
        return self._scanner.scan_once(timeout=timeout)

    def get_known_hosts(self) -> list[RemoteHost]:
        """Return previously discovered hosts."""
        return self._scanner.hosts

    # ------------------------------------------------------------------
    # Device listing
    # ------------------------------------------------------------------

    def list_remote_devices(self, host_ip: str, port: int = USBIP_PORT) -> list[UsbipDevice]:
        """List USB devices available on a remote host."""
        return usbip.list_remote(host_ip, port)

    def list_remote_controllers(self, host_ip: str, port: int = USBIP_PORT) -> list[tuple[UsbipDevice, bool]]:
        """
        List remote devices, classified as game controller or not.
        Returns list of (device, is_controller) tuples.
        """
        devices = self.list_remote_devices(host_ip, port)
        return classify_usbip_devices(devices)

    # ------------------------------------------------------------------
    # Connect / disconnect
    # ------------------------------------------------------------------

    def connect(
        self,
        host_ip: str,
        busid: str,
        host_name: str = "",
        device_name: str = "",
        remote_port: int = USBIP_PORT,
        vendor_id: str = "",
        product_id: str = "",
        reconnect: bool = True,
    ) -> ActiveConnection:
        """Attach a remote USB device to this machine."""
        with self._connect_lock:
            existing = self._find_live_connection(host_ip, busid, remote_port, vendor_id, product_id)
            if existing:
                existing.reconnect_enabled = existing.reconnect_enabled or reconnect
                return existing

            adopted = self._adopt_kernel_connection(
                host_ip=host_ip,
                busid=busid,
                host_name=host_name,
                device_name=device_name,
                remote_port=remote_port,
                vendor_id=vendor_id,
                product_id=product_id,
                reconnect=reconnect,
            )
            if adopted:
                return adopted

            port = self._attach_remote(host_ip, busid, remote_port)

            conn = ActiveConnection(
                host_ip=host_ip,
                host_name=host_name or host_ip,
                busid=busid,
                port=port,
                device_name=device_name or busid,
                remote_port=remote_port,
                vendor_id=vendor_id,
                product_id=product_id,
                reconnect_enabled=reconnect,
            )
            with self._lock:
                self._connections[port] = conn

        return conn

    def _attach_remote(self, host_ip: str, busid: str, remote_port: int) -> int:
        """Attach a remote device and require a confirmed virtual port."""
        port = usbip.attach(host_ip, busid, remote_port)
        if port < 0:
            raise UsbipError(
                f"Attached {busid} from {host_ip}, but could not confirm the virtual USB port"
            )
        return port

    def _find_live_connection(
        self,
        host_ip: str,
        busid: str,
        remote_port: int,
        vendor_id: str = "",
        product_id: str = "",
    ) -> Optional[ActiveConnection]:
        """Return an existing live connection for the same remote device, pruning stale matches."""
        with self._lock:
            candidates = [
                conn for conn in self._connections.values()
                if conn.host_ip == host_ip
                and conn.busid == busid
                and conn.remote_port == remote_port
                and _compatible_connection_identity(conn, vendor_id, product_id)
            ]

        for conn in candidates:
            if conn.is_alive():
                return conn
            with self._lock:
                self._connections.pop(conn.port, None)
        return None

    def _adopt_kernel_connection(
        self,
        host_ip: str,
        busid: str,
        host_name: str = "",
        device_name: str = "",
        remote_port: int = USBIP_PORT,
        vendor_id: str = "",
        product_id: str = "",
        reconnect: bool = True,
    ) -> Optional[ActiveConnection]:
        """Register an already-attached kernel port for this remote device."""
        try:
            ports = usbip.list_attached_ports()
        except Exception:
            return None

        for ap in ports:
            if ap.remote_host != host_ip or ap.busid != busid:
                continue
            if ap.remote_port is not None and ap.remote_port != remote_port:
                continue
            if vendor_id and product_id and ap.vendor_id and ap.product_id:
                if ap.vendor_id != vendor_id or ap.product_id != product_id:
                    continue

            conn = ActiveConnection(
                host_ip=host_ip,
                host_name=host_name or host_ip,
                busid=busid,
                port=ap.port,
                device_name=device_name or f"{ap.vendor_name} {ap.product_name}".strip() or busid,
                remote_port=remote_port,
                vendor_id=vendor_id or ap.vendor_id,
                product_id=product_id or ap.product_id,
                reconnect_enabled=reconnect,
            )
            with self._lock:
                self._connections[ap.port] = conn
            log.info("Adopted existing kernel connection: port %d → %s/%s.", ap.port, host_ip, busid)
            return conn

        return None

    def disconnect(self, port: int) -> None:
        """Detach a virtual USB port."""
        with self._lock:
            conn = self._connections.get(port)
            if conn:
                conn.reconnect_enabled = False
        with self._connect_lock:
            attached = usbip.list_attached_ports(strict=True)
            target = next((p for p in attached if p.port == port and (conn is None or conn.matches(p))), None)
            if target:
                usbip.detach(port, expected=target)
            with self._lock:
                if self._connections.get(port) is conn:
                    self._connections.pop(port, None)

    def disconnect_all(self) -> None:
        """Detach all active connections."""
        with self._lock:
            ports = list(self._connections.keys())

        for port in ports:
            try:
                self.disconnect(port)
            except Exception:
                pass

    def get_connections(self) -> list[ActiveConnection]:
        with self._lock:
            return list(self._connections.values())

    def sync_connections(self) -> None:
        """Sync connection list with actually attached ports in the kernel."""
        try:
            attached = {p.port: p for p in usbip.list_attached_ports()}
        except Exception:
            return

        with self._lock:
            for port in list(self._connections.keys()):
                if not conn_is_present(self._connections[port], attached) and not self._connections[port].reconnect_enabled:
                    del self._connections[port]

    def shutdown(self) -> None:
        """Disconnect all and stop monitoring."""
        self.stop_monitoring()
        self.disconnect_all()


_session: Optional[ClientSession] = None


def conn_is_present(conn: ActiveConnection, attached: dict[int, AttachedPort]) -> bool:
    return conn.port in attached and conn.matches(attached[conn.port])


def get_session() -> ClientSession:
    global _session
    if _session is None:
        _session = ClientSession()
    return _session


def _resolve_auto_connect_devices(
    auto_connect_devices,
    remote_devices: list[UsbipDevice],
    already_connected: set[str],
):
    """
    Match saved auto-connect entries to remote devices as a batch.

    Exact busid+identity matches win first. VID:PID fallback is used when it is
    unambiguous, and duplicate saved identities claim duplicate remote devices
    in stable busid order. This supports two identical, serial-less controllers
    without collapsing them into one auto-connect entry.
    """
    remote_by_busid = {d.busid: d for d in remote_devices}
    claimed = set(already_connected)
    assignments = []
    pending_by_identity: dict[tuple[str, str], list] = {}

    for ac_device in auto_connect_devices:
        remote = remote_by_busid.get(ac_device.busid)
        if (remote is not None and remote.busid in already_connected
                and _same_auto_connect_identity(ac_device, remote)):
            continue
        if (
            remote is not None
            and remote.busid not in claimed
            and _same_auto_connect_identity(ac_device, remote)
        ):
            assignments.append((ac_device, remote))
            claimed.add(remote.busid)
            continue

        if ac_device.vendor_id and ac_device.product_id:
            identity = (ac_device.vendor_id, ac_device.product_id)
            pending_by_identity.setdefault(identity, []).append(ac_device)

    for identity, devices in pending_by_identity.items():
        available = [
            d for d in sorted(remote_devices, key=lambda d: d.busid)
            if (d.vendor_id, d.product_id) == identity and d.busid not in claimed
        ]
        if not available:
            continue
        if len(devices) == 1 and len(available) > 1:
            log.warning(
                "Auto-connect device [%s:%s] matched multiple remote busids (%s); keeping saved busid %s.",
                identity[0],
                identity[1],
                ", ".join(d.busid for d in available),
                devices[0].busid,
            )
            continue

        for ac_device, remote in zip(devices, available):
            assignments.append((ac_device, remote))
            claimed.add(remote.busid)

    return assignments


def _same_auto_connect_identity(ac_device, remote: UsbipDevice) -> bool:
    if not ac_device.vendor_id or not ac_device.product_id:
        return True
    return (
        ac_device.vendor_id == remote.vendor_id
        and ac_device.product_id == remote.product_id
    )


def _same_connection_identity(conn: ActiveConnection, remote: UsbipDevice) -> bool:
    if not conn.vendor_id or not conn.product_id:
        return True
    return conn.vendor_id == remote.vendor_id and conn.product_id == remote.product_id


def _compatible_connection_identity(
    conn: ActiveConnection,
    vendor_id: str = "",
    product_id: str = "",
) -> bool:
    if not conn.vendor_id or not conn.product_id:
        return True
    if not vendor_id or not product_id:
        return True
    return conn.vendor_id == vendor_id and conn.product_id == product_id


def _remote_device_name(remote: UsbipDevice) -> str:
    return f"{remote.vendor_name} {remote.product_name}".strip()


def _is_generic_device_name(name: str) -> bool:
    normalised = " ".join((name or "").strip().lower().split())
    if not normalised:
        return True
    if normalised in {
        "usb device",
        "unknown device",
        "unknown usb device",
        "usb gamepad",
        "gamepad",
    }:
        return True
    return normalised.startswith("usb device [") or "unknown product" in normalised


def _preferred_device_name(saved_name: str, remote: UsbipDevice) -> str:
    remote_name = _remote_device_name(remote)
    if remote_name and (not _is_generic_device_name(remote_name) or _is_generic_device_name(saved_name)):
        return remote_name
    return saved_name or remote_name or remote.busid


def _find_saved_auto_connect_device(
    auto_connect_devices,
    busid: str,
    vendor_id: str = "",
    product_id: str = "",
):
    for dev in auto_connect_devices:
        if dev.busid != busid:
            continue
        if vendor_id and product_id and dev.vendor_id and dev.product_id:
            if dev.vendor_id != vendor_id or dev.product_id != product_id:
                continue
        return dev
    return None
