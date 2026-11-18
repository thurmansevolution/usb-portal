package org.usbportal.core;

import java.util.EnumSet;
import java.util.Locale;
import java.util.regex.Pattern;

/** Best-effort presentation category; this does not indicate USB sharing support. */
public enum DeviceKind {
    GAMEPAD, KEYBOARD, MOUSE, PRINTER, STORAGE, CAMERA, AUDIO, HUB, UNKNOWN;

    private static final Pattern CONTROLLER_NAME = Pattern.compile(
            "(?:^|[^a-z0-9])(?:game[ -]?pad|joystick|snes|super nintendo|dolphin[ -]?bar)(?:$|[^a-z0-9])");
    private static final Pattern MAYFLASH_NAME = Pattern.compile("(?:^|[^a-z0-9])mayflash(?:$|[^a-z0-9])");
    private static final Pattern WIIMOTE_NAME = Pattern.compile("(?:^|[^a-z0-9])wii[ -]?mote(?:$|[^a-z0-9])");

    /**
     * Classifies descriptors without opening a device or requesting USB permission.
     * Each interface is a {class, subclass, protocol} triple. The boolean is
     * independent evidence from a matching Android game-controller input device.
     * Explicit descriptor types take precedence over that evidence and name hints.
     * Conflicting interface types remain unknown rather than depending on order.
     */
    public static DeviceKind detect(String name, int deviceClass, int[][] interfaces,
                                    boolean gameController) {
        DeviceKind direct = classKind(deviceClass);
        if (direct != UNKNOWN) return direct;

        EnumSet<DeviceKind> kinds = EnumSet.noneOf(DeviceKind.class);
        if (interfaces != null) {
            for (int[] iface : interfaces) {
                if (iface == null || iface.length < 3 || !unsignedByte(iface[0]) ||
                        !unsignedByte(iface[1]) || !unsignedByte(iface[2])) continue;
                DeviceKind kind = classKind(iface[0]);
                if (iface[0] == 3 && iface[1] == 1) {
                    if (iface[2] == 1) kind = KEYBOARD;
                    else if (iface[2] == 2) kind = MOUSE;
                }
                if (kind != UNKNOWN) kinds.add(kind);
            }
        }
        if (kinds.size() == 1) return kinds.iterator().next();
        if (kinds.size() > 1) return UNKNOWN;
        if (gameController) return GAMEPAD;

        String lower = name == null ? "" : name.toLowerCase(Locale.ROOT);
        if (CONTROLLER_NAME.matcher(lower).find() ||
                (MAYFLASH_NAME.matcher(lower).find() && WIIMOTE_NAME.matcher(lower).find()))
            return GAMEPAD;
        return UNKNOWN;
    }

    private static boolean unsignedByte(int value) { return value >= 0 && value <= 255; }

    private static DeviceKind classKind(int usbClass) {
        switch (usbClass) {
            case 1: return AUDIO;
            case 6: return CAMERA;
            case 7: return PRINTER;
            case 8: return STORAGE;
            case 9: return HUB;
            case 14: return CAMERA;
            default: return UNKNOWN;
        }
    }
}
