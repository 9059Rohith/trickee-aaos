package com.trickee.driver;

import android.content.Context;
import android.content.SharedPreferences;
import java.util.UUID;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/** Durable, driver-scoped issue reports and follow-ups awaiting server confirmation. */
final class ServiceCaseCommandQueue {
    private static final String PREFS = "service_case_command_queue";
    private static final String ITEMS = "items_v1";
    private static final int LIMIT = 100;
    private final SharedPreferences prefs;

    ServiceCaseCommandQueue(Context context) {
        this(context.getSharedPreferences(PREFS, Context.MODE_PRIVATE));
    }

    ServiceCaseCommandQueue(SharedPreferences prefs) { this.prefs = prefs; }

    private JSONArray items() {
        try { return new JSONArray(prefs.getString(ITEMS, "[]")); }
        catch (JSONException corrupt) { throw new IllegalStateException("Service case queue needs recovery", corrupt); }
    }

    private void save(JSONArray items) {
        if (!prefs.edit().putString(ITEMS, items.toString()).commit())
            throw new IllegalStateException("Service command could not be saved");
    }

    static boolean owns(JSONObject command, String origin, JSONObject me) {
        return origin.equals(command.optString("origin"))
                && me.optString("tenant_id").equals(command.optString("tenant_id"))
                && me.optString("driver_id").equals(command.optString("driver_id"))
                && me.optString("vehicle_id").equals(command.optString("vehicle_id"));
    }

    private static String id(String prefix) {
        return prefix + UUID.randomUUID().toString().replace("-", "");
    }

    JSONObject create(String origin, JSONObject me, String category, String priority,
                      String title, String description) throws JSONException {
        if (!("VEHICLE_FAULT".equals(category) || "CHARGER_PROBLEM".equals(category)
                || "ROUTE_PROBLEM".equals(category) || "TRIP_ASSISTANCE".equals(category)
                || "OTHER".equals(category)) || !("ROUTINE".equals(priority) || "URGENT".equals(priority))
                || title.isBlank() || title.length() > 120 || description.isBlank()
                || description.length() > 1000)
            throw new IllegalArgumentException("Enter a valid issue category, priority, title and description");
        String caseId = id("CASE-");
        JSONObject body = new JSONObject().put("case_id", caseId)
                .put("idempotency_key", id("report-"))
                .put("category", category).put("priority", priority)
                .put("title", title.trim()).put("description", description.trim());
        return put(origin, me, caseId, "CREATE", "/api/v1/driver/service-cases", body);
    }

    JSONObject followUp(String origin, JSONObject me, String caseId, String text) throws JSONException {
        if (text.isBlank() || text.length() > 1000)
            throw new IllegalArgumentException("Enter a follow-up of at most 1000 characters");
        JSONObject body = new JSONObject().put("message_id", id("MSG-"))
                .put("idempotency_key", id("update-"))
                .put("body", text.trim());
        return put(origin, me, caseId, "MESSAGE",
                "/api/v1/driver/service-cases/" + caseId + "/messages", body);
    }

    private JSONObject put(String origin, JSONObject me, String caseId, String kind,
                           String path, JSONObject body) throws JSONException {
        synchronized (ServiceCaseCommandQueue.class) {
            if (origin.isBlank() || me.optString("tenant_id").isBlank()
                    || me.optString("driver_id").isBlank() || me.optString("vehicle_id").isBlank()
                    || !caseId.matches("[A-Za-z0-9_-]{1,64}"))
                throw new IllegalArgumentException("A connected driver and valid case are required");
            JSONArray all = items();
            if (all.length() >= LIMIT)
                throw new IllegalStateException("Service case queue is full; sync existing reports first");
            JSONObject command = new JSONObject().put("origin", origin)
                    .put("tenant_id", me.getString("tenant_id"))
                    .put("driver_id", me.getString("driver_id"))
                    .put("vehicle_id", me.getString("vehicle_id"))
                    .put("case_id", caseId).put("kind", kind)
                    .put("path", path).put("body", body)
                    .put("command_key", body.getString("idempotency_key"))
                    .put("state", "QUEUED");
            all.put(command); save(all); return command;
        }
    }

    JSONArray forOwner(String origin, JSONObject me) throws JSONException {
        synchronized (ServiceCaseCommandQueue.class) {
            JSONArray result = new JSONArray(), all = items();
            for (int index = 0; index < all.length(); index++) {
                JSONObject command = all.getJSONObject(index);
                if (owns(command, origin, me)) result.put(command);
            }
            return result;
        }
    }

    void remove(String origin, JSONObject me, String commandKey) throws JSONException {
        synchronized (ServiceCaseCommandQueue.class) {
            JSONArray all = items(), remaining = new JSONArray();
            for (int index = 0; index < all.length(); index++) {
                JSONObject command = all.getJSONObject(index);
                if (!(owns(command, origin, me) && commandKey.equals(command.optString("command_key"))))
                    remaining.put(command);
            }
            if (remaining.length() != all.length()) save(remaining);
        }
    }

    void needsReview(String origin, JSONObject me, String commandKey, int status) throws JSONException {
        changeState(origin, me, commandKey, "NEEDS_REVIEW", status);
    }

    void retry(String origin, JSONObject me, String commandKey) throws JSONException {
        changeState(origin, me, commandKey, "QUEUED", 0);
    }

    private void changeState(String origin, JSONObject me, String commandKey, String state,
                             int status) throws JSONException {
        synchronized (ServiceCaseCommandQueue.class) {
            JSONArray all = items();
            for (int index = 0; index < all.length(); index++) {
                JSONObject command = all.getJSONObject(index);
                if (owns(command, origin, me) && commandKey.equals(command.optString("command_key"))) {
                    command.put("state", state);
                    if (status == 0) command.remove("http_status");
                    else command.put("http_status", status);
                    save(all); return;
                }
            }
        }
    }
}
