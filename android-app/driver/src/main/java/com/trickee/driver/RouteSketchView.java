package com.trickee.driver;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Path;
import android.view.View;
import org.json.JSONArray;
import org.json.JSONObject;

/** North-up shape of a provider road route. No map tiles or live position are implied. */
final class RouteSketchView extends View {
    private static final int MAX_DRAW_POINTS = 1500;
    private final double[] east;
    private final double[] north;
    private final Path line = new Path();
    private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private float startX, startY, endX, endY;
    private final float density;

    private RouteSketchView(Context context, double[] east, double[] north) {
        super(context);
        this.east = east;
        this.north = north;
        density = getResources().getDisplayMetrics().density;
        setContentDescription("North-up road route shape preview, without map tiles or live position");
    }

    static RouteSketchView from(Context context, JSONObject geometry) {
        if (geometry == null || !"LineString".equals(geometry.optString("type"))) return null;
        JSONArray coordinates = geometry.optJSONArray("coordinates");
        if (coordinates == null || coordinates.length() < 2 || coordinates.length() > 20000) return null;
        int stride = Math.max(1, (coordinates.length() - 1 + MAX_DRAW_POINTS - 2) / (MAX_DRAW_POINTS - 1));
        int count = (coordinates.length() - 2) / stride + 2;
        double[] east = new double[count], north = new double[count];
        int at = 0;
        for (int index = 0; index < coordinates.length(); index += stride) {
            JSONArray point = coordinates.optJSONArray(index);
            if (point == null || point.length() < 2) return null;
            double longitude = point.optDouble(0, Double.NaN), latitude = point.optDouble(1, Double.NaN);
            if (!Double.isFinite(longitude) || !Double.isFinite(latitude)
                    || Math.abs(longitude) > 180 || Math.abs(latitude) > 90) return null;
            east[at] = longitude;
            north[at++] = latitude;
        }
        if ((coordinates.length() - 1) % stride != 0) {
            JSONArray point = coordinates.optJSONArray(coordinates.length() - 1);
            if (point == null || point.length() < 2) return null;
            double longitude = point.optDouble(0, Double.NaN), latitude = point.optDouble(1, Double.NaN);
            if (!Double.isFinite(longitude) || !Double.isFinite(latitude)
                    || Math.abs(longitude) > 180 || Math.abs(latitude) > 90) return null;
            east[at] = longitude;
            north[at++] = latitude;
        }
        // Trim only when the final point was already visited by the stride loop.
        if (at != east.length) {
            double[] trimmedEast = new double[at], trimmedNorth = new double[at];
            System.arraycopy(east, 0, trimmedEast, 0, at);
            System.arraycopy(north, 0, trimmedNorth, 0, at);
            east = trimmedEast;
            north = trimmedNorth;
        }
        return new RouteSketchView(context, east, north);
    }

    @Override protected void onSizeChanged(int width, int height, int oldWidth, int oldHeight) {
        super.onSizeChanged(width, height, oldWidth, oldHeight);
        line.reset();
        if (width == 0 || height == 0) return;
        double minLat = 90, maxLat = -90, minLng = 180, maxLng = -180;
        for (int index = 0; index < east.length; index++) {
            minLat = Math.min(minLat, north[index]); maxLat = Math.max(maxLat, north[index]);
            minLng = Math.min(minLng, east[index]); maxLng = Math.max(maxLng, east[index]);
        }
        double latitudeScale = Math.max(0.01, Math.cos(Math.toRadians((minLat + maxLat) / 2)));
        double horizontal = Math.max(1e-7, (maxLng - minLng) * latitudeScale);
        double vertical = Math.max(1e-7, maxLat - minLat);
        float pad = 27 * density, usableWidth = Math.max(1, width - 2 * pad), usableHeight = Math.max(1, height - 2 * pad);
        double scale = Math.min(usableWidth / horizontal, usableHeight / vertical);
        float left = (float) ((width - horizontal * scale) / 2);
        float top = (float) ((height - vertical * scale) / 2);
        for (int index = 0; index < east.length; index++) {
            float x = left + (float) ((east[index] - minLng) * latitudeScale * scale);
            float y = top + (float) ((maxLat - north[index]) * scale);
            if (index == 0) { line.moveTo(x, y); startX = x; startY = y; }
            else line.lineTo(x, y);
            if (index == east.length - 1) { endX = x; endY = y; }
        }
    }

    @Override protected void onDraw(Canvas canvas) {
        super.onDraw(canvas);
        canvas.drawColor(0xFF09181E);
        paint.setStyle(Paint.Style.STROKE);
        paint.setStrokeWidth(1 * density);
        paint.setColor(0xFF1C3B45);
        for (int index = 1; index < 4; index++) {
            float x = getWidth() * index / 4f, y = getHeight() * index / 4f;
            canvas.drawLine(x, 0, x, getHeight(), paint);
            canvas.drawLine(0, y, getWidth(), y, paint);
        }
        paint.setStrokeCap(Paint.Cap.ROUND);
        paint.setStrokeJoin(Paint.Join.ROUND);
        paint.setStrokeWidth(11 * density);
        paint.setColor(0x553AD4E7);
        canvas.drawPath(line, paint);
        paint.setStrokeWidth(4 * density);
        paint.setColor(0xFF3AD4E7);
        canvas.drawPath(line, paint);
        paint.setStyle(Paint.Style.FILL);
        paint.setColor(0xFFFFDD00);
        canvas.drawCircle(startX, startY, 7 * density, paint);
        paint.setColor(0xFFFFFFFF);
        canvas.drawCircle(endX, endY, 7 * density, paint);
    }
}
