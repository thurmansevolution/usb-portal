import contextlib
import io
import logging
import multiprocessing
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from usb_portal.logging_setup import (
    LOGGER_NAME, LockedRotatingFileHandler, configure_logging, default_log_path,
    log_subprocess_output, read_recent_logs,
)


def write_records(path, barrier, worker):
    logger = logging.getLogger(f"log-worker-{worker}")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    handler = LockedRotatingFileHandler(Path(path), max_bytes=512, backups=30)
    logger.addHandler(handler)
    barrier.wait(timeout=10)
    for index in range(30):
        logger.warning("worker-%d-record-%d %s", worker, index, "x" * 40)
    if handler.last_error:
        raise RuntimeError(handler.last_error)
    handler.close()


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger(LOGGER_NAME)
        self.previous = (self.logger.handlers[:], self.logger.level, self.logger.propagate)
        self.logger.handlers = []
        self.hooks = (sys.excepthook, threading.excepthook)

    def tearDown(self):
        for handler in self.logger.handlers:
            handler.close()
        self.logger.handlers, level, self.logger.propagate = self.previous
        self.logger.setLevel(level)
        sys.excepthook, threading.excepthook = self.hooks

    def test_importing_menus_does_not_configure_root_logging(self):
        result = subprocess.run([
            sys.executable, "-c",
            "import logging; from usb_portal.cli import menu; "
            "assert not logging.getLogger().handlers",
        ], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout + result.stderr, "")

    def test_cli_and_background_logs_only_go_to_text_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "logs/usb-portal.txt"
            output = io.StringIO()
            # Even an existing console handler on the embedding app's root
            # logger must not receive USBPortal's records.
            root = logging.getLogger()
            console_handler = logging.StreamHandler(output)
            root.addHandler(console_handler)
            try:
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    configure_logging(path)
                    configure_logging(path)
                    self.logger.info("foreground record")
                    worker = threading.Thread(target=self.logger.warning, args=("background record",))
                    worker.start()
                    worker.join(timeout=5)
                self.assertEqual(output.getvalue(), "")
                content = path.read_text()
                self.assertEqual(content.count("foreground record"), 1)
                self.assertEqual(content.count("background record"), 1)
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
                self.assertEqual(os.stat(path.parent).st_mode & 0o777, 0o700)
            finally:
                root.removeHandler(console_handler)

    def test_concurrent_processes_rotate_without_losing_or_duplicating_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usb-portal.txt"
            context = multiprocessing.get_context("spawn")
            barrier = context.Barrier(4)
            workers = [context.Process(target=write_records, args=(str(path), barrier, i))
                       for i in range(4)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=15)
                self.assertEqual(worker.exitcode, 0)
            files = list(Path(tmp).glob("usb-portal.txt*"))
            self.assertGreater(len(files), 1)
            self.assertLessEqual(len(files), 31)
            records = [line.split(" ", 1)[0] for file in files for line in file.read_text().splitlines()]
            self.assertEqual(len(records), 120)
            self.assertEqual(set(records), {f"worker-{i}-record-{j}" for i in range(4) for j in range(30)})

    def test_runtime_write_failure_does_not_corrupt_menu(self):
        with tempfile.TemporaryDirectory() as tmp:
            configure_logging(Path(tmp) / "usb-portal.txt")
            handler = self.logger.handlers[0]
            output = io.StringIO()
            with patch.object(handler, "_open", side_effect=OSError("disk full")), \
                 contextlib.redirect_stderr(output):
                self.logger.error("record that cannot be saved")
            self.assertIn("disk full", handler.last_error)
            self.assertEqual(output.getvalue(), "")

    def test_startup_logging_failure_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usb-portal.txt"
            path.symlink_to(Path(tmp) / "other.txt")
            with self.assertRaises(OSError):
                configure_logging(path)

    def test_native_child_output_and_exceptions_are_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usb-portal.txt"
            configure_logging(path)
            child = subprocess.Popen([
                sys.executable, "-c", "import sys; print('native stdout'); print('native stderr', file=sys.stderr)",
            ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            reader = log_subprocess_output(child, "usbipd-test")
            child.wait(timeout=5)
            reader.join(timeout=5)
            self.assertFalse(reader.is_alive())
            output = io.StringIO()
            with contextlib.redirect_stderr(output):
                worker = threading.Thread(target=lambda: 1 / 0, name="failing-worker")
                worker.start()
                worker.join(timeout=5)
            content = path.read_text()
            self.assertIn("native stdout", content)
            self.assertIn("native stderr", content)
            self.assertIn("ZeroDivisionError", content)
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(read_recent_logs(1, path), content.splitlines()[-1])

    def test_unprivileged_log_path_uses_absolute_state_home(self):
        with patch("os.geteuid", return_value=1000), \
             patch.dict(os.environ, {"XDG_STATE_HOME": "/tmp/test-state"}):
            self.assertEqual(default_log_path(), Path("/tmp/test-state/usb-portal/usb-portal.txt"))


if __name__ == "__main__":
    unittest.main()
