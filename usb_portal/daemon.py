"""
usb|portal headless daemon.
Runs as a systemd service: manages usbipd, LAN discovery, and persists
bound device state across restarts.

Auto-rebind logic:
  - Saves {busid, vendor_id, product_id} for every bound device.
  - Polls sysfs every 2 s for newly appeared USB devices.
  - If a saved device reappears (matched by busid OR by VID:PID when the
    busid changes — e.g. different USB port), it is automatically rebound.

Entry point: usb-portal-daemon
"""

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from usb_portal.config import USBIP_PORT
from usb_portal.discovery import DiscoveryAdvertiser, get_local_hostname
from usb_portal import usbip
from usb_portal.usbip import UsbipError
from usb_portal.client import ClientSession
from usb_portal.persistence import atomic_write, file_lock
from usb_portal.transport import usbip_environment
from usb_portal.logging_setup import configure_logging, get_log_path, log_subprocess_output

STATE_DIR = Path("/var/lib/usb-portal")
STATE_FILE = STATE_DIR / "state.json"
_LEGACY_STATE_DIR = Path("/var/lib/gamepad-share")
_LEGACY_STATE_FILE = _LEGACY_STATE_DIR / "state.json"
log = logging.getLogger("usb-portal-daemon")


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

@dataclass
class BoundDevice:
    busid: str
    vendor_id: str
    product_id: str
    name: str = ""
    auto_rebind: bool = True

    @property
    def vid_pid(self) -> str:
        return f"{self.vendor_id}:{self.product_id}"


def _merge_bindings(*groups: list[BoundDevice]) -> list[BoundDevice]:
    """
    Merge binding lists by busid. Later groups refresh metadata without
    dropping entries that are remembered but currently unplugged.
    """
    merged: dict[str, BoundDevice] = {}
    for group in groups:
        for device in group:
            if not device.busid:
                continue
            merged[device.busid] = device
    return list(merged.values())


def _refresh_remembered_bindings(
    remembered: list[BoundDevice],
    active: list[BoundDevice],
) -> list[BoundDevice]:
    """
    Refresh remembered bindings with active metadata.

    The state file is treated as the source of truth for what should be
    remembered. Active bindings only update existing remembered entries, so a
    user removing an auto-rebind entry does not have it re-added by shutdown.
    """
    refreshed = list(remembered)
    active_by_busid = {device.busid: device for device in active if device.busid}
    current_devices = {
        device.busid: (device.vendor_id, device.product_id)
        for device in active
        if device.busid and device.vendor_id and device.product_id
    }
    assignments = _resolve_wanted_busids(refreshed, current_devices)

    remembered_busid_indices = {
        device.busid: i for i, device in enumerate(refreshed) if device.busid
    }

    for i, busid in assignments.items():
        old_owner = remembered_busid_indices.get(busid)
        if old_owner is not None and old_owner != i and old_owner not in assignments:
            continue
        active_device = active_by_busid.get(busid)
        if active_device:
            refreshed[i] = active_device

    return _merge_bindings(refreshed)


def _same_device_identity(device: BoundDevice, vendor_id: str, product_id: str) -> bool:
    """Return True when current VID:PID is compatible with a remembered device."""
    if not device.vendor_id or not device.product_id:
        return True
    return device.vendor_id == vendor_id and device.product_id == product_id


def _pending_wanted_bindings(
    wanted: list[BoundDevice],
    bound: list[BoundDevice],
) -> list[BoundDevice]:
    """
    Return wanted devices that are not already satisfied by active bindings.

    Matching is identity-aware. A bound device at the same busid only satisfies
    a wanted entry if VID:PID is compatible; otherwise that wanted entry still
    needs to be resolved to its moved busid.
    """
    claimed_bound: set[int] = set()
    pending: list[BoundDevice] = []

    for wanted_device in wanted:
        exact_index = next(
            (
                i for i, bound_device in enumerate(bound)
                if i not in claimed_bound
                and bound_device.busid == wanted_device.busid
                and _same_device_identity(
                    wanted_device,
                    bound_device.vendor_id,
                    bound_device.product_id,
                )
            ),
            None,
        )
        if exact_index is not None:
            claimed_bound.add(exact_index)
            continue
        pending.append(wanted_device)

    still_pending: list[BoundDevice] = []
    for wanted_device in pending:
        identity_index = next(
            (
                i for i, bound_device in enumerate(bound)
                if i not in claimed_bound
                and wanted_device.vendor_id
                and wanted_device.product_id
                and bound_device.vendor_id == wanted_device.vendor_id
                and bound_device.product_id == wanted_device.product_id
            ),
            None,
        )
        if identity_index is not None:
            claimed_bound.add(identity_index)
            continue
        still_pending.append(wanted_device)

    return still_pending


def _resolve_wanted_busids(
    devices: list[BoundDevice],
    current_devices: dict[str, tuple[str, str]],
    excluded_busids: Optional[set[str]] = None,
) -> dict[int, str]:
    """
    Resolve wanted devices to current busids as a batch.

    Exact busid+identity matches always win. VID:PID fallback is used for
    unique devices, and for duplicate remembered identities it allocates the
    available matching ports in stable order. This lets two identical,
    serial-less adapters reconnect as "two SNES adapters" without pretending
    we can distinguish the physical units.
    """
    claimed = set(excluded_busids or set())
    assignments: dict[int, str] = {}
    unresolved_by_identity: dict[tuple[str, str], list[int]] = {}

    for i, device in enumerate(devices):
        if device.busid in current_devices and device.busid not in claimed:
            current_vid, current_pid = current_devices[device.busid]
            if _same_device_identity(device, current_vid, current_pid):
                assignments[i] = device.busid
                claimed.add(device.busid)
                continue
            log.info(
                "  Saved busid %s now has [%s:%s], expected [%s]. Looking for moved device.",
                device.busid,
                current_vid,
                current_pid,
                device.vid_pid,
            )

        if device.vendor_id and device.product_id:
            identity = (device.vendor_id, device.product_id)
            unresolved_by_identity.setdefault(identity, []).append(i)

    for identity, indices in unresolved_by_identity.items():
        available = [
            busid for busid, vidpid in sorted(current_devices.items())
            if vidpid == identity and busid not in claimed
        ]
        if not available:
            continue

        if len(indices) == 1 and len(available) > 1:
            log.warning(
                "  Device [%s:%s] matched multiple USB ports (%s); keeping saved busid %s.",
                identity[0],
                identity[1],
                ", ".join(available),
                devices[indices[0]].busid,
            )
            continue

        for i, busid in zip(indices, available):
            device = devices[i]
            if device.busid != busid:
                log.info(
                    "  Device [%s] moved from %s → %s (different port).",
                    device.vid_pid, device.busid, busid,
                )
            assignments[i] = busid
            claimed.add(busid)

    return assignments


class DaemonState:
    """Persistent state stored on disk between daemon runs."""

    def __init__(self, path: Path = STATE_FILE):
        self._path = path
        self._lock = threading.Lock()
        self.last_error = ""
        self._migrate_if_needed()

    def _migrate_if_needed(self) -> None:
        """Migrate state.json from the legacy path if needed."""
        if not self._path.exists() and _LEGACY_STATE_FILE.exists():
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                import shutil as _sh
                _sh.copy2(str(_LEGACY_STATE_FILE), str(self._path))
                log.info("Migrated state from %s to %s", _LEGACY_STATE_FILE, self._path)
            except Exception as e:
                log.warning("State migration failed: %s", e)

    def load(self) -> dict:
        try:
            data = json.loads(self._path.read_text())
            if not isinstance(data, dict) or not isinstance(data.get("bindings", []), list):
                raise ValueError("expected an object containing a bindings list")
            return data
        except FileNotFoundError:
            return {"bindings": [], "version": 2}
        except (OSError, ValueError) as error:
            raise OSError(f"Cannot read {self._path}: {error}") from error

    def save(self, data: dict) -> bool:
        self.last_error = ""
        try:
            atomic_write(self._path, data)
            return True
        except OSError as e:
            self.last_error = str(e)
            log.warning("Could not save state: %s", e)
            return False

    def get_bindings(self) -> list[BoundDevice]:
        data = self.load()
        bindings = []
        for item in data.get("bindings", []):
            if isinstance(item, str):
                bindings.append(BoundDevice(busid=item, vendor_id="", product_id="", auto_rebind=True))
            elif isinstance(item, dict):
                bindings.append(BoundDevice(
                    busid=item.get("busid", ""),
                    vendor_id=item.get("vendor_id", ""),
                    product_id=item.get("product_id", ""),
                    name=item.get("name", ""),
                    auto_rebind=item.get("auto_rebind", True),
                ))
        return bindings

    def set_bindings(self, devices: list[BoundDevice]) -> bool:
        return self.update_bindings(lambda current: devices)

    def update_bindings(self, update) -> bool:
        """Read, modify and persist remembered intent under one process lock."""
        with self._lock, file_lock(self._path):
            data = self.load()
            devices = update(self.get_bindings())
            data["bindings"] = [asdict(d) for d in devices]
            data["version"] = 2
            return self.save(data)


# ---------------------------------------------------------------------------
# USB sysfs monitor
# ---------------------------------------------------------------------------

def _read_sysfs(path: Path, default: str = "") -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return default


def _enumerate_sysfs_devices() -> dict[str, tuple[str, str]]:
    """
    Return {busid: (vendor_id, product_id)} for all current USB devices from sysfs.
    Only includes devices that have idVendor/idProduct (actual devices, not hubs/ports).
    """
    devices: dict[str, tuple[str, str]] = {}
    usb_root = Path("/sys/bus/usb/devices")
    if not usb_root.exists():
        return devices

    for entry in usb_root.iterdir():
        name = entry.name
        if not name[0].isdigit() or ":" in name:
            continue
        vid = _read_sysfs(entry / "idVendor")
        pid = _read_sysfs(entry / "idProduct")
        if vid and pid:
            devices[name] = (vid, pid)

    return devices


class UsbDeviceMonitor:
    """
    Polls /sys/bus/usb/devices every POLL_INTERVAL seconds.
    Calls on_new_devices with newly appeared {busid: (vid, pid)} entries.
    Calls on_removed_devices with a set of busids that disappeared.
    """

    POLL_INTERVAL = 2.0

    def __init__(self, on_new_devices, on_removed_devices=None):
        self._callback = on_new_devices
        self._removed_callback = on_removed_devices
        self._known: dict[str, tuple[str, str]] = {}
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._known = _enumerate_sysfs_devices()
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="usb-monitor")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while self._running:
            time.sleep(self.POLL_INTERVAL)
            try:
                current = _enumerate_sysfs_devices()
                new = {k: v for k, v in current.items() if self._known.get(k) != v}
                removed = {k for k, v in self._known.items() if current.get(k) != v}
                self._known = current
                if removed and self._removed_callback:
                    self._removed_callback(removed)
                if new:
                    self._callback(new)
            except Exception as e:
                log.debug("USB monitor error: %s", e)


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

class GamepadDaemon:
    """
    Headless daemon that:
    1. Loads USB/IP kernel modules
    2. Starts usbipd
    3. Restores previously bound devices
    4. Monitors USB bus for reconnected devices and auto-rebinds them
    5. Advertises presence on LAN via UDP broadcast
    6. Saves state on shutdown for clean restore on next start
    """

    def __init__(self):
        self._state = DaemonState()
        self._advertiser: Optional[DiscoveryAdvertiser] = None
        self._usbipd_proc: Optional[subprocess.Popen] = None
        self._running = False
        self._stop = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None
        self._usb_monitor: Optional[UsbDeviceMonitor] = None
        self._client: Optional[ClientSession] = None

        self._bound: dict[str, BoundDevice] = {}
        self._wanted: list[BoundDevice] = []
        self._lock = threading.Lock()

    def start(self) -> None:
        log.info("usb|portal daemon starting (PID %d).", os.getpid())
        self._running = True
        self._stop.clear()

        self._setup_signal_handlers()

        log.info("Loading kernel modules...")
        try:
            usbip.load_host_modules()
        except UsbipError as e:
            log.error("Failed to load modules: %s", e)
            sys.exit(1)

        log.info("Starting usbipd...")
        try:
            self._start_usbipd()
        except UsbipError as e:
            log.error("Failed to start usbipd: %s", e)
            sys.exit(1)

        self._wanted = self._state.get_bindings()
        self._restore_bindings()
        self._discover_existing_bindings()
        self._save_state()
        self._start_usb_monitor()
        self._start_advertising()
        self._start_health_monitor()
        self._start_client_session()

        log.info("Daemon ready. Listening on port %d.", USBIP_PORT)

        while self._running:
            time.sleep(1)

        self._shutdown()

    # ------------------------------------------------------------------
    # Low-latency tuning
    # ------------------------------------------------------------------

    def _apply_low_latency_tuning(self) -> None:
        """Compatibility hook; USBPortal does not change global network settings."""

    # ------------------------------------------------------------------
    # Signal handling
    # ------------------------------------------------------------------

    def _setup_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum: int, frame) -> None:
        log.info("Received signal %d, shutting down...", signum)
        self._running = False
        self._stop.set()

    # ------------------------------------------------------------------
    # usbipd lifecycle
    # ------------------------------------------------------------------

    def _start_usbipd(self) -> None:
        usbipd = usbip._find_executable("usbipd")
        if not usbipd:
            raise UsbipError("usbipd not found. Install usbip-utils or linux-tools-generic.")

        existing_pid = self._find_running_usbipd()
        if existing_pid:
            log.info("usbipd already running (PID %d), adopting it.", existing_pid)
            return

        self._usbipd_proc = subprocess.Popen(
            [usbipd],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env=usbip_environment(),
        )
        log_subprocess_output(self._usbipd_proc, "usbipd")
        time.sleep(0.8)
        if self._usbipd_proc.poll() is not None:
            raise UsbipError(f"usbipd exited immediately; see {get_log_path()} for details")

        log.info("usbipd started (PID %d).", self._usbipd_proc.pid)

    def _find_running_usbipd(self) -> Optional[int]:
        """Return PID of a running usbipd process, or None."""
        try:
            result = subprocess.run(
                ["pgrep", "-x", "usbipd"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                pids = [int(p) for p in result.stdout.strip().splitlines() if p.strip().isdigit()]
                return pids[0] if pids else None
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Binding / restore
    # ------------------------------------------------------------------

    def _restore_bindings(self) -> None:
        self._sync_wanted_with_state()
        auto_rebind_devices = [d for d in self._wanted if d.auto_rebind]
        if not auto_rebind_devices:
            log.info("No auto-rebind bindings to restore.")
            return

        log.info("Restoring %d auto-rebind binding(s)...", len(auto_rebind_devices))
        current_devices = _enumerate_sysfs_devices()
        currently_bound = set(usbip.get_bound_devices())
        claimed: set[str] = set()
        assignments = _resolve_wanted_busids(auto_rebind_devices, current_devices, claimed)

        for i, device in enumerate(auto_rebind_devices):
            busid = assignments.get(i)
            if busid is None:
                log.info("  %s [%s] — not present, will bind when plugged in.", device.busid, device.vid_pid)
                continue
            if busid in currently_bound:
                log.info("  %s [%s] — already bound.", busid, device.vid_pid)
                with self._lock:
                    self._bound[busid] = device
                claimed.add(busid)
                continue
            if self._do_bind(busid, device):
                claimed.add(busid)

    def _resolve_busid(
        self,
        device: BoundDevice,
        current_devices: dict[str, tuple[str, str]],
        excluded_busids: Optional[set[str]] = None,
    ) -> Optional[str]:
        """
        Find the current busid for a saved device.
        First tries exact busid match; then falls back to VID:PID match
        (handles the case where the device was plugged into a different port).
        """
        excluded_busids = excluded_busids or set()
        return _resolve_wanted_busids([device], current_devices, excluded_busids).get(0)

    def _do_bind(self, busid: str, device: BoundDevice) -> bool:
        """Attempt to bind a device. Returns True on success."""
        try:
            usbip.bind(busid)
            actual_sysfs = _enumerate_sysfs_devices()
            actual_vid, actual_pid = actual_sysfs.get(busid, (device.vendor_id, device.product_id))
            actual_name = device.name
            if not actual_name:
                manufacturer, product = usbip._sysfs_device_names(busid)
                actual_name = f"{manufacturer} {product}".strip() or busid
            with self._lock:
                updated = BoundDevice(
                    busid=busid,
                    vendor_id=actual_vid,
                    product_id=actual_pid,
                    name=actual_name,
                    auto_rebind=device.auto_rebind,
                )
                self._bound[busid] = updated
                self._update_wanted(device, updated)
            log.info("  %s [%s:%s] — bound successfully.", busid, actual_vid, actual_pid)
            return True
        except UsbipError as e:
            log.warning("  %s — bind failed: %s", busid, e)
            return False

    def _discover_existing_bindings(self) -> None:
        """
        Scan sysfs for devices already bound to usbip-host driver and register
        them in _bound. This handles two cases:
          1. Devices bound via the CLI service menu while the daemon was running
             (the CLI writes state.json but can't update the daemon's memory).
          2. Devices bound by a previous usbipd session that survived a daemon restart.

        Persisted state remains the source of truth for auto-rebind intent, so
        a single-session binding is tracked as active without being promoted to
        an automatic future binding.
        """
        self._sync_wanted_with_state()
        currently_bound = set(usbip.get_bound_devices())
        if not currently_bound:
            return

        with self._lock:
            already_tracked = set(self._bound.keys())

        current_devices = _enumerate_sysfs_devices()
        added = []

        for busid in currently_bound:
            if busid in already_tracked:
                continue

            vid_pid = current_devices.get(busid, ("", ""))
            vendor_id, product_id = vid_pid

            existing_wanted = self._find_wanted_for_device(busid, vendor_id, product_id)
            if existing_wanted:
                device = existing_wanted
            else:
                manufacturer, product = usbip._sysfs_device_names(busid)
                name = f"{manufacturer} {product}".strip() or busid
                device = BoundDevice(
                    busid=busid,
                    vendor_id=vendor_id,
                    product_id=product_id,
                    name=name,
                    auto_rebind=False,
                )
                log.info(
                    "Discovered existing sysfs binding: %s [%s:%s] %s",
                    busid, vendor_id, product_id, device.name,
                )

            with self._lock:
                self._bound[busid] = device
                if device.auto_rebind and device not in self._wanted:
                    self._wanted.append(device)
            added.append(device)

        if added:
            self._save_state()

    def _sync_wanted_with_state(self) -> None:
        """
        Refresh in-memory auto-rebind intent from state.json.

        The service menu edits state.json directly while the daemon is running,
        so this method prevents stale in-memory _wanted entries from rebinding
        devices after the user removed them from auto-rebind.
        """
        disk_devices = [d for d in self._state.get_bindings() if d.auto_rebind]
        with self._lock:
            bound_devices = [d for d in self._bound.values() if d.auto_rebind]
        refreshed = _refresh_remembered_bindings(disk_devices, bound_devices)
        with self._lock:
            self._wanted = refreshed

    def _update_wanted(self, old: BoundDevice, new: BoundDevice) -> None:
        """Replace an entry in _wanted (busid may have changed)."""
        for i, w in enumerate(self._wanted):
            if w is old:
                self._wanted[i] = new
                return

        if old.vendor_id and old.product_id:
            identity_matches = [
                i for i, w in enumerate(self._wanted)
                if w.vendor_id == old.vendor_id and w.product_id == old.product_id
            ]
            if len(identity_matches) == 1:
                self._wanted[identity_matches[0]] = new
                return

        for i, w in enumerate(self._wanted):
            if w.busid == old.busid and _same_device_identity(w, new.vendor_id, new.product_id):
                self._wanted[i] = new
                return
        self._wanted.append(new)

    def _find_wanted_for_device(self, busid: str, vendor_id: str, product_id: str) -> Optional[BoundDevice]:
        """Find a wanted binding, using VID:PID fallback only when unambiguous."""
        exact = next(
            (
                d for d in self._wanted
                if d.busid == busid and _same_device_identity(d, vendor_id, product_id)
            ),
            None,
        )
        if exact:
            return exact
        if not vendor_id or not product_id:
            return None
        matches = [
            d for d in self._wanted
            if d.vendor_id == vendor_id and d.product_id == product_id
        ]
        return matches[0] if len(matches) == 1 else None

    # ------------------------------------------------------------------
    # USB device monitor — auto-rebind on physical reconnect
    # ------------------------------------------------------------------

    def _start_usb_monitor(self) -> None:
        self._usb_monitor = UsbDeviceMonitor(
            on_new_devices=self._on_usb_devices_appeared,
            on_removed_devices=self._on_usb_devices_removed,
        )
        self._usb_monitor.start()
        log.info("USB device monitor started (polling every %.0fs).", UsbDeviceMonitor.POLL_INTERVAL)

    def _on_usb_devices_appeared(self, new_devices: dict[str, tuple[str, str]]) -> None:
        """
        Called when new USB devices appear on the bus.
        Check each against our wanted auto-rebind list and bind matches.
        """
        self._sync_wanted_with_state()
        with self._lock:
            wanted = [d for d in self._wanted if d.auto_rebind]
            bound_devices = [d for d in self._bound.values() if d.auto_rebind]
        current_devices = _enumerate_sysfs_devices()
        currently_bound_sysfs = set(usbip.get_bound_devices())
        claimed = set(currently_bound_sysfs)
        pending = _pending_wanted_bindings(wanted, bound_devices)
        assignments = _resolve_wanted_busids(pending, current_devices, claimed)

        for i, device in enumerate(pending):
            matched_busid = assignments.get(i)

            if matched_busid and matched_busid not in currently_bound_sysfs:
                log.info(
                    "Device reconnected: %s [%s:%s]. Auto-binding...",
                    matched_busid, device.vendor_id, device.product_id,
                )
                time.sleep(0.5)
                if self._do_bind(matched_busid, device):
                    claimed.add(matched_busid)
                self._save_state()

    def _on_usb_devices_removed(self, removed_busids: set) -> None:
        """
        Called when USB devices disappear from the bus.
        Clear them from _bound immediately so that when they are replugged
        the auto-rebind check in _on_usb_devices_appeared won't skip them.
        """
        with self._lock:
            for busid in removed_busids:
                if busid in self._bound:
                    dev = self._bound.pop(busid)
                    if dev.auto_rebind:
                        log.info(
                            "Device unplugged: %s [%s]. Cleared from active bindings — will auto-rebind when reconnected.",
                            busid, dev.vid_pid or dev.name,
                        )
                    else:
                        log.info(
                            "Device unplugged: %s [%s]. Cleared from active bindings.",
                            busid, dev.vid_pid or dev.name,
                        )

    # ------------------------------------------------------------------
    # LAN advertising
    # ------------------------------------------------------------------

    def _start_advertising(self) -> None:
        self._advertiser = DiscoveryAdvertiser(
            hostname=get_local_hostname(),
            usbip_port=USBIP_PORT,
            get_device_count=self._bound_count,
        )
        self._advertiser.start()
        log.info("LAN discovery broadcaster started.")

    def _bound_count(self) -> int:
        with self._lock:
            return len(self._bound)

    # ------------------------------------------------------------------
    # Health monitor — watches usbipd and syncs state periodically
    # ------------------------------------------------------------------

    def _start_health_monitor(self) -> None:
        self._monitor_thread = threading.Thread(
            target=self._health_loop, daemon=True, name="health-monitor"
        )
        self._monitor_thread.start()

    def _health_loop(self) -> None:
        tick = 0
        while self._running:
            try:
                if self._stop.wait(5) or not self._running:
                    return
                tick += 1

                owned_died = self._usbipd_proc is not None and self._usbipd_proc.poll() is not None
                not_running_at_all = self._find_running_usbipd() is None
                if owned_died or not_running_at_all:
                    log.warning("usbipd is not running. Restarting...")
                    self._usbipd_proc = None
                    try:
                        self._start_usbipd()
                        self._restore_bindings()
                    except UsbipError as e:
                        log.error("Failed to restart usbipd: %s", e)

                self._sync_bound_with_sysfs()
                self._rebind_wanted_unbound()

                if tick % 12 == 0:
                    self._sync_bound_with_sysfs()
                    self._save_state()
            except Exception as e:
                log.error("Health monitor error (tick %d): %s", tick, e, exc_info=True)

    def _rebind_wanted_unbound(self) -> None:
        """Re-attempt binding for any wanted devices that are in sysfs but not yet bound."""
        self._sync_wanted_with_state()
        with self._lock:
            wanted = list(self._wanted)

        if not wanted:
            return

        current_devices = _enumerate_sysfs_devices()
        currently_bound_sysfs = set(usbip.get_bound_devices())

        with self._lock:
            bound_devices = [d for d in self._bound.values() if d.auto_rebind]
        claimed = set(currently_bound_sysfs)
        pending = _pending_wanted_bindings(
            [d for d in wanted if d.auto_rebind],
            bound_devices,
        )
        assignments = _resolve_wanted_busids(pending, current_devices, claimed)

        for i, device in enumerate(pending):
            busid = assignments.get(i)
            if busid is None or busid in currently_bound_sysfs:
                continue
            log.info("Retrying bind for %s [%s]...", busid, device.vid_pid)
            if self._do_bind(busid, device):
                claimed.add(busid)
                self._save_state()

    def _sync_bound_with_sysfs(self) -> None:
        """Sync _bound with actual sysfs binding list: remove stale, add newly discovered."""
        log.debug("Running sysfs sync...")
        self._sync_wanted_with_state()
        currently_bound = set(usbip.get_bound_devices())
        current_devices = _enumerate_sysfs_devices()
        state_devices = {d.busid: d for d in self._state.get_bindings()}

        with self._lock:
            stale = [b for b in self._bound if b not in currently_bound]
            for b in stale:
                log.info("Device %s disappeared from sysfs binding list.", b)
                del self._bound[b]
            known_bound = set(self._bound.keys())

        for busid in currently_bound & known_bound:
            vid, pid = current_devices.get(busid, ("", ""))
            state_entry = state_devices.get(busid)
            existing_wanted = self._find_wanted_for_device(busid, vid, pid)
            with self._lock:
                current = self._bound.get(busid)
                if state_entry or existing_wanted:
                    self._bound[busid] = state_entry or existing_wanted
                elif current and current.auto_rebind:
                    self._bound[busid] = BoundDevice(
                        busid=busid,
                        vendor_id=vid or current.vendor_id,
                        product_id=pid or current.product_id,
                        name=current.name,
                        auto_rebind=False,
                    )

        for busid in currently_bound:
            if busid in known_bound:
                continue
            vid, pid = current_devices.get(busid, ("", ""))
            state_entry = state_devices.get(busid)
            existing_wanted = self._find_wanted_for_device(busid, vid, pid)
            if state_entry:
                device = state_entry
            elif existing_wanted:
                device = existing_wanted
            else:
                manufacturer, product = usbip._sysfs_device_names(busid)
                name = f"{manufacturer} {product}".strip() or busid
                device = BoundDevice(
                    busid=busid,
                    vendor_id=vid,
                    product_id=pid,
                    name=name,
                    auto_rebind=False,
                )
            with self._lock:
                self._bound[busid] = device
                if device.auto_rebind and not any(d.busid == busid for d in self._wanted):
                    self._wanted.append(device)
            log.info("Discovered newly bound device: %s [%s:%s] %s", busid, vid, pid, device.name)

    def _save_state(self) -> None:
        """Persist auto-rebind intent without dropping unplugged or newly added entries."""
        with self._lock:
            bound_devices = [d for d in self._bound.values() if d.auto_rebind]
        def refresh(disk_devices):
            return _refresh_remembered_bindings(
                [d for d in disk_devices if d.auto_rebind], bound_devices,
            )
        if self._state.update_bindings(refresh):
            with self._lock:
                self._wanted = self._state.get_bindings()

    # ------------------------------------------------------------------
    # Client session — auto-connect to saved hosts in the background
    # ------------------------------------------------------------------

    def _start_client_session(self) -> None:
        """
        Start a background ClientSession so the daemon auto-reconnects to
        saved hosts (with auto-connect enabled) without needing the CLI open.
        This is what makes the Use side reconnect transparently after the
        Share side reboots.
        """
        try:
            usbip.load_client_modules()
        except UsbipError as e:
            log.warning("Could not load client (vhci_hcd) modules: %s. Auto-connect disabled.", e)
            return

        self._client = ClientSession()
        self._client.start_monitoring()
        self._client.start_auto_connect()
        log.info("Client session started (background auto-connect active).")

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _shutdown(self) -> None:
        self._running = False
        self._stop.set()
        if self._monitor_thread:
            self._monitor_thread.join(timeout=35)

        if self._client:
            self._client.stop_monitoring()

        if self._usb_monitor:
            self._usb_monitor.stop()

        log.info("Saving state...")
        self._save_state()

        if self._advertiser:
            self._advertiser.stop()

        with self._lock:
            busids = list(self._bound.keys())

        log.info("Unbinding %d device(s)...", len(busids))
        for busid in busids:
            try:
                usbip.unbind(busid)
                log.info("  %s — unbound.", busid)
            except Exception as e:
                log.warning("  %s — unbind failed: %s", busid, e)

        if self._usbipd_proc and self._usbipd_proc.poll() is None:
            log.info("Stopping usbipd...")
            self._usbipd_proc.terminate()
            try:
                self._usbipd_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._usbipd_proc.kill()

        log.info("Daemon stopped cleanly.")


def main() -> None:
    if os.geteuid() != 0:
        print("ERROR: usb-portal-daemon must run as root.", file=sys.stderr)
        sys.exit(1)

    try:
        configure_logging()
    except OSError as error:
        print(f"ERROR: could not open the USBPortal log file: {error}", file=sys.stderr)
        sys.exit(1)

    GamepadDaemon().start()


if __name__ == "__main__":
    main()
