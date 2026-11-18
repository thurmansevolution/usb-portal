import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rich.console import Console

from usb_portal.cli import client_menu
from usb_portal.discovery import CidrScanCancelled, RemoteHost
from usb_portal.saved_hosts import SavedHostsConfig
from usb_portal.saved_subnets import SavedSubnetsConfig
from usb_portal.usbip import UsbipError


class DiscoveryMenuTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.hosts = SavedHostsConfig(base / "hosts.json")
        self.subnets = SavedSubnetsConfig(base / "subnets.json")
        self.session = Mock()
        for name, value in [("console", Console(file=io.StringIO())),
                            ("get_config", lambda: self.hosts),
                            ("get_subnets_config", lambda: self.subnets)]:
            patcher = patch.object(client_menu, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("clear_screen", "pause"):
            patcher = patch.object(client_menu, name)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_opening_discovery_menu_does_not_start_lan_or_subnet_scan(self):
        with patch.object(client_menu, "prompt_choice", return_value=4), \
             patch.object(client_menu, "scan_cidr") as scan:
            client_menu._discovery_menu(self.session)
        self.session.discover_hosts.assert_not_called()
        scan.assert_not_called()

    def test_lan_scan_runs_only_after_its_explicit_selection(self):
        self.session.discover_hosts.return_value = []
        with patch.object(client_menu, "prompt_choice", side_effect=[0, 4]):
            client_menu._discovery_menu(self.session)
        self.session.discover_hosts.assert_called_once_with(timeout=3.0)

    def test_scan_all_some_one_or_none(self):
        cidrs = ["10.8.0.0/24", "10.9.0.0/24", "192.168.70.0/24"]
        for cidr in cidrs:
            self.subnets.add(cidr)
        for choices, expected in [([0], cidrs), ([1, 0, 2, 3], [cidrs[0], cidrs[2]]),
                                  ([1, 1, 3], [cidrs[1]]), ([1, 0, 0, 3], []), ([2], [])]:
            with self.subTest(choices=choices), \
                 patch.object(client_menu, "prompt_choice", side_effect=choices), \
                 patch.object(client_menu, "_scan_subnets_and_connect") as scan:
                client_menu._scan_subnets_menu(self.session)
            if expected:
                scan.assert_called_once_with(self.session, expected)
            else:
                scan.assert_not_called()

    def test_empty_saved_subnets_does_not_scan(self):
        with patch.object(client_menu, "scan_cidr") as scan:
            client_menu._scan_subnets_menu(self.session)
        scan.assert_not_called()

    def test_subnet_menu_add_edit_remove_persists_without_scanning(self):
        with patch.object(client_menu, "prompt_choice", side_effect=[0, 0, 1, 0, 2, 1, 3]), \
             patch.object(client_menu, "prompt", side_effect=["10.8.0.77/24", "10.9.0.0/24", "10.10.1.1/16"]), \
             patch.object(client_menu, "prompt_yes_no", return_value=True), \
             patch.object(client_menu, "scan_cidr") as scan:
            client_menu._saved_subnets_menu()
        self.assertEqual(SavedSubnetsConfig(self.subnets._path).get_all(), ["10.10.0.0/16"])
        self.assertEqual(self.hosts.get_all(), [])
        scan.assert_not_called()

    def test_failed_subnet_write_is_reported_without_success(self):
        with patch.object(client_menu, "prompt_choice", side_effect=[0, 3]), \
             patch.object(client_menu, "prompt", return_value="10.8.0.0/24"), \
             patch("usb_portal.saved_subnets.atomic_write", side_effect=OSError("disk full")), \
             patch.object(client_menu, "print_success") as success, \
             patch.object(client_menu, "print_error") as error:
            client_menu._saved_subnets_menu()
        success.assert_not_called()
        self.assertIn("disk full", error.call_args.args[0])
        self.assertEqual(self.subnets.get_all(), [])

    def test_cancelled_batch_keeps_prior_results_and_skips_remaining_ranges(self):
        first = RemoteHost("10.8.0.5", "pi", 3240, 1)
        second = RemoteHost("10.9.0.7", "shield", 4321, 2)
        with patch.object(client_menu, "scan_cidr", side_effect=[[first], CidrScanCancelled([second])]) as scan, \
             patch.object(client_menu, "_show_hosts_and_connect") as show:
            client_menu._scan_subnets_and_connect(self.session, ["10.8.0.0/24", "10.9.0.0/24", "10.10.0.0/24"])
        self.assertEqual(scan.call_count, 2)
        show.assert_called_once_with(self.session, [first, second], networks={
            (first.ip, first.port): "10.8.0.0/24", (second.ip, second.port): "10.9.0.0/24"})
        self.assertEqual(self.hosts.get_all(), [])

    def test_overlapping_scans_deduplicate_results_and_keep_narrower_network(self):
        host = RemoteHost("10.8.0.5", "pi", 3240, 1)
        with patch.object(client_menu, "scan_cidr", return_value=[host]), \
             patch.object(client_menu, "_show_hosts_and_connect") as show:
            client_menu._scan_subnets_and_connect(self.session, ["10.8.0.0/16", "10.8.0.0/24"])
        show.assert_called_once_with(self.session, [host], networks={(host.ip, host.port): "10.8.0.0/24"})

    def test_only_selected_host_is_saved_with_its_network_and_port(self):
        first = RemoteHost("10.8.0.5", "pi", 3240, 1)
        second = RemoteHost("10.9.0.7", "shield", 4321, 2, machine_id="shield")
        networks = {(first.ip, first.port): "10.8.0.0/24", (second.ip, second.port): "10.9.0.0/24"}
        with patch.object(client_menu, "prompt_choice", return_value=1), \
             patch.object(client_menu, "_browse_host") as browse:
            client_menu._show_hosts_and_connect(self.session, [first, second], networks=networks)
        self.assertEqual([host.ip for host in self.hosts.get_all()], [second.ip])
        saved = self.hosts.get(second.ip)
        self.assertEqual((saved.port, saved.network), (4321, "10.9.0.0/24"))
        browse.assert_called_once_with(self.session, second.ip, second.hostname, second.port)

    def test_back_from_scan_results_does_not_save_any_host(self):
        with patch.object(client_menu, "prompt_choice", return_value=None), \
             patch.object(client_menu, "_browse_host") as browse:
            client_menu._show_hosts_and_connect(self.session, [RemoteHost("10.8.0.5", "pi", 3240, 1)])
        self.assertEqual(self.hosts.get_all(), [])
        browse.assert_not_called()

    def test_ip_scan_saves_reachable_selected_host_but_not_unreachable_input(self):
        for reachable in (True, False):
            self.session.list_remote_controllers.side_effect = None if reachable else UsbipError("offline")
            self.session.list_remote_controllers.return_value = []
            address = "10.8.0.5" if reachable else "10.8.0.6"
            with patch.object(client_menu, "prompt", side_effect=[address, "4321"]):
                client_menu._connect_by_ip(self.session)
            saved = self.hosts.get(address)
            if reachable:
                self.assertEqual(saved.port, 4321)
            else:
                self.assertIsNone(saved)

    def test_ip_scan_rejects_cidr_bad_address_and_bad_port_without_network_requests(self):
        for inputs in [["10.8.0.0/24"], ["hostname.invalid"], ["10.8.0.5", "0"], ["10.8.0.5", "65536"]]:
            with self.subTest(inputs=inputs), patch.object(client_menu, "prompt", side_effect=inputs):
                client_menu._connect_by_ip(self.session)
        self.session.list_remote_controllers.assert_not_called()
        self.assertEqual(self.hosts.get_all(), [])


if __name__ == "__main__":
    unittest.main()
