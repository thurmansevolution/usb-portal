"""Incremental discovery for saved hosts on large routed networks."""

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Iterator, Optional

from usb_portal.discovery import CidrScanCancelled, RemoteHost, parse_scan_network, scan_cidr

log = logging.getLogger("usb-portal-daemon")


@dataclass
class _Search:
    chunks: Iterator
    hosts: dict[str, RemoteHost] = field(default_factory=dict)
    requested_at: float = field(default_factory=time.monotonic)
    ready_at: float = 0
    current_chunk: Optional[str] = None


class RoutedDiscovery:
    """Share a background search across saved hosts, without blocking reconnects.

    Visit /22 chunks round-robin across requested networks. This bounds the wait
    before another network gets a turn and retains the cursor across reconnect
    attempts. Each chunk uses the scanner's single socket and paced retries.
    """

    def __init__(self, stop_event: threading.Event):
        self._stop = stop_event
        self._lock = threading.Lock()
        self._searches: OrderedDict[str, _Search] = OrderedDict()
        self._thread: Optional[threading.Thread] = None

    def is_alive(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def join(self, timeout: float = 5) -> None:
        with self._lock:
            worker = self._thread
        if worker and worker is not threading.current_thread():
            worker.join(timeout=timeout)

    def request(self, cidr: str) -> list[RemoteHost]:
        network = parse_scan_network(cidr)
        key = str(network)
        with self._lock:
            if self._stop.is_set():
                return []
            search = self._searches.get(key)
            if search is None:
                search = _Search(iter(network.subnets(new_prefix=max(22, network.prefixlen))))
                self._searches[key] = search
            search.requested_at = time.monotonic()
            search.hosts = {key: host for key, host in search.hosts.items()
                            if time.time() - host.last_seen <= 90}
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True, name="routed-discovery")
                self._thread.start()
            return list(search.hosts.values())

    def _run(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            with self._lock:
                # Searches no longer requested by recovery eventually stop, e.g.
                # when auto-connect was disabled or the last host recovered.
                for key in list(self._searches):
                    if now - self._searches[key].requested_at > 60:
                        del self._searches[key]
                if not self._searches:
                    self._thread = None
                    return
                choice = next(((key, search) for key, search in self._searches.items()
                               if search.ready_at <= now), None)
                if choice:
                    cidr, search = choice
                    self._searches.move_to_end(cidr)
                    if search.current_chunk is None:
                        chunk = next(search.chunks, None)
                        if chunk is None:
                            network = parse_scan_network(cidr)
                            search.chunks = iter(network.subnets(new_prefix=max(22, network.prefixlen)))
                            search.ready_at = now + 60
                            continue
                        search.current_chunk = str(chunk)
            if choice is None:
                if self._stop.wait(0.2):
                    return
                continue

            def remember(host: RemoteHost) -> None:
                with self._lock:
                    if self._searches.get(cidr) is search:
                        search.hosts[host.machine_id or host.ip] = host

            try:
                scan_cidr(search.current_chunk, cancel_event=self._stop,
                          on_host=remember, include_edges=True)
            except CidrScanCancelled:
                return
            except Exception:
                log.exception("Background discovery failed for %s; retrying", cidr)
                with self._lock:
                    search.ready_at = time.monotonic() + 10
            else:
                with self._lock:
                    search.current_chunk = None
