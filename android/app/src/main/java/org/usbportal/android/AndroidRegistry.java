package org.usbportal.android;

import android.hardware.usb.*;
import org.usbportal.core.UsbIp;
import java.io.*;
import java.util.*;
import java.util.function.Consumer;

final class AndroidRegistry implements UsbIp.Registry, AutoCloseable {
    static final class Row {
        final UsbDevice device;
        final int slot;
        final String status;
        final boolean ready, inUse;
        final String error;
        Row(UsbDevice device, int slot, String status, boolean ready, boolean inUse, String error) {
            this.device = device; this.slot = slot; this.status = status;
            this.ready = ready; this.inUse = inUse; this.error = error;
        }
    }
    private static final class Export {
        final UsbDevice device;
        final UsbIp.Device description;
        boolean reserved;
        AndroidBackend backend;
        Export(UsbDevice device, UsbIp.Device description) { this.device = device; this.description = description; }
    }
    final UsbManager manager;
    private final ShareStore store;
    private final Consumer<String> log;
    private List<ShareStore.Slot> slots;
    private final Map<Integer, String> assigned = new HashMap<>();
    private final Map<Integer, Export> exports = new HashMap<>();
    private final Map<String, String> errors = new HashMap<>();
    private boolean closed;
    AndroidRegistry(UsbManager manager, ShareStore store, Consumer<String> log) {
        this.manager = manager; this.store = store; this.log = log; slots = store.load();
    }
    void refresh() {
        Map<String, UsbDevice> attached = manager.getDeviceList();
        List<AndroidBackend> stop = new ArrayList<>();
        synchronized (this) {
            if (closed) return;
            errors.clear();
            assigned.entrySet().removeIf(e -> !attached.containsKey(e.getValue()) ||
                    slots.stream().noneMatch(s -> s.id == e.getKey() &&
                            plausible(s, attached.get(e.getValue()))));
            List<UsbDevice> devices = new ArrayList<>(attached.values());
            devices.sort(Comparator.comparing(UsbDevice::getDeviceName));
            // Preserve existing assignments, then match serial identities before anonymous adapters.
            List<ShareStore.Slot> ordered = new ArrayList<>(slots);
            ordered.sort(Comparator.comparing((ShareStore.Slot s) -> s.serial.isEmpty()).thenComparingInt(s -> s.id));
            for (ShareStore.Slot slot : ordered) {
                if (assigned.containsKey(slot.id)) continue;
                for (UsbDevice device : devices) {
                    if (!assigned.containsValue(device.getDeviceName()) && plausible(slot, device)) {
                        assigned.put(slot.id, device.getDeviceName()); break;
                    }
                }
            }
            Iterator<Map.Entry<Integer, Export>> it = exports.entrySet().iterator();
            while (it.hasNext()) {
                Map.Entry<Integer, Export> e = it.next(); Export export = e.getValue();
                if (!export.device.getDeviceName().equals(assigned.get(e.getKey())) ||
                        !manager.hasPermission(export.device)) {
                    if (export.backend != null) stop.add(export.backend);
                    it.remove();
                }
            }
            for (ShareStore.Slot slot : slots) {
                UsbDevice device = attached.get(assigned.get(slot.id));
                if (device == null || exports.containsKey(slot.id)) continue;
                if (!manager.hasPermission(device)) { errors.put(device.getDeviceName(), "USB permission needed"); continue; }
                try { exports.put(slot.id, new Export(device, describe(device, slot.id))); }
                catch (IOException | RuntimeException e) { errors.put(device.getDeviceName(), e.getMessage()); }
            }
        }
        for (AndroidBackend backend : stop) backend.close();
    }
    private boolean plausible(ShareStore.Slot slot, UsbDevice device) {
        if (device == null) return false;
        boolean permission = manager.hasPermission(device);
        return slot.matches(device, permission) || (!permission && slot.vendor == device.getVendorId() &&
                slot.product == device.getProductId());
    }
    synchronized void remember(UsbDevice device) {
        for (String name : assigned.values()) if (name.equals(device.getDeviceName())) return;
        if (!manager.hasPermission(device)) throw new IllegalStateException("Grant USB permission first");
        if (slots.size() >= 64) throw new IllegalStateException("Maximum 64 remembered devices");
        int id = 1;
        Set<Integer> used = new HashSet<>(); for (ShareStore.Slot slot : slots) used.add(slot.id);
        while (used.contains(id)) id++;
        List<ShareStore.Slot> updated = new ArrayList<>(slots);
        updated.add(new ShareStore.Slot(id, device.getVendorId(), device.getProductId(), ShareStore.serialHash(device)));
        store.save(updated); slots = updated; assigned.put(id, device.getDeviceName());
    }
    void forget(int id) {
        AndroidBackend stop;
        synchronized (this) {
            List<ShareStore.Slot> updated = new ArrayList<>(slots); updated.removeIf(s -> s.id == id);
            store.save(updated); slots = updated; assigned.remove(id);
            Export export = exports.remove(id); stop = export == null ? null : export.backend;
        }
        if (stop != null) stop.close();
    }
    synchronized List<ShareStore.Slot> missing() {
        List<ShareStore.Slot> result = new ArrayList<>();
        for (ShareStore.Slot slot : slots) if (!assigned.containsKey(slot.id)) result.add(slot);
        return result;
    }
    synchronized List<Row> rows() {
        List<Row> result = new ArrayList<>();
        List<UsbDevice> devices = new ArrayList<>(manager.getDeviceList().values());
        devices.sort(Comparator.comparing(UsbDevice::getDeviceName));
        for (UsbDevice device : devices) {
            int slot = 0;
            for (Map.Entry<Integer, String> e : assigned.entrySet())
                if (e.getValue().equals(device.getDeviceName())) { slot = e.getKey(); break; }
            Export export = exports.get(slot);
            String status = errors.get(device.getDeviceName());
            if (status == null) status = export != null ? (export.reserved ? "In use by remote client" : "Ready to share") :
                    (slot == 0 ? "Not shared" : "Waiting for USB permission");
            result.add(new Row(device, slot, status, export != null,
                    export != null && export.reserved, errors.get(device.getDeviceName())));
        }
        return result;
    }
    synchronized int count() { return exports.size(); }
    synchronized int waitingPermission() {
        int count = 0;
        for (String name : assigned.values()) {
            UsbDevice device = manager.getDeviceList().get(name);
            if (device != null && !manager.hasPermission(device)) count++;
        }
        return count;
    }
    @Override public synchronized List<UsbIp.Device> list() {
        List<UsbIp.Device> result = new ArrayList<>();
        for (Export export : exports.values()) result.add(export.description);
        result.sort(Comparator.comparingInt(d -> d.number)); return result;
    }
    @Override public UsbIp.Backend acquire(String busId) throws IOException {
        Export export;
        synchronized (this) {
            export = exports.values().stream().filter(e -> e.description.busId.equals(busId)).findFirst().orElse(null);
            if (closed || export == null || export.reserved) return null;
            export.reserved = true;
        }
        final Export lease = export;
        try {
            AndroidBackend backend = new AndroidBackend(manager, export.device, export.description, () -> {
                synchronized (AndroidRegistry.this) { lease.reserved = false; lease.backend = null; }
            });
            boolean obsolete;
            synchronized (this) {
                obsolete = closed || !exports.containsValue(export);
                if (!obsolete) export.backend = backend;
            }
            if (obsolete) { backend.close(); return null; }
            return backend;
        } catch (IOException | RuntimeException e) {
            synchronized (this) { export.reserved = false; errors.put(export.device.getDeviceName(), e.getMessage()); }
            log.accept("Cannot claim " + busId + ": " + e.getMessage());
            return null;
        }
    }
    private UsbIp.Device describe(UsbDevice device, int slot) throws IOException {
        if (device.getConfigurationCount() != 1) throw new IOException("Only single-configuration USB devices supported");
        UsbConfiguration config = device.getConfiguration(0);
        ByteArrayOutputStream interfaces = new ByteArrayOutputStream();
        int speed = 2;
        for (int i = 0; i < config.getInterfaceCount(); i++) {
            UsbInterface iface = config.getInterface(i);
            if (iface.getInterfaceClass() == 9) throw new IOException("USB hubs cannot be shared");
            if (iface.getAlternateSetting() == 0) {
                interfaces.write(iface.getInterfaceClass()); interfaces.write(iface.getInterfaceSubclass());
                interfaces.write(iface.getInterfaceProtocol()); interfaces.write(0);
            }
            for (int e = 0; e < iface.getEndpointCount(); e++) {
                UsbEndpoint ep = iface.getEndpoint(e);
                if (ep.getType() == UsbConstants.USB_ENDPOINT_XFER_ISOC)
                    throw new IOException("Isochronous USB audio/video is unsupported");
                if (ep.getMaxPacketSize() > 64) speed = 3;
            }
        }
        UsbDeviceConnection connection = manager.openDevice(device);
        if (connection == null) throw new IOException("Cannot read USB descriptors");
        byte[] raw;
        try { raw = connection.getRawDescriptors(); } finally { connection.close(); }
        if (raw == null || raw.length < 18) throw new IOException("Incomplete USB device descriptor");
        if (UsbIp.u16(raw, 2) >= 0x0300) speed = 5;
        return new UsbIp.Device("/android/usb/1-" + slot, "1-" + slot, 1, slot, speed,
                device.getVendorId(), device.getProductId(), UsbIp.u16(raw, 12), device.getDeviceClass(),
                device.getDeviceSubclass(), device.getDeviceProtocol(), config.getId(), 1, interfaces.toByteArray());
    }
    @Override public void close() {
        List<AndroidBackend> stop = new ArrayList<>();
        synchronized (this) {
            closed = true;
            for (Export export : exports.values()) if (export.backend != null) stop.add(export.backend);
            exports.clear();
        }
        for (AndroidBackend backend : stop) backend.close();
    }
}
