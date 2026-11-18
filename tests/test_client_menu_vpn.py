import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from usb_portal.cli.client_menu import _connect_by_cidr, _maybe_save_host
from usb_portal.discovery import CidrScanCancelled, RemoteHost
from usb_portal.saved_hosts import SavedHost, SavedHostsConfig


class ClientMenuVpnTests(unittest.TestCase):
    def test_cancelled_scan_offers_partial_results_without_saving_unselected_hosts(self) -> None:
        remote = RemoteHost("10.20.8.5", "host", 3240, 1, machine_id="host-id")
        with patch("usb_portal.cli.client_menu.scan_cidr", side_effect=CidrScanCancelled([remote])), \
             patch("usb_portal.cli.client_menu._maybe_save_host") as save, \
             patch("usb_portal.cli.client_menu._show_hosts_and_connect") as show:
            session = object()
            _connect_by_cidr(session, "10.20.0.0/16")
        save.assert_not_called()
        show.assert_called_once_with(session, [remote], networks={(remote.ip, remote.port): "10.20.0.0/16"})

    def test_maybe_save_host_preserves_discovered_usbip_port(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = SavedHostsConfig(Path(tmp) / "hosts.json")
            remote = RemoteHost(
                ip="10.8.0.5",
                hostname="floormodel",
                machine_id="pi-machine-id",
                port=4321,
                device_count=3,
            )

            with patch("usb_portal.cli.client_menu.get_config", return_value=config):
                _maybe_save_host(remote.ip, remote.hostname, remote=remote, network="10.8.0.0/24")

            host = config.get_by_machine_id("pi-machine-id")
            self.assertIsNotNone(host)
            self.assertEqual(host.ip, "10.8.0.5")
            self.assertEqual(host.port, 4321)
            self.assertEqual(host.network, "10.8.0.0/24")

    def test_maybe_save_host_updates_existing_host_ip_and_port_by_machine_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = SavedHostsConfig(Path(tmp) / "hosts.json")
            config.save_host(SavedHost(
                ip="10.8.0.5",
                hostname="floormodel",
                machine_id="pi-machine-id",
                network="10.8.0.0/24",
                port=3240,
            ))
            remote = RemoteHost(
                ip="10.8.0.9",
                hostname="floormodel",
                machine_id="pi-machine-id",
                port=4321,
                device_count=3,
            )

            with patch("usb_portal.cli.client_menu.get_config", return_value=config):
                _maybe_save_host(remote.ip, remote.hostname, remote=remote, network="10.8.0.0/24")

            host = config.get_by_machine_id("pi-machine-id")
            self.assertIsNotNone(host)
            self.assertEqual(host.ip, "10.8.0.9")
            self.assertEqual(host.port, 4321)


if __name__ == "__main__":
    unittest.main()
