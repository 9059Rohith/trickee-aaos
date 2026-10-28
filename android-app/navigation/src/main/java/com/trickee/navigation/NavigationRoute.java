package com.trickee.navigation;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Locale;

/** Validated road route from a configured OSRM-compatible provider. */
final class NavigationRoute {
    static final class Turn {
        final Point position;
        final double distanceAlongMetres;
        final String instruction;
        final String type;
        final String modifier;
        final String road;

        Turn(Point position, double distanceAlongMetres, String instruction,
             String type, String modifier, String road) {
            this.position = position;
            this.distanceAlongMetres = distanceAlongMetres;
            this.instruction = instruction;
            this.type = type;
            this.modifier = modifier;
            this.road = road;
        }
    }

    static final class Point {
        final double latitude;
        final double longitude;

        Point(double latitude, double longitude) {
            this.latitude = latitude;
            this.longitude = longitude;
        }

        double distanceMetres(Point other) {
            double lat = Math.toRadians(other.latitude - latitude);
            double lon = Math.toRadians(other.longitude - longitude);
            double mean = Math.toRadians((other.latitude + latitude) / 2.0);
            return Math.hypot(lat * 6_371_000.0, lon * 6_371_000.0 * Math.cos(mean));
        }
    }

    final List<Point> shape;
    final double distanceMetres;
    final double durationSeconds;
    final String firstInstruction;
    final List<Turn> turns;

    private NavigationRoute(List<Point> shape, double distanceMetres,
                            double durationSeconds, String firstInstruction, List<Turn> turns) {
        this.shape = Collections.unmodifiableList(shape);
        this.distanceMetres = distanceMetres;
        this.durationSeconds = durationSeconds;
        this.firstInstruction = firstInstruction;
        this.turns = Collections.unmodifiableList(turns);
    }

    static NavigationRoute parse(String body, Point origin, Point destination) throws JSONException {
        if (body == null || body.length() > 1_000_000) {
            throw new JSONException("route response size is invalid");
        }
        JSONObject envelope = new JSONObject(body);
        if (!"Ok".equals(envelope.optString("code"))) {
            throw new JSONException("route provider did not return Ok");
        }
        JSONArray routes = envelope.optJSONArray("routes");
        if (routes == null || routes.length() == 0) {
            throw new JSONException("route provider returned no routes");
        }
        JSONObject route = routes.getJSONObject(0);
        double distance = route.optDouble("distance", Double.NaN);
        double duration = route.optDouble("duration", Double.NaN);
        if (!Double.isFinite(distance) || distance <= 0 || distance > 20_000_000
                || !Double.isFinite(duration) || duration <= 0 || duration > 2_000_000) {
            throw new JSONException("route distance or duration is invalid");
        }
        JSONObject geometry = route.optJSONObject("geometry");
        if (geometry == null || !"LineString".equals(geometry.optString("type"))) {
            throw new JSONException("route geometry is missing");
        }
        JSONArray coordinates = geometry.optJSONArray("coordinates");
        if (coordinates == null || coordinates.length() < 2 || coordinates.length() > 4_000) {
            throw new JSONException("route shape point count is invalid");
        }
        List<Point> shape = new ArrayList<>(coordinates.length());
        for (int index = 0; index < coordinates.length(); index++) {
            JSONArray xy = coordinates.optJSONArray(index);
            if (xy == null || xy.length() != 2) {
                throw new JSONException("route coordinate must be a pair");
            }
            double longitude = xy.optDouble(0, Double.NaN);
            double latitude = xy.optDouble(1, Double.NaN);
            if (!valid(latitude, longitude)) {
                throw new JSONException("route coordinate is invalid");
            }
            shape.add(new Point(latitude, longitude));
        }
        if (shape.get(0).distanceMetres(origin) > 2_000
                || shape.get(shape.size() - 1).distanceMetres(destination) > 2_000) {
            throw new JSONException("route endpoints do not match the request");
        }
        double drawnDistance = 0;
        for (int index = 1; index < shape.size(); index++) {
            if (Math.abs(shape.get(index).longitude - shape.get(index - 1).longitude) > 180) {
                throw new JSONException("antimeridian crossing needs split geometry");
            }
            drawnDistance += shape.get(index - 1).distanceMetres(shape.get(index));
        }
        if (drawnDistance < 1 || drawnDistance > distance * 3 + 2_000) {
            throw new JSONException("route shape length is implausible");
        }
        String instruction = "Follow provider route";
        List<Turn> turns = new ArrayList<>();
        JSONArray legs = route.optJSONArray("legs");
        if (legs != null) {
            if (legs.length() > 32) throw new JSONException("too many route legs");
            for (int legIndex = 0; legIndex < legs.length(); legIndex++) {
                JSONObject leg = legs.optJSONObject(legIndex);
                if (leg == null) throw new JSONException("route leg is invalid");
                JSONArray steps = leg.optJSONArray("steps");
                if (steps == null) continue;
                if (steps.length() > 200 || turns.size() + steps.length() > 200) {
                    throw new JSONException("too many route turns");
                }
                for (int stepIndex = 0; stepIndex < steps.length(); stepIndex++) {
                    JSONObject step = steps.optJSONObject(stepIndex);
                    if (step == null) throw new JSONException("route step is invalid");
                    JSONObject maneuver = step.optJSONObject("maneuver");
                    String type = maneuver == null ? "" : safeText(maneuver.optString("type"), 24);
                    String modifier = maneuver == null ? "" : safeText(maneuver.optString("modifier"), 24);
                    String road = safeText(step.optString("name"), 48);
                    String text = (type + " " + modifier + (road.isEmpty() ? "" : " onto " + road)).trim();
                    if (instruction.equals("Follow provider route") && !text.isEmpty()) instruction = text;
                    if (maneuver == null || !maneuver.has("location")) continue;
                    JSONArray xy = maneuver.optJSONArray("location");
                    if (xy == null || xy.length() != 2) throw new JSONException("turn location is invalid");
                    double longitude = xy.optDouble(0, Double.NaN);
                    double latitude = xy.optDouble(1, Double.NaN);
                    if (!valid(latitude, longitude)) throw new JSONException("turn location is invalid");
                    Point position = new Point(latitude, longitude);
                    Projection projected = project(shape, position);
                    if (projected.distanceMetres > 100 || (!turns.isEmpty()
                            && projected.alongMetres + 1 < turns.get(turns.size() - 1).distanceAlongMetres)) {
                        throw new JSONException("turn is off route or out of order");
                    }
                    turns.add(new Turn(position, projected.alongMetres,
                            text.isEmpty() ? "Continue on provider route" : text,
                            type, modifier, road));
                }
            }
        }
        return new NavigationRoute(shape, distance, duration, instruction, turns);
    }

    static final class Projection {
        final double alongMetres;
        final double distanceMetres;

        Projection(double alongMetres, double distanceMetres) {
            this.alongMetres = alongMetres;
            this.distanceMetres = distanceMetres;
        }
    }

    static Projection project(List<Point> shape, Point fix) {
        Projection best = null;
        double cumulative = 0;
        for (int i = 1; i < shape.size(); i++) {
            Point start = shape.get(i - 1);
            Point end = shape.get(i);
            double cosine = Math.max(.01, Math.abs(Math.cos(Math.toRadians((start.latitude + end.latitude) / 2))));
            double dx = (end.longitude - start.longitude) * 111_320 * cosine;
            double dy = (end.latitude - start.latitude) * 111_320;
            double fx = (fix.longitude - start.longitude) * 111_320 * cosine;
            double fy = (fix.latitude - start.latitude) * 111_320;
            double square = dx * dx + dy * dy;
            double fraction = square <= 0 ? 0 : Math.max(0, Math.min(1, (fx * dx + fy * dy) / square));
            double distance = Math.hypot(fx - fraction * dx, fy - fraction * dy);
            double length = start.distanceMetres(end);
            Projection candidate = new Projection(cumulative + fraction * length, distance);
            if (best == null || candidate.distanceMetres < best.distanceMetres) best = candidate;
            cumulative += length;
        }
        return best;
    }

    static boolean valid(double latitude, double longitude) {
        return Double.isFinite(latitude) && Double.isFinite(longitude)
                && latitude >= -90 && latitude <= 90
                && longitude >= -180 && longitude <= 180;
    }

    static String safeText(String text, int limit) {
        if (text == null) return "";
        String cleaned = text.replaceAll("[\\p{Cntrl}\\p{Cf}]", " ").trim();
        if (cleaned.length() > limit) cleaned = cleaned.substring(0, limit).trim();
        return cleaned;
    }

    String summary() {
        return String.format(Locale.US, "Provider road route: %.1f km, about %d min. %s.",
                distanceMetres / 1000.0, Math.max(1, Math.round(durationSeconds / 60.0)),
                firstInstruction);
    }
}
