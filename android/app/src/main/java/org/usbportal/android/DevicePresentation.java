package org.usbportal.android;

import android.hardware.usb.UsbDevice;
import android.hardware.usb.UsbInterface;
import android.view.InputDevice;
import org.usbportal.core.DeviceKind;

/** Read-only icon hints: classification never opens or claims a USB interface. */
final class DevicePresentation {
    private DevicePresentation() {}
    static String name(UsbDevice device) {
        try {
            String name = device.getProductName();
            if (name != null && !name.trim().isEmpty()) return name.trim();
        } catch (RuntimeException ignored) { /* OEM descriptors can be unavailable before permission. */ }
        return "USB device";
    }
    static DeviceKind kind(UsbDevice device) {
        int[][] interfaces = new int[device.getInterfaceCount()][];
        for (int i = 0; i < interfaces.length; i++) {
            UsbInterface iface = device.getInterface(i);
            interfaces[i] = new int[] {iface.getInterfaceClass(), iface.getInterfaceSubclass(), iface.getInterfaceProtocol()};
        }
        boolean controller = false;
        try {
            for (int id : InputDevice.getDeviceIds()) {
                InputDevice input = InputDevice.getDevice(id);
                if (input == null || input.isVirtual() || input.getVendorId() != device.getVendorId() ||
                        input.getProductId() != device.getProductId()) continue;
                int sources = input.getSources();
                if ((sources & InputDevice.SOURCE_GAMEPAD) == InputDevice.SOURCE_GAMEPAD ||
                        (sources & InputDevice.SOURCE_JOYSTICK) == InputDevice.SOURCE_JOYSTICK) {
                    controller = true; break;
                }
            }
        } catch (RuntimeException ignored) { /* Cosmetic hint only; USB sharing does not depend on it. */ }
        return DeviceKind.detect(name(device), device.getDeviceClass(), interfaces, controller);
    }
    static String label(DeviceKind kind) {
        switch (kind) {
            case GAMEPAD: return "Game controller";
            case KEYBOARD: return "Keyboard";
            case MOUSE: return "Mouse";
            case PRINTER: return "Printer";
            case STORAGE: return "Storage";
            case CAMERA: return "Camera";
            case AUDIO: return "Audio device";
            case HUB: return "USB hub";
            default: return "USB device";
        }
    }
}
