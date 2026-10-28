package com.trickee.driver;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.hardware.SensorManager;
import android.location.Location;
import android.location.LocationListener;
import android.location.LocationManager;
import android.os.Build;
import android.os.IBinder;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import org.json.JSONObject;

/** Explicit active-trip location collection with a persistent notification. */
public final class GpsTrackingService extends Service implements LocationListener {
    static final String ACTION_START = "com.trickee.driver.START_GPS";
    static final String ACTION_STOP = "com.trickee.driver.STOP_GPS";
    static final String EXTRA_TRIP = "trip_id";
    static final String EXTRA_DRIVER = "driver_id";
    static final String EXTRA_VEHICLE = "vehicle_id";
    static final String EXTRA_TENANT = "tenant_id";
    private static final String CHANNEL = "driver_trip_location";
    private LocationManager locationManager;
    private PhoneImuAccumulator imu;
    private final ExecutorService queueExecutor = Executors.newSingleThreadExecutor();
    private String tripId, driverId, vehicleId, tenantId;
    private long lastSchedule;

    @Override public void onCreate() {
        super.onCreate();
        NotificationChannel channel = new NotificationChannel(CHANNEL, "Trip GPS", NotificationManager.IMPORTANCE_LOW);
        channel.setDescription("Location collection during an active Trickee driver trip");
        getSystemService(NotificationManager.class).createNotificationChannel(channel);
        locationManager = getSystemService(LocationManager.class);
        imu = new PhoneImuAccumulator(getSystemService(SensorManager.class));
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null || ACTION_STOP.equals(intent.getAction())) { stopSelf(); return START_NOT_STICKY; }
        if (!ACTION_START.equals(intent.getAction())) { stopSelf(); return START_NOT_STICKY; }
        tripId = intent.getStringExtra(EXTRA_TRIP);
        driverId = intent.getStringExtra(EXTRA_DRIVER);
        vehicleId = intent.getStringExtra(EXTRA_VEHICLE);
        tenantId = intent.getStringExtra(EXTRA_TENANT);
        if (tripId == null || driverId == null || vehicleId == null || tenantId == null
                || !PhoneTripConsent.isGranted(this, tenantId, driverId)
                || checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) != PackageManager.PERMISSION_GRANTED
                && checkSelfPermission(Manifest.permission.ACCESS_COARSE_LOCATION) != PackageManager.PERMISSION_GRANTED) {
            stopSelf(); return START_NOT_STICKY;
        }
        imu.stop();
        imu = new PhoneImuAccumulator(getSystemService(SensorManager.class));
        try {
            Notification notification = notification("Collecting phone GPS and motion for trip " + tripId);
            if (Build.VERSION.SDK_INT >= 29)
                startForeground(42, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION);
            else startForeground(42, notification);
            boolean registered = false;
            if (locationManager.isProviderEnabled(LocationManager.GPS_PROVIDER)) {
                locationManager.requestLocationUpdates(LocationManager.GPS_PROVIDER, 1000, 3, this); registered = true;
            }
            if (locationManager.isProviderEnabled(LocationManager.NETWORK_PROVIDER)) {
                locationManager.requestLocationUpdates(LocationManager.NETWORK_PROVIDER, 5000, 10, this); registered = true;
            }
            if (!registered) updateNotification("Location is off. Enable it to collect phone GPS.");
            imu.start();
            return START_NOT_STICKY;
        } catch (SecurityException | IllegalArgumentException exception) {
            stopSelf(); return START_NOT_STICKY;
        }
    }

    @Override public void onLocationChanged(Location location) {
        if (tripId == null) return;
        if (!PhoneTripConsent.isGranted(this, tenantId, driverId)) {
            stopSelf();
            return;
        }
        final String currentTrip = tripId, currentDriver = driverId, currentVehicle = vehicleId,
                currentTenant = tenantId;
        final JSONObject sensorWindow = imu.snapshot();
        queueExecutor.execute(() -> {
            if (!PhoneTripConsent.isGranted(this, currentTenant, currentDriver)) return;
            try (GpsQueue queue = new GpsQueue(this)) {
                queue.append(currentVehicle, currentDriver, currentTrip, location, sensorWindow);
                long now = System.currentTimeMillis();
                if (now - lastSchedule >= 20_000) { lastSchedule = now; GpsUploadWorker.schedule(this); }
            } catch (Exception exception) {
                updateNotification("GPS queue stopped: " + exception.getMessage());
                locationManager.removeUpdates(this);
            }
        });
    }

    private Notification notification(String message) {
        Intent open = new Intent(this, MainActivity.class);
        PendingIntent action = PendingIntent.getActivity(this, 0, open, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        return new Notification.Builder(this, CHANNEL).setSmallIcon(android.R.drawable.ic_menu_mylocation)
                .setContentTitle("Trickee trip tracking").setContentText(message).setContentIntent(action)
                .setOngoing(true).build();
    }

    private void updateNotification(String message) {
        getSystemService(NotificationManager.class).notify(42, notification(message));
    }

    @Override public void onDestroy() {
        locationManager.removeUpdates(this);
        imu.stop();
        // Keep the final upload request behind every queued location write.
        queueExecutor.execute(() -> GpsUploadWorker.schedule(getApplicationContext()));
        queueExecutor.shutdown();
        super.onDestroy();
    }

    @Override public IBinder onBind(Intent intent) { return null; }
}
