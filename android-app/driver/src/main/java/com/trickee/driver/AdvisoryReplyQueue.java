package com.trickee.driver;

import android.content.Context;
import android.content.SharedPreferences;
import java.util.UUID;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/** Durable, bounded advisory choices. A choice is never a server-confirmed response. */
final class AdvisoryReplyQueue {
    private static final String PREFS = "advisory_reply_queue";
    private static final String ITEMS = "items_v1";
    private static final int LIMIT = 100;
    private final SharedPreferences prefs;

    AdvisoryReplyQueue(Context context) {
        this(context.getSharedPreferences(PREFS, Context.MODE_PRIVATE));
    }

    AdvisoryReplyQueue(SharedPreferences prefs) { this.prefs = prefs; }

    private JSONArray items() {
        try { return new JSONArray(prefs.getString(ITEMS, "[]")); }
        catch (JSONException corrupt) { throw new IllegalStateException("Advisory queue needs recovery", corrupt); }
    }

    private void save(JSONArray items) {
        if (!prefs.edit().putString(ITEMS, items.toString()).commit())
            throw new IllegalStateException("Advisory choice could not be saved");
    }

    static boolean owns(JSONObject item, String origin, JSONObject me) {
        return origin.equals(item.optString("origin"))
                && me.optString("tenant_id").equals(item.optString("tenant_id"))
                && me.optString("driver_id").equals(item.optString("driver_id"))
                && me.optString("vehicle_id").equals(item.optString("vehicle_id"));
    }

    JSONObject put(String origin, JSONObject me, String id, String outcome) throws JSONException {
        synchronized (AdvisoryReplyQueue.class) {
        if (origin.isBlank() || me.optString("tenant_id").isBlank()
                || me.optString("driver_id").isBlank() || me.optString("vehicle_id").isBlank()
                || !id.matches("[A-Za-z0-9_-]{1,64}")
                || !("ACKNOWLEDGED".equals(outcome) || "DECLINED".equals(outcome)))
            throw new IllegalArgumentException("A connected driver and valid advisory are required");
        JSONArray all = items();
        for (int i = 0; i < all.length(); i++) {
            JSONObject existing = all.getJSONObject(i);
            if (owns(existing, origin, me) && id.equals(existing.optString("nudge_id"))) {
                if (!outcome.equals(existing.optString("outcome")))
                    throw new IllegalStateException("A different reply is already queued for this advisory");
                return existing;
            }
        }
        if (all.length() >= LIMIT) throw new IllegalStateException("Advisory queue is full; sync existing choices first");
        JSONObject item = new JSONObject().put("origin", origin)
                .put("tenant_id", me.getString("tenant_id"))
                .put("driver_id", me.getString("driver_id"))
                .put("vehicle_id", me.getString("vehicle_id"))
                .put("nudge_id", id).put("outcome", outcome)
                .put("idempotency_key", "reply-" + UUID.randomUUID().toString().replace("-", ""))
                .put("state", "QUEUED");
        all.put(item); save(all); return item;
        }
    }

    JSONArray forOwner(String origin, JSONObject me) throws JSONException {
        synchronized (AdvisoryReplyQueue.class) {
        JSONArray result = new JSONArray(), all = items();
        for (int i = 0; i < all.length(); i++) {
            JSONObject item = all.getJSONObject(i);
            if (owns(item, origin, me)) result.put(item);
        }
        return result;
        }
    }

    JSONObject find(String origin, JSONObject me, String id) throws JSONException {
        synchronized (AdvisoryReplyQueue.class) {
        JSONArray all = forOwner(origin, me);
        for (int i = 0; i < all.length(); i++) {
            JSONObject item = all.getJSONObject(i);
            if (id.equals(item.optString("nudge_id"))) return item;
        }
        return null;
        }
    }

    void remove(String origin, JSONObject me, String id) throws JSONException {
        synchronized (AdvisoryReplyQueue.class) {
        JSONArray all = items(), remaining = new JSONArray();
        for (int i = 0; i < all.length(); i++) {
            JSONObject item = all.getJSONObject(i);
            if (!(owns(item, origin, me) && id.equals(item.optString("nudge_id")))) remaining.put(item);
        }
        if (remaining.length() != all.length()) save(remaining);
        }
    }

    void needsReview(String origin, JSONObject me, String id, int status) throws JSONException {
        synchronized (AdvisoryReplyQueue.class) {
        JSONArray all = items();
        for (int i = 0; i < all.length(); i++) {
            JSONObject item = all.getJSONObject(i);
            if (owns(item, origin, me) && id.equals(item.optString("nudge_id"))) {
                item.put("state", "NEEDS_REVIEW").put("http_status", status);
                save(all); return;
            }
        }
        }
    }

    void retry(String origin, JSONObject me, String id) throws JSONException {
        synchronized (AdvisoryReplyQueue.class) {
        JSONArray all = items();
        for (int i = 0; i < all.length(); i++) {
            JSONObject item = all.getJSONObject(i);
            if (owns(item, origin, me) && id.equals(item.optString("nudge_id"))) {
                item.put("state", "QUEUED").remove("http_status");
                save(all); return;
            }
        }
        }
    }
}
