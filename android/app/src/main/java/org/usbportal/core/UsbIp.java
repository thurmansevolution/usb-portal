package org.usbportal.core;

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.*;

/** USB/IP 1.1.1 wire format. This layer deliberately has no Android dependency. */
public final class UsbIp {
    public static final int VERSION = 0x0111;
    public static final int MAX_TRANSFER = 65536, MAX_PENDING = 32;
    public static final int EIO = -5, EPIPE = -32, EINVAL = -22, ENODEV = -19;
    private UsbIp() {}

    public static final class Device {
        public final String path, busId;
        public final int bus, number, speed, vendor, product, revision;
        public final int deviceClass, subclass, protocol, configuration, configurations;
        public final byte[] interfaces;
        public Device(String path, String busId, int bus, int number, int speed,
                      int vendor, int product, int revision, int deviceClass,
                      int subclass, int protocol, int configuration, int configurations,
                      byte[] interfaces) {
            if (!busId.matches("[0-9]+-[0-9.]+") || busId.length() > 31 ||
                    interfaces.length % 4 != 0 || interfaces.length > 1020) {
                throw new IllegalArgumentException("Invalid exported device");
            }
            this.path = path; this.busId = busId; this.bus = bus; this.number = number;
            this.speed = speed; this.vendor = vendor; this.product = product;
            this.revision = revision; this.deviceClass = deviceClass;
            this.subclass = subclass; this.protocol = protocol;
            this.configuration = configuration; this.configurations = configurations;
            this.interfaces = interfaces.clone();
        }
        public int id() { return (bus << 16) | number; }
        public void write(DataOutputStream out, boolean withInterfaces) throws IOException {
            writeString(out, path, 256); writeString(out, busId, 32);
            out.writeInt(bus); out.writeInt(number); out.writeInt(speed);
            out.writeShort(vendor); out.writeShort(product); out.writeShort(revision);
            out.writeByte(deviceClass); out.writeByte(subclass); out.writeByte(protocol);
            out.writeByte(configuration); out.writeByte(configurations);
            out.writeByte(interfaces.length / 4);
            if (withInterfaces) out.write(interfaces);
        }
    }

    public static void writeString(DataOutputStream out, String value, int length) throws IOException {
        byte[] field = new byte[length];
        byte[] encoded = value.getBytes(StandardCharsets.UTF_8);
        System.arraycopy(encoded, 0, field, 0, Math.min(encoded.length, length - 1));
        out.write(field);
    }
    public static String readBusId(DataInputStream in) throws IOException {
        byte[] bytes = new byte[32]; in.readFully(bytes);
        int end = 0; while (end < bytes.length && bytes[end] != 0) end++;
        if (end == bytes.length) throw new IOException("Unterminated bus ID");
        String value = new String(bytes, 0, end, StandardCharsets.US_ASCII);
        if (!value.matches("[0-9]+-[0-9.]+")) throw new IOException("Invalid bus ID");
        return value;
    }
    public static void reply(DataOutputStream out, int code, int status) throws IOException {
        out.writeShort(VERSION); out.writeShort(code); out.writeInt(status);
    }

    public static final class Transfer {
        public final int sequence, device, direction, endpoint, flags, length, interval;
        public final byte[] setup, data;
        private Transfer(int sequence, int device, int direction, int endpoint,
                         int flags, int length, int interval, byte[] setup, byte[] data) {
            this.sequence = sequence; this.device = device; this.direction = direction;
            this.endpoint = endpoint; this.flags = flags; this.length = length;
            this.interval = interval; this.setup = setup; this.data = data;
        }
        public static Transfer read(DataInputStream in, int seq, int dev, int dir, int ep) throws IOException {
            int flags = in.readInt(), length = in.readInt();
            in.readInt(); // start frame (only meaningful for isochronous transfers)
            int packets = in.readInt(), interval = in.readInt();
            byte[] setup = new byte[8]; in.readFully(setup);
            if ((dir != 0 && dir != 1) || ep < 0 || ep > 15 || length < 0 ||
                    length > MAX_TRANSFER || (packets != 0 && packets != -1)) {
                throw new IOException("Unsupported or oversized transfer");
            }
            if (ep == 0 && (u16(setup, 6) != length || ((setup[0] & 0x80) >>> 7) != dir)) {
                throw new IOException("Control setup does not match transfer");
            }
            byte[] data = dir == 0 ? new byte[length] : new byte[0];
            in.readFully(data);
            return new Transfer(seq, dev, dir, ep, flags, length, interval, setup, data);
        }
        public int address() { return endpoint | (direction == 1 ? 0x80 : 0); }
        public int requestType() { return setup[0] & 255; }
        public int request() { return setup[1] & 255; }
        public int value() { return u16(setup, 2); }
        public int index() { return u16(setup, 4); }
    }
    public static int u16(byte[] b, int offset) {
        return (b[offset] & 255) | ((b[offset + 1] & 255) << 8);
    }

    public interface Completion {
        void complete(int status, int actualLength, byte[] data);
    }
    public interface Backend extends AutoCloseable {
        Device device();
        void onDisconnect(Runnable listener);
        void submit(Transfer transfer, Completion completion) throws IOException;
        void cancel(int sequence);
        @Override void close();
    }
    public interface Registry {
        List<Device> list();
        /** Exclusively leases an export; returns null for unavailable/unknown devices. */
        Backend acquire(String busId) throws IOException;
    }
}
