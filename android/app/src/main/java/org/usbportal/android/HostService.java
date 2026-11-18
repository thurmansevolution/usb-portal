package org.usbportal.android;

import android.app.*;
import android.content.*;
import android.hardware.usb.*;
import android.net.wifi.WifiManager;
import android.os.*;
import android.system.*;
import android.util.Log;
import org.usbportal.core.*;
import java.net.*;
import java.util.*;

public final class HostService extends Service {
    static final String START = "org.usbportal.START", STOP = "org.usbportal.STOP";
    private static final String CHANNEL = "sharing";
    private final IBinder binder = new LocalBinder();
    final class LocalBinder extends Binder { HostService service() { return HostService.this; } }
    private HandlerThread thread;
    private Handler worker;
    private ShareStore store;
    AndroidRegistry registry;
    private UsbIpServer server;
    private Discovery discovery;
    private PowerManager.WakeLock wake;
    private WifiManager.WifiLock wifi;
    private WifiManager.MulticastLock multicast;
    private volatile boolean running;
    private volatile boolean wanted;
    private volatile String status = "Stopped";
    private final ArrayDeque<String> messages = new ArrayDeque<>();
    private boolean policyWarning;
    private final Runnable poll = new Runnable() {
        @Override public void run() {
            try {
                if (registry != null) registry.refresh();
                if (wanted && !running) startServing();
                if (running) {
                    status = "Shared devices: " + registry.count();
                    int pending = registry.waitingPermission();
                    if (pending > 0) status += " · " + pending + " need USB permission";
                    notifyStatus();
                }
            } catch (RuntimeException e) { status = e.getMessage(); log(status); }
            worker.postDelayed(this, 3000);
        }
    };
    @Override public void onCreate() {
        super.onCreate();
        store = new ShareStore(this);
        thread = new HandlerThread("usbportal-host"); thread.start(); worker = new Handler(thread.getLooper());
        NotificationManager notifications = getSystemService(NotificationManager.class);
        notifications.createNotificationChannel(new NotificationChannel(CHANNEL, "USB sharing", NotificationManager.IMPORTANCE_LOW));
        try { registry = new AndroidRegistry(getSystemService(UsbManager.class), store, this::log); }
        catch (RuntimeException e) { status = e.getMessage(); log(status); }
        worker.post(poll);
    }
    @Override public IBinder onBind(Intent intent) { return binder; }
    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? START : intent.getAction();
        if (STOP.equals(action)) {
            wanted = false;
            if (!store.prefs.edit().putBoolean(ShareStore.RUNNING, false).commit()) log("Could not save stopped state");
            worker.post(() -> {
                if (wanted) return;
                stopServing();
                new Handler(Looper.getMainLooper()).post(() -> {
                    if (!wanted) { stopForeground(STOP_FOREGROUND_REMOVE); stopSelfResult(startId); }
                });
            });
            return START_NOT_STICKY;
        }
        if (intent == null && !store.prefs.getBoolean(ShareStore.RUNNING, false)) {
            stopSelf(); return START_NOT_STICKY;
        }
        if (!store.prefs.edit().putBoolean(ShareStore.RUNNING, true).commit()) {
            status = "Could not save running state"; stopSelf(); return START_NOT_STICKY;
        }
        startForeground(1, notification("Starting USB sharing…"));
        wanted = true;
        worker.post(this::startServing);
        return START_STICKY;
    }
    void remember(UsbDevice device) {
        worker.post(() -> {
            try { registry.refresh(); registry.remember(device); registry.refresh(); }
            catch (RuntimeException e) { log(e.getMessage()); }
        });
    }
    void forget(int id) {
        worker.post(() -> {
            try { registry.forget(id); registry.refresh(); }
            catch (RuntimeException e) { log(e.getMessage()); }
        });
    }
    void applyFilter() {
        worker.post(() -> { if (running) { stopServing(); startServing(); } });
    }
    boolean isRunning() { return running; }
    String status() { return status; }
    synchronized String recentLog() { return String.join("\n", messages); }
    private synchronized void log(String message) {
        if (message == null) message = "Unknown USB error";
        Log.i("USBPortal", message);
        messages.addLast(message); while (messages.size() > 12) messages.removeFirst();
    }
    private void startServing() {
        if (running || !wanted) return;
        if (registry == null) { notifyStatus(); return; }
        try {
            AccessPolicy access = new AccessPolicy(store.prefs.getString(ShareStore.CLIENTS, ""));
            registry.refresh();
            server = new UsbIpServer(registry, access, this::configureSocket, this::log);
            server.start(3240);
            running = true;
            String id = store.prefs.getString("identity", "");
            if (id.isEmpty()) {
                id = "usbportal-android-" + UUID.randomUUID();
                if (!store.prefs.edit().putString("identity", id).commit()) throw new IllegalStateException("Cannot save host identity");
            }
            try { discovery = new Discovery("USBPortal " + Build.MODEL, id, registry::count, access, this::log); }
            catch (Exception e) { log("Discovery unavailable; connect by IP: " + e.getMessage()); }
            acquirePower(); status = "Sharing on TCP 3240"; notifyStatus();
            log("USB sharing started");
        } catch (Exception e) {
            stopServing(); status = "Cannot start: " + e.getMessage(); log(status); notifyStatus();
        }
    }
    private void stopServing() {
        running = false;
        if (discovery != null) { discovery.close(); discovery = null; }
        if (server != null) { server.close(); server = null; }
        if (wake != null && wake.isHeld()) wake.release();
        if (wifi != null && wifi.isHeld()) wifi.release();
        if (multicast != null && multicast.isHeld()) multicast.release();
        wake = null; wifi = null; multicast = null; status = "Stopped";
    }
    private void acquirePower() {
        wake = getSystemService(PowerManager.class).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "USBPortal:sharing");
        wake.setReferenceCounted(false); wake.acquire();
        WifiManager manager = (WifiManager) getApplicationContext().getSystemService(WIFI_SERVICE);
        if (manager != null) {
            try {
            wifi = manager.createWifiLock(Build.VERSION.SDK_INT >= 29 ? WifiManager.WIFI_MODE_FULL_LOW_LATENCY :
                    WifiManager.WIFI_MODE_FULL_HIGH_PERF, "USBPortal:sharing");
            wifi.setReferenceCounted(false); wifi.acquire();
            multicast = manager.createMulticastLock("USBPortal:discovery");
            multicast.setReferenceCounted(false); multicast.acquire();
            } catch (RuntimeException e) { log("Wi-Fi power/discovery locks unavailable: " + e.getMessage()); }
        }
    }
    private void configureSocket(Socket socket) {
        try (ParcelFileDescriptor fd = ParcelFileDescriptor.fromSocket(socket)) {
            // Linux TCP_KEEPIDLE, TCP_KEEPINTVL, TCP_KEEPCNT, TCP_USER_TIMEOUT. Public Os API since API 21.
            for (int[] option : new int[][] {{4, 5}, {5, 3}, {6, 3}, {18, 15000}})
                Os.setsockoptInt(fd.getFileDescriptor(), OsConstants.IPPROTO_TCP, option[0], option[1]);
        } catch (Exception e) {
            if (!policyWarning) { policyWarning = true; log("Kernel TCP recovery options unavailable: " + e.getMessage()); }
        }
    }
    private Notification notification(String text) {
        PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);
        PendingIntent stop = PendingIntent.getService(this, 1, new Intent(this, HostService.class).setAction(STOP), PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, CHANNEL).setSmallIcon(R.drawable.ic_usb).setContentTitle("USBPortal sharing")
                .setContentText(text).setContentIntent(open).setOngoing(true)
                .addAction(new Notification.Action.Builder(null, "Stop sharing", stop).build()).build();
    }
    private void notifyStatus() { getSystemService(NotificationManager.class).notify(1, notification(status)); }
    @Override public void onDestroy() {
        wanted = false;
        worker.removeCallbacks(poll);
        // Do not block Android's main thread while terminating USB requests and network workers.
        worker.post(() -> { stopServing(); if (registry != null) registry.close(); thread.quitSafely(); });
        super.onDestroy();
    }
}
