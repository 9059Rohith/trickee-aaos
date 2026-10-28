package com.trickee.navigation;

import org.json.JSONException;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URI;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.Locale;

/** Small fail-closed OSRM route client for an operator-configured service. */
final class NavigationRouteClient {
    private final URI base;

    NavigationRouteClient(String rawUrl, boolean debugBuild) {
        if (rawUrl == null || rawUrl.isBlank()) {
            throw new IllegalArgumentException("route provider is not configured");
        }
        URI parsed;
        try {
            parsed = URI.create(rawUrl.trim());
        } catch (IllegalArgumentException error) {
            throw new IllegalArgumentException("route provider URL is invalid", error);
        }
        String scheme = parsed.getScheme();
        boolean loopback = "127.0.0.1".equals(parsed.getHost()) || "localhost".equals(parsed.getHost());
        if (parsed.getHost() == null || parsed.getUserInfo() != null || parsed.getFragment() != null
                || parsed.getQuery() != null || parsed.getHost().length() > 253
                || !("https".equals(scheme) || (debugBuild && loopback && "http".equals(scheme)))) {
            throw new IllegalArgumentException("route provider must use HTTPS without URL credentials");
        }
        String path = parsed.getPath();
        if (path == null || path.isEmpty()) path = "/";
        if (!path.endsWith("/")) path += "/";
        base = URI.create(parsed.getScheme() + "://" + parsed.getRawAuthority() + path);
    }

    String requestPath(NavigationRoute.Point origin, NavigationRoute.Point destination) {
        if (!NavigationRoute.valid(origin.latitude, origin.longitude)
                || !NavigationRoute.valid(destination.latitude, destination.longitude)) {
            throw new IllegalArgumentException("route coordinates are invalid");
        }
        return String.format(Locale.US,
                "route/v1/driving/%.7f,%.7f;%.7f,%.7f?overview=full&geometries=geojson&steps=true&alternatives=false",
                origin.longitude, origin.latitude, destination.longitude, destination.latitude);
    }

    NavigationRoute fetch(NavigationRoute.Point origin,
                          NavigationRoute.Point destination) throws IOException {
        URL url = base.resolve(requestPath(origin, destination)).toURL();
        HttpURLConnection connection = (HttpURLConnection) url.openConnection();
        connection.setConnectTimeout(4_000);
        connection.setReadTimeout(5_000);
        connection.setInstanceFollowRedirects(false);
        connection.setRequestProperty("Accept", "application/json");
        connection.setRequestProperty("User-Agent", "Trickee-AAOS-Navigation/0.1");
        try {
            if (connection.getResponseCode() != 200) {
                throw new IOException("route provider returned HTTP " + connection.getResponseCode());
            }
            String contentType = connection.getContentType();
            if (contentType == null || !contentType.toLowerCase(Locale.ROOT).contains("application/json")) {
                throw new IOException("route provider content type is invalid");
            }
            try (InputStream input = connection.getInputStream();
                 ByteArrayOutputStream output = new ByteArrayOutputStream()) {
                byte[] chunk = new byte[8192];
                int count;
                while ((count = input.read(chunk)) >= 0) {
                    if (output.size() + count > 1_000_000) {
                        throw new IOException("route provider response is too large");
                    }
                    output.write(chunk, 0, count);
                }
                try {
                    return NavigationRoute.parse(output.toString(StandardCharsets.UTF_8.name()),
                            origin, destination);
                } catch (JSONException error) {
                    throw new IOException("route provider returned invalid geometry", error);
                }
            }
        } finally {
            connection.disconnect();
        }
    }
}
