package org.usbportal.core;

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;

/** Synthetic HID transport for protocol tests only; never packaged in the Android APK. */
final class FakeUsb implements UsbIp.Registry, AutoCloseable {
    static final String BUS_ID = "1-50";
    static final int DEVICE_ID = (1 << 16) | 50;
    static final byte[] REPORT = hex("05010905a10115002501750195100509190129108102050109300931150026ff00750895028102c0");
    static final byte[] DEVICE = hex("1201000200000040feca0140000101020001");
    static final byte[] CONFIG = hex("09022200010100803209040000010300000009211101000122000007058103040008");
    static { CONFIG[25] = (byte) REPORT.length; CONFIG[26] = (byte) (REPORT.length >>> 8); }
    static final UsbIp.Device DESCRIPTION = new UsbIp.Device("/test/usb/1-50", BUS_ID, 1, 50, 2,
            0xcafe, 0x4001, 0x0100, 0, 0, 0, 1, 1, new byte[] {3, 0, 0, 0});
    volatile Backend current;
    final AtomicInteger leases = new AtomicInteger(), releases = new AtomicInteger();
    volatile boolean holdInterrupts;
    volatile boolean failSubmit;
    @Override public List<UsbIp.Device> list() { return Collections.singletonList(DESCRIPTION); }
    @Override public synchronized UsbIp.Backend acquire(String busId) {
        if (!BUS_ID.equals(busId) || current != null) return null;
        current = new Backend(); leases.incrementAndGet(); return current;
    }
    final class Backend implements UsbIp.Backend {
        final AtomicBoolean closed = new AtomicBoolean();
        final ConcurrentMap<Integer, UsbIp.Completion> waiting = new ConcurrentHashMap<>();
        final ScheduledExecutorService scheduler = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "fake-usb"); t.setDaemon(true); return t;
        });
        volatile Runnable disconnected = () -> {};
        int report;
        @Override public UsbIp.Device device() { return DESCRIPTION; }
        @Override public void onDisconnect(Runnable listener) {
            disconnected = listener;
            // Match AndroidBackend: detach can occur before the import session registers.
            if (closed.get()) listener.run();
        }
        @Override public void submit(UsbIp.Transfer t, UsbIp.Completion completion) throws IOException {
            if (failSubmit) throw new IOException("Synthetic USB transport failed");
            if (t.endpoint == 1 && t.direction == 1) {
                waiting.put(t.sequence, completion);
                if (!holdInterrupts) scheduler.schedule(() -> {
                    UsbIp.Completion pending = waiting.remove(t.sequence);
                    if (pending != null) {
                        byte[] bytes = {(byte) ((report++ / 10) & 1), 0, (byte)128, (byte)128};
                        int actual = Math.min(bytes.length, t.length);
                        pending.complete(0, actual, Arrays.copyOf(bytes, actual));
                    }
                }, 8, TimeUnit.MILLISECONDS);
                return;
            }
            if (t.endpoint != 0) { completion.complete(UsbIp.EINVAL, 0, new byte[0]); return; }
            byte[] data = new byte[0]; int status = 0;
            if (t.direction == 0) { completion.complete(0, t.length, data); return; }
            if (t.request() == 6) {
                int type = t.value() >>> 8, index = t.value() & 255;
                if (type == 1) data = DEVICE;
                else if (type == 2) data = CONFIG;
                else if (type == 0x22) data = REPORT;
                else if (type == 0x21) data = Arrays.copyOfRange(CONFIG, 18, 27);
                else if (type == 3) {
                    if (index == 0) data = hex("04030904");
                    else data = string(index == 1 ? "USBPortal" : "USBPortal protocol test gamepad");
                } else status = UsbIp.EPIPE;
            } else if (t.requestType() == 0xa1 && t.request() == 1) data = hex("00008080");
            else if (t.request() == 0) data = new byte[2];
            else if (t.request() == 8) data = new byte[] {1};
            else if (t.request() == 10) data = new byte[] {0};
            else status = UsbIp.EPIPE;
            int actual = Math.min(data.length, t.length);
            completion.complete(status, actual, Arrays.copyOf(data, actual));
        }
        @Override public void cancel(int sequence) { waiting.remove(sequence); }
        @Override public void close() {
            if (!closed.compareAndSet(false, true)) return;
            disconnected.run(); scheduler.shutdownNow(); waiting.clear();
            synchronized (FakeUsb.this) { if (current == this) current = null; }
            releases.incrementAndGet();
        }
    }
    @Override public void close() { Backend active = current; if (active != null) active.close(); }
    static byte[] string(String text) {
        byte[] value = text.getBytes(StandardCharsets.UTF_16LE), result = new byte[value.length + 2];
        result[0] = (byte) result.length; result[1] = 3; System.arraycopy(value, 0, result, 2, value.length); return result;
    }
    static byte[] hex(String text) {
        byte[] bytes = new byte[text.length() / 2];
        for (int i = 0; i < bytes.length; i++) bytes[i] = (byte) Integer.parseInt(text.substring(i * 2, i * 2 + 2), 16);
        return bytes;
    }
}
