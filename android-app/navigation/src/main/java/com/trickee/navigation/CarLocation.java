package com.trickee.navigation;

import android.Manifest;
import android.content.Context;
import android.content.pm.PackageManager;
import android.location.Location;
import android.location.LocationListener;
import android.location.LocationManager;
import android.os.Handler;
import android.os.Looper;

/** Foreground location gate for car route preview. No background trace is stored. */
final class CarLocation {
    private static final long MAX_FIX_AGE_MS = 15_000;
    interface Subscription {
        void close();
    }

    interface Callback {
        void onLocation(Location location);
    }

    private CarLocation() { }

    static boolean permitted(Context context) {
        return context.checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)
                == PackageManager.PERMISSION_GRANTED;
    }

    static boolean usable(Location location, boolean debugBuild) {
        if (location == null || !NavigationRoute.valid(location.getLatitude(), location.getLongitude())) {
            return false;
        }
        if (!location.hasAccuracy() || !validFixMetadata(
                location.getTime(), System.currentTimeMillis(), location.getAccuracy())) {
            return false;
        }
        return debugBuild || !location.isFromMockProvider();
    }

    static boolean validFixMetadata(long observedAtMs, long nowMs, float accuracyMetres) {
        long age = nowMs - observedAtMs;
        return age >= -5_000 && age <= MAX_FIX_AGE_MS
                && Float.isFinite(accuracyMetres)
                && accuracyMetres > 0 && accuracyMetres <= 100;
    }

    static Location latest(Context context, boolean debugBuild) {
        if (!permitted(context)) return null;
        LocationManager manager = (LocationManager) context.getSystemService(Context.LOCATION_SERVICE);
        if (manager == null) return null;
        Location gps = null;
        Location network = null;
        try {
            gps = manager.getLastKnownLocation(LocationManager.GPS_PROVIDER);
            network = manager.getLastKnownLocation(LocationManager.NETWORK_PROVIDER);
        } catch (SecurityException | IllegalArgumentException ignored) {
            return null;
        }
        boolean gpsUsable = usable(gps, debugBuild);
        boolean networkUsable = usable(network, debugBuild);
        if (gpsUsable && networkUsable) {
            return gps.getTime() >= network.getTime() ? gps : network;
        }
        return gpsUsable ? gps : networkUsable ? network : null;
    }

    static void requestFresh(Context context, boolean debugBuild, Callback callback) {
        if (!permitted(context)) {
            callback.onLocation(null);
            return;
        }
        LocationManager manager = (LocationManager) context.getSystemService(Context.LOCATION_SERVICE);
        if (manager == null) {
            callback.onLocation(null);
            return;
        }
        Handler handler = new Handler(Looper.getMainLooper());
        final boolean[] finished = {false};
        final LocationListener[] listener = new LocationListener[1];
        Runnable timeout = () -> {
            if (!finished[0]) {
                finished[0] = true;
                manager.removeUpdates(listener[0]);
                callback.onLocation(null);
            }
        };
        listener[0] = location -> {
            if (!finished[0] && usable(location, debugBuild)) {
                finished[0] = true;
                handler.removeCallbacks(timeout);
                manager.removeUpdates(listener[0]);
                callback.onLocation(location);
            }
        };
        try {
            manager.requestLocationUpdates(LocationManager.GPS_PROVIDER,
                    0L, 0f, listener[0], Looper.getMainLooper());
            handler.postDelayed(timeout, 8_000);
        } catch (SecurityException | IllegalArgumentException error) {
            callback.onLocation(null);
        }
    }

    /** Foreground-only updates owned by a visible car screen. */
    static Subscription listen(Context context, Callback callback) {
        if (!permitted(context)) return null;
        LocationManager manager = (LocationManager) context.getSystemService(Context.LOCATION_SERVICE);
        if (manager == null) return null;
        LocationListener listener = callback::onLocation;
        try {
            manager.requestLocationUpdates(LocationManager.GPS_PROVIDER,
                    1_000L, 2f, listener, Looper.getMainLooper());
        } catch (SecurityException | IllegalArgumentException error) {
            return null;
        }
        return new Subscription() {
            private boolean closed;

            @Override public void close() {
                if (closed) return;
                closed = true;
                try {
                    manager.removeUpdates(listener);
                } catch (SecurityException ignored) {
                    // Permission may have been revoked after registration.
                }
            }
        };
    }
}
