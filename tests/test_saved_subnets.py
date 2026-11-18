import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from usb_portal.saved_subnets import SavedSubnetsConfig


class SavedSubnetsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "subnets.json"
        self.config = SavedSubnetsConfig(self.path)

    def test_crud_survives_reopening_and_canonicalises_networks(self):
        self.assertEqual(self.config.get_all(), [])
        self.assertFalse(self.path.exists())
        self.assertEqual(self.config.add("10.20.12.34/16"), "10.20.0.0/16")
        self.config.add("192.168.70.0/24")
        self.config.edit("10.20.0.0/16", "10.21.1.7/16")
        self.assertEqual(SavedSubnetsConfig(self.path).get_all(), ["10.21.0.0/16", "192.168.70.0/24"])
        self.assertTrue(self.config.remove("192.168.70.0/24"))
        self.assertFalse(self.config.remove("192.168.70.0/24"))
        self.assertEqual(SavedSubnetsConfig(self.path).get_all(), ["10.21.0.0/16"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_private_enterprise_and_vpn_ranges_remain_supported(self):
        subnets = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"]
        for cidr in subnets:
            self.config.add(cidr)
        self.assertEqual(self.config.get_all(), subnets)

    def test_invalid_ranges_never_change_saved_subnets(self):
        self.config.add("10.8.0.0/24")
        original = self.path.read_bytes()
        for cidr in ["8.8.8.0/24", "0.0.0.0/0", "172.0.0.0/8", "fd42::/64", "10.1.2.3", "not-a-network", None]:
            with self.subTest(cidr=cidr), self.assertRaises(ValueError):
                self.config.add(cidr)
            self.assertEqual(self.path.read_bytes(), original)

    def test_duplicate_add_or_edit_preserves_both_entries(self):
        self.config.add("10.8.0.0/24")
        self.config.add("10.9.0.0/24")
        with self.assertRaises(ValueError):
            self.config.add("10.8.0.99/24")
        with self.assertRaises(ValueError):
            self.config.edit("10.9.0.0/24", "10.8.0.99/24")
        self.assertEqual(self.config.get_all(), ["10.8.0.0/24", "10.9.0.0/24"])

    def test_stale_edit_cannot_recreate_removed_subnet(self):
        self.config.add("10.8.0.0/24")
        other = SavedSubnetsConfig(self.path)
        other.remove("10.8.0.0/24")
        with self.assertRaises(ValueError):
            self.config.edit("10.8.0.0/24", "10.9.0.0/24")
        self.assertEqual(self.config.get_all(), [])

    def test_corrupt_or_unknown_format_is_reported_and_never_overwritten(self):
        for data in ["broken", "[]", '{"version": 2, "subnets": []}',
                     '{"version": 1, "subnets": ["8.8.8.0/24"]}',
                     '{"version": 1, "subnets": ["10.8.0.0/24", "10.8.0.9/24]}',
                     '{"version": 1, "subnets": [{}]}']:
            self.path.write_text(data)
            with self.subTest(data=data), self.assertRaises(OSError):
                self.config.add("10.9.0.0/24")
            self.assertEqual(self.path.read_text(), data)

    def test_failed_write_retains_previous_configuration(self):
        self.config.add("10.8.0.0/24")
        original = self.path.read_bytes()
        with patch("usb_portal.saved_subnets.atomic_write", side_effect=OSError("disk full")):
            for operation in [lambda: self.config.add("10.9.0.0/24"),
                              lambda: self.config.edit("10.8.0.0/24", "10.9.0.0/24"),
                              lambda: self.config.remove("10.8.0.0/24")]:
                with self.assertRaises(OSError):
                    operation()
                self.assertEqual(self.path.read_bytes(), original)

    def test_concurrent_instances_preserve_independent_additions(self):
        subnets = [f"10.{index}.0.0/16" for index in range(12)]
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda cidr: SavedSubnetsConfig(self.path).add(cidr), subnets))
        self.assertEqual(set(self.config.get_all()), set(subnets))
        self.assertEqual(json.loads(self.path.read_text())["version"], 1)


if __name__ == "__main__":
    unittest.main()
