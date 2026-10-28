package com.trickee.navigation;

import android.Manifest;
import android.content.Intent;
import android.content.res.Configuration;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageManager;
import android.location.Location;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;

import androidx.annotation.NonNull;
import androidx.car.app.CarAppService;
import androidx.car.app.AppManager;
import androidx.car.app.Screen;
import androidx.car.app.Session;
import androidx.car.app.model.Action;
import androidx.car.app.model.ActionStrip;
import androidx.car.app.model.Distance;
import androidx.car.app.model.MessageTemplate;
import androidx.car.app.model.ParkedOnlyOnClickListener;
import androidx.car.app.model.Template;
import androidx.car.app.validation.HostValidator;
import androidx.car.app.navigation.NavigationManager;
import androidx.car.app.navigation.NavigationManagerCallback;
import androidx.car.app.navigation.model.Destination;
import androidx.car.app.navigation.model.MessageInfo;
import androidx.car.app.navigation.model.NavigationTemplate;
import androidx.car.app.navigation.model.RoutingInfo;
import androidx.car.app.navigation.model.Step;
import androidx.car.app.navigation.model.TravelEstimate;
import androidx.lifecycle.Lifecycle;
import androidx.lifecycle.LifecycleEventObserver;

import java.util.Locale;
import java.util.Arrays;
import java.time.ZonedDateTime;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Host-rendered entry point. It never advertises a route until provider guidance exists. */
public final class TrickeeNavigationService extends CarAppService {
    @NonNull @Override public HostValidator createHostValidator() {
        if ((getApplicationInfo().flags & ApplicationInfo.FLAG_DEBUGGABLE) != 0) {
            return HostValidator.ALLOW_ALL_HOSTS_VALIDATOR;
        }
        return new HostValidator.Builder(this)
                .addAllowedHosts(androidx.car.app.R.array.hosts_allowlist_sample)
                .build();
    }

    @NonNull @Override public Session onCreateSession() {
        return new NavigationSession();
    }

    private static final class NavigationSession extends Session {
        private DestinationScreen screen;

        @NonNull @Override public Screen onCreateScreen(@NonNull Intent intent) {
            screen = new DestinationScreen(getCarContext());
            screen.update(intent);
            return screen;
        }

        @Override public void onNewIntent(@NonNull Intent intent) {
            if (screen != null) {
                screen.update(intent);
            }
        }

        @Override public void onCarConfigurationChanged(@NonNull Configuration configuration) {
            super.onCarConfigurationChanged(configuration);
            if (screen != null) screen.onThemeChanged();
        }
    }

    private static final class DestinationScreen extends Screen {
        private final Handler main = new Handler(Looper.getMainLooper());
        private final ExecutorService routes = Executors.newSingleThreadExecutor();
        private final boolean debugBuild;
        private final NavigationRouteClient client;
        private final NavigationManager navigationManager;
        private final GuidanceNotice notice;
        private final RouteSurface surface = new RouteSurface();
        private GeoDestination destination;
        private boolean malformed;
        private boolean loading;
        private boolean locationPending;
        private String failure;
        private NavigationRoute route;
        private NavigationProgress guidance;
        private NavigationProgress.State guidanceState;
        private CarLocation.Subscription locationSubscription;
        private boolean rerouting;
        private boolean managerStarted;
        private NavigationRoute.Point currentPosition;
        private long lastLiveFixAtMs;
        private long lastObservationAtMs;
        private int generation;
        private final Runnable locationWatchdog = new Runnable() {
            @Override public void run() {
                if (guidance == null) return;
                Location recovered = CarLocation.latest(getCarContext(), debugBuild);
                if (recovered != null && recovered.getTime() > lastObservationAtMs) {
                    onLiveFix(recovered);
                } else if (System.currentTimeMillis() - lastLiveFixAtMs > 15_000
                        && guidanceState != null) {
                    guidanceState = new NavigationProgress.State(
                            NavigationProgress.Status.LOCATION_UNAVAILABLE,
                            guidanceState.remainingMetres, guidanceState.distanceToTurnMetres,
                            guidanceState.nextTurnIndex, false);
                    currentPosition = null;
                    surface.setRoute(route, null, getCarContext().isDarkMode());
                    publishNotice();
                    invalidate();
                }
                main.postDelayed(this, 5_000);
            }
        };

        DestinationScreen(androidx.car.app.CarContext carContext) {
            super(carContext);
            debugBuild = (carContext.getApplicationInfo().flags & ApplicationInfo.FLAG_DEBUGGABLE) != 0;
            NavigationRouteClient configured;
            try {
                configured = new NavigationRouteClient(
                        carContext.getString(R.string.trickee_router_url), debugBuild);
            } catch (IllegalArgumentException error) {
                configured = null;
            }
            client = configured;
            carContext.getCarService(AppManager.class).setSurfaceCallback(surface);
            navigationManager = carContext.getCarService(NavigationManager.class);
            notice = new GuidanceNotice(carContext);
            navigationManager.setNavigationManagerCallback(new NavigationManagerCallback() {
                @Override public void onStopNavigation() {
                    stopGuidance(true);
                }
            });
            getLifecycle().addObserver((LifecycleEventObserver) (source, event) -> {
                if (event == Lifecycle.Event.ON_STOP || event == Lifecycle.Event.ON_DESTROY) {
                    stopGuidance(false);
                }
                if (event == Lifecycle.Event.ON_DESTROY) {
                    generation++;
                    navigationManager.clearNavigationManagerCallback();
                    routes.shutdownNow();
                }
            });
        }

        void update(Intent intent) {
            stopGuidance(false);
            String action = intent.getAction();
            boolean navigationIntent = "androidx.car.app.action.NAVIGATE".equals(action);
            destination = navigationIntent && intent.getData() != null
                    ? GeoDestination.parse(intent.getDataString()) : null;
            malformed = navigationIntent && destination == null;
            generation++;
            loading = false;
            locationPending = false;
            route = null;
            failure = null;
            invalidate();
        }

        void onThemeChanged() {
            surface.setRoute(guidance == null ? null : route, currentPosition,
                    getCarContext().isDarkMode());
            invalidate();
        }

        private void requestLocationPermission() {
            getCarContext().requestPermissions(Arrays.asList(
                            Manifest.permission.ACCESS_COARSE_LOCATION,
                            Manifest.permission.ACCESS_FINE_LOCATION),
                    (granted, rejected) -> {
                        failure = granted.contains(Manifest.permission.ACCESS_FINE_LOCATION)
                                ? null : "Precise car location was not granted.";
                        invalidate();
                    });
        }

        private boolean noticePermitted() {
            return Build.VERSION.SDK_INT < 33 || getCarContext().checkSelfPermission(
                    Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED;
        }

        private void requestNoticePermission() {
            if (Build.VERSION.SDK_INT < 33) return;
            getCarContext().requestPermissions(Arrays.asList(Manifest.permission.POST_NOTIFICATIONS),
                    (granted, rejected) -> {
                        failure = granted.contains(Manifest.permission.POST_NOTIFICATIONS)
                                ? null : "Navigation notifications were not granted.";
                        invalidate();
                    });
        }

        private void requestFreshLocation() {
            if (locationPending) return;
            locationPending = true;
            int requestGeneration = generation;
            failure = null;
            invalidate();
            CarLocation.requestFresh(getCarContext(), debugBuild, location -> {
                if (requestGeneration != generation) return;
                locationPending = false;
                if (location == null) {
                    failure = "Fresh car location is unavailable.";
                    invalidate();
                } else {
                    loadRoute(location);
                }
            });
        }

        private void loadRoute(Location location) {
            GeoDestination chosen = destination;
            if (chosen == null || client == null || !CarLocation.usable(location, debugBuild)) {
                failure = "Fresh car location or road provider is unavailable.";
                invalidate();
                return;
            }
            int requestGeneration = ++generation;
            NavigationRoute.Point origin = new NavigationRoute.Point(
                    location.getLatitude(), location.getLongitude());
            NavigationRoute.Point target = new NavigationRoute.Point(
                    chosen.latitude, chosen.longitude);
            route = null;
            failure = null;
            loading = true;
            invalidate();
            routes.execute(() -> {
                NavigationRoute loaded = null;
                String problem = null;
                try {
                    loaded = client.fetch(origin, target);
                } catch (Exception error) {
                    if (debugBuild) {
                        Log.w("TrickeeNav", "Route failure: " + error.getClass().getSimpleName()
                                + (error.getCause() == null ? "" : "/" + error.getCause().getClass().getSimpleName()));
                    }
                    problem = "Road provider is unavailable or returned an invalid route.";
                }
                NavigationRoute result = loaded;
                String failureResult = problem;
                main.post(() -> {
                    if (requestGeneration != generation) return;
                    route = result;
                    failure = failureResult;
                    loading = false;
                    invalidate();
                });
            });
        }

        private void startGuidance() {
            NavigationRoute selected = route;
            if (selected == null || selected.turns.isEmpty() || locationPending || !noticePermitted()) return;
            locationPending = true;
            failure = null;
            int requestGeneration = generation;
            invalidate();
            CarLocation.requestFresh(getCarContext(), debugBuild, location -> {
                if (requestGeneration != generation || selected != route) return;
                locationPending = false;
                if (location == null || !CarLocation.usable(location, debugBuild)) {
                    failure = "Fresh car location is required to start guidance.";
                    invalidate();
                    return;
                }
                guidance = new NavigationProgress(selected);
                guidanceState = guidance.update(location.getLatitude(), location.getLongitude(),
                        location.getAccuracy(), location.getTime(), System.currentTimeMillis(),
                        !debugBuild && location.isFromMockProvider());
                if (guidanceState.status != NavigationProgress.Status.ON_ROUTE) {
                    guidance = null;
                    guidanceState = null;
                    route = null;
                    failure = "Fresh route position is unavailable. Refresh the road route.";
                    surface.setRoute(null, null, getCarContext().isDarkMode());
                    invalidate();
                    return;
                }
                lastLiveFixAtMs = System.currentTimeMillis();
                lastObservationAtMs = location.getTime();
                currentPosition = new NavigationRoute.Point(
                        location.getLatitude(), location.getLongitude());
                locationSubscription = CarLocation.listen(getCarContext(), this::onLiveFix);
                if (locationSubscription == null) {
                    stopGuidance(false);
                    failure = "Car location updates are unavailable.";
                } else {
                    navigationManager.navigationStarted();
                    managerStarted = true;
                    publishTrip();
                    publishNotice();
                    surface.setRoute(route, currentPosition, getCarContext().isDarkMode());
                    main.removeCallbacks(locationWatchdog);
                    main.postDelayed(locationWatchdog, 5_000);
                }
                invalidate();
            });
        }

        private void onLiveFix(Location location) {
            if (guidance == null) return;
            lastLiveFixAtMs = System.currentTimeMillis();
            lastObservationAtMs = location.getTime();
            currentPosition = new NavigationRoute.Point(
                    location.getLatitude(), location.getLongitude());
            guidanceState = guidance.update(location.getLatitude(), location.getLongitude(),
                    location.hasAccuracy() ? location.getAccuracy() : Float.NaN,
                    location.getTime(), lastLiveFixAtMs,
                    !debugBuild && location.isFromMockProvider());
            if (guidanceState.status == NavigationProgress.Status.ARRIVED) {
                stopGuidance(false);
                failure = "Destination reached.";
            } else if (guidanceState.status == NavigationProgress.Status.ON_ROUTE) {
                failure = null;
            } else if (guidanceState.rerouteDue && !rerouting) {
                reroute(location);
            }
            if (guidance != null) {
                if (guidanceState.status == NavigationProgress.Status.LOCATION_UNAVAILABLE) {
                    currentPosition = null;
                }
                surface.setRoute(route, currentPosition, getCarContext().isDarkMode());
                publishTrip();
                publishNotice();
            }
            invalidate();
        }

        private void reroute(Location current) {
            if (client == null || destination == null || !CarLocation.usable(current, debugBuild)) return;
            int requestGeneration = ++generation;
            GeoDestination target = destination;
            rerouting = true;
            failure = null;
            routes.execute(() -> {
                NavigationRoute replacement = null;
                try {
                    replacement = client.fetch(
                            new NavigationRoute.Point(current.getLatitude(), current.getLongitude()),
                            new NavigationRoute.Point(target.latitude, target.longitude));
                } catch (Exception error) {
                    if (debugBuild) Log.w("TrickeeNav", "Reroute failed: " + error.getClass().getSimpleName());
                }
                NavigationRoute result = replacement;
                main.post(() -> {
                    if (requestGeneration != generation || guidance == null) return;
                    rerouting = false;
                    if (result == null || result.turns.isEmpty()) {
                        failure = "Road provider unavailable; guidance paused while off route.";
                    } else {
                        route = result;
                        guidance = new NavigationProgress(result);
                        guidanceState = guidance.update(current.getLatitude(), current.getLongitude(),
                                current.getAccuracy(), current.getTime(), System.currentTimeMillis(),
                                !debugBuild && current.isFromMockProvider());
                        failure = null;
                        currentPosition = new NavigationRoute.Point(
                                current.getLatitude(), current.getLongitude());
                        surface.setRoute(route, currentPosition, getCarContext().isDarkMode());
                    }
                    publishTrip();
                    publishNotice();
                    invalidate();
                });
            });
        }

        private void stopGuidance(boolean redraw) {
            if (locationSubscription != null) {
                locationSubscription.close();
                locationSubscription = null;
            }
            main.removeCallbacks(locationWatchdog);
            guidance = null;
            guidanceState = null;
            rerouting = false;
            currentPosition = null;
            lastObservationAtMs = 0;
            surface.setRoute(null, null, getCarContext().isDarkMode());
            if (managerStarted) {
                navigationManager.navigationEnded();
                managerStarted = false;
            }
            notice.cancel();
            generation++;
            if (redraw) failure = null;
            if (redraw) invalidate();
        }

        private TravelEstimate estimate(double remainingMetres) {
            long seconds = Math.max(0, Math.round(route.durationSeconds
                    * Math.max(0, remainingMetres) / route.distanceMetres));
            return new TravelEstimate.Builder(
                    Distance.create(Math.max(0, remainingMetres) / 1000.0, Distance.UNIT_KILOMETERS_P1),
                    ZonedDateTime.now().plusSeconds(seconds))
                    .setRemainingTimeSeconds(seconds).build();
        }

        private void publishTrip() {
            if (!managerStarted || route == null || guidanceState == null || destination == null) return;
            NavigationProgress.State current = guidanceState;
            if (current.status != NavigationProgress.Status.ON_ROUTE || current.nextTurnIndex < 0) {
                navigationManager.updateTrip(new androidx.car.app.navigation.model.Trip.Builder()
                        .setLoading(true).build());
                return;
            }
            Step step = NavigationManeuver.step(route.turns.get(current.nextTurnIndex));
            if (step == null) {
                navigationManager.updateTrip(new androidx.car.app.navigation.model.Trip.Builder()
                        .addDestination(new Destination.Builder().setName(destination.label).build(),
                                estimate(current.remainingMetres)).build());
                return;
            }
            androidx.car.app.navigation.model.Trip trip =
                    new androidx.car.app.navigation.model.Trip.Builder()
                            .addDestination(new Destination.Builder().setName(destination.label).build(),
                                    estimate(current.remainingMetres))
                            .addStep(step, estimate(current.distanceToTurnMetres))
                            .build();
            navigationManager.updateTrip(trip);
        }

        private void publishNotice() {
            if (managerStarted && route != null && destination != null && guidanceState != null) {
                notice.update(route, destination, guidanceState);
            }
        }

        private Template activeTemplate() {
            NavigationProgress.State current = guidanceState;
            NavigationTemplate.Builder builder = new NavigationTemplate.Builder()
                    .setActionStrip(new ActionStrip.Builder()
                            .addAction(action("Stop", () -> stopGuidance(true), false)).build());
            if (current.status == NavigationProgress.Status.ON_ROUTE
                    && current.nextTurnIndex >= 0) {
                NavigationRoute.Turn turn = route.turns.get(current.nextTurnIndex);
                Step step = NavigationManeuver.step(turn);
                if (step == null) {
                    builder.setNavigationInfo(new MessageInfo.Builder(turn.instruction).build());
                } else {
                    builder.setNavigationInfo(new RoutingInfo.Builder()
                            .setCurrentStep(step, Distance.create(
                                    Math.max(0, current.distanceToTurnMetres), Distance.UNIT_METERS)).build());
                }
                builder.setDestinationTravelEstimate(estimate(current.remainingMetres));
            } else {
                String message = current.status == NavigationProgress.Status.LOCATION_UNAVAILABLE
                        ? "Car location unavailable. Guidance paused."
                        : rerouting ? "Off route. Checking a new road route."
                        : failure == null ? "Off route. Waiting for a provider route."
                        : failure;
                builder.setNavigationInfo(new MessageInfo.Builder(message).build());
            }
            return builder.build();
        }

        private Action action(String title, Runnable callback, boolean parkedOnly) {
            Action.Builder builder = new Action.Builder().setTitle(title);
            if (parkedOnly) {
                builder.setOnClickListener(ParkedOnlyOnClickListener.create(callback::run));
            } else {
                builder.setOnClickListener(callback::run);
            }
            return builder.build();
        }

        @NonNull @Override public Template onGetTemplate() {
            if (guidance != null && guidanceState != null) return activeTemplate();
            String message;
            Action next = null;
            if (malformed) {
                message = "This destination is unavailable. Send a coordinate destination from a supported car app.";
            } else if (destination == null) {
                message = "Send a coordinate destination from a supported car app.";
            } else if (client == null) {
                message = "Road guidance is not configured on this vehicle.";
            } else if (!CarLocation.permitted(getCarContext())) {
                message = "Allow car location while parked to preview a provider road route.";
                next = action("Allow location", this::requestLocationPermission, true);
            } else if (locationPending) {
                message = "Waiting for a fresh car location.";
            } else if (loading) {
                message = "Checking road route with the configured provider.";
            } else if (route != null) {
                message = (failure == null ? "" : failure + " ") + route.summary() + (route.turns.isEmpty()
                        ? " Positioned turns are unavailable; preview only."
                        : " Start to follow positioned provider turns on this car screen.");
                next = !route.turns.isEmpty() && !noticePermitted()
                        ? action("Allow notices", this::requestNoticePermission, true)
                        : route.turns.isEmpty()
                        ? action("Refresh route", this::requestFreshLocation, false)
                        : action("Start guidance", this::startGuidance, false);
                if (!route.turns.isEmpty() && !noticePermitted()) {
                    message = "Allow navigation notices while parked to start guidance to "
                            + destination.label + ".";
                }
            } else {
                Location current = CarLocation.latest(getCarContext(), debugBuild);
                if (current != null && failure == null) {
                    loadRoute(current);
                    message = "Checking road route with the configured provider.";
                } else {
                    message = String.format(Locale.US,
                            "%s (%.5f, %.5f). %s", destination.label,
                            destination.latitude, destination.longitude,
                            failure == null ? "Fresh car location is needed." : failure);
                    next = action("Find route", this::requestFreshLocation, false);
                }
            }
            MessageTemplate.Builder builder = new MessageTemplate.Builder(message)
                    .setTitle("Trickee navigation")
                    .setHeaderAction(Action.APP_ICON);
            if (next != null) builder.addAction(next);
            return builder.build();
        }
    }
}
