package com.trickee.driver;

import android.content.Context;
import android.util.Log;
import androidx.annotation.NonNull;
import androidx.work.BackoffPolicy;
import androidx.work.Constraints;
import androidx.work.ExistingPeriodicWorkPolicy;
import androidx.work.ExistingWorkPolicy;
import androidx.work.NetworkType;
import androidx.work.OneTimeWorkRequest;
import androidx.work.PeriodicWorkRequest;
import androidx.work.Worker;
import androidx.work.WorkerParameters;
import androidx.work.WorkManager;
import java.io.IOException;
import java.util.concurrent.TimeUnit;
import org.json.JSONObject;

/** Uploads exact queued phone points. Failure never deletes a queued point. */
public final class GpsUploadWorker extends Worker {
    private static final String TAG = "TrickeeGpsUpload";
    private static final String ONCE = "driver-gps-upload";
    private static final String PERIODIC = "driver-gps-recovery";

    public GpsUploadWorker(@NonNull Context context, @NonNull WorkerParameters parameters) {
        super(context, parameters);
    }

    static void schedule(Context context) {
        WorkManager.getInstance(context).enqueueUniqueWork(ONCE, ExistingWorkPolicy.KEEP, request());
    }

    /** A deliberate user retry bypasses an older automatic job's exponential backoff. */
    static void retryNow(Context context) {
        WorkManager.getInstance(context).enqueueUniqueWork(ONCE, ExistingWorkPolicy.REPLACE, request());
    }

    private static OneTimeWorkRequest request() {
        Constraints connected = new Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build();
        return new OneTimeWorkRequest.Builder(GpsUploadWorker.class)
                .setConstraints(connected).setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS).build();
    }

    static void scheduleRecovery(Context context) {
        Constraints connected = new Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build();
        PeriodicWorkRequest request = new PeriodicWorkRequest.Builder(GpsUploadWorker.class, 15, TimeUnit.MINUTES)
                .setConstraints(connected).build();
        WorkManager.getInstance(context).enqueueUniquePeriodicWork(PERIODIC, ExistingPeriodicWorkPolicy.KEEP, request);
    }

    @NonNull @Override public Result doWork() {
        try (GpsQueue queue = new GpsQueue(getApplicationContext())) {
            int pending = queue.pendingCount();
            if (pending == 0) {
                GpsSyncStatus.clear(getApplicationContext());
                return Result.success();
            }
            Log.i(TAG, "Upload starting with " + pending + " queued point(s)");
            DriverApi api = DriverApi.saved(getApplicationContext());
            JSONObject identity = (JSONObject) api.call("GET", "/api/v1/driver/me", null);
            String tenantId = identity.optString("tenant_id"), driverId = identity.optString("driver_id");
            if (PhoneTripConsent.isPending(getApplicationContext(), tenantId, driverId)
                    || !PhoneTripConsent.isGranted(getApplicationContext(), tenantId, driverId)) {
                GpsSyncStatus.consentWithdrawn(getApplicationContext());
                return Result.failure();
            }
            JSONObject consent = (JSONObject) api.call("GET", "/api/v1/driver/consent/phone-trip", null);
            if (!consent.optBoolean("granted")) {
                PhoneTripConsent.record(getApplicationContext(), tenantId, driverId, false);
                GpsSyncStatus.consentWithdrawn(getApplicationContext());
                return Result.failure();
            }
            boolean differentAssignment = false;
            for (String tripId : queue.tripIds()) {
                GpsQueue.Batch batch;
                while ((batch = queue.firstBatch(tripId)) != null) {
                    if (!batch.vehicleId.equals(identity.optString("vehicle_id"))
                            || !batch.driverId.equals(identity.optString("driver_id"))) {
                        Log.w(TAG, "Queued point belongs to a different driver assignment; preserving it");
                        differentAssignment = true;
                        break; // Preserve old-account points; allow this driver's other trips to sync.
                    }
                    api.call("POST", "/api/v1/driver/trips/" + tripId + "/gps-batches", batch.body());
                    queue.acknowledge(batch);
                    Log.i(TAG, "Acknowledged " + batch.sequences.size() + " point(s)");
                }
            }
            if (queue.pendingCountFor(identity.optString("vehicle_id"), identity.optString("driver_id")) > 0) {
                GpsSyncStatus.retrying(getApplicationContext());
                return Result.retry(); // A point inserted during this run must not wait for periodic recovery.
            }
            if (differentAssignment) {
                GpsSyncStatus.assignmentMismatch(getApplicationContext());
                return Result.failure();
            }
            GpsSyncStatus.clear(getApplicationContext());
            return Result.success();
        } catch (DriverApi.ApiFailure failure) {
            Log.w(TAG, "Server rejected GPS upload with HTTP " + failure.status);
            if (failure.status == 429 || failure.status >= 500) {
                GpsSyncStatus.retrying(getApplicationContext());
                return Result.retry();
            }
            GpsSyncStatus.rejected(getApplicationContext(), failure.status);
            return Result.failure(); // 401, 403, 404 and conflicts need a visible account/server fix.
        } catch (IOException exception) {
            Log.w(TAG, "GPS upload network unavailable: " + exception.getClass().getSimpleName());
            GpsSyncStatus.retrying(getApplicationContext());
            return Result.retry();
        } catch (Exception exception) {
            Log.e(TAG, "GPS upload failed: " + exception.getClass().getSimpleName());
            GpsSyncStatus.localFailure(getApplicationContext());
            return Result.failure();
        }
    }
}
