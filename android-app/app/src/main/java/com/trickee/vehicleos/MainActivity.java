package com.trickee.vehicleos;

import android.app.Activity;
import android.car.Car;
import android.car.VehiclePropertyIds;
import android.car.drivingstate.CarUxRestrictions;
import android.car.drivingstate.CarUxRestrictionsManager;
import android.car.hardware.property.CarPropertyManager;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.text.InputType;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.HashSet;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import org.json.JSONArray;
import org.json.JSONObject;

/** Foreground AAOS Car API gateway. Unsupported properties stay UNKNOWN. */
public final class MainActivity extends Activity {
    private static final int INK = 0xFF03070B;
    private static final int PANEL = 0xFF0B141C;
    private static final int BORDER = 0xFF203039;
    private static final int YELLOW = 0xFFFFE000;
    private static final int CYAN = 0xFF48DFF4;
    private static final int MUTED = 0xFF8FA3AA;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final ExecutorService io = Executors.newSingleThreadExecutor();
    private Car car;
    private volatile CarPropertyManager properties;
    private CarUxRestrictionsManager restrictions;
    private VehicleQueue queue;
    private SharedPreferences settings;
    private TextView status, battery, speed, sync;
    private EditText vehicleId, endpoint, vehicleKey;
    private Button save;
    private LinearLayout setupPanel;
    private volatile boolean moving;
    private boolean uxRestricted = true;
    private final CarUxRestrictionsManager.OnUxRestrictionsChangedListener restrictionListener =
        info -> ui.post(() -> { uxRestricted = info == null || info.isRequiresDistractionOptimization(); applyInteractionLock(); });
    private final Car.CarServiceLifecycleListener carLifecycle = (connectedCar, ready) -> {
        if (isFinishing() || isDestroyed()) return;
        if (restrictions != null) {
            try { restrictions.unregisterListener(); } catch (Exception ignored) { }
            restrictions = null;
        }
        properties = null;
        uxRestricted = true;
        if (!ready) {
            status.setText("Car Service reconnecting");
            applyInteractionLock();
            return;
        }
        try {
            properties = (CarPropertyManager) connectedCar.getCarManager(Car.PROPERTY_SERVICE);
            restrictions = (CarUxRestrictionsManager) connectedCar.getCarManager(Car.CAR_UX_RESTRICTION_SERVICE);
            if (restrictions != null) {
                restrictions.registerListener(restrictionListener);
                CarUxRestrictions current = restrictions.getCurrentCarUxRestrictions();
                uxRestricted = current == null || current.isRequiresDistractionOptimization();
            }
            status.setText(properties == null ? "Car properties unavailable" : "Car Service connected");
        } catch (Exception e) {
            properties = null;
            status.setText("Car Service unavailable: " + e.getClass().getSimpleName());
        }
        applyInteractionLock();
    };
    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        settings = getSharedPreferences("trickee", MODE_PRIVATE);
        queue = new VehicleQueue(this);
        ScrollView scroll = new ScrollView(this);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(dp(24), dp(16), dp(24), dp(28));
        root.setBackgroundColor(INK);
        scroll.addView(root);
        LinearLayout brand = new LinearLayout(this);
        brand.setOrientation(LinearLayout.HORIZONTAL);
        brand.setGravity(android.view.Gravity.CENTER_VERTICAL);
        LinearLayout.LayoutParams brandLayout = new LinearLayout.LayoutParams(-1, -2);
        brandLayout.bottomMargin = dp(16);
        root.addView(brand, brandLayout);
        ImageView mark = new ImageView(this);
        mark.setImageResource(R.drawable.trickee_logo);
        mark.setContentDescription("Trickee");
        brand.addView(mark, new LinearLayout.LayoutParams(dp(68), dp(68)));
        LinearLayout brandText = new LinearLayout(this);
        brandText.setOrientation(LinearLayout.VERTICAL);
        brandText.setPadding(dp(12), 0, 0, 0);
        brand.addView(brandText);
        title(brandText, "TRICKEE", 25, Color.WHITE);
        title(brandText, "VEHICLE OS / AAOS", 12, YELLOW);

        LinearLayout signalPanel = card(root);
        title(signalPanel, "LIVE VEHICLE SIGNALS", 13, YELLOW);
        status = title(signalPanel, "Connecting to Car Service…", 16, MUTED);
        battery = title(signalPanel, "BATTERY  UNKNOWN", 42, YELLOW);
        speed = title(signalPanel, "SPEED  UNKNOWN", 30, CYAN);
        sync = title(card(root), "LOCAL OUTBOX  0", 17, MUTED);

        setupPanel = card(root);
        title(setupPanel, "PARKED SETUP", 15, YELLOW);
        vehicleId = input(setupPanel, "Vehicle ID", settings.getString("vehicle_id", ""));
        endpoint = input(setupPanel, "Cloud URL (HTTPS in release)", settings.getString("endpoint", ""));
        vehicleKey = input(setupPanel, SecretStore.hasKey(settings) ? "Vehicle ingest key saved; enter to replace" : "Vehicle ingest key", "");
        vehicleKey.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        save = new Button(this);
        save.setText("Save connection");
        save.setAllCaps(false);
        save.setTextColor(INK);
        save.setBackground(surface(YELLOW, YELLOW, 5));
        save.setMinHeight(dp(76));
        setupPanel.addView(save, new LinearLayout.LayoutParams(-1, dp(76)));
        save.setOnClickListener(v -> {
            if (moving || uxRestricted) return;
            String url = endpoint.getText().toString().trim();
            String id = vehicleId.getText().toString().trim();
            String enteredKey = vehicleKey.getText().toString().trim();
            if (!id.matches("[A-Za-z0-9_-]{1,64}")) { status.setText("Enter a valid vehicle ID"); return; }
            if (!url.startsWith("https://") && !(BuildConfig.DEBUG && url.startsWith("http://"))) {
                status.setText("Enter a valid HTTPS cloud URL"); return;
            }
            if (enteredKey.isEmpty() && !SecretStore.hasKey(settings)) { status.setText("Enter a vehicle ingest key"); return; }
            vehicleKey.setText("");
            io.execute(() -> {
                try {
                    if (!enteredKey.isEmpty()) SecretStore.saveKey(settings, enteredKey);
                    settings.edit().putString("vehicle_id", id).putString("endpoint", url).apply();
                    ui.post(() -> status.setText("Connection saved. Readings remain local when offline."));
                } catch (Exception e) { ui.post(() -> status.setText("Secure key storage unavailable")); }
            });
        });
        setContentView(scroll);
        applyInteractionLock();
        if (!getPackageManager().hasSystemFeature("android.hardware.type.automotive")) {
            status.setText("AAOS hardware feature unavailable"); return;
        }
        try {
            status.setText("Connecting to Car Service");
            car = Car.createCar(this, ui, Car.CAR_WAIT_TIMEOUT_DO_NOT_WAIT, carLifecycle);
        } catch (Exception e) { status.setText("Car Service unavailable: " + e.getClass().getSimpleName()); }
        applyInteractionLock();
        ui.post(poll);
    }

    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }
    private GradientDrawable surface(int fill, int stroke, int radiusDp) {
        GradientDrawable background = new GradientDrawable();
        background.setColor(fill);
        background.setStroke(dp(1), stroke);
        background.setCornerRadius(dp(radiusDp));
        return background;
    }
    private LinearLayout card(LinearLayout root) {
        LinearLayout panel = new LinearLayout(this);
        panel.setOrientation(LinearLayout.VERTICAL);
        panel.setPadding(dp(20), dp(16), dp(20), dp(16));
        panel.setBackground(surface(PANEL, BORDER, 6));
        LinearLayout.LayoutParams layout = new LinearLayout.LayoutParams(-1, -2);
        layout.bottomMargin = dp(12);
        root.addView(panel, layout);
        return panel;
    }
    private TextView title(LinearLayout root, String value, int sp, int color) {
        TextView view = new TextView(this); view.setText(value); view.setTextColor(color);
        view.setTextSize(sp); view.setPadding(0, dp(3), 0, dp(9)); root.addView(view); return view;
    }
    private EditText input(LinearLayout root, String hint, String value) {
        EditText edit = new EditText(this); edit.setHint(hint); edit.setText(value);
        edit.setSingleLine(true);
        edit.setTextColor(Color.WHITE); edit.setHintTextColor(MUTED);
        edit.setBackground(surface(INK, 0xFF36505A, 5));
        edit.setPadding(dp(14), 0, dp(14), 0);
        LinearLayout.LayoutParams layout = new LinearLayout.LayoutParams(-1, dp(76));
        layout.bottomMargin = dp(10);
        root.addView(edit, layout);
        return edit;
    }
    private void applyInteractionLock() {
        if (setupPanel == null) return;
        boolean locked = moving || uxRestricted;
        setupPanel.setVisibility(locked ? View.GONE : View.VISIBLE);
        sync.setText("LOCAL OUTBOX  " + queue.pending() + (locked ? " · SETUP LOCKED" : " · PARKED SETUP AVAILABLE"));
    }
    private Float read(int propertyId) {
        if (properties == null) return null;
        try { return properties.getFloatProperty(propertyId, 0); }
        catch (Exception ignored) { return null; }
    }
    private final Runnable poll = new Runnable() {
        @Override public void run() {
            io.execute(() -> {
                Float levelWh = read(VehiclePropertyIds.EV_BATTERY_LEVEL);
                Float capacityWh = read(VehiclePropertyIds.INFO_EV_BATTERY_CAPACITY);
                Float speedMps = read(VehiclePropertyIds.PERF_VEHICLE_SPEED);
                Float soc = levelWh != null && capacityWh != null && capacityWh > 0
                    ? Math.max(0, Math.min(100, levelWh * 100 / capacityWh)) : null;
                Float kmh = speedMps == null ? null : Math.abs(speedMps) * 3.6f;
                moving = kmh != null && kmh > 1;
                ui.post(() -> {
                    battery.setText("BATTERY  " + (soc == null ? "UNKNOWN" : Math.round(soc) + "%"));
                    speed.setText("SPEED  " + (kmh == null ? "UNKNOWN" : Math.round(kmh) + " km/h"));
                    applyInteractionLock();
                });
                if ((soc != null || kmh != null) && !settings.getString("vehicle_id", "").isEmpty()) {
                    try {
                        JSONObject frame = new JSONObject();
                        frame.put("schema_version", 1);
                        frame.put("vehicle_id", settings.getString("vehicle_id", ""));
                        frame.put("observed_at", Instant.now().toString());
                        frame.put("source", "CAR_API");
                        if (soc != null) frame.put("soc_pct", soc.doubleValue());
                        if (kmh != null) frame.put("speed_kmh", kmh.doubleValue());
                        queue.enqueue(frame);
                    } catch (Exception e) { ui.post(() -> status.setText("Local queue error: " + e.getMessage())); }
                }
                flush();
            });
            ui.postDelayed(this, 5000);
        }
    };

    private void flush() {
        String base = settings.getString("endpoint", "");
        String key;
        try { key = SecretStore.readKey(settings); }
        catch (Exception e) { ui.post(() -> status.setText("Secure key unavailable; re-enter while parked")); return; }
        if (base.isEmpty() || key.isEmpty()) return;
        if (!BuildConfig.DEBUG && !base.startsWith("https://")) return;
        for (int batchNumber = 0; batchNumber < 20; batchNumber++) {
            HttpURLConnection connection = null;
            try {
                JSONArray frames = queue.oldestBatch(100);
                if (frames.length() == 0) return;
                Set<Long> expected = new HashSet<>();
                for (int i = 0; i < frames.length(); i++) expected.add(frames.getJSONObject(i).getLong("sequence"));
                JSONObject body = new JSONObject(); body.put("frames", frames);
                connection = (HttpURLConnection) new URL(base + "/api/v1/telemetry/batches").openConnection();
                connection.setInstanceFollowRedirects(false);
                connection.setRequestMethod("POST"); connection.setConnectTimeout(3000); connection.setReadTimeout(3000);
                connection.setRequestProperty("Content-Type", "application/json");
                connection.setRequestProperty("X-Vehicle-Key", key); connection.setDoOutput(true);
                try (OutputStream output = connection.getOutputStream()) { output.write(body.toString().getBytes(StandardCharsets.UTF_8)); }
                if (connection.getResponseCode() != 200) return;
                ByteArrayOutputStream responseBytes = new ByteArrayOutputStream();
                try (InputStream input = connection.getInputStream()) {
                    byte[] buffer = new byte[4096];
                    int size;
                    while ((size = input.read(buffer)) != -1) {
                        if (responseBytes.size() + size > 64 * 1024) throw new IOException("Acknowledgement too large");
                        responseBytes.write(buffer, 0, size);
                    }
                }
                String response = responseBytes.toString(StandardCharsets.UTF_8.name());
                JSONArray acknowledged = new JSONObject(response).getJSONArray("acknowledged");
                Set<Long> confirmed = new HashSet<>();
                for (int i = 0; i < acknowledged.length(); i++) {
                    JSONObject ack = acknowledged.getJSONObject(i);
                    if (settings.getString("vehicle_id", "").equals(ack.getString("vehicle_id"))) confirmed.add(ack.getLong("sequence"));
                }
                if (!expected.equals(confirmed)) return;
                queue.acknowledge(confirmed);
                ui.post(this::applyInteractionLock);
            } catch (Exception ignored) { return; /* Offline: retain every unacknowledged frame. */ }
            finally { if (connection != null) connection.disconnect(); }
        }
    }

    @Override protected void onDestroy() {
        ui.removeCallbacks(poll);
        io.shutdownNow();
        if (restrictions != null) try { restrictions.unregisterListener(); } catch (Exception ignored) { }
        if (car != null) try { car.disconnect(); } catch (Exception ignored) { }
        queue.close();
        super.onDestroy();
    }
}
