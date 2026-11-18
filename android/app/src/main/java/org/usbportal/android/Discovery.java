package org.usbportal.android;

import org.json.JSONObject;
import org.usbportal.core.AccessPolicy;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.function.*;

/** Compatible with USBPortal Linux discovery, including unicast over routed VPNs. */
final class Discovery implements AutoCloseable {
    private final DatagramSocket socket;
    private final String hostname, identity;
    private final IntSupplier count;
    private final AccessPolicy access;
    private final Consumer<String> log;
    private volatile boolean closed;
    Discovery(String hostname, String identity, IntSupplier count, AccessPolicy access,
              Consumer<String> log) throws SocketException {
        this.hostname = hostname; this.identity = identity; this.count = count; this.access = access; this.log = log;
        socket = new DatagramSocket(null); socket.setReuseAddress(true); socket.setBroadcast(true);
        try { socket.bind(new InetSocketAddress("0.0.0.0", 3241)); socket.setSoTimeout(1000); }
        catch (SocketException e) { socket.close(); throw e; }
        Thread t = new Thread(this::run, "usbportal-discovery"); t.setDaemon(true); t.start();
    }
    private byte[] announcement() throws Exception {
        return new JSONObject().put("app", "usb-portal").put("type", "announce").put("version", 1)
                .put("hostname", hostname).put("machine_id", identity).put("port", 3240)
                .put("devices", count.getAsInt()).toString().getBytes(StandardCharsets.UTF_8);
    }
    private void run() {
        long next = 0;
        while (!closed) {
            try {
                long now = System.nanoTime();
                if (now >= next) {
                    next = now + 5_000_000_000L;
                    try { broadcast(announcement()); }
                    catch (Exception e) { if (!closed) log.accept("Discovery broadcast: " + e.getMessage()); }
                }
                byte[] buffer = new byte[2049]; DatagramPacket packet = new DatagramPacket(buffer, buffer.length);
                try { socket.receive(packet); } catch (SocketTimeoutException expected) { continue; }
                if (packet.getLength() > 2048 || !access.allows(packet.getAddress())) continue;
                try {
                    JSONObject query = new JSONObject(new String(packet.getData(), 0, packet.getLength(), StandardCharsets.UTF_8));
                    if (!"usb-portal".equals(query.opt("app")) || !"query".equals(query.opt("type"))) continue;
                    byte[] response = announcement();
                    socket.send(new DatagramPacket(response, response.length, packet.getAddress(), packet.getPort()));
                } catch (Exception malformed) { /* Untrusted discovery packets must not kill the listener. */ }
            } catch (Exception e) {
                if (!closed) {
                    log.accept("Discovery: " + e.getMessage());
                    try { Thread.sleep(250); }
                    catch (InterruptedException interrupted) { Thread.currentThread().interrupt(); return; }
                }
            }
        }
    }
    private void broadcast(byte[] data) throws SocketException {
        Set<InetAddress> targets = new HashSet<>();
        Enumeration<NetworkInterface> interfaces = NetworkInterface.getNetworkInterfaces();
        if (interfaces != null) while (interfaces.hasMoreElements()) {
            NetworkInterface iface = interfaces.nextElement();
            if (!iface.isUp() || iface.isLoopback()) continue;
            for (InterfaceAddress address : iface.getInterfaceAddresses())
                if (address.getBroadcast() != null) targets.add(address.getBroadcast());
        }
        for (InetAddress target : targets) {
            try { socket.send(new DatagramPacket(data, data.length, target, 3241)); }
            catch (Exception ignored) {}
        }
    }
    @Override public void close() { closed = true; socket.close(); }
}
