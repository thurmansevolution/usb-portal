"""
systemd service management for usb|portal.
Handles installing, enabling, starting, stopping, and removing the service.
"""

import shutil
import subprocess
from pathlib import Path
from typing import Optional

UNIT_NAME = "usb-portal"
UNIT_FILE = Path(f"/etc/systemd/system/{UNIT_NAME}.service")
STATE_DIR = Path("/var/lib/usb-portal")
_LEGACY_UNIT_NAME = "gamepad-share"
_LEGACY_STATE_DIR = Path("/var/lib/gamepad-share")


class ServiceError(Exception):
    pass


def _run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True, text=True, timeout=15)
    if check and result.returncode != 0:
        raise ServiceError(result.stderr.strip() or result.stdout.strip())
    return result


def _find_daemon_executable() -> str:
    """Locate the usb-portal-daemon binary."""
    for name in ("usb-portal-daemon",):
        path = shutil.which(name)
        if path:
            return path
    candidates = [
        "/usr/local/bin/usb-portal-daemon",
        "/usr/bin/usb-portal-daemon",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    raise ServiceError(
        "usb-portal-daemon not found in PATH. "
        "Run: sudo ./install.sh install"
    )


def _unit_content(daemon_path: str) -> str:
    return f"""[Unit]
Description=usb|portal — USB game controller sharing over LAN
After=network-online.target usbipd.service
Wants=network-online.target

[Service]
Type=simple
ExecStart={daemon_path}
Restart=on-failure
RestartSec=5
TimeoutStopSec=60
UMask=0077
PrivateTmp=true
ProtectHome=true
ProtectSystem=full
NoNewPrivileges=true
RestrictSUIDSGID=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
StandardOutput=journal
StandardError=journal
SyslogIdentifier=usb-portal
Nice=-5
IOSchedulingClass=best-effort
IOSchedulingPriority=2

[Install]
WantedBy=multi-user.target
"""


def _migrate_old_install() -> None:
    """Stop/remove legacy service and migrate state dir if needed."""
    old_unit = Path(f"/etc/systemd/system/{_LEGACY_UNIT_NAME}.service")
    try:
        _run(["systemctl", "stop", _LEGACY_UNIT_NAME], check=False)
        _run(["systemctl", "disable", _LEGACY_UNIT_NAME], check=False)
    except Exception:
        pass
    if old_unit.exists():
        old_unit.unlink()
        _run(["systemctl", "daemon-reload"], check=False)
    if _LEGACY_STATE_DIR.exists() and not STATE_DIR.exists():
        import shutil as _shutil
        _shutil.move(str(_LEGACY_STATE_DIR), str(STATE_DIR))


def install(daemon_path: Optional[str] = None) -> None:
    """Write the systemd unit file and enable the service."""
    if daemon_path is None:
        daemon_path = _find_daemon_executable()

    _migrate_old_install()
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    try:
        UNIT_FILE.write_text(_unit_content(daemon_path))
    except OSError as e:
        raise ServiceError(f"Could not write unit file: {e}")

    _run(["systemctl", "daemon-reload"])
    _run(["systemctl", "enable", UNIT_NAME])


def uninstall() -> None:
    """Stop, disable, and remove the service unit file."""
    if is_active():
        try:
            stop()
        except ServiceError:
            pass

    _run(["systemctl", "disable", UNIT_NAME], check=False)

    if UNIT_FILE.exists():
        UNIT_FILE.unlink()

    _migrate_old_install()
    _run(["systemctl", "daemon-reload"], check=False)


def start() -> None:
    """Start the systemd service."""
    _run(["systemctl", "start", UNIT_NAME])


def stop() -> None:
    """Stop the systemd service."""
    _run(["systemctl", "stop", UNIT_NAME])


def restart() -> None:
    """Restart the systemd service."""
    _run(["systemctl", "restart", UNIT_NAME])


def is_installed() -> bool:
    """Return True if the unit file exists."""
    return UNIT_FILE.exists()


def is_active() -> bool:
    """Return True if the service is currently running."""
    result = _run(["systemctl", "is-active", "--quiet", UNIT_NAME], check=False)
    return result.returncode == 0


def is_enabled() -> bool:
    """Return True if the service is enabled to start at boot."""
    result = _run(["systemctl", "is-enabled", "--quiet", UNIT_NAME], check=False)
    return result.returncode == 0


def get_status() -> str:
    """Return the full systemctl status output."""
    result = _run(["systemctl", "status", UNIT_NAME, "--no-pager"], check=False)
    return result.stdout or result.stderr


def get_logs(lines: int = 30) -> str:
    """Return recent application logs; systemd's own events remain in its journal."""
    from usb_portal.logging_setup import read_recent_logs
    return read_recent_logs(lines)


def has_systemd() -> bool:
    """Check if systemd is available on this system."""
    return shutil.which("systemctl") is not None
