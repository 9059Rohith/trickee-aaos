package com.trickee.driver;

import android.content.Context;
import android.content.SharedPreferences;
import android.util.Log;

/** Device-private upload state; never stores credentials, coordinates or point payloads. */
final class GpsSyncStatus {
    private static final String PREFS = "gps_sync_status";
    private static final String REASON = "reason";
    private static final String TAG = "TrickeeGpsSync";

    private GpsSyncStatus() { }

    private static void save(Context context, String reason) {
        SharedPreferences.Editor edit = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .edit().putString(REASON, reason);
        if (!edit.commit()) Log.e(TAG, "Could not persist GPS upload state");
    }

    static void clear(Context context) {
        SharedPreferences.Editor edit = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .edit().remove(REASON);
        if (!edit.commit()) Log.e(TAG, "Could not clear GPS upload state");
    }

    static void rejected(Context context, int httpStatus) { save(context, "HTTP_" + httpStatus); }
    static void retrying(Context context) { save(context, "RETRYING"); }
    static void assignmentMismatch(Context context) { save(context, "ASSIGNMENT"); }
    static void consentWithdrawn(Context context) { save(context, "CONSENT"); }
    static void localFailure(Context context) { save(context, "LOCAL"); }

    static String message(Context context) {
        String reason = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(REASON, "");
        if (reason == null) return "";
        switch (reason) {
            case "HTTP_409": return "GPS upload blocked: HTTP 409 conflict. Points retained; contact your fleet operator.";
            case "HTTP_401": return "GPS upload blocked: credential rejected. Points retained; reconnect with a valid driver credential.";
            case "HTTP_403": return "GPS upload blocked: phone-data consent does not cover these points. Points retained on this device.";
            case "CONSENT": return "GPS upload blocked: trip phone-data consent is absent or withdrawn. Points retained on this device.";
            case "HTTP_404": return "GPS upload blocked: trip not found. Points retained; contact your fleet operator.";
            case "ASSIGNMENT": return "GPS upload blocked: points belong to a different driver assignment. Reconnect as the original driver.";
            case "RETRYING": return "GPS upload retrying. Points remain on this device.";
            case "LOCAL": return "GPS upload blocked by a device error. Points retained; retry or contact support.";
            case "": return "";
            default:
                if (reason.matches("HTTP_[0-9]{3}"))
                    return "GPS upload blocked: HTTP " + reason.substring(5) + ". Points retained; contact your fleet operator.";
                return "";
        }
    }
}
