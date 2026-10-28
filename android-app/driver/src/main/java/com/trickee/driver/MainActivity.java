package com.trickee.driver;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.graphics.drawable.StateListDrawable;
import android.location.Location;
import android.location.LocationListener;
import android.location.LocationManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.CancellationSignal;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.view.WindowManager;
import android.view.inputmethod.InputMethodManager;
import android.widget.Button;
import android.widget.ArrayAdapter;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Spinner;
import android.widget.TextView;
import java.util.UUID;
import java.util.Locale;
import java.util.HashSet;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import org.json.JSONArray;
import org.json.JSONObject;

/** Native driver trip, schedule and advisory companion. Never presents phone GPS as vehicle BMS data. */
public final class MainActivity extends Activity {
    private static final int LOCATION_PERMISSION = 101;
    private static final int CHARGER_PERMISSION = 102;
    private static final int ROUTE_PERMISSION = 103;
    private static final int INK = Color.rgb(3, 7, 11), PANEL = Color.rgb(12, 24, 31);
    private static final int YELLOW = Color.rgb(255, 224, 0), CYAN = Color.rgb(72, 223, 244);
    private static final int BORDER = Color.rgb(32, 48, 57), MUTED = Color.rgb(143, 163, 170);
    private final ExecutorService network = Executors.newSingleThreadExecutor();
    private final ExecutorService updatesNetwork = Executors.newSingleThreadExecutor();
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final Runnable queueRefresh = new Runnable() {
        @Override public void run() {
            network.execute(() -> {
                try (GpsQueue gps = new GpsQueue(MainActivity.this)) {
                    int count = gps.pendingCount();
                    runOnUiThread(() -> { queuedPoints = count; renderQueue(); });
                } catch (Exception ignored) { /* A failed read must not hide the trip screen. */ }
            });
            mainHandler.postDelayed(this, 5000);
        }
    };
    private final Runnable advisoryRefresh = new Runnable() {
        @Override public void run() {
            if (identity != null) {
                int generation = sessionGeneration;
                network.execute(() -> {
                    try {
                        DriverApi api = DriverApi.saved(MainActivity.this);
                        JSONObject me = (JSONObject) api.call("GET", "/api/v1/driver/me", null);
                        if (generation != sessionGeneration) return;
                        JSONArray inbox = (JSONArray) api.call("GET", "/api/v1/driver/nudges", null);
                        JSONArray proposals = (JSONArray) api.call("GET", "/api/v1/driver/dispatch-proposals", null);
                        JSONArray orders = (JSONArray) api.call("GET", "/api/v1/driver/internal-orders", null);
                        replayAdvisoryReplies(api, me, inbox, generation);
                        replayDispatchReplies(api, me, proposals, generation);
                        replayOrderActions(api, me, orders, generation);
                        replayServiceCommands(api, me, generation);
                        JSONArray updatedCases = (JSONArray) api.call("GET", "/api/v1/driver/service-cases", null);
                        runOnUiThread(() -> {
                            if (generation != sessionGeneration || identity == null
                                    || !identity.optString("driver_id").equals(me.optString("driver_id"))
                                    || !identity.optString("vehicle_id").equals(me.optString("vehicle_id"))) return;
                            nudgeData = inbox; dispatchData = proposals; orderData = orders;
                            serviceCaseData = updatedCases; render();
                        });
                    } catch (Exception ignored) { /* Offline choices remain visible for manual retry. */ }
                });
            }
            mainHandler.postDelayed(this, 60_000);
        }
    };
    private SharedPreferences prefs;
    private ScrollView mainScroll;
    private EditText originInput, keyInput, startSocInput, endSocInput, caseTitleInput, caseDescriptionInput;
    private Spinner caseCategory, casePriority;
    private TextView status, vehicle, battery, trip, queue, plansTitle, nudgesTitle, dispatchTitle, orderTitle,
            serviceTitle, historyTitle,
            consentStatus;
    private LinearLayout plans, routePanel, chargerPanel, nudges, dispatchOffers, orderPanel,
            serviceCasesPanel, history;
    private Button startTrip, startGps, stopGps, startWait, startCharge, endStop, completeTrip, sync,
            syncAdvisories, syncDispatch, syncOrders, reportServiceCase, syncServiceCases,
            grantConsent, withdrawConsent, erasePhoneDataButton;
    private JSONObject identity, activeTrip;
    private JSONArray planData = new JSONArray(), nudgeData = new JSONArray(),
            dispatchData = new JSONArray(), orderData = new JSONArray(),
            serviceCaseData = new JSONArray(), tripHistory = new JSONArray();
    private int queuedPoints;
    private volatile int sessionGeneration;
    private volatile int updatesEpoch;
    private volatile boolean foreground;
    private boolean updatesWatchRunning;
    private final HashSet<String> dispatchSeenInFlight = new HashSet<>();
    private int routeGeneration;
    private int chargerGeneration;
    private CancellationSignal phoneLocationLookup, routeLocationLookup;
    private LocationManager guidanceManager;
    private LocationListener guidanceListener;
    private RouteProgress guidanceProgress;
    private TextView guidanceStatus;
    private LinearLayout guidanceRouteBody;
    private int guidanceGeneration;
    private boolean guidanceRerouteInFlight;
    private boolean guidanceRerouteFailed;
    private boolean consentLoaded;
    private volatile boolean erasureInFlight;
    private double guidanceDestinationLat, guidanceDestinationLng;
    private Typeface bodyFont, headingFont, technicalFont;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_STATE_ALWAYS_HIDDEN);
        prefs = getSharedPreferences(DriverApi.PREFS, MODE_PRIVATE);
        bodyFont = font("fonts/manrope-latin.ttf", Typeface.DEFAULT);
        headingFont = font("fonts/syne-latin.ttf", Typeface.DEFAULT_BOLD);
        technicalFont = font("fonts/michroma-latin.ttf", Typeface.MONOSPACE);
        buildUi();
        originInput.setText(prefs.getString(DriverApi.ORIGIN, ""));
        if (state != null) {
            originInput.setText(state.getString("origin_draft", originInput.getText().toString()));
            startSocInput.setText(state.getString("start_soc_draft", ""));
            endSocInput.setText(state.getString("end_soc_draft", ""));
            int scrollY = state.getInt("scroll_y", 0);
            mainScroll.post(() -> mainScroll.scrollTo(0, scrollY));
        }
        GpsUploadWorker.scheduleRecovery(this);
        if (prefs.contains("driver_credential_sealed")) refresh();
    }

    @Override protected void onSaveInstanceState(Bundle state) {
        try {
            state.putString("origin_draft", DriverApi.validateOrigin(originInput.getText().toString()));
        } catch (IllegalArgumentException invalidOrigin) {
            // Do not serialize a partly typed URL that might contain credentials.
        }
        state.putString("start_soc_draft", startSocInput.getText().toString());
        state.putString("end_soc_draft", endSocInput.getText().toString());
        state.putInt("scroll_y", mainScroll.getScrollY());
        // A typed driver credential is deliberately excluded from saved UI state.
        super.onSaveInstanceState(state);
    }

    @Override protected void onResume() {
        super.onResume();
        foreground = true;
        mainHandler.post(queueRefresh);
        mainHandler.postDelayed(advisoryRefresh, 60_000);
        if (identity != null) { refresh(); startUpdates(); }
    }

    @Override protected void onPause() {
        foreground = false;
        stopUpdates();
        mainHandler.removeCallbacks(queueRefresh);
        mainHandler.removeCallbacks(advisoryRefresh);
        stopGuidance();
        ++chargerGeneration;
        ++routeGeneration;
        cancelPhoneLocationLookup();
        cancelRouteLocationLookup();
        super.onPause();
    }

    private void buildUi() {
        ScrollView scroll = new ScrollView(this); scroll.setFillViewport(true); scroll.setBackgroundColor(INK);
        mainScroll = scroll;
        LinearLayout root = new LinearLayout(this); root.setOrientation(LinearLayout.VERTICAL);
        root.setFocusableInTouchMode(true);
        root.setPadding(dp(20), dp(24), dp(20), dp(40)); scroll.addView(root);
        root.addView(label("TRICKEE / DRIVER", 12, CYAN));
        root.addView(label("Drive with clarity.", 31, YELLOW));
        root.addView(label("Phone GPS and motion, driver reports, and vehicle telemetry stay separate.", 14, Color.LTGRAY));

        LinearLayout access = card(root, "01 / CONNECT");
        originInput = input(access, "HTTPS server origin", false);
        keyInput = input(access, "Driver credential", true);
        keyInput.setSaveEnabled(false);
        action(access, "Connect", this::connect);
        action(access, "Disconnect", this::disconnect);
        status = label("Connect with a fleet-issued credential.", 13, CYAN); access.addView(status);

        LinearLayout assigned = card(root, "02 / ASSIGNED VEHICLE");
        vehicle = label("No vehicle connected", 20, YELLOW); assigned.addView(vehicle);
        battery = label("Battery: — · vehicle telemetry unavailable", 13, Color.LTGRAY); assigned.addView(battery);
        action(assigned, "Refresh", this::refresh);
        action(assigned, "Personal workspace (parked)", () ->
                startActivity(new Intent(this, WorkspaceActivity.class)));

        LinearLayout tripCard = card(root, "03 / TRIP CONTROL");
        trip = label("No active trip", 18, YELLOW); tripCard.addView(trip);
        consentStatus = label("Connect to review phone data consent.", 13, CYAN);
        tripCard.addView(consentStatus);
        tripCard.addView(label("If you agree, active-trip tracking collects phone GPS and motion windows for your fleet. Withdrawal stops collection. Queued points remain on this device for recovery or later deletion.", 12, Color.LTGRAY));
        grantConsent = action(tripCard, "Agree to trip GPS and motion", this::grantPhoneConsent);
        withdrawConsent = action(tripCard, "Withdraw phone data consent", this::withdrawPhoneConsent);
        startSocInput = input(tripCard, "Starting dashboard charge % (optional)", false);
        startTrip = action(tripCard, "Start trip", this::beginTrip);
        startGps = action(tripCard, "Start phone GPS", this::requestGps);
        stopGps = action(tripCard, "Stop phone GPS", this::stopGps);
        startWait = action(tripCard, "Start waiting stop", () -> beginStop("WAITING"));
        startCharge = action(tripCard, "Report charging stop", () -> beginStop("CHARGING"));
        endStop = action(tripCard, "End reported stop", this::finishStop);
        endSocInput = input(tripCard, "Ending dashboard charge % (optional)", false);
        completeTrip = action(tripCard, "Complete trip", this::finishTrip);
        queue = label("Queued phone GPS: 0", 13, CYAN); tripCard.addView(queue);
        sync = action(tripCard, "Sync queued GPS", () -> {
            GpsUploadWorker.retryNow(this); message("Sync requested. Queued GPS will upload when the server is reachable.");
        });
        tripCard.addView(label("Phone location and raw motion are collected only during active trip tracking. A persistent notification stays visible until you stop. Neither phone data nor driver-reported stops are BMS evidence.", 12, Color.LTGRAY));

        LinearLayout schedule = card(root, "04 / CONFIRMED SCHEDULE");
        plansTitle = label("No schedule loaded", 14, CYAN); schedule.addView(plansTitle);
        plans = new LinearLayout(this); plans.setOrientation(LinearLayout.VERTICAL); schedule.addView(plans);
        schedule.addView(label("Road previews begin at the saved plan origin. They are provider baselines, not live navigation or traffic.", 12, Color.LTGRAY));
        routePanel = new LinearLayout(this); routePanel.setOrientation(LinearLayout.VERTICAL); schedule.addView(routePanel);
        action(schedule, "Find chargers near my phone", this::findPhoneChargers);
        chargerPanel = new LinearLayout(this); chargerPanel.setOrientation(LinearLayout.VERTICAL); schedule.addView(chargerPanel);

        LinearLayout phoneData = card(root, "05 / YOUR PHONE DATA");
        phoneData.addView(label("Erase cloud trip GPS and motion after completing every trip. This also clears queued points for this connected vehicle and driver on this phone. Other devices and backups need separate cleanup.", 12, Color.LTGRAY));
        erasePhoneDataButton = action(phoneData, "Erase phone data", this::confirmErasePhoneData);

        LinearLayout inbox = card(root, "06 / FLEET ADVISORIES");
        nudgesTitle = label("No inbox loaded", 14, CYAN); inbox.addView(nudgesTitle);
        nudges = new LinearLayout(this); nudges.setOrientation(LinearLayout.VERTICAL); inbox.addView(nudges);
        syncAdvisories = action(inbox, "Sync advisory replies", this::refresh);
        inbox.addView(label("Queued replies remain on this phone until the server confirms them. Pending means stored in your inbox; there is no push delivery receipt.", 12, Color.LTGRAY));

        LinearLayout dispatchCard = card(root, "07 / PICKUP PROPOSALS");
        dispatchTitle = label("No pickup proposals loaded", 14, CYAN); dispatchCard.addView(dispatchTitle);
        dispatchCard.addView(label("Manual proposals from your fleet. Check both pins and the message. Accepting records a decision; it does not start a trip, create a partner order or control the vehicle.", 12, Color.LTGRAY));
        dispatchOffers = new LinearLayout(this); dispatchOffers.setOrientation(LinearLayout.VERTICAL);
        dispatchCard.addView(dispatchOffers);
        syncDispatch = action(dispatchCard, "Sync pickup decisions", this::refresh);
        dispatchCard.addView(label("Offline decisions stay on this phone until the server confirms them. Expired or withdrawn proposals require review.", 12, Color.LTGRAY));

        LinearLayout orderCard = card(root, "08 / INTERNAL ASSIGNMENTS");
        orderTitle = label("No assignments loaded", 14, CYAN); orderCard.addView(orderTitle);
        orderCard.addView(label("Your fleet confirms an accepted proposal before it becomes an internal assignment. Pickup and delivery are your reports. They do not verify a partner order, and the fleet must reconcile delivery separately.", 12, Color.LTGRAY));
        orderPanel = new LinearLayout(this); orderPanel.setOrientation(LinearLayout.VERTICAL);
        orderCard.addView(orderPanel);
        syncOrders = action(orderCard, "Sync assignment reports", this::refresh);
        orderCard.addView(label("An offline report stays on this phone until confirmed by the server. Report delivery only after pickup has been confirmed.", 12, Color.LTGRAY));

        LinearLayout serviceCard = card(root, "09 / SERVICE CASES");
        serviceCard.addView(label("Report a vehicle, charger, route or trip issue to your fleet. For immediate danger use local emergency or roadside channels; this inbox needs online staff.", 12, Color.LTGRAY));
        caseCategory = new Spinner(this);
        ArrayAdapter<String> categories = serviceSpinnerAdapter(new String[] {
                "Vehicle fault", "Charger problem", "Route problem", "Trip assistance", "Other"});
        categories.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);
        caseCategory.setAdapter(categories); caseCategory.setContentDescription("Issue type");
        caseCategory.setBackground(shape(INK, BORDER)); serviceCard.addView(caseCategory);
        casePriority = new Spinner(this);
        ArrayAdapter<String> priorities = serviceSpinnerAdapter(new String[] {"Routine", "Urgent"});
        priorities.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);
        casePriority.setAdapter(priorities); casePriority.setContentDescription("Issue priority");
        casePriority.setBackground(shape(INK, BORDER)); serviceCard.addView(casePriority);
        caseTitleInput = input(serviceCard, "Short issue title", false);
        caseTitleInput.setFilters(new android.text.InputFilter[] {new android.text.InputFilter.LengthFilter(120)});
        caseDescriptionInput = input(serviceCard, "What happened and what help do you need?", false);
        caseDescriptionInput.setSingleLine(false); caseDescriptionInput.setMinLines(3);
        caseDescriptionInput.setFilters(new android.text.InputFilter[] {new android.text.InputFilter.LengthFilter(1000)});
        caseDescriptionInput.getLayoutParams().height = dp(105);
        reportServiceCase = action(serviceCard, "Report issue", this::reportIssue);
        serviceTitle = label("No service cases loaded", 14, CYAN); serviceCard.addView(serviceTitle);
        serviceCasesPanel = new LinearLayout(this); serviceCasesPanel.setOrientation(LinearLayout.VERTICAL);
        serviceCard.addView(serviceCasesPanel);
        syncServiceCases = action(serviceCard, "Sync saved reports", this::refresh);
        serviceCard.addView(label("Issue reports and follow-ups are saved on this phone before upload. A queued report is not yet visible to your fleet.", 12, Color.LTGRAY));

        LinearLayout pastTrips = card(root, "10 / RECENT TRIPS");
        historyTitle = label("No trip history loaded", 14, CYAN); pastTrips.addView(historyTitle);
        history = new LinearLayout(this); history.setOrientation(LinearLayout.VERTICAL); pastTrips.addView(history);
        setContentView(scroll);
        root.requestFocus();
        render();
        if (android.animation.ValueAnimator.areAnimatorsEnabled()) {
            scroll.setAlpha(0f);
            scroll.animate().alpha(1f).setDuration(420).start();
        }
    }

    private Typeface font(String path, Typeface fallback) {
        try { return Typeface.createFromAsset(getAssets(), path); }
        catch (RuntimeException missing) { return fallback; }
    }

    private ArrayAdapter<String> serviceSpinnerAdapter(String[] labels) {
        return new ArrayAdapter<String>(this, android.R.layout.simple_spinner_item, labels) {
            @Override public View getView(int position, View convertView, android.view.ViewGroup parent) {
                View view = super.getView(position, convertView, parent);
                ((TextView) view).setTextColor(Color.WHITE);
                ((TextView) view).setPadding(dp(14), dp(12), dp(14), dp(12));
                return view;
            }
            @Override public View getDropDownView(int position, View convertView,
                                                   android.view.ViewGroup parent) {
                View view = super.getDropDownView(position, convertView, parent);
                ((TextView) view).setTextColor(INK);
                return view;
            }
        };
    }

    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }

    private GradientDrawable shape(int fill, int border) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(fill); drawable.setCornerRadius(dp(10));
        drawable.setStroke(dp(1), border);
        return drawable;
    }

    private LinearLayout card(LinearLayout root, String title) {
        LinearLayout panel = new LinearLayout(this); panel.setOrientation(LinearLayout.VERTICAL);
        panel.setPadding(dp(18), dp(17), dp(18), dp(17));
        panel.setBackground(shape(PANEL, BORDER)); panel.setElevation(dp(2));
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, -2);
        params.topMargin = dp(14); root.addView(panel, params);
        panel.addView(label(title, 12, YELLOW)); return panel;
    }

    private TextView label(String text, int size, int color) {
        TextView view = new TextView(this); view.setText(text); view.setTextSize(size); view.setTextColor(color);
        view.setTypeface(text.equals("Drive with clarity.") ? headingFont
                : text.matches("^(TRICKEE|[0-9][0-9] /).*" ) ? technicalFont : bodyFont);
        view.setPadding(0, dp(7), 0, dp(7));
        view.setLineSpacing(dp(2), 1f); return view;
    }

    private EditText input(LinearLayout parent, String hint, boolean secret) {
        EditText field = new EditText(this); field.setSingleLine(true); field.setHint(hint);
        field.setHintTextColor(MUTED); field.setTextColor(Color.WHITE); field.setTypeface(bodyFont);
        field.setBackground(shape(INK, BORDER));
        field.setPadding(dp(14), dp(10), dp(14), dp(10));
        if (secret) field.setInputType(129);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, dp(54));
        params.topMargin = dp(8); parent.addView(field, params); return field;
    }

    private Button action(LinearLayout parent, String title, Runnable run) {
        Button button = new Button(this); button.setText(title); button.setAllCaps(false);
        boolean primary = title.equals("Connect") || title.equals("Start trip") || title.equals("Complete trip")
                || title.equals("Start phone GPS") || title.equals("Acknowledge");
        StateListDrawable states = new StateListDrawable();
        states.addState(new int[] {-android.R.attr.state_enabled}, shape(Color.rgb(22, 35, 42), BORDER));
        states.addState(new int[] {}, shape(primary ? YELLOW : PANEL, primary ? YELLOW : CYAN));
        button.setBackground(new RippleDrawable(android.content.res.ColorStateList.valueOf(
                primary ? Color.rgb(210, 180, 0) : Color.rgb(41, 112, 125)), states, null));
        button.setTextColor(new android.content.res.ColorStateList(
                new int[][] {new int[] {-android.R.attr.state_enabled}, new int[] {}},
                new int[] {MUTED, primary ? INK : Color.WHITE}));
        button.setTypeface(bodyFont, Typeface.BOLD);
        button.setMinHeight(dp(54));
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, dp(54)); params.topMargin = dp(8);
        parent.addView(button, params); button.setOnClickListener(view -> run.run()); return button;
    }

    private void message(String value) { runOnUiThread(() -> status.setText(value)); }

    private void dismissKeyboard() {
        View focused = getCurrentFocus();
        if (focused == null) return;
        InputMethodManager keyboard = (InputMethodManager) getSystemService(INPUT_METHOD_SERVICE);
        keyboard.hideSoftInputFromWindow(focused.getWindowToken(), 0);
        focused.clearFocus();
    }

    private void task(Work work) {
        network.execute(() -> {
            try { work.run(); }
            catch (Exception exception) {
                runOnUiThread(this::render);
                message(exception.getMessage() == null ? "Operation failed" : exception.getMessage());
            }
        });
    }
    private interface Work { void run() throws Exception; }

    private void stopUpdates() {
        ++updatesEpoch;
        updatesWatchRunning = false;
    }

    private void startUpdates() {
        if (!foreground || identity == null || updatesWatchRunning) return;
        final int epoch = ++updatesEpoch, generation = sessionGeneration;
        updatesWatchRunning = true;
        updatesNetwork.execute(() -> {
            int cursor = 0;
            try {
                while (foreground && epoch == updatesEpoch && generation == sessionGeneration
                        && !Thread.currentThread().isInterrupted()) {
                    try {
                        JSONObject batch = DriverApi.saved(this).updates(cursor);
                        if (!foreground || epoch != updatesEpoch || generation != sessionGeneration) break;
                        if (batch.optBoolean("reset_required")
                                || batch.optJSONArray("events") != null
                                && batch.optJSONArray("events").length() > 0) {
                            Future<?> snapshot = network.submit(() -> {
                                try { refreshNow(generation); }
                                catch (Exception failure) { throw new RuntimeException(failure); }
                            });
                            snapshot.get();
                        }
                        cursor = batch.optInt("next_cursor", cursor);
                    } catch (Exception failure) {
                        if (!foreground || epoch != updatesEpoch || generation != sessionGeneration
                                || Thread.currentThread().isInterrupted()) break;
                        Throwable cause = failure;
                        while (cause != null && !(cause instanceof DriverApi.ApiFailure)) cause = cause.getCause();
                        if (cause instanceof DriverApi.ApiFailure
                                && ((DriverApi.ApiFailure) cause).status == 401) {
                            runOnUiThread(() -> {
                                if (epoch == updatesEpoch && generation == sessionGeneration)
                                    disconnect();
                            });
                            break;
                        }
                        try { Thread.sleep(1500); }
                        catch (InterruptedException interrupted) { Thread.currentThread().interrupt(); break; }
                    }
                }
            } finally {
                runOnUiThread(() -> { if (epoch == updatesEpoch) updatesWatchRunning = false; });
            }
        });
    }

    private void connect() {
        String origin = originInput.getText().toString().trim(), key = keyInput.getText().toString().trim();
        dismissKeyboard();
        int generation = ++sessionGeneration;
        stopUpdates();
        dispatchSeenInFlight.clear();
        ++routeGeneration;
        stopGuidance();
        ++chargerGeneration;
        cancelPhoneLocationLookup();
        cancelRouteLocationLookup();
        stopGps();
        SecretStore.clear(prefs);
        identity = null; consentLoaded = false; activeTrip = null; planData = new JSONArray();
        nudgeData = new JSONArray(); dispatchData = new JSONArray(); orderData = new JSONArray();
        serviceCaseData = new JSONArray();
        tripHistory = new JSONArray(); routePanel.removeAllViews(); chargerPanel.removeAllViews(); render();
        task(() -> {
            DriverApi api = new DriverApi(origin, key);
            JSONObject me = (JSONObject) api.call("GET", "/api/v1/driver/me", null);
            if (generation != sessionGeneration) return;
            String previousDriver = prefs.getString("last_driver", ""), previousVehicle = prefs.getString("last_vehicle", "");
            boolean assignmentChanged = !previousDriver.equals(me.optString("driver_id"))
                    || !previousVehicle.equals(me.optString("vehicle_id"));
            if (assignmentChanged) {
                SharedPreferences.Editor reset = prefs.edit();
                for (String name : prefs.getAll().keySet())
                    if (name.startsWith("pending_start") || name.startsWith("pending_end_") || name.startsWith("pending_stop_"))
                        reset.remove(name);
                if (!reset.commit()) throw new IllegalStateException("pending command reset failed");
            }
            SecretStore.save(prefs, key);
            if (!prefs.edit().putString(DriverApi.ORIGIN, DriverApi.validateOrigin(origin))
                    .putString("last_driver", me.optString("driver_id"))
                    .putString("last_vehicle", me.optString("vehicle_id")).commit())
                throw new IllegalStateException("server storage failed");
            runOnUiThread(() -> { if (generation == sessionGeneration) {
                identity = me; keyInput.setText("");
                if (assignmentChanged) { startSocInput.setText(""); endSocInput.setText(""); }
                render();
            } });
            load(api, me, generation);
        });
    }

    private void disconnect() {
        ++sessionGeneration;
        stopUpdates();
        dispatchSeenInFlight.clear();
        ++routeGeneration;
        stopGuidance();
        ++chargerGeneration;
        cancelPhoneLocationLookup();
        cancelRouteLocationLookup();
        stopGps();
        task(() -> {
            SecretStore.clear(prefs);
            runOnUiThread(() -> {
                identity = null; consentLoaded = false; activeTrip = null; planData = new JSONArray();
                nudgeData = new JSONArray(); dispatchData = new JSONArray(); orderData = new JSONArray();
                serviceCaseData = new JSONArray();
                tripHistory = new JSONArray();
                startSocInput.setText(""); endSocInput.setText(""); keyInput.setText("");
                routePanel.removeAllViews(); chargerPanel.removeAllViews();
                render(); message("Disconnected. Queued GPS, advisories, pickup decisions, assignment reports and service cases remain on this device.");
            });
        });
    }

    private void refresh() {
        int generation = sessionGeneration;
        task(() -> refreshNow(generation));
    }

    private void refreshNow(int generation) throws Exception {
        DriverApi api = DriverApi.saved(this);
        load(api, (JSONObject) api.call("GET", "/api/v1/driver/me", null), generation);
    }

    private void load(DriverApi api, JSONObject me, int generation) throws Exception {
        JSONArray trips = (JSONArray) api.call("GET", "/api/v1/driver/trips", null);
        JSONArray confirmed = (JSONArray) api.call("GET", "/api/v1/driver/daily-plans", null);
        JSONArray inbox = (JSONArray) api.call("GET", "/api/v1/driver/nudges", null);
        JSONArray proposals = (JSONArray) api.call("GET", "/api/v1/driver/dispatch-proposals", null);
        JSONArray orders = (JSONArray) api.call("GET", "/api/v1/driver/internal-orders", null);
        JSONObject consent = (JSONObject) api.call("GET", "/api/v1/driver/consent/phone-trip", null);
        String tenantId = me.optString("tenant_id"), driverId = me.optString("driver_id");
        if (generation != sessionGeneration) return;
        replayAdvisoryReplies(api, me, inbox, generation);
        replayDispatchReplies(api, me, proposals, generation);
        replayOrderActions(api, me, orders, generation);
        replayServiceCommands(api, me, generation);
        JSONArray cases = (JSONArray) api.call("GET", "/api/v1/driver/service-cases", null);
        if (PhoneTripConsent.isPending(this, tenantId, driverId)) {
            try {
                api.call("POST", "/api/v1/driver/consent/phone-trip",
                        new JSONObject().put("granted", false));
                if (!PhoneTripConsent.record(this, tenantId, driverId, false))
                    throw new IllegalStateException("consent state storage failed");
            } catch (Exception pending) {
                // Keep the local gate closed; a later refresh retries server withdrawal.
            }
        } else if (!PhoneTripConsent.record(this, tenantId, driverId, consent.optBoolean("granted"))) {
            throw new IllegalStateException("consent state storage failed");
        }
        JSONObject active = null;
        for (int index = 0; index < trips.length(); index++) {
            JSONObject candidate = trips.getJSONObject(index);
            if ("ACTIVE".equals(candidate.optString("status"))) { active = candidate; break; }
        }
        int count;
        try (GpsQueue gps = new GpsQueue(this)) { count = gps.pendingCount(); }
        JSONObject activeResult = active;
        JSONArray finalCases = cases;
        runOnUiThread(() -> {
            if (generation != sessionGeneration) return;
            identity = me; activeTrip = activeResult; planData = confirmed; nudgeData = inbox;
            dispatchData = proposals; orderData = orders; serviceCaseData = finalCases;
            tripHistory = trips; queuedPoints = count; consentLoaded = true;
            if (!PhoneTripConsent.isGranted(this, tenantId, driverId)) stopGps();
            render(); message("Connected as " + me.optString("driver_id") + ". Phone GPS remains separate from vehicle signals.");
            startUpdates();
        });
        if (count > 0 && generation == sessionGeneration
                && PhoneTripConsent.isGranted(this, tenantId, driverId)) GpsUploadWorker.schedule(this);
    }

    private void render() {
        boolean connected = identity != null, active = activeTrip != null;
        String tenantId = connected ? identity.optString("tenant_id") : "";
        String driverId = connected ? identity.optString("driver_id") : "";
        boolean consentPending = connected && PhoneTripConsent.isPending(this, tenantId, driverId);
        boolean consentGranted = connected && consentLoaded
                && PhoneTripConsent.isGranted(this, tenantId, driverId);
        consentStatus.setText(!connected ? "Connect to review phone data consent."
                : consentPending ? "Consent withdrawal pending server confirmation. GPS remains off."
                : !consentLoaded ? "Checking trip phone-data consent with the server."
                : consentGranted ? "Trip phone GPS and motion consent granted."
                : "Trip phone GPS and motion consent is not granted.");
        grantConsent.setEnabled(connected && consentLoaded && !consentPending && !consentGranted);
        withdrawConsent.setEnabled(connected && (consentPending || consentGranted));
        withdrawConsent.setText(consentPending ? "Retry consent withdrawal" : "Withdraw phone data consent");
        vehicle.setText(connected ? identity.optString("vehicle_id") : "No vehicle connected");
        JSONObject twin = connected ? identity.optJSONObject("twin") : null;
        JSONObject pack = twin == null ? null : twin.optJSONObject("battery");
        battery.setText(pack != null && !pack.isNull("soc_pct")
                ? "Vehicle battery: " + pack.optString("soc_pct") + "% · " + twin.optString("quality")
                : "Battery: — · vehicle telemetry unavailable");
        trip.setText(active ? activeTrip.optString("trip_id") + " · ACTIVE" : "No active trip");
        JSONObject openStop = active ? activeTrip.optJSONObject("active_stop") : null;
        boolean stopped = openStop != null;
        startTrip.setEnabled(connected && !active); startGps.setEnabled(connected && active && consentGranted);
        stopGps.setEnabled(connected && active);
        startWait.setEnabled(active && !stopped); startCharge.setEnabled(active && !stopped);
        endStop.setEnabled(active && stopped); completeTrip.setEnabled(active && !stopped);
        startSocInput.setVisibility(active ? View.GONE : View.VISIBLE);
        startTrip.setVisibility(active ? View.GONE : View.VISIBLE);
        startGps.setVisibility(active ? View.VISIBLE : View.GONE);
        stopGps.setVisibility(active ? View.VISIBLE : View.GONE);
        startWait.setVisibility(active && !stopped ? View.VISIBLE : View.GONE);
        startCharge.setVisibility(active && !stopped ? View.VISIBLE : View.GONE);
        endStop.setVisibility(active && stopped ? View.VISIBLE : View.GONE);
        endSocInput.setVisibility(active && !stopped ? View.VISIBLE : View.GONE);
        completeTrip.setVisibility(active && !stopped ? View.VISIBLE : View.GONE);
        sync.setEnabled(consentGranted); renderQueue();
        erasePhoneDataButton.setEnabled(connected && !active && !erasureInFlight);
        plans.removeAllViews();
        plansTitle.setText(connected ? planData.length() + " confirmed plan(s)" : "Connect to review your schedule");
        for (int index = 0; index < planData.length(); index++) {
            JSONObject plan = planData.optJSONObject(index); if (plan == null) continue;
            plans.addView(label(plan.optString("service_date") + " · " + plan.optString("timezone"), 16, YELLOW));
            JSONObject origin = plan.optJSONObject("origin");
            double startLat = origin == null ? Double.NaN : origin.optDouble("latitude", Double.NaN);
            double startLng = origin == null ? Double.NaN : origin.optDouble("longitude", Double.NaN);
            if (validPoint(startLat, startLng))
                action(plans, "Check chargers near plan origin", () -> checkPlanChargers(startLat, startLng));
            JSONArray stops = plan.optJSONArray("stops"); if (stops == null) continue;
            for (int stop = 0; stop < stops.length(); stop++) {
                JSONObject item = stops.optJSONObject(stop); if (item == null) continue;
                plans.addView(label(item.optString("requested_arrival_local") + " · " + item.optString("label"), 13, Color.WHITE));
                JSONObject coordinates = item.optJSONObject("coordinates");
                if (coordinates != null) {
                    double latitude = coordinates.optDouble("latitude", Double.NaN);
                    double longitude = coordinates.optDouble("longitude", Double.NaN);
                    if (validPoint(latitude, longitude)) {
                        action(plans, "Open directions in Maps", () -> openDirections(latitude, longitude));
                        if (validPoint(startLat, startLng))
                            action(plans, "Preview road route to " + item.optString("label"),
                                    () -> previewRoute(item.optString("label"), startLat, startLng, latitude, longitude));
                        action(plans, "Preview from my phone to " + item.optString("label"),
                                () -> previewRouteFromPhone(item.optString("label"), latitude, longitude));
                    }
                }
            }
        }
        nudges.removeAllViews();
        AdvisoryReplyQueue replies = new AdvisoryReplyQueue(this);
        String origin = prefs.getString(DriverApi.ORIGIN, "");
        int queuedReplies = 0;
        JSONArray localReplies = new JSONArray();
        if (connected) {
            try { localReplies = replies.forOwner(origin, identity); queuedReplies = localReplies.length(); }
            catch (Exception queueError) { nudges.addView(label("Advisory queue needs recovery: " + queueError.getMessage(), 13, YELLOW)); }
        }
        syncAdvisories.setEnabled(connected);
        nudgesTitle.setText(connected ? nudgeData.length() + " stored advisories · "
                + queuedReplies + " replies on phone" : "Connect to review your inbox");
        HashSet<String> visibleAdvisories = new HashSet<>();
        for (int index = 0; index < nudgeData.length(); index++) {
            JSONObject nudge = nudgeData.optJSONObject(index); if (nudge == null) continue;
            String id = nudge.optString("nudge_id");
            visibleAdvisories.add(id);
            JSONObject local = null;
            try { if (connected) local = replies.find(origin, identity, id); }
            catch (Exception queueError) { nudges.addView(label("Advisory queue needs recovery", 13, YELLOW)); }
            nudges.addView(label(nudge.optString("kind") + " · SERVER " + nudge.optString("status")
                    + (local == null ? "" : " · PHONE " + local.optString("state") + " "
                    + local.optString("outcome")), 14, YELLOW));
            nudges.addView(label(nudge.optString("message"), 13, Color.WHITE));
            if (local != null && "NEEDS_REVIEW".equals(local.optString("state"))) {
                nudges.addView(label("Server rejected this reply (HTTP " + local.optInt("http_status")
                        + "). Review it with your fleet before retrying.", 12, YELLOW));
                action(nudges, "Retry reviewed reply", () -> retryAdvisoryReply(id));
                action(nudges, "Clear reviewed local reply", () -> clearReviewedAdvisoryReply(id));
            }
            if (local == null && "PENDING".equals(nudge.optString("status"))) {
                action(nudges, "Acknowledge", () -> respond(id, "ACKNOWLEDGED"));
                action(nudges, "Decline", () -> respond(id, "DECLINED"));
            }
        }
        for (int index = 0; index < localReplies.length(); index++) {
            JSONObject local = localReplies.optJSONObject(index);
            if (local == null || visibleAdvisories.contains(local.optString("nudge_id"))) continue;
            String id = local.optString("nudge_id");
            nudges.addView(label("PHONE " + local.optString("state") + " " + local.optString("outcome")
                    + " · " + id, 14, YELLOW));
            nudges.addView(label("This advisory is absent from the current server inbox. Review it with your fleet before clearing the local choice.", 12, MUTED));
            action(nudges, "Clear reviewed local reply", () -> clearReviewedAdvisoryReply(id));
        }
        if (queuedReplies > 0) nudges.addView(label("Use Sync advisory replies to retry. Choices for another driver or vehicle are kept separate.", 12, CYAN));
        dispatchOffers.removeAllViews();
        DispatchReplyQueue decisions = new DispatchReplyQueue(this);
        JSONArray localDecisions = new JSONArray();
        int queuedDecisions = 0;
        if (connected) {
            try { localDecisions = decisions.forOwner(origin, identity); queuedDecisions = localDecisions.length(); }
            catch (Exception queueError) {
                dispatchOffers.addView(label("Pickup decision queue needs recovery: " + queueError.getMessage(), 13, YELLOW));
            }
        }
        syncDispatch.setEnabled(connected);
        dispatchTitle.setText(connected ? dispatchData.length() + " stored proposals · "
                + queuedDecisions + " decisions on phone" : "Connect to review pickup proposals");
        HashSet<String> visibleOffers = new HashSet<>();
        for (int index = 0; index < dispatchData.length(); index++) {
            JSONObject offer = dispatchData.optJSONObject(index); if (offer == null) continue;
            String id = offer.optString("offer_id"), serverStatus = offer.optString("status");
            visibleOffers.add(id);
            JSONObject local = null;
            try { if (connected) local = decisions.find(origin, identity, id); }
            catch (Exception queueError) {
                dispatchOffers.addView(label("Pickup decision queue needs recovery", 13, YELLOW));
            }
            dispatchOffers.addView(label(offer.optString("pickup_label") + " → "
                    + offer.optString("destination_label") + " · SERVER " + serverStatus
                    + (local == null ? "" : " · PHONE " + local.optString("state") + " "
                    + local.optString("outcome")), 14, YELLOW));
            dispatchOffers.addView(label(offer.optString("message"), 13, Color.WHITE));
            dispatchOffers.addView(label("Expires " + offer.optString("expires_at")
                    + (offer.isNull("external_reference") ? "" : " · reference " + offer.optString("external_reference")),
                    12, MUTED));
            JSONObject pickup = offer.optJSONObject("pickup"), destination = offer.optJSONObject("destination");
            if (pickup != null) {
                double lat = pickup.optDouble("latitude", Double.NaN), lng = pickup.optDouble("longitude", Double.NaN);
                if (validPoint(lat, lng)) {
                    dispatchOffers.addView(label("Pickup pin: " + lat + ", " + lng, 12, CYAN));
                    action(dispatchOffers, "Open pickup directions", () -> openDirections(lat, lng));
                }
            }
            if (destination != null) {
                double lat = destination.optDouble("latitude", Double.NaN), lng = destination.optDouble("longitude", Double.NaN);
                if (validPoint(lat, lng)) {
                    dispatchOffers.addView(label("Destination pin: " + lat + ", " + lng, 12, CYAN));
                    action(dispatchOffers, "Open destination directions", () -> openDirections(lat, lng));
                }
            }
            if (connected && "PENDING".equals(serverStatus) && offer.isNull("seen_at")
                    && dispatchSeenInFlight.add(id)) markDispatchSeen(id);
            if (local != null && "NEEDS_REVIEW".equals(local.optString("state"))) {
                dispatchOffers.addView(label("Server rejected this decision (HTTP " + local.optInt("http_status")
                        + "). Review the current outcome before retrying or clearing it.", 12, YELLOW));
                action(dispatchOffers, "Retry reviewed decision", () -> retryDispatchReply(id));
                action(dispatchOffers, "Clear reviewed local decision", () -> clearReviewedDispatchReply(id));
            }
            if (local == null && "PENDING".equals(serverStatus)) {
                action(dispatchOffers, "Accept proposal", () -> respondDispatch(id, "ACCEPTED"));
                action(dispatchOffers, "Decline proposal", () -> respondDispatch(id, "DECLINED"));
            }
        }
        for (int index = 0; index < localDecisions.length(); index++) {
            JSONObject local = localDecisions.optJSONObject(index);
            if (local == null || visibleOffers.contains(local.optString("offer_id"))) continue;
            String id = local.optString("offer_id");
            dispatchOffers.addView(label("PHONE " + local.optString("state") + " "
                    + local.optString("outcome") + " · " + id, 14, YELLOW));
            dispatchOffers.addView(label("This proposal is absent from the current server inbox. Review it with your fleet before clearing the local decision.", 12, MUTED));
            action(dispatchOffers, "Clear reviewed local decision", () -> clearReviewedDispatchReply(id));
        }
        if (queuedDecisions > 0)
            dispatchOffers.addView(label("Use Sync pickup decisions to retry. Decisions for another driver or vehicle stay separate.", 12, CYAN));
        orderPanel.removeAllViews();
        OrderActionQueue orderQueue = new OrderActionQueue(this);
        JSONArray localOrders = new JSONArray();
        if (connected) {
            try { localOrders = orderQueue.forOwner(origin, identity); }
            catch (Exception queueError) {
                orderPanel.addView(label("Assignment action queue needs recovery: "
                        + queueError.getMessage(), 13, YELLOW));
            }
        }
        syncOrders.setEnabled(connected);
        orderTitle.setText(connected ? orderData.length() + " stored assignments · "
                + localOrders.length() + " reports on phone" : "Connect to review assignments");
        HashSet<String> visibleOrders = new HashSet<>();
        for (int index = 0; index < orderData.length(); index++) {
            JSONObject order = orderData.optJSONObject(index); if (order == null) continue;
            String id = order.optString("order_id"), serverStatus = order.optString("status");
            visibleOrders.add(id);
            JSONObject local = null;
            try { if (connected) local = orderQueue.find(origin, identity, id); }
            catch (Exception queueError) {
                orderPanel.addView(label("Assignment action queue needs recovery", 13, YELLOW));
            }
            orderPanel.addView(label(order.optString("pickup_label") + " → "
                    + order.optString("destination_label") + " · SERVER " + serverStatus
                    + (local == null ? "" : " · PHONE " + local.optString("state") + " "
                    + local.optString("action")), 14, YELLOW));
            orderPanel.addView(label(id + " · " + order.optString("fulfilment_quality")
                    + (order.isNull("reconciliation_note") ? ""
                    : " · " + order.optString("reconciliation_note")), 12, MUTED));
            JSONObject pickup = order.optJSONObject("pickup"), destination = order.optJSONObject("destination");
            if (pickup != null) {
                double lat = pickup.optDouble("latitude", Double.NaN),
                        lng = pickup.optDouble("longitude", Double.NaN);
                if (validPoint(lat, lng)) {
                    orderPanel.addView(label("Pickup pin: " + lat + ", " + lng, 12, CYAN));
                    action(orderPanel, "Open assignment pickup directions", () -> openDirections(lat, lng));
                }
            }
            if (destination != null) {
                double lat = destination.optDouble("latitude", Double.NaN),
                        lng = destination.optDouble("longitude", Double.NaN);
                if (validPoint(lat, lng)) {
                    orderPanel.addView(label("Destination pin: " + lat + ", " + lng, 12, CYAN));
                    action(orderPanel, "Open assignment destination directions", () -> openDirections(lat, lng));
                }
            }
            if (local != null && "NEEDS_REVIEW".equals(local.optString("state"))) {
                orderPanel.addView(label("Server rejected this report (HTTP " + local.optInt("http_status")
                        + "). Review the current assignment before retrying or clearing it.", 12, YELLOW));
                action(orderPanel, "Retry reviewed assignment report", () -> retryOrderAction(id));
                action(orderPanel, "Clear reviewed assignment report", () -> clearReviewedOrderAction(id));
            } else if (local == null && "ASSIGNED".equals(serverStatus)) {
                action(orderPanel, "Report assignment pickup", () -> reportOrderAction(id, OrderActionQueue.PICKUP));
            } else if (local == null && "PICKED_UP".equals(serverStatus)) {
                action(orderPanel, "Report assignment delivery", () -> reportOrderAction(id, OrderActionQueue.REPORT_DELIVERY));
            }
        }
        for (int index = 0; index < localOrders.length(); index++) {
            JSONObject local = localOrders.optJSONObject(index);
            if (local == null || visibleOrders.contains(local.optString("order_id"))) continue;
            String id = local.optString("order_id");
            orderPanel.addView(label("PHONE " + local.optString("state") + " "
                    + local.optString("action") + " · " + id, 14, YELLOW));
            orderPanel.addView(label("This assignment is absent from the current server inbox. Review with your fleet before clearing its local report.", 12, MUTED));
            action(orderPanel, "Clear reviewed assignment report", () -> clearReviewedOrderAction(id));
        }
        if (connected && orderData.length() == 0 && localOrders.length() == 0)
            orderPanel.addView(label("No internal assignments for your vehicle.", 12, MUTED));
        serviceCasesPanel.removeAllViews();
        reportServiceCase.setEnabled(connected);
        syncServiceCases.setEnabled(connected);
        ServiceCaseCommandQueue serviceQueue = new ServiceCaseCommandQueue(this);
        JSONArray localService = new JSONArray();
        if (connected) {
            try { localService = serviceQueue.forOwner(origin, identity); }
            catch (Exception queueError) {
                serviceCasesPanel.addView(label("Service case queue needs recovery: "
                        + queueError.getMessage(), 13, YELLOW));
            }
        }
        serviceTitle.setText(connected ? serviceCaseData.length() + " stored cases · "
                + localService.length() + " commands on phone" : "Connect to review service cases");
        for (int index = 0; index < localService.length(); index++) {
            JSONObject command = localService.optJSONObject(index); if (command == null) continue;
            JSONObject body = command.optJSONObject("body");
            String commandKey = command.optString("command_key");
            serviceCasesPanel.addView(label("PHONE " + command.optString("state") + " "
                    + command.optString("kind") + " · " + command.optString("case_id"), 14, YELLOW));
            if (body != null) serviceCasesPanel.addView(label("CREATE".equals(command.optString("kind"))
                    ? body.optString("title") + " · " + body.optString("description")
                    : body.optString("body"), 12, Color.WHITE));
            if ("NEEDS_REVIEW".equals(command.optString("state"))) {
                serviceCasesPanel.addView(label("Server rejected this command (HTTP "
                        + command.optInt("http_status") + "). Review it before retrying or clearing it.", 12, YELLOW));
                action(serviceCasesPanel, "Retry reviewed command", () -> retryServiceCommand(commandKey));
                action(serviceCasesPanel, "Clear reviewed command", () -> clearReviewedServiceCommand(commandKey));
            }
        }
        for (int index = 0; index < serviceCaseData.length(); index++) {
            JSONObject item = serviceCaseData.optJSONObject(index); if (item == null) continue;
            String caseId = item.optString("case_id");
            serviceCasesPanel.addView(label(item.optString("priority") + " · "
                    + item.optString("category").replace('_', ' ') + " · "
                    + item.optString("status"), 14, YELLOW));
            serviceCasesPanel.addView(label(item.optString("title") + " · "
                    + item.optString("description"), 13, Color.WHITE));
            if (!item.isNull("resolution_note"))
                serviceCasesPanel.addView(label("Resolution: " + item.optString("resolution_code")
                        + " · " + item.optString("resolution_note"), 12, CYAN));
            action(serviceCasesPanel, "View replies", () -> showServiceMessages(caseId));
            if (!"RESOLVED".equals(item.optString("status")))
                action(serviceCasesPanel, "Add follow-up", () -> askServiceFollowUp(caseId));
        }
        if (connected && serviceCaseData.length() == 0 && localService.length() == 0)
            serviceCasesPanel.addView(label("No service cases reported yet.", 12, MUTED));
        history.removeAllViews();
        historyTitle.setText(connected ? "Latest completed driver trips" : "Connect to review your trips");
        int shown = 0;
        for (int index = 0; index < tripHistory.length() && shown < 5; index++) {
            JSONObject previous = tripHistory.optJSONObject(index);
            if (previous == null || !"COMPLETED".equals(previous.optString("status"))) continue;
            shown++;
            history.addView(label(previous.optString("trip_id") + " · "
                    + previous.optString("started_at", "").replace('T', ' ').replaceAll("\\..*", " UTC"), 13, YELLOW));
            String soc = previous.isNull("ending_soc_pct") ? "no driver charge report"
                    : previous.optString("ending_soc_pct") + "% " + previous.optString("ending_soc_source");
            history.addView(label("Phone GPS " + previous.optInt("phone_gps_sample_count")
                    + " points · phone IMU " + previous.optInt("phone_imu_window_count")
                    + " windows · " + soc, 12, Color.WHITE));
        }
        if (connected && shown == 0) history.addView(label("No completed trips yet.", 13, MUTED));
    }

    private boolean validPoint(double latitude, double longitude) {
        return Double.isFinite(latitude) && Double.isFinite(longitude)
                && Math.abs(latitude) <= 90 && Math.abs(longitude) <= 180;
    }

    private void cancelPhoneLocationLookup() {
        if (phoneLocationLookup != null) {
            phoneLocationLookup.cancel();
            phoneLocationLookup = null;
        }
    }

    private void cancelRouteLocationLookup() {
        if (routeLocationLookup != null) {
            routeLocationLookup.cancel();
            routeLocationLookup = null;
        }
    }

    private void previewRouteFromPhone(String destination, double endLat, double endLng) {
        boolean fine = checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED;
        boolean coarse = checkSelfPermission(Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED;
        if (!fine && !coarse) {
            requestPermissions(new String[] {Manifest.permission.ACCESS_FINE_LOCATION,
                    Manifest.permission.ACCESS_COARSE_LOCATION}, ROUTE_PERMISSION);
            routePanel.removeAllViews();
            routePanel.addView(label("Allow phone location, then tap the route preview again.", 13, MUTED));
            return;
        }
        LocationManager manager = getSystemService(LocationManager.class);
        String provider = fine && manager.isProviderEnabled(LocationManager.GPS_PROVIDER)
                ? LocationManager.GPS_PROVIDER
                : manager.isProviderEnabled(LocationManager.NETWORK_PROVIDER) ? LocationManager.NETWORK_PROVIDER : null;
        if (provider == null) {
            routePanel.removeAllViews();
            routePanel.addView(label("Turn on phone location to preview a route from where you are.", 13, MUTED));
            return;
        }
        cancelRouteLocationLookup();
        int generation = sessionGeneration, request = ++routeGeneration;
        routePanel.removeAllViews();
        routePanel.addView(label("Finding one current phone location…", 13, CYAN));
        if (Build.VERSION.SDK_INT >= 30) {
            CancellationSignal signal = new CancellationSignal();
            routeLocationLookup = signal;
            try {
                manager.getCurrentLocation(provider, signal, getMainExecutor(), location -> {
                    if (generation != sessionGeneration || request != routeGeneration) return;
                    routeLocationLookup = null;
                    usePhoneRouteLocation(location, destination, endLat, endLng);
                });
                mainHandler.postDelayed(() -> {
                    if (generation != sessionGeneration || request != routeGeneration) return;
                    ++routeGeneration;
                    cancelRouteLocationLookup();
                    usePhoneRouteLocation(null, destination, endLat, endLng);
                }, 20_000);
            } catch (SecurityException exception) {
                cancelRouteLocationLookup();
                usePhoneRouteLocation(null, destination, endLat, endLng);
            }
        } else {
            try { usePhoneRouteLocation(manager.getLastKnownLocation(provider), destination, endLat, endLng); }
            catch (SecurityException exception) { usePhoneRouteLocation(null, destination, endLat, endLng); }
        }
    }

    private void usePhoneRouteLocation(Location location, String destination, double endLat, double endLng) {
        if (location == null || !validPoint(location.getLatitude(), location.getLongitude())
                || Math.abs(System.currentTimeMillis() - location.getTime()) > 60_000
                || location.hasAccuracy() && location.getAccuracy() > 100) {
            routePanel.removeAllViews();
            routePanel.addView(label("A current accurate phone location is unavailable. Try again outdoors.", 13, MUTED));
            return;
        }
        previewRoute(destination, location.getLatitude(), location.getLongitude(), endLat, endLng,
                "current phone location");
    }

    private void findPhoneChargers() {
        boolean fine = checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED;
        boolean coarse = checkSelfPermission(Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED;
        if (!fine && !coarse) {
            requestPermissions(new String[] {Manifest.permission.ACCESS_FINE_LOCATION,
                    Manifest.permission.ACCESS_COARSE_LOCATION}, CHARGER_PERMISSION);
            return;
        }
        LocationManager manager = getSystemService(LocationManager.class);
        String provider = fine && manager.isProviderEnabled(LocationManager.GPS_PROVIDER)
                ? LocationManager.GPS_PROVIDER
                : manager.isProviderEnabled(LocationManager.NETWORK_PROVIDER) ? LocationManager.NETWORK_PROVIDER : null;
        if (provider == null) {
            chargerPanel.removeAllViews();
            chargerPanel.addView(label("Turn on phone location to search for chargers near you.", 13, MUTED));
            return;
        }
        cancelPhoneLocationLookup();
        int generation = sessionGeneration, request = ++chargerGeneration;
        chargerPanel.removeAllViews();
        chargerPanel.addView(label("Finding one current phone location…", 13, CYAN));
        if (Build.VERSION.SDK_INT >= 30) {
            CancellationSignal signal = new CancellationSignal();
            phoneLocationLookup = signal;
            try {
                manager.getCurrentLocation(provider, signal, getMainExecutor(), location -> {
                    if (generation != sessionGeneration || request != chargerGeneration) return;
                    phoneLocationLookup = null;
                    usePhoneChargerLocation(location);
                });
                mainHandler.postDelayed(() -> {
                    if (generation != sessionGeneration || request != chargerGeneration) return;
                    cancelPhoneLocationLookup();
                    usePhoneChargerLocation(null);
                }, 20_000);
            } catch (SecurityException exception) {
                cancelPhoneLocationLookup();
                usePhoneChargerLocation(null);
            }
        } else {
            try { usePhoneChargerLocation(manager.getLastKnownLocation(provider)); }
            catch (SecurityException exception) { usePhoneChargerLocation(null); }
        }
    }

    private void usePhoneChargerLocation(Location location) {
        if (location == null || !validPoint(location.getLatitude(), location.getLongitude())
                || Math.abs(System.currentTimeMillis() - location.getTime()) > 60_000) {
            chargerPanel.removeAllViews();
            chargerPanel.addView(label("A current phone location is unavailable. Try again outdoors or use the plan origin.", 13, MUTED));
            return;
        }
        checkChargers(location.getLatitude(), location.getLongitude(), "phone location");
    }

    private void checkPlanChargers(double latitude, double longitude) {
        checkChargers(latitude, longitude, "plan origin");
    }

    private void checkChargers(double latitude, double longitude, String anchor) {
        int generation = sessionGeneration, request = ++chargerGeneration;
        chargerPanel.removeAllViews();
        chargerPanel.addView(label("Checking chargers near " + anchor + "…", 13, CYAN));
        task(() -> {
            try {
                JSONObject response = (JSONObject) DriverApi.saved(this).nearbyChargers(latitude, longitude);
                runOnUiThread(() -> {
                    if (generation != sessionGeneration || request != chargerGeneration) return;
                    showChargers(response, anchor);
                });
            } catch (Exception failure) {
                runOnUiThread(() -> {
                    if (generation != sessionGeneration || request != chargerGeneration) return;
                    chargerPanel.removeAllViews();
                    chargerPanel.addView(label("Charger availability unavailable. Try again when connected.", 13, MUTED));
                });
            }
        });
    }

    private void showChargers(JSONObject response, String anchor) {
        chargerPanel.removeAllViews();
        String quality = response.optString("quality", "UNAVAILABLE");
        if ("SIMULATED".equals(quality))
            chargerPanel.addView(label("SIMULATED chargers near " + anchor + " · no live availability or price", 13, YELLOW));
        else if ("PROVIDER".equals(quality))
            chargerPanel.addView(label("Provider chargers near " + anchor + " · observed "
                    + response.optString("observed_at", "unknown"), 13, CYAN));
        else {
            chargerPanel.addView(label("Charger availability " + quality.toLowerCase(Locale.US)
                    + " · " + response.optString("reason", "no usable data"), 13, MUTED));
            return;
        }
        JSONArray chargers = response.optJSONArray("chargers");
        if (chargers == null || chargers.length() == 0) {
            chargerPanel.addView(label("No chargers returned near " + anchor + ".", 12, MUTED)); return;
        }
        for (int index = 0; index < Math.min(5, chargers.length()); index++) {
            JSONObject charger = chargers.optJSONObject(index);
            if (charger == null) continue;
            chargerPanel.addView(label(charger.optString("name") + " · "
                    + (charger.optBoolean("available") ? "AVAILABLE" : "UNAVAILABLE"), 13, YELLOW));
            chargerPanel.addView(label(charger.optString("connector") + " · "
                    + charger.optString("power_kw") + " kW · " + charger.optString("distance_km") + " km from " + anchor, 12, Color.WHITE));
            if ("PROVIDER".equals(quality))
                chargerPanel.addView(label(charger.optString("queue_min") + " min provider queue · "
                        + charger.optString("price_per_kwh") + " " + charger.optString("currency") + "/kWh", 12, Color.LTGRAY));
        }
    }

    private void previewRoute(String destination, double startLat, double startLng, double endLat, double endLng) {
        previewRoute(destination, startLat, startLng, endLat, endLng, "planned origin");
    }

    private void previewRoute(String destination, double startLat, double startLng, double endLat, double endLng,
                              String anchor) {
        stopGuidance();
        cancelRouteLocationLookup();
        int generation = sessionGeneration, request = ++routeGeneration;
        routePanel.removeAllViews();
        routePanel.addView(label("Loading road routes from " + anchor + " to " + destination + "…", 13, CYAN));
        task(() -> {
            try {
                JSONObject origin = new JSONObject().put("latitude", startLat).put("longitude", startLng);
                JSONObject target = new JSONObject().put("latitude", endLat).put("longitude", endLng);
                JSONObject body = new JSONObject().put("origin", origin).put("destination", target);
                JSONObject result = (JSONObject) DriverApi.saved(this).call("POST", "/api/v1/driver/routes/road", body);
                JSONArray routes = result.optJSONArray("routes");
                if (routes == null || routes.length() == 0) throw new IllegalStateException("Road provider returned no routes");
                runOnUiThread(() -> {
                    if (generation != sessionGeneration || request != routeGeneration) return;
                    showRoutes(destination, routes, anchor, endLat, endLng);
                });
            } catch (DriverApi.ApiFailure failure) {
                runOnUiThread(() -> {
                    if (generation != sessionGeneration || request != routeGeneration) return;
                    routePanel.removeAllViews();
                    routePanel.addView(label(failure.status == 503
                            ? "Road routing is unavailable. Open directions in Maps for guidance."
                            : "Road preview failed (HTTP " + failure.status + ").", 13, MUTED));
                });
            } catch (Exception failure) {
                runOnUiThread(() -> {
                    if (generation != sessionGeneration || request != routeGeneration) return;
                    routePanel.removeAllViews();
                    routePanel.addView(label("Road preview unavailable. Open directions in Maps for guidance.", 13, MUTED));
                });
            }
        });
    }

    private void showRoutes(String destination, JSONArray routes, String anchor,
                            double endLat, double endLng) {
        routePanel.removeAllViews();
        routePanel.addView(label("PROVIDER ROAD PREVIEW / " + anchor + " → " + destination, 13, YELLOW));
        routePanel.addView(label("Distance and time are provider baselines. Foreground phone guidance can request a fresh route when off route. Live traffic is unavailable.", 12, Color.LTGRAY));
        LinearLayout details = new LinearLayout(this); details.setOrientation(LinearLayout.VERTICAL);
        for (int index = 0; index < Math.min(3, routes.length()); index++) {
            JSONObject candidate = routes.optJSONObject(index);
            if (candidate == null) continue;
            JSONObject route = candidate.optJSONObject("route");
            if (route == null) continue;
            int selected = index;
            String summary = String.format(Locale.US, "Route %d · %.1f km · %.0f min", index + 1,
                    route.optDouble("distance_km"), route.optDouble("baseline_eta_min"));
            action(routePanel, summary, () -> showRouteSteps(details, routes.optJSONObject(selected),
                    anchor, endLat, endLng));
        }
        routePanel.addView(details);
        showRouteSteps(details, routes.optJSONObject(0), anchor, endLat, endLng);
    }

    private void showRouteSteps(LinearLayout details, JSONObject candidate, String anchor,
                                double endLat, double endLng) {
        stopGuidance();
        details.removeAllViews();
        if (candidate == null) return;
        LinearLayout routeBody = new LinearLayout(this);
        routeBody.setOrientation(LinearLayout.VERTICAL);
        details.addView(routeBody);
        renderRouteBody(routeBody, candidate);
        if ("current phone location".equals(anchor)) {
            details.addView(label("Foreground phone GPS only. Provider route baseline; no live traffic or lane guidance.",
                    12, Color.LTGRAY));
            action(details, "Start guidance", () -> startGuidance(candidate, endLat, endLng, details, routeBody));
            action(details, "Stop guidance", this::stopGuidance);
        }
    }

    private void renderRouteBody(LinearLayout routeBody, JSONObject candidate) {
        routeBody.removeAllViews();
        RouteSketchView sketch = RouteSketchView.from(this, candidate.optJSONObject("geometry"));
        if (sketch != null) {
            routeBody.addView(label("ROAD SHAPE / NORTH UP / NO MAP TILES", 11, CYAN));
            LinearLayout.LayoutParams sketchSize = new LinearLayout.LayoutParams(-1, dp(170));
            sketchSize.topMargin = dp(8);
            routeBody.addView(sketch, sketchSize);
        }
        JSONArray steps = candidate.optJSONArray("steps");
        if (steps == null || steps.length() == 0) {
            routeBody.addView(label("No turn details were supplied by this provider.", 12, MUTED)); return;
        }
        for (int index = 0; index < Math.min(10, steps.length()); index++) {
            JSONObject step = steps.optJSONObject(index);
            if (step == null) continue;
            String name = step.optString("name"), type = step.optString("type").replace('_', ' ');
            if (!type.isEmpty()) type = Character.toUpperCase(type.charAt(0)) + type.substring(1);
            String modifier = step.isNull("modifier") ? "" : step.optString("modifier");
            String distance = String.format(Locale.US, "%.0f m", step.optDouble("distance_m"));
            routeBody.addView(label((index + 1) + ". " + type + (modifier.isEmpty() ? "" : " " + modifier)
                    + (name.isEmpty() ? "" : " · " + name) + " · " + distance, 12, Color.WHITE));
        }
    }

    private RouteProgress guidanceRoute(JSONObject candidate) throws Exception {
        JSONObject shape = candidate.getJSONObject("geometry");
        if (!"LineString".equals(shape.getString("type"))) throw new IllegalArgumentException("route shape unavailable");
        JSONArray coordinates = shape.getJSONArray("coordinates");
        if (coordinates.length() < 2 || coordinates.length() > 20_000)
            throw new IllegalArgumentException("route shape is too large");
        double[][] points = new double[coordinates.length()][2];
        for (int index = 0; index < coordinates.length(); index++) {
            JSONArray point = coordinates.getJSONArray(index);
            if (point.length() != 2) throw new IllegalArgumentException("invalid route point");
            points[index][0] = point.getDouble(0);
            points[index][1] = point.getDouble(1);
        }
        JSONArray turns = candidate.getJSONArray("steps");
        if (turns.length() > 200) throw new IllegalArgumentException("too many turns");
        RouteProgress.Step[] steps = new RouteProgress.Step[turns.length()];
        for (int index = 0; index < turns.length(); index++) {
            JSONObject turn = turns.getJSONObject(index), point = turn.getJSONObject("location");
            steps[index] = new RouteProgress.Step(point.getDouble("latitude"), point.getDouble("longitude"),
                    turn.getString("type"), turn.isNull("modifier") ? "" : turn.optString("modifier"),
                    turn.optString("name"));
        }
        return new RouteProgress(points, steps);
    }

    private void startGuidance(JSONObject candidate, double endLat, double endLng,
                               LinearLayout details, LinearLayout routeBody) {
        stopGuidance();
        View previousStatus = details.findViewWithTag("guidance-status");
        if (previousStatus != null) details.removeView(previousStatus);
        guidanceStatus = label("GUIDANCE / waiting for a fresh phone location", 13, CYAN);
        guidanceStatus.setTag("guidance-status");
        details.addView(guidanceStatus);
        boolean fine = checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED;
        boolean coarse = checkSelfPermission(Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED;
        if ((!fine && !coarse) || identity == null) {
            guidanceStatus.setText("GUIDANCE / location permission and a connected driver are required.");
            return;
        }
        try { guidanceProgress = guidanceRoute(candidate); }
        catch (Exception invalid) {
            guidanceStatus.setText("GUIDANCE / provider route is invalid. Open directions in Maps.");
            return;
        }
        guidanceDestinationLat = endLat;
        guidanceDestinationLng = endLng;
        guidanceRouteBody = routeBody;
        guidanceManager = getSystemService(LocationManager.class);
        String provider = fine && guidanceManager.isProviderEnabled(LocationManager.GPS_PROVIDER)
                ? LocationManager.GPS_PROVIDER
                : guidanceManager.isProviderEnabled(LocationManager.NETWORK_PROVIDER)
                    ? LocationManager.NETWORK_PROVIDER : null;
        if (provider == null) {
            guidanceStatus.setText("GUIDANCE / turn on phone location before starting.");
            guidanceProgress = null;
            return;
        }
        int generation = ++guidanceGeneration;
        guidanceListener = location -> {
            if (generation == guidanceGeneration && identity != null) onGuidanceLocation(location, generation);
        };
        try {
            guidanceManager.requestLocationUpdates(provider, 1000L, 2f, guidanceListener, Looper.getMainLooper());
            Location recent = guidanceManager.getLastKnownLocation(provider);
            if (recent != null) onGuidanceLocation(recent, generation);
        } catch (SecurityException failure) {
            TextView stopped = guidanceStatus;
            stopGuidance();
            stopped.setText("GUIDANCE / phone location permission was revoked.");
        }
    }

    private void onGuidanceLocation(Location location, int generation) {
        if (generation != guidanceGeneration || guidanceProgress == null || guidanceStatus == null) return;
        boolean mocked = Build.VERSION.SDK_INT >= 31 ? location.isMock() : location.isFromMockProvider();
        RouteProgress.State state = guidanceProgress.update(location.getLatitude(), location.getLongitude(),
                location.hasAccuracy() ? location.getAccuracy() : 101f,
                location.getTime(), System.currentTimeMillis(), mocked);
        if (state.status == RouteProgress.Status.LOCATION_UNAVAILABLE) {
            guidanceStatus.setText("GUIDANCE / current phone position is stale, mocked or inaccurate. Check location outdoors.");
        } else if (state.status == RouteProgress.Status.ARRIVED) {
            TextView arrived = guidanceStatus;
            stopGuidance();
            arrived.setText("GUIDANCE / destination reached by phone GPS. Confirm your safe stopping place.");
        } else if (state.status == RouteProgress.Status.OFF_ROUTE) {
            String action = state.rerouteDue || guidanceRerouteInFlight ? "Requesting a fresh provider route."
                    : guidanceRerouteFailed ? "Reroute unavailable. Stop safely or open directions in Maps."
                    : "Check the road ahead.";
            guidanceStatus.setText("GUIDANCE / off route by " + Math.round(state.distanceFromRouteMeters)
                    + " m. " + action);
            if (state.rerouteDue && !guidanceRerouteInFlight)
                rerouteGuidance(location.getLatitude(), location.getLongitude(), generation);
        } else {
            guidanceRerouteFailed = false;
            String next = "Continue on the provider route";
            if (state.nextStepIndex >= 0) {
                RouteProgress.Step step = guidanceProgress.step(state.nextStepIndex);
                next = step.type.replace('_', ' ') + (step.modifier.isEmpty() ? "" : " " + step.modifier)
                        + (step.name.isEmpty() ? "" : " onto " + step.name)
                        + " in " + Math.round(state.distanceToTurnMeters) + " m";
            }
            guidanceStatus.setText(String.format(Locale.US,
                    "GUIDANCE / following provider route · %.1f km remaining\n%s\nPhone GPS only · live traffic unavailable",
                    state.remainingMeters / 1000, next));
        }
    }

    private void rerouteGuidance(double latitude, double longitude, int generation) {
        guidanceRerouteInFlight = true;
        double destinationLat = guidanceDestinationLat, destinationLng = guidanceDestinationLng;
        network.execute(() -> {
            try {
                JSONObject body = new JSONObject()
                        .put("origin", new JSONObject().put("latitude", latitude).put("longitude", longitude))
                        .put("destination", new JSONObject().put("latitude", destinationLat)
                                .put("longitude", destinationLng));
                JSONObject response = (JSONObject) DriverApi.saved(this)
                        .call("POST", "/api/v1/driver/routes/road", body);
                JSONArray candidates = response.getJSONArray("routes");
                RouteProgress updated = guidanceRoute(candidates.getJSONObject(0));
                JSONObject updatedCandidate = candidates.getJSONObject(0);
                runOnUiThread(() -> {
                    if (generation != guidanceGeneration || guidanceStatus == null) return;
                    guidanceProgress = updated;
                    guidanceRerouteInFlight = false;
                    guidanceRerouteFailed = false;
                    if (guidanceRouteBody != null) renderRouteBody(guidanceRouteBody, updatedCandidate);
                    guidanceStatus.setText("GUIDANCE / provider route updated. Waiting for the next fresh phone position.");
                });
            } catch (Exception failure) {
                runOnUiThread(() -> {
                    if (generation != guidanceGeneration || guidanceStatus == null) return;
                    guidanceRerouteInFlight = false;
                    guidanceRerouteFailed = true;
                    guidanceStatus.setText("GUIDANCE / reroute unavailable. Stop safely or open directions in Maps.");
                });
            }
        });
    }

    private void stopGuidance() {
        ++guidanceGeneration;
        if (guidanceManager != null && guidanceListener != null) {
            try { guidanceManager.removeUpdates(guidanceListener); }
            catch (SecurityException ignored) { /* Permission can be revoked while guidance is active. */ }
        }
        guidanceListener = null;
        guidanceManager = null;
        guidanceProgress = null;
        guidanceRouteBody = null;
        guidanceRerouteInFlight = false;
        guidanceRerouteFailed = false;
        if (guidanceStatus != null) guidanceStatus.setText("Guidance stopped. Route preview remains available.");
        guidanceStatus = null;
    }

    private void openDirections(double latitude, double longitude) {
        Uri uri = new Uri.Builder().scheme("https").authority("www.google.com")
                .appendPath("maps").appendPath("dir")
                .appendQueryParameter("api", "1")
                .appendQueryParameter("destination", String.format(Locale.US, "%.6f,%.6f", latitude, longitude))
                .appendQueryParameter("travelmode", "driving")
                .appendQueryParameter("dir_action", "navigate").build();
        try { startActivity(new Intent(Intent.ACTION_VIEW, uri)); }
        catch (ActivityNotFoundException error) { message("Install a maps app or browser to open directions."); }
    }

    private void renderQueue() {
        JSONObject openStop = activeTrip == null ? null : activeTrip.optJSONObject("active_stop");
        String syncStatus = queuedPoints == 0 ? "" : GpsSyncStatus.message(this);
        queue.setText("Queued phone GPS: " + queuedPoints
                + (openStop == null ? "" : " · " + openStop.optString("kind") + " stop open")
                + (syncStatus.isEmpty() ? "" : "\n" + syncStatus));
        queue.setTextColor(syncStatus.startsWith("GPS upload blocked") ? YELLOW : CYAN);
    }

    private JSONObject manualSoc(String raw, String name, JSONObject body) throws Exception {
        if (!raw.isEmpty()) {
            double value = Double.parseDouble(raw);
            if (!Double.isFinite(value) || value < 0 || value > 100) throw new IllegalArgumentException("Dashboard charge must be 0–100%");
            body.put(name, value);
        }
        return body;
    }

    private void beginTrip() {
        startTrip.setEnabled(false);
        String soc = startSocInput.getText().toString().trim();
        int generation = sessionGeneration;
        task(() -> {
            String pending = prefs.getString("pending_start", "");
            JSONObject body;
            if (pending.isEmpty()) {
                body = new JSONObject(); body.put("trip_id", "P-" + UUID.randomUUID());
                body.put("idempotency_key", "start-" + UUID.randomUUID());
                manualSoc(soc, "starting_soc_pct", body);
                if (!prefs.edit().putString("pending_start", body.toString()).commit()) throw new IllegalStateException("pending trip storage failed");
            } else body = new JSONObject(pending);
            DriverApi.saved(this).call("POST", "/api/v1/driver/trips", body);
            prefs.edit().remove("pending_start").commit(); refreshNow(generation);
        });
    }

    private void finishTrip() {
        JSONObject current = activeTrip; if (current == null || current.optJSONObject("active_stop") != null) return;
        completeTrip.setEnabled(false);
        String id = current.optString("trip_id"); stopGuidance(); stopGps(); GpsUploadWorker.schedule(this);
        String soc = endSocInput.getText().toString().trim();
        int generation = sessionGeneration;
        task(() -> {
            String key = "pending_end_" + id, pending = prefs.getString(key, "");
            JSONObject body;
            if (pending.isEmpty()) {
                body = new JSONObject(); body.put("idempotency_key", "end-" + id);
                manualSoc(soc, "ending_soc_pct", body);
                if (!prefs.edit().putString(key, body.toString()).commit()) throw new IllegalStateException("pending completion storage failed");
            } else body = new JSONObject(pending);
            DriverApi.saved(this).call("POST", "/api/v1/driver/trips/" + id + "/complete", body);
            prefs.edit().remove(key).commit(); refreshNow(generation);
        });
    }

    private void beginStop(String kind) {
        JSONObject current = activeTrip; if (current == null || current.optJSONObject("active_stop") != null) return;
        startWait.setEnabled(false); startCharge.setEnabled(false);
        String id = current.optString("trip_id");
        int generation = sessionGeneration;
        task(() -> {
            String key = "pending_stop_" + id, pending = prefs.getString(key, "");
            JSONObject body;
            if (pending.isEmpty()) {
                body = new JSONObject(); body.put("stop_id", "S-" + UUID.randomUUID());
                body.put("idempotency_key", "stop-" + UUID.randomUUID()); body.put("kind", kind);
                if (!prefs.edit().putString(key, body.toString()).commit()) throw new IllegalStateException("pending stop storage failed");
            } else body = new JSONObject(pending);
            DriverApi.saved(this).call("POST", "/api/v1/driver/trips/" + id + "/stops", body);
            prefs.edit().remove(key).commit(); refreshNow(generation);
        });
    }

    private void finishStop() {
        JSONObject current = activeTrip, stop = current == null ? null : current.optJSONObject("active_stop");
        if (stop == null) return;
        endStop.setEnabled(false);
        String tripId = current.optString("trip_id"), stopId = stop.optString("stop_id");
        int generation = sessionGeneration;
        task(() -> {
            JSONObject body = new JSONObject(); body.put("idempotency_key", "finish-" + stopId);
            DriverApi.saved(this).call("POST", "/api/v1/driver/trips/" + tripId + "/stops/" + stopId + "/complete", body);
            refreshNow(generation);
        });
    }

    private void respond(String id, String outcome) {
        JSONObject me = identity;
        if (me == null) return;
        String origin = prefs.getString(DriverApi.ORIGIN, "");
        int generation = sessionGeneration;
        AdvisoryReplyQueue replies = new AdvisoryReplyQueue(this);
        JSONObject choice;
        try { choice = replies.put(origin, me, id, outcome); }
        catch (Exception storageError) {
            message(storageError.getMessage()); return;
        }
        render();
        task(() -> {
            if (generation != sessionGeneration) return;
            try {
                JSONObject confirmed = sendAdvisoryReply(DriverApi.saved(this), choice);
                replies.remove(origin, me, id);
                runOnUiThread(() -> {
                    if (generation != sessionGeneration) return;
                    for (int i = 0; i < nudgeData.length(); i++) {
                        JSONObject nudge = nudgeData.optJSONObject(i);
                        if (nudge != null && id.equals(nudge.optString("nudge_id"))) {
                            try { nudgeData.put(i, confirmed); } catch (Exception ignored) { /* Local display refreshes. */ }
                            break;
                        }
                    }
                    render(); message("Advisory reply confirmed by server.");
                });
            } catch (DriverApi.ApiFailure failure) {
                if (failure.status != 429 && failure.status < 500)
                    replies.needsReview(origin, me, id, failure.status);
                runOnUiThread(() -> { if (generation == sessionGeneration) {
                    render(); message(failure.status == 429 || failure.status >= 500
                            ? "Server unavailable. Advisory reply is queued on this phone."
                            : "Server rejected advisory reply (HTTP " + failure.status + "). Review before retrying.");
                }});
            } catch (java.io.IOException offline) {
                runOnUiThread(() -> { if (generation == sessionGeneration) {
                    render(); message("Network unavailable. Advisory reply is queued on this phone.");
                }});
            }
        });
    }

    private void markDispatchSeen(String id) {
        int generation = sessionGeneration;
        JSONObject owner = identity;
        network.execute(() -> {
            try {
                JSONObject seen = (JSONObject) DriverApi.saved(this).call("POST",
                        "/api/v1/driver/dispatch-proposals/" + id + "/seen", new JSONObject());
                runOnUiThread(() -> {
                    if (generation != sessionGeneration || owner != identity) return;
                    for (int index = 0; index < dispatchData.length(); index++) {
                        JSONObject candidate = dispatchData.optJSONObject(index);
                        if (candidate != null && id.equals(candidate.optString("offer_id"))) {
                            try { dispatchData.put(index, seen); }
                            catch (Exception ignored) { /* Next inbox refresh repairs this view. */ }
                            break;
                        }
                    }
                });
            } catch (Exception offline) {
                runOnUiThread(() -> dispatchSeenInFlight.remove(id));
            }
        });
    }

    private void respondDispatch(String id, String outcome) {
        JSONObject me = identity;
        if (me == null) return;
        String origin = prefs.getString(DriverApi.ORIGIN, "");
        int generation = sessionGeneration;
        DispatchReplyQueue replies = new DispatchReplyQueue(this);
        JSONObject choice;
        try { choice = replies.put(origin, me, id, outcome); }
        catch (Exception storageError) { message(storageError.getMessage()); return; }
        render();
        task(() -> {
            if (generation != sessionGeneration) return;
            try {
                JSONObject confirmed = sendDispatchReply(DriverApi.saved(this), choice);
                replies.remove(origin, me, id);
                runOnUiThread(() -> {
                    if (generation != sessionGeneration) return;
                    for (int index = 0; index < dispatchData.length(); index++) {
                        JSONObject offer = dispatchData.optJSONObject(index);
                        if (offer != null && id.equals(offer.optString("offer_id"))) {
                            try { dispatchData.put(index, confirmed); }
                            catch (Exception ignored) { /* Next inbox refresh repairs this view. */ }
                            break;
                        }
                    }
                    render(); message("Pickup decision confirmed by server.");
                });
            } catch (DriverApi.ApiFailure failure) {
                if (failure.status != 429 && failure.status < 500)
                    replies.needsReview(origin, me, id, failure.status);
                runOnUiThread(() -> { if (generation == sessionGeneration) {
                    render(); message(failure.status == 429 || failure.status >= 500
                            ? "Server unavailable. Pickup decision is queued on this phone."
                            : "Server rejected pickup decision (HTTP " + failure.status + "). Review before retrying.");
                }});
            } catch (java.io.IOException offline) {
                runOnUiThread(() -> { if (generation == sessionGeneration) {
                    render(); message("Network unavailable. Pickup decision is queued on this phone.");
                }});
            }
        });
    }

    private JSONObject sendDispatchReply(DriverApi api, JSONObject choice) throws Exception {
        JSONObject body = new JSONObject().put("idempotency_key", choice.getString("idempotency_key"))
                .put("outcome", choice.getString("outcome"));
        return (JSONObject) api.call("POST", "/api/v1/driver/dispatch-proposals/"
                + choice.getString("offer_id") + "/respond", body);
    }

    private void retryDispatchReply(String id) {
        if (identity == null) return;
        try {
            new DispatchReplyQueue(this).retry(prefs.getString(DriverApi.ORIGIN, ""), identity, id);
            render(); refresh();
        } catch (Exception error) { message(error.getMessage()); }
    }

    private void clearReviewedDispatchReply(String id) {
        if (identity == null) return;
        JSONObject me = identity;
        String origin = prefs.getString(DriverApi.ORIGIN, "");
        new AlertDialog.Builder(this).setTitle("Clear local pickup decision?")
                .setMessage("The fleet's server outcome will remain. Clear this unsent choice only after reviewing the conflict.")
                .setNegativeButton("Keep decision", null)
                .setPositiveButton("Clear local decision", (dialog, which) -> {
                    try {
                        new DispatchReplyQueue(this).remove(origin, me, id);
                        render(); message("Local pickup decision cleared. Server outcome is unchanged.");
                    } catch (Exception error) { message(error.getMessage()); }
                }).show();
    }

    private void replayDispatchReplies(DriverApi api, JSONObject me, JSONArray inbox, int generation)
            throws Exception {
        DispatchReplyQueue replies = new DispatchReplyQueue(this);
        String origin = prefs.getString(DriverApi.ORIGIN, "");
        JSONArray pending = replies.forOwner(origin, me);
        for (int index = 0; index < pending.length() && generation == sessionGeneration; index++) {
            JSONObject choice = pending.getJSONObject(index);
            String id = choice.getString("offer_id"), outcome = choice.getString("outcome");
            for (int row = 0; row < inbox.length(); row++) {
                JSONObject offer = inbox.optJSONObject(row);
                if (offer == null || !id.equals(offer.optString("offer_id"))) continue;
                if (outcome.equals(offer.optString("status"))) {
                    replies.remove(origin, me, id);
                } else if (!"PENDING".equals(offer.optString("status"))) {
                    replies.needsReview(origin, me, id, 409);
                } else if ("QUEUED".equals(choice.optString("state"))) {
                    try {
                        JSONObject confirmed = sendDispatchReply(api, choice);
                        replies.remove(origin, me, id);
                        inbox.put(row, confirmed);
                    } catch (DriverApi.ApiFailure failure) {
                        if (failure.status != 429 && failure.status < 500)
                            replies.needsReview(origin, me, id, failure.status);
                    } catch (java.io.IOException offline) {
                        // Keep the durable decision for a later refresh or manual sync.
                    }
                }
                break;
            }
        }
    }

    private JSONObject sendOrderAction(DriverApi api, JSONObject choice) throws Exception {
        String action = choice.getString("action");
        if (!OrderActionQueue.PICKUP.equals(action)
                && !OrderActionQueue.REPORT_DELIVERY.equals(action))
            throw new IllegalArgumentException("Invalid saved assignment action");
        String endpoint = OrderActionQueue.PICKUP.equals(action) ? "pickup" : "report-delivery";
        JSONObject body = new JSONObject().put("idempotency_key", choice.getString("idempotency_key"));
        return (JSONObject) api.call("POST", "/api/v1/driver/internal-orders/"
                + choice.getString("order_id") + "/" + endpoint, body);
    }

    private void reportOrderAction(String id, String action) {
        JSONObject me = identity;
        if (me == null) return;
        String origin = prefs.getString(DriverApi.ORIGIN, "");
        int generation = sessionGeneration;
        OrderActionQueue queue = new OrderActionQueue(this);
        JSONObject choice;
        try { choice = queue.put(origin, me, id, action); }
        catch (Exception storageError) { message(storageError.getMessage()); return; }
        render();
        task(() -> {
            if (generation != sessionGeneration) return;
            try {
                JSONObject confirmed = sendOrderAction(DriverApi.saved(this), choice);
                queue.remove(origin, me, id);
                runOnUiThread(() -> {
                    if (generation != sessionGeneration) return;
                    for (int index = 0; index < orderData.length(); index++) {
                        JSONObject order = orderData.optJSONObject(index);
                        if (order != null && id.equals(order.optString("order_id"))) {
                            try { orderData.put(index, confirmed); }
                            catch (Exception ignored) { /* A later refresh repairs this view. */ }
                            break;
                        }
                    }
                    render(); message(OrderActionQueue.PICKUP.equals(action)
                            ? "Pickup report confirmed by server."
                            : "Delivery report confirmed. Your fleet must reconcile it.");
                });
            } catch (DriverApi.ApiFailure failure) {
                if (failure.status != 429 && failure.status < 500)
                    queue.needsReview(origin, me, id, failure.status);
                runOnUiThread(() -> { if (generation == sessionGeneration) {
                    render(); message(failure.status == 429 || failure.status >= 500
                            ? "Server unavailable. Assignment report is queued on this phone."
                            : "Server rejected assignment report (HTTP " + failure.status
                            + "). Review before retrying.");
                }});
            } catch (java.io.IOException offline) {
                runOnUiThread(() -> { if (generation == sessionGeneration) {
                    render(); message("Network unavailable. Assignment report is queued on this phone.");
                }});
            }
        });
    }

    private void replayOrderActions(DriverApi api, JSONObject me, JSONArray orders, int generation)
            throws Exception {
        OrderActionQueue queue = new OrderActionQueue(this);
        String origin = prefs.getString(DriverApi.ORIGIN, "");
        JSONArray pending = queue.forOwner(origin, me);
        for (int index = 0; index < pending.length() && generation == sessionGeneration; index++) {
            JSONObject choice = pending.getJSONObject(index);
            String id = choice.getString("order_id"), action = choice.getString("action");
            for (int row = 0; row < orders.length(); row++) {
                JSONObject order = orders.optJSONObject(row);
                if (order == null || !id.equals(order.optString("order_id"))) continue;
                String status = order.optString("status");
                boolean pickup = OrderActionQueue.PICKUP.equals(action);
                if (pickup && ("PICKED_UP".equals(status) || "DELIVERED_REPORTED".equals(status)
                        || "RECONCILED".equals(status))
                        || !pickup && ("DELIVERED_REPORTED".equals(status) || "RECONCILED".equals(status))) {
                    queue.remove(origin, me, id);
                } else if (pickup && "ASSIGNED".equals(status)
                        || !pickup && "PICKED_UP".equals(status)) {
                    if ("QUEUED".equals(choice.optString("state"))) {
                        try {
                            JSONObject confirmed = sendOrderAction(api, choice);
                            queue.remove(origin, me, id);
                            orders.put(row, confirmed);
                        } catch (DriverApi.ApiFailure failure) {
                            if (failure.status != 429 && failure.status < 500)
                                queue.needsReview(origin, me, id, failure.status);
                        } catch (java.io.IOException offline) {
                            // The durable report stays queued for a later refresh.
                        }
                    }
                } else {
                    queue.needsReview(origin, me, id, 409);
                }
                break;
            }
        }
    }

    private void retryOrderAction(String id) {
        if (identity == null) return;
        try {
            new OrderActionQueue(this).retry(prefs.getString(DriverApi.ORIGIN, ""), identity, id);
            render(); refresh();
        } catch (Exception error) { message(error.getMessage()); }
    }

    private void clearReviewedOrderAction(String id) {
        if (identity == null) return;
        JSONObject me = identity;
        String origin = prefs.getString(DriverApi.ORIGIN, "");
        new AlertDialog.Builder(this).setTitle("Clear local assignment report?")
                .setMessage("The server outcome will remain. Clear this unsent report only after reviewing the conflict with your fleet.")
                .setNegativeButton("Keep report", null)
                .setPositiveButton("Clear local report", (dialog, which) -> {
                    try {
                        new OrderActionQueue(this).remove(origin, me, id);
                        render(); message("Local assignment report cleared. Server outcome is unchanged.");
                    } catch (Exception error) { message(error.getMessage()); }
                }).show();
    }

    private JSONObject sendAdvisoryReply(DriverApi api, JSONObject choice) throws Exception {
        JSONObject body = new JSONObject().put("idempotency_key", choice.getString("idempotency_key"))
                .put("outcome", choice.getString("outcome"));
        return (JSONObject) api.call("POST", "/api/v1/driver/nudges/"
                + choice.getString("nudge_id") + "/respond", body);
    }

    private void retryAdvisoryReply(String id) {
        if (identity == null) return;
        try {
            new AdvisoryReplyQueue(this).retry(prefs.getString(DriverApi.ORIGIN, ""), identity, id);
            render(); refresh();
        } catch (Exception error) { message(error.getMessage()); }
    }

    private void clearReviewedAdvisoryReply(String id) {
        if (identity == null) return;
        JSONObject me = identity;
        String origin = prefs.getString(DriverApi.ORIGIN, "");
        new AlertDialog.Builder(this).setTitle("Clear local advisory reply?")
                .setMessage("The fleet's server outcome will remain. Clear this unsent choice only after reviewing the conflict.")
                .setNegativeButton("Keep choice", null)
                .setPositiveButton("Clear local choice", (dialog, which) -> {
                    try {
                        new AdvisoryReplyQueue(this).remove(origin, me, id);
                        render(); message("Local reply cleared. Server outcome is unchanged.");
                    } catch (Exception error) { message(error.getMessage()); }
                }).show();
    }

    private void replayAdvisoryReplies(DriverApi api, JSONObject me, JSONArray inbox, int generation)
            throws Exception {
        AdvisoryReplyQueue replies = new AdvisoryReplyQueue(this);
        String origin = prefs.getString(DriverApi.ORIGIN, "");
        JSONArray pending = replies.forOwner(origin, me);
        for (int index = 0; index < pending.length() && generation == sessionGeneration; index++) {
            JSONObject choice = pending.getJSONObject(index);
            String id = choice.getString("nudge_id"), outcome = choice.getString("outcome");
            for (int row = 0; row < inbox.length(); row++) {
                JSONObject nudge = inbox.optJSONObject(row);
                if (nudge == null || !id.equals(nudge.optString("nudge_id"))) continue;
                if (outcome.equals(nudge.optString("status"))) {
                    replies.remove(origin, me, id);
                } else if (!"PENDING".equals(nudge.optString("status"))) {
                    replies.needsReview(origin, me, id, 409);
                } else if ("QUEUED".equals(choice.optString("state"))) {
                    try {
                        JSONObject confirmed = sendAdvisoryReply(api, choice);
                        replies.remove(origin, me, id);
                        inbox.put(row, confirmed);
                    } catch (DriverApi.ApiFailure failure) {
                        if (failure.status != 429 && failure.status < 500)
                            replies.needsReview(origin, me, id, failure.status);
                    } catch (java.io.IOException offline) {
                        // Keep the durable choice for a later refresh or manual sync.
                    }
                }
                break;
            }
        }
    }

    private void reportIssue() {
        JSONObject me = identity;
        if (me == null) return;
        String[] categories = {"VEHICLE_FAULT", "CHARGER_PROBLEM", "ROUTE_PROBLEM",
                "TRIP_ASSISTANCE", "OTHER"};
        String category = categories[Math.max(0, Math.min(categories.length - 1,
                caseCategory.getSelectedItemPosition()))];
        String priority = casePriority.getSelectedItemPosition() == 1 ? "URGENT" : "ROUTINE";
        String title = caseTitleInput.getText().toString().trim();
        String description = caseDescriptionInput.getText().toString().trim();
        try {
            new ServiceCaseCommandQueue(this).create(prefs.getString(DriverApi.ORIGIN, ""), me,
                    category, priority, title, description);
            caseTitleInput.setText(""); caseDescriptionInput.setText("");
            dismissKeyboard(); render();
            message("Issue report saved on this phone. Syncing with your fleet.");
            refresh();
        } catch (Exception storageError) { message(storageError.getMessage()); }
    }

    private void askServiceFollowUp(String caseId) {
        JSONObject me = identity;
        if (me == null) return;
        EditText field = new EditText(this);
        field.setHint("Describe your update for the fleet");
        field.setMinLines(3); field.setMaxLines(6);
        field.setInputType(android.text.InputType.TYPE_CLASS_TEXT
                | android.text.InputType.TYPE_TEXT_FLAG_MULTI_LINE);
        field.setFilters(new android.text.InputFilter[] {new android.text.InputFilter.LengthFilter(1000)});
        new AlertDialog.Builder(this).setTitle("Add a service case update")
                .setView(field).setNegativeButton("Cancel", null)
                .setPositiveButton("Save update", (dialog, which) -> {
                    try {
                        new ServiceCaseCommandQueue(this).followUp(
                                prefs.getString(DriverApi.ORIGIN, ""), me, caseId,
                                field.getText().toString().trim());
                        render(); message("Follow-up saved on this phone. Syncing with your fleet.");
                        refresh();
                    } catch (Exception storageError) { message(storageError.getMessage()); }
                }).show();
    }

    private void showServiceMessages(String caseId) {
        if (identity == null) return;
        int generation = sessionGeneration;
        task(() -> {
            JSONArray messages = (JSONArray) DriverApi.saved(this).call("GET",
                    "/api/v1/driver/service-cases/" + caseId + "/messages", null);
            StringBuilder body = new StringBuilder();
            for (int index = 0; index < messages.length(); index++) {
                JSONObject item = messages.getJSONObject(index);
                if (body.length() > 0) {
                    body.append("\n\n");
                }
                body.append(item.optString("actor_kind")).append(" · ")
                        .append(item.optString("created_at")).append("\n")
                        .append(item.optString("body"));
            }
            runOnUiThread(() -> {
                if (generation != sessionGeneration || identity == null) return;
                new AlertDialog.Builder(this).setTitle("Case " + caseId + " replies")
                        .setMessage(body.length() == 0 ? "No follow-up messages yet." : body.toString())
                        .setPositiveButton("Close", null).show();
            });
        });
    }

    private void retryServiceCommand(String commandKey) {
        if (identity == null) return;
        try {
            new ServiceCaseCommandQueue(this).retry(prefs.getString(DriverApi.ORIGIN, ""),
                    identity, commandKey);
            render(); refresh();
        } catch (Exception error) { message(error.getMessage()); }
    }

    private void clearReviewedServiceCommand(String commandKey) {
        if (identity == null) return;
        JSONObject me = identity;
        String origin = prefs.getString(DriverApi.ORIGIN, "");
        new AlertDialog.Builder(this).setTitle("Clear local service command?")
                .setMessage("The server record will remain. Clear only after reviewing why this command failed.")
                .setNegativeButton("Keep command", null)
                .setPositiveButton("Clear local command", (dialog, which) -> {
                    try {
                        new ServiceCaseCommandQueue(this).remove(origin, me, commandKey);
                        render(); message("Local command cleared. Server records are unchanged.");
                    } catch (Exception error) { message(error.getMessage()); }
                }).show();
    }

    private void replayServiceCommands(DriverApi api, JSONObject me, int generation) throws Exception {
        ServiceCaseCommandQueue queue = new ServiceCaseCommandQueue(this);
        String origin = prefs.getString(DriverApi.ORIGIN, "");
        JSONArray pending = queue.forOwner(origin, me);
        for (int index = 0; index < pending.length() && generation == sessionGeneration; index++) {
            JSONObject command = pending.getJSONObject(index);
            if (!"QUEUED".equals(command.optString("state"))) continue;
            String commandKey = command.getString("command_key");
            try {
                api.call("POST", command.getString("path"), command.getJSONObject("body"));
                queue.remove(origin, me, commandKey);
            } catch (DriverApi.ApiFailure failure) {
                if (failure.status != 429 && failure.status < 500) {
                    queue.needsReview(origin, me, commandKey, failure.status);
                    if ("CREATE".equals(command.optString("kind"))) break;
                    continue;
                }
                break;
            } catch (java.io.IOException offline) {
                break;
            }
        }
    }

    private void requestGps() {
        if (identity == null || activeTrip == null) return;
        if (!consentLoaded || !PhoneTripConsent.isGranted(this, identity.optString("tenant_id"),
                identity.optString("driver_id"))) {
            message("Agree to trip phone GPS and motion collection before starting GPS.");
            return;
        }
        boolean location = checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED
                || checkSelfPermission(Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED;
        boolean notification = Build.VERSION.SDK_INT < 33
                || checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED;
        if (!location || !notification) {
            if (Build.VERSION.SDK_INT >= 33)
                requestPermissions(new String[] {Manifest.permission.ACCESS_FINE_LOCATION,
                        Manifest.permission.ACCESS_COARSE_LOCATION, Manifest.permission.POST_NOTIFICATIONS}, LOCATION_PERMISSION);
            else requestPermissions(new String[] {Manifest.permission.ACCESS_FINE_LOCATION,
                    Manifest.permission.ACCESS_COARSE_LOCATION}, LOCATION_PERMISSION);
            return;
        }
        startGpsService();
    }

    @Override public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grants) {
        super.onRequestPermissionsResult(requestCode, permissions, grants);
        if (requestCode == ROUTE_PERMISSION) {
            routePanel.removeAllViews();
            routePanel.addView(label("Tap the phone route preview again to use a current location.", 13, MUTED));
            return;
        }
        if (requestCode == CHARGER_PERMISSION) {
            boolean allowed = checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED
                    || checkSelfPermission(Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED;
            if (allowed) findPhoneChargers();
            else {
                chargerPanel.removeAllViews();
                chargerPanel.addView(label("Location permission is required to find chargers near your phone.", 13, MUTED));
            }
            return;
        }
        if (requestCode == LOCATION_PERMISSION) {
            boolean location = checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED
                    || checkSelfPermission(Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED;
            boolean notification = Build.VERSION.SDK_INT < 33
                    || checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED;
            if (location && notification) startGpsService();
            else message("Location and notification permissions are required to show active trip tracking.");
        }
    }

    private void startGpsService() {
        if (identity == null || activeTrip == null) return;
        if (!consentLoaded || !PhoneTripConsent.isGranted(this, identity.optString("tenant_id"),
                identity.optString("driver_id"))) {
            message("Trip phone-data consent is required before GPS starts.");
            return;
        }
        Intent intent = new Intent(this, GpsTrackingService.class); intent.setAction(GpsTrackingService.ACTION_START);
        intent.putExtra(GpsTrackingService.EXTRA_TRIP, activeTrip.optString("trip_id"));
        intent.putExtra(GpsTrackingService.EXTRA_DRIVER, identity.optString("driver_id"));
        intent.putExtra(GpsTrackingService.EXTRA_VEHICLE, identity.optString("vehicle_id"));
        intent.putExtra(GpsTrackingService.EXTRA_TENANT, identity.optString("tenant_id"));
        try { startForegroundService(intent); message("Phone GPS started for this active trip."); }
        catch (Exception error) { message("Could not start GPS: " + error.getMessage()); }
    }

    private void stopGps() {
        Intent intent = new Intent(this, GpsTrackingService.class); intent.setAction(GpsTrackingService.ACTION_STOP);
        try { startService(intent); message("Phone GPS stopped. Queued points remain for upload."); }
        catch (Exception error) { message("Could not stop GPS: " + error.getMessage()); }
    }

    private void grantPhoneConsent() {
        if (identity == null || !consentLoaded) return;
        String tenantId = identity.optString("tenant_id"), driverId = identity.optString("driver_id");
        if (PhoneTripConsent.isPending(this, tenantId, driverId)) return;
        grantConsent.setEnabled(false);
        int generation = sessionGeneration;
        task(() -> {
            JSONObject result = (JSONObject) DriverApi.saved(this).call("POST",
                    "/api/v1/driver/consent/phone-trip", new JSONObject().put("granted", true));
            if (generation != sessionGeneration) return;
            if (!result.optBoolean("granted") || !PhoneTripConsent.record(this, tenantId, driverId, true))
                throw new IllegalStateException("consent grant was not saved");
            runOnUiThread(() -> {
                if (generation != sessionGeneration) return;
                render(); message("Trip GPS and motion consent recorded. Start GPS separately for an active trip.");
                GpsUploadWorker.schedule(this);
            });
        });
    }

    private void withdrawPhoneConsent() {
        if (identity == null || !consentLoaded) return;
        String tenantId = identity.optString("tenant_id"), driverId = identity.optString("driver_id");
        if (!PhoneTripConsent.beginWithdrawal(this, tenantId, driverId)) {
            message("Could not save withdrawal locally. Try again before tracking.");
            return;
        }
        stopGps();
        render();
        int generation = sessionGeneration;
        task(() -> {
            DriverApi.saved(this).call("POST", "/api/v1/driver/consent/phone-trip",
                    new JSONObject().put("granted", false));
            if (generation != sessionGeneration) return;
            if (!PhoneTripConsent.record(this, tenantId, driverId, false))
                throw new IllegalStateException("consent withdrawal state was not saved");
            runOnUiThread(() -> {
                if (generation != sessionGeneration) return;
                render(); message("Trip phone-data consent withdrawn. Queued GPS remains on this device.");
            });
        });
    }

    private void confirmErasePhoneData() {
        if (identity == null || activeTrip != null) return;
        new AlertDialog.Builder(this)
                .setTitle("Erase trip phone data?")
                .setMessage("Cloud GPS and motion for completed trips will be erased. Queued points for this connected vehicle and driver will be cleared on this phone after cloud confirmation. This cannot be undone; other devices and backups need separate cleanup.")
                .setNegativeButton("Cancel", (dialog, which) -> { })
                .setPositiveButton("Erase cloud and local data", (dialog, which) -> erasePhoneData())
                .show();
    }

    private void erasePhoneData() {
        if (identity == null || activeTrip != null) return;
        String tenantId = identity.optString("tenant_id"), driverId = identity.optString("driver_id");
        String vehicleId = identity.optString("vehicle_id");
        if (!PhoneTripConsent.beginWithdrawal(this, tenantId, driverId)) {
            message("Could not close phone-data collection locally. Erasure was not sent.");
            return;
        }
        stopGps();
        erasureInFlight = true;
        render();
        int generation = sessionGeneration;
        task(() -> {
            try {
                JSONObject result = (JSONObject) DriverApi.saved(this).call("DELETE",
                        "/api/v1/driver/phone-data", null);
                if (generation != sessionGeneration) return;
                if (!PhoneTripConsent.record(this, tenantId, driverId, false))
                    throw new IllegalStateException("Cloud data erased; local consent state needs repair");
                int removed;
                try (GpsQueue gps = new GpsQueue(this)) {
                    removed = gps.eraseForAssignment(vehicleId, driverId);
                    queuedPoints = gps.pendingCount();
                }
                if (queuedPoints == 0) GpsSyncStatus.clear(this);
                refreshNow(generation);
                int localRemoved = removed;
                runOnUiThread(() -> {
                    if (generation != sessionGeneration) return;
                    render();
                    message("Cloud phone data erased: " + result.optInt("deleted_samples")
                            + " sample(s). Local queued points cleared: " + localRemoved + ".");
                });
            } finally {
                erasureInFlight = false;
                runOnUiThread(this::render);
            }
        });
    }

    @Override protected void onDestroy() {
        foreground = false;
        stopUpdates();
        ++sessionGeneration;
        mainHandler.removeCallbacks(queueRefresh);
        mainHandler.removeCallbacks(advisoryRefresh);
        updatesNetwork.shutdownNow();
        network.shutdown();
        super.onDestroy();
    }
}
