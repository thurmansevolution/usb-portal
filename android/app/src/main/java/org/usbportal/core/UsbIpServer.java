package org.usbportal.core;

import java.io.*;
import java.net.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.Consumer;

/** Bounded USB/IP host. Each import owns one backend and one TCP connection. */
public final class UsbIpServer implements AutoCloseable {
    public interface SocketPolicy { void configure(Socket socket) throws IOException; }
    private final UsbIp.Registry registry;
    private final AccessPolicy access;
    private final SocketPolicy policy;
    private final Consumer<String> log;
    private final Set<Socket> sockets = ConcurrentHashMap.newKeySet();
    private final ThreadPoolExecutor workers = new ThreadPoolExecutor(0, 12, 30,
            TimeUnit.SECONDS, new SynchronousQueue<>(), runnable -> {
                Thread t = new Thread(runnable, "usbip-client"); t.setDaemon(true); return t;
            });
    private final AtomicBoolean closed = new AtomicBoolean();
    private ServerSocket listener;
    private Thread acceptor;

    public UsbIpServer(UsbIp.Registry registry, AccessPolicy access,
                       SocketPolicy policy, Consumer<String> log) {
        this.registry = registry; this.access = access; this.policy = policy; this.log = log;
    }
    public synchronized void start(int port) throws IOException {
        if (closed.get() || listener != null) throw new IOException("Server already started/closed");
        listener = new ServerSocket();
        try {
            listener.setReuseAddress(true);
            listener.bind(new InetSocketAddress(port), 12);
        } catch (IOException e) { listener.close(); listener = null; throw e; }
        acceptor = new Thread(this::accept, "usbip-listener"); acceptor.setDaemon(true); acceptor.start();
    }
    public synchronized int port() { return listener == null ? 0 : listener.getLocalPort(); }
    private void accept() {
        while (!closed.get()) {
            Socket socket = null;
            try {
                socket = listener.accept();
                if (!access.allows(socket.getInetAddress())) { socket.close(); continue; }
                socket.setTcpNoDelay(true); socket.setKeepAlive(true); socket.setSoTimeout(5000);
                policy.configure(socket);
                sockets.add(socket);
                Socket accepted = socket;
                workers.execute(() -> serve(accepted));
            } catch (RejectedExecutionException e) {
                discard(socket);
            } catch (IOException e) {
                discard(socket);
                if (!closed.get()) log.accept("Network listener: " + e.getMessage());
            }
        }
    }
    private void discard(Socket socket) {
        if (socket == null) return;
        sockets.remove(socket);
        try { socket.close(); } catch (IOException ignored) {}
    }
    private void serve(Socket socket) {
        try (Socket ignored = socket) {
            DataInputStream in = new DataInputStream(new BufferedInputStream(socket.getInputStream()));
            // Flush once per reply/report: avoid a TCP_NODELAY packet for every four-byte header field.
            DataOutputStream out = new DataOutputStream(new BufferedOutputStream(socket.getOutputStream()));
            int version = in.readUnsignedShort(), operation = in.readUnsignedShort(), status = in.readInt();
            if (version != UsbIp.VERSION || status != 0) throw new IOException("Invalid USB/IP handshake");
            if (operation == 0x8005) {
                List<UsbIp.Device> devices = registry.list();
                UsbIp.reply(out, 0x0005, 0); out.writeInt(devices.size());
                for (UsbIp.Device device : devices) device.write(out, true);
                out.flush();
            } else if (operation == 0x8003) {
                String busId = UsbIp.readBusId(in);
                UsbIp.Backend backend = registry.acquire(busId);
                if (backend == null) { UsbIp.reply(out, 3, 1); out.flush(); return; }
                try {
                    UsbIp.reply(out, 3, 0); backend.device().write(out, false); out.flush();
                    socket.setSoTimeout(0); // Keepalive/user-timeout detects dead peers without dropping idle HID.
                    log.accept("Connected " + busId + " to " + socket.getInetAddress().getHostAddress());
                    new Session(socket, in, out, backend).run();
                } finally { backend.close(); log.accept("Released " + busId); }
            } else { UsbIp.reply(out, operation & 0x7fff, 1); out.flush(); }
        } catch (EOFException | SocketException ignored) {
        } catch (IOException | RuntimeException e) {
            if (!closed.get()) log.accept("Client: " + e.getMessage());
        } finally { sockets.remove(socket); }
    }

    private static final class Session {
        final Socket socket;
        final DataInputStream in;
        final DataOutputStream out;
        final UsbIp.Backend backend;
        final Map<Integer, UsbIp.Transfer> pending = new HashMap<>();
        final Object outputLock = new Object();
        boolean ended;
        Session(Socket socket, DataInputStream in, DataOutputStream out, UsbIp.Backend backend) {
            this.socket = socket; this.in = in; this.out = out; this.backend = backend;
            backend.onDisconnect(this::end);
        }
        void run() throws IOException {
            try {
                while (!socket.isClosed()) {
                    int command = in.readInt(), seq = in.readInt(), dev = in.readInt();
                    int dir = in.readInt(), ep = in.readInt();
                    if (dev != backend.device().id()) throw new IOException("Wrong device ID");
                    if (command == 1) {
                        UsbIp.Transfer transfer = UsbIp.Transfer.read(in, seq, dev, dir, ep);
                        synchronized (outputLock) {
                            if (pending.size() >= UsbIp.MAX_PENDING || pending.containsKey(seq))
                                throw new IOException("Too many/duplicate transfers");
                            pending.put(seq, transfer);
                        }
                        try {
                            backend.submit(transfer, (status, actual, data) -> complete(transfer, status, actual, data));
                        } catch (IOException | RuntimeException e) {
                            complete(transfer, UsbIp.EIO, 0, new byte[0]);
                            end();
                        }
                    } else if (command == 2) {
                        int target = in.readInt(); byte[] padding = new byte[24]; in.readFully(padding);
                        UsbIp.Transfer removed;
                        synchronized (outputLock) { removed = pending.remove(target); }
                        if (removed != null) backend.cancel(target);
                        synchronized (outputLock) {
                            if (!ended) {
                                header(4, seq); out.writeInt(removed == null ? 0 : -104);
                                out.write(new byte[24]); out.flush();
                            }
                        }
                    } else throw new IOException("Unsupported USB/IP command");
                }
            } finally { end(); }
        }
        void header(int command, int seq) throws IOException {
            out.writeInt(command); out.writeInt(seq); out.writeInt(0); out.writeInt(0); out.writeInt(0);
        }
        void complete(UsbIp.Transfer transfer, int status, int actual, byte[] data) {
            synchronized (outputLock) {
                if (ended || pending.get(transfer.sequence) != transfer) return;
                pending.remove(transfer.sequence);
                if (actual < 0 || actual > transfer.length ||
                        (transfer.direction == 1 && actual > data.length)) {
                    status = UsbIp.EIO; actual = 0;
                }
                try {
                    header(3, transfer.sequence); out.writeInt(status); out.writeInt(actual);
                    out.writeInt(0); out.writeInt(0); out.writeInt(0); out.writeLong(0);
                    if (transfer.direction == 1) out.write(data, 0, actual);
                    out.flush();
                } catch (IOException e) { end(); }
            }
        }
        void end() {
            synchronized (outputLock) {
                if (ended) return;
                ended = true; pending.clear();
                try { socket.close(); } catch (IOException ignored) {}
            }
        }
    }
    @Override public void close() {
        if (!closed.compareAndSet(false, true)) return;
        try { if (listener != null) listener.close(); } catch (IOException ignored) {}
        for (Socket socket : sockets) discard(socket);
        workers.shutdownNow();
        try { workers.awaitTermination(4, TimeUnit.SECONDS); }
        catch (InterruptedException e) { Thread.currentThread().interrupt(); }
    }
}
