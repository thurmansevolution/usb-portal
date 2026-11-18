"""
Wrapper around Linux USB/IP kernel tools (usbipd, usbip).
Handles module loading, daemon management, device binding/attaching.
"""

import os
import re
import shutil
import socket
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from usb_portal.transport import usbip_environment
from usb_portal.persistence import file_lock
from usb_portal.logging_setup import get_log_path, log_subprocess_output


@dataclass
class UsbipDevice:
    busid: str
    vendor_id: str
    product_id: str
    vendor_name: str
    product_name: str
    speed: str = ""
    status: str = "available"

    @property
    def vid_pid(self) -> str:
        return f"{self.vendor_id}:{self.product_id}"

    def __str__(self) -> str:
        return f"{self.busid} [{self.vid_pid}] {self.vendor_name} {self.product_name}"


@dataclass
class AttachedPort:
    port: int
    busid: str
    remote_host: str
    vendor_id: str
    product_id: str
    vendor_name: str = ""
    product_name: str = ""
    speed: str = ""
    remote_port: Optional[int] = None

    def __str__(self) -> str:
        return (
            f"Port {self.port}: {self.busid} [{self.vendor_id}:{self.product_id}] "
            f"{self.vendor_name} {self.product_name} <- {self.remote_host}"
        )


class UsbipError(Exception):
    pass


class UsbipNotFound(UsbipError):
    pass


class PermissionError(UsbipError):
    pass


def _find_executable(cmd: str) -> Optional[str]:
    path = shutil.which(cmd)
    if path:
        return path

    for directory in ("/usr/local/sbin", "/usr/sbin", "/sbin"):
        candidate = Path(directory) / cmd
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)

    return None


def _resolve_executable(cmd: str) -> str:
    return _find_executable(cmd) or cmd


def _run(args: list[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    resolved_args = [_resolve_executable(args[0]), *args[1:]]
    try:
        result = subprocess.run(
            resolved_args,
            capture_output=capture,
            text=True,
            timeout=8,
            env=usbip_environment() if args[0] in {"usbip", "usbipd"} else None,
        )
        if check and result.returncode != 0:
            raise UsbipError(
                result.stderr.strip() or result.stdout.strip() or f"Command failed: {' '.join(args)}"
            )
        return result
    except FileNotFoundError:
        raise UsbipNotFound(f"Command not found: {args[0]}")
    except subprocess.TimeoutExpired:
        raise UsbipError(f"Command timed out: {' '.join(args)}")


def _usbip_args(port: int, *args: str) -> list[str]:
    """Build a usbip command, preserving the default CLI shape for port 3240."""
    cmd = ["usbip"]
    if port and port != 3240:
        cmd += ["--tcp-port", str(port)]
    cmd += list(args)
    return cmd


def check_prerequisites() -> dict[str, bool]:
    """Check if required tools and modules are available."""
    checks = {
        "usbip": _find_executable("usbip") is not None,
        "usbipd": _find_executable("usbipd") is not None,
        "root": os.geteuid() == 0,
        "socket_policy": (Path(__file__).parent / "native/socket_policy.so").is_file(),
        "module_usbip_core": _is_module_loaded("usbip_core"),
        "module_usbip_host": _is_module_loaded("usbip_host"),
        "module_vhci_hcd": _is_module_loaded("vhci_hcd"),
    }
    return checks


def _is_module_loaded(name: str) -> bool:
    try:
        result = _run(["lsmod"], check=False)
        return name.replace("-", "_") in result.stdout
    except Exception:
        return False


def load_host_modules() -> None:
    """Load kernel modules required for hosting (exporting) USB devices."""
    for mod in ["usbip_core", "usbip_host"]:
        try:
            _run(["modprobe", mod])
        except UsbipError as e:
            raise UsbipError(f"Failed to load module {mod}: {e}")


def load_client_modules() -> None:
    """Load kernel modules required for client (importing) USB devices."""
    for mod in ["usbip_core", "vhci_hcd"]:
        try:
            _run(["modprobe", mod])
        except UsbipError as e:
            raise UsbipError(f"Failed to load module {mod}: {e}")


_daemon_process: Optional[subprocess.Popen] = None


def start_daemon(debug: bool = False) -> None:
    """Start the usbipd daemon in the background."""
    global _daemon_process
    if _daemon_process and _daemon_process.poll() is None:
        return

    usbipd = _find_executable("usbipd")
    if not usbipd:
        raise UsbipNotFound("usbipd not found. Install usbutils or usbip-utils package.")

    if is_daemon_running():
        return

    args = [usbipd]
    if debug:
        args.append("--debug")

    _daemon_process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        env=usbip_environment(),
    )
    log_subprocess_output(_daemon_process, "usbipd")
    time.sleep(0.5)

    if _daemon_process.poll() is not None:
        raise UsbipError(f"usbipd failed to start; see {get_log_path()} for details")


def stop_daemon() -> None:
    """Stop the usbipd daemon."""
    global _daemon_process
    if _daemon_process and _daemon_process.poll() is None:
        _daemon_process.send_signal(signal.SIGTERM)
        try:
            _daemon_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _daemon_process.kill()
    _daemon_process = None


def is_daemon_running() -> bool:
    """Check if usbipd is currently running."""
    global _daemon_process
    if _daemon_process and _daemon_process.poll() is None:
        return True
    result = _run(["pgrep", "-x", "usbipd"], check=False)
    return result.returncode == 0


def list_exportable() -> list[UsbipDevice]:
    """List USB devices available for export on this machine."""
    try:
        result = _run(["usbip", "list", "-p", "-l"])
        devices = _parse_local_list_parsable(result.stdout)
        if devices:
            try:
                human = _run(["usbip", "list", "-l"], check=False)
                _enrich_local_names(devices, human.stdout)
            except Exception:
                pass
            return devices
    except UsbipError:
        try:
            result = _run(["usbip", "list", "-l"])
            return _parse_local_list_human(result.stdout)
        except UsbipError as e:
            raise UsbipError(f"Failed to list local devices: {e}")

    return []


def _sysfs_device_names(busid: str) -> tuple[str, str]:
    """Read manufacturer and product name from sysfs for a given busid."""
    base = Path(f"/sys/bus/usb/devices/{busid}")
    manufacturer = ""
    product = ""
    try:
        manufacturer = (base / "manufacturer").read_text().strip()
    except OSError:
        pass
    try:
        product = (base / "product").read_text().strip()
    except OSError:
        pass
    return manufacturer, product


def get_local_device(busid: str) -> Optional[UsbipDevice]:
    """Return local USB metadata for a busid using sysfs, or None if unavailable."""
    try:
        for device in list_exportable():
            if device.busid == busid:
                return device
    except Exception:
        pass

    base = Path(f"/sys/bus/usb/devices/{busid}")
    if not base.exists():
        return None

    try:
        vendor_id = (base / "idVendor").read_text().strip()
        product_id = (base / "idProduct").read_text().strip()
    except OSError:
        return None

    if not vendor_id or not product_id:
        return None

    vendor_name, product_name = _sysfs_device_names(busid)
    return UsbipDevice(
        busid=busid,
        vendor_id=vendor_id,
        product_id=product_id,
        vendor_name=vendor_name,
        product_name=product_name,
    )


def _parse_local_list_parsable(output: str) -> list[UsbipDevice]:
    """Parse output of 'usbip list -p -l' (parsable format).
    Each line: busid=X-Y#usbid=VVVV:PPPP#
    """
    devices = []
    for line in output.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        busid_m = re.search(r"busid=([^\s#]+)", line)
        usbid_m = re.search(r"usbid=([0-9a-f]{4}):([0-9a-f]{4})", line)
        if busid_m and usbid_m:
            busid = busid_m.group(1)
            vendor_id = usbid_m.group(1)
            product_id = usbid_m.group(2)
            vendor_name, product_name = _sysfs_device_names(busid)
            devices.append(UsbipDevice(
                busid=busid,
                vendor_id=vendor_id,
                product_id=product_id,
                vendor_name=vendor_name,
                product_name=product_name,
            ))
    return devices


def _parse_local_list_human(output: str) -> list[UsbipDevice]:
    """Parse human-readable output of 'usbip list -l'."""
    devices = []
    lines = output.strip().splitlines()

    i = 0
    while i < len(lines):
        line = lines[i].strip()

        busid_match = re.match(r"-?\s*busid\s+(\S+)\s+\(([0-9a-f]{4}):([0-9a-f]{4})\)", line)
        if busid_match:
            busid = busid_match.group(1)
            vendor_id = busid_match.group(2)
            product_id = busid_match.group(3)

            vendor_name, product_name = _sysfs_device_names(busid)

            if not vendor_name and i + 1 < len(lines):
                name_line = lines[i + 1].strip()
                parts = name_line.split(" : ", 1)
                if len(parts) == 2:
                    vendor_name = parts[0].strip()
                    product_name = re.sub(r"\s*\([0-9a-f]{4}:[0-9a-f]{4}\)\s*$", "", parts[1]).strip()

            devices.append(UsbipDevice(
                busid=busid,
                vendor_id=vendor_id,
                product_id=product_id,
                vendor_name=vendor_name,
                product_name=product_name,
            ))
            i += 2
            continue
        i += 1

    return devices


def _enrich_local_names(devices: list[UsbipDevice], human_output: str) -> None:
    """Fill local device names from human-readable 'usbip list -l' output."""
    human_devices = _parse_local_list_human(human_output)
    by_busid = {d.busid: d for d in human_devices}
    for device in devices:
        human = by_busid.get(device.busid)
        if not human:
            continue
        if human.vendor_name:
            device.vendor_name = human.vendor_name
        if human.product_name:
            device.product_name = human.product_name


def list_remote(host: str, port: int = 3240) -> list[UsbipDevice]:
    """List USB devices available for import from a remote host."""
    try:
        parsable = _run(_usbip_args(port, "list", "-p", "-r", host))
        devices = _parse_remote_list_parsable(parsable.stdout)
        if devices:
            try:
                human = _run(_usbip_args(port, "list", "-r", host), check=False)
                _enrich_remote_names(devices, human.stdout)
            except Exception:
                pass
            return devices
    except UsbipError:
        pass

    try:
        result = _run(_usbip_args(port, "list", "-r", host))
    except UsbipError as e:
        raise UsbipError(f"Failed to list remote devices on {host}: {e}")

    return _parse_remote_list_human(result.stdout)


def _parse_remote_list_parsable(output: str) -> list[UsbipDevice]:
    """Parse parsable output of 'usbip list -p -r <host>'.
    Each line: busid=X-Y#usbid=VVVV:PPPP#
    """
    devices = []
    for line in output.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        busid_m = re.search(r"busid=([^\s#]+)", line)
        usbid_m = re.search(r"usbid=([0-9a-f]{4}):([0-9a-f]{4})", line)
        if busid_m and usbid_m:
            devices.append(UsbipDevice(
                busid=busid_m.group(1),
                vendor_id=usbid_m.group(1),
                product_id=usbid_m.group(2),
                vendor_name="",
                product_name="",
            ))
    return devices


def _enrich_remote_names(devices: list[UsbipDevice], human_output: str) -> None:
    """Fill in vendor/product names from human-readable output into already-parsed devices."""
    by_busid = {d.busid: d for d in devices}
    for line in human_output.splitlines():
        line = line.strip()
        m = re.match(r"(\d+[-\.]\S+)\s*:(.+)\(([0-9a-f]{4}):([0-9a-f]{4})\)", line)
        if m:
            busid = m.group(1).rstrip(":")
            name_str = m.group(2).strip()
            device = by_busid.get(busid)
            if device:
                parts = name_str.split(" : ", 1)
                if len(parts) == 2:
                    device.vendor_name = parts[0].strip()
                    device.product_name = parts[1].strip()
                else:
                    device.product_name = name_str


def _parse_remote_list_human(output: str) -> list[UsbipDevice]:
    """Parse human-readable output of 'usbip list -r <host>'.

    usbip-utils 2.0 format:
        Exportable USB devices
        ======================
         - 192.168.x.x
              1-1: Vendor Name : Product Name (vvvv:pppp)
                 : /sys/...
    """
    devices = []
    lines = output.strip().splitlines()

    for line in lines:
        stripped = line.strip()
        m = re.match(r"(\d+[-\.]\S+)\s*:(.+)\(([0-9a-f]{4}):([0-9a-f]{4})\)", stripped)
        if not m:
            continue

        busid = m.group(1).rstrip(":")
        name_str = m.group(2).strip()
        vendor_id = m.group(3)
        product_id = m.group(4)

        vendor_name = ""
        product_name = ""
        parts = name_str.split(" : ", 1)
        if len(parts) == 2:
            vendor_name = parts[0].strip()
            product_name = parts[1].strip()
        else:
            product_name = name_str

        devices.append(UsbipDevice(
            busid=busid,
            vendor_id=vendor_id,
            product_id=product_id,
            vendor_name=vendor_name,
            product_name=product_name,
        ))

    return devices


def bind(busid: str) -> None:
    """Bind a USB device for export via USB/IP."""
    if is_bound(busid):
        return
    if not Path(f"/sys/bus/usb/devices/{busid}").exists():
        raise UsbipError(f"Device {busid} is not present")

    try:
        _run(["usbip", "bind", "-b", busid])
    except UsbipError as e:
        if is_bound(busid):
            return
        raise UsbipError(f"Failed to bind {busid}: {e}")

    if not _wait_for_bound_state(busid, wanted=True):
        raise UsbipError(f"Failed to bind {busid}: device did not enter usbip-host state")


def unbind(busid: str) -> None:
    """Unbind a USB device from USB/IP export."""
    if not is_bound(busid):
        return

    try:
        _run(["usbip", "unbind", "-b", busid])
    except UsbipError as e:
        if not is_bound(busid):
            return
        raise UsbipError(f"Failed to unbind {busid}: {e}")

    if not _wait_for_bound_state(busid, wanted=False):
        raise UsbipError(f"Failed to unbind {busid}: device stayed bound")


def attach(host: str, busid: str, port: int = 3240) -> int:
    with file_lock(Path("/run/lock/usb-portal-attach")):
        # Serialize CLI and daemon operations against the same kernel vhci ports.
        for current in list_attached_ports(strict=True):
            if (current.remote_host == host and current.busid == busid
                    and (current.remote_port is None or current.remote_port == port)):
                return current.port
        return _attach(host, busid, port)


def _attach(host: str, busid: str, port: int) -> int:
    """
    Attach a remote USB device to this machine.
    Returns the local virtual port number.
    """
    try:
        _run(_usbip_args(port, "attach", "-r", host, "-b", busid))
    except UsbipError as e:
        raise UsbipError(f"Failed to attach {busid} from {host}: {e}")

    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    except OSError:
        addresses = set()
    addresses.add(host)
    deadline = time.monotonic() + 2
    last_error = None
    while True:
        try:
            ports = list_attached_ports(strict=True)
            last_error = None
        except UsbipError as error:
            # vhci may be unreadable while a newly attached USB device is
            # enumerating. Retry the status read, never the attach command.
            last_error = error
        else:
            for p in reversed(ports):
                if (p.remote_host in addresses and p.busid == busid
                        and (p.remote_port is None or p.remote_port == port)):
                    return p.port
        if time.monotonic() >= deadline:
            break
        time.sleep(0.1)

    if last_error is not None:
        raise UsbipError(
            f"Could not confirm the imported port for {host}/{busid}; "
            f"the device may still be attached: {last_error}"
        ) from last_error
    return -1


def detach(port: int, expected: Optional[AttachedPort] = None) -> None:
    """Detach an attached virtual USB port."""
    try:
        with file_lock(Path("/run/lock/usb-portal-attach")):
            if expected is not None and not any(
                p.port == port and p.remote_host == expected.remote_host
                and p.busid == expected.busid and p.remote_port == expected.remote_port
                and (p.vendor_id, p.product_id) == (expected.vendor_id, expected.product_id)
                for p in list_attached_ports(strict=True)
            ):
                return
            _run(["usbip", "detach", "-p", str(port)])
    except UsbipError as e:
        raise UsbipError(f"Failed to detach port {port}: {e}")


def list_attached_ports(strict: bool = False) -> list[AttachedPort]:
    """List currently attached remote USB devices."""
    try:
        result = _run(["usbip", "port"])
    except UsbipError:
        if strict:
            raise
        return []

    return _parse_port_list(result.stdout)


def _parse_port_list(output: str) -> list[AttachedPort]:
    """Parse output of 'usbip port'.

    usbip-utils 2.0 format (4 lines per device):
        Port 00: <Port in Use> at High Speed(480Mbps)
           Vendor Name : Product Name (vvvv:pppp)
           N -> usbip://host_ip/busid
              -> remote bus/dev 001/002
    """
    ports = []
    lines = output.strip().splitlines()

    i = 0
    while i < len(lines):
        line = lines[i].strip()

        port_match = re.match(r"Port\s+(\d+):", line)
        if port_match:
            port_num = int(port_match.group(1))
            vendor_id = ""
            product_id = ""
            remote_host = ""
            remote_port = None
            busid = ""
            speed = ""
            vendor_name = ""
            product_name = ""

            speed_match = re.search(r"at\s+(.+?)(?:\(|$)", line)
            if speed_match:
                speed = speed_match.group(1).strip()

            if i + 1 < len(lines):
                name_line = lines[i + 1].strip()
                vid_pid_m = re.search(r"\(([0-9a-f]{4}):([0-9a-f]{4})\)", name_line)
                if vid_pid_m:
                    vendor_id = vid_pid_m.group(1)
                    product_id = vid_pid_m.group(2)
                name_no_vidpid = re.sub(r"\s*\([0-9a-f]{4}:[0-9a-f]{4}\)\s*$", "", name_line)
                parts = name_no_vidpid.split(" : ", 1)
                if len(parts) == 2:
                    vendor_name = parts[0].strip()
                    product_name = parts[1].strip()
                else:
                    product_name = name_no_vidpid.strip()

            if i + 2 < len(lines):
                url_line = lines[i + 2].strip()
                usbip_m = re.search(r"usbip://(\[[^\]]+\]|[^/:]+)(?::(\d+))?/(\S+)", url_line)
                if usbip_m:
                    remote_host = usbip_m.group(1).strip("[]")
                    remote_port = int(usbip_m.group(2) or 3240)
                    busid = usbip_m.group(3)
                else:
                    legacy_m = re.search(r"(\d+\.\d+\.\d+\.\d+|[\w.-]+)\s*,\s*(\S+)", url_line)
                    if legacy_m:
                        remote_host = legacy_m.group(1)
                        busid = legacy_m.group(2)

            if "<Port in Use>" not in line or not remote_host or not busid:
                i += 1
                continue
            ports.append(AttachedPort(
                port=port_num,
                busid=busid,
                remote_host=remote_host,
                vendor_id=vendor_id,
                product_id=product_id,
                vendor_name=vendor_name,
                product_name=product_name,
                speed=speed,
                remote_port=remote_port,
            ))
            i += 4
            continue

        i += 1

    return ports


def get_bound_devices() -> list[str]:
    """Return list of busids currently bound for USB/IP export."""
    bound = []
    sysfs_path = Path("/sys/bus/usb/drivers/usbip-host")
    if not sysfs_path.exists():
        return bound

    for entry in sysfs_path.iterdir():
        if re.match(r"\d+-\d+", entry.name):
            bound.append(entry.name)

    return bound


def is_bound(busid: str) -> bool:
    """Return True if a busid is currently bound to usbip-host."""
    return busid in set(get_bound_devices())


def _wait_for_bound_state(busid: str, wanted: bool, timeout: float = 2.0) -> bool:
    """Wait briefly for sysfs to reflect a bind/unbind operation."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_bound(busid) is wanted:
            return True
        time.sleep(0.1)
    return is_bound(busid) is wanted
