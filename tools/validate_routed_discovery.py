"""Exercise candidate discovery through a wholly isolated Linux router."""
import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path


def command(*args):
    subprocess.run(args, check=True, capture_output=True, text=True)


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: sudo python3 tools/validate_routed_discovery.py /path/to/USBPortal")
    if os.geteuid() != 0:
        raise SystemExit("Run with sudo to create the isolated network namespaces")
    source = str(Path(sys.argv[1]).resolve())
    if not (Path(source) / "usb_portal/discovery.py").is_file():
        raise SystemExit("The source path must contain usb_portal/discovery.py")
    suffix = str(os.getpid())
    client, router, host = ["up-" + role + "-" + suffix for role in ("client", "router", "host")]
    created = []
    server = None
    try:
        for namespace in (client, router, host):
            command("ip", "netns", "add", namespace)
            created.append(namespace)
            command("ip", "-n", namespace, "link", "set", "lo", "up")
        command("ip", "-n", client, "link", "add", "uplink", "type", "veth", "peer", "name", "lan")
        command("ip", "-n", client, "link", "set", "lan", "netns", router)
        command("ip", "-n", router, "link", "add", "remote", "type", "veth", "peer", "name", "uplink")
        command("ip", "-n", router, "link", "set", "uplink", "netns", host)
        for namespace, interface, address in (
            (client, "uplink", "10.254.10.2/24"), (router, "lan", "10.254.10.1/24"),
            (router, "remote", "172.31.252.1/24"), (host, "uplink", "172.31.252.2/24"),
        ):
            command("ip", "-n", namespace, "address", "add", address, "dev", interface)
            command("ip", "-n", namespace, "link", "set", interface, "up")
        command("ip", "-n", client, "route", "add", "default", "via", "10.254.10.1")
        command("ip", "-n", host, "route", "add", "default", "via", "172.31.252.1")
        command("ip", "netns", "exec", router, "sysctl", "-qw", "net.ipv4.ip_forward=1")
        server_code = '''
import signal, sys
sys.path.insert(0, sys.argv[1])
from usb_portal.discovery import DiscoveryAdvertiser
advertiser = DiscoveryAdvertiser("routed-validation", 4321, lambda: 2)
advertiser.start()
print("ready", flush=True)
signal.signal(signal.SIGTERM, lambda *args: sys.exit(0))
try:
    signal.pause()
finally:
    advertiser.stop()
'''
        server = subprocess.Popen(["ip", "netns", "exec", host, "python3", "-u", "-c", server_code, source],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        readable, _, _ = select.select([server.stdout], [], [], 5)
        if not readable or server.stdout.readline().strip() != "ready":
            raise RuntimeError("Isolated discovery server failed to start")
        scanner_code = '''
import json, sys, time
sys.path.insert(0, sys.argv[1])
from usb_portal.discovery import scan_cidr
last = [0]
def progress(state):
    if state.elapsed - last[0] >= 15:
        print(json.dumps({"elapsed_s": round(state.elapsed, 2), "addresses_considered": state.completed,
                          "total": state.total, "found": state.found}), flush=True)
        last[0] = state.elapsed
started = time.monotonic()
hosts = scan_cidr("172.31.0.0/16", on_progress=progress)
result = {"elapsed_s": round(time.monotonic() - started, 3),
          "hosts": [{"ip": h.ip, "hostname": h.hostname, "port": h.port, "devices": h.device_count} for h in hosts]}
assert result["hosts"] == [{"ip": "172.31.252.2", "hostname": "routed-validation", "port": 4321, "devices": 2}], result
print(json.dumps({"result": result}), flush=True)
'''
        subprocess.run(["ip", "netns", "exec", client, "python3", "-u", "-c", scanner_code, source],
                       check=True, timeout=190)
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=4)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
        for namespace in reversed(created):
            subprocess.run(["ip", "netns", "delete", namespace], check=True)
        print(json.dumps({"isolated_namespaces_removed": created}), flush=True)


if __name__ == "__main__":
    main()
