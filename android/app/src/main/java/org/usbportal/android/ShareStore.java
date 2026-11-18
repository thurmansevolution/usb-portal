package org.usbportal.android;

import android.content.*;
import android.hardware.usb.*;
import org.json.*;
import java.nio.charset.StandardCharsets;
import java.security.*;
import java.util.*;

/** Durable sharing intent, independent of ephemeral Android USB device addresses. */
final class ShareStore {
    static final String PREFS = "usbportal", RUNNING = "running", BOOT = "start_on_boot", CLIENTS = "clients";
    final SharedPreferences prefs;
    static final class Slot {
        final int id, vendor, product;
        final String serial;
        Slot(int id, int vendor, int product, String serial) {
            this.id = id; this.vendor = vendor; this.product = product; this.serial = serial;
        }
        boolean matches(UsbDevice device, boolean permission) {
            return vendor == device.getVendorId() && product == device.getProductId() &&
                    (serial.isEmpty() || (permission && serial.equals(serialHash(device))));
        }
    }
    ShareStore(Context context) { prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE); }
    List<Slot> load() {
        List<Slot> slots = new ArrayList<>(); Set<Integer> ids = new HashSet<>();
        try {
            JSONArray array = new JSONArray(prefs.getString("shares", "[]"));
            if (array.length() > 64) throw new JSONException("Too many saved devices");
            for (int i = 0; i < array.length(); i++) {
                JSONObject v = array.getJSONObject(i);
                int id = v.getInt("id"), vendor = v.getInt("vendor"), product = v.getInt("product");
                if (id < 1 || id > 65535 || !ids.add(id) || vendor < 0 || vendor > 65535 ||
                        product < 0 || product > 65535) throw new JSONException("Invalid saved device");
                slots.add(new Slot(id, vendor, product, v.getString("serial")));
            }
            return slots;
        } catch (JSONException e) { throw new IllegalStateException("Saved sharing settings are invalid", e); }
    }
    void save(List<Slot> slots) {
        JSONArray array = new JSONArray();
        try {
            for (Slot slot : slots) array.put(new JSONObject().put("id", slot.id).put("vendor", slot.vendor)
                    .put("product", slot.product).put("serial", slot.serial));
        } catch (JSONException impossible) { throw new IllegalStateException(impossible); }
        if (!prefs.edit().putString("shares", array.toString()).commit())
            throw new IllegalStateException("Could not save sharing settings");
    }
    static String serialHash(UsbDevice device) {
        try {
            String serial = device.getSerialNumber();
            if (serial == null || serial.isEmpty()) return "";
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(serial.getBytes(StandardCharsets.UTF_8));
            StringBuilder result = new StringBuilder();
            for (byte b : digest) result.append(String.format(Locale.ROOT, "%02x", b & 255));
            return result.toString();
        } catch (SecurityException | NoSuchAlgorithmException e) { return ""; }
    }
}
