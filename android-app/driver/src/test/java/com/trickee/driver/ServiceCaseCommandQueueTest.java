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
import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;

public final class ServiceCaseCommandQueueTest {
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

    @Test public void storesScopedReportAndFollowUpBeforeTransmission() throws Exception {
        ServiceCaseCommandQueue queue = new ServiceCaseCommandQueue(memoryPreferences(false));
        JSONObject driverA = owner("a"), driverB = owner("b");
        JSONObject report = queue.create("https://api.example", driverA, "CHARGER_PROBLEM", "URGENT",
                "Broken connector", "It will not latch");
        String caseId = report.getString("case_id");
        assertEquals("CREATE", report.getString("kind"));
        assertEquals("/api/v1/driver/service-cases", report.getString("path"));
        assertEquals(caseId, report.getJSONObject("body").getString("case_id"));
        assertEquals(0, queue.forOwner("https://api.example", driverB).length());
        assertEquals(0, queue.forOwner("https://other.example", driverA).length());
        JSONObject follow = queue.followUp("https://api.example", driverA, caseId, "A second charger worked");
        assertEquals("MESSAGE", follow.getString("kind"));
        assertTrue(follow.getString("path").endsWith("/" + caseId + "/messages"));
        JSONArray pending = queue.forOwner("https://api.example", driverA);
        assertEquals(2, pending.length());
        assertEquals("CREATE", pending.getJSONObject(0).getString("kind"));
        assertEquals("MESSAGE", pending.getJSONObject(1).getString("kind"));
        queue.remove("https://api.example", driverB, report.getString("command_key"));
        assertEquals(2, queue.forOwner("https://api.example", driverA).length());
        queue.remove("https://api.example", driverA, report.getString("command_key"));
        assertEquals(1, queue.forOwner("https://api.example", driverA).length());
    }

    @Test public void rejectedCommandCanBeReviewedRetriedAndCleared() throws Exception {
        ServiceCaseCommandQueue queue = new ServiceCaseCommandQueue(memoryPreferences(false));
        JSONObject driver = owner("a");
        JSONObject command = queue.create("https://api.example", driver, "VEHICLE_FAULT", "ROUTINE",
                "Warning light", "The warning light turned on");
        String key = command.getString("command_key");
        queue.needsReview("https://api.example", driver, key, 409);
        assertEquals("NEEDS_REVIEW", queue.forOwner("https://api.example", driver)
                .getJSONObject(0).getString("state"));
        queue.retry("https://api.example", driver, key);
        JSONObject retried = queue.forOwner("https://api.example", driver).getJSONObject(0);
        assertEquals("QUEUED", retried.getString("state"));
        assertFalse(retried.has("http_status"));
        assertEquals(key, retried.getJSONObject("body").getString("idempotency_key"));
        queue.remove("https://api.example", driver, key);
        assertEquals(0, queue.forOwner("https://api.example", driver).length());
    }

    @Test public void invalidContentAndFailedCommitCannotBePresentedAsQueued() throws Exception {
        ServiceCaseCommandQueue queue = new ServiceCaseCommandQueue(memoryPreferences(false));
        try {
            queue.create("https://api.example", owner("a"), "UNKNOWN", "ROUTINE", "Title", "Details");
            fail("invalid issue type must be rejected");
        } catch (IllegalArgumentException expected) { /* rejected */ }
        try {
            queue.followUp("https://api.example", owner("a"), "CASE-1", "  ");
            fail("empty update must be rejected");
        } catch (IllegalArgumentException expected) { /* rejected */ }
        ServiceCaseCommandQueue failed = new ServiceCaseCommandQueue(memoryPreferences(true));
        try {
            failed.create("https://api.example", owner("a"), "OTHER", "URGENT", "Need help", "Details");
            fail("failed persistence must stop transmission");
        } catch (IllegalStateException expected) { /* rejected */ }
        assertEquals(0, failed.forOwner("https://api.example", owner("a")).length());
    }

    @Test public void parallelInstancesDoNotLoseReports() throws Exception {
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
                        new ServiceCaseCommandQueue(storage).create("https://api.example", driver,
                                "OTHER", "ROUTINE", "Issue " + id, "Details " + id);
                    } catch (Exception failure) { throw new RuntimeException(failure); }
                });
            }
            start.countDown();
            for (Future<?> future : futures) future.get();
            assertEquals(32, new ServiceCaseCommandQueue(storage)
                    .forOwner("https://api.example", driver).length());
        } finally { workers.shutdownNow(); }
    }
}
