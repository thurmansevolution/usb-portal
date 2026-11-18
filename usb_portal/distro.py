"""
Linux distro and architecture detection.
Provides distro-aware install instructions for USB/IP tools.
"""

import platform
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def get_os_release() -> dict[str, str]:
    """Parse /etc/os-release into a dict."""
    result: dict[str, str] = {}
    for path in [Path("/etc/os-release"), Path("/usr/lib/os-release")]:
        if path.exists():
            for line in path.read_text().splitlines():
                line = line.strip()
                if "=" not in line or line.startswith("#"):
                    continue
                key, _, val = line.partition("=")
                result[key.strip()] = val.strip().strip('"')
            break
    return result


@lru_cache(maxsize=1)
def get_distro_family() -> str:
    """
    Return a normalised distro family name:
    'debian' | 'fedora' | 'arch' | 'suse' | 'alpine' | 'unknown'
    """
    info = get_os_release()
    id_ = info.get("ID", "").lower()
    like = info.get("ID_LIKE", "").lower()

    debian_ids = {"debian", "ubuntu", "raspbian", "linuxmint", "pop", "elementary", "kali", "parrot"}
    fedora_ids = {"fedora", "rhel", "centos", "rocky", "almalinux", "ol"}
    arch_ids = {"arch", "manjaro", "endeavouros", "garuda"}
    suse_ids = {"opensuse", "suse", "opensuse-leap", "opensuse-tumbleweed"}
    alpine_ids = {"alpine"}

    all_ids = {id_} | set(like.split())

    if all_ids & debian_ids:
        return "debian"
    if all_ids & fedora_ids:
        return "fedora"
    if all_ids & arch_ids:
        return "arch"
    if all_ids & suse_ids:
        return "suse"
    if all_ids & alpine_ids:
        return "alpine"

    return "unknown"


@lru_cache(maxsize=1)
def is_arm() -> bool:
    """Return True if the CPU is ARM (32-bit or 64-bit)."""
    machine = platform.machine().lower()
    return machine.startswith(("arm", "aarch"))


@lru_cache(maxsize=1)
def is_raspberry_pi() -> bool:
    """Return True if running on a Raspberry Pi."""
    info = get_os_release()
    if "raspbian" in info.get("ID", "").lower():
        return True
    model_path = Path("/proc/device-tree/model")
    if model_path.exists():
        try:
            return "raspberry pi" in model_path.read_text().lower()
        except OSError:
            pass
    return False


def get_distro_display_name() -> str:
    """Human-readable distro name for display."""
    info = get_os_release()
    name = info.get("PRETTY_NAME") or info.get("NAME") or "Linux"
    if is_arm():
        arch = "ARM64" if platform.machine().lower() == "aarch64" else "ARM"
        return f"{name} ({arch})"
    return name


def get_usbip_install_command() -> str:
    """Return the appropriate install command for this distro."""
    family = get_distro_family()

    if family == "debian":
        if is_arm():
            return "sudo apt install usbip usbutils"
        else:
            return "sudo apt install linux-tools-generic usbip usbutils"

    if family == "fedora":
        return "sudo dnf install usbip usbip-utils"

    if family == "arch":
        return "sudo pacman -S usbip"

    if family == "suse":
        return "sudo zypper install usbip"

    if family == "alpine":
        return "sudo apk add usbip usbip-utils"

    return "sudo apt install usbip usbutils  # adjust for your distro"


def get_modprobe_command() -> str:
    """Return the modprobe command (same across all distros)."""
    return "sudo modprobe usbip_core usbip_host vhci_hcd"


def get_platform_summary() -> dict[str, str]:
    """Return a dict of platform info for display."""
    info = get_os_release()
    return {
        "OS": get_distro_display_name(),
        "Kernel": platform.release(),
        "Architecture": platform.machine(),
        "Raspberry Pi": "Yes" if is_raspberry_pi() else "No",
    }
