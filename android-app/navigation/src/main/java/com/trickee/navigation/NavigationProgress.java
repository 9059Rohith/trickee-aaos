package com.trickee.navigation;

/** Foreground progress over one validated provider geometry. It stores no location history. */
final class NavigationProgress {
    private static final long MAX_FIX_AGE_MS = 15_000;
    private static final long REROUTE_COOLDOWN_MS = 30_000;
    private static final double MAX_SPEED_METRES_PER_SECOND = 60;

    enum Status { ON_ROUTE, OFF_ROUTE, ARRIVED, LOCATION_UNAVAILABLE }

    static final class State {
        final Status status;
        final double remainingMetres;
        final double distanceToTurnMetres;
        final int nextTurnIndex;
        final boolean rerouteDue;

        State(Status status, double remainingMetres, double distanceToTurnMetres,
              int nextTurnIndex, boolean rerouteDue) {
            this.status = status;
            this.remainingMetres = remainingMetres;
            this.distanceToTurnMetres = distanceToTurnMetres;
            this.nextTurnIndex = nextTurnIndex;
            this.rerouteDue = rerouteDue;
        }
    }

    private final NavigationRoute route;
    private final double drawnMetres;
    private double progressMetres;
    private long lastObservedAtMs = -1;
    private long lastGoodAtMs = -1;
    private long lastRerouteAtMs = -1;
    private int offRouteStreak;

    NavigationProgress(NavigationRoute route) {
        if (route == null || route.shape.size() < 2 || route.turns.isEmpty()) {
            throw new IllegalArgumentException("route needs geometry and positioned turns");
        }
        this.route = route;
        double length = 0;
        for (int index = 1; index < route.shape.size(); index++) {
            length += route.shape.get(index - 1).distanceMetres(route.shape.get(index));
        }
        if (length < 1 || !Double.isFinite(length)) throw new IllegalArgumentException("route is degenerate");
        drawnMetres = length;
    }

    State update(double latitude, double longitude, float accuracyMetres,
                 long observedAtMs, long nowMs, boolean mocked) {
        if (mocked || !NavigationRoute.valid(latitude, longitude)
                || !Float.isFinite(accuracyMetres) || accuracyMetres <= 0 || accuracyMetres > 100
                || observedAtMs <= lastObservedAtMs || observedAtMs > nowMs + 5_000
                || nowMs - observedAtMs > MAX_FIX_AGE_MS) {
            offRouteStreak = 0;
            return state(Status.LOCATION_UNAVAILABLE, false);
        }
        NavigationRoute.Point position = new NavigationRoute.Point(latitude, longitude);
        NavigationRoute.Projection projected = NavigationRoute.project(route.shape, position);
        lastObservedAtMs = observedAtMs;
        if (projected.distanceMetres > Math.max(35, accuracyMetres * 2)) {
            offRouteStreak++;
            boolean due = offRouteStreak >= 3 && (lastRerouteAtMs < 0
                    || nowMs - lastRerouteAtMs >= REROUTE_COOLDOWN_MS);
            if (due) lastRerouteAtMs = nowMs;
            return state(Status.OFF_ROUTE, due);
        }
        offRouteStreak = 0;
        if (lastGoodAtMs >= 0 && projected.alongMetres > progressMetres + 100
                + Math.max(0, observedAtMs - lastGoodAtMs) * MAX_SPEED_METRES_PER_SECOND / 1000.0) {
            return state(Status.LOCATION_UNAVAILABLE, false);
        }
        progressMetres = Math.max(progressMetres, projected.alongMetres);
        lastGoodAtMs = observedAtMs;
        boolean atEnd = progressMetres >= drawnMetres - 35
                && position.distanceMetres(route.shape.get(route.shape.size() - 1)) <= 35;
        return state(atEnd ? Status.ARRIVED : Status.ON_ROUTE, false);
    }

    private State state(Status status, boolean rerouteDue) {
        double remaining = Math.max(0, route.distanceMetres * (1 - progressMetres / drawnMetres));
        int turn = -1;
        for (int index = 0; index < route.turns.size(); index++) {
            if (route.turns.get(index).distanceAlongMetres > progressMetres + 15) {
                turn = index;
                break;
            }
        }
        return new State(status, remaining,
                turn < 0 ? Double.NaN : route.turns.get(turn).distanceAlongMetres - progressMetres,
                turn, rerouteDue);
    }
}
