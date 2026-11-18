"""Environment for the USB/IP tools; policy never affects unrelated programs."""

import logging
import os
from pathlib import Path

_warned = False


def usbip_environment() -> dict[str, str]:
    global _warned
    environment = os.environ.copy()
    library = Path(__file__).parent / "native" / "socket_policy.so"
    if library.is_file():
        environment["LD_PRELOAD"] = str(library)
    elif not _warned:
        logging.getLogger("usb-portal-daemon").warning(
            "USB/IP socket policy unavailable; network failure detection uses system defaults. "
            "Install with a C compiler to enable bounded VPN recovery."
        )
        _warned = True
    return environment
