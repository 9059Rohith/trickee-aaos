package com.trickee.driver;

import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.database.sqlite.SQLiteOpenHelper;
import android.location.Location;
import android.os.Build;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/** Durable, sequence-stable phone GPS queue. Vehicle telemetry is never written here. */
final class GpsQueue extends SQLiteOpenHelper {
    private static final int MAX_POINTS = 5000;

    GpsQueue(Context context) { super(context, "driver_gps_queue.db", null, 1); }

    @Override public void onCreate(SQLiteDatabase db) {
        db.execSQL("CREATE TABLE sequences(trip_id TEXT PRIMARY KEY, vehicle_id TEXT NOT NULL, driver_id TEXT NOT NULL, next_sequence INTEGER NOT NULL)");
        db.execSQL("CREATE TABLE points(vehicle_id TEXT NOT NULL, driver_id TEXT NOT NULL, trip_id TEXT NOT NULL, sequence INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(trip_id,sequence))");
    }

    @Override public void onUpgrade(SQLiteDatabase db, int oldVersion, int newVersion) {
        throw new IllegalStateException("GPS queue migration is required; never drop queued points");
    }

    synchronized int pendingCount() {
        try (Cursor cursor = getReadableDatabase().rawQuery("SELECT COUNT(*) FROM points", null)) {
            cursor.moveToFirst(); return cursor.getInt(0);
        }
    }

    synchronized int pendingCountFor(String vehicleId, String driverId) {
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT COUNT(*) FROM points WHERE vehicle_id=? AND driver_id=?",
                new String[] {vehicleId, driverId})) {
            cursor.moveToFirst(); return cursor.getInt(0);
        }
    }

    synchronized long append(String vehicleId, String driverId, String tripId, Location location,
                             JSONObject imu) throws JSONException {
        if (tripId == null || !tripId.matches("[A-Za-z0-9_-]{1,64}")) throw new IllegalArgumentException("invalid trip ID");
        if (vehicleId == null || vehicleId.isBlank() || driverId == null || driverId.isBlank())
            throw new IllegalArgumentException("missing driver assignment");
        if (!Double.isFinite(location.getLatitude()) || !Double.isFinite(location.getLongitude())
                || Math.abs(location.getLatitude()) > 90 || Math.abs(location.getLongitude()) > 180)
            throw new IllegalArgumentException("invalid location");
        long now = System.currentTimeMillis();
        if (Math.abs(now - location.getTime()) > 120_000) throw new IllegalArgumentException("stale location");
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            try (Cursor count = db.rawQuery("SELECT COUNT(*) FROM points", null)) {
                count.moveToFirst();
                if (count.getInt(0) >= MAX_POINTS) throw new IllegalStateException("GPS queue full; sync before continuing");
            }
            long sequence = 1;
            try (Cursor current = db.rawQuery("SELECT vehicle_id,driver_id,next_sequence FROM sequences WHERE trip_id=?", new String[] {tripId})) {
                if (current.moveToFirst()) {
                    if (!vehicleId.equals(current.getString(0)) || !driverId.equals(current.getString(1)))
                        throw new IllegalStateException("trip belongs to another driver assignment");
                    sequence = current.getLong(2);
                }
            }
            JSONObject point = new JSONObject();
            point.put("sequence", sequence);
            point.put("timestamp", Instant.ofEpochMilli(location.getTime()).toString());
            point.put("lat", location.getLatitude()); point.put("lng", location.getLongitude());
            if (location.hasAccuracy() && location.getAccuracy() >= 0 && location.getAccuracy() <= 1000)
                point.put("accuracy", location.getAccuracy());
            if (location.hasSpeed() && location.getSpeed() >= 0 && location.getSpeed() <= 100)
                point.put("speed", location.getSpeed());
            if (location.hasBearing()) point.put("heading", location.getBearing() % 360);
            if (location.hasAltitude() && location.getAltitude() >= -500 && location.getAltitude() <= 10000)
                point.put("altitude", location.getAltitude());
            point.put("mocked", Build.VERSION.SDK_INT >= 31 ? location.isMock() : location.isFromMockProvider());
            point.put("provider", location.getProvider() == null ? "unknown" : location.getProvider());
            point.put("app_version", "native-0.1.0");
            point.put("device_model", Build.MODEL == null ? "unknown" : Build.MODEL.substring(0, Math.min(80, Build.MODEL.length())));
            if (imu != null) point.put("imu", imu);
            ContentValues values = new ContentValues();
            values.put("vehicle_id", vehicleId); values.put("driver_id", driverId);
            values.put("trip_id", tripId); values.put("sequence", sequence); values.put("payload", point.toString());
            if (db.insertOrThrow("points", null, values) < 0) throw new IllegalStateException("queue write failed");
            ContentValues next = new ContentValues(); next.put("trip_id", tripId);
            next.put("vehicle_id", vehicleId); next.put("driver_id", driverId);
            next.put("next_sequence", sequence + 1);
            db.insertWithOnConflict("sequences", null, next, SQLiteDatabase.CONFLICT_REPLACE);
            db.setTransactionSuccessful();
            return sequence;
        } finally { db.endTransaction(); }
    }

    synchronized List<String> tripIds() {
        List<String> result = new ArrayList<>();
        try (Cursor cursor = getReadableDatabase().rawQuery("SELECT DISTINCT trip_id FROM points ORDER BY trip_id", null)) {
            while (cursor.moveToNext()) result.add(cursor.getString(0));
        }
        return result;
    }

    synchronized Batch firstBatch(String tripId) throws JSONException {
        List<Long> sequences = new ArrayList<>();
        JSONArray points = new JSONArray();
        String vehicleId = null, driverId = null;
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT vehicle_id,driver_id,sequence,payload FROM points WHERE trip_id=? ORDER BY sequence LIMIT 100", new String[] {tripId})) {
            while (cursor.moveToNext()) {
                if (vehicleId == null) { vehicleId = cursor.getString(0); driverId = cursor.getString(1); }
                if (!vehicleId.equals(cursor.getString(0)) || !driverId.equals(cursor.getString(1)))
                    throw new IllegalStateException("mixed driver assignment in GPS queue");
                sequences.add(cursor.getLong(2));
                points.put(new JSONObject(cursor.getString(3)));
            }
        }
        if (sequences.isEmpty()) return null;
        String material = tripId + ":" + sequences.get(0) + ":" + sequences.get(sequences.size() - 1);
        String batchId = "B-" + UUID.nameUUIDFromBytes(material.getBytes(StandardCharsets.UTF_8)).toString().replace("-", "");
        return new Batch(vehicleId, driverId, tripId, batchId, points, sequences);
    }

    synchronized void acknowledge(Batch batch) {
        SQLiteDatabase db = getWritableDatabase(); db.beginTransaction();
        try {
            for (Long sequence : batch.sequences)
                db.delete("points", "trip_id=? AND sequence=?", new String[] {batch.tripId, sequence.toString()});
            db.setTransactionSuccessful();
        } finally { db.endTransaction(); }
    }

    /** Remove only this connected assignment's local phone data after cloud erasure succeeds. */
    synchronized int eraseForAssignment(String vehicleId, String driverId) {
        SQLiteDatabase db = getWritableDatabase(); db.beginTransaction();
        try {
            int deleted = db.delete("points", "vehicle_id=? AND driver_id=?",
                    new String[] {vehicleId, driverId});
            db.delete("sequences", "vehicle_id=? AND driver_id=?", new String[] {vehicleId, driverId});
            db.setTransactionSuccessful();
            return deleted;
        } finally { db.endTransaction(); }
    }

    static final class Batch {
        final String tripId;
        final String vehicleId;
        final String driverId;
        final String batchId;
        final JSONArray points;
        final List<Long> sequences;
        Batch(String vehicleId, String driverId, String tripId, String batchId, JSONArray points, List<Long> sequences) {
            this.vehicleId = vehicleId; this.driverId = driverId; this.tripId = tripId;
            this.batchId = batchId; this.points = points; this.sequences = sequences;
        }
        JSONObject body() throws JSONException {
            JSONObject body = new JSONObject(); body.put("batch_id", batchId); body.put("points", points); return body;
        }
    }
}
