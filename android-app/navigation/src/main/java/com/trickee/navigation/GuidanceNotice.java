package com.trickee.navigation;

import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;

import androidx.car.app.notification.CarAppExtender;
import androidx.car.app.notification.CarNotificationManager;
import androidx.core.app.NotificationChannelCompat;
import androidx.core.app.NotificationCompat;
import androidx.core.app.NotificationManagerCompat;

import java.util.Locale;

/** One quiet, current turn notice for the car rail while guidance is active. */
final class GuidanceNotice {
    static final int ID = 17001;
    private static final String CHANNEL = "trickee_navigation_guidance";

    private final Context context;
    private final CarNotificationManager manager;

    GuidanceNotice(Context context) {
        this.context = context;
        manager = CarNotificationManager.from(context);
        manager.createNotificationChannel(new NotificationChannelCompat.Builder(
                CHANNEL, NotificationManagerCompat.IMPORTANCE_LOW)
                .setName("Navigation guidance")
                .setDescription("Current turn and route progress")
                .build());
    }

    static String content(NavigationRoute route, NavigationProgress.State state) {
        if (state.status == NavigationProgress.Status.LOCATION_UNAVAILABLE) {
            return "Car location unavailable. Guidance paused.";
        }
        if (state.status != NavigationProgress.Status.ON_ROUTE) {
            return "Off route. Waiting for a safe road route.";
        }
        if (state.nextTurnIndex < 0 || state.nextTurnIndex >= route.turns.size()) {
            return "Continue to the destination.";
        }
        double metres = Math.max(0, state.distanceToTurnMetres);
        String distance = metres < 1000
                ? String.format(Locale.US, "%d m", Math.round(metres / 10) * 10)
                : String.format(Locale.US, "%.1f km", metres / 1000);
        return "In " + distance + ", " + route.turns.get(state.nextTurnIndex).instruction;
    }

    void update(NavigationRoute route, GeoDestination destination, NavigationProgress.State state) {
        if (Build.VERSION.SDK_INT >= 33 && context.checkSelfPermission(
                android.Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            cancel();
            return;
        }
        Intent launch = context.getPackageManager().getLaunchIntentForPackage(context.getPackageName());
        if (launch == null) return;
        launch.addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent open = PendingIntent.getActivity(context, ID, launch,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        String title = "Navigation to " + destination.label;
        manager.notify(ID, new NotificationCompat.Builder(context, CHANNEL)
                .setSmallIcon(R.drawable.trickee_logo)
                .setContentTitle(title)
                .setContentText(content(route, state))
                .setContentIntent(open)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setCategory(NotificationCompat.CATEGORY_NAVIGATION)
                .extend(new CarAppExtender.Builder()
                        .setContentTitle(title)
                        .setContentIntent(open)
                        .build()));
    }

    void cancel() {
        manager.cancel(ID);
    }
}
