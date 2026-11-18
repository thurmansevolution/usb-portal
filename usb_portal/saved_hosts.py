"""
Saved Hosts configuration.
Stores known remote hosts and their per-device auto-connect preferences.
Config file: /var/lib/usb-portal/hosts.json

Each saved host optionally carries a list of devices to auto-attach on startup,
enabling VPN / non-LAN hosts to behave just like locally discovered ones.
"""

import copy
import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from usb_portal.persistence import atomic_write, file_lock
from usb_portal.discovery import private_machine_id

log = logging.getLogger("usb-portal-daemon")


CONFIG_FILE = Path("/var/lib/usb-portal/hosts.json")
_LEGACY_CONFIG_FILE = Path("/var/lib/gamepad-share/hosts.json")


@dataclass
class AutoConnectDevice:
    """A device on a saved host that should be automatically attached."""
    busid: str
    vendor_id: str
    product_id: str
    name: str = ""

    @property
    def vid_pid(self) -> str:
        return f"{self.vendor_id}:{self.product_id}"

    def __str__(self) -> str:
        return self.name or f"{self.vid_pid} ({self.busid})"


@dataclass
class SavedHost:
    ip: str
    name: str = ""
    hostname: str = ""
    machine_id: str = ""
    network: str = ""
    port: int = 3240
    auto_connect: list[AutoConnectDevice] = field(default_factory=list)
    _baseline: Optional[dict] = field(default=None, repr=False, compare=False, init=False)

    @property
    def display_name(self) -> str:
        return self.name or self.hostname or self.ip

    def has_auto_connect_device(self, busid: str) -> bool:
        return any(d.busid == busid for d in self.auto_connect)

    def __str__(self) -> str:
        suffix = f" ({len(self.auto_connect)} auto-connect)" if self.auto_connect else ""
        label = f" [{self.hostname or self.ip}]" if self.name else ""
        return f"{self.display_name}{label}{suffix}"


class SavedHostsConfig:
    """
    Loads and persists the saved hosts list.
    Thread-safe for concurrent reads from the auto-connect background thread.
    """

    def __init__(self, path: Path = CONFIG_FILE):
        self._path = path
        self._lock = threading.Lock()
        self._migrate_if_needed()

    def _migrate_if_needed(self) -> None:
        """Migrate hosts.json from the legacy path if needed."""
        if not self._path.exists() and _LEGACY_CONFIG_FILE.exists():
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                import shutil as _sh
                _sh.copy2(str(_LEGACY_CONFIG_FILE), str(self._path))
            except Exception:
                pass

    def _load_raw(self) -> dict:
        try:
            data = json.loads(self._path.read_text())
            if not isinstance(data, dict) or not isinstance(data.get("hosts", []), list):
                raise ValueError("expected an object containing a hosts list")
            return data
        except FileNotFoundError:
            return {"version": 1, "hosts": []}
        except (OSError, ValueError) as error:
            # A failed read must never become an empty successful write.
            raise OSError(f"Cannot read {self._path}: {error}") from error

    def _save_raw(self, data: dict) -> bool:
        try:
            atomic_write(self._path, data)
            return True
        except OSError as error:
            log.warning("Could not save hosts: %s", error)
            return False

    def _parse_host(self, item: dict) -> SavedHost:
        devices = [
            AutoConnectDevice(
                busid=d.get("busid", ""),
                vendor_id=d.get("vendor_id", ""),
                product_id=d.get("product_id", ""),
                name=d.get("name", ""),
            )
            for d in item.get("auto_connect", []) if isinstance(d, dict)
        ]
        host = SavedHost(
            ip=item.get("ip", ""),
            name=item.get("name", ""),
            hostname=item.get("hostname", ""),
            machine_id=private_machine_id(item.get("machine_id", "")),
            network=item.get("network", ""),
            port=item.get("port", 3240),
            auto_connect=devices,
        )
        host._baseline = copy.deepcopy(self._serialise_host(host))
        return host

    def _serialise_host(self, host: SavedHost) -> dict:
        return {
            "ip": host.ip,
            "name": host.name,
            "hostname": host.hostname,
            "machine_id": private_machine_id(host.machine_id),
            "network": host.network,
            "port": host.port,
            "auto_connect": [asdict(d) for d in host.auto_connect],
        }

    def get_all(self) -> list[SavedHost]:
        with self._lock:
            data = self._load_raw()
        return [self._parse_host(h) for h in data.get("hosts", []) if isinstance(h, dict) and h.get("ip")]

    def get(self, ip: str) -> Optional[SavedHost]:
        """Look up a saved host by its current IP address."""
        for host in self.get_all():
            if host.ip == ip:
                return host
        return None

    def get_by_machine_id(self, machine_id: str) -> Optional[SavedHost]:
        """Look up a saved host by its stable machine ID."""
        if not machine_id:
            return None
        machine_id = private_machine_id(machine_id)
        for host in self.get_all():
            if host.machine_id == machine_id:
                return host
        return None

    def find(self, ip_or_machine_id: str) -> Optional[SavedHost]:
        """Look up by machine_id first, then by IP. Useful when IP may have changed."""
        return self.get_by_machine_id(ip_or_machine_id) or self.get(ip_or_machine_id)

    def save_host(self, host: SavedHost) -> bool:
        """Add or update a host entry. Deduplicates by machine_id when available, else by IP."""
        with self._lock, file_lock(self._path):
            data = self._load_raw()
            hosts = data.get("hosts", [])
            existing = None
            if host.machine_id:
                existing = next(
                    (i for i, h in enumerate(hosts)
                     if private_machine_id(h.get("machine_id", "")) == private_machine_id(host.machine_id)),
                    None,
                )
            if existing is None:
                previous_ip = host._baseline.get("ip") if host._baseline else None
                existing = next(
                    (i for i, h in enumerate(hosts) if h.get("ip") in {host.ip, previous_ip}),
                    None,
                )
            serialised = self._serialise_host(host)
            if host._baseline is None and existing is not None:
                serialised = _merge_host_update(
                    hosts[existing], self._serialise_host(SavedHost(ip=host.ip)), serialised,
                )
            if host._baseline is not None:
                if existing is None:
                    # An entry removed while a worker was offline stays removed.
                    return False
                serialised = _merge_host_update(hosts[existing], host._baseline, serialised)
            serialised["machine_id"] = private_machine_id(serialised.get("machine_id", ""))
            if existing is not None:
                hosts[existing] = serialised
            else:
                hosts.append(serialised)
            data["hosts"] = hosts
            if not self._save_raw(data):
                return False
            host.machine_id = private_machine_id(host.machine_id)
            host._baseline = copy.deepcopy(self._serialise_host(host))
            return True

    def remove_host(self, ip: str) -> bool:
        """Remove a host by IP. Returns True if it existed."""
        with self._lock, file_lock(self._path):
            data = self._load_raw()
            before = len(data.get("hosts", []))
            data["hosts"] = [h for h in data.get("hosts", []) if h.get("ip") != ip]
            changed = len(data["hosts"]) < before
            if changed:
                return self._save_raw(data)
        return changed

    def remove_host_by_machine_id(self, machine_id: str) -> bool:
        """Remove a host by machine_id. Returns True if it existed."""
        with self._lock, file_lock(self._path):
            data = self._load_raw()
            before = len(data.get("hosts", []))
            data["hosts"] = [h for h in data.get("hosts", [])
                             if private_machine_id(h.get("machine_id", "")) != private_machine_id(machine_id)]
            changed = len(data["hosts"]) < before
            if changed:
                return self._save_raw(data)
        return changed

    def update_host_ip(self, machine_id: str, new_ip: str) -> bool:
        """Update the cached IP for a host identified by machine_id. Returns True on success."""
        host = self.get_by_machine_id(machine_id)
        if host and host.ip != new_ip:
            host.ip = new_ip
            return self.save_host(host)
        return False

    def add_auto_connect_device(self, host_ip: str, device: AutoConnectDevice) -> bool:
        """Mark a device on a host for auto-connect, creating the host entry if needed."""
        host = self.get(host_ip) or SavedHost(ip=host_ip)
        existing = {(d.busid, d.vendor_id, d.product_id) for d in host.auto_connect}
        if (device.busid, device.vendor_id, device.product_id) not in existing:
            host.auto_connect.append(device)
            return self.save_host(host)
        return True

    def remove_auto_connect_device(self, host_ip: str, busid: str) -> bool:
        """Remove a device from the auto-connect list for a host."""
        host = self.get(host_ip)
        if not host:
            return False
        before = len(host.auto_connect)
        host.auto_connect = [d for d in host.auto_connect if d.busid != busid]
        if len(host.auto_connect) == before:
            return False
        return self.save_host(host)

    def rename_host(self, ip: str, new_name: str) -> bool:
        host = self.get(ip)
        if host:
            host.name = new_name
            return self.save_host(host)
        return False


_config: Optional[SavedHostsConfig] = None


def _merge_host_update(current: dict, baseline: dict, desired: dict) -> dict:
    """Apply a caller's edits without replacing concurrent, unrelated edits."""
    result = copy.deepcopy(current)
    for key, value in desired.items():
        if key != "auto_connect" and value != baseline.get(key):
            result[key] = value
    before = baseline.get("auto_connect", [])
    after = desired.get("auto_connect", [])
    if before == after:
        return result
    def identity(device):
        return (device.get("busid"), device.get("vendor_id"), device.get("product_id"))
    old = {identity(d): d for d in before}
    new = {identity(d): d for d in after}
    live = {identity(d): d for d in current.get("auto_connect", [])}
    moved = {}
    for previous, desired_device in zip(before, after):
        old_key, new_key = identity(previous), identity(desired_device)
        if (old_key not in new and new_key not in old and old_key[1:] == new_key[1:]):
            moved[new_key] = old_key
    live_keys = set(live)
    for key in old.keys() - new.keys():
        live.pop(key, None)
    for key, device in new.items():
        if key in moved and moved[key] not in live_keys:
            continue
        if key not in old or (key in live and device != old[key]):
            live[key] = device
    result["auto_connect"] = list(live.values())
    return result


def get_config() -> SavedHostsConfig:
    global _config
    if _config is None:
        _config = SavedHostsConfig()
    return _config
