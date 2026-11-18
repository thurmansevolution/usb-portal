import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from usb_portal.client import (
    ActiveConnection,
    ClientSession,
    _preferred_device_name,
    _resolve_auto_connect_devices,
)
from usb_portal.discovery import RemoteHost
from usb_portal.saved_hosts import AutoConnectDevice, SavedHost, SavedHostsConfig
from usb_portal.usbip import AttachedPort, UsbipDevice, UsbipError


def remote(busid: str, vendor_id: str, product_id: str) -> UsbipDevice:
    return UsbipDevice(
        busid=busid,
        vendor_id=vendor_id,
        product_id=product_id,
        vendor_name="",
        product_name="",
    )


def named_remote(
    busid: str,
    vendor_id: str,
    product_id: str,
    vendor_name: str,
    product_name: str,
) -> UsbipDevice:
    return UsbipDevice(
        busid=busid,
        vendor_id=vendor_id,
        product_id=product_id,
        vendor_name=vendor_name,
        product_name=product_name,
    )


class ClientAutoConnectTests(unittest.TestCase):
    def test_saved_hosts_save_host_reports_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = SavedHostsConfig(Path(tmp) / "hosts.json")

            self.assertTrue(config.save_host(SavedHost(ip="192.168.71.49", hostname="floormodel")))
            self.assertEqual(config.get("192.168.71.49").hostname, "floormodel")

    def test_saved_hosts_atomic_write_preserves_existing_file_on_replace_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hosts.json"
            config = SavedHostsConfig(path)
            self.assertTrue(config.save_host(SavedHost(ip="192.168.71.49", hostname="floormodel")))

            with patch.object(Path, "replace", side_effect=OSError("disk full")):
                result = config.save_host(SavedHost(ip="192.168.71.50", hostname="other"))

            self.assertFalse(result)
            self.assertIsNotNone(config.get("192.168.71.49"))
            self.assertIsNone(config.get("192.168.71.50"))
            self.assertFalse((path.parent / ".hosts.json.tmp").exists())

    def test_saved_hosts_allows_identical_vidpid_devices_by_busid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = SavedHostsConfig(Path(tmp) / "hosts.json")

            config.add_auto_connect_device(
                "192.168.71.49",
                AutoConnectDevice("1-1.3.3", "0079", "1804", "SNES A"),
            )
            config.add_auto_connect_device(
                "192.168.71.49",
                AutoConnectDevice("1-1.3.4", "0079", "1804", "SNES B"),
            )

            host = config.get("192.168.71.49")
            self.assertIsNotNone(host)
            self.assertEqual([d.busid for d in host.auto_connect], ["1-1.3.3", "1-1.3.4"])

    def test_connect_raises_when_attach_port_cannot_be_confirmed(self) -> None:
        session = ClientSession()

        with patch("usb_portal.client.usbip.attach", return_value=-1):
            with self.assertRaises(UsbipError):
                session.connect("192.168.71.49", "1-1.3.3")

        self.assertEqual(session.get_connections(), [])

    def test_connect_returns_existing_live_connection_without_duplicate_attach(self) -> None:
        session = ClientSession()
        existing = ActiveConnection(
            host_ip="192.168.71.49",
            host_name="floormodel",
            busid="1-1.3.3",
            port=2,
            device_name="SNES",
            vendor_id="0079",
            product_id="1804",
        )
        session._connections[2] = existing

        with patch("usb_portal.client.usbip.list_attached_ports", return_value=[
            AttachedPort(
                port=2,
                busid="1-1.3.3",
                remote_host="192.168.71.49",
                vendor_id="0079",
                product_id="1804",
            )
        ]), \
             patch("usb_portal.client.usbip.attach") as attach:
            conn = session.connect(
                "192.168.71.49",
                "1-1.3.3",
                vendor_id="0079",
                product_id="1804",
                reconnect=True,
            )

        self.assertIs(conn, existing)
        attach.assert_not_called()

    def test_connect_adopts_existing_kernel_connection_without_duplicate_attach(self) -> None:
        session = ClientSession()

        with patch("usb_portal.client.usbip.list_attached_ports", return_value=[
            AttachedPort(
                port=7,
                busid="1-1.3.3",
                remote_host="192.168.71.49",
                vendor_id="0079",
                product_id="1804",
                vendor_name="HuiJia",
                product_name="SNES",
            )
        ]), \
             patch("usb_portal.client.usbip.attach") as attach:
            conn = session.connect(
                "192.168.71.49",
                "1-1.3.3",
                host_name="floormodel",
                vendor_id="0079",
                product_id="1804",
            )

        attach.assert_not_called()
        self.assertEqual(conn.port, 7)
        self.assertEqual(conn.host_name, "floormodel")
        self.assertIn(7, session._connections)

    def test_connect_prunes_stale_duplicate_before_attach(self) -> None:
        session = ClientSession()
        session._connections[2] = ActiveConnection(
            host_ip="192.168.71.49",
            host_name="floormodel",
            busid="1-1.3.3",
            port=2,
            device_name="SNES",
            vendor_id="0079",
            product_id="1804",
        )

        with patch("usb_portal.client.usbip.list_attached_ports", return_value=[]), \
             patch("usb_portal.client.usbip.attach", return_value=4):
            conn = session.connect(
                "192.168.71.49",
                "1-1.3.3",
                vendor_id="0079",
                product_id="1804",
            )

        self.assertEqual(conn.port, 4)
        self.assertNotIn(2, session._connections)
        self.assertIn(4, session._connections)

    def test_disconnect_is_idempotent_when_kernel_port_is_already_gone(self) -> None:
        session = ClientSession()
        session._connections[2] = ActiveConnection(
            host_ip="192.168.71.49",
            host_name="floormodel",
            busid="1-1.3.3",
            port=2,
            device_name="SNES",
        )

        with patch("usb_portal.client.usbip.list_attached_ports", return_value=[]), \
             patch("usb_portal.client.usbip.detach") as detach:
            session.disconnect(2)

        detach.assert_not_called()
        self.assertEqual(session.get_connections(), [])

    def test_start_auto_connect_is_singleton(self) -> None:
        session = ClientSession()

        class AliveThread:
            def is_alive(self):
                return True

        session._auto_connect_thread = AliveThread()
        with patch.object(session, "start_monitoring") as start_monitoring:
            session.start_auto_connect()

        start_monitoring.assert_not_called()
        self.assertIsInstance(session._auto_connect_thread, AliveThread)

    def test_stop_monitoring_does_not_join_current_auto_connect_thread(self) -> None:
        session = ClientSession()
        session._running = True
        current = threading.current_thread()
        session._auto_connect_thread = current

        with patch.object(session._scanner, "stop_listening"):
            session.stop_monitoring()

        self.assertIs(session._auto_connect_thread, current)

    def test_resolve_auto_connect_devices_allocates_duplicate_serialless_devices(self) -> None:
        saved = [
            AutoConnectDevice("1-1.3.1", "0079", "1804", "SNES A"),
            AutoConnectDevice("1-1.3.2", "0079", "1804", "SNES B"),
        ]
        remote_devices = [
            remote("1-1.3.3", "0079", "1804"),
            remote("1-1.3.4", "0079", "1804"),
        ]

        assignments = _resolve_auto_connect_devices(saved, remote_devices, set())

        self.assertEqual(
            [(saved.busid, remote.busid) for saved, remote in assignments],
            [("1-1.3.1", "1-1.3.3"), ("1-1.3.2", "1-1.3.4")],
        )

    def test_resolve_auto_connect_devices_keeps_single_duplicate_match_ambiguous(self) -> None:
        saved = [
            AutoConnectDevice("1-1.3.1", "0079", "1804", "SNES"),
        ]
        remote_devices = [
            remote("1-1.3.3", "0079", "1804"),
            remote("1-1.3.4", "0079", "1804"),
        ]

        self.assertEqual(_resolve_auto_connect_devices(saved, remote_devices, set()), [])

    def test_resolve_auto_connect_devices_skips_already_connected_busids(self) -> None:
        saved = [
            AutoConnectDevice("1-1.3.3", "0079", "1804", "SNES A"),
            AutoConnectDevice("1-1.3.4", "0079", "1804", "SNES B"),
        ]
        remote_devices = [
            remote("1-1.3.3", "0079", "1804"),
            remote("1-1.3.4", "0079", "1804"),
        ]

        assignments = _resolve_auto_connect_devices(saved, remote_devices, {"1-1.3.3"})

        self.assertEqual(
            [(saved.busid, remote.busid) for saved, remote in assignments],
            [("1-1.3.4", "1-1.3.4")],
        )

    def test_reconnect_resolve_busid_checks_identity_before_trusting_saved_busid(self) -> None:
        session = ClientSession()
        conn = ActiveConnection(
            host_ip="192.168.71.49",
            host_name="floormodel",
            busid="1-1.3.1",
            port=0,
            device_name="N64",
            vendor_id="2563",
            product_id="0575",
        )
        remote_devices = [
            remote("1-1.3.1", "0079", "1804"),
            remote("1-1.3.2", "2563", "0575"),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            config = SavedHostsConfig(Path(tmp) / "hosts.json")
            config.save_host(SavedHost(
                ip="192.168.71.49",
                auto_connect=[
                    AutoConnectDevice("1-1.3.1", "0079", "1804", "SNES"),
                    AutoConnectDevice("1-1.3.1", "2563", "0575", "N64"),
                ],
            ))

            with patch("usb_portal.client.usbip.list_remote", return_value=remote_devices), \
                 patch("usb_portal.saved_hosts.get_config", return_value=config):
                self.assertEqual(session._resolve_remote_busid(conn), "1-1.3.2")

    def test_update_saved_busid_uses_identity_when_old_busid_is_duplicated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = SavedHostsConfig(Path(tmp) / "hosts.json")
            config.save_host(SavedHost(
                ip="192.168.71.49",
                auto_connect=[
                    AutoConnectDevice("1-1.3.1", "0079", "1804", "SNES"),
                    AutoConnectDevice("1-1.3.1", "2563", "0575", "N64"),
                ],
            ))

            session = ClientSession()
            with patch("usb_portal.saved_hosts.get_config", return_value=config):
                session._update_saved_busid("192.168.71.49", "1-1.3.1", "1-1.3.2", "2563", "0575")

            host = config.get("192.168.71.49")
            self.assertEqual(
                [(d.busid, d.vid_pid) for d in host.auto_connect],
                [("1-1.3.1", "0079:1804"), ("1-1.3.2", "2563:0575")],
            )

    def test_update_saved_busid_does_not_mutate_live_config_when_save_fails(self) -> None:
        config = SavedHostsConfig(Path("/tmp/unused-hosts.json"))
        saved = SavedHost(
            ip="192.168.71.49",
            auto_connect=[
                AutoConnectDevice("1-1.3.1", "2563", "0575", "N64"),
            ],
        )

        with patch.object(config, "get", return_value=saved), \
             patch.object(config, "save_host", return_value=False), \
             patch("usb_portal.saved_hosts.get_config", return_value=config):
            ClientSession()._update_saved_busid(
                "192.168.71.49",
                "1-1.3.1",
                "1-1.3.2",
                "2563",
                "0575",
            )

        self.assertEqual(saved.auto_connect[0].busid, "1-1.3.1")

    def test_auto_connect_uses_saved_host_port(self) -> None:
        session = ClientSession()
        session._running = True
        saved = SavedHost(
            ip="10.8.0.5",
            name="vpn-pi",
            port=4321,
            auto_connect=[
                AutoConnectDevice("1-1.3.2", "2563", "0575", "N64"),
            ],
        )
        remote_devices = [remote("1-1.3.2", "2563", "0575")]

        with patch("usb_portal.client.usbip.list_remote", return_value=remote_devices) as list_remote, \
             patch.object(session, "_connected_busids_for", return_value=set()), \
             patch.object(session, "connect") as connect:
            session._auto_connect_host(saved)

        list_remote.assert_called_once_with("10.8.0.5", 4321)
        connect.assert_called_once()
        self.assertEqual(connect.call_args.kwargs["remote_port"], 4321)

    def test_reconnect_resolve_busid_uses_connection_remote_port(self) -> None:
        session = ClientSession()
        conn = ActiveConnection(
            host_ip="10.8.0.5",
            host_name="vpn-pi",
            busid="1-1.3.2",
            port=0,
            device_name="N64",
            remote_port=4321,
            vendor_id="2563",
            product_id="0575",
        )

        with patch("usb_portal.client.usbip.list_remote", return_value=[remote("1-1.3.2", "2563", "0575")]) as list_remote:
            self.assertEqual(session._resolve_remote_busid(conn), "1-1.3.2")

        list_remote.assert_called_once_with("10.8.0.5", 4321)

    def test_reconnect_resolve_busid_returns_none_when_device_is_not_exported(self) -> None:
        session = ClientSession()
        conn = ActiveConnection(
            host_ip="192.168.71.49",
            host_name="floormodel",
            busid="1-1.3.2",
            port=0,
            device_name="N64",
            vendor_id="2563",
            product_id="0575",
        )

        with patch("usb_portal.saved_hosts.get_config") as config, \
             patch("usb_portal.client.usbip.list_remote", return_value=[
            remote("1-1.3.3", "0810", "0003"),
        ]):
            config.return_value.get.return_value = None
            self.assertIsNone(session._resolve_remote_busid(conn))

    def test_attempt_reconnect_drops_connection_when_host_is_up_but_device_absent(self) -> None:
        session = ClientSession()
        session._running = True
        conn = ActiveConnection(
            host_ip="192.168.71.49",
            host_name="floormodel",
            busid="1-1.3.2",
            port=0,
            device_name="N64",
            vendor_id="2563",
            product_id="0575",
        )
        session._connections[0] = conn

        with tempfile.TemporaryDirectory() as tmp:
            config = SavedHostsConfig(Path(tmp) / "hosts.json")
            config.save_host(SavedHost(
                ip="192.168.71.49",
                auto_connect=[
                    AutoConnectDevice("1-1.3.2", "2563", "0575", "N64"),
                ],
            ))
            with patch("usb_portal.saved_hosts.get_config", return_value=config), \
                 patch("usb_portal.client.usbip.list_remote", return_value=[
                     remote("1-1.3.3", "0810", "0003"),
                 ]), \
                 patch("usb_portal.client.usbip.attach") as attach:
                session._attempt_reconnect(conn)

        attach.assert_not_called()
        self.assertEqual(session.get_connections(), [])
        self.assertFalse(conn.reconnect_enabled)

    def test_auto_connect_refreshes_saved_host_from_cidr_scan(self) -> None:
        session = ClientSession()
        session._running = True
        remote_host = RemoteHost(
            ip="10.8.0.9",
            hostname="floormodel",
            machine_id="pi-machine-id",
            port=4321,
            device_count=1,
        )
        remote_devices = [remote("1-1.3.2", "2563", "0575")]

        with tempfile.TemporaryDirectory() as tmp:
            config = SavedHostsConfig(Path(tmp) / "hosts.json")
            saved = SavedHost(
                ip="10.8.0.5",
                hostname="floormodel",
                machine_id="pi-machine-id",
                network="10.8.0.0/24",
                port=3240,
                auto_connect=[
                    AutoConnectDevice("1-1.3.2", "2563", "0575", "N64"),
                ],
            )
            config.save_host(saved)

            with patch("usb_portal.saved_hosts.get_config", return_value=config), \
                 patch("usb_portal.client.scan_cidr", return_value=[remote_host]) as scan_cidr, \
                 patch("usb_portal.client.usbip.list_remote", side_effect=[UsbipError("down"), remote_devices]) as list_remote, \
                 patch.object(session, "_connected_busids_for", return_value=set()), \
                 patch.object(session, "connect") as connect:
                session._auto_connect_host(saved)

            scan_cidr.assert_called_once_with("10.8.0.0/24", cancel_event=session._stop)
            self.assertEqual(list_remote.call_args_list[0].args, ("10.8.0.5", 3240))
            self.assertEqual(list_remote.call_args_list[1].args, ("10.8.0.9", 4321))
            connect.assert_called_once()
            self.assertEqual(connect.call_args.kwargs["host_ip"], "10.8.0.9")
            self.assertEqual(connect.call_args.kwargs["remote_port"], 4321)

            updated = config.get_by_machine_id("pi-machine-id")
            self.assertEqual(updated.ip, "10.8.0.9")
            self.assertEqual(updated.port, 4321)

    def test_update_saved_host_from_remote_does_not_mutate_when_save_fails(self) -> None:
        config = SavedHostsConfig(Path("/tmp/unused-hosts.json"))
        saved = SavedHost(
            ip="10.8.0.5",
            hostname="floormodel",
            machine_id="pi-machine-id",
            port=3240,
        )
        remote_host = RemoteHost(
            ip="10.8.0.9",
            hostname="floormodel-new",
            machine_id="pi-machine-id",
            port=4321,
            device_count=1,
        )

        with patch.object(config, "save_host", return_value=False), \
             patch("usb_portal.saved_hosts.get_config", return_value=config):
            ClientSession()._update_saved_host_from_remote(saved, remote_host)

        self.assertEqual(saved.ip, "10.8.0.5")
        self.assertEqual(saved.hostname, "floormodel")
        self.assertEqual(saved.port, 3240)

    def test_auto_connect_refreshes_generic_saved_device_name(self) -> None:
        session = ClientSession()
        session._running = True
        saved = SavedHost(
            ip="192.168.71.49",
            auto_connect=[
                AutoConnectDevice("1-1.3.3", "0810", "0003", "usb gamepad"),
            ],
        )
        remote_devices = [
            named_remote(
                "1-1.3.3",
                "0810",
                "0003",
                "Personal Communication Systems, Inc.",
                "PlayStation Gamepad",
            ),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            config = SavedHostsConfig(Path(tmp) / "hosts.json")
            config.save_host(saved)
            with patch("usb_portal.saved_hosts.get_config", return_value=config), \
                 patch("usb_portal.client.usbip.list_remote", return_value=remote_devices), \
                 patch.object(session, "_connected_busids_for", return_value=set()), \
                 patch.object(session, "connect"):
                session._auto_connect_host(saved)

            updated = config.get("192.168.71.49")
            self.assertEqual(
                updated.auto_connect[0].name,
                "Personal Communication Systems, Inc. PlayStation Gamepad",
            )

    def test_auto_connect_metadata_does_not_mutate_when_save_fails(self) -> None:
        config = SavedHostsConfig(Path("/tmp/unused-hosts.json"))
        ac_device = AutoConnectDevice("1-1.3.3", "0810", "0003", "usb gamepad")
        saved = SavedHost(
            ip="192.168.71.49",
            auto_connect=[ac_device],
        )
        remote_device = named_remote(
            "1-1.3.4",
            "0810",
            "0003",
            "Personal Communication Systems, Inc.",
            "PlayStation Gamepad",
        )

        with patch.object(config, "save_host", return_value=False), \
             patch("usb_portal.saved_hosts.get_config", return_value=config):
            ClientSession()._update_saved_auto_connect_metadata(
                saved,
                ac_device,
                remote_device,
                "Personal Communication Systems, Inc. PlayStation Gamepad",
            )

        self.assertEqual(ac_device.busid, "1-1.3.3")
        self.assertEqual(ac_device.name, "usb gamepad")

    def test_preferred_device_name_keeps_specific_name_over_unknown_remote_name(self) -> None:
        remote_device = named_remote(
            "1-1.3.3",
            "0079",
            "1804",
            "DragonRise Inc.",
            "unknown product",
        )

        self.assertEqual(
            _preferred_device_name("HuiJia NES/FC/SFC Joypad To USB BOX", remote_device),
            "HuiJia NES/FC/SFC Joypad To USB BOX",
        )

    def test_cidr_resolution_rejects_same_ip_with_different_machine_id(self) -> None:
        session = ClientSession()
        saved = SavedHost(
            ip="10.8.0.5",
            hostname="floormodel",
            machine_id="expected-machine-id",
            network="10.8.0.0/24",
        )
        different_host = RemoteHost(
            ip="10.8.0.5",
            hostname="floormodel",
            machine_id="different-machine-id",
            port=3240,
            device_count=1,
        )

        with patch("usb_portal.client.scan_cidr", return_value=[different_host]):
            self.assertIsNone(session._resolve_host_via_cidr(saved))


if __name__ == "__main__":
    unittest.main()
