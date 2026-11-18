"""File logging shared by the interactive CLI and background service."""

import logging
import os
import subprocess
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from usb_portal.persistence import file_lock

LOGGER_NAME = "usb-portal-daemon"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 5
LOG_FORMAT = "%(asctime)s  %(levelname)-8s  [%(process)d:%(threadName)s]  %(message)s"


def default_log_path() -> Path:
    if os.geteuid() == 0:
        return Path("/var/log/usb-portal/usb-portal.txt")
    state_home = Path(os.environ.get("XDG_STATE_HOME", ""))
    if not state_home.is_absolute():
        state_home = Path.home() / ".local/state"
    return state_home / "usb-portal/usb-portal.txt"


def get_log_path() -> Path:
    for handler in logging.getLogger(LOGGER_NAME).handlers:
        if isinstance(handler, LockedRotatingFileHandler):
            return Path(handler.baseFilename)
    return default_log_path()


class LockedRotatingFileHandler(RotatingFileHandler):
    """Serialize rotation across processes; never retain a renamed file handle.

    A CLI and the service can write concurrently. The standard rotating handler
    alone only locks threads, so it can lose messages when two processes rotate.
    Closing after each record also makes external file replacement safe.
    """

    def __init__(self, path: Path, max_bytes=LOG_MAX_BYTES, backups=LOG_BACKUPS):
        super().__init__(path, maxBytes=max_bytes, backupCount=backups,
                         encoding="utf-8", errors="backslashreplace", delay=True)
        self.last_error = ""

    def _open(self):
        fd = os.open(self.baseFilename,
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        return os.fdopen(fd, "a", encoding=self.encoding, errors=self.errors)

    def emit(self, record):
        try:
            with file_lock(Path(self.baseFilename)):
                try:
                    super().emit(record)
                finally:
                    if self.stream is not None:
                        self.stream.close()
                        self.stream = None
        except Exception:
            self.handleError(record)

    def handleError(self, record):
        # A full disk must not produce asynchronous tracebacks over a menu.
        # Startup checks writability; retain a later failure for diagnostics.
        self.last_error = str(sys.exc_info()[1])


def configure_logging(path: Path | None = None) -> Path:
    """Configure only USBPortal's logger, with no console or import side effects."""
    path = (path or default_log_path()).absolute()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = LockedRotatingFileHandler(path)
    # Fail explicitly at startup if logging is unavailable, rather than silently
    # discarding records or falling back to the terminal.
    with file_lock(path):
        with handler._open():
            pass
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger = logging.getLogger(LOGGER_NAME)
    for old in list(logger.handlers):
        logger.removeHandler(old)
        old.close()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    threading.excepthook = _thread_exception
    sys.excepthook = _unhandled_exception
    return path


def _thread_exception(args):
    if args.exc_type is not SystemExit:
        logging.getLogger(LOGGER_NAME).error(
            "Unhandled exception in %s", args.thread.name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )


def _unhandled_exception(exc_type, value, traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        return
    logging.getLogger(LOGGER_NAME).critical(
        "Unhandled exception", exc_info=(exc_type, value, traceback),
    )
    print(f"USBPortal could not complete this operation: {value}. "
          f"Details: {get_log_path()}", file=sys.stderr)


def log_subprocess_output(process: subprocess.Popen, label: str) -> threading.Thread:
    """Drain a child's combined output to the same rotating log, never the menu."""
    def read_output():
        with process.stdout as stream:
            for line in iter(lambda: stream.readline(8192), ""):
                logging.getLogger(LOGGER_NAME).info("%s: %s", label, line.rstrip())

    thread = threading.Thread(target=read_output, daemon=True, name=f"{label}-log")
    thread.start()
    return thread


def read_recent_logs(lines: int = 40, path: Path | None = None) -> str:
    """Return a bounded tail of the active text log, without following it."""
    path = path or get_log_path()
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - 128 * 1024))
            data = stream.read().decode("utf-8", errors="replace")
        return "\n".join(data.splitlines()[-max(1, lines):]) or "(no logs available)"
    except FileNotFoundError:
        return "(no logs available yet)"
    except OSError as error:
        return f"Could not read {path}: {error}"
