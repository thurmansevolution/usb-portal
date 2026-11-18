"""
USB device enumeration with game controller identification.
Uses sysfs to enumerate USB devices and classify game controllers.
"""

import fcntl
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from usb_portal.config import (
    KNOWN_CONTROLLER_VIDS,
    USB_CLASS_HID,
    HID_PROTOCOL_KEYBOARD,
    HID_PROTOCOL_MOUSE,
)
from usb_portal.usbip import UsbipDevice


@dataclass
class UsbDevice:
    busid: str
    vendor_id: str
    product_id: str
    vendor_name: str = ""
    product_name: str = ""
    manufacturer: str = ""
    product: str = ""
    serial: str = ""
    device_class: str = ""
    speed: str = ""
    interfaces: list[dict] = field(default_factory=list)

    @property
    def vid_pid(self) -> str:
        return f"{self.vendor_id}:{self.product_id}"

    @property
    def display_name(self) -> str:
        if self.product:
            return self.product
        if self.product_name:
            return self.product_name
        return f"USB Device [{self.vid_pid}]"

    @property
    def manufacturer_display(self) -> str:
        if self.manufacturer:
            return self.manufacturer
        return KNOWN_CONTROLLER_VIDS.get(self.vendor_id, self.vendor_name or "Unknown")

    def to_usbip_device(self) -> UsbipDevice:
        return UsbipDevice(
            busid=self.busid,
            vendor_id=self.vendor_id,
            product_id=self.product_id,
            vendor_name=self.manufacturer_display,
            product_name=self.display_name,
            speed=self.speed,
        )


def _read_sysfs(path: Path, default: str = "") -> str:
    try:
        return path.read_text().strip()
    except (OSError, PermissionError):
        return default


def enumerate_usb_devices() -> list[UsbDevice]:
    """Enumerate all USB devices from sysfs."""
    devices = []
    usb_root = Path("/sys/bus/usb/devices")

    if not usb_root.exists():
        return devices

    for entry in sorted(usb_root.iterdir()):
        name = entry.name
        if not name[0].isdigit():
            continue
        if ":" in name:
            continue

        vendor_id = _read_sysfs(entry / "idVendor")
        product_id = _read_sysfs(entry / "idProduct")

        if not vendor_id or not product_id:
            continue

        interfaces = _enumerate_interfaces(entry)

        device = UsbDevice(
            busid=name,
            vendor_id=vendor_id,
            product_id=product_id,
            vendor_name="",
            product_name="",
            manufacturer=_read_sysfs(entry / "manufacturer"),
            product=_read_sysfs(entry / "product"),
            serial=_read_sysfs(entry / "serial"),
            device_class=_read_sysfs(entry / "bDeviceClass"),
            speed=_read_sysfs(entry / "speed"),
            interfaces=interfaces,
        )

        devices.append(device)

    return devices


def _enumerate_interfaces(device_path: Path) -> list[dict]:
    """Enumerate USB interfaces for a device."""
    interfaces = []
    busid = device_path.name

    for entry in sorted(device_path.iterdir()):
        if not entry.name.startswith(busid + ":"):
            continue

        iface = {
            "class": _read_sysfs(entry / "bInterfaceClass"),
            "subclass": _read_sysfs(entry / "bInterfaceSubClass"),
            "protocol": _read_sysfs(entry / "bInterfaceProtocol"),
            "number": _read_sysfs(entry / "bInterfaceNumber"),
        }
        interfaces.append(iface)

    return interfaces


def is_game_controller(device: UsbDevice) -> bool:
    """
    Determine if a USB device is likely a game controller/gamepad/joystick.

    Checks multiple criteria:
    1. Known controller vendor IDs
    2. HID class with non-keyboard, non-mouse protocol
    3. Presence of joystick input device in /dev/input
    4. Joystick capabilities via evdev ioctl
    """
    if device.vendor_id in KNOWN_CONTROLLER_VIDS:
        if _has_hid_interface_not_kb_mouse(device):
            return True

    if _has_hid_gamepad_interface(device):
        return True

    if _has_joystick_input_node(device.busid):
        return True

    return False


def _has_hid_gamepad_interface(device: UsbDevice) -> bool:
    """Check if device has an HID interface that is not keyboard or mouse."""
    for iface in device.interfaces:
        if iface.get("class") == USB_CLASS_HID:
            protocol = iface.get("protocol", "")
            if protocol not in (HID_PROTOCOL_KEYBOARD, HID_PROTOCOL_MOUSE):
                return True
    return False


def _has_hid_interface_not_kb_mouse(device: UsbDevice) -> bool:
    """Check if device has any HID interface (including protocol 00)."""
    for iface in device.interfaces:
        if iface.get("class") == USB_CLASS_HID:
            protocol = iface.get("protocol", "")
            if protocol not in (HID_PROTOCOL_KEYBOARD, HID_PROTOCOL_MOUSE):
                return True
    return False


def _has_joystick_input_node(busid: str) -> bool:
    """
    Check if any /dev/input/jsN or /dev/input/eventN corresponds to this USB device
    by looking at sysfs symlinks.
    """
    input_root = Path("/sys/bus/usb/devices") / busid
    if not input_root.exists():
        return False

    for path in input_root.rglob("*/event*"):
        caps_path = path / "device" / "capabilities" / "abs"
        if caps_path.exists():
            caps = _read_sysfs(caps_path)
            try:
                if int(caps, 16) & 0x3C:
                    return True
            except ValueError:
                pass

    js_root = Path("/sys/bus/usb/devices") / busid
    for path in js_root.rglob("js*"):
        if path.name.startswith("js"):
            return True

    return False


def _check_evdev_joystick(event_path: str) -> bool:
    """Check if an evdev device has joystick/gamepad capabilities using ioctl."""
    EVIOCGBIT_ABS = 0x80404521

    try:
        with open(event_path, "rb") as f:
            buf = bytearray(8)
            fcntl.ioctl(f, EVIOCGBIT_ABS, buf)
            abs_bits = int.from_bytes(buf, "little")
            ABS_X = 0
            ABS_Y = 1
            ABS_RX = 3
            ABS_RY = 4
            joystick_axes = (1 << ABS_X) | (1 << ABS_Y)
            if abs_bits & joystick_axes:
                return True
    except Exception:
        pass
    return False


def get_game_controllers() -> list[UsbDevice]:
    """Return list of detected game controllers."""
    all_devices = enumerate_usb_devices()
    return [d for d in all_devices if is_game_controller(d)]


def find_device_by_busid(busid: str) -> Optional[UsbDevice]:
    """Find a USB device by its bus ID."""
    for device in enumerate_usb_devices():
        if device.busid == busid:
            return device
    return None


def classify_usbip_devices(usbip_devices: list[UsbipDevice]) -> list[tuple[UsbipDevice, bool]]:
    """
    Given a list of UsbipDevice objects (e.g., from remote list),
    classify each as game controller or not based on VID.
    Returns list of (device, is_controller) tuples.
    """
    results = []
    for device in usbip_devices:
        is_controller = device.vendor_id in KNOWN_CONTROLLER_VIDS
        results.append((device, is_controller))
    return results
