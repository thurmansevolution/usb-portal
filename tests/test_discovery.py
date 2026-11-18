import json
import socket
import time
import unittest
from unittest.mock import Mock, patch

from usb_portal.discovery import DiscoveryScanner


class DiscoveryTests(unittest.TestCase):
    def test_active_scan_receives_unicast_replies_on_its_own_socket(self) -> None:
        scanner = DiscoveryScanner()
        scanner._running = True
        scanner._sock = Mock()
        announcement = json.dumps({
            "app": "usb-portal", "type": "announce", "hostname": "Shield",
            "devices": 1,
        }).encode()
        with patch("usb_portal.discovery.socket.socket") as create_socket:
            query_socket = create_socket.return_value.__enter__.return_value
            query_socket.recvfrom.side_effect = [
                (b"[]", ("192.0.2.10", 3241)),
                (announcement, ("192.0.2.10", 3241)),
                socket.timeout(),
            ]
            hosts = scanner.scan_once(timeout=0.1)
        self.assertEqual([(h.ip, h.hostname) for h in hosts], [("192.0.2.10", "Shield")])
        query_socket.sendto.assert_called_once()
        scanner._sock.sendto.assert_not_called()
        create_socket.return_value.__exit__.assert_called_once()

    def test_scan_keeps_fresh_hosts_but_expires_stale_ones(self) -> None:
        scanner = DiscoveryScanner()
        scanner._running = True
        for ip in ("192.0.2.10", "192.0.2.11"):
            scanner._handle_packet(json.dumps({
                "app": "usb-portal", "type": "announce", "hostname": ip,
            }).encode(), ip)
        scanner._hosts["192.0.2.11"].last_seen = time.time() - 60
        with patch.object(scanner, "_send_query"):
            hosts = scanner.scan_once(timeout=0)
        self.assertEqual([h.ip for h in hosts], ["192.0.2.10"])

    def test_existing_host_announcement_refreshes_identity_and_port(self) -> None:
        scanner = DiscoveryScanner()

        scanner._handle_packet(
            json.dumps({
                "type": "announce",
                "app": "usb-portal",
                "hostname": "old-name",
                "machine_id": "",
                "port": 3240,
                "devices": 1,
            }).encode(),
            "10.8.0.5",
        )
        scanner._handle_packet(
            json.dumps({
                "type": "announce",
                "app": "usb-portal",
                "hostname": "floormodel",
                "machine_id": "pi-machine-id",
                "port": 4321,
                "devices": 3,
                "controllers": 2,
            }).encode(),
            "10.8.0.5",
        )

        hosts = scanner.hosts
        self.assertEqual(len(hosts), 1)
        self.assertEqual(hosts[0].hostname, "floormodel")
        self.assertEqual(hosts[0].machine_id, "pi-machine-id")
        self.assertEqual(hosts[0].port, 4321)
        self.assertEqual(hosts[0].device_count, 3)
        self.assertEqual(hosts[0].controller_count, 2)


if __name__ == "__main__":
    unittest.main()
