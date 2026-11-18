import json
import threading
import tracemalloc
import unittest
from collections import Counter, deque
from contextlib import ExitStack
from unittest.mock import patch

from usb_portal.discovery import CidrScanCancelled, parse_scan_network, scan_cidr


class SimulatedNetwork:
    """A clocked datagram network: no real enterprise address space is probed."""

    def __init__(self, responders=None, noise=False):
        self.now = 0.0
        self.responders = responders or {}
        self.queries = Counter()
        self.sent = 0
        self.packets = deque()
        self.closed = False
        self.noise = noise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def setblocking(self, value):
        pass

    def setsockopt(self, *args):
        pass

    def bind(self, address):
        pass

    def select(self, readers, writers, errors, timeout):
        if self.packets:
            return readers, [], []
        self.now += timeout
        return [], [], []

    def sendto(self, data, address):
        ip, port = address
        self.sent += 1
        if self.noise:
            self.noise = False
            # Malformed, oversized, outside the range, and not yet queried.
            self.packets.extend([
                (b"[]", address),
                (b"x" * 2049, address),
                (self.announce(), ("192.168.9.1", port)),
                (self.announce(), ("10.31.255.253", port)),
            ])
        if ip in self.responders:
            self.queries[ip] += 1
            if self.queries[ip] >= self.responders[ip]:
                self.packets.append((self.announce(), address))
        return len(data)

    @staticmethod
    def announce():
        return json.dumps({
            "type": "announce", "app": "usb-portal", "hostname": "routed-host",
            "machine_id": "host-id", "port": 4321, "devices": 2,
        }).encode()

    def recvfrom(self, size):
        if not self.packets:
            raise BlockingIOError()
        return self.packets.popleft()

    def patches(self):
        stack = ExitStack()
        stack.enter_context(patch("usb_portal.discovery.socket.socket", return_value=self))
        stack.enter_context(patch("usb_portal.discovery.select.select", side_effect=self.select))
        stack.enter_context(patch("usb_portal.discovery.time.monotonic", side_effect=lambda: self.now))
        stack.enter_context(patch("usb_portal.discovery._get_local_ips", return_value=set()))
        return stack


class CidrDiscoveryTests(unittest.TestCase):
    def test_private_enterprise_ranges_normalize_without_enumeration(self):
        for cidr, expected in [
            ("10.42.3.4/8", "10.0.0.0/8"),
            ("172.16.0.0/12", "172.16.0.0/12"),
            ("192.168.0.0/16", "192.168.0.0/16"),
            ("100.64.0.0/10", "100.64.0.0/10"),
        ]:
            self.assertEqual(str(parse_scan_network(cidr)), expected)

    def test_invalid_public_mixed_and_ipv6_ranges_never_open_a_socket(self):
        with patch("usb_portal.discovery.socket.socket") as create:
            for cidr in ("bad", "10.0.0.0/7", "192.168.0.0/15", "0.0.0.0/0",
                         "8.8.8.0/24", "127.0.0.1/32", "fd00::/64"):
                with self.subTest(cidr=cidr), self.assertRaises(ValueError):
                    scan_cidr(cidr)
            create.assert_not_called()

    def test_sixteen_bit_scan_reaches_far_end_and_ignores_noise(self):
        network = SimulatedNetwork({"10.31.255.254": 1}, noise=True)
        progress = []
        with network.patches():
            hosts = scan_cidr("10.31.0.0/16", on_progress=progress.append)
        self.assertEqual([(h.ip, h.port, h.device_count) for h in hosts],
                         [("10.31.255.254", 4321, 2)])
        self.assertEqual(network.sent, 2 * 65534 - 1)
        self.assertEqual(network.queries["10.31.255.254"], 1)
        self.assertEqual(progress[-1].completed, progress[-1].total)
        # A /16 is paced at 1,000 queries/s, with final reply waits, not 25 minutes.
        self.assertGreater(network.now, 130)
        self.assertLess(network.now, 135)
        self.assertTrue(network.closed)

    def test_lost_query_retried_and_successful_host_not_queried_again(self):
        network = SimulatedNetwork({"10.8.0.1": 1, "10.8.0.2": 2})
        announced = []
        with network.patches():
            hosts = scan_cidr("10.8.0.0/30", on_host=announced.append)
        self.assertEqual({host.ip for host in hosts}, {"10.8.0.1", "10.8.0.2"})
        self.assertEqual(network.queries, {"10.8.0.1": 1, "10.8.0.2": 2})
        self.assertEqual(len(announced), 2)

    def test_stop_retains_received_hosts_and_closes_socket(self):
        stop = threading.Event()
        network = SimulatedNetwork({"10.0.0.1": 1})
        with network.patches(), self.assertRaises(CidrScanCancelled) as caught:
            scan_cidr("10.0.0.0/8", cancel_event=stop, on_host=lambda host: stop.set())
        self.assertEqual([host.ip for host in caught.exception.hosts], ["10.0.0.1"])
        self.assertLessEqual(network.sent, 32)
        self.assertTrue(network.closed)

    def test_huge_range_can_stop_with_bounded_memory_before_any_reply(self):
        stop = threading.Event()
        network = SimulatedNetwork()

        def update(progress):
            if progress.completed >= 2000:
                stop.set()

        with network.patches():
            tracemalloc.start()
            try:
                with self.assertRaises(CidrScanCancelled):
                    scan_cidr("10.0.0.0/8", cancel_event=stop, on_progress=update)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
        self.assertLess(peak, 2 * 1024 * 1024)
        self.assertLess(network.sent, 2500)
        self.assertTrue(network.closed)

    def test_keyboard_interrupt_also_closes_socket_and_retains_results(self):
        network = SimulatedNetwork({"10.8.0.1": 1})

        def interrupt(host):
            raise KeyboardInterrupt()

        with network.patches(), self.assertRaises(CidrScanCancelled) as caught:
            scan_cidr("10.8.0.0/24", on_host=interrupt)
        self.assertEqual(len(caught.exception.hosts), 1)
        self.assertTrue(network.closed)

    def test_point_to_point_single_host_and_chunk_boundary_addresses(self):
        for cidr, responder, include_edges in [
            ("10.8.0.0/31", "10.8.0.0", False),
            ("10.8.0.5/32", "10.8.0.5", False),
            ("10.8.4.0/22", "10.8.4.0", True),
            ("10.8.4.0/22", "10.8.7.255", True),
        ]:
            with self.subTest(cidr=cidr, responder=responder):
                network = SimulatedNetwork({responder: 1})
                with network.patches():
                    hosts = scan_cidr(cidr, include_edges=include_edges)
                self.assertEqual([host.ip for host in hosts], [responder])


if __name__ == "__main__":
    unittest.main()
