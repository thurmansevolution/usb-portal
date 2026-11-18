#!/usr/bin/env python3
"""Exercise the test fixture with a real Linux USB/IP/HID/input stack.

Start `bash android/test.sh --serve 3248` first. Run this on the same Linux
machine with permission to run usbip and read input events (e.g. sudo python3).
Only the test fixture's exact URL is ever detached. No real USB input is sent.
"""
import os
import re
import select
import struct
import subprocess
import time
from pathlib import Path


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def imports():
    return run("usbip", "port")


def fixture_port():
    for block in re.split(r"(?=Port \d+:)", imports()):
        if "usbip://127.0.0.1:3248/1-50" in block:
            return str(int(re.search(r"Port (\d+):", block).group(1)))
    return None


def main():
    if fixture_port() is not None:
        raise SystemExit("The fixture is already imported; detach it before running this test.")
    initial = set(re.findall(r"usbip://\S+", imports()))
    try:
        for iteration in range(2):
            run("usbip", "--tcp-port", "3248", "attach", "-r", "127.0.0.1", "-b", "1-50")
            event = None
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                for item in Path("/sys/class/input").glob("event*"):
                    ids = item / "device/id"
                    try:
                        if (ids / "vendor").read_text().strip() == "cafe" and (ids / "product").read_text().strip() == "4001":
                            event = "/dev/input/" + item.name
                            break
                    except OSError:
                        pass
                if event:
                    break
                time.sleep(0.1)
            assert event, "Linux did not enumerate the synthetic gamepad"
            fd = os.open(event, os.O_RDONLY | os.O_NONBLOCK)
            try:
                values, count = set(), 0
                event_size = struct.calcsize("@llHHi")
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if not select.select([fd], [], [], 0.2)[0]:
                        continue
                    data = os.read(fd, event_size * 64)
                    for offset in range(0, len(data), event_size):
                        _, _, kind, code, value = struct.unpack("@llHHi", data[offset:offset + event_size])
                        if kind == 1 and 0x120 <= code <= 0x13F:
                            values.add(value)
                            count += 1
                assert values == {0, 1}, ("No real press/release input events", values)
                print(f"Cycle {iteration + 1}: {event}, {count} button transitions, press/release verified", flush=True)
            finally:
                os.close(fd)
            port = fixture_port()
            assert port is not None
            run("usbip", "detach", "-p", port)
            time.sleep(0.5)
            assert fixture_port() is None
        assert set(re.findall(r"usbip://\S+", imports())) == initial, "Other imports changed"
        print("Both native-kernel cycles passed; other USB/IP imports preserved", flush=True)
    finally:
        port = fixture_port()
        if port is not None:
            run("usbip", "detach", "-p", port)


if __name__ == "__main__":
    main()
