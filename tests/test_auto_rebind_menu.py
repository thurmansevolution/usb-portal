import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from usb_portal.cli import service_menu
from usb_portal.daemon import BoundDevice, DaemonState, GamepadDaemon
from usb_portal.usbip import UsbipDevice


class AutoRebindMenuTests(unittest.TestCase):
    def test_enable_already_shared_dolphinbar_without_rebinding(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = DaemonState(Path(tmp) / "state.json")
            dolphinbar = UsbipDevice("1-3", "057e", "0306", "Nintendo", "DolphinBar")
            with patch.object(service_menu, "DaemonState", return_value=state), \
                 patch.object(service_menu, "require_root", return_value=True), \
                 patch.object(service_menu, "get_bound_devices", return_value=["1-3"]), \
                 patch.object(service_menu, "get_local_device", return_value=dolphinbar), \
                 patch.object(service_menu, "prompt_choice", side_effect=[0, 0, 3]), \
                 patch.object(service_menu, "clear_screen"), patch.object(service_menu, "pause"), \
                 patch.object(service_menu, "print_success") as success, \
                 patch.object(service_menu, "bind") as bind, patch.object(service_menu, "unbind") as unbind:
                service_menu._auto_rebind_settings()
            saved = DaemonState(state._path).get_bindings()
            self.assertEqual([(d.busid, d.vid_pid, d.auto_rebind) for d in saved], [("1-3", "057e:0306", True)])
            bind.assert_not_called()
            unbind.assert_not_called()
            success.assert_called_once()

    def test_enable_reports_disk_failure_and_preserves_original_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = DaemonState(Path(tmp) / "state.json")
            state.set_bindings([BoundDevice("1-1", "0079", "1804", "SNES")])
            with patch.object(service_menu, "DaemonState", return_value=state), \
                 patch.object(service_menu, "_has_unique_active_identity", return_value=True), \
                 patch("usb_portal.daemon.atomic_write", side_effect=OSError("disk full")), \
                 patch.object(service_menu, "print_error") as error:
                self.assertFalse(service_menu._update_daemon_state(BoundDevice("1-3", "057e", "0306", "DolphinBar")))
            self.assertIn("disk full", error.call_args.args[0])
            self.assertEqual([d.busid for d in state.get_bindings()], ["1-1"])

    def test_nonroot_auto_rebind_is_explained_before_touching_state(self):
        with patch.object(service_menu, "require_root", return_value=False), \
             patch.object(service_menu, "DaemonState") as state, patch.object(service_menu, "pause"):
            service_menu._auto_rebind_settings()
        state.assert_not_called()

    def test_daemon_save_cannot_disable_newly_enabled_active_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = DaemonState(Path(tmp) / "state.json")
            state.set_bindings([BoundDevice("1-3", "057e", "0306", "DolphinBar", True)])
            daemon = GamepadDaemon()
            daemon._state = state
            # The device was previously shared for this session only. A CLI
            # enabled auto-rebind just before the daemon's shutdown/save.
            daemon._bound = {"1-3": BoundDevice("1-3", "057e", "0306", "DolphinBar", False)}
            daemon._save_state()
            self.assertTrue(DaemonState(state._path).get_bindings()[0].auto_rebind)
            self.assertTrue(daemon._wanted[0].auto_rebind)

    def test_remove_does_not_claim_success_when_state_is_unwritable(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = DaemonState(Path(tmp) / "state.json")
            state.set_bindings([BoundDevice("1-3", "057e", "0306", "DolphinBar")])
            with patch.object(service_menu, "DaemonState", return_value=state), \
                 patch.object(service_menu, "require_root", return_value=True), \
                 patch.object(service_menu, "prompt_choice", side_effect=[1, 0, 3]), \
                 patch.object(service_menu, "clear_screen"), patch.object(service_menu, "pause"), \
                 patch("usb_portal.daemon.atomic_write", side_effect=OSError("read-only filesystem")), \
                 patch.object(service_menu, "print_success") as success, \
                 patch.object(service_menu, "print_error") as error:
                service_menu._auto_rebind_settings()
            success.assert_not_called()
            self.assertIn("read-only filesystem", error.call_args.args[0])
            self.assertEqual(len(state.get_bindings()), 1)

    def test_clear_does_not_claim_success_when_state_is_unwritable(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = DaemonState(Path(tmp) / "state.json")
            state.set_bindings([BoundDevice("1-3", "057e", "0306", "DolphinBar")])
            with patch.object(service_menu, "DaemonState", return_value=state), \
                 patch.object(service_menu, "require_root", return_value=True), \
                 patch.object(service_menu, "prompt_choice", side_effect=[2, 3]), \
                 patch.object(service_menu, "prompt_yes_no", return_value=True), \
                 patch.object(service_menu, "clear_screen"), patch.object(service_menu, "pause"), \
                 patch("usb_portal.daemon.atomic_write", side_effect=OSError("disk full")), \
                 patch.object(service_menu, "print_success") as success, \
                 patch.object(service_menu, "print_error") as error:
                service_menu._auto_rebind_settings()
            success.assert_not_called()
            self.assertIn("disk full", error.call_args.args[0])
            self.assertEqual(len(state.get_bindings()), 1)

    def test_unreadable_settings_return_to_menu_with_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("corrupt json")
            with patch.object(service_menu, "DaemonState", return_value=DaemonState(path)), \
                 patch.object(service_menu, "require_root", return_value=True), \
                 patch.object(service_menu, "clear_screen"), patch.object(service_menu, "pause"), \
                 patch.object(service_menu, "print_error") as error:
                service_menu._auto_rebind_settings()
            error.assert_called_once()
            self.assertIn("Cannot read", error.call_args.args[0])
            self.assertEqual(path.read_text(), "corrupt json")


if __name__ == "__main__":
    unittest.main()
