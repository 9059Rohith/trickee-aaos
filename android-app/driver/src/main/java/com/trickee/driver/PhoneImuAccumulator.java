package com.trickee.driver;

import android.hardware.Sensor;
import android.hardware.SensorEvent;
import android.hardware.SensorEventListener;
import android.hardware.SensorManager;
import android.os.SystemClock;
import org.json.JSONException;
import org.json.JSONObject;

/** Bounded raw phone-sensor RMS windows. These values are never treated as vehicle signals or a driver score. */
final class PhoneImuAccumulator implements SensorEventListener {
    private final SensorManager manager;
    private final Sensor accelerometer;
    private final Sensor gyroscope;
    private long startedNs = SystemClock.elapsedRealtimeNanos();
    private int accelerometerSamples, gyroscopeSamples, accelerometerAccuracy, gyroscopeAccuracy;
    private double accelerationSquareSum, angularRateSquareSum;

    PhoneImuAccumulator(SensorManager manager) {
        this.manager = manager;
        accelerometer = manager.getDefaultSensor(Sensor.TYPE_ACCELEROMETER);
        gyroscope = manager.getDefaultSensor(Sensor.TYPE_GYROSCOPE);
    }

    void start() {
        if (accelerometer != null) manager.registerListener(this, accelerometer, SensorManager.SENSOR_DELAY_GAME);
        if (gyroscope != null) manager.registerListener(this, gyroscope, SensorManager.SENSOR_DELAY_GAME);
    }

    void stop() { manager.unregisterListener(this); }

    @Override public synchronized void onSensorChanged(SensorEvent event) {
        long now = SystemClock.elapsedRealtimeNanos();
        if (now - startedNs >= 60_000_000_000L) reset(now);
        if (event.values.length < 3) return;
        double x = event.values[0], y = event.values[1], z = event.values[2];
        double square = x * x + y * y + z * z;
        if (!Double.isFinite(square)) return;
        if (event.sensor.getType() == Sensor.TYPE_ACCELEROMETER && square <= 200 * 200
                && accelerometerSamples < 5000) {
            accelerationSquareSum += square;
            accelerometerSamples++;
            accelerometerAccuracy = Math.max(0, Math.min(3, event.accuracy));
        } else if (event.sensor.getType() == Sensor.TYPE_GYROSCOPE && square <= 100 * 100
                && gyroscopeSamples < 5000) {
            angularRateSquareSum += square;
            gyroscopeSamples++;
            gyroscopeAccuracy = Math.max(0, Math.min(3, event.accuracy));
        }
    }

    @Override public void onAccuracyChanged(Sensor sensor, int accuracy) { }

    synchronized JSONObject snapshot() {
        long now = SystemClock.elapsedRealtimeNanos();
        int durationMs = (int) Math.max(1, Math.min(60000, (now - startedNs) / 1_000_000));
        if (accelerometerSamples == 0 && gyroscopeSamples == 0) {
            reset(now); return null;
        }
        try {
            JSONObject data = new JSONObject();
            data.put("source", "PHONE_IMU"); data.put("window_ms", durationMs);
            data.put("accelerometer_present", accelerometer != null);
            data.put("gyroscope_present", gyroscope != null);
            data.put("accelerometer_samples", accelerometerSamples);
            data.put("gyroscope_samples", gyroscopeSamples);
            if (accelerometerSamples > 0)
                data.put("raw_acceleration_rms_mps2", Math.sqrt(accelerationSquareSum / accelerometerSamples));
            if (gyroscopeSamples > 0)
                data.put("angular_rate_rms_radps", Math.sqrt(angularRateSquareSum / gyroscopeSamples));
            data.put("accelerometer_accuracy", accelerometerAccuracy);
            data.put("gyroscope_accuracy", gyroscopeAccuracy);
            reset(now);
            return data;
        } catch (JSONException exception) {
            reset(now);
            return null;
        }
    }

    private void reset(long now) {
        startedNs = now;
        accelerometerSamples = gyroscopeSamples = 0;
        accelerationSquareSum = angularRateSquareSum = 0;
        accelerometerAccuracy = gyroscopeAccuracy = 0;
    }
}
