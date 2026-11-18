# USBPortal for Android

Share USB devices **plugged into Android** with the existing Linux USB/IP client.
This is a native sideloadable APK, with remote-control navigation for Android TV
and touch controls for phones/tablets. It uses public Android USB Host APIs;
root, ADB, a custom kernel, and Android USB/IP kernel modules are not required.
Apple TV and receiving remote devices on Android are outside this port's scope.

The current build is `1.3.0-android.2` and remains an alpha. It introduces a dark,
smoked-glass native interface with Devices, Settings, and Diagnostics screens,
device-type icons, and remote/touch navigation. The USB/IP transport and Android
permission model are unchanged from `android.1`.

The initial build was tested on a Shield with a physical SNES adapter imported
by Ubuntu. A two-minute gameplay check verified controller reports and forwarded
Linux input without a reconnect or reported USB transfer failure. Permission
restoration and longer recovery tests remain unverified. The network/protocol
core has also been tested with Ubuntu's real USB/IP kernel client using a
synthetic controller. The redesigned interface has native rendering and lifecycle
checks; physical Shield navigation and permission recovery still need testing.

## Install without ADB

1. Copy `dist/usbportal-android-1.3.0-android.2.apk` to Android using a USB drive,
   a network file share, or another file-transfer method you already use.
2. Open the APK with a file manager and allow that installer to install unknown
   apps if Android asks. Install over the existing app to retain its settings;
   do not uninstall first. Open **USBPortal** from the TV or Android launcher.
3. Plug in a controller. On **Devices**, select **Allow USB access & share** and approve
   Android's USB dialog. Devices already granted permission have a separate
   **Share & remember** button. No unselected device is exported.
4. Select **Start sharing**. The screen shows the host's IPv4 address and each
   exported device's bus ID, such as `1-1`. If Android offers a default USB app
   choice, select USBPortal for the devices you want to share automatically.
5. In Ubuntu's existing USBPortal interface, discover the Android host or add its
   address to **Saved Hosts**, then connect and enable **auto-connect** for each
   device. Both ends must remember their respective sharing/connection intent.

If an older Linux installation intermittently misses the Android host, update
its discovery scanner and reopen `sudo usb-portal`. The November 8 discovery fix
gives active scans a dedicated reply port so the background service cannot
receive the terminal interface's replies. Connecting through a saved IP also
avoids waiting for LAN discovery; no Android reinstall is required for this fix.

For a direct check on Ubuntu (replace `ANDROID_IP` and `1-1` with the displayed
address and bus ID):

```sh
sudo usbip list -r ANDROID_IP
sudo usbip attach -r ANDROID_IP -b 1-1
sudo usbip port
```

One remote client can import each USB device at a time. During an import,
Android's local driver is detached; after release, Android's USB API releases
the interface and requests driver reconnection. If a vendor's driver does not
resume, unplug and reconnect the adapter. Multiple identical devices are
separate exports.

## Interface and device icons

**Devices** shows the host controls and attached/remembered USB devices.
**Settings** holds reboot behavior, the client filter, and USB permission help.
**Diagnostics** shows service status, host addresses, and the latest 12 connection
messages for this service session, with a copy button. It is not a complete or
persistent log history. Returning home leaves active foreground sharing running;
Back from Settings or Diagnostics returns to Devices.

Icons cover game controllers, keyboards, mice, printers, storage, cameras, audio,
and hubs. They use USB class/interface descriptors, Android controller input
information, and narrow controller-name hints without opening or claiming a USB
interface. Generic HID does not automatically mean gamepad. Unidentified devices
use a USB-C plug illustration; this does not assert their physical connector.
Older remembered/offline slots have no saved type/name and use that fallback.
Icons do not expand device compatibility or Android's permission policy.

The glass appearance uses static native gradients and borders rather than
live blur. There are no new runtime UI libraries or network-fetched assets.

## Remembering, reboots and permissions

Selections are stored in Android's private app storage using synchronous
SharedPreferences commits. **Stop sharing** pauses the server and retains those
selections. **Stop sharing & forget** removes that device's intent.
Disconnected remembered devices remain visible and can also be forgotten.

**Resume sharing after reboot** starts the foreground service after boot if
sharing was running. The app must have been opened once; Android force-stop and
some vendor background restrictions prevent automatic restart. The notification
provides status and a stop button. A failed listener start is retried while
sharing remains enabled.

Android's temporary USB permission normally lasts until disconnection. Saved
sharing intent cannot bypass a new permission dialog. Choosing the default USB
app can improve reconnection, but unattended permission restoration depends on
Android/OEM policy. The app shows **USB permission needed**, preserves intent,
and resumes exporting when access returns. Rebooting Android or reconnecting an
adapter therefore cannot be promised completely unattended on every stock OS.

Enabling **USB debugging** alone does not grant USBPortal USB-device access or
remove the attach/permission dialog. ADB is a separate development tool; this
app does not use it. Accepting Android's USB app-opening dialog can itself grant
device access, so declining it can leave a remembered device waiting for approval.
Once Android grants access, remembered devices resume sharing while the host is
running; the Linux client's saved auto-connect setting controls its reconnection.

Devices with serial numbers are matched using a stored SHA-256 digest. Anonymous
adapters are matched by vendor/product and the number of remembered selections;
existing assignments are preserved while attached. Android's public API does
not expose a reliable physical USB port path. Identical serial-less adapters
may exchange player assignments across reboots or reconnects, even when left
in the same physical ports. Bus IDs belong to remembered slots, not USB device
addresses assigned by Android.

## LAN, VPN and power

TCP **3240** carries USB/IP; UDP **3241** carries compatible USBPortal discovery.
The server listens across interfaces. LAN broadcasts and unicast discovery are
supported. From Ubuntu's **Use Mode → Discover hosts on LAN → Saved Subnets**,
add a remote private subnet/CIDR, then select it under **Scan Subnet** to discover
Android hosts across routed VLANs or a VPN;
broadcast forwarding is not required. Linux supports large private IPv4 ranges,
including `/16` and `10.0.0.0/8`, as well as the shared VPN range `100.64.0.0/10`.
Scans show progress, retry unanswered queries, and retain results when stopped
with Ctrl+C. A full `/16` scan takes approximately two minutes; a `/8` takes
hours. Use the narrowest useful CIDR. Saved large networks are searched in the
background when a host's address changes, while known-IP reconnects continue.
With a blank client filter, Android permits private peers from other subnets as
well as its own. Explicit filters must include the client's routed source IP.
Ping alone does not confirm that UDP 3241 and TCP 3240 are allowed. OpenVPN
remains a separate application; its routing/firewall and per-app VPN settings
must permit USBPortal traffic and the Ubuntu client's return route.

Connections use TCP_NODELAY and keepalive; the app attempts Linux TCP keepalive
idle 5 s, interval 3 s, count 3, and user timeout 15 s. Unsupported kernel options
are reported in the connection log. Once a dead import releases, the existing
Ubuntu auto-connect loop can import it again. Saving a host found by CIDR scan
retains that network and its application identity for address rediscovery.
Hosts added only by IP need a saved network or manual address update after an IP
change. No VPN or router configuration is changed by this app.

The optional client filter accepts comma/space-separated literal addresses or
CIDRs, for example `192.168.1.10,10.8.0.0/24`. Blank permits private LAN/VPN,
loopback, link-local, and CGNAT addresses; explicit rules replace that default.
The filter also applies to discovery queries. It is an address filter, not peer
authentication. USB/IP remains unencrypted; trusted LAN/VPN use only. Internet
exposure is unsupported.

Sharing keeps a CPU wake lock and requests Wi-Fi performance/discovery locks.
This increases battery use. On mobile devices, use external power and Android's
unrestricted battery setting when uninterrupted background hosting matters.
Android deep idle, OEM power management, Wi-Fi roaming, and powered-off USB ports
can still interrupt hosting. Ethernet is preferable when available. A powered
USB hub/OTG adapter may be necessary on phones/tablets.

## Device and protocol limits

- Android **8.0/API 26 or newer**, with USB Host/OTG hardware support. There are no
  architecture-specific native binaries in the APK.
- The initial target is HID gamepads. Control, interrupt and bulk transfers are
  implemented; other device classes require hardware testing before relying on
  them. Android can restrict certain USB devices, including boot keyboards/mice.
- Isochronous devices (many USB audio interfaces/webcams), USB hubs, and devices
  with multiple configurations are rejected. Physical USB resets and bulk OUT
  `URB_ZERO_PACKET` are unsupported. This is not universal USB passthrough.
- Transfers are limited to 64 KiB and 32 pending transfers per import, with 12
  concurrent TCP clients/handshakes. Android 8's own UsbRequest limit can further
  restrict requests to 16 KiB. Gamepad reports are much smaller.
- Android's public asynchronous API does not expose every native URB status or
  exact negotiated USB speed. Speed is inferred conservatively from descriptors;
  native endpoint scheduling remains under Android's USB host controller.
  USB/IP flags that Android cannot express have limited support.
- No latency or live Shield compatibility claim is made without hardware testing.

## Build and test

Use a JDK **17+** and the official Android SDK with `platforms;android-35` and
`build-tools;35.0.0`. No Gradle, NDK, third-party runtime library, or network
dependency resolution is needed once those tools are installed.

```sh
export ANDROID_SDK_ROOT=/path/to/Android/Sdk
export JAVA_HOME=/path/to/jdk
bash android/build.sh
bash android/test.sh
```

The build verifies its APK signature and writes a `.sha256` file. Its private
development signing key is generated under `android/.signing/` and ignored by
Git. Preserve it to install future builds as updates; losing it requires an
uninstall, which deletes app settings. For another signing key set
`USBPORTAL_KEYSTORE`, `USBPORTAL_KEY_ALIAS`, and `USBPORTAL_KEYSTORE_PASSWORD`.
Never publish the signing key. Output is under `android/dist/`.

The test runner checks the USB/IP protocol with real loopback sockets and a
synthetic USB backend, plus the dependency-free device-icon classifier. To expose
that **test fixture only** on loopback for a native Linux-client check:

```sh
bash android/test.sh --serve 3248
sudo usbip --tcp-port 3248 list -r 127.0.0.1
sudo usbip --tcp-port 3248 attach -r 127.0.0.1 -b 1-50
# Find the corresponding port with usbip port, then detach it when finished.
```

Alternatively, with the fixture running and no existing import of it, run
`sudo python3 android/tests/linux_kernel_smoke.py`. It checks two import/input/
detach cycles and preserves unrelated imports.

Tests and the synthetic backend are excluded from the APK. The protocol layer
is in `org.usbportal.core`; Android USB, permissions, storage, UI and lifecycle
are in `org.usbportal.android`.

Platform references: [Android USB Host API](https://developer.android.com/develop/connectivity/usb/host),
[UsbRequest](https://developer.android.com/reference/android/hardware/usb/UsbRequest),
[connected-device foreground services](https://developer.android.com/develop/background-work/services/fgs/service-types#connected-device),
[USB/IP protocol](https://docs.kernel.org/usb/usbip_protocol.html).
