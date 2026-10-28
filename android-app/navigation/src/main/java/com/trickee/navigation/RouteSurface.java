package com.trickee.navigation;

import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.Rect;
import android.view.Surface;

import androidx.annotation.NonNull;
import androidx.car.app.SurfaceCallback;
import androidx.car.app.SurfaceContainer;

import java.util.List;

/** Route geometry only; no invented street map or unlicensed tiles. */
final class RouteSurface implements SurfaceCallback {
    private SurfaceContainer container;
    private Rect visible;
    private NavigationRoute route;
    private NavigationRoute.Point current;
    private boolean dark = true;

    void setRoute(NavigationRoute route, NavigationRoute.Point current, boolean dark) {
        this.route = route;
        this.current = current;
        this.dark = dark;
        draw();
    }

    @Override public void onSurfaceAvailable(@NonNull SurfaceContainer surfaceContainer) {
        container = surfaceContainer;
        visible = new Rect(0, 0, surfaceContainer.getWidth(), surfaceContainer.getHeight());
        draw();
    }

    @Override public void onVisibleAreaChanged(@NonNull Rect area) {
        visible = new Rect(area);
        draw();
    }

    @Override public void onSurfaceDestroyed(@NonNull SurfaceContainer surfaceContainer) {
        container = null;
        visible = null;
    }

    private void draw() {
        if (container == null) return;
        Surface surface = container.getSurface();
        if (surface == null || !surface.isValid()) return;
        Canvas canvas;
        try {
            canvas = surface.lockCanvas(null);
        } catch (RuntimeException unavailable) {
            return;
        }
        try {
            render(canvas);
        } finally {
            surface.unlockCanvasAndPost(canvas);
        }
    }

    private void render(Canvas canvas) {
        int background = dark ? Color.rgb(18, 20, 23) : Color.rgb(246, 247, 248);
        canvas.drawColor(background);
        if (route == null) return;
        List<NavigationRoute.Point> shape = route.shape;
        if (shape.size() < 2) return;
        Rect area = visible == null ? new Rect(0, 0, canvas.getWidth(), canvas.getHeight()) : visible;
        double south = Double.POSITIVE_INFINITY, north = Double.NEGATIVE_INFINITY;
        double west = Double.POSITIVE_INFINITY, east = Double.NEGATIVE_INFINITY;
        for (NavigationRoute.Point point : shape) {
            south = Math.min(south, point.latitude);
            north = Math.max(north, point.latitude);
            west = Math.min(west, point.longitude);
            east = Math.max(east, point.longitude);
        }
        double cosine = Math.max(.01, Math.cos(Math.toRadians((south + north) / 2)));
        double routeWidth = Math.max(0.00001, (east - west) * cosine);
        double routeHeight = Math.max(0.00001, north - south);
        float margin = Math.max(24, Math.min(area.width(), area.height()) * .12f);
        float usableWidth = Math.max(1, area.width() - 2 * margin);
        float usableHeight = Math.max(1, area.height() - 2 * margin);
        double scale = Math.min(usableWidth / routeWidth, usableHeight / routeHeight);
        float centerX = area.centerX();
        float centerY = area.centerY();
        double centerLon = (east + west) / 2;
        double centerLat = (north + south) / 2;
        Path path = new Path();
        for (int index = 0; index < shape.size(); index++) {
            NavigationRoute.Point point = shape.get(index);
            float x = centerX + (float) ((point.longitude - centerLon) * cosine * scale);
            float y = centerY - (float) ((point.latitude - centerLat) * scale);
            if (index == 0) path.moveTo(x, y);
            else path.lineTo(x, y);
        }
        Paint routePaint = new Paint(Paint.ANTI_ALIAS_FLAG);
        routePaint.setStyle(Paint.Style.STROKE);
        routePaint.setStrokeCap(Paint.Cap.ROUND);
        routePaint.setStrokeJoin(Paint.Join.ROUND);
        routePaint.setStrokeWidth(Math.max(5, container.getDpi() / 160f * 5));
        routePaint.setColor(dark ? Color.rgb(52, 209, 229) : Color.rgb(0, 111, 139));
        canvas.drawPath(path, routePaint);
        Paint endpoint = new Paint(Paint.ANTI_ALIAS_FLAG);
        endpoint.setColor(dark ? Color.rgb(245, 198, 62) : Color.rgb(123, 88, 0));
        NavigationRoute.Point end = shape.get(shape.size() - 1);
        canvas.drawCircle(centerX + (float) ((end.longitude - centerLon) * cosine * scale),
                centerY - (float) ((end.latitude - centerLat) * scale), 10, endpoint);
        if (current != null && NavigationRoute.valid(current.latitude, current.longitude)) {
            endpoint.setColor(dark ? Color.WHITE : Color.BLACK);
            canvas.drawCircle(centerX + (float) ((current.longitude - centerLon) * cosine * scale),
                    centerY - (float) ((current.latitude - centerLat) * scale), 9, endpoint);
        }
    }
}
