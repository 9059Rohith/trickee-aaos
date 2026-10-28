package com.trickee.driver;

import android.content.Context;
import android.content.SharedPreferences;
import java.util.UUID;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/** Driver-owned assignment actions saved before an attempted network request. */
final class OrderActionQueue {
    static final String PICKUP = "PICKUP";
    static final String REPORT_DELIVERY = "REPORT_DELIVERY";
    private static final String PREFS = "order_action_queue";
    private static final String ITEMS = "items_v1";
    private static final int LIMIT = 100;
    private final SharedPreferences prefs;

    OrderActionQueue(Context context) {
        this(context.getSharedPreferences(PREFS, Context.MODE_PRIVATE));
    }

    OrderActionQueue(SharedPreferences prefs) { this.prefs = prefs; }

    private JSONArray items() {
        try { return new JSONArray(prefs.getString(ITEMS, "[]")); }
        catch (JSONException corrupt) { throw new IllegalStateException("Assignment action queue needs recovery", corrupt); }
    }

    private void save(JSONArray items) {
        if (!prefs.edit().putString(ITEMS, items.toString()).commit())
            throw new IllegalStateException("Assignment action could not be saved");
    }

    static boolean owns(JSONObject item, String origin, JSONObject me) {
        return origin.equals(item.optString("origin"))
                && me.optString("tenant_id").equals(item.optString("tenant_id"))
                && me.optString("driver_id").equals(item.optString("driver_id"))
                && me.optString("vehicle_id").equals(item.optString("vehicle_id"));
    }

    JSONObject put(String origin, JSONObject me, String orderId, String action) throws JSONException {
        synchronized (OrderActionQueue.class) {
            if (origin.isBlank() || me.optString("tenant_id").isBlank()
                    || me.optString("driver_id").isBlank() || me.optString("vehicle_id").isBlank()
                    || !orderId.matches("[A-Za-z0-9_-]{1,64}")
                    || !(PICKUP.equals(action) || REPORT_DELIVERY.equals(action)))
                throw new IllegalArgumentException("A connected driver and valid assignment are required");
            JSONArray all = items();
            for (int i = 0; i < all.length(); i++) {
                JSONObject existing = all.getJSONObject(i);
                if (owns(existing, origin, me) && orderId.equals(existing.optString("order_id"))) {
                    if (!action.equals(existing.optString("action")))
                        throw new IllegalStateException("Sync the previous assignment action first");
                    return existing;
                }
            }
            if (all.length() >= LIMIT)
                throw new IllegalStateException("Assignment queue is full; sync existing actions first");
            JSONObject item = new JSONObject().put("origin", origin)
                    .put("tenant_id", me.getString("tenant_id"))
                    .put("driver_id", me.getString("driver_id"))
                    .put("vehicle_id", me.getString("vehicle_id"))
                    .put("order_id", orderId).put("action", action)
                    .put("idempotency_key", "order-" + UUID.randomUUID().toString().replace("-", ""))
                    .put("state", "QUEUED");
            all.put(item); save(all); return item;
        }
    }

    JSONArray forOwner(String origin, JSONObject me) throws JSONException {
        synchronized (OrderActionQueue.class) {
            JSONArray result = new JSONArray(), all = items();
            for (int i = 0; i < all.length(); i++) {
                JSONObject item = all.getJSONObject(i);
                if (owns(item, origin, me)) result.put(item);
            }
            return result;
        }
    }

    JSONObject find(String origin, JSONObject me, String orderId) throws JSONException {
        synchronized (OrderActionQueue.class) {
            JSONArray owned = forOwner(origin, me);
            for (int i = 0; i < owned.length(); i++) {
                JSONObject item = owned.getJSONObject(i);
                if (orderId.equals(item.optString("order_id"))) return item;
            }
            return null;
        }
    }

    void remove(String origin, JSONObject me, String orderId) throws JSONException {
        synchronized (OrderActionQueue.class) {
            JSONArray all = items(), remaining = new JSONArray();
            for (int i = 0; i < all.length(); i++) {
                JSONObject item = all.getJSONObject(i);
                if (!(owns(item, origin, me) && orderId.equals(item.optString("order_id"))))
                    remaining.put(item);
            }
            if (remaining.length() != all.length()) save(remaining);
        }
    }

    void needsReview(String origin, JSONObject me, String orderId, int status) throws JSONException {
        changeState(origin, me, orderId, "NEEDS_REVIEW", status);
    }

    void retry(String origin, JSONObject me, String orderId) throws JSONException {
        changeState(origin, me, orderId, "QUEUED", 0);
    }

    private void changeState(String origin, JSONObject me, String orderId, String state, int status)
            throws JSONException {
        synchronized (OrderActionQueue.class) {
            JSONArray all = items();
            for (int i = 0; i < all.length(); i++) {
                JSONObject item = all.getJSONObject(i);
                if (owns(item, origin, me) && orderId.equals(item.optString("order_id"))) {
                    item.put("state", state);
                    if (status == 0) item.remove("http_status");
                    else item.put("http_status", status);
                    save(all); return;
                }
            }
        }
    }
}
