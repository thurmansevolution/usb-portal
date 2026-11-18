package org.usbportal.android;

import android.hardware.usb.*;
import org.usbportal.core.UsbIp;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;

/** Public Android USB Host API transport; no native library, root, or hidden APIs. */
final class AndroidBackend implements UsbIp.Backend {
    private final UsbIp.Device description;
    private final UsbDevice device;
    private final UsbDeviceConnection connection;
    private final Runnable released;
    private final Map<Integer, UsbInterface> claimed = new HashMap<>();
    private final Map<Integer, UsbEndpoint> endpoints = new HashMap<>();
    private final Map<Integer, Request> requests = new HashMap<>();
    private final AtomicBoolean closed = new AtomicBoolean();
    private volatile Runnable disconnected = () -> {};
    private final Map<Integer, FutureTask<Void>> controlRequests = new HashMap<>();
    private final ThreadPoolExecutor controls = new ThreadPoolExecutor(1, 1, 0, TimeUnit.SECONDS,
            new ArrayBlockingQueue<>(UsbIp.MAX_PENDING), r -> {
        Thread t = new Thread(r, "usb-control"); t.setDaemon(true); return t;
    });
    private Thread reaper;

    private static final class Request {
        final UsbRequest nativeRequest;
        final ByteBuffer buffer;
        final UsbIp.Transfer transfer;
        final UsbIp.Completion completion;
        boolean cancelled;
        Request(UsbRequest request, ByteBuffer buffer, UsbIp.Transfer t, UsbIp.Completion c) {
            this.nativeRequest = request; this.buffer = buffer; transfer = t; completion = c;
        }
    }
    AndroidBackend(UsbManager manager, UsbDevice device, UsbIp.Device description,
                   Runnable released) throws IOException {
        this.device = device; this.description = description; this.released = released;
        connection = manager.openDevice(device);
        if (connection == null) throw new IOException("USB permission/device unavailable");
        try {
            UsbConfiguration config = device.getConfiguration(0);
            for (int i = 0; i < config.getInterfaceCount(); i++) {
                UsbInterface iface = config.getInterface(i);
                if (iface.getAlternateSetting() != 0) continue;
                if (!connection.claimInterface(iface, true))
                    throw new IOException("Android refused interface " + iface.getId());
                claimed.put(iface.getId(), iface);
            }
            rebuildEndpoints();
            reaper = new Thread(this::reap, "usb-completions"); reaper.setDaemon(true); reaper.start();
        } catch (IOException | RuntimeException e) {
            for (UsbInterface iface : claimed.values()) connection.releaseInterface(iface);
            connection.close(); controls.shutdownNow(); throw e;
        }
    }
    @Override public UsbIp.Device device() { return description; }
    @Override public void onDisconnect(Runnable listener) {
        disconnected = listener;
        if (closed.get()) listener.run();
    }
    private void rebuildEndpoints() {
        endpoints.clear();
        for (UsbInterface iface : claimed.values())
            for (int e = 0; e < iface.getEndpointCount(); e++) {
                UsbEndpoint ep = iface.getEndpoint(e); endpoints.put(ep.getAddress(), ep);
            }
    }
    @Override public void submit(UsbIp.Transfer transfer, UsbIp.Completion completion) throws IOException {
        if (closed.get()) throw new IOException("USB device closed");
        if (transfer.endpoint == 0) {
            synchronized (controlRequests) {
                if (controlRequests.containsKey(transfer.sequence)) throw new IOException("Duplicate control transfer");
                FutureTask<Void> task = new FutureTask<Void>(() -> { control(transfer, completion); return null; }) {
                    @Override protected void done() {
                        synchronized (controlRequests) { controlRequests.remove(transfer.sequence, this); }
                    }
                };
                controlRequests.put(transfer.sequence, task);
                try { controls.execute(task); }
                catch (RejectedExecutionException e) { controlRequests.remove(transfer.sequence); throw new IOException("USB control queue full"); }
            }
            return;
        }
        synchronized (requests) {
            if (closed.get() || requests.size() >= UsbIp.MAX_PENDING || requests.containsKey(transfer.sequence))
                throw new IOException("USB transfer queue full");
            UsbEndpoint endpoint = endpoints.get(transfer.address());
            if (endpoint == null || (endpoint.getType() != UsbConstants.USB_ENDPOINT_XFER_BULK &&
                    endpoint.getType() != UsbConstants.USB_ENDPOINT_XFER_INT)) {
                completion.complete(UsbIp.EINVAL, 0, new byte[0]); return;
            }
            // Public UsbRequest cannot express URB_ZERO_PACKET on bulk OUT.
            if (transfer.direction == 0 && (transfer.flags & 0x40) != 0) {
                completion.complete(-95, 0, new byte[0]); return;
            }
            ByteBuffer buffer = ByteBuffer.allocateDirect(transfer.length);
            if (transfer.direction == 0) { buffer.put(transfer.data); buffer.flip(); }
            UsbRequest nativeRequest = new UsbRequest();
            if (!nativeRequest.initialize(connection, endpoint)) {
                nativeRequest.close(); throw new IOException("Android could not initialize USB request");
            }
            Request request = new Request(nativeRequest, buffer, transfer, completion);
            nativeRequest.setClientData(request);
            requests.put(transfer.sequence, request);
            try {
                if (!nativeRequest.queue(buffer)) throw new IOException("Android rejected USB request");
            } catch (IOException | RuntimeException e) {
                requests.remove(transfer.sequence); nativeRequest.close();
                throw new IOException("Android could not queue USB request", e);
            }
        }
    }
    private void control(UsbIp.Transfer transfer, UsbIp.Completion completion) {
        if (closed.get()) return;
        int result;
        byte[] buffer = transfer.direction == 1 ? new byte[transfer.length] : transfer.data;
        try {
            int type = transfer.requestType(), request = transfer.request();
            if (type == 0 && request == 5) {
                // SET_ADDRESS belongs to the virtual bus; Android already assigned the physical address.
                result = 0;
            } else if (type == 0 && request == 9) {
                // Claiming the existing configuration is sufficient. Never reset a live physical device here.
                result = transfer.value() == description.configuration || transfer.value() == 0 ? 0 : -95;
            } else if (type == 1 && request == 11) {
                result = setInterface(transfer.index(), transfer.value()) ? 0 : UsbIp.EPIPE;
            } else if (type == 0x23 && request == 3 && transfer.value() == 4) {
                result = -95; // Android's public API cannot perform a physical port reset.
            } else {
                result = connection.controlTransfer(type, request, transfer.value(), transfer.index(),
                        buffer, transfer.length, 2500);
                if (result < 0) result = UsbIp.EPIPE;
            }
            completion.complete(result < 0 ? result : 0, Math.max(0, result), buffer);
        } catch (RuntimeException e) { completion.complete(UsbIp.EIO, 0, new byte[0]); }
    }
    private boolean setInterface(int number, int alternate) {
        synchronized (requests) {
            UsbConfiguration config = device.getConfiguration(0);
            for (int i = 0; i < config.getInterfaceCount(); i++) {
                UsbInterface iface = config.getInterface(i);
                if (iface.getId() == number && iface.getAlternateSetting() == alternate) {
                    if (!connection.setInterface(iface)) return false;
                    claimed.put(number, iface); rebuildEndpoints(); return true;
                }
            }
            return false;
        }
    }
    private void reap() {
        try {
            while (true) {
                synchronized (requests) { if (closed.get() && requests.isEmpty()) break; }
                UsbRequest nativeRequest;
                try { nativeRequest = connection.requestWait(1000); }
                catch (TimeoutException expected) { continue; }
                if (nativeRequest == null) throw new IOException("USB connection ended");
                Request request = (Request) nativeRequest.getClientData();
                boolean cancelled;
                synchronized (requests) {
                    requests.remove(request.transfer.sequence, request);
                    cancelled = request.cancelled;
                }
                if (!cancelled && !closed.get()) {
                    int actual = request.buffer.position();
                    byte[] data = request.transfer.direction == 1 ? new byte[actual] : new byte[0];
                    if (data.length != 0) { request.buffer.flip(); request.buffer.get(data); }
                    int status = request.transfer.direction == 1 && actual < request.transfer.length &&
                            (request.transfer.flags & 1) != 0 ? -121 : 0;
                    request.completion.complete(status, actual, data);
                }
                nativeRequest.close();
            }
        } catch (IOException | RuntimeException e) {
            if (!closed.get()) { disconnected.run(); close(); }
        }
    }
    @Override public void cancel(int sequence) {
        synchronized (controlRequests) {
            FutureTask<Void> task = controlRequests.get(sequence);
            if (task != null) { task.cancel(false); controls.remove(task); }
        }
        synchronized (requests) {
            Request request = requests.get(sequence);
            if (request != null) { request.cancelled = true; request.nativeRequest.cancel(); }
        }
        // A synchronous Android controlTransfer cannot be cancelled. Its late reply is discarded by Session.
    }
    @Override public void close() {
        if (!closed.compareAndSet(false, true)) return;
        disconnected.run();
        controls.shutdownNow();
        synchronized (requests) {
            for (Request request : requests.values()) {
                request.cancelled = true; request.nativeRequest.cancel();
            }
        }
        // Joining before closing avoids racing requestWait with Android's native connection teardown.
        if (reaper != null && reaper != Thread.currentThread()) {
            try { reaper.join(1500); } catch (InterruptedException e) { Thread.currentThread().interrupt(); }
        }
        try { controls.awaitTermination(3, TimeUnit.SECONDS); }
        catch (InterruptedException e) { Thread.currentThread().interrupt(); }
        synchronized (requests) {
            for (UsbInterface iface : claimed.values()) connection.releaseInterface(iface);
            connection.close();
            for (Request request : requests.values()) request.nativeRequest.close();
            requests.clear();
        }
        released.run();
    }
}
