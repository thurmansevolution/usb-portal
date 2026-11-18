"""
Host mode: share local game controllers over USB/IP.
Manages daemon lifecycle, device binding, and LAN advertising.
"""

import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Union

from usb_portal import usbip
from usb_portal.config import USBIP_PORT
from usb_portal.devices import UsbDevice, get_game_controllers
from usb_portal.discovery import DiscoveryAdvertiser, get_local_hostname, get_local_ip
from usb_portal.usbip import UsbipDevice, UsbipError


@dataclass
class SharedDevice:
    busid: str
    vendor_id: str
    product_id: str
    name: str
    bound_at: float = field(default_factory=time.time)

    def uptime_str(self) -> str:
        elapsed = int(time.time() - self.bound_at)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}h {m}m"
        if m:
            return f"{m}m {s}s"
        return f"{s}s"


class HostSession:
    """
    Manages the host-side USB/IP session:
    - Loads kernel modules
    - Starts/stops the usbipd daemon
    - Binds/unbinds USB devices
    - Advertises presence on LAN
    """

    def __init__(self):
        self._shared: dict[str, SharedDevice] = {}
        self._lock = threading.Lock()
        self._advertiser: Optional[DiscoveryAdvertiser] = None
        self._daemon_started = False

    def setup(self) -> None:
        """Load required kernel modules."""
        usbip.load_host_modules()

    def is_service_mode(self) -> bool:
        """Return True if the systemd service is managing usbipd/discovery."""
        try:
            from usb_portal import service as svc
            return svc.has_systemd() and svc.is_installed() and svc.is_active()
        except Exception:
            return False

    def start_daemon(self) -> None:
        """Start the usbipd export daemon (skipped if service mode is active)."""
        if self.is_service_mode():
            return
        usbip.start_daemon()
        self._daemon_started = True

    def stop_daemon(self) -> None:
        """Stop the usbipd daemon (skipped if service mode is active)."""
        if self.is_service_mode():
            return
        usbip.stop_daemon()
        self._daemon_started = False

    def is_daemon_running(self) -> bool:
        return usbip.is_daemon_running()

    def start_advertising(self) -> None:
        """Begin broadcasting LAN presence (skipped if service mode handles it)."""
        if self.is_service_mode():
            return
        if self._advertiser:
            return
        self._advertiser = DiscoveryAdvertiser(
            hostname=get_local_hostname(),
            usbip_port=USBIP_PORT,
            get_device_count=self.shared_count,
        )
        self._advertiser.start()

    def stop_advertising(self) -> None:
        if self._advertiser:
            self._advertiser.stop()
            self._advertiser = None

    def shared_count(self) -> int:
        with self._lock:
            return len(self._shared)

    def get_controllers(self) -> list[UsbDevice]:
        """List local game controllers available for sharing."""
        return get_game_controllers()

    def get_all_exportable(self) -> list[UsbipDevice]:
        """List all USB devices available for export."""
        try:
            return usbip.list_exportable()
        except UsbipError:
            return []

    def share_device(self, busid: str, device: Optional[Union[UsbDevice, UsbipDevice]] = None) -> SharedDevice:
        """Bind a USB device for USB/IP export."""
        with self._lock:
            tracked = self._shared.get(busid)
            if tracked and usbip.is_bound(busid):
                return tracked
            if tracked:
                self._shared.pop(busid, None)

        if device is None:
            device = usbip.get_local_device(busid)

        usbip.bind(busid)

        if device is None:
            device = usbip.get_local_device(busid)

        shared = self._make_shared_device(busid, device)
        with self._lock:
            self._shared[busid] = shared

        return shared

    def _make_shared_device(
        self,
        busid: str,
        device: Optional[Union[UsbDevice, UsbipDevice]] = None,
    ) -> SharedDevice:
        """Build display/state metadata for a shared busid."""
        if device is None:
            return SharedDevice(
                busid=busid,
                vendor_id="",
                product_id="",
                name="Unknown Device",
            )

        vendor_id = device.vendor_id
        product_id = device.product_id
        if hasattr(device, "manufacturer_display") and hasattr(device, "display_name"):
            name = f"{device.manufacturer_display} {device.display_name}".strip()
        else:
            name = f"{device.vendor_name} {device.product_name}".strip()

        return SharedDevice(
            busid=busid,
            vendor_id=vendor_id,
            product_id=product_id,
            name=name or "Unknown Device",
        )

    def adopt_bound_device(self, busid: str) -> SharedDevice:
        """Register an already-bound device in the current Share Mode session."""
        with self._lock:
            tracked = self._shared.get(busid)
            if tracked:
                return tracked

        shared = self._make_shared_device(busid, usbip.get_local_device(busid))
        with self._lock:
            self._shared[busid] = shared
        return shared

    def unshare_device(self, busid: str) -> None:
        """Unbind a USB device from USB/IP export."""
        usbip.unbind(busid)
        with self._lock:
            self._shared.pop(busid, None)

    def get_shared_devices(self) -> list[SharedDevice]:
        with self._lock:
            return list(self._shared.values())

    def sync_bound_devices(self) -> None:
        """Sync shared list with actually bound devices in sysfs."""
        bound = set(usbip.get_bound_devices())
        with self._lock:
            for busid in list(self._shared.keys()):
                if busid not in bound:
                    del self._shared[busid]
            tracked = set(self._shared.keys())

        for busid in sorted(bound - tracked):
            self.adopt_bound_device(busid)

    def shutdown(self) -> None:
        """Unbind all devices and stop daemon/advertising."""
        service_mode = self.is_service_mode()

        self.stop_advertising()

        with self._lock:
            busids = list(self._shared.keys())

        if not service_mode:
            for busid in busids:
                try:
                    usbip.unbind(busid)
                except Exception:
                    pass

        with self._lock:
            self._shared.clear()

        if self._daemon_started and not service_mode:
            self.stop_daemon()


_session: Optional[HostSession] = None


def get_session() -> HostSession:
    global _session
    if _session is None:
        _session = HostSession()
    return _session
