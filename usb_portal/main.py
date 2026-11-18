"""
usb|portal — entry point.
Supports both interactive menu mode and direct CLI flags.
"""

import argparse
import sys

from usb_portal.config import APP_NAME, APP_VERSION


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="usb-portal",
        description=f"{APP_NAME} v{APP_VERSION} — Share USB game controllers over LAN via USB/IP",
    )
    parser.add_argument(
        "--version", "-V",
        action="version",
        version=f"{APP_NAME} {APP_VERSION}",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    share_p = subparsers.add_parser("share", help="Share a USB device from this machine")
    share_p.add_argument("busid", nargs="?", help="USB bus ID to share (e.g. 1-1.2). Omit for interactive selection.")

    use_p = subparsers.add_parser("use", help="Use a controller from a remote host")
    use_p.add_argument("host", nargs="?", help="Remote host IP. Omit for LAN discovery.")
    use_p.add_argument("--busid", "-b", help="Remote bus ID to attach directly.")
    use_p.add_argument("--port", "-p", type=int, default=3240, help="Remote USB/IP TCP port.")

    subparsers.add_parser("check", help="Check USB/IP prerequisites")
    subparsers.add_parser("menu", help="Launch interactive menu (default)")

    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    from usb_portal.cli.common import console, print_error
    from usb_portal.logging_setup import configure_logging

    try:
        configure_logging()
    except OSError as error:
        print_error(f"Could not open the USBPortal log file: {error}")
        sys.exit(1)

    if args.command == "check":
        _run_check()
        return

    if args.command == "share":
        _run_share_direct(args)
        return

    if args.command == "use":
        _run_use_direct(args)
        return

    from usb_portal.cli.menu import run_main_menu
    try:
        run_main_menu()
    except KeyboardInterrupt:
        console.print("\n\n[dim]  Interrupted. Goodbye![/dim]\n")
        sys.exit(0)


def _run_check() -> None:
    from rich.console import Console
    from usb_portal.usbip import check_prerequisites
    from usb_portal.discovery import get_local_hostname, get_local_ip

    console = Console()
    checks = check_prerequisites()

    console.print(f"\n[bold]{APP_NAME}[/bold] — System Check\n")
    for key, ok in checks.items():
        status = "[green]✓[/green]" if ok else "[red]✗[/red]"
        console.print(f"  {status}  {key}")

    all_ok = all(checks.values())
    if all_ok:
        console.print("\n[bold green]All prerequisites satisfied.[/bold green]\n")
    else:
        console.print("\n[bold yellow]Some prerequisites are missing.[/bold yellow]")
        console.print("  [dim]sudo apt install linux-tools-generic usbip usbutils[/dim]")
        console.print("  [dim]sudo modprobe usbip_core usbip_host vhci_hcd[/dim]\n")
    sys.exit(0 if all_ok else 1)


def _run_share_direct(args: argparse.Namespace) -> None:
    import os
    from usb_portal.cli.common import print_error, print_success, print_info
    from usb_portal.host import get_session
    from usb_portal.usbip import UsbipError, UsbipNotFound

    if os.geteuid() != 0:
        print_error("Share mode requires root. Run with sudo.")
        sys.exit(1)

    session = get_session()

    try:
        session.setup()
        session.start_daemon()
        session.start_advertising()
    except (UsbipError, UsbipNotFound) as e:
        print_error(str(e))
        sys.exit(1)

    if args.busid:
        try:
            shared = session.share_device(args.busid)
            print_success(f"Sharing {args.busid}. Press Ctrl+C to stop.")
            _wait_for_interrupt()
        except UsbipError as e:
            print_error(str(e))
            sys.exit(1)
        finally:
            session.shutdown()
    else:
        from usb_portal.cli.host_menu import run_host_menu
        try:
            run_host_menu()
        except KeyboardInterrupt:
            session.shutdown()


def _run_use_direct(args: argparse.Namespace) -> None:
    import os
    from usb_portal.cli.common import print_error, print_success, print_info
    from usb_portal.client import get_session
    from usb_portal.usbip import UsbipError, UsbipNotFound

    if os.geteuid() != 0:
        print_error("Use mode requires root. Run with sudo.")
        sys.exit(1)

    session = get_session()
    try:
        session.setup()
    except (UsbipError, UsbipNotFound) as e:
        print_error(str(e))
        sys.exit(1)

    session.start_monitoring()

    if args.host and args.busid:
        try:
            conn = session.connect(
                host_ip=args.host,
                busid=args.busid,
                host_name=args.host,
                remote_port=args.port,
            )
            print_success(f"Attached {args.busid} from {args.host} on virtual port {conn.port}.")
            print_info("Press Ctrl+C to disconnect.")
            _wait_for_interrupt()
        except UsbipError as e:
            print_error(str(e))
            sys.exit(1)
        finally:
            session.shutdown()
    else:
        from usb_portal.cli.client_menu import run_client_menu
        try:
            run_client_menu()
        except KeyboardInterrupt:
            session.shutdown()


def _wait_for_interrupt() -> None:
    import time
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
