package org.usbportal.android;

import android.content.*;
import android.util.Log;

public final class BootReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context context, Intent intent) {
        if (!Intent.ACTION_BOOT_COMPLETED.equals(intent.getAction()) &&
                !Intent.ACTION_MY_PACKAGE_REPLACED.equals(intent.getAction())) return;
        ShareStore store = new ShareStore(context);
        if (store.prefs.getBoolean(ShareStore.BOOT, true) && store.prefs.getBoolean(ShareStore.RUNNING, false)) {
            try { context.startForegroundService(new Intent(context, HostService.class).setAction(HostService.START)); }
            catch (RuntimeException e) { Log.e("USBPortal", "Android prevented automatic startup", e); }
        }
    }
}
