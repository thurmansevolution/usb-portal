package org.usbportal.core;

import java.util.Locale;

/** JVM executable tests; independent of Android permission/device implementations. */
public final class DeviceKindTest {
    private static int checks;

    private DeviceKindTest() {}

    public static void main(String[] args) {
        expect(DeviceKind.PRINTER, "USB Printer", 7, null, false);
        expect(DeviceKind.STORAGE, "USB disk", 8, null, false);
        expect(DeviceKind.CAMERA, "Camera", 14, null, false);
        expect(DeviceKind.CAMERA, "PTP still camera", 6, null, false);
        expect(DeviceKind.AUDIO, "Headset", 1, null, false);
        expect(DeviceKind.HUB, "Hub", 9, null, false);
        expect(DeviceKind.PRINTER, null, 0, new int[][] {{7, 1, 2}}, false);
        expect(DeviceKind.STORAGE, null, 0, new int[][] {{8, 6, 80}, {255, 0, 0}}, false);
        expect(DeviceKind.CAMERA, null, 0, new int[][] {{14, 1, 0}, {14, 2, 0}}, false);
        expect(DeviceKind.CAMERA, "Digital camera", 0, new int[][] {{6, 1, 1}}, false);
        expect(DeviceKind.KEYBOARD, "Composite HID", 0, new int[][] {{3, 1, 1}, {3, 0, 0}}, false);
        expect(DeviceKind.MOUSE, "USB mouse", 3, new int[][] {{3, 1, 2}}, false);

        // HID class alone and misleadingly similar names do not prove a gamepad.
        expect(DeviceKind.UNKNOWN, "USB HID", 3, new int[][] {{3, 0, 0}}, false);
        expect(DeviceKind.UNKNOWN, "USB Controller", 3, new int[][] {{3, 0, 0}}, false);
        expect(DeviceKind.UNKNOWN, "USB Adapter", 255, null, false);
        expect(DeviceKind.UNKNOWN, "Gamepadder", 3, null, false);
        expect(DeviceKind.UNKNOWN, "Mayflash adapter", 3, null, false);
        expect(DeviceKind.UNKNOWN, "Wiimote charger", 0, null, false);
        expect(DeviceKind.GAMEPAD, "USB Gamepad", 3, new int[][] {{3, 0, 0}}, false);
        expect(DeviceKind.GAMEPAD, "USB Game Pad", 0, null, false);
        expect(DeviceKind.GAMEPAD, "Retro SNES USB adapter", 0, null, false);
        expect(DeviceKind.GAMEPAD, "Super Nintendo adapter", 0, null, false);
        expect(DeviceKind.GAMEPAD, "USB Joystick", 0, null, false);
        expect(DeviceKind.GAMEPAD, "HJZ Mayflash Wiimote PC Adapter", 3, null, false);
        expect(DeviceKind.GAMEPAD, "Mayflash DolphinBar", 0, null, false);
        expect(DeviceKind.GAMEPAD, "Dolphin Bar", 0, null, false);
        expect(DeviceKind.GAMEPAD, null, 3, new int[][] {{3, 0, 0}}, true);

        // Strong USB evidence wins over names/input hints, even when conflicting.
        expect(DeviceKind.STORAGE, "SNES backup drive", 8, null, true);
        expect(DeviceKind.KEYBOARD, "USB Gamepad", 3, new int[][] {{3, 1, 1}}, true);
        expect(DeviceKind.MOUSE, "USB Gamepad", 3, new int[][] {{3, 1, 2}}, true);
        expect(DeviceKind.PRINTER, "Joystick", 7, new int[][] {{3, 1, 1}}, true);
        expect(DeviceKind.STORAGE, "USB Gamepad", 0, new int[][] {{8, 6, 80}}, true);
        expect(DeviceKind.UNKNOWN, "Gamepad", 0, new int[][] {{7, 1, 2}, {8, 6, 80}}, true);
        expect(DeviceKind.UNKNOWN, "Gamepad", 0, new int[][] {{8, 6, 80}, {7, 1, 2}}, true);
        expect(DeviceKind.UNKNOWN, "Composite HID", 0, new int[][] {{3, 1, 1}, {3, 1, 2}}, false);

        expect(DeviceKind.UNKNOWN, null, -1, null, false);
        expect(DeviceKind.UNKNOWN, "", 256, new int[][] {}, false);
        expect(DeviceKind.UNKNOWN, null, 3, new int[][] {null, {}, {7}, {8, 6}, {-1, 0, 0},
                {256, 0, 0}, {7, -1, 0}, {8, 0, 256}}, false);
        expect(DeviceKind.PRINTER, null, -1, new int[][] {null, {8}, {7, 1, 2}}, false);
        expect(DeviceKind.GAMEPAD, null, -1, new int[][] {null, {8}}, true);

        Locale previous = Locale.getDefault();
        try {
            Locale.setDefault(Locale.forLanguageTag("tr-TR"));
            expect(DeviceKind.GAMEPAD, "USB JOYSTICK", 0, null, false);
        } finally { Locale.setDefault(previous); }
        System.out.println("DeviceKindTest: " + checks + " checks passed");
    }

    private static void expect(DeviceKind expected, String name, int deviceClass,
                               int[][] interfaces, boolean gameController) {
        DeviceKind actual = DeviceKind.detect(name, deviceClass, interfaces, gameController);
        checks++;
        if (actual != expected)
            throw new AssertionError("Expected " + expected + " for " + name + " but got " + actual);
    }
}
