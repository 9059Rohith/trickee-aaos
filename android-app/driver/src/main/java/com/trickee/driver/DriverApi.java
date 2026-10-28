package com.trickee.driver;

import android.content.Context;
import android.content.SharedPreferences;
import java.io.IOException;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URI;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.Locale;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;
import org.json.JSONTokener;

/** Synchronous API transport. Call from a worker thread only. */
final class DriverApi {
    static final String PREFS = "driver_settings";
    static final String ORIGIN = "api_origin";
    private final String origin;
    private final String token;

    DriverApi(String origin, String token) {
        this.origin = validateOrigin(origin);
        if (token == null || token.isBlank()) throw new IllegalArgumentException("driver credential is required");
        this.token = token;
    }

    static DriverApi saved(Context context) throws Exception {
        SharedPreferences prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        return new DriverApi(prefs.getString(ORIGIN, ""), SecretStore.read(prefs));
    }

    static String validateOrigin(String value) {
        try {
            URI uri = new URI(value.trim());
            boolean secure = "https".equalsIgnoreCase(uri.getScheme());
            boolean localDebug = BuildConfig.DEBUG && "http".equalsIgnoreCase(uri.getScheme())
                    && ("10.0.2.2".equals(uri.getHost()) || "127.0.0.1".equals(uri.getHost()));
            if ((!secure && !localDebug) || uri.getHost() == null || uri.getRawUserInfo() != null
                    || uri.getRawPath() != null && !uri.getRawPath().isEmpty() && !"/".equals(uri.getRawPath())
                    || uri.getRawQuery() != null || uri.getRawFragment() != null)
                throw new IllegalArgumentException("Use an HTTPS server origin (debug emulator permits http://10.0.2.2)");
            return uri.getScheme() + "://" + uri.getAuthority();
        } catch (Exception exception) {
            if (exception instanceof IllegalArgumentException) throw (IllegalArgumentException) exception;
            throw new IllegalArgumentException("Enter a valid server origin", exception);
        }
    }

    Object call(String method, String path, JSONObject payload) throws IOException, JSONException {
        if (!path.startsWith("/api/v1/") || path.contains("..") || path.contains("?"))
            throw new IllegalArgumentException("invalid API path");
        return request(method, path, payload);
    }

    Object nearbyChargers(double latitude, double longitude) throws IOException, JSONException {
        if (!Double.isFinite(latitude) || !Double.isFinite(longitude)
                || Math.abs(latitude) > 90 || Math.abs(longitude) > 180)
            throw new IllegalArgumentException("invalid charger search location");
        String path = String.format(Locale.US,
                "/api/v1/driver/chargers/nearby?latitude=%.6f&longitude=%.6f", latitude, longitude);
        return request("GET", path, null);
    }

    Object workspacePage(String resource, int offset) throws IOException, JSONException {
        String path = WorkspaceRules.pagePath(resource, offset);
        return request("GET", path, null);
    }

    Object deleteWorkspaceEvent(String identifier, int revision) throws IOException, JSONException {
        if (revision < 1) throw new IllegalArgumentException("invalid event revision");
        return request("DELETE", "/api/v1/driver/workspace/events/" + WorkspaceRules.id(identifier)
                + "?revision=" + revision, null);
    }

    JSONObject updates(int after) throws IOException, JSONException {
        if (after < 0) throw new IllegalArgumentException("invalid update cursor");
        return (JSONObject) request("GET", "/api/v1/driver/updates?after=" + after
                + "&wait_seconds=8", null);
    }

    private Object request(String method, String path, JSONObject payload) throws IOException, JSONException {
        HttpURLConnection connection = (HttpURLConnection) new URL(origin + path).openConnection();
        connection.setInstanceFollowRedirects(false);
        connection.setConnectTimeout(8000);
        connection.setReadTimeout(12000);
        connection.setRequestMethod(method);
        connection.setRequestProperty("X-Driver-Key", token);
        connection.setRequestProperty("Accept", "application/json");
        if (payload != null) {
            connection.setDoOutput(true);
            connection.setRequestProperty("Content-Type", "application/json");
            byte[] body = payload.toString().getBytes(StandardCharsets.UTF_8);
            connection.setFixedLengthStreamingMode(body.length);
            try (OutputStream output = connection.getOutputStream()) { output.write(body); }
        }
        try {
            int status = connection.getResponseCode();
            if (status < 200 || status >= 300) throw new ApiFailure(status, "Server returned HTTP " + status);
            try (InputStream input = connection.getInputStream()) {
                ByteArrayOutputStream output = new ByteArrayOutputStream();
                byte[] buffer = new byte[8192]; int size;
                while ((size = input.read(buffer)) >= 0) {
                    if (output.size() + size > 2 * 1024 * 1024) throw new IOException("API response exceeds limit");
                    output.write(buffer, 0, size);
                }
                return new JSONTokener(output.toString(StandardCharsets.UTF_8.name())).nextValue();
            }
        } finally {
            connection.disconnect();
        }
    }

    static final class ApiFailure extends IOException {
        final int status;
        ApiFailure(int status, String message) { super(message); this.status = status; }
    }
}
