package com.trickee.driver;

import android.content.Context;
import android.content.SharedPreferences;
import java.util.UUID;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/** Driver-owned pickup decisions that remain distinct from server-confirmed outcomes. */
final class DispatchReplyQueue {
    private static final String PREFS = "dispatch_reply_queue";
    private static final String ITEMS = "items_v1";
    private static final int LIMIT = 100;
    private final SharedPreferences prefs;

    DispatchReplyQueue(Context context) {
        this(context.getSharedPreferences(PREFS, Context.MODE_PRIVATE));
    }

    DispatchReplyQueue(SharedPreferences prefs) { this.prefs = prefs; }

    private JSONArray items() {
        try { return new JSONArray(prefs.getString(ITEMS, "[]")); }
        catch (JSONException corrupt) { throw new IllegalStateException("Pickup decision queue needs recovery", corrupt); }
    }

    private void save(JSONArray items) {
        if (!prefs.edit().putString(ITEMS, items.toString()).commit())
            throw new IllegalStateException("Pickup decision could not be saved");
    }

    static boolean owns(JSONObject item, String origin, JSONObject me) {
        return origin.equals(item.optString("origin"))
                && me.optString("tenant_id").equals(item.optString("tenant_id"))
                && me.optString("driver_id").equals(item.optString("driver_id"))
                && me.optString("vehicle_id").equals(item.optString("vehicle_id"));
    }

    JSONObject put(String origin, JSONObject me, String id, String outcome) throws JSONException {
        synchronized (DispatchReplyQueue.class) {
            if (origin.isBlank() || me.optString("tenant_id").isBlank()
                    || me.optString("driver_id").isBlank() || me.optString("vehicle_id").isBlank()
                    || !id.matches("[A-Za-z0-9_-]{1,64}")
                    || !("ACCEPTED".equals(outcome) || "DECLINED".equals(outcome)))
                throw new IllegalArgumentException("A connected driver and valid pickup proposal are required");
            JSONArray all = items();
            for (int i = 0; i < all.length(); i++) {
                JSONObject existing = all.getJSONObject(i);
                if (owns(existing, origin, me) && id.equals(existing.optString("offer_id"))) {
                    if (!outcome.equals(existing.optString("outcome")))
                        throw new IllegalStateException("A different decision is already queued for this proposal");
                    return existing;
                }
            }
            if (all.length() >= LIMIT)
                throw new IllegalStateException("Pickup decision queue is full; sync existing choices first");
            JSONObject item = new JSONObject().put("origin", origin)
                    .put("tenant_id", me.getString("tenant_id"))
                    .put("driver_id", me.getString("driver_id"))
                    .put("vehicle_id", me.getString("vehicle_id"))
                    .put("offer_id", id).put("outcome", outcome)
                    .put("idempotency_key", "answer-" + UUID.randomUUID().toString().replace("-", ""))
                    .put("state", "QUEUED");
            all.put(item); save(all); return item;
        }
    }

    JSONArray forOwner(String origin, JSONObject me) throws JSONException {
        synchronized (DispatchReplyQueue.class) {
            JSONArray result = new JSONArray(), all = items();
            for (int i = 0; i < all.length(); i++) {
                JSONObject item = all.getJSONObject(i);
                if (owns(item, origin, me)) result.put(item);
            }
            return result;
        }
    }

    JSONObject find(String origin, JSONObject me, String id) throws JSONException {
        synchronized (DispatchReplyQueue.class) {
            JSONArray all = forOwner(origin, me);
            for (int i = 0; i < all.length(); i++) {
                JSONObject item = all.getJSONObject(i);
                if (id.equals(item.optString("offer_id"))) return item;
            }
            return null;
        }
    }

    void remove(String origin, JSONObject me, String id) throws JSONException {
        synchronized (DispatchReplyQueue.class) {
            JSONArray all = items(), remaining = new JSONArray();
            for (int i = 0; i < all.length(); i++) {
                JSONObject item = all.getJSONObject(i);
                if (!(owns(item, origin, me) && id.equals(item.optString("offer_id")))) remaining.put(item);
            }
            if (remaining.length() != all.length()) save(remaining);
        }
    }

    void needsReview(String origin, JSONObject me, String id, int status) throws JSONException {
        changeState(origin, me, id, "NEEDS_REVIEW", status);
    }

    void retry(String origin, JSONObject me, String id) throws JSONException {
        changeState(origin, me, id, "QUEUED", 0);
    }

    private void changeState(String origin, JSONObject me, String id, String state, int status)
            throws JSONException {
        synchronized (DispatchReplyQueue.class) {
            JSONArray all = items();
            for (int i = 0; i < all.length(); i++) {
                JSONObject item = all.getJSONObject(i);
                if (owns(item, origin, me) && id.equals(item.optString("offer_id"))) {
                    item.put("state", state);
                    if (status == 0) item.remove("http_status");
                    else item.put("http_status", status);
                    save(all); return;
                }
            }
        }
    }
}
