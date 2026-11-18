"""Explicit scan ranges, stored separately from hosts and auto-connect intent."""

import json
import threading
from pathlib import Path
from typing import Optional

from usb_portal.discovery import parse_scan_network
from usb_portal.persistence import atomic_write, file_lock


CONFIG_FILE = Path("/var/lib/usb-portal/subnets.json")


class SavedSubnetsConfig:
    """Atomic, process-locked subnet CRUD; opening a menu never starts a scan."""

    def __init__(self, path: Path = CONFIG_FILE):
        self._path = path
        self._lock = threading.Lock()

    @staticmethod
    def normalise(cidr: str) -> str:
        if not isinstance(cidr, str) or "/" not in cidr:
            raise ValueError("Enter a Network ID/CIDR, such as 10.20.0.0/16.")
        return str(parse_scan_network(cidr))

    def _load_raw(self) -> dict:
        try:
            data = json.loads(self._path.read_text())
            if (not isinstance(data, dict) or type(data.get("version")) is not int
                    or data["version"] != 1 or not isinstance(data.get("subnets"), list)):
                raise ValueError("expected a version 1 object containing a subnets list")
            subnets = [self.normalise(cidr) for cidr in data["subnets"]]
            if len(set(subnets)) != len(subnets):
                raise ValueError("duplicate saved subnets")
            data["subnets"] = subnets
            return data
        except FileNotFoundError:
            return {"version": 1, "subnets": []}
        except (OSError, ValueError, TypeError) as error:
            raise OSError(f"Cannot read {self._path}: {error}") from error

    def get_all(self) -> list[str]:
        with self._lock:
            return self._load_raw()["subnets"]

    def add(self, cidr: str) -> str:
        subnet = self.normalise(cidr)
        with self._lock, file_lock(self._path):
            data = self._load_raw()
            if subnet in data["subnets"]:
                raise ValueError(f"Subnet {subnet} is already saved.")
            data["subnets"].append(subnet)
            atomic_write(self._path, data)
        return subnet

    def edit(self, old: str, new: str) -> str:
        old, subnet = self.normalise(old), self.normalise(new)
        with self._lock, file_lock(self._path):
            data = self._load_raw()
            if old not in data["subnets"]:
                raise ValueError("That subnet is no longer saved. Refresh and try again.")
            if subnet != old and subnet in data["subnets"]:
                raise ValueError(f"Subnet {subnet} is already saved.")
            data["subnets"][data["subnets"].index(old)] = subnet
            atomic_write(self._path, data)
        return subnet

    def remove(self, cidr: str) -> bool:
        subnet = self.normalise(cidr)
        with self._lock, file_lock(self._path):
            data = self._load_raw()
            if subnet not in data["subnets"]:
                return False
            data["subnets"].remove(subnet)
            atomic_write(self._path, data)
            return True


_config: Optional[SavedSubnetsConfig] = None


def get_subnets_config() -> SavedSubnetsConfig:
    global _config
    if _config is None:
        _config = SavedSubnetsConfig()
    return _config
