import unittest
from unittest.mock import patch

from usb_portal.cli.service_menu import _bound_device_info
from usb_portal.host import HostSession
from usb_portal.usbip import UsbipDevice


class HostShareTests(unittest.TestCase):
    def test_share_any_usb_device_uses_usbip_device_name(self) -> None:
        session = HostSession()
        device = UsbipDevice(
            busid="1-1.3.3",
            vendor_id="0079",
            product_id="1804",
            vendor_name="HuiJia",
            product_name="NES/FC/SFC Joypad To USB BOX",
        )

        with patch("usb_portal.host.usbip.bind") as bind:
            shared = session.share_device(device.busid, device)

        bind.assert_called_once_with("1-1.3.3")
        self.assertEqual(shared.name, "HuiJia NES/FC/SFC Joypad To USB BOX")
        self.assertEqual(shared.vendor_id, "0079")
        self.assertEqual(shared.product_id, "1804")

    def test_share_device_retries_when_tracked_device_is_no_longer_bound(self) -> None:
        session = HostSession()
        session._shared["1-1.3.3"] = session._make_shared_device(
            "1-1.3.3",
            UsbipDevice(
                busid="1-1.3.3",
                vendor_id="0079",
                product_id="1804",
                vendor_name="Old",
                product_name="Name",
            ),
        )
        device = UsbipDevice(
            busid="1-1.3.3",
            vendor_id="0079",
            product_id="1804",
            vendor_name="HuiJia",
            product_name="NES/FC/SFC Joypad To USB BOX",
        )

        with patch("usb_portal.host.usbip.is_bound", return_value=False), \
             patch("usb_portal.host.usbip.get_local_device", return_value=device), \
             patch("usb_portal.host.usbip.bind") as bind:
            shared = session.share_device("1-1.3.3")

        bind.assert_called_once_with("1-1.3.3")
        self.assertEqual(shared.name, "HuiJia NES/FC/SFC Joypad To USB BOX")

    def test_sync_bound_devices_adopts_devices_bound_outside_session(self) -> None:
        session = HostSession()
        device = UsbipDevice(
            busid="1-1.3.3",
            vendor_id="0079",
            product_id="1804",
            vendor_name="HuiJia",
            product_name="NES/FC/SFC Joypad To USB BOX",
        )

        with patch("usb_portal.host.usbip.get_bound_devices", return_value=["1-1.3.3"]), \
             patch("usb_portal.host.usbip.get_local_device", return_value=device):
            session.sync_bound_devices()

        shared = session.get_shared_devices()
        self.assertEqual(len(shared), 1)
        self.assertEqual(shared[0].busid, "1-1.3.3")
        self.assertEqual(shared[0].name, "HuiJia NES/FC/SFC Joypad To USB BOX")

    def test_service_bound_device_info_uses_local_usb_metadata(self) -> None:
        device = UsbipDevice(
            busid="1-1.3.3",
            vendor_id="0079",
            product_id="1804",
            vendor_name="HuiJia",
            product_name="NES/FC/SFC Joypad To USB BOX",
        )

        with patch("usb_portal.cli.service_menu.get_local_device", return_value=device):
            info = _bound_device_info("1-1.3.3")

        self.assertEqual(info.name, "HuiJia NES/FC/SFC Joypad To USB BOX")
        self.assertEqual(info.vid_pid, "0079:1804")


if __name__ == "__main__":
    unittest.main()
