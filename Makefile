INSTALL_LIB  := /usr/local/lib/usb-portal
INSTALL_BIN  := /usr/local/bin
APP          := usb-portal

.PHONY: install uninstall check help

help:
	@echo ""
	@echo "  usb|portal — Makefile"
	@echo ""
	@echo "  sudo make install     Install the application"
	@echo "  sudo make uninstall   Remove the application"
	@echo "       make check       Check prerequisites (no root needed)"
	@echo ""

install:
	@bash install.sh install

uninstall:
	@bash install.sh uninstall

check:
	@bash install.sh check
