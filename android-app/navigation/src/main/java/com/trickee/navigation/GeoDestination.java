package com.trickee.navigation;

import java.net.URLDecoder;
import java.io.UnsupportedEncodingException;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** A coordinate-only subset of Android geo intents. Search terms need a real place provider. */
final class GeoDestination {
    private static final String NUMBER = "-?(?:\\d{1,3})(?:\\.\\d{1,10})?";
    private static final Pattern PAIR = Pattern.compile("^(" + NUMBER + "),(" + NUMBER + ")$");
    private static final Pattern LABELED = Pattern.compile("^(" + NUMBER + "),(" + NUMBER + ")(?:\\(([^()]*)\\))?$");

    final double latitude;
    final double longitude;
    final String label;

    private GeoDestination(double latitude, double longitude, String label) {
        this.latitude = latitude;
        this.longitude = longitude;
        this.label = label;
    }

    static GeoDestination parse(String uri) {
        if (uri == null || uri.length() > 512 || !uri.regionMatches(true, 0, "geo:", 0, 4)) {
            return null;
        }
        String content = uri.substring(4);
        String[] parts = content.split("\\?", -1);
        if (parts.length > 2 || !PAIR.matcher(parts[0]).matches()) {
            return null;
        }
        String coordinates = parts[0];
        String label = "Destination";
        if (parts.length == 2) {
            if (!parts[1].startsWith("q=") || parts[1].contains("&")) {
                return null;
            }
            String decoded;
            try {
                decoded = URLDecoder.decode(parts[1].substring(2), "UTF-8");
            } catch (IllegalArgumentException | UnsupportedEncodingException error) {
                return null;
            }
            Matcher match = LABELED.matcher(decoded);
            if (!match.matches()) {
                return null;
            }
            coordinates = match.group(1) + "," + match.group(2);
            if (match.group(3) != null && !match.group(3).isBlank()) {
                label = match.group(3).trim();
            }
        }
        if (label.length() > 64 || label.chars().anyMatch(ch -> Character.isISOControl(ch))) {
            return null;
        }
        Matcher pair = PAIR.matcher(coordinates);
        if (!pair.matches()) {
            return null;
        }
        double lat = Double.parseDouble(pair.group(1));
        double lon = Double.parseDouble(pair.group(2));
        if (!Double.isFinite(lat) || !Double.isFinite(lon)
                || lat < -90 || lat > 90 || lon < -180 || lon > 180) {
            return null;
        }
        return new GeoDestination(lat, lon, label);
    }
}
