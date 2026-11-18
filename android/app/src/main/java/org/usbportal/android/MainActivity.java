package org.usbportal.android;

import android.Manifest;
import android.app.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.*;
import android.hardware.usb.*;
import android.os.*;
import android.text.InputType;
import android.view.*;
import android.view.inputmethod.InputMethodManager;
import android.widget.*;
import org.usbportal.core.AccessPolicy;
import org.usbportal.core.DeviceKind;
import java.net.*;
import java.util.*;

/** Native, remote-first interface. The foreground host service owns all USB/network work. */
public final class MainActivity extends Activity {
    private static final String USB_PERMISSION = "org.usbportal.USB_PERMISSION";
    private static final int INK = 0xfff0f5fa, MUTED = 0xffa9bac9, CYAN = 0xffa4e0ef;
    private static final int MINT = 0xff9bdbc5, AMBER = 0xfff0cc8b, RED = 0xfff1aaa5;
    private static final String[] TABS = {"Devices", "Settings", "Diagnostics"};
    private final Handler refresh = new Handler(Looper.getMainLooper());
    private final Map<String, DeviceCard> cards = new LinkedHashMap<>();
    private HostService service;
    private ShareStore store;
    private UsbManager usb;
    private boolean bound, receiverRegistered, wide;
    private int selectedTab;
    private String pendingDevice = "", gridKey = "";
    private Object systemBack;
    private LinearLayout root, header, deviceGrid;
    private FrameLayout pages;
    private final ScrollView[] scrolls = new ScrollView[3];
    private final Button[] tabs = new Button[3];
    private Button toggle;
    private TextView serverPill, heroTitle, heroDetail, addresses, deviceCount, actionMessage;
    private TextView readyCount, inUseCount, diagnosticStatus, diagnosticAddresses, log;
    private EditText clientFilter;
    private final Runnable render = new Runnable() {
        @Override public void run() { update(); refresh.postDelayed(this, 1000); }
    };
    private final ServiceConnection connection = new ServiceConnection() {
        @Override public void onServiceConnected(ComponentName name, IBinder binder) {
            service = ((HostService.LocalBinder) binder).service(); finishPendingPermission(); update();
        }
        @Override public void onServiceDisconnected(ComponentName name) { service = null; update(); }
    };
    private final BroadcastReceiver permission = new BroadcastReceiver() {
        @Override public void onReceive(Context context, Intent intent) {
            if (!USB_PERMISSION.equals(intent.getAction())) return;
            UsbDevice device = intent.getParcelableExtra(UsbManager.EXTRA_DEVICE);
            if (device == null || !device.getDeviceName().equals(pendingDevice)) return;
            if (intent.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED, false) && usb.hasPermission(device)) {
                // Keep explicit intent pending until the service binds, including Activity recreation.
                finishPendingPermission();
            } else {
                pendingDevice = "";
                message("USB access was not granted. Your remembered sharing choice is unchanged.", AMBER);
            }
            update();
        }
    };

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        store = new ShareStore(this); usb = getSystemService(UsbManager.class);
        wide = getResources().getConfiguration().screenWidthDp >= 720;
        selectedTab = state == null ? 0 : Math.max(0, Math.min(2, state.getInt("tab", 0)));
        pendingDevice = state == null ? "" : state.getString("pending_usb", "");
        root = column(); root.setBackground(new PortalSurface());
        int side = dp(wide ? 36 : 20), top = dp(wide ? 22 : 16);
        root.setPadding(side, top, side, 0);
        root.setOnApplyWindowInsetsListener((view, insets) -> {
            view.setPadding(side + insets.getSystemWindowInsetLeft(), top + insets.getSystemWindowInsetTop(),
                    side + insets.getSystemWindowInsetRight(), insets.getSystemWindowInsetBottom());
            return insets;
        });
        buildHeader(); buildNavigation();
        pages = new FrameLayout(this); root.addView(pages, new LinearLayout.LayoutParams(-1, 0, 1));
        buildDevices(); buildSettings(state); buildDiagnostics();
        setContentView(root); root.requestApplyInsets();
        if (Build.VERSION.SDK_INT >= 33) registerReceiver(permission, new IntentFilter(USB_PERMISSION), Context.RECEIVER_NOT_EXPORTED);
        else registerReceiver(permission, new IntentFilter(USB_PERMISSION));
        receiverRegistered = true;
        showTab(selectedTab, false);
        if (state != null) for (int i = 0; i < scrolls.length; i++) {
            final ScrollView scroll = scrolls[i]; final int y = state.getInt("scroll_" + i, 0);
            scroll.post(() -> scroll.scrollTo(0, y));
        }
        if (store.prefs.getBoolean(ShareStore.RUNNING, false)) {
            try { startForegroundService(new Intent(this, HostService.class).setAction(HostService.START)); }
            catch (RuntimeException e) { message("Could not resume sharing: " + error(e), RED); }
        }
    }
    private void buildHeader() {
        header = row(); header.setGravity(Gravity.CENTER_VERTICAL);
        ImageView mark = new ImageView(this); mark.setImageDrawable(new PortalIcon(DeviceKind.UNKNOWN, CYAN));
        header.addView(mark, new LinearLayout.LayoutParams(dp(34), dp(34)));
        LinearLayout brand = column(); brand.setPadding(dp(12), 0, 0, 0);
        brand.addView(text("USBPortal", 25, INK, true));
        brand.addView(text("Your USB devices, connected.", 12, MUTED, false));
        header.addView(brand, new LinearLayout.LayoutParams(0, -2, 1));
        serverPill = pill("HOST", MUTED);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, -2);
        if (wide) params.bottomMargin = dp(16);
        root.addView(header, params);
    }
    private void buildNavigation() {
        LinearLayout navigation = row(); navigation.setBaselineAligned(false);
        navigation.setPadding(0, wide ? 0 : dp(18), wide ? dp(18) : 0, wide ? 0 : dp(15));
        float[] labelWidths = new float[TABS.length];
        for (int i = 0; i < TABS.length; i++) {
            final int tab = i;
            Button button = button(TABS[i], () -> showTab(tab, true), false);
            button.setTextSize(wide ? 15 : 13); button.setPadding(dp(8), dp(6), dp(8), dp(6));
            button.setGravity(Gravity.CENTER); button.setTag("tab_" + i); button.setMinHeight(dp(48));
            labelWidths[i] = button.getPaint().measureText(TABS[i]) + dp(16);
            tabs[i] = button;
        }
        float available = dp(getResources().getConfiguration().screenWidthDp - 40);
        boolean wrap = !wide && Math.max(labelWidths[0], Math.max(labelWidths[1], labelWidths[2])) > (available - dp(16)) / 3f;
        boolean stack = wrap && Math.max(labelWidths[0], labelWidths[1]) > (available - dp(8)) / 2f;
        if (wrap) {
            // Preserve complete labels and 48dp targets rather than splitting a tab mid-word.
            navigation.setOrientation(LinearLayout.VERTICAL);
            LinearLayout first = row(); first.setBaselineAligned(false);
            if (!stack) navigation.addView(first, new LinearLayout.LayoutParams(-1, -2));
            for (int i = 0; i < tabs.length; i++) {
                if (stack || i == 2) {
                    LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, -2);
                    if (i > 0) params.topMargin = dp(8);
                    navigation.addView(tabs[i], params);
                } else {
                    LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(0, -2, 1);
                    if (i > 0) params.leftMargin = dp(8);
                    first.addView(tabs[i], params);
                }
            }
        } else {
            for (int i = 0; i < tabs.length; i++) {
                LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(0, -2, 1);
                if (i > 0) params.leftMargin = dp(8);
                navigation.addView(tabs[i], params);
            }
        }
        if (wide) {
            int width = getResources().getConfiguration().screenWidthDp;
            header.addView(navigation, new LinearLayout.LayoutParams(dp(width >= 900 ? 420 : 360), -2));
            if (width >= 900) header.addView(serverPill);
        } else root.addView(navigation, new LinearLayout.LayoutParams(-1, -2));
    }
    private LinearLayout page(int index) {
        ScrollView scroll = new ScrollView(this); scroll.setFillViewport(true);
        scroll.setClipToPadding(false); scroll.setPadding(dp(2), dp(2), dp(2), dp(24));
        scroll.setVerticalScrollBarEnabled(false);
        LinearLayout content = column(); scroll.addView(content, new ScrollView.LayoutParams(-1, -2));
        pages.addView(scroll, new FrameLayout.LayoutParams(-1, -1)); scrolls[index] = scroll;
        return content;
    }
    private void buildDevices() {
        LinearLayout content = page(0);
        LinearLayout hero = panel();
        LinearLayout heroTop = row(); heroTop.setGravity(Gravity.CENTER_VERTICAL);
        LinearLayout heading = column();
        heading.addView(eyebrow("ANDROID USB HOST"));
        heroTitle = text("Connecting to service…", wide ? 28 : 24, INK, true); heading.addView(heroTitle);
        heroDetail = text("Your sharing preferences stay on this device.", wide ? 13 : 14, MUTED, false);
        toggle = button("Start sharing", this::toggleSharing, true); toggle.setTag("toggle_sharing");
        addresses = text("Finding local addresses…", 13, MUTED, false);
        if (wide) {
            // Keep the first row of device actions visible on a 540dp-high TV viewport.
            hero.setPadding(dp(22), dp(16), dp(22), dp(16));
            heroTop.addView(heading, new LinearLayout.LayoutParams(0, -2, 1));
            LinearLayout stats = row(); stats.setGravity(Gravity.CENTER);
            readyCount = statistic(stats, "AVAILABLE"); inUseCount = statistic(stats, "IN USE");
            heroTop.addView(stats);
            LinearLayout.LayoutParams actionParams = new LinearLayout.LayoutParams(dp(180), -2); actionParams.leftMargin = dp(20);
            heroTop.addView(toggle, actionParams); hero.addView(heroTop);
            LinearLayout detailLine = row(); detailLine.setGravity(Gravity.CENTER_VERTICAL);
            heroDetail.setPadding(0, dp(10), dp(14), 0); addresses.setPadding(dp(10), dp(10), 0, 0);
            addresses.setGravity(Gravity.END);
            detailLine.addView(heroDetail, new LinearLayout.LayoutParams(0, -2, 1));
            detailLine.addView(addresses, new LinearLayout.LayoutParams(0, -2, 1)); hero.addView(detailLine);
        } else {
            heroDetail.setPadding(0, dp(7), 0, dp(12)); heading.addView(heroDetail);
            heroTop.addView(heading, new LinearLayout.LayoutParams(0, -2, 1)); hero.addView(heroTop);
            hero.addView(toggle, new LinearLayout.LayoutParams(-1, -2));
            addresses.setPadding(0, dp(12), 0, 0); hero.addView(addresses, new LinearLayout.LayoutParams(-1, -2));
        }
        content.addView(hero);
        actionMessage = text("", 14, AMBER, false); actionMessage.setVisibility(View.GONE);
        actionMessage.setPadding(dp(4), dp(12), dp(4), 0); content.addView(actionMessage);
        LinearLayout section = row(); section.setGravity(Gravity.CENTER_VERTICAL); section.setPadding(dp(2), dp(22), dp(2), dp(12));
        boolean narrowText = !wide && (getResources().getConfiguration().screenWidthDp < 380 ||
                getResources().getConfiguration().fontScale > 1.2f);
        if (narrowText) section.setOrientation(LinearLayout.VERTICAL);
        section.addView(text("Devices", 21, INK, true), new LinearLayout.LayoutParams(narrowText ? -1 : 0, -2, narrowText ? 0 : 1));
        deviceCount = text("", 12, MUTED, false); if (narrowText) deviceCount.setPadding(0, dp(5), 0, 0);
        section.addView(deviceCount); content.addView(section);
        deviceGrid = column(); content.addView(deviceGrid);
        TextView footer = text("Remember a device here. Enable auto-connect on your Linux client.", 12, MUTED, false);
        footer.setPadding(dp(2), dp(7), dp(2), dp(8)); content.addView(footer);
    }
    private TextView statistic(LinearLayout parent, String label) {
        LinearLayout cell = column(); cell.setGravity(Gravity.CENTER); cell.setPadding(dp(23), 0, dp(10), 0);
        TextView count = text("0", 29, INK, true); cell.addView(count); cell.addView(text(label, 10, MUTED, true));
        parent.addView(cell); return count;
    }
    private void buildSettings(Bundle state) {
        LinearLayout content = page(1);
        addPageHeading(content, "Make it automatic", "Remembered choices, with Android in control of USB access.");
        LinearLayout startup = panel(); startup.addView(eyebrow("STARTUP"));
        Switch boot = new Switch(this); boot.setText("Resume sharing after reboot"); boot.setTextColor(INK); boot.setTextSize(16);
        boot.setMinHeight(dp(56)); boot.setPadding(dp(4), dp(8), dp(4), dp(8));
        boot.setThumbTintList(new ColorStateList(new int[][] {new int[] {android.R.attr.state_checked}, new int[] {}}, new int[] {CYAN, MUTED}));
        boot.setTrackTintList(new ColorStateList(new int[][] {new int[] {android.R.attr.state_checked}, new int[] {}}, new int[] {0xff456d78, 0xff34404c}));
        boot.setBackground(controlBackground(false)); boot.setChecked(store.prefs.getBoolean(ShareStore.BOOT, true));
        boot.setOnCheckedChangeListener((view, checked) -> saveBoot(boot, checked));
        startup.addView(boot);
        startup.addView(body("Resumes only if sharing was running. Force-stopping the app or Android power restrictions can prevent restart."));
        addSpaced(content, startup);
        LinearLayout clients = panel(); clients.addView(eyebrow("CLIENT ACCESS"));
        clients.addView(text("Allowed clients", 19, INK, true));
        clients.addView(body("Leave blank for private LAN/VPN clients, or enter IP addresses and CIDRs separated by spaces or commas."));
        clientFilter = new EditText(this); clientFilter.setTextColor(INK); clientFilter.setHintTextColor(0xff8298ac);
        clientFilter.setTextSize(15); clientFilter.setSingleLine(true); clientFilter.setSelectAllOnFocus(false);
        clientFilter.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
        clientFilter.setHint("192.168.71.105, 10.8.0.0/24"); clientFilter.setPadding(dp(14), dp(10), dp(14), dp(10));
        clientFilter.setMinHeight(dp(52)); clientFilter.setBackground(controlBackground(false));
        clientFilter.setText(state == null ? store.prefs.getString(ShareStore.CLIENTS, "") :
                state.getString("filter_draft", store.prefs.getString(ShareStore.CLIENTS, "")));
        LinearLayout.LayoutParams editParams = new LinearLayout.LayoutParams(-1, -2); editParams.topMargin = dp(8);
        clients.addView(clientFilter, editParams);
        Button apply = button("Apply client filter", this::applyClientFilter, true);
        LinearLayout.LayoutParams applyParams = new LinearLayout.LayoutParams(wide ? dp(220) : -1, -2); applyParams.topMargin = dp(12);
        clients.addView(apply, applyParams); clients.addView(body("Applying a filter reconnects active devices.")); addSpaced(content, clients);
        LinearLayout permissionHelp = panel(); permissionHelp.addView(eyebrow("USB PERMISSIONS"));
        permissionHelp.addView(text("Sharing and USB permissions", 19, INK, true));
        permissionHelp.addView(body("Android may ask for USB access again after unplugging a device or rebooting. Choose USBPortal as the default USB app when offered. Missing permission pauses that device’s sharing; your saved choice stays intact."));
        permissionHelp.addView(body("Once sharing is active, you can return to the home screen. USB debugging is not required and does not grant USBPortal controller access."));
        permissionHelp.addView(body("Device icons describe their type, not guaranteed compatibility. USB hubs and isochronous audio/video remain unsupported."));
        addSpaced(content, permissionHelp);
    }
    private void saveBoot(Switch boot, boolean checked) {
        if (!store.prefs.edit().putBoolean(ShareStore.BOOT, checked).commit()) {
            boot.setOnCheckedChangeListener(null); boot.setChecked(!checked);
            boot.setOnCheckedChangeListener((view, value) -> saveBoot(boot, value));
            toast("Could not save startup preference");
        }
    }
    private void buildDiagnostics() {
        LinearLayout content = page(2);
        addPageHeading(content, "A closer look", "Connection details stay here, away from your device controls.");
        LinearLayout connectionPanel = panel(); connectionPanel.addView(eyebrow("HOST CONNECTION"));
        diagnosticStatus = text("Connecting to service…", 18, INK, true); connectionPanel.addView(diagnosticStatus);
        diagnosticAddresses = text("", 14, MUTED, false); diagnosticAddresses.setTextIsSelectable(true);
        diagnosticAddresses.setTypeface(Typeface.MONOSPACE); diagnosticAddresses.setPadding(0, dp(10), 0, dp(8));
        connectionPanel.addView(diagnosticAddresses);
        connectionPanel.addView(body("USB/IP · TCP 3240    Discovery · UDP 3241\nUse your Linux client’s Connect by IP or Network for routed LANs and VPNs."));
        Button copy = button("Copy diagnostics", this::copyDiagnostics, false);
        LinearLayout.LayoutParams copyParams = new LinearLayout.LayoutParams(wide ? dp(220) : -1, -2); copyParams.topMargin = dp(12);
        connectionPanel.addView(copy, copyParams); addSpaced(content, connectionPanel);
        LinearLayout logPanel = panel(); logPanel.addView(eyebrow("RECENT ACTIVITY"));
        logPanel.addView(text("Connection log", 19, INK, true));
        logPanel.addView(body("Latest 12 messages from this service session. This is not a complete history."));
        log = text("No recent activity.", 13, MUTED, false); log.setTextIsSelectable(true); log.setTypeface(Typeface.MONOSPACE);
        log.setPadding(0, dp(12), 0, 0); logPanel.addView(log); addSpaced(content, logPanel);
        TextView footnote = body("USBPortal is intended for trusted LANs and VPNs. Internet exposure is unsupported."); content.addView(footnote);
    }
    private void addPageHeading(LinearLayout content, String title, String subtitle) {
        content.addView(text(title, wide ? 27 : 24, INK, true));
        TextView description = body(subtitle); description.setPadding(0, dp(6), 0, dp(19)); content.addView(description);
    }
    private void addSpaced(LinearLayout parent, View child) {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, -2); params.bottomMargin = dp(14); parent.addView(child, params);
    }
    private void applyClientFilter() {
        String value = clientFilter.getText().toString();
        try { new AccessPolicy(value); }
        catch (IllegalArgumentException e) { clientFilter.setError(error(e)); clientFilter.requestFocus(); return; }
        Runnable save = () -> {
            if (!store.prefs.edit().putString(ShareStore.CLIENTS, value).commit()) { toast("Could not save client filter"); return; }
            clientFilter.setError(null);
            if (service != null) service.applyFilter();
            ((InputMethodManager) getSystemService(INPUT_METHOD_SERVICE)).hideSoftInputFromWindow(clientFilter.getWindowToken(), 0);
            toast("Client filter saved");
        };
        if (service != null && service.isRunning()) new AlertDialog.Builder(this).setTitle("Apply client filter?")
                .setMessage("Active devices will disconnect and can reconnect if the new filter allows their client.")
                .setNegativeButton("Cancel", null).setPositiveButton("Apply", (dialog, which) -> save.run()).show();
        else save.run();
    }
    private void showTab(int index, boolean fromUser) {
        if (fromUser && index != selectedTab && clientFilter != null)
            ((InputMethodManager) getSystemService(INPUT_METHOD_SERVICE)).hideSoftInputFromWindow(clientFilter.getWindowToken(), 0);
        selectedTab = index;
        if (Build.VERSION.SDK_INT >= 33) {
            if (index != 0 && systemBack == null) systemBack = ModernBack.register(this, () -> showTab(0, true));
            else if (index == 0 && systemBack != null) { ModernBack.unregister(this, systemBack); systemBack = null; }
        }
        for (int i = 0; i < 3; i++) {
            scrolls[i].setVisibility(i == index ? View.VISIBLE : View.GONE);
            tabs[i].setSelected(i == index); tabs[i].setTextColor(i == index ? CYAN : MUTED);
            tabs[i].setContentDescription(TABS[i] + (i == index ? ", selected" : ""));
        }
        if (fromUser) tabs[index].requestFocus();
        update();
    }
    @Override protected void onStart() {
        super.onStart();
        bound = bindService(new Intent(this, HostService.class), connection, Context.BIND_AUTO_CREATE);
        refresh.post(render);
    }
    @Override protected void onResume() { super.onResume(); finishPendingPermission(); }
    @Override protected void onStop() {
        refresh.removeCallbacks(render);
        if (bound) { unbindService(connection); bound = false; service = null; }
        super.onStop();
    }
    @Override protected void onDestroy() {
        if (receiverRegistered) unregisterReceiver(permission);
        if (Build.VERSION.SDK_INT >= 33 && systemBack != null) ModernBack.unregister(this, systemBack);
        super.onDestroy();
    }
    @Override protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent); setIntent(intent);
        // A default-app launch grants Android access, but never selects a previously unshared device.
        finishPendingPermission(); update();
    }
    @Override protected void onSaveInstanceState(Bundle state) {
        state.putInt("tab", selectedTab); state.putString("pending_usb", pendingDevice);
        if (clientFilter != null) state.putString("filter_draft", clientFilter.getText().toString());
        for (int i = 0; i < scrolls.length; i++) state.putInt("scroll_" + i, scrolls[i].getScrollY());
        super.onSaveInstanceState(state);
    }
    @Override public void onBackPressed() {
        if (selectedTab != 0) showTab(0, true); else super.onBackPressed();
    }
    private void toggleSharing() {
        if (service == null) { message("Waiting for the USB sharing service. Try again in a moment.", AMBER); return; }
        if (service.isRunning()) {
            int inUse = 0;
            if (service.registry != null) for (AndroidRegistry.Row row : service.registry.rows()) if (row.inUse) inUse++;
            if (inUse > 0) new AlertDialog.Builder(this).setTitle("Stop USB sharing?")
                    .setMessage("Devices currently in use will disconnect. Your remembered sharing choices will remain saved.")
                    .setNegativeButton("Cancel", null).setPositiveButton("Stop sharing", (dialog, which) -> sendToggle(true)).show();
            else sendToggle(true);
        } else sendToggle(false);
    }
    private void sendToggle(boolean stop) {
        try {
            Intent intent = new Intent(this, HostService.class).setAction(stop ? HostService.STOP : HostService.START);
            if (stop) startService(intent);
            else {
                if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
                    requestPermissions(new String[] {Manifest.permission.POST_NOTIFICATIONS}, 2);
                startForegroundService(intent);
            }
            actionMessage.setVisibility(View.GONE);
        } catch (RuntimeException e) { message("Could not change sharing: " + error(e), RED); }
    }
    private void update() {
        if (heroTitle == null) return;
        String host = hostAddresses(); setText(addresses, host);
        setText(diagnosticAddresses, host);
        if (service == null) {
            setText(heroTitle, "Connecting to service…"); setText(heroDetail, "Your sharing preferences stay on this device.");
            setText(diagnosticStatus, "Waiting for host service"); setText(serverPill, "CHECKING HOST");
            serverPill.setTextColor(MUTED); toggle.setEnabled(false);
            if (readyCount != null) { setText(readyCount, "0"); setText(inUseCount, "0"); }
            for (DeviceCard card : cards.values()) card.serviceUnavailable();
            return;
        }
        toggle.setEnabled(true);
        boolean running = service.isRunning(); setText(toggle, running ? "Stop sharing" : "Start sharing");
        setText(serverPill, running ? "HOST ACTIVE" : "HOST PAUSED"); serverPill.setTextColor(running ? MINT : MUTED);
        setText(heroTitle, running ? "Sharing is on" : "Sharing is off");
        setText(heroDetail, running ? "Ready for your Linux client, on a trusted LAN or VPN." :
                ("Stopped".equals(service.status()) ? "Remembered devices resume when you start sharing." : service.status()));
        setText(diagnosticStatus, service.status());
        String messages = service.recentLog(); setText(log, messages.isEmpty() ? "No recent activity." : messages);
        if (service.registry == null) {
            setText(heroDetail, service.status());
            if (deviceGrid.getChildCount() == 0) deviceGrid.addView(emptyPanel("USB settings are unavailable", "See Diagnostics for the service error."));
            return;
        }
        List<AndroidRegistry.Row> rows = service.registry.rows();
        List<ShareStore.Slot> missing = service.registry.missing();
        int ready = 0, inUse = 0;
        for (AndroidRegistry.Row row : rows) if (running && row.ready) { if (row.inUse) inUse++; else ready++; }
        if (readyCount != null) { setText(readyCount, Integer.toString(ready)); setText(inUseCount, Integer.toString(inUse)); }
        setText(deviceCount, rows.size() + " attached" + (missing.isEmpty() ? "" : " · " + missing.size() + " remembered offline"));
        List<String> order = new ArrayList<>();
        for (AndroidRegistry.Row row : rows) {
            String key = "device_" + row.device.getDeviceName(); order.add(key);
            DeviceCard card = cards.get(key);
            if (card == null) { card = new DeviceCard(key); cards.put(key, card); }
            card.bind(row, running);
        }
        for (ShareStore.Slot slot : missing) {
            String key = "missing_" + slot.id; order.add(key);
            DeviceCard card = cards.get(key);
            if (card == null) { card = new DeviceCard(key); cards.put(key, card); }
            card.bindMissing(slot);
        }
        String key = order.toString();
        if (!gridKey.equals(key) || deviceGrid.getChildCount() == 0) {
            gridKey = key; rebuildGrid(order);
        }
    }
    private void rebuildGrid(List<String> order) {
        View focus = getCurrentFocus(); Object tag = focus == null ? null : focus.getTag();
        boolean deviceFocus = tag instanceof String && ((String) tag).startsWith("action_");
        int scrollY = scrolls[0].getScrollY();
        deviceGrid.removeAllViews();
        if (order.isEmpty()) deviceGrid.addView(emptyPanel("A place for your devices", "Plug in a controller or another supported USB device, then choose Share to remember it."));
        LinearLayout line = null;
        for (int i = 0; i < order.size(); i++) {
            DeviceCard card = cards.get(order.get(i));
            if (card.surface.getParent() != null) ((ViewGroup) card.surface.getParent()).removeView(card.surface);
            if (!wide || i % 2 == 0) {
                line = row(); line.setGravity(Gravity.TOP); deviceGrid.addView(line, new LinearLayout.LayoutParams(-1, -2));
            }
            LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(0, -2, 1); params.bottomMargin = dp(12);
            if (wide && i % 2 == 1) params.leftMargin = dp(12);
            line.addView(card.surface, params);
            if (wide && i == order.size() - 1 && i % 2 == 0) {
                Space spacer = new Space(this); LinearLayout.LayoutParams space = new LinearLayout.LayoutParams(0, 1, 1);
                space.leftMargin = dp(12); line.addView(spacer, space);
            }
        }
        cards.keySet().retainAll(new HashSet<>(order));
        if (deviceFocus) {
            View replacement = deviceGrid.findViewWithTag(tag);
            if (replacement != null && replacement.getVisibility() == View.VISIBLE) replacement.requestFocus(); else tabs[0].requestFocus();
        }
        scrolls[0].post(() -> scrolls[0].scrollTo(0, scrollY));
    }
    private LinearLayout emptyPanel(String title, String description) {
        LinearLayout empty = panel(); empty.setPadding(dp(22), dp(25), dp(22), dp(25));
        ImageView icon = new ImageView(this); icon.setImageDrawable(new PortalIcon(DeviceKind.UNKNOWN, CYAN));
        empty.addView(icon, new LinearLayout.LayoutParams(dp(42), dp(42)));
        TextView heading = text(title, 20, INK, true); heading.setPadding(0, dp(16), 0, dp(6)); empty.addView(heading);
        empty.addView(body(description)); return empty;
    }
    private final class DeviceCard {
        final LinearLayout surface, actions;
        final TextView title, details, state, hint;
        final ImageView icon;
        final Button primary, secondary;
        DeviceKind kind;
        DeviceCard(String key) {
            surface = panel(); surface.setPadding(dp(18), dp(18), dp(18), dp(17));
            LinearLayout top = row(); top.setGravity(Gravity.CENTER_VERTICAL);
            icon = new ImageView(MainActivity.this); icon.setPadding(dp(11), dp(11), dp(11), dp(11));
            icon.setBackground(glass(13, 0xff243a46, 0xff1b2a34, 0xff3c515c));
            top.addView(icon, new LinearLayout.LayoutParams(dp(58), dp(58)));
            LinearLayout labels = column(); labels.setPadding(dp(14), 0, 0, 0);
            title = text("USB device", 18, INK, true); labels.addView(title);
            details = text("", 11, MUTED, false); details.setPadding(0, dp(5), 0, 0); labels.addView(details);
            top.addView(labels, new LinearLayout.LayoutParams(0, -2, 1)); surface.addView(top);
            state = pill("Not shared", MUTED); if (wide) state.setTextSize(13); LinearLayout.LayoutParams stateParams = new LinearLayout.LayoutParams(-2, -2);
            stateParams.topMargin = dp(14); surface.addView(state, stateParams);
            hint = text("", 13, MUTED, false); hint.setMinLines(2); hint.setPadding(0, dp(8), 0, dp(12)); surface.addView(hint);
            actions = column(); primary = button("Share & remember", () -> {}, true); primary.setTextSize(14);
            primary.setTag("action_" + key); actions.addView(primary, new LinearLayout.LayoutParams(-1, -2));
            secondary = button("Forget device", () -> {}, false); secondary.setTextSize(13); secondary.setTag("action_secondary_" + key);
            LinearLayout.LayoutParams extra = new LinearLayout.LayoutParams(-1, -2); extra.topMargin = dp(7);
            actions.addView(secondary, extra); secondary.setVisibility(View.GONE); surface.addView(actions);
        }
        void bind(AndroidRegistry.Row row, boolean running) {
            UsbDevice device = row.device;
            if (kind == null || kind == DeviceKind.UNKNOWN) {
                try { kind = DevicePresentation.kind(device); }
                catch (RuntimeException e) { kind = DeviceKind.UNKNOWN; }
                icon.setImageDrawable(new PortalIcon(kind, CYAN));
                icon.setContentDescription(DevicePresentation.label(kind));
            }
            setText(title, DevicePresentation.name(device));
            setText(details, DevicePresentation.label(kind) + String.format(Locale.ROOT, "  ·  %04x:%04x", device.getVendorId(), device.getProductId()) +
                    (row.slot > 0 ? "\nBus ID 1-" + row.slot : ""));
            boolean allowed = usb.hasPermission(device);
            int tint; String label, description;
            if (row.slot == 0) {
                tint = MUTED; label = "Not shared"; description = "Only devices you choose are shared with remote clients.";
            } else if (!allowed) {
                tint = AMBER; label = "Permission needed"; description = "Sharing is remembered. Approve Android’s USB access to resume.";
            } else if (row.error != null) {
                tint = RED; label = "Needs attention"; description = row.error;
            } else if (!running) {
                tint = MUTED; label = "Sharing paused"; description = "Device remembered. Start sharing to make it available.";
            } else if (row.inUse) {
                tint = MINT; label = "In use"; description = "A remote client has exclusive use of this device.";
            } else if (row.ready) {
                tint = MINT; label = "Available"; description = "Ready for a remote client. Auto-connect is set on the client.";
            } else {
                tint = AMBER; label = "Preparing"; description = "Waiting for Android to make the remembered device available.";
            }
            setText(state, label); state.setTextColor(tint); setText(hint, description);
            primary.setEnabled(true); secondary.setEnabled(true);
            boolean secondaryFocused = secondary.hasFocus();
            if (!allowed) {
                setText(primary, row.slot == 0 ? "Allow USB access & share" : "Allow USB access");
                primary.setOnClickListener(view -> requestUsb(device));
                secondary.setVisibility(row.slot > 0 ? View.VISIBLE : View.GONE);
                secondary.setOnClickListener(view -> confirmForget(row.slot, DevicePresentation.name(device), row.inUse && running));
            } else {
                setText(primary, row.slot == 0 ? "Share & remember" : "Stop sharing & forget");
                primary.setOnClickListener(view -> {
                    if (service == null) { message("Host service is unavailable. Try again in a moment.", AMBER); return; }
                    if (row.slot == 0) { service.remember(device); message("Sharing requested. The device status will confirm when it is ready.", MUTED); }
                    else confirmForget(row.slot, DevicePresentation.name(device), row.inUse && running);
                }); secondary.setVisibility(View.GONE);
            }
            primary.setContentDescription(primary.getText() + " for " + DevicePresentation.name(device) + ", " + label);
            secondary.setContentDescription("Forget " + DevicePresentation.name(device));
            if (secondaryFocused && secondary.getVisibility() != View.VISIBLE) primary.requestFocus();
        }
        void bindMissing(ShareStore.Slot slot) {
            kind = DeviceKind.UNKNOWN; icon.setImageDrawable(new PortalIcon(kind, MUTED));
            icon.setContentDescription("Remembered USB device");
            setText(title, "Remembered USB device");
            setText(details, String.format(Locale.ROOT, "%04x:%04x  ·  Bus ID 1-%d", slot.vendor, slot.product, slot.id));
            setText(state, "Disconnected"); state.setTextColor(MUTED);
            setText(hint, "Waiting for the device to return. Android may ask for USB access again.");
            setText(primary, "Forget device"); primary.setOnClickListener(view -> confirmForget(slot.id, "this remembered device", false));
            primary.setEnabled(true); secondary.setEnabled(true);
            primary.setContentDescription(String.format(Locale.ROOT, "Forget remembered USB device %04x:%04x, bus ID 1-%d", slot.vendor, slot.product, slot.id));
            secondary.setVisibility(View.GONE);
        }
        void serviceUnavailable() {
            setText(state, "Checking host"); state.setTextColor(MUTED);
            setText(hint, "Waiting for the sharing service to report the current device state.");
            primary.setContentDescription("Action unavailable for " + title.getText() + " while checking the host service");
            primary.setEnabled(false); secondary.setEnabled(false);
        }
    }
    private void confirmForget(int slot, String name, boolean inUse) {
        new AlertDialog.Builder(this).setTitle("Forget " + name + "?")
                .setMessage((inUse ? "The remote client will disconnect. " : "") + "This removes the saved sharing choice. You can share the device again later.")
                .setNegativeButton("Cancel", null).setPositiveButton("Forget", (dialog, which) -> {
                    if (service != null) {
                        service.forget(slot); message("Removal requested. The device status will confirm the saved change.", MUTED);
                    } else message("Host service is unavailable. The sharing choice has not changed.", AMBER);
                }).show();
    }
    private void requestUsb(UsbDevice device) {
        if (!pendingDevice.isEmpty() && !pendingDevice.equals(device.getDeviceName())) {
            UsbDevice pending = usb.getDeviceList().get(pendingDevice);
            if (pending != null && !usb.hasPermission(pending)) {
                message("Finish the current USB permission request first. Select that device again to retry if its dialog was dismissed.", AMBER); return;
            }
        }
        pendingDevice = device.getDeviceName();
        Intent result = new Intent(USB_PERMISSION).setPackage(getPackageName());
        PendingIntent callback = PendingIntent.getBroadcast(this, device.getDeviceId(), result,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_MUTABLE);
        try { usb.requestPermission(device, callback); }
        catch (RuntimeException e) { pendingDevice = ""; message("USB access: " + error(e), RED); }
    }
    private void finishPendingPermission() {
        if (pendingDevice.isEmpty() || usb == null) return;
        UsbDevice device = usb.getDeviceList().get(pendingDevice);
        if (device == null) { pendingDevice = ""; return; }
        if (usb.hasPermission(device) && service != null) {
            pendingDevice = ""; service.remember(device);
            message("USB access approved. Sharing will resume for this device.", MUTED);
        }
    }
    private void copyDiagnostics() {
        String report = "USBPortal Android\nDevice: " + Build.MODEL + "\nAndroid: " + Build.VERSION.RELEASE +
                "\nHost: " + hostAddresses() + "\nUSB/IP TCP 3240; discovery UDP 3241\nStatus: " +
                (service == null ? "Service unavailable" : service.status()) + "\n\nRecent activity (session only):\n" +
                (service == null ? "Unavailable" : service.recentLog());
        ((ClipboardManager) getSystemService(CLIPBOARD_SERVICE)).setPrimaryClip(ClipData.newPlainText("USBPortal diagnostics", report));
        toast("Diagnostics copied");
    }
    private String hostAddresses() {
        SortedSet<String> ips = new TreeSet<>();
        try {
            Enumeration<NetworkInterface> interfaces = NetworkInterface.getNetworkInterfaces();
            if (interfaces != null) while (interfaces.hasMoreElements()) {
                NetworkInterface iface = interfaces.nextElement(); if (!iface.isUp() || iface.isLoopback()) continue;
                Enumeration<InetAddress> addresses = iface.getInetAddresses();
                while (addresses.hasMoreElements()) {
                    InetAddress address = addresses.nextElement(); if (address instanceof Inet4Address) ips.add(address.getHostAddress());
                }
            }
        } catch (SocketException ignored) { }
        return ips.isEmpty() ? "No local IPv4 address · connect to a LAN or VPN" : String.join("  /  ", ips);
    }
    private void message(String value, int color) {
        if (actionMessage == null) return;
        actionMessage.setText(value); actionMessage.setTextColor(color); actionMessage.setVisibility(View.VISIBLE);
    }
    private static String error(Throwable error) {
        String message = error.getMessage(); return message == null || message.isEmpty() ? error.getClass().getSimpleName() : message;
    }
    private LinearLayout row() { LinearLayout view = new LinearLayout(this); view.setOrientation(LinearLayout.HORIZONTAL); return view; }
    private LinearLayout column() { LinearLayout view = new LinearLayout(this); view.setOrientation(LinearLayout.VERTICAL); return view; }
    private LinearLayout panel() {
        LinearLayout panel = column(); panel.setPadding(dp(22), dp(20), dp(22), dp(20));
        panel.setBackground(glass(19, 0xef22313d, 0xe616222d, 0xff394a57)); panel.setElevation(dp(1)); return panel;
    }
    private TextView text(String value, int size, int color, boolean bold) {
        TextView text = new TextView(this); text.setText(value); text.setTextSize(size); text.setTextColor(color);
        text.setFontFeatureSettings("kern"); text.setIncludeFontPadding(false); text.setLineSpacing(dp(2), 1f);
        text.setTypeface(Typeface.create(bold ? "sans-serif-medium" : "sans-serif", Typeface.NORMAL)); return text;
    }
    private TextView eyebrow(String value) {
        TextView text = text(value, 10, CYAN, true); text.setLetterSpacing(.14f); text.setPadding(0, 0, 0, dp(10)); return text;
    }
    private TextView body(String value) {
        TextView text = text(value, 14, MUTED, false); text.setPadding(0, dp(8), 0, dp(3)); return text;
    }
    private TextView pill(String value, int color) {
        TextView text = text(value, 11, color, true); text.setPadding(dp(10), dp(5), dp(10), dp(5));
        text.setBackground(glass(6, 0x71364a56, 0x42253640, 0x603f5665)); return text;
    }
    private Button button(String title, Runnable action, boolean primary) {
        Button button = new Button(this); button.setText(title); button.setAllCaps(false); button.setTextSize(15);
        button.setTextColor(primary ? CYAN : INK); button.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        button.setPadding(dp(12), dp(6), dp(12), dp(6)); button.setMinHeight(dp(46)); button.setMinimumWidth(0);
        button.setStateListAnimator(null); button.setBackground(controlBackground(primary));
        button.setOnClickListener(view -> action.run()); return button;
    }
    private Drawable controlBackground(boolean primary) {
        StateListDrawable states = new StateListDrawable();
        states.addState(new int[] {-android.R.attr.state_enabled}, glass(10, 0xff24303a, 0xff1c252e, 0xff36414b));
        states.addState(new int[] {android.R.attr.state_pressed}, glass(10, 0xff436675, 0xff304b59, CYAN));
        states.addState(new int[] {android.R.attr.state_focused}, glass(10, 0xff385967, 0xff263f4d, CYAN));
        states.addState(new int[] {android.R.attr.state_selected}, glass(10, 0xff2a414f, 0xff20333f, 0xff587583));
        states.addState(new int[] {}, glass(10, primary ? 0xff2a4553 : 0x312b3c4a, primary ? 0xff203541 : 0x17182430,
                primary ? 0xff536e7c : 0xff374b59));
        return new RippleDrawable(ColorStateList.valueOf(0x304db1c9), states, glass(10, Color.WHITE, Color.WHITE, Color.WHITE));
    }
    private GradientDrawable glass(int radius, int top, int bottom, int border) {
        GradientDrawable drawable = new GradientDrawable(GradientDrawable.Orientation.TL_BR, new int[] {top, bottom});
        drawable.setCornerRadius(dp(radius)); drawable.setStroke(dp(1), border); return drawable;
    }
    private static void setText(TextView view, String value) {
        if (value == null) value = "";
        if (view != null && !value.contentEquals(view.getText())) view.setText(value);
    }
    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }
    private void toast(String value) { Toast.makeText(this, value, Toast.LENGTH_LONG).show(); }
    /** Keep API33-only types out of the Activity's API26 class signatures. */
    private static final class ModernBack {
        static Object register(Activity activity, Runnable action) {
            android.window.OnBackInvokedCallback callback = action::run;
            activity.getOnBackInvokedDispatcher().registerOnBackInvokedCallback(
                    android.window.OnBackInvokedDispatcher.PRIORITY_DEFAULT, callback);
            return callback;
        }
        static void unregister(Activity activity, Object callback) {
            activity.getOnBackInvokedDispatcher().unregisterOnBackInvokedCallback((android.window.OnBackInvokedCallback) callback);
        }
    }
}
