package com.trickee.driver;

import android.app.Activity;
import android.app.AlertDialog;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.WindowManager;
import android.widget.*;
import java.util.UUID;
import java.util.concurrent.*;
import org.json.*;

/** Phone only, authenticated native workspace. Nothing private is passed through an Intent. */
public final class WorkspaceActivity extends Activity {
  private static final String BASE = "/api/v1/driver/workspace";
  private final ExecutorService worker = Executors.newSingleThreadExecutor();
  private final Handler main = new Handler(Looper.getMainLooper());
  private LinearLayout body;
  private volatile boolean active;
  private boolean busy;
  private volatile int generation;
  private Future<?> request;
  private volatile long verifiedAt;
  private boolean privacyCovered;
  private String owner;
  private String credentialContext;
  private AlertDialog dialog;
  private final Runnable safety = new Runnable() {
    public void run() {
      if (active && !busy)
        perform(api -> {
          check(api);
          return null;
        }, ignored -> {}, true);
      if (active)
        main.postDelayed(this, 4000);
    }
  };
  private final Runnable watchdog = new Runnable() {
    public void run() {
      if (active && !privacyCovered
          && !WorkspaceRules.freshSafety(verifiedAt, android.os.SystemClock.elapsedRealtime())) {
        screen("Parked verification delayed. Refresh when the vehicle connection is available.");
        privacyCovered = true;
      }
      if (active)
        main.postDelayed(this, 1000);
    }
  };
  interface Work {
    Object run(DriverApi api) throws Exception;
  }
  interface Result {
    void show(Object value) throws Exception;
  }
  @Override
  public void onCreate(Bundle saved) {
    super.onCreate(saved);
    getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);
  }
  @Override
  protected void onResume() {
    super.onResume();
    active = true;
    screen("Checking parked vehicle and workspace access...");
    home();
    main.postDelayed(safety, 4000);
    main.postDelayed(watchdog, 1000);
  }
  @Override
  protected void onPause() {
    active = false;
    generation++;
    main.removeCallbacksAndMessages(null);
    if (request != null)
      request.cancel(true);
    busy = false;
    clear();
    super.onPause();
  }
  @Override
  protected void onDestroy() {
    worker.shutdownNow();
    super.onDestroy();
  }
  private void clear() {
    if (dialog != null) {
      dialog.dismiss();
      dialog = null;
    }
    if (body != null)
      body.removeAllViews();
  }
  private void screen(String title) {
    clear();
    privacyCovered = false;
    ScrollView scroll = new ScrollView(this);
    body = new LinearLayout(this);
    body.setOrientation(LinearLayout.VERTICAL);
    body.setPadding(24, 24, 24, 40);
    scroll.addView(body);
    setContentView(scroll);
    text(title);
    button("Workspace home / refresh", this::home);
  }
  private void text(String value) {
    TextView label = new TextView(this);
    label.setText(value);
    label.setTextSize(16);
    label.setPadding(0, 12, 0, 12);
    body.addView(label);
  }
  private void button(String title, Runnable run) {
    Button button = new Button(this);
    button.setText(title);
    button.setMinHeight((int) (48 * getResources().getDisplayMetrics().density + 0.5f));
    body.addView(button);
    button.setOnClickListener(v -> {
      if (!busy)
        run.run();
    });
  }
  private EditText field(LinearLayout panel, String label, String value) {
    TextView caption = new TextView(this);
    caption.setText(label);
    panel.addView(caption);
    EditText input = new EditText(this);
    input.setContentDescription(label);
    input.setText(value);
    input.setSingleLine(false);
    input.setSaveEnabled(false);
    panel.addView(input);
    return input;
  }
  private void check(DriverApi api) throws Exception {
    android.content.SharedPreferences settings =
        getSharedPreferences(DriverApi.PREFS, MODE_PRIVATE);
    String context = settings.getString(DriverApi.ORIGIN, "") + "|"
        + settings.getString("driver_credential_sealed", "");
    if (credentialContext != null && !credentialContext.equals(context))
      throw new SecurityException("Connection credentials changed. Reopen workspace.");
    credentialContext = context;
    JSONObject me = (JSONObject) api.call("GET", "/api/v1/driver/me", null);
    String identity = me.optString("tenant_id") + "/" + me.optString("driver_id") + "/"
        + me.optString("vehicle_id");
    if (owner != null && !owner.equals(identity))
      throw new SecurityException("Connected assignment changed. Reopen workspace.");
    owner = identity;
    if (!WorkspaceRules.parked(me.optJSONObject("twin")))
      throw new SecurityException("Personal workspace requires current parked vehicle telemetry.");
    verifiedAt = android.os.SystemClock.elapsedRealtime();
  }
  private void perform(Work work, Result done) {
    perform(work, done, false);
  }
  private void perform(Work work, Result done, boolean preserveScreen) {
    if (!active || busy)
      return;
    if (!preserveScreen)
      screen("Checking workspace access...");
    busy = true;
    int epoch = generation;
    request = worker.submit(() -> {
      try {
        DriverApi api = DriverApi.saved(this);
        check(api);
        if (Thread.currentThread().isInterrupted() || !active || epoch != generation)
          return;
        Object result = work.run(api);
        check(api);
        runOnUiThread(() -> {
          if (!active || epoch != generation)
            return;
          busy = false;
          try {
            done.show(result);
          } catch (Exception e) {
            failure(e);
          }
        });
      } catch (Exception error) {
        runOnUiThread(() -> {
          if (active && epoch == generation) {
            busy = false;
            failure(error);
          }
        });
      }
    });
  }
  private void failure(Exception error) {
    screen("Workspace unavailable");
    if (error instanceof SecurityException) {
      text(error.getMessage());
      button("Close workspace", this::finish);
      return;
    }
    if (error instanceof DriverApi.ApiFailure) {
      int code = ((DriverApi.ApiFailure) error).status;
      text(code == 409 ? "Changed elsewhere. Refresh and review before editing again."
              : code == 401 || code == 403
              ? "Access unavailable. Return to connection settings."
              : "Server returned HTTP " + code + ". Refresh and review before retrying.");
    } else
      text("Unable to load workspace. Check connection and refresh. No automatic mutation retry "
          + "was made.");
  }
  private void home() {
    perform(
        api
        -> {
          JSONObject data = new JSONObject();
          data.put("preferences", api.call("GET", BASE + "/preferences", null));
          data.put("summary", api.call("GET", BASE + "/summary", null));
          return data;
        },
        value -> {
          screen("Personal workspace (parked)");
          JSONObject data = (JSONObject) value;
          JSONObject prefs = data.getJSONObject("preferences");
          text("Time zone: " + prefs.optString("timezone") + "\nDisplay name: "
              + prefs.optString("display_name") + "\nReminders: " + prefs.optBoolean("reminders"));
          text("Daily summary: " + data.getJSONObject("summary").optString("date") + "\n"
              + data.getJSONObject("summary").optString("message"));
          button("Edit preferences", () -> editPreferences(prefs));
          button("Calendar events", this::events);
          button("Reminders", this::notices);
          button("Conversations", this::conversations);
        });
  }
  private void editPreferences(JSONObject preferences) {
    LinearLayout panel = new LinearLayout(this);
    panel.setOrientation(LinearLayout.VERTICAL);
    EditText name =
        field(panel, "Display name (up to 80 characters)", preferences.optString("display_name"));
    EditText zone =
        field(panel, "IANA summary time zone", preferences.optString("timezone", "UTC"));
    CheckBox reminders = new CheckBox(this);
    reminders.setText("Enable in-app reminders");
    reminders.setChecked(preferences.optBoolean("reminders", true));
    panel.addView(reminders);
    dialog = new AlertDialog.Builder(this)
                 .setTitle("Workspace preferences")
                 .setView(panel)
                 .setNegativeButton("Cancel", null)
                 .setPositiveButton("Save", null)
                 .create();
    dialog.setOnShowListener(
        view -> dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v -> {
          if (busy) {
            Toast.makeText(this, "Checking access. Try Save again.", Toast.LENGTH_SHORT).show();
            return;
          }
          try {
            JSONObject payload = WorkspaceRules.preferences(
                name.getText().toString(), zone.getText().toString().trim(), reminders.isChecked());
            dialog.dismiss();
            mutate("PUT", "/preferences", payload, this::home);
          } catch (IllegalArgumentException invalid) {
            zone.setError(invalid.getMessage());
          }
        }));
    dialog.show();
  }
  private void events() {
    events(0);
  }
  private void events(int offset) {
    perform(api -> api.workspacePage("/events", offset), value -> {
      screen("Calendar events");
      button("Create event", () -> editEvent(null));
      JSONObject page = (JSONObject) value;
      text("Total: " + page.optInt("total"));
      if (offset > 0)
        button("Previous events", () -> events(Math.max(0, offset - 100)));
      if (offset + 100 < page.optInt("total"))
        button("Next events", () -> events(offset + 100));
      JSONArray items = page.getJSONArray("items");
      for (int i = 0; i < items.length(); i++) {
        JSONObject event = items.getJSONObject(i);
        text(event.optString("title") + "\n" + event.optString("starts_at") + " to "
            + event.optString("ends_at") + "\n"
            + (event.optBoolean("completed") ? "Completed" : "Pending"));
        button("Edit " + event.optString("title"), () -> editEvent(event));
        button(event.optBoolean("completed") ? "Mark pending" : "Complete event",
            ()
                -> mutate("PUT", "/events/" + WorkspaceRules.id(event.optString("event_id")),
                    eventPayload(event, !event.optBoolean("completed")), this::events));
        button("Delete " + event.optString("title"),
            ()
                -> confirm("Delete event?",
                    ()
                        -> perform(api
                            -> api.deleteWorkspaceEvent(
                                event.optString("event_id"), event.getInt("revision")),
                            ignored -> events())));
      }
    });
  }
  private JSONObject eventPayload(JSONObject event, boolean completed) {
    try {
      JSONObject out = new JSONObject();
      for (String key : new String[] {"title", "description", "starts_at", "ends_at", "timezone",
               "category", "remind_minutes", "revision"})
        if (event.has(key))
          out.put(key, event.get(key));
      out.put("completed", completed);
      return out;
    } catch (JSONException bad) {
      throw new IllegalArgumentException(bad);
    }
  }
  private void editEvent(JSONObject event) {
    LinearLayout panel = new LinearLayout(this);
    panel.setOrientation(LinearLayout.VERTICAL);
    panel.setPadding(24, 0, 24, 0);
    String[] names = {
        "title", "description", "starts_at", "ends_at", "timezone", "category", "remind_minutes"};
    String[] labels = {"Title", "Description", "Start ISO date with offset",
        "End ISO date with offset", "IANA time zone",
        "Category: TASK, MAINTENANCE, CHARGING, APPOINTMENT", "Remind minutes (0 to 10080)"};
    String[] defaults = {"", "", "", "", "UTC", "TASK", "15"};
    EditText[] fields = new EditText[names.length];
    for (int i = 0; i < names.length; i++)
      fields[i] = field(
          panel, labels[i], event == null ? defaults[i] : event.optString(names[i], defaults[i]));
    ScrollView scroll = new ScrollView(this);
    scroll.addView(panel);
    dialog = new AlertDialog.Builder(this)
                 .setTitle(event == null ? "Create event" : "Edit event")
                 .setView(scroll)
                 .setNegativeButton("Cancel", null)
                 .setPositiveButton("Save", null)
                 .create();
    dialog.setOnShowListener(v
        -> dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(w -> {
      if (busy) {
        Toast.makeText(this, "Checking access. Try Save again.", Toast.LENGTH_SHORT).show();
        return;
      }
      try {
        JSONObject payload = new JSONObject();
        for (int i = 0; i < names.length; i++)
          payload.put(names[i],
              i == 6 ? Integer.parseInt(fields[i].getText().toString())
                     : fields[i].getText().toString().trim());
        WorkspaceRules.dates(payload.getString("starts_at"), payload.getString("ends_at"),
            payload.getString("timezone"));
        if (event != null) {
          payload.put("revision", event.getInt("revision"));
          payload.put("completed", event.optBoolean("completed"));
        } else
          payload.put("request_id", UUID.randomUUID().toString());
        dialog.dismiss();
        mutate(event == null ? "POST" : "PUT",
            event == null ? "/events" : "/events/" + WorkspaceRules.id(event.optString("event_id")),
            payload, this::events);
      } catch (Exception invalid) {
        fields[0].setError("Check fields. Dates need explicit UTC offsets; end must follow start.");
      }
    }));
    dialog.show();
  }
  private void notices() {
    notices(0);
  }
  private void notices(int offset) {
    perform(api -> api.workspacePage("/notifications", offset), value -> {
      screen("Reminders");
      JSONObject page = (JSONObject) value;
      text("Unread: " + page.optInt("unread") + " / " + page.optInt("total"));
      if (offset > 0)
        button("Previous reminders", () -> notices(Math.max(0, offset - 100)));
      if (offset + 100 < page.optInt("total"))
        button("Next reminders", () -> notices(offset + 100));
      button("Mark all read",
          () -> mutate("POST", "/notifications/read-all", new JSONObject(), this::notices));
      JSONArray items = page.getJSONArray("items");
      for (int i = 0; i < items.length(); i++) {
        JSONObject item = items.getJSONObject(i);
        text(item.optString("message") + "\n" + item.optString("created_at"));
        button("Mark read", () -> notice(item, false));
        button("Archive reminder", () -> notice(item, true));
      }
    });
  }
  private void notice(JSONObject notice, boolean archive) {
    try {
      mutate("PUT", "/notifications/" + WorkspaceRules.id(notice.optString("notice_id")),
          new JSONObject().put("read", true).put("archived", archive), this::notices);
    } catch (JSONException error) {
      failure(error);
    }
  }
  private void conversations() {
    perform(api -> api.call("GET", BASE + "/conversations", null), value -> {
      screen("Saved conversations (up to 50)");
      button("New conversation",
          ()
              -> prompt("Conversation title",
                  title
                  -> mutate("POST", "/conversations", new JSONObject().put("title", title),
                      this::conversations)));
      JSONArray items = (JSONArray) value;
      for (int i = 0; i < items.length(); i++) {
        JSONObject item = items.getJSONObject(i);
        String id = WorkspaceRules.id(item.optString("conversation_id"));
        button(item.optString("title"), () -> messages(id));
        button("Delete " + item.optString("title"),
            ()
                -> confirm("Delete conversation and history?",
                    () -> mutate("DELETE", "/conversations/" + id, null, this::conversations)));
      }
    });
  }
  private void messages(String id) {
    messages(id, 0);
  }
  private void messages(String id, int offset) {
    perform(api -> api.workspacePage("/conversations/" + id + "/messages", offset), value -> {
      screen("Conversation history");
      JSONObject page = (JSONObject) value;
      JSONArray items = page.getJSONArray("items");
      text("Total messages: " + page.optInt("total"));
      if (offset > 0)
        button("Earlier messages", () -> messages(id, Math.max(0, offset - 100)));
      if (offset + 100 < page.optInt("total"))
        button("Later messages", () -> messages(id, offset + 100));
      for (int i = 0; i < items.length(); i++) {
        JSONObject item = items.getJSONObject(i);
        text(item.optString("speaker") + ": " + item.optString("content"));
      }
      button("Send message",
          ()
              -> prompt("Message (up to 2000 characters)",
                  message
                  -> mutate("POST", "/conversations/" + id + "/messages",
                      new JSONObject()
                          .put("message", message)
                          .put("request_id", UUID.randomUUID().toString()),
                      () -> messages(id))));
    });
  }
  interface TextAction {
    void run(String text) throws Exception;
  }
  private void prompt(String title, TextAction action) {
    LinearLayout panel = new LinearLayout(this);
    panel.setOrientation(LinearLayout.VERTICAL);
    EditText input = field(panel, title, "");
    dialog = new AlertDialog.Builder(this)
                 .setTitle(title)
                 .setView(panel)
                 .setNegativeButton("Cancel", null)
                 .setPositiveButton("Save", null)
                 .create();
    dialog.setOnShowListener(
        view -> dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v -> {
          if (busy) {
            Toast.makeText(this, "Checking access. Try Save again.", Toast.LENGTH_SHORT).show();
            return;
          }
          try {
            String value = input.getText().toString().trim();
            dialog.dismiss();
            action.run(value);
          } catch (Exception bad) {
            failure(bad);
          }
        }));
    dialog.show();
  }
  private void confirm(String title, Runnable action) {
    dialog = new AlertDialog.Builder(this)
                 .setTitle(title)
                 .setNegativeButton("Cancel", null)
                 .setPositiveButton("Delete", null)
                 .create();
    dialog.setOnShowListener(
        view -> dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v -> {
          if (busy) {
            Toast.makeText(this, "Checking access. Try Delete again.", Toast.LENGTH_SHORT).show();
            return;
          }
          dialog.dismiss();
          action.run();
        }));
    dialog.show();
  }
  private void mutate(String method, String path, JSONObject payload, Runnable refresh) {
    perform(api -> api.call(method, BASE + path, payload), ignored -> refresh.run());
  }
}
