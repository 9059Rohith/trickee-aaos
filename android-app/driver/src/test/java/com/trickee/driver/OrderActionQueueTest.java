package com.trickee.driver;

import static org.junit.Assert.*;
import android.content.SharedPreferences;
import java.lang.reflect.Proxy;
import java.util.HashMap;
import java.util.Map;
import org.json.JSONObject;
import org.junit.Test;

public final class OrderActionQueueTest {
    private static JSONObject owner(String driver) throws Exception {
        return new JSONObject().put("tenant_id", "fleet").put("driver_id", driver)
                .put("vehicle_id", "car");
    }

    private static SharedPreferences memoryPreferences(boolean rejectCommit) {
        Map<String, String> data = new HashMap<>();
        return (SharedPreferences) Proxy.newProxyInstance(SharedPreferences.class.getClassLoader(),
                new Class<?>[]{SharedPreferences.class}, (proxy, method, args) -> {
                    if ("getString".equals(method.getName()))
                        return data.getOrDefault((String) args[0], (String) args[1]);
                    if ("edit".equals(method.getName())) {
                        Map<String, String> pending = new HashMap<>();
                        return Proxy.newProxyInstance(SharedPreferences.Editor.class.getClassLoader(),
                                new Class<?>[]{SharedPreferences.Editor.class}, (editor, operation, values) -> {
                                    if ("putString".equals(operation.getName())) {
                                        pending.put((String) values[0], (String) values[1]); return editor;
                                    }
                                    if ("commit".equals(operation.getName())) {
                                        if (!rejectCommit) data.putAll(pending);
                                        return !rejectCommit;
                                    }
                                    throw new UnsupportedOperationException(operation.getName());
                                });
                    }
                    throw new UnsupportedOperationException(method.getName());
                });
    }

    @Test public void ownerScopeRetryAndOrderedActions() throws Exception {
        OrderActionQueue queue = new OrderActionQueue(memoryPreferences(false));
        JSONObject north = owner("north"), south = owner("south");
        JSONObject first = queue.put("https://api.example", north, "ORDER-1", OrderActionQueue.PICKUP);
        assertEquals(first.getString("idempotency_key"), queue.put("https://api.example", north,
                "ORDER-1", OrderActionQueue.PICKUP).getString("idempotency_key"));
        assertEquals(0, queue.forOwner("https://api.example", south).length());
        assertEquals(0, queue.forOwner("https://other.example", north).length());
        try {
            queue.put("https://api.example", north, "ORDER-1", OrderActionQueue.REPORT_DELIVERY);
            fail("delivery must wait for pickup confirmation");
        } catch (IllegalStateException expected) { /* First action preserved. */ }
        queue.needsReview("https://api.example", north, "ORDER-1", 409);
        assertEquals("NEEDS_REVIEW", queue.find("https://api.example", north,
                "ORDER-1").getString("state"));
        queue.retry("https://api.example", north, "ORDER-1");
        assertEquals("QUEUED", queue.find("https://api.example", north,
                "ORDER-1").getString("state"));
        queue.remove("https://api.example", north, "ORDER-1");
        assertEquals(OrderActionQueue.REPORT_DELIVERY, queue.put("https://api.example", north,
                "ORDER-1", OrderActionQueue.REPORT_DELIVERY).getString("action"));
    }

    @Test public void failedStorageAndCapacityAreExplicit() throws Exception {
        JSONObject owner = owner("north");
        OrderActionQueue failed = new OrderActionQueue(memoryPreferences(true));
        try {
            failed.put("https://api.example", owner, "ORDER-1", OrderActionQueue.PICKUP);
            fail("transmission must not begin without durable queue storage");
        } catch (IllegalStateException expected) { /* No command saved. */ }
        assertEquals(0, failed.forOwner("https://api.example", owner).length());
        OrderActionQueue queue = new OrderActionQueue(memoryPreferences(false));
        for (int i = 0; i < 100; i++)
            queue.put("https://api.example", owner, "ORDER-" + i, OrderActionQueue.PICKUP);
        try {
            queue.put("https://api.example", owner, "OVERFLOW", OrderActionQueue.PICKUP);
            fail("queue must remain bounded");
        } catch (IllegalStateException expected) { /* Capacity is explicit. */ }
    }
}
