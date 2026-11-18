import threading
import unittest
from unittest.mock import patch

from usb_portal.client import ClientSession
from usb_portal.discovery import CidrScanCancelled, RemoteHost
from usb_portal.routed_discovery import RoutedDiscovery
from usb_portal.saved_hosts import SavedHost


class RoutedDiscoveryTests(unittest.TestCase):
    def test_networks_advance_round_robin_and_share_host_cache(self):
        stop = threading.Event()
        recovery = RoutedDiscovery(stop)
        with patch("usb_portal.routed_discovery.threading.Thread") as create:
            recovery.request("10.20.3.4/16")
            recovery.request("10.20.0.0/16")
            recovery.request("172.20.0.0/16")
            create.assert_called_once()

        scans = []

        def scan(cidr, *, cancel_event, on_host, include_edges):
            self.assertIs(cancel_event, stop)
            self.assertTrue(include_edges)
            scans.append(cidr)
            if len(scans) == 1:
                on_host(RemoteHost("10.20.0.5", "host", 3240, 1, machine_id="stable-id"))
            elif len(scans) == 3:
                on_host(RemoteHost("10.20.4.0", "host", 4321, 1, machine_id="stable-id"))
            elif len(scans) == 4:
                stop.set()
                raise CidrScanCancelled([])
            return []

        with patch("usb_portal.routed_discovery.scan_cidr", side_effect=scan):
            recovery._run()
        self.assertEqual(scans, ["10.20.0.0/22", "172.20.0.0/22",
                                 "10.20.4.0/22", "172.20.4.0/22"])
        cache = recovery._searches["10.20.0.0/16"].hosts
        self.assertEqual([(h.ip, h.port) for h in cache.values()], [("10.20.4.0", 4321)])

    def test_slow_search_does_not_block_requests_and_stops_cleanly(self):
        stop = threading.Event()
        entered = threading.Event()
        recovery = RoutedDiscovery(stop)

        def scan(cidr, *, cancel_event, **kwargs):
            entered.set()
            if not cancel_event.wait(2):
                self.fail("Recovery worker did not receive cancellation")
            raise CidrScanCancelled([])

        with patch("usb_portal.routed_discovery.scan_cidr", side_effect=scan):
            try:
                self.assertEqual(recovery.request("10.0.0.0/8"), [])
                self.assertTrue(entered.wait(1))
                self.assertTrue(recovery.is_alive())
                self.assertEqual(recovery.request("172.16.0.0/12"), [])
            finally:
                stop.set()
                recovery.join()
        self.assertFalse(recovery.is_alive())

    def test_disabled_or_recovered_hosts_stop_requesting_their_network(self):
        recovery = RoutedDiscovery(threading.Event())
        with patch("usb_portal.routed_discovery.threading.Thread"):
            recovery.request("10.0.0.0/8")
        recovery._searches["10.0.0.0/8"].requested_at -= 61
        with patch("usb_portal.routed_discovery.scan_cidr") as scan:
            recovery._run()
        scan.assert_not_called()
        self.assertFalse(recovery.is_alive())

    def test_client_uses_async_result_and_keeps_identity_check(self):
        session = ClientSession()
        saved = SavedHost("10.20.1.5", machine_id="expected", network="10.20.0.0/16")
        wrong = RemoteHost(saved.ip, "wrong", 3240, 1, machine_id="different")
        right = RemoteHost("10.20.200.5", "right", 4321, 1, machine_id="expected")
        with patch.object(session._routed_discovery, "request", side_effect=[[wrong], [wrong, right]]) as request, \
             patch("usb_portal.client.scan_cidr") as sync_scan:
            self.assertIsNone(session._resolve_host_via_cidr(saved))
            self.assertIs(session._resolve_host_via_cidr(saved), right)
        self.assertEqual(request.call_count, 2)
        sync_scan.assert_not_called()

    def test_partial_enterprise_results_cannot_establish_hostname_uniqueness(self):
        session = ClientSession()
        saved = SavedHost("10.20.1.5", hostname="shared-name", network="10.20.0.0/16")
        candidate = RemoteHost("10.20.200.5", "shared-name", 3240, 1)
        with patch.object(session._routed_discovery, "request", return_value=[candidate]):
            self.assertIsNone(session._resolve_host_via_cidr(saved))


if __name__ == "__main__":
    unittest.main()
