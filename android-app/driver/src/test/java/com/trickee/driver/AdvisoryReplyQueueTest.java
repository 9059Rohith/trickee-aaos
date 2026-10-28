package com.trickee.driver;

import static org.junit.Assert.*;
import android.content.SharedPreferences;
import java.lang.reflect.Proxy;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import org.json.JSONObject;
import org.junit.Test;

public final class AdvisoryReplyQueueTest {
    private static JSONObject owner(String driver) throws Exception {
        return new JSONObject().put("tenant_id", "fleet").put("driver_id", driver).put("vehicle_id", "car");
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

    @Test public void scopesAndRetainsStableIdempotencyKey() throws Exception {
        AdvisoryReplyQueue queue = new AdvisoryReplyQueue(memoryPreferences(false));
        JSONObject driverA = owner("a"), driverB = owner("b");
        JSONObject first = queue.put("https://api.example", driverA, "advisory-1", "ACKNOWLEDGED");
        assertEquals(first.getString("idempotency_key"), queue.put("https://api.example", driverA,
                "advisory-1", "ACKNOWLEDGED").getString("idempotency_key"));
        assertEquals(0, queue.forOwner("https://api.example", driverB).length());
        assertEquals(0, queue.forOwner("https://other.example", driverA).length());
        assertEquals(1, queue.forOwner("https://api.example", driverA).length());
        try {
            queue.put("https://api.example", driverA, "advisory-1", "DECLINED");
            fail("a different outcome must not overwrite the saved choice");
        } catch (IllegalStateException expected) { /* protected */ }
        queue.put("https://api.example", driverB, "advisory-1", "DECLINED");
        queue.remove("https://api.example", driverB, "advisory-1");
        assertEquals(1, queue.forOwner("https://api.example", driverA).length());
    }

    @Test public void reviewRetryAndCapacityDoNotDiscardChoices() throws Exception {
        AdvisoryReplyQueue queue = new AdvisoryReplyQueue(memoryPreferences(false));
        JSONObject driver = owner("a");
        queue.put("https://api.example", driver, "first", "DECLINED");
        queue.needsReview("https://api.example", driver, "first", 409);
        assertEquals("NEEDS_REVIEW", queue.find("https://api.example", driver, "first").getString("state"));
        assertEquals(409, queue.find("https://api.example", driver, "first").getInt("http_status"));
        queue.retry("https://api.example", driver, "first");
        assertEquals("QUEUED", queue.find("https://api.example", driver, "first").getString("state"));
        for (int index = 1; index < 100; index++)
            queue.put("https://api.example", driver, "n-" + index, "ACKNOWLEDGED");
        try {
            queue.put("https://api.example", driver, "overflow", "ACKNOWLEDGED");
            fail("the queue must remain bounded");
        } catch (IllegalStateException expected) { /* bounded */ }
        assertEquals(100, queue.forOwner("https://api.example", driver).length());
    }

    @Test public void failedPersistenceCannotAppearQueued() throws Exception {
        AdvisoryReplyQueue queue = new AdvisoryReplyQueue(memoryPreferences(true));
        try {
            queue.put("https://api.example", owner("a"), "first", "DECLINED");
            fail("a failed write must prevent transmission");
        } catch (IllegalStateException expected) { /* reported */ }
        assertEquals(0, queue.forOwner("https://api.example", owner("a")).length());
    }

    @Test public void separateInstancesSerializeReadModifyWrite() throws Exception {
        SharedPreferences storage = memoryPreferences(false);
        JSONObject driver = owner("a");
        ExecutorService workers = Executors.newFixedThreadPool(8);
        CountDownLatch start = new CountDownLatch(1);
        try {
            @SuppressWarnings("unchecked") Future<?>[] futures = new Future<?>[32];
            for (int index = 0; index < futures.length; index++) {
                final int id = index;
                futures[index] = workers.submit(() -> {
                    try {
                        start.await();
                        new AdvisoryReplyQueue(storage).put("https://api.example", driver,
                                "advisory-" + id, "ACKNOWLEDGED");
                    } catch (Exception failure) { throw new RuntimeException(failure); }
                });
            }
            start.countDown();
            for (Future<?> future : futures) future.get();
            assertEquals(32, new AdvisoryReplyQueue(storage).forOwner("https://api.example", driver).length());
        } finally { workers.shutdownNow(); }
    }
}
