package com.trickee.driver;

/** Foreground route progress from provider geometry and current phone fixes. No network or map tiles. */
final class RouteProgress {
    private static final double METERS_PER_DEGREE = 111_320.0;
    private static final long MAX_FIX_AGE_MS = 15_000;
    private static final long REROUTE_COOLDOWN_MS = 30_000;
    private static final double MAX_SPEED_MPS = 60.0;

    enum Status { ON_ROUTE, OFF_ROUTE, ARRIVED, LOCATION_UNAVAILABLE }

    static final class Step {
        final double latitude, longitude;
        final String type, modifier, name;
        double routeMeters;

        Step(double latitude, double longitude, String type, String modifier, String name) {
            if (!valid(latitude, longitude)) throw new IllegalArgumentException("invalid turn location");
            this.latitude = latitude;
            this.longitude = longitude;
            this.type = type;
            this.modifier = modifier;
            this.name = name;
        }
    }

    static final class State {
        final Status status;
        final double progressMeters, remainingMeters, distanceFromRouteMeters, distanceToTurnMeters;
        final int nextStepIndex;
        final boolean rerouteDue;

        State(Status status, double progressMeters, double remainingMeters,
              double distanceFromRouteMeters, double distanceToTurnMeters,
              int nextStepIndex, boolean rerouteDue) {
            this.status = status;
            this.progressMeters = progressMeters;
            this.remainingMeters = remainingMeters;
            this.distanceFromRouteMeters = distanceFromRouteMeters;
            this.distanceToTurnMeters = distanceToTurnMeters;
            this.nextStepIndex = nextStepIndex;
            this.rerouteDue = rerouteDue;
        }
    }

    private static final class Projection {
        final double alongMeters, distanceMeters;
        Projection(double alongMeters, double distanceMeters) {
            this.alongMeters = alongMeters;
            this.distanceMeters = distanceMeters;
        }
    }

    private final double[][] points;
    private final double[] cumulative;
    private final Step[] steps;
    private final double totalMeters;
    private double progressMeters;
    private long lastObservedAtMs = -1;
    private long lastProgressAtMs = -1;
    private long lastRerouteAtMs = -1;
    private int offRouteStreak;

    RouteProgress(double[][] routeLonLat, Step[] routeSteps) {
        if (routeLonLat == null || routeLonLat.length < 2 || routeLonLat.length > 20_000
                || routeSteps == null || routeSteps.length > 200) {
            throw new IllegalArgumentException("invalid route size");
        }
        points = new double[routeLonLat.length][2];
        cumulative = new double[routeLonLat.length];
        for (int index = 0; index < routeLonLat.length; index++) {
            double[] point = routeLonLat[index];
            if (point == null || point.length != 2 || !valid(point[1], point[0]))
                throw new IllegalArgumentException("invalid route point");
            points[index] = point.clone();
            if (index > 0) cumulative[index] = cumulative[index - 1]
                    + distance(points[index - 1][1], points[index - 1][0], point[1], point[0]);
        }
        totalMeters = cumulative[cumulative.length - 1];
        if (totalMeters < 1 || totalMeters > 3_000_000) throw new IllegalArgumentException("invalid route length");
        steps = routeSteps.clone();
        double previousStep = 0;
        for (Step step : steps) {
            if (step == null) throw new IllegalArgumentException("missing turn");
            Projection nearest = project(step.latitude, step.longitude, previousStep, Double.POSITIVE_INFINITY);
            if (nearest.distanceMeters > 100 || nearest.alongMeters + 1 < previousStep)
                throw new IllegalArgumentException("turn is off route or out of order");
            step.routeMeters = nearest.alongMeters;
            previousStep = nearest.alongMeters;
        }
    }

    Step step(int index) { return steps[index]; }

    State update(double latitude, double longitude, float accuracyMeters,
                 long observedAtMs, long nowMs, boolean mocked) {
        if (mocked || !valid(latitude, longitude) || !Float.isFinite(accuracyMeters)
                || accuracyMeters <= 0 || accuracyMeters > 100 || observedAtMs <= lastObservedAtMs
                || observedAtMs > nowMs + 5_000 || nowMs - observedAtMs > MAX_FIX_AGE_MS) {
            offRouteStreak = 0;
            return state(Status.LOCATION_UNAVAILABLE, Double.NaN, false);
        }
        Projection nearest = project(latitude, longitude, Math.max(0, progressMeters - 100),
                lastProgressAtMs < 0 ? 500 : progressMeters + 5000);
        lastObservedAtMs = observedAtMs;
        if (nearest.distanceMeters > Math.max(35, accuracyMeters * 2)) {
            offRouteStreak++;
            boolean rerouteDue = offRouteStreak >= 3
                    && (lastRerouteAtMs < 0 || nowMs - lastRerouteAtMs >= REROUTE_COOLDOWN_MS);
            if (rerouteDue) lastRerouteAtMs = nowMs;
            return state(Status.OFF_ROUTE, nearest.distanceMeters, rerouteDue);
        }
        if (lastProgressAtMs >= 0 && nearest.alongMeters > progressMeters + 100
                + Math.max(0, observedAtMs - lastProgressAtMs) * MAX_SPEED_MPS / 1000.0) {
            offRouteStreak = 0;
            return state(Status.LOCATION_UNAVAILABLE, nearest.distanceMeters, false);
        }
        offRouteStreak = 0;
        progressMeters = Math.max(progressMeters, nearest.alongMeters);
        lastProgressAtMs = observedAtMs;
        boolean nearEnd = distance(latitude, longitude,
                points[points.length - 1][1], points[points.length - 1][0]) <= 35;
        return state(progressMeters >= totalMeters - 35 && nearEnd ? Status.ARRIVED : Status.ON_ROUTE,
                nearest.distanceMeters, false);
    }

    private State state(Status status, double distanceFromRouteMeters, boolean rerouteDue) {
        int next = -1;
        for (int index = 0; index < steps.length; index++) {
            if (steps[index].routeMeters > progressMeters + 15) { next = index; break; }
        }
        return new State(status, progressMeters, Math.max(0, totalMeters - progressMeters),
                distanceFromRouteMeters, next < 0 ? Double.NaN : steps[next].routeMeters - progressMeters,
                next, rerouteDue);
    }

    private Projection project(double latitude, double longitude, double minAlongMeters,
                               double maxAlongMeters) {
        Projection best = null;
        for (int index = 0; index < points.length - 1; index++) {
            if (cumulative[index + 1] < minAlongMeters || cumulative[index] > maxAlongMeters) continue;
            double[] first = points[index], second = points[index + 1];
            double cosLatitude = Math.max(0.01, Math.abs(Math.cos(Math.toRadians((first[1] + second[1]) / 2))));
            double segmentX = (second[0] - first[0]) * METERS_PER_DEGREE * cosLatitude;
            double segmentY = (second[1] - first[1]) * METERS_PER_DEGREE;
            double fixX = (longitude - first[0]) * METERS_PER_DEGREE * cosLatitude;
            double fixY = (latitude - first[1]) * METERS_PER_DEGREE;
            double square = segmentX * segmentX + segmentY * segmentY;
            double fraction = square == 0 ? 0 : Math.max(0, Math.min(1,
                    (fixX * segmentX + fixY * segmentY) / square));
            double distanceMeters = Math.hypot(fixX - fraction * segmentX, fixY - fraction * segmentY);
            double alongMeters = cumulative[index] + fraction * (cumulative[index + 1] - cumulative[index]);
            if (best == null || distanceMeters < best.distanceMeters - 0.001) {
                best = new Projection(alongMeters, distanceMeters);
            }
        }
        if (best == null) throw new IllegalStateException("route has no eligible segment");
        return best;
    }

    private static boolean valid(double latitude, double longitude) {
        return Double.isFinite(latitude) && Double.isFinite(longitude)
                && Math.abs(latitude) <= 90 && Math.abs(longitude) <= 180;
    }

    private static double distance(double lat1, double lon1, double lat2, double lon2) {
        double latitude = Math.toRadians(lat2 - lat1);
        double longitude = Math.toRadians(lon2 - lon1);
        double sine = Math.pow(Math.sin(latitude / 2), 2)
                + Math.cos(Math.toRadians(lat1)) * Math.cos(Math.toRadians(lat2))
                * Math.pow(Math.sin(longitude / 2), 2);
        return 6_371_000 * 2 * Math.atan2(Math.sqrt(sine), Math.sqrt(Math.max(0, 1 - sine)));
    }
}
