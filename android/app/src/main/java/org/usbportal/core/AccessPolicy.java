package org.usbportal.core;

import java.net.*;
import java.util.*;

/** Optional literal IP/CIDR allowlist; an empty list permits private LAN/VPN peers. */
public final class AccessPolicy {
    private final List<byte[]> networks = new ArrayList<>();
    private final List<Integer> prefixes = new ArrayList<>();
    public AccessPolicy(String rules) {
        for (String rule : rules.trim().split("[ ,;\\n]+")) {
            if (rule.isEmpty()) continue;
            String[] parts = rule.split("/", -1);
            boolean ipv4 = parts[0].matches("[0-9]{1,3}(\\.[0-9]{1,3}){3}");
            boolean ipv6 = parts[0].contains(":") && parts[0].matches("[0-9a-fA-F:.]+");
            if (parts.length > 2 || (!ipv4 && !ipv6))
                throw new IllegalArgumentException("Use literal IP addresses or CIDRs");
            try {
                byte[] address;
                if (ipv4) {
                    String[] octets = parts[0].split("\\."); address = new byte[4];
                    for (int i = 0; i < 4; i++) {
                        int value = Integer.parseInt(octets[i]);
                        if (value > 255) throw new IllegalArgumentException();
                        address[i] = (byte) value;
                    }
                } else address = InetAddress.getByName(parts[0]).getAddress();
                int prefix = parts.length == 2 ? Integer.parseInt(parts[1]) : address.length * 8;
                if (prefix < 0 || prefix > address.length * 8) throw new IllegalArgumentException();
                networks.add(address); prefixes.add(prefix);
            } catch (Exception e) { throw new IllegalArgumentException("Invalid client address: " + rule); }
        }
    }
    public boolean allows(InetAddress peer) {
        byte[] bytes = peer.getAddress();
        if (networks.isEmpty()) {
            return peer.isLoopbackAddress() || peer.isSiteLocalAddress() || peer.isLinkLocalAddress() ||
                    (bytes.length == 4 && (bytes[0] & 255) == 100 && (bytes[1] & 0xc0) == 64) ||
                    (bytes.length == 16 && (bytes[0] & 0xfe) == 0xfc);
        }
        for (int n = 0; n < networks.size(); n++) {
            byte[] net = networks.get(n); int bits = prefixes.get(n);
            if (net.length != bytes.length) continue;
            boolean matches = true;
            for (int i = 0; bits > 0; i++, bits -= 8) {
                int mask = (0xff << Math.max(0, 8 - bits)) & 0xff;
                if ((net[i] & mask) != (bytes[i] & mask)) { matches = false; break; }
            }
            if (matches) return true;
        }
        return false;
    }
}
