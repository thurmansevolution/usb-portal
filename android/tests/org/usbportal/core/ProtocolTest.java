package org.usbportal.core;

import java.io.*;
import java.net.*;
import java.util.*;
import java.util.concurrent.*;

/** Real TCP protocol/state tests, with no Android runtime or testing framework dependency. */
public final class ProtocolTest {
    private static int tests;
    interface Check { void run() throws Exception; }
    static void test(String name, Check check) throws Exception {
        check.run(); tests++; System.out.println("PASS " + name);
    }
    static void require(boolean condition, String message) { if (!condition) throw new AssertionError(message); }
    static final class Host implements AutoCloseable {
        final FakeUsb usb = new FakeUsb();
        final UsbIpServer server = new UsbIpServer(usb, new AccessPolicy(""), s -> {}, System.out::println);
        Host() throws IOException { server.start(0); }
        Peer peer() throws IOException { return new Peer(server.port()); }
        @Override public void close() { server.close(); usb.close(); }
    }
    static final class Peer implements AutoCloseable {
        final Socket socket;
        final DataInputStream in;
        final DataOutputStream out;
        Peer(int port) throws IOException {
            socket = new Socket("127.0.0.1", port); socket.setSoTimeout(2500); socket.setTcpNoDelay(true);
            in = new DataInputStream(socket.getInputStream()); out = new DataOutputStream(socket.getOutputStream());
        }
        void handshake(int operation) throws IOException {
            out.writeShort(UsbIp.VERSION); out.writeShort(operation); out.writeInt(0);
        }
        int reply(int expected) throws IOException {
            require(in.readUnsignedShort() == UsbIp.VERSION, "reply version");
            require(in.readUnsignedShort() == expected, "reply operation"); return in.readInt();
        }
        void acquire() throws IOException {
            handshake(0x8003); UsbIp.writeString(out, FakeUsb.BUS_ID, 32); out.flush();
            require(reply(3) == 0, "import succeeded"); byte[] device = new byte[312]; in.readFully(device);
        }
        void header(int command, int seq, int direction, int endpoint) throws IOException {
            out.writeInt(command); out.writeInt(seq); out.writeInt(FakeUsb.DEVICE_ID);
            out.writeInt(direction); out.writeInt(endpoint);
        }
        void submit(int seq, int dir, int ep, int length, byte[] setup, byte[] data) throws IOException {
            header(1, seq, dir, ep); out.writeInt(0); out.writeInt(length); out.writeInt(0);
            out.writeInt(0); out.writeInt(8); out.write(setup); out.write(data); out.flush();
        }
        void interrupt(int sequence) throws IOException { submit(sequence, 1, 1, 4, new byte[8], new byte[0]); }
        void unlink(int sequence, int target) throws IOException {
            header(2, sequence, 0, 0); out.writeInt(target); out.write(new byte[24]); out.flush();
        }
        byte[] completion(int command, int seq, int status, int size) throws IOException {
            require(in.readInt() == command, "completion command"); require(in.readInt() == seq, "completion sequence");
            require(in.readInt() == 0 && in.readInt() == 0 && in.readInt() == 0, "response reserved fields");
            require(in.readInt() == status, "completion status");
            if (command == 4) { in.readFully(new byte[24]); return new byte[0]; }
            require(in.readInt() == size, "actual length"); in.readFully(new byte[20]);
            byte[] data = new byte[size]; in.readFully(data); return data;
        }
        @Override public void close() throws IOException { socket.close(); }
    }
    static void await(Check check) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(3);
        while (true) {
            try { check.run(); return; }
            catch (AssertionError e) { if (System.nanoTime() >= deadline) throw e; Thread.sleep(10); }
        }
    }
    public static void main(String[] args) throws Exception {
        if (args.length != 0 && args[0].equals("--serve")) {
            FakeUsb usb = new FakeUsb();
            UsbIpServer server = new UsbIpServer(usb, new AccessPolicy("127.0.0.1"), s -> {}, System.out::println);
            server.start(args.length > 1 ? Integer.parseInt(args[1]) : 3248);
            Runtime.getRuntime().addShutdownHook(new Thread(() -> { server.close(); usb.close(); }));
            System.out.println("Synthetic USB/IP gamepad listening on " + server.port());
            new CountDownLatch(1).await(); return;
        }
        test("private LAN, VPN, CGNAT and IPv6 defaults", () -> {
            AccessPolicy policy = new AccessPolicy("");
            for (String ip : new String[] {"127.0.0.1", "192.168.71.105", "10.8.0.4", "172.16.0.1", "100.64.0.1", "fd42::4"})
                require(policy.allows(InetAddress.getByName(ip)), "private peer " + ip);
            for (String ip : new String[] {"8.8.8.8", "172.32.0.1", "100.63.255.255", "100.128.0.1", "2001:4860::1"})
                require(!policy.allows(InetAddress.getByName(ip)), "public peer " + ip);
        });
        test("literal IP/CIDR allowlist", () -> {
            AccessPolicy policy = new AccessPolicy("192.168.71.105, 10.8.0.0/24 fd42::/64");
            require(policy.allows(InetAddress.getByName("192.168.71.105")), "exact IP");
            require(!policy.allows(InetAddress.getByName("192.168.71.106")), "other LAN peer rejected");
            require(policy.allows(InetAddress.getByName("10.8.0.254")), "VPN subnet");
            require(!policy.allows(InetAddress.getByName("10.8.1.1")), "other VPN subnet rejected");
            require(policy.allows(InetAddress.getByName("fd42::abcd")), "IPv6 subnet");
        });
        test("invalid client rules rejected", () -> {
            for (String rule : new String[] {"example.com", "10.0.0.0/33", "fd42::/129", "garbage", "face", "127.1", "256.0.0.1", "1.2.3.4/", "1.2.3.4/24/3"}) {
                boolean rejected = false; try { new AccessPolicy(rule); } catch (IllegalArgumentException expected) { rejected = true; }
                require(rejected, rule);
            }
        });
        test("device record and interfaces have exact wire lengths", () -> {
            ByteArrayOutputStream bytes = new ByteArrayOutputStream();
            FakeUsb.DESCRIPTION.write(new DataOutputStream(bytes), false); require(bytes.size() == 312, "import record length");
            bytes.reset(); FakeUsb.DESCRIPTION.write(new DataOutputStream(bytes), true); require(bytes.size() == 316, "list record length");
        });
        test("non-ISO sentinel -1 accepted", () -> {
            ByteArrayOutputStream bytes = new ByteArrayOutputStream(); DataOutputStream out = new DataOutputStream(bytes);
            out.writeInt(0); out.writeInt(4); out.writeInt(0); out.writeInt(-1); out.writeInt(8); out.write(new byte[8]);
            require(UsbIp.Transfer.read(new DataInputStream(new ByteArrayInputStream(bytes.toByteArray())), 1, 1, 1, 1).length == 4, "non-ISO sentinel");
        });
        test("USB/IP list interoperable metadata", () -> {
            try (Host host = new Host(); Peer p = host.peer()) {
                p.handshake(0x8005); p.out.flush(); require(p.reply(5) == 0 && p.in.readInt() == 1, "one exported device");
                p.in.readFully(new byte[256]); require(UsbIp.readBusId(p.in).equals(FakeUsb.BUS_ID), "bus ID");
                require(p.in.readInt() == 1 && p.in.readInt() == 50 && p.in.readInt() == 2, "bus/number/speed");
                require(p.in.readUnsignedShort() == 0xcafe && p.in.readUnsignedShort() == 0x4001, "vendor/product");
                p.in.readFully(new byte[8]); require(p.in.readUnsignedByte() == 3, "HID interface");
            }
        });
        test("fragmented handshakes tolerated", () -> {
            try (Host host = new Host(); Peer p = host.peer()) {
                for (byte b : FakeUsb.hex("0111800500000000")) { p.out.writeByte(b); p.out.flush(); }
                require(p.reply(5) == 0 && p.in.readInt() == 1, "fragmented list");
            }
        });
        test("one exclusive client per device", () -> {
            try (Host host = new Host(); Peer one = host.peer(); Peer two = host.peer()) {
                one.acquire(); two.handshake(0x8003); UsbIp.writeString(two.out, FakeUsb.BUS_ID, 32); two.out.flush();
                require(two.reply(3) != 0, "second client rejected"); require(host.usb.leases.get() == 1, "single lease");
            }
        });
        test("unknown device import rejected", () -> {
            try (Host host = new Host(); Peer p = host.peer()) {
                p.handshake(0x8003); UsbIp.writeString(p.out, "1-999", 32); p.out.flush(); require(p.reply(3) != 0, "unknown device");
            }
        });
        test("control GET_DESCRIPTOR forwards data", () -> {
            try (Host host = new Host(); Peer p = host.peer()) {
                p.acquire(); p.submit(1, 1, 0, 18, FakeUsb.hex("8006000100001200"), new byte[0]);
                require(Arrays.equals(p.completion(3, 1, 0, 18), FakeUsb.DEVICE), "descriptor bytes");
            }
        });
        test("interrupt input forwards reports", () -> {
            try (Host host = new Host(); Peer p = host.peer()) {
                p.acquire(); p.interrupt(1); require(p.completion(3, 1, 0, 4)[2] == (byte)128, "axis data");
            }
        });
        test("unlink suppresses a late completed report", () -> {
            try (Host host = new Host(); Peer p = host.peer()) {
                host.usb.holdInterrupts = true; p.acquire(); p.interrupt(10);
                await(() -> require(host.usb.current.waiting.containsKey(10), "request queued"));
                UsbIp.Completion late = host.usb.current.waiting.get(10);
                p.unlink(11, 10); p.completion(4, 11, -104, 0);
                late.complete(0, 4, new byte[4]);
                p.unlink(12, 10); p.completion(4, 12, 0, 0);
            }
        });
        test("completed unlink race preserves framing", () -> {
            try (Host host = new Host(); Peer p = host.peer()) {
                p.acquire(); p.interrupt(1); p.completion(3, 1, 0, 4); p.unlink(2, 1); p.completion(4, 2, 0, 0);
            }
        });
        test("outbound control data has no reply payload", () -> {
            try (Host host = new Host(); Peer p = host.peer()) {
                p.acquire(); p.submit(1, 0, 0, 2, FakeUsb.hex("2109000200000200"), new byte[] {1, 2});
                require(p.in.readInt() == 3 && p.in.readInt() == 1, "OUT completion"); p.in.readFully(new byte[16]);
                require(p.in.readInt() == 2, "OUT actual length"); p.in.readFully(new byte[20]);
                p.interrupt(2); p.completion(3, 2, 0, 4);
            }
        });
        test("peer disconnect releases USB lease and allows reconnect", () -> {
            try (Host host = new Host()) {
                Peer p = host.peer(); p.acquire(); p.close(); await(() -> require(host.usb.current == null, "lease released"));
                try (Peer next = host.peer()) { next.acquire(); }
                require(host.usb.leases.get() == 2, "re-import succeeded");
            }
        });
        test("USB detach closes importing connection", () -> {
            try (Host host = new Host(); Peer p = host.peer()) {
                p.acquire(); host.usb.close(); require(p.in.read() == -1, "connection ended on detach");
            }
        });
        test("oversized transfer rejected before payload allocation", () -> {
            try (Host host = new Host(); Peer p = host.peer()) {
                p.acquire(); p.header(1, 1, 1, 1); p.out.writeInt(0); p.out.writeInt(UsbIp.MAX_TRANSFER + 1);
                p.out.write(new byte[20]); p.out.flush(); require(p.in.read() == -1, "oversized transfer closed");
            }
        });
        test("unbounded pending request flood rejected", () -> {
            try (Host host = new Host(); Peer p = host.peer()) {
                host.usb.holdInterrupts = true; p.acquire();
                for (int seq = 1; seq <= UsbIp.MAX_PENDING + 1; seq++) p.interrupt(seq);
                require(p.in.read() == -1, "pending request limit");
            }
        });
        test("duplicate in-flight sequence rejected", () -> {
            try (Host host = new Host(); Peer p = host.peer()) {
                host.usb.holdInterrupts = true; p.acquire(); p.interrupt(1); p.interrupt(1);
                require(p.in.read() == -1, "duplicate sequence closed");
            }
        });
        test("client address policy enforced on TCP", () -> {
            FakeUsb usb = new FakeUsb();
            try (UsbIpServer server = new UsbIpServer(usb, new AccessPolicy("192.168.71.105"), s -> {}, s -> {})) {
                server.start(0); try (Peer p = new Peer(server.port())) { require(p.in.read() == -1, "loopback not allowed by explicit rule"); }
            }
        });
        test("USB transport failure ends lease for automatic re-import", () -> {
            try (Host host = new Host(); Peer p = host.peer()) {
                p.acquire(); host.usb.failSubmit = true; p.interrupt(1); p.completion(3, 1, UsbIp.EIO, 0);
                require(p.in.read() == -1, "transport failure closes session");
                await(() -> require(host.usb.current == null, "failed USB lease released"));
                host.usb.failSubmit = false;
                try (Peer next = host.peer()) { next.acquire(); next.interrupt(1); next.completion(3, 1, 0, 4); }
            }
        });
        test("invalid backend lengths cannot corrupt the wire", () -> {
            try (Host host = new Host(); Peer p = host.peer()) {
                host.usb.holdInterrupts = true; p.acquire(); p.interrupt(1);
                await(() -> require(host.usb.current.waiting.containsKey(1), "request queued"));
                host.usb.current.waiting.remove(1).complete(0, 8, new byte[2]);
                p.completion(3, 1, UsbIp.EIO, 0);
                p.unlink(2, 1); p.completion(4, 2, 0, 0);
            }
        });
        test("control setup direction mismatch rejected", () -> {
            try (Host host = new Host(); Peer p = host.peer()) {
                p.acquire(); p.submit(1, 1, 0, 18, FakeUsb.hex("0006000100001200"), new byte[0]);
                require(p.in.read() == -1, "mismatched control direction");
            }
        });
        test("foreign device IDs rejected", () -> {
            try (Host host = new Host(); Peer p = host.peer()) {
                p.acquire(); p.out.writeInt(1); p.out.writeInt(1); p.out.writeInt(0x20001);
                p.out.writeInt(1); p.out.writeInt(1); p.out.flush();
                require(p.in.read() == -1, "wrong device");
            }
        });
        test("late callback cannot satisfy a reused sequence", () -> {
            try (Host host = new Host(); Peer p = host.peer()) {
                host.usb.holdInterrupts = true; p.acquire(); p.interrupt(10);
                await(() -> require(host.usb.current.waiting.containsKey(10), "original request queued"));
                UsbIp.Completion late = host.usb.current.waiting.get(10);
                p.unlink(11, 10); p.completion(4, 11, -104, 0); p.interrupt(10);
                await(() -> require(host.usb.current.waiting.containsKey(10), "new request queued"));
                late.complete(0, 4, new byte[] {1, 1, 1, 1});
                host.usb.current.waiting.remove(10).complete(0, 4, new byte[] {2, 2, 2, 2});
                require(p.completion(3, 10, 0, 4)[0] == 2, "new request owns sequence");
            }
        });
        System.out.println(tests + " protocol tests passed");
    }
}
