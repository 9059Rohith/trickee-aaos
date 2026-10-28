package com.trickee.driver;

import android.content.Context;
import android.content.SharedPreferences;

/** Local collection gate. The cloud consent event remains authoritative for ingestion. */
final class PhoneTripConsent {
    private static final String PREFS = "phone_trip_consent";

    private PhoneTripConsent() { }

    private static String key(String tenantId, String driverId) {
        return tenantId + ":" + driverId;
    }

    private static SharedPreferences prefs(Context context) {
        return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    static boolean isPending(Context context, String tenantId, String driverId) {
        return prefs(context).getBoolean("pending:" + key(tenantId, driverId), false);
    }

    static boolean isGranted(Context context, String tenantId, String driverId) {
        String key = key(tenantId, driverId);
        return !isPending(context, tenantId, driverId)
                && prefs(context).getBoolean("granted:" + key, false);
    }

    static boolean record(Context context, String tenantId, String driverId, boolean granted) {
        String key = key(tenantId, driverId);
        return prefs(context).edit().putBoolean("granted:" + key, granted)
                .putBoolean("pending:" + key, false).commit();
    }

    static boolean beginWithdrawal(Context context, String tenantId, String driverId) {
        String key = key(tenantId, driverId);
        return prefs(context).edit().putBoolean("granted:" + key, false)
                .putBoolean("pending:" + key, true).commit();
    }
}
