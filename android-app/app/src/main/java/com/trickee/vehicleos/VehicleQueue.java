package com.trickee.vehicleos;

import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.database.sqlite.SQLiteOpenHelper;
import org.json.JSONException;
import org.json.JSONArray;
import org.json.JSONObject;
import java.util.Set;

final class VehicleQueue extends SQLiteOpenHelper {
    VehicleQueue(Context context) { super(context, "trickee-vehicle-outbox.db", null, 1); }
    @Override public void onCreate(SQLiteDatabase db) {
        db.execSQL("CREATE TABLE frames (sequence INTEGER PRIMARY KEY, payload TEXT NOT NULL)");
        db.execSQL("CREATE TABLE sequence_counter (id INTEGER PRIMARY KEY CHECK(id=1), next_value INTEGER NOT NULL)");
        db.execSQL("INSERT INTO sequence_counter(id,next_value) VALUES(1,1)");
    }
    @Override public void onUpgrade(SQLiteDatabase db, int oldVersion, int newVersion) { throw new IllegalStateException("Migration required"); }

    synchronized long enqueue(JSONObject payload) throws JSONException {
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            try (Cursor count = db.rawQuery("SELECT COUNT(*) FROM frames", null)) {
                count.moveToFirst();
                if (count.getInt(0) >= 100000) throw new IllegalStateException("outbox full; telemetry paused");
            }
            long next;
            try (Cursor cursor = db.rawQuery("SELECT next_value FROM sequence_counter WHERE id=1", null)) {
                cursor.moveToFirst(); next = cursor.getLong(0);
            }
            payload.put("sequence", next);
            ContentValues values = new ContentValues();
            values.put("sequence", next); values.put("payload", payload.toString());
            db.insertOrThrow("frames", null, values);
            db.execSQL("UPDATE sequence_counter SET next_value=next_value+1 WHERE id=1");
            db.setTransactionSuccessful();
            return next;
        } finally { db.endTransaction(); }
    }

    synchronized JSONArray oldestBatch(int limit) throws JSONException {
        JSONArray frames = new JSONArray();
        try (Cursor cursor = getReadableDatabase().rawQuery("SELECT payload FROM frames ORDER BY sequence LIMIT ?", new String[] {Integer.toString(limit)})) {
            while (cursor.moveToNext()) frames.put(new JSONObject(cursor.getString(0)));
        }
        return frames;
    }
    synchronized void acknowledge(Set<Long> sequences) {
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            for (long sequence : sequences) db.delete("frames", "sequence=?", new String[] {Long.toString(sequence)});
            db.setTransactionSuccessful();
        } finally { db.endTransaction(); }
    }
    synchronized int pending() {
        try (Cursor cursor = getReadableDatabase().rawQuery("SELECT COUNT(*) FROM frames", null)) {
            cursor.moveToFirst(); return cursor.getInt(0);
        }
    }
}
