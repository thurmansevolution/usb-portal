import unittest
from unittest.mock import patch

from usb_portal import usbip
from usb_portal.usbip import AttachedPort


class UsbipPortTests(unittest.TestCase):
    def test_bind_is_noop_when_device_is_already_bound(self) -> None:
        with patch.object(usbip, "is_bound", return_value=True), \
             patch.object(usbip, "_run") as run:
            usbip.bind("1-1.3.3")

        run.assert_not_called()

    def test_bind_treats_command_failure_as_success_if_device_became_bound(self) -> None:
        states = iter([False, True])

        with patch.object(usbip, "is_bound", side_effect=lambda busid: next(states)), \
             patch.object(usbip.Path, "exists", return_value=True), \
             patch.object(usbip, "_run", side_effect=usbip.UsbipError("already bound")):
            usbip.bind("1-1.3.3")

    def test_unbind_is_noop_when_device_is_not_bound(self) -> None:
        with patch.object(usbip, "is_bound", return_value=False), \
             patch.object(usbip, "_run") as run:
            usbip.unbind("1-1.3.3")

        run.assert_not_called()

    def test_unbind_treats_command_failure_as_success_if_device_became_unbound(self) -> None:
        states = iter([True, False])

        with patch.object(usbip, "is_bound", side_effect=lambda busid: next(states)), \
             patch.object(usbip, "_run", side_effect=usbip.UsbipError("not bound")):
            usbip.unbind("1-1.3.3")

    def test_list_exportable_enriches_names_from_human_output(self) -> None:
        calls = []

        def fake_run(args, check=True, capture=True):
            calls.append(args)
            class Result:
                stderr = ""
                returncode = 0

                @property
                def stdout(self):
                    if args == ["usbip", "list", "-p", "-l"]:
                        return "busid=1-1.3.3#usbid=0810:0003#\n"
                    return (
                        " - busid 1-1.3.3 (0810:0003)\n"
                        "   Personal Communication Systems, Inc. : PlayStation Gamepad (0810:0003)\n"
                    )
            return Result()

        with patch.object(usbip, "_run", side_effect=fake_run), \
             patch.object(usbip, "_sysfs_device_names", return_value=("", "USB Gamepad")):
            devices = usbip.list_exportable()

        self.assertEqual(devices[0].vendor_name, "Personal Communication Systems, Inc.")
        self.assertEqual(devices[0].product_name, "PlayStation Gamepad")
        self.assertEqual(calls[0], ["usbip", "list", "-p", "-l"])
        self.assertEqual(calls[1], ["usbip", "list", "-l"])

    def test_list_remote_uses_tcp_port_for_non_default_port(self) -> None:
        calls = []

        def fake_run(args, check=True, capture=True):
            calls.append(args)
            class Result:
                stdout = "busid=1-1.3.2#usbid=2563:0575#\n"
                stderr = ""
                returncode = 0
            return Result()

        with patch.object(usbip, "_run", side_effect=fake_run):
            devices = usbip.list_remote("10.8.0.5", port=4321)

        self.assertEqual(devices[0].busid, "1-1.3.2")
        self.assertEqual(calls[0], ["usbip", "--tcp-port", "4321", "list", "-p", "-r", "10.8.0.5"])
        self.assertEqual(calls[1], ["usbip", "--tcp-port", "4321", "list", "-r", "10.8.0.5"])

    def test_list_remote_omits_tcp_port_for_default_port(self) -> None:
        calls = []

        def fake_run(args, check=True, capture=True):
            calls.append(args)
            class Result:
                stdout = "busid=1-1.3.2#usbid=2563:0575#\n"
                stderr = ""
                returncode = 0
            return Result()

        with patch.object(usbip, "_run", side_effect=fake_run):
            usbip.list_remote("192.168.71.49")

        self.assertEqual(calls[0], ["usbip", "list", "-p", "-r", "192.168.71.49"])

    def test_attach_uses_tcp_port_for_non_default_port(self) -> None:
        calls = []

        def fake_run(args, check=True, capture=True):
            calls.append(args)
            class Result:
                stdout = ""
                stderr = ""
                returncode = 0
            return Result()

        with patch.object(usbip, "_run", side_effect=fake_run), \
             patch.object(usbip, "list_attached_ports", return_value=[
                 AttachedPort(
                     port=2,
                     busid="1-1.3.2",
                     remote_host="10.8.0.5",
                     vendor_id="2563",
                     product_id="0575",
                 )
             ]), \
             patch.object(usbip.time, "sleep"):
            port = usbip._attach("10.8.0.5", "1-1.3.2", port=4321)

        self.assertEqual(port, 2)
        self.assertEqual(calls[0], ["usbip", "--tcp-port", "4321", "attach", "-r", "10.8.0.5", "-b", "1-1.3.2"])

    def test_attach_survives_port_read_failure_during_usb_enumeration(self) -> None:
        attached = AttachedPort(0, "1-3.2", "192.168.70.3", "0079", "0006")
        with patch.object(usbip, "_run") as run, \
             patch.object(usbip.socket, "getaddrinfo", return_value=[]), \
             patch.object(usbip, "list_attached_ports", side_effect=[
                 usbip.UsbipError("usbip: error: open vhci_driver (is vhci_hcd loaded?)"),
                 [],
                 [attached],
             ]), \
             patch.object(usbip.time, "monotonic", side_effect=[0, 0.1, 0.2]), \
             patch.object(usbip.time, "sleep"):
            port = usbip._attach("192.168.70.3", "1-3.2", 3240)

        self.assertEqual(port, 0)
        run.assert_called_once_with(["usbip", "attach", "-r", "192.168.70.3", "-b", "1-3.2"])

    def test_attach_reports_persistent_port_read_failure_without_reattaching(self) -> None:
        with patch.object(usbip, "_run") as run, \
             patch.object(usbip.socket, "getaddrinfo", return_value=[]), \
             patch.object(usbip, "list_attached_ports", side_effect=usbip.UsbipError("vhci unreadable")), \
             patch.object(usbip.time, "monotonic", side_effect=[0, 0.1, 2.1]), \
             patch.object(usbip.time, "sleep"):
            with self.assertRaisesRegex(usbip.UsbipError, "may still be attached.*vhci unreadable"):
                usbip._attach("192.168.70.3", "1-3.2", 3240)

        run.assert_called_once()

    def test_attach_does_not_accept_another_hosts_device_after_port_read_failure(self) -> None:
        other = AttachedPort(0, "1-3.2", "192.168.71.155", "0079", "0006")
        with patch.object(usbip, "_run") as run, \
             patch.object(usbip.socket, "getaddrinfo", return_value=[]), \
             patch.object(usbip, "list_attached_ports", side_effect=[
                 usbip.UsbipError("vhci unreadable"), [other],
             ]), \
             patch.object(usbip.time, "monotonic", side_effect=[0, 0.1, 2.1]), \
             patch.object(usbip.time, "sleep"):
            self.assertEqual(usbip._attach("192.168.70.3", "1-3.2", 3240), -1)

        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
