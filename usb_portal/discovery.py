"""
LAN discovery for usb|portal hosts using UDP broadcast.
Hosts advertise their presence; clients scan for available hosts.
Also supports CIDR-range unicast scanning for VPN/routed networks.
"""

import hashlib
import hmac
import ipaddress
import json
import math
import re
import select
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from usb_portal.config import (
    CIDR_SCAN_ATTEMPTS,
    CIDR_SCAN_RATE,
    CIDR_SCAN_TIMEOUT,
    DISCOVERY_BROADCAST,
    DISCOVERY_INTERVAL,
    DISCOVERY_PORT,
    DISCOVERY_TIMEOUT,
)


def _get_local_ips() -> set[str]:
    """Return all local IP addresses for this machine to filter self-announcements."""
    ips = {"127.0.0.1", "::1"}
    try:
        import subprocess
        result = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"],
            capture_output=True, text=True, timeout=3
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            for part in parts:
                if "/" in part:
                    ips.add(part.split("/")[0])
    except Exception:
        pass
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            ips.add(info[4][0])
    except Exception:
        pass
    return ips


def _get_machine_id() -> str:
    """Read the system machine ID from /etc/machine-id (stable across reboots and IP changes)."""
    try:
        return private_machine_id(Path("/etc/machine-id").read_text().strip())
    except OSError:
        return ""


def private_machine_id(value: str) -> str:
    """Derive an application identity; also upgrades saved legacy identities."""
    if re.fullmatch(r"[0-9a-fA-F]{32}", value):
        return "usbportal-" + hmac.new(
            bytes.fromhex(value), b"usb-portal/discovery/v1", hashlib.sha256,
        ).hexdigest()
    return value


def _decode_packet(data: bytes) -> Optional[dict]:
    if len(data) > 2048:
        return None
    try:
        msg = json.loads(data.decode())
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(msg, dict) or msg.get("app") != "usb-portal":
        return None
    if msg.get("type") == "query":
        return msg
    if msg.get("type") != "announce":
        return None
    for key in ("hostname", "machine_id"):
        value = msg.get(key, "")
        if not isinstance(value, str) or len(value) > 255 or any(ord(c) < 32 for c in value):
            return None
    for key, default, minimum, maximum in (
        ("port", 3240, 1, 65535), ("devices", 0, 0, 4096), ("controllers", 0, 0, 4096),
    ):
        value = msg.get(key, default)
        if type(value) is not int or not minimum <= value <= maximum:
            return None
    msg["machine_id"] = private_machine_id(msg.get("machine_id", ""))
    return msg


@dataclass
class RemoteHost:
    ip: str
    hostname: str
    port: int
    device_count: int
    last_seen: float = field(default_factory=time.time)
    controller_count: int = 0
    machine_id: str = ""

    @property
    def address(self) -> str:
        return self.ip

    @property
    def is_stale(self) -> bool:
        return (time.time() - self.last_seen) > (DISCOVERY_INTERVAL * 3)

    def __str__(self) -> str:
        suffix = f" ({self.controller_count} controllers)" if self.controller_count > 0 else ""
        return f"{self.hostname} [{self.ip}]{suffix}"


class DiscoveryAdvertiser:
    """
    Broadcasts this machine's presence as a usb|portal host on the LAN.
    Runs in a background thread.
    """

    def __init__(self, hostname: str, usbip_port: int, get_device_count: Callable[[], int]):
        self._hostname = hostname
        self._port = usbip_port
        self._get_count = get_device_count
        self._machine_id = _get_machine_id()
        self._running = False
        self._broadcast_thread: Optional[threading.Thread] = None
        self._query_thread: Optional[threading.Thread] = None
        self._query_sock: Optional[socket.socket] = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._broadcast_thread = threading.Thread(
            target=self._broadcast_loop, daemon=True, name="discovery-broadcast"
        )
        self._query_thread = threading.Thread(
            target=self._query_listener, daemon=True, name="discovery-query"
        )
        self._broadcast_thread.start()
        self._query_thread.start()

    def stop(self) -> None:
        self._running = False
        if self._query_sock:
            try:
                self._query_sock.close()
            except Exception:
                pass
        if self._broadcast_thread:
            self._broadcast_thread.join(timeout=3)
        if self._query_thread:
            self._query_thread.join(timeout=3)

    def _make_announce(self) -> bytes:
        return json.dumps({
            "type": "announce",
            "hostname": self._hostname,
            "machine_id": self._machine_id,
            "port": self._port,
            "devices": self._get_count(),
            "app": "usb-portal",
            "version": 1,
        }).encode()

    def _broadcast_loop(self) -> None:
        """Periodically broadcast presence to the LAN."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            while self._running:
                try:
                    sock.sendto(self._make_announce(), (DISCOVERY_BROADCAST, DISCOVERY_PORT))
                except OSError:
                    pass
                for _ in range(DISCOVERY_INTERVAL * 10):
                    if not self._running:
                        break
                    time.sleep(0.1)
        finally:
            sock.close()

    def _query_listener(self) -> None:
        """Listen for query packets and respond immediately with an announce."""
        self._query_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._query_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._query_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        self._query_sock.settimeout(1.0)
        try:
            self._query_sock.bind(("", DISCOVERY_PORT))
        except OSError:
            self._query_sock.close()
            return

        broadcast_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        broadcast_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        while self._running:
            try:
                data, addr = self._query_sock.recvfrom(1024)
                try:
                    msg = json.loads(data.decode())
                    if msg.get("app") == "usb-portal" and msg.get("type") == "query":
                        announce = self._make_announce()
                        try:
                            broadcast_sock.sendto(
                                announce, (DISCOVERY_BROADCAST, DISCOVERY_PORT)
                            )
                        except OSError:
                            pass
                        try:
                            broadcast_sock.sendto(announce, addr)
                        except OSError:
                            pass
                except Exception:
                    pass
            except socket.timeout:
                continue
            except OSError:
                break

        broadcast_sock.close()
        try:
            self._query_sock.close()
        except Exception:
            pass
        self._query_sock = None


class DiscoveryScanner:
    """
    Scans the LAN for usb|portal hosts using UDP broadcast.
    Maintains a live list of discovered hosts.

    A background socket receives periodic announcements on the discovery port.
    Active scans use a separate ephemeral port so unicast replies cannot land
    in another USBPortal process that shares the discovery listener port.
    """

    def __init__(self):
        self._hosts: dict[str, RemoteHost] = {}
        self._lock = threading.Lock()
        self._running = False
        self._listen_thread: Optional[threading.Thread] = None
        self._sock: Optional[socket.socket] = None
        self._local_ips: set[str] = set()

    @property
    def hosts(self) -> list[RemoteHost]:
        with self._lock:
            self._prune_stale()
            return list(self._hosts.values())

    def _prune_stale(self) -> None:
        stale = [ip for ip, h in self._hosts.items() if h.is_stale]
        for ip in stale:
            del self._hosts[ip]

    def start_listening(self) -> None:
        """Start background listener for host announcements."""
        if self._running:
            return
        self._local_ips = _get_local_ips()
        self._running = True
        self._listen_thread = threading.Thread(
            target=self._listen, daemon=True, name="discovery-listener"
        )
        self._listen_thread.start()

    def stop_listening(self) -> None:
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
        if self._listen_thread:
            self._listen_thread.join(timeout=3)

    def _bind_socket(self) -> Optional[socket.socket]:
        """Create and bind the UDP listener socket."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        sock.settimeout(1.0)
        try:
            sock.bind(("", DISCOVERY_PORT))
            return sock
        except OSError:
            sock.close()
            return None

    def _listen(self) -> None:
        self._sock = self._bind_socket()
        if self._sock is None:
            self._running = False
            return

        while self._running:
            try:
                data, addr = self._sock.recvfrom(1024)
                msg = _decode_packet(data)
                if msg and msg.get("type") == "query":
                    # SO_REUSEPORT can deliver queries to either listener.
                    # Every listener must answer, including an open CLI.
                    from usb_portal.usbip import get_bound_devices
                    announce = DiscoveryAdvertiser(
                        get_local_hostname(), 3240, lambda: len(get_bound_devices()),
                    )._make_announce()
                    self._sock.sendto(announce, addr)
                    continue
                self._handle_packet(data, addr[0])
            except socket.timeout:
                continue
            except OSError:
                break

        try:
            self._sock.close()
        except Exception:
            pass
        self._sock = None
        self._running = False

    def _handle_packet(self, data: bytes, sender_ip: str) -> None:
        if sender_ip in self._local_ips:
            return

        msg = _decode_packet(data)
        if msg is None:
            return

        if msg.get("type") == "announce":
            with self._lock:
                self._prune_stale()
                if sender_ip not in self._hosts and len(self._hosts) >= 1024:
                    return
                existing = self._hosts.get(sender_ip)
                if existing:
                    existing.last_seen = time.time()
                    existing.hostname = msg.get("hostname", existing.hostname)
                    existing.port = msg.get("port", existing.port)
                    existing.device_count = msg.get("devices", 0)
                    existing.controller_count = msg.get("controllers", 0)
                    if msg.get("machine_id"):
                        existing.machine_id = msg.get("machine_id", existing.machine_id)
                else:
                    self._hosts[sender_ip] = RemoteHost(
                        ip=sender_ip,
                        hostname=msg.get("hostname", sender_ip),
                        machine_id=msg.get("machine_id", ""),
                        port=msg.get("port", 3240),
                        device_count=msg.get("devices", 0),
                        controller_count=msg.get("controllers", 0),
                    )

    def scan_once(self, timeout: float = DISCOVERY_TIMEOUT) -> list[RemoteHost]:
        """
        Trigger an active scan and return discovered hosts.

        Collect direct replies on a dedicated socket alongside announcements
        from the background listener, starting a temporary listener if needed.
        """
        was_listening = self._running
        if not was_listening:
            self._local_ips = _get_local_ips()
            self._running = True
            self._listen_thread = threading.Thread(
                target=self._listen, daemon=True, name="discovery-scan"
            )
            self._listen_thread.start()
            time.sleep(0.1)

        # Keep fresh announcements: a scan may be shorter than the host's
        # broadcast interval, and a lost UDP reply must not erase a known host.
        self._send_query(timeout)

        results = self.hosts

        if not was_listening:
            self.stop_listening()

        return results

    def _send_query(self, timeout: float = DISCOVERY_TIMEOUT) -> None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.settimeout(max(timeout, 0.001))
                msg = json.dumps({"type": "query", "app": "usb-portal", "version": 1}).encode()
                deadline = time.monotonic() + timeout
                sock.sendto(msg, (DISCOVERY_BROADCAST, DISCOVERY_PORT))
                while self._running:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    sock.settimeout(remaining)
                    try:
                        data, addr = sock.recvfrom(2049)
                    except socket.timeout:
                        break
                    self._handle_packet(data, addr[0])
        except OSError:
            pass


_SCAN_NETWORKS = tuple(ipaddress.ip_network(cidr) for cidr in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
))


def parse_scan_network(cidr: str) -> ipaddress.IPv4Network:
    """Normalize an explicit LAN/VPN scan range without including public addresses."""
    network = ipaddress.ip_network(cidr, strict=False)
    if network.version != 4 or not any(network.subnet_of(private) for private in _SCAN_NETWORKS):
        raise ValueError(
            "Enter an IPv4 network within 10.0.0.0/8, 172.16.0.0/12, "
            "192.168.0.0/16, or the VPN range 100.64.0.0/10"
        )
    return network


@dataclass(frozen=True)
class CidrScanProgress:
    completed: int
    total: int
    found: int
    pass_number: int
    elapsed: float


class CidrScanCancelled(Exception):
    """A stopped scan retains hosts that already answered."""

    def __init__(self, hosts: list[RemoteHost]):
        super().__init__("Network scan stopped")
        self.hosts = hosts


def scan_cidr(
    cidr: str,
    port: int = DISCOVERY_PORT,
    timeout: float = CIDR_SCAN_TIMEOUT,
    *,
    cancel_event: Optional[threading.Event] = None,
    on_progress: Optional[Callable[[CidrScanProgress], None]] = None,
    on_host: Optional[Callable[[RemoteHost], None]] = None,
    rate: float = CIDR_SCAN_RATE,
    attempts: int = CIDR_SCAN_ATTEMPTS,
    include_edges: bool = False,
) -> list[RemoteHost]:
    """
    Discover hosts across an explicitly entered private IPv4 CIDR, including /8.

    One ephemeral UDP socket sends paced queries and collects replies concurrently.
    Addresses are generated lazily; there are no per-address sockets, threads, or
    queued jobs. Retry passes skip hosts already found. Each pass allows `timeout`
    seconds for its final replies. Progress counts addresses considered across all
    passes (including local/already-found addresses, which need no query).

    Ctrl+C or cancel_event raises CidrScanCancelled with the partial results.
    on_host runs as soon as a validated first response arrives, before scan end.
    include_edges includes chunk endpoints when a larger network is subdivided.
    """
    network = parse_scan_network(cidr)
    if (not math.isfinite(timeout) or timeout <= 0
            or not math.isfinite(rate) or rate <= 0
            or type(attempts) is not int or not 1 <= attempts <= 5
            or type(port) is not int or not 1 <= port <= 65535):
        raise ValueError("Invalid discovery timeout, rate, attempts, or port")
    local_ips = _get_local_ips()
    found: dict[str, RemoteHost] = {}
    count = network.num_addresses if include_edges or network.prefixlen >= 31 else network.num_addresses - 2
    completed = 0
    highest_probed = int(network.network_address) - 1
    started = time.monotonic()
    next_progress = started
    query = json.dumps({"type": "query", "app": "usb-portal", "version": 1}).encode()

    def report(pass_number: int, force: bool = False) -> None:
        nonlocal next_progress
        now = time.monotonic()
        if on_progress and (force or now >= next_progress):
            on_progress(CidrScanProgress(completed, count * attempts, len(found), pass_number, now - started))
            next_progress = now + 0.25

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setblocking(False)
            # Accommodate simultaneous replies without a socket per target.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 256 * 1024)
            sock.bind(("", 0))
            for pass_number in range(1, attempts + 1):
                addresses = iter(network) if include_edges else iter(network.hosts())
                draining_until = None
                next_send = time.monotonic()
                report(pass_number, force=True)
                while True:
                    if cancel_event and cancel_event.is_set():
                        raise CidrScanCancelled(list(found.values()))
                    now = time.monotonic()
                    if draining_until is None and now >= next_send:
                        sent = 0
                        # Small bursts bound work between receive/cancel checks.
                        for _ in range(32):
                            address = next(addresses, None)
                            if address is None:
                                draining_until = time.monotonic() + timeout
                                break
                            completed += 1
                            ip = str(address)
                            if ip in local_ips or ip in found:
                                continue
                            highest_probed = max(highest_probed, int(address))
                            try:
                                sock.sendto(query, (ip, port))
                            except OSError:
                                # One unreachable destination must not end a routed scan.
                                pass
                            sent += 1
                        next_send = time.monotonic() + sent / rate
                    report(pass_number)
                    now = time.monotonic()
                    if draining_until is not None and now >= draining_until:
                        break
                    wake_at = draining_until if draining_until is not None else next_send
                    readable, _, _ = select.select([sock], [], [], max(0, min(0.1, wake_at - now)))
                    if not readable:
                        continue
                    for _ in range(256):
                        try:
                            data, addr = sock.recvfrom(2049)
                        except BlockingIOError:
                            break
                        except OSError:
                            break
                        sender = ipaddress.ip_address(addr[0])
                        if (sender not in network or int(sender) > highest_probed
                                or addr[0] in local_ips
                                or (not include_edges and network.prefixlen < 31
                                    and sender in (network.network_address, network.broadcast_address))):
                            continue
                        pkt = _decode_packet(data)
                        if not pkt or pkt.get("type") != "announce" or addr[0] in found:
                            continue
                        host = RemoteHost(
                            ip=addr[0], hostname=pkt.get("hostname", addr[0]),
                            machine_id=pkt.get("machine_id", ""), port=pkt.get("port", 3240),
                            device_count=pkt.get("devices", 0), controller_count=pkt.get("controllers", 0),
                        )
                        found[host.ip] = host
                        if on_host:
                            on_host(host)
            report(attempts, force=True)
    except KeyboardInterrupt:
        raise CidrScanCancelled(list(found.values())) from None
    return list(found.values())


def get_local_hostname() -> str:
    """Get the local machine's hostname."""
    return socket.gethostname()


def get_local_ip() -> str:
    """Best-effort attempt to get the primary LAN IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"
