import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from usb_portal.cli import service_menu
from usb_portal.daemon import (
    BoundDevice,
    DaemonState,
    GamepadDaemon,
    _pending_wanted_bindings,
    _refresh_remembered_bindings,
    _resolve_wanted_busids,
)
from usb_portal.usbip import UsbipDevice


class DeviceMemoryTests(unittest.TestCase):
    def test_service_menu_remembers_identical_vidpid_devices_by_busid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"

            with patch.object(service_menu, "DaemonState", lambda: DaemonState(state_path)), \
                 patch.object(service_menu, "_has_unique_active_identity", return_value=False):
                service_menu._update_daemon_state(BoundDevice("1-1", "045e", "028e", "Pad A", True))
                service_menu._update_daemon_state(BoundDevice("1-2", "045e", "028e", "Pad B", True))

            bindings = DaemonState(state_path).get_bindings()
            self.assertEqual([b.busid for b in bindings], ["1-1", "1-2"])

    def test_service_menu_non_auto_rebind_removes_existing_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            DaemonState(state_path).set_bindings([
                BoundDevice("1-1", "045e", "028e", "Pad A", True),
            ])

            with patch.object(service_menu, "DaemonState", lambda: DaemonState(state_path)):
                service_menu._update_daemon_state(BoundDevice("1-1", "045e", "028e", "Pad A", False))

            self.assertEqual(DaemonState(state_path).get_bindings(), [])

    def test_service_menu_updates_unique_moved_auto_rebind_device(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            DaemonState(state_path).set_bindings([
                BoundDevice("1-1.3.1", "2563", "0575", "N64 old port", True),
            ])

            with patch.object(service_menu, "DaemonState", lambda: DaemonState(state_path)), \
                 patch.object(service_menu, "get_bound_devices", return_value=["1-1.3.4"]), \
                 patch.object(service_menu, "get_local_device", return_value=UsbipDevice(
                     busid="1-1.3.4",
                     vendor_id="2563",
                     product_id="0575",
                     vendor_name="",
                     product_name="",
                 )):
                service_menu._update_daemon_state(
                    BoundDevice("1-1.3.4", "2563", "0575", "N64 new port", True)
                )

            bindings = DaemonState(state_path).get_bindings()
            self.assertEqual([(b.busid, b.name) for b in bindings], [("1-1.3.4", "N64 new port")])

    def test_service_menu_keeps_duplicate_identity_memory_busid_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            DaemonState(state_path).set_bindings([
                BoundDevice("1-1.3.1", "0079", "1804", "SNES A", True),
                BoundDevice("1-1.3.2", "0079", "1804", "SNES B", True),
            ])

            with patch.object(service_menu, "DaemonState", lambda: DaemonState(state_path)), \
                 patch.object(service_menu, "_has_unique_active_identity", return_value=False):
                service_menu._update_daemon_state(
                    BoundDevice("1-1.3.4", "0079", "1804", "SNES new", True)
                )

            bindings = DaemonState(state_path).get_bindings()
            self.assertEqual(
                [(b.busid, b.name) for b in bindings],
                [
                    ("1-1.3.1", "SNES A"),
                    ("1-1.3.2", "SNES B"),
                    ("1-1.3.4", "SNES new"),
                ],
            )

    def test_service_menu_remove_from_state_uses_unique_identity_when_busid_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            DaemonState(state_path).set_bindings([
                BoundDevice("1-1.3.1", "2563", "0575", "N64 old port", True),
            ])

            with patch.object(service_menu, "DaemonState", lambda: DaemonState(state_path)):
                service_menu._remove_from_daemon_state("1-1.3.4", "2563", "0575")

            self.assertEqual(DaemonState(state_path).get_bindings(), [])

    def test_service_menu_state_helpers_report_save_failures(self) -> None:
        with patch.object(service_menu, "DaemonState", side_effect=RuntimeError("disk full")):
            self.assertFalse(
                service_menu._update_daemon_state(
                    BoundDevice("1-1.3.1", "0079", "1804", "SNES", True)
                )
            )
            self.assertFalse(
                service_menu._remove_from_daemon_state("1-1.3.1", "0079", "1804")
            )

    def test_service_menu_state_helpers_report_set_bindings_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"

            class FailingState(DaemonState):
                def __init__(self) -> None:
                    super().__init__(state_path)

                def update_bindings(self, update) -> bool:
                    return False

            with patch.object(service_menu, "DaemonState", FailingState):
                self.assertFalse(
                    service_menu._update_daemon_state(
                        BoundDevice("1-1.3.1", "0079", "1804", "SNES", True)
                    )
                )
                self.assertFalse(
                    service_menu._remove_from_daemon_state("1-1.3.1", "0079", "1804")
                )

    def test_daemon_state_set_bindings_reports_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = DaemonState(Path(tmp) / "state.json")

            self.assertTrue(state.set_bindings([
                BoundDevice("1-1.3.1", "0079", "1804", "SNES", True),
            ]))

            self.assertEqual(state.get_bindings()[0].busid, "1-1.3.1")

    def test_daemon_state_atomic_write_preserves_existing_file_on_replace_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state = DaemonState(state_path)
            state.set_bindings([
                BoundDevice("1-1.3.1", "0079", "1804", "SNES", True),
            ])

            with patch.object(Path, "replace", side_effect=OSError("disk full")):
                result = state.set_bindings([
                    BoundDevice("1-1.3.2", "2563", "0575", "N64", True),
                ])

            self.assertFalse(result)
            bindings = DaemonState(state_path).get_bindings()
            self.assertEqual([(b.busid, b.vid_pid) for b in bindings], [("1-1.3.1", "0079:1804")])
            self.assertFalse((state_path.parent / ".state.json.tmp").exists())

    def test_daemon_save_preserves_entries_added_by_cli_before_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state = DaemonState(state_path)
            state.set_bindings([
                BoundDevice("1-1", "045e", "028e", "Pad A", True),
                BoundDevice("1-2", "046d", "c216", "Pad B", True),
            ])

            daemon = GamepadDaemon()
            daemon._state = state
            daemon._bound = {
                "1-1": BoundDevice("1-1", "045e", "028e", "Pad A refreshed", True),
            }

            daemon._save_state()

            bindings = DaemonState(state_path).get_bindings()
            self.assertEqual([b.busid for b in bindings], ["1-1", "1-2"])
            self.assertEqual(bindings[0].name, "Pad A refreshed")

    def test_daemon_save_updates_wanted_after_successful_persist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state = DaemonState(state_path)
            state.set_bindings([
                BoundDevice("1-1.3.1", "0079", "1804", "SNES", True),
            ])

            daemon = GamepadDaemon()
            daemon._state = state
            daemon._wanted = [
                BoundDevice("1-1.3.1", "0079", "1804", "SNES", True),
            ]
            daemon._bound = {
                "1-1.3.2": BoundDevice("1-1.3.2", "0079", "1804", "SNES moved", True),
            }

            daemon._save_state()

            self.assertEqual([(b.busid, b.name) for b in daemon._wanted], [("1-1.3.2", "SNES moved")])

    def test_daemon_save_keeps_wanted_when_persist_fails(self) -> None:
        class FailingState:
            def get_bindings(self):
                return [BoundDevice("1-1.3.1", "0079", "1804", "SNES", True)]

            def update_bindings(self, update) -> bool:
                return False

        daemon = GamepadDaemon()
        daemon._state = FailingState()
        daemon._wanted = [
            BoundDevice("1-1.3.1", "0079", "1804", "SNES", True),
        ]
        daemon._bound = {
            "1-1.3.2": BoundDevice("1-1.3.2", "0079", "1804", "SNES moved", True),
        }

        daemon._save_state()

        self.assertEqual([(b.busid, b.name) for b in daemon._wanted], [("1-1.3.1", "SNES")])

    def test_daemon_save_converges_two_active_moved_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state = DaemonState(state_path)
            state.set_bindings([
                BoundDevice("1-1.3.3", "0079", "1804", "SNES", True),
                BoundDevice("1-1.3.2", "2563", "0575", "N64", True),
            ])

            daemon = GamepadDaemon()
            daemon._state = state
            daemon._bound = {
                "1-1.3.2": BoundDevice("1-1.3.2", "0079", "1804", "SNES moved", True),
                "1-1.3.3": BoundDevice("1-1.3.3", "2563", "0575", "N64 moved", True),
            }

            daemon._save_state()

            bindings = DaemonState(state_path).get_bindings()
            by_vidpid = {b.vid_pid: b for b in bindings}
            self.assertEqual(by_vidpid["0079:1804"].busid, "1-1.3.2")
            self.assertEqual(by_vidpid["2563:0575"].busid, "1-1.3.3")

    def test_daemon_save_converges_duplicate_snes_and_n64_port_shuffle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state = DaemonState(state_path)
            state.set_bindings([
                BoundDevice("1-1.3.1", "0079", "1804", "SNES A", True),
                BoundDevice("1-1.3.2", "0079", "1804", "SNES B", True),
                BoundDevice("1-1.3.4", "2563", "0575", "N64", True),
            ])

            daemon = GamepadDaemon()
            daemon._state = state
            daemon._bound = {
                "1-1.3.1": BoundDevice("1-1.3.1", "2563", "0575", "N64 moved", True),
                "1-1.3.3": BoundDevice("1-1.3.3", "0079", "1804", "SNES A moved", True),
                "1-1.3.4": BoundDevice("1-1.3.4", "0079", "1804", "SNES B moved", True),
            }

            daemon._save_state()

            bindings = DaemonState(state_path).get_bindings()
            snes_busids = sorted(b.busid for b in bindings if b.vid_pid == "0079:1804")
            n64 = next(b for b in bindings if b.vid_pid == "2563:0575")
            self.assertEqual(snes_busids, ["1-1.3.3", "1-1.3.4"])
            self.assertEqual(n64.busid, "1-1.3.1")

    def test_daemon_save_does_not_resurrect_removed_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state = DaemonState(state_path)
            state.set_bindings([])

            daemon = GamepadDaemon()
            daemon._state = state
            daemon._bound = {
                "1-1": BoundDevice("1-1", "045e", "028e", "Pad A", True),
            }

            daemon._save_state()

            self.assertEqual(DaemonState(state_path).get_bindings(), [])

    def test_daemon_usb_reconnect_reloads_state_before_rebinding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state = DaemonState(state_path)
            state.set_bindings([])

            daemon = GamepadDaemon()
            daemon._state = state
            daemon._wanted = [
                BoundDevice("1-1.3.1", "0079", "1804", "SNES stale memory", True),
            ]

            with patch("usb_portal.daemon._enumerate_sysfs_devices", return_value={
                "1-1.3.1": ("0079", "1804"),
            }), \
                 patch("usb_portal.daemon.usbip.get_bound_devices", return_value=[]), \
                 patch.object(daemon, "_do_bind") as do_bind:
                daemon._on_usb_devices_appeared({"1-1.3.1": ("0079", "1804")})

            do_bind.assert_not_called()
            self.assertEqual(daemon._wanted, [])

    def test_daemon_restore_reloads_state_before_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state = DaemonState(state_path)
            state.set_bindings([])

            daemon = GamepadDaemon()
            daemon._state = state
            daemon._wanted = [
                BoundDevice("1-1.3.1", "0079", "1804", "SNES stale memory", True),
            ]

            with patch("usb_portal.daemon._enumerate_sysfs_devices", return_value={
                "1-1.3.1": ("0079", "1804"),
            }), \
                 patch("usb_portal.daemon.usbip.get_bound_devices", return_value=[]), \
                 patch.object(daemon, "_do_bind") as do_bind:
                daemon._restore_bindings()

            do_bind.assert_not_called()
            self.assertEqual(daemon._wanted, [])

    def test_daemon_rebind_loop_picks_up_state_added_by_service_menu(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state = DaemonState(state_path)
            state.set_bindings([
                BoundDevice("1-1.3.1", "0079", "1804", "SNES saved", True),
            ])

            daemon = GamepadDaemon()
            daemon._state = state
            daemon._wanted = []

            with patch("usb_portal.daemon._enumerate_sysfs_devices", return_value={
                "1-1.3.1": ("0079", "1804"),
            }), \
                 patch("usb_portal.daemon.usbip.get_bound_devices", return_value=[]), \
                 patch.object(daemon, "_do_bind", return_value=True) as do_bind:
                daemon._rebind_wanted_unbound()

            do_bind.assert_called_once()
            self.assertEqual(do_bind.call_args.args[0], "1-1.3.1")

    def test_daemon_sysfs_sync_does_not_promote_single_session_binding_to_wanted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state = DaemonState(state_path)
            state.set_bindings([])

            daemon = GamepadDaemon()
            daemon._state = state

            with patch("usb_portal.daemon.usbip.get_bound_devices", return_value=["1-1.3.1"]), \
                 patch("usb_portal.daemon._enumerate_sysfs_devices", return_value={
                     "1-1.3.1": ("0079", "1804"),
                 }), \
                 patch("usb_portal.daemon.usbip._sysfs_device_names", return_value=("HuiJia", "SNES")):
                daemon._sync_bound_with_sysfs()

            self.assertEqual(list(daemon._bound.keys()), ["1-1.3.1"])
            self.assertFalse(daemon._bound["1-1.3.1"].auto_rebind)
            self.assertEqual(daemon._wanted, [])

    def test_daemon_sysfs_sync_demotes_bound_device_removed_from_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state = DaemonState(state_path)
            state.set_bindings([])

            daemon = GamepadDaemon()
            daemon._state = state
            daemon._bound = {
                "1-1.3.1": BoundDevice("1-1.3.1", "0079", "1804", "SNES", True),
            }
            daemon._wanted = [
                BoundDevice("1-1.3.1", "0079", "1804", "SNES", True),
            ]

            with patch("usb_portal.daemon.usbip.get_bound_devices", return_value=["1-1.3.1"]), \
                 patch("usb_portal.daemon._enumerate_sysfs_devices", return_value={
                     "1-1.3.1": ("0079", "1804"),
                 }):
                daemon._sync_bound_with_sysfs()

            self.assertFalse(daemon._bound["1-1.3.1"].auto_rebind)
            self.assertEqual(daemon._wanted, [])

    def test_resolve_busid_uses_vidpid_when_saved_busid_has_different_device(self) -> None:
        daemon = GamepadDaemon()
        saved_snes = BoundDevice("1-1.3.1", "0079", "1804", "SNES", True)
        current_devices = {
            "1-1.3.1": ("2563", "0575"),
            "1-1.3.3": ("0079", "1804"),
        }

        self.assertEqual(daemon._resolve_busid(saved_snes, current_devices), "1-1.3.3")

    def test_resolve_wanted_busids_allocates_duplicate_serialless_devices(self) -> None:
        wanted = [
            BoundDevice("1-1.3.1", "0079", "1804", "SNES A", True),
            BoundDevice("1-1.3.2", "0079", "1804", "SNES B", True),
        ]
        current_devices = {
            "1-1.3.3": ("0079", "1804"),
            "1-1.3.4": ("0079", "1804"),
        }

        assignments = _resolve_wanted_busids(wanted, current_devices)

        self.assertEqual(assignments, {0: "1-1.3.3", 1: "1-1.3.4"})

    def test_resolve_wanted_busids_leaves_single_device_ambiguous_when_duplicates_present(self) -> None:
        wanted = [
            BoundDevice("1-1.3.1", "0079", "1804", "SNES", True),
        ]
        current_devices = {
            "1-1.3.3": ("0079", "1804"),
            "1-1.3.4": ("0079", "1804"),
        }

        self.assertEqual(_resolve_wanted_busids(wanted, current_devices), {})

    def test_pending_wanted_bindings_keeps_device_pending_when_old_busid_has_wrong_identity(self) -> None:
        wanted = [
            BoundDevice("1-1.3.3", "0079", "1804", "SNES A", True),
            BoundDevice("1-1.3.4", "0079", "1804", "SNES B", True),
            BoundDevice("1-1.3.1", "2563", "0575", "N64", True),
        ]
        bound = [
            BoundDevice("1-1.3.1", "0079", "1804", "SNES A moved", True),
            BoundDevice("1-1.3.3", "0079", "1804", "SNES B moved", True),
        ]

        pending = _pending_wanted_bindings(wanted, bound)

        self.assertEqual([(d.busid, d.vid_pid) for d in pending], [("1-1.3.1", "2563:0575")])

    def test_refresh_remembered_bindings_updates_unique_vidpid_port_swap(self) -> None:
        remembered = [
            BoundDevice("1-1.3.1", "0079", "1804", "SNES", True),
            BoundDevice("1-1.3.2", "2563", "0575", "N64", True),
        ]
        active = [
            BoundDevice("1-1.3.1", "2563", "0575", "N64 moved", True),
            BoundDevice("1-1.3.2", "0079", "1804", "SNES moved", True),
        ]

        refreshed = _refresh_remembered_bindings(remembered, active)
        by_vidpid = {b.vid_pid: b for b in refreshed}

        self.assertEqual(by_vidpid["2563:0575"].busid, "1-1.3.1")
        self.assertEqual(by_vidpid["2563:0575"].name, "N64 moved")
        self.assertEqual(by_vidpid["0079:1804"].busid, "1-1.3.2")
        self.assertEqual(by_vidpid["0079:1804"].name, "SNES moved")

    def test_refresh_remembered_bindings_skips_single_update_that_collides_with_other_memory(self) -> None:
        remembered = [
            BoundDevice("1-1.3.1", "0079", "1804", "SNES", True),
            BoundDevice("1-1.3.2", "2563", "0575", "N64", True),
        ]
        active = [
            BoundDevice("1-1.3.1", "2563", "0575", "N64 moved", True),
        ]

        refreshed = _refresh_remembered_bindings(remembered, active)

        self.assertEqual([b.busid for b in refreshed], ["1-1.3.1", "1-1.3.2"])
        self.assertEqual([b.vid_pid for b in refreshed], ["0079:1804", "2563:0575"])

    def test_refresh_remembered_bindings_keeps_exact_match_when_identical_device_appears(self) -> None:
        remembered = [
            BoundDevice("1-1.3.1", "0079", "1804", "SNES", True),
        ]
        active = [
            BoundDevice("1-1.3.1", "0079", "1804", "SNES refreshed", True),
            BoundDevice("1-1.3.2", "0079", "1804", "Second SNES", True),
        ]

        refreshed = _refresh_remembered_bindings(remembered, active)

        self.assertEqual(len(refreshed), 1)
        self.assertEqual(refreshed[0].busid, "1-1.3.1")
        self.assertEqual(refreshed[0].name, "SNES refreshed")


if __name__ == "__main__":
    unittest.main()
