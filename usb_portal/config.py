USBIP_PORT = 3240
DISCOVERY_PORT = 3241
DISCOVERY_BROADCAST = "<broadcast>"
DISCOVERY_INTERVAL = 5
DISCOVERY_TIMEOUT = 3
RECONNECT_INTERVAL = 3
RECONNECT_MAX_ATTEMPTS = 12
CIDR_SCAN_TIMEOUT = 1.5
CIDR_SCAN_RATE = 1000
CIDR_SCAN_ATTEMPTS = 2
# Retained for older processes/modules during an incremental installation update.
CIDR_SCAN_MAX_WORKERS = 64

APP_NAME = "usb|portal"
APP_VERSION = "1.3.0"

KNOWN_CONTROLLER_VIDS = {
    "054c": "Sony",
    "045e": "Microsoft",
    "046d": "Logitech",
    "0079": "DragonRise",
    "2dc8": "8BitDo",
    "057e": "Nintendo",
    "0f0d": "HORI",
    "1532": "Razer",
    "0e8f": "GreenAsia",
    "24c6": "PowerA",
    "1bad": "Harmonix",
    "0738": "Mad Catz",
    "1038": "SteelSeries",
    "20d6": "PowerA",
    "1a34": "ACRUX",
}

USB_CLASS_HID = "03"
HID_SUBCLASS_BOOT = "01"
HID_PROTOCOL_GAMEPAD = "00"
HID_PROTOCOL_KEYBOARD = "01"
HID_PROTOCOL_MOUSE = "02"
