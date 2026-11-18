#!/usr/bin/env bash
# usb|portal — installer
# Usage: sudo ./install.sh [install|uninstall|check|update]
set -euo pipefail

APP_NAME="usb-portal"
DISPLAY_NAME="usb|portal"
INSTALL_LIB="/usr/local/lib/usb-portal"
INSTALL_BIN="/usr/local/bin"
MODULE_PERSIST="/etc/modules-load.d/usb-portal.conf"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

LEGACY_APP_NAME="gamepad-share"
LEGACY_DAEMON_NAME="gamepad-share-daemon"
LEGACY_INSTALL_LIB="/usr/local/lib/gamepad-share"
LEGACY_MODULE_PERSIST="/etc/modules-load.d/gamepad-share.conf"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()    { echo -e "${CYAN}  ℹ  ${NC}$*"; }
success() { echo -e "${GREEN}  ✓  ${NC}$*"; }
warn()    { echo -e "${YELLOW}  ⚠  ${NC}$*"; }
error()   { echo -e "${RED}  ✗  ${NC}$*" >&2; }
header()  { echo -e "\n${BOLD}${CYAN}$*${NC}\n"; }

# ---------------------------------------------------------------------------
# Distro detection
# ---------------------------------------------------------------------------

detect_family() {
    local id="" like=""
    if [[ -f /etc/os-release ]]; then
        id=$(grep -oP '^ID=\K.*' /etc/os-release | tr -d '"' | tr '[:upper:]' '[:lower:]')
        like=$(grep -oP '^ID_LIKE=\K.*' /etc/os-release 2>/dev/null | tr -d '"' | tr '[:upper:]' '[:lower:]' || true)
    fi
    local all="$id $like"
    for deb in debian ubuntu raspbian linuxmint pop elementary kali; do
        [[ "$all" == *"$deb"* ]] && echo "debian" && return
    done
    for rpm in fedora rhel centos rocky almalinux ol; do
        [[ "$all" == *"$rpm"* ]] && echo "fedora" && return
    done
    for arch in arch manjaro endeavouros garuda; do
        [[ "$all" == *"$arch"* ]] && echo "arch" && return
    done
    [[ "$all" == *"suse"* ]] && echo "suse" && return
    [[ "$all" == *"alpine"* ]] && echo "alpine" && return
    echo "unknown"
}

is_arm() {
    local arch
    arch=$(uname -m)
    [[ "$arch" == arm* || "$arch" == aarch64 ]]
}

is_raspberry_pi() {
    [[ -f /proc/device-tree/model ]] && grep -qi "raspberry pi" /proc/device-tree/model 2>/dev/null
}

# ---------------------------------------------------------------------------
# Dependency installation
# ---------------------------------------------------------------------------

install_system_deps() {
    local family
    family=$(detect_family)

    header "Installing system dependencies (${family})"

    case "$family" in
        debian)
            apt-get update -qq
            if is_arm; then
                # Raspberry Pi OS / Debian ARM: linux-tools-generic doesn't exist
                apt-get install -y usbip usbutils python3 python3-rich gcc libc6-dev
            else
                # Try linux-tools-generic; fall back gracefully if not found
                apt-get install -y usbutils python3 python3-rich gcc libc6-dev || true
                apt-get install -y linux-tools-generic usbip 2>/dev/null || \
                apt-get install -y usbip 2>/dev/null || \
                warn "Could not install usbip automatically. Install it manually."
            fi
            ;;
        fedora)
            dnf install -y usbip usbip-utils python3 python3-rich gcc glibc-devel
            ;;
        arch)
            pacman -Sy --noconfirm usbip python python-rich gcc
            ;;
        suse)
            zypper install -y usbip python3 python3-rich gcc glibc-devel
            ;;
        alpine)
            apk add usbip usbip-utils python3 py3-rich gcc musl-dev
            ;;
        *)
            warn "Unknown distro. Please install manually:"
            warn "  - usbip / usbipd"
            warn "  - python3"
            warn "  - python3-rich  (or: pip3 install rich)"
            ;;
    esac

    # Verify python3-rich is importable; fall back to pip if not in repos
    if ! python3 -c "import rich" 2>/dev/null; then
        warn "python3-rich not available from package manager. Installing via pip..."
        python3 -m pip install --quiet rich || {
            error "Could not install 'rich'. Install it manually: pip3 install rich"
            exit 1
        }
    fi

    success "System dependencies installed."
}

# ---------------------------------------------------------------------------
# Kernel modules
# ---------------------------------------------------------------------------

load_modules() {
    header "Loading USB/IP kernel modules"

    local modules=(usbip_core usbip_host vhci_hcd)
    for mod in "${modules[@]}"; do
        if modprobe "$mod" 2>/dev/null; then
            success "Loaded: $mod"
        else
            warn "Could not load $mod (may already be built-in or unavailable)"
        fi
    done

    # Persist across reboots
    printf '%s\n' "${modules[@]}" > "$MODULE_PERSIST"
    success "Modules will auto-load on boot ($MODULE_PERSIST)."
}

# ---------------------------------------------------------------------------
# File installation
# ---------------------------------------------------------------------------

migrate_old_install() {
    # Stop and remove legacy service if present
    if systemctl is-active --quiet "$LEGACY_APP_NAME" 2>/dev/null; then
        systemctl stop "$LEGACY_APP_NAME" && info "Stopped legacy $LEGACY_APP_NAME service."
    fi
    if systemctl is-enabled --quiet "$LEGACY_APP_NAME" 2>/dev/null; then
        systemctl disable "$LEGACY_APP_NAME" && info "Disabled legacy $LEGACY_APP_NAME service."
    fi
    local old_unit="/etc/systemd/system/${LEGACY_APP_NAME}.service"
    if [[ -f "$old_unit" ]]; then
        rm -f "$old_unit"
        systemctl daemon-reload
        info "Removed old service unit: $old_unit"
    fi
    # Migrate state directory
    if [[ -d /var/lib/gamepad-share ]] && [[ ! -d /var/lib/usb-portal ]]; then
        mv /var/lib/gamepad-share /var/lib/usb-portal
        info "Migrated state: /var/lib/gamepad-share → /var/lib/usb-portal"
    fi
    # Migrate module persist
    if [[ -f "$LEGACY_MODULE_PERSIST" ]]; then
        rm -f "$LEGACY_MODULE_PERSIST"
    fi
    # Remove legacy binaries
    rm -f "$INSTALL_BIN/$LEGACY_APP_NAME" "$INSTALL_BIN/$LEGACY_DAEMON_NAME"
    rm -rf "$LEGACY_INSTALL_LIB"
}

install_files() {
    header "Installing application files"

    migrate_old_install

    # Prepare and compile before touching the current installation.
    local package_stage restart_service=false
    package_stage=$(mktemp -d "${INSTALL_LIB}.new.XXXXXX")
    chmod 755 "$package_stage"
    cp -r "$SRC_DIR/usb_portal" "$package_stage/"
    # Keep application code readable even when a privileged caller uses umask 077
    # for private backups. State/log permissions are managed separately.
    find "$package_stage/usb_portal" -type d -exec chmod 755 {} +
    find "$package_stage/usb_portal" -type f -exec chmod 644 {} +
    # Build for the target architecture; never copy a foreign prebuilt library.
    rm -f "$package_stage/usb_portal/native/socket_policy.so"
    if command -v cc >/dev/null 2>&1; then
        if ! cc -shared -fPIC -O2 -Wall -Wextra -Werror \
            -o "$package_stage/usb_portal/native/socket_policy.so" \
            "$package_stage/usb_portal/native/socket_policy.c" -ldl; then
            rm -rf "$package_stage"
            error "Socket policy build failed; current installation was preserved."
            return 1
        fi
        chmod 755 "$package_stage/usb_portal/native/socket_policy.so"
        success "Built USB/IP latency and connection recovery policy."
    else
        warn "C compiler unavailable; install gcc and run update to enable bounded VPN recovery."
    fi
    if systemctl is-active --quiet usb-portal 2>/dev/null; then
        restart_service=true
        systemctl stop usb-portal
    fi
    rm -rf "$INSTALL_LIB"
    mv "$package_stage" "$INSTALL_LIB"
    success "Copied package → $INSTALL_LIB/usb_portal/"

    # Main CLI wrapper
    cat > "$INSTALL_BIN/usb-portal" << EOF
#!/bin/bash
export PYTHONPATH="$INSTALL_LIB"
exec python3 -c 'import runpy, sys; sys.path[0] = "$INSTALL_LIB"; runpy.run_module("usb_portal.main", run_name="__main__")' "\$@"
EOF
    chmod 755 "$INSTALL_BIN/usb-portal"
    success "Created: $INSTALL_BIN/usb-portal"

    # Daemon wrapper (used by systemd ExecStart)
    cat > "$INSTALL_BIN/usb-portal-daemon" << EOF
#!/bin/bash
export PYTHONPATH="$INSTALL_LIB"
exec python3 -c 'import runpy, sys; sys.path[0] = "$INSTALL_LIB"; runpy.run_module("usb_portal.daemon", run_name="__main__")' "\$@"
EOF
    chmod 755 "$INSTALL_BIN/usb-portal-daemon"
    success "Created: $INSTALL_BIN/usb-portal-daemon"
    if [[ "$restart_service" == true ]]; then
        systemctl start usb-portal
        info "Service restarted."
    fi
}

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

do_uninstall() {
    header "Uninstalling usb|portal"

    for svc_name in "$APP_NAME" "$LEGACY_APP_NAME"; do
        if systemctl is-active --quiet "$svc_name" 2>/dev/null; then
            systemctl stop "$svc_name" && info "Service stopped: $svc_name"
        fi
        if systemctl is-enabled --quiet "$svc_name" 2>/dev/null; then
            systemctl disable "$svc_name" && info "Service disabled: $svc_name"
        fi
        local unit="/etc/systemd/system/${svc_name}.service"
        [[ -f "$unit" ]] && rm "$unit" && info "Removed: $unit"
    done
    systemctl daemon-reload 2>/dev/null || true

    # Remove files
    rm -rf "$INSTALL_LIB" "$LEGACY_INSTALL_LIB"
    rm -f "$INSTALL_BIN/usb-portal" "$INSTALL_BIN/usb-portal-daemon"
    rm -f "$INSTALL_BIN/$LEGACY_APP_NAME" "$INSTALL_BIN/$LEGACY_DAEMON_NAME"
    rm -f "$MODULE_PERSIST" "$LEGACY_MODULE_PERSIST"

    success "usb|portal uninstalled."
}

# ---------------------------------------------------------------------------
# Preflight check
# ---------------------------------------------------------------------------

do_check() {
    header "usb|portal — System Check"

    local ok=true

    check_item() {
        local label="$1" cmd="$2"
        if eval "$cmd" &>/dev/null; then
            echo -e "  ${GREEN}✓${NC}  $label"
        else
            echo -e "  ${RED}✗${NC}  $label"
            ok=false
        fi
    }

    check_item "python3 available"        "command -v python3"
    check_item "python3-rich importable"  "python3 -c 'import rich'"
    check_item "usbip command"            "command -v usbip"
    check_item "usbipd command"           "command -v usbipd"
    check_item "Running as root"          "[[ \$EUID -eq 0 ]]"
    check_item "Module: usbip_core"       "lsmod | grep -q usbip_core"
    check_item "Module: usbip_host"       "lsmod | grep -q usbip_host"
    check_item "Module: vhci_hcd"         "lsmod | grep -q vhci_hcd"

    if [[ "$ok" == true ]]; then
        echo -e "\n${GREEN}${BOLD}All checks passed.${NC}"
    else
        echo -e "\n${YELLOW}${BOLD}Some checks failed. Run: sudo ./install.sh${NC}"
    fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

ACTION="${1:-install}"

if [[ "$ACTION" != "check" && $EUID -ne 0 ]]; then
    error "This script must be run as root."
    echo "  Usage: sudo ./install.sh [install|uninstall|update|check]"
    exit 1
fi

case "$ACTION" in
    install)
        header "usb|portal — Installer"
        if is_raspberry_pi; then
            info "Detected: Raspberry Pi"
        fi
        install_system_deps
        load_modules
        install_files
        echo ""
        success "Installation complete!"
        echo ""
        echo -e "  ${BOLD}Run with:${NC}  sudo usb-portal"
        echo -e "  ${BOLD}Check:${NC}     sudo usb-portal check"
        echo ""
        ;;
    update)
        header "usb|portal — Update"
        install_files
        echo ""
        success "Update complete!"
        echo ""
        ;;
    uninstall)
        do_uninstall
        ;;
    check)
        do_check
        ;;
    *)
        error "Unknown action: $ACTION"
        echo "  Usage: sudo ./install.sh [install|uninstall|update|check]"
        exit 1
        ;;
esac
