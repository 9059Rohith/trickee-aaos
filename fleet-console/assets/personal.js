/* Personal calendar and read-only assistant; operational actions stay in their existing workflows. */
(() => {
  "use strict";
  const audience = location.pathname.startsWith("/driver")
    ? "driver"
    : "operator";
  const base = `/api/v1/${audience}/workspace`;
  const context = () =>
    window.trickeeWorkspaceContext?.() || { connected: false, generation: 0 };
  const same = (stamp) =>
    context().connected && !context().driving && context().generation === stamp;
  let generation = 0,
    busy = false,
    events = [],
    conversations = [],
    conversation = "",
    messageOffset = 0;
  let currentDate = new Date(),
    editing = null,
    pendingMessage = null,
    pendingEvent = null,
    preferences = { timezone: "UTC" };
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const launcher = document.createElement("button");
  launcher.id = "personalLauncher";
  launcher.className = "personal-launcher";
  launcher.type = "button";
  launcher.textContent = "Planner & assistant";
  launcher.setAttribute("aria-haspopup", "dialog");
  launcher.setAttribute("aria-controls", "personalDialog");
  const dialog = document.createElement("dialog");
  dialog.id = "personalDialog";
  dialog.className = "workspace-dialog personal-dialog";
  dialog.setAttribute("aria-labelledby", "personalTitle");
  dialog.innerHTML = `<div class="workspace-heading"><h2 id="personalTitle">Your daily workspace</h2><button id="personalClose" type="button">Close</button></div>
    <p id="personalStatus" role="status" aria-live="polite"></p><button id="personalRefresh" type="button">Refresh workspace</button><p id="personalDaily"></p>
    <nav class="personal-tabs" aria-label="Personal workspace pages"><button type="button" data-personal-tab="calendar" aria-pressed="true">Calendar</button><button type="button" data-personal-tab="assistant" aria-pressed="false">Assistant</button><button type="button" data-personal-tab="notifications" aria-pressed="false">Notifications</button><button type="button" data-personal-tab="settings" aria-pressed="false">Settings & help</button></nav>
    <section data-personal-page="calendar"><h3>Personal calendar</h3><p class="workspace-help">Separate from confirmed fleet plans. Calendar entry times use your device zone: <span id="personalDeviceZone"></span>. The configured server worker stores due reminders while this page is closed. Refresh Notifications to read them. No external push or email delivery.</p>
      <div class="personal-toolbar"><button id="personalPrevious" type="button" aria-label="Previous calendar period">Previous</button><button id="personalToday" type="button">Today</button><button id="personalNext" type="button" aria-label="Next calendar period">Next</button><label>View<select id="personalView"><option value="month">Month</option><option value="week">Week</option><option value="day">Day</option><option value="agenda" selected>Agenda</option></select></label><label>Find event<input id="personalSearch" type="search" maxlength="120" placeholder="Title or description"></label><button id="personalSearchApply" type="button">Search events</button></div>
      <h4 id="personalRange"></h4><div id="personalEvents"></div><button id="personalMoreEvents" type="button" hidden>Load more events</button>
      <h3 id="personalEventHeading">Create an event</h3><form id="personalEventForm"><label>Title<input id="personalEventTitle" maxlength="120" required></label><label>Description<textarea id="personalEventDescription" maxlength="2000" rows="2"></textarea></label><label>Starts (device local time)<input id="personalEventStart" type="datetime-local" required></label><label>Ends (device local time)<input id="personalEventEnd" type="datetime-local" required></label><label>Category<select id="personalCategory"><option value="TASK">Task</option><option value="MAINTENANCE">Maintenance</option><option value="CHARGING">Charging</option><option value="APPOINTMENT">Appointment</option></select></label><label>Remind minutes before<input id="personalRemind" type="number" min="0" max="10080" value="15" required></label><div class="personal-toolbar"><button type="submit">Save event</button><button id="personalCancelEdit" type="button" hidden>Cancel editing</button></div></form>
    </section>
    <section data-personal-page="assistant" hidden><h3>Read-only daily assistant</h3><p class="workspace-help">Rule-based assistance using your authorized record counts. No AI provider is connected. No vehicle controls or autonomous changes. Never paste credentials or sensitive personal data.</p><div class="personal-toolbar"><label>Conversation<select id="personalConversations"></select></label><button id="personalNewChat" type="button">New conversation</button><button id="personalDeleteChat" type="button">Delete conversation</button></div><button id="personalOlderMessages" type="button" hidden>Earlier messages</button><div id="personalMessages" role="log" aria-label="Assistant conversation" aria-live="polite"></div><form id="personalChatForm"><label>Message<textarea id="personalQuestion" maxlength="2000" required rows="3"></textarea></label><button type="submit">Send message</button></form></section>
    <section data-personal-page="notifications" hidden><h3>Personal reminders</h3><p class="workspace-help">Operational alerts and driver messages remain in their existing inboxes. These are personal calendar reminders.</p><button id="personalReadAll" type="button">Mark all read</button><div id="personalNotices"></div><button id="personalMoreNotices" type="button" hidden>More reminders</button></section>
    <section data-personal-page="settings" hidden><h3>Preferences</h3><form id="personalSettingsForm"><label>Display name<input id="personalName" maxlength="80"></label><label>Daily-summary time zone<input id="personalZone" required placeholder="Asia/Kolkata"></label><label class="workspace-preference"><input id="personalReminders" type="checkbox">Generate in-app calendar reminders</label><button type="submit">Save preferences</button></form><button id="personalExport" type="button">Export personal workspace</button>
      <h3>Account access & recovery</h3><div id="personalAccount"></div><h3>Help & support</h3><p>Fleet operators: use daily plans for confirmed schedules, advisories for guidance, dispatch for pickup proposals, and service cases for driver reports. Driver: review confirmed plans and proposals before starting a trip. GPS is foreground-only and requires consent. Use your service-case inbox to contact fleet staff; it is not an emergency service.</p><p>Personal calendar deletion removes the event and its reminders. Conversation deletion removes its messages. Phone-data export and erasure use the separate phone-data controls. Existing backups require separate retention and deletion handling.</p></section>`;
  dialog
    .querySelector("#personalEventForm .personal-toolbar")
    .insertAdjacentHTML(
      "beforebegin",
      `<label>Repeat<select id="personalRepeat"><option value="">Once</option><option value="DAILY">Daily</option><option value="WEEKLY">Weekly</option><option value="MONTHLY">Monthly</option></select></label><label>Repeat every<input id="personalRepeatInterval" type="number" min="1" max="52" value="1"></label><label>Repeat limit<select id="personalRepeatBound"><option value="count">Occurrence count</option><option value="until">Through local date/time</option></select></label><label>Repeat through (device local time)<input id="personalRepeatUntil" type="datetime-local"></label><label>Occurrences (including first)<input id="personalRepeatCount" type="number" min="1" max="366" value="6"></label><p class="workspace-help">Repeats keep the device IANA zone and local start time. Month ends clamp, then return to the original day. Spring gaps shift forward; fall overlaps use the earlier time. Events keep elapsed duration. Maximum 366 occurrences within your 2,000-event quota.</p><label id="personalScopeLabel" hidden>Edit scope<select id="personalEditScope"><option value="occurrence">Only this occurrence</option><option value="series">Entire series: rebuild from these dates</option></select></label>`,
    );
  document.body.append(launcher, dialog);
  const $ = (id) => dialog.querySelector(`#${id}`);
  $("personalDeviceZone").textContent = zone;
  const status = (text) => {
    $("personalStatus").textContent = text;
  };
  function dateInput(date) {
    const local = new Date(date);
    local.setMinutes(local.getMinutes() - local.getTimezoneOffset());
    return local.toISOString().slice(0, 16);
  }
  function newEvent() {
    pendingEvent = null;
    editing = null;
    $("personalEventForm").reset();
    $("personalEventHeading").textContent = "Create an event";
    $("personalCancelEdit").hidden = true;
    $("personalScopeLabel").hidden = true;
    const start = new Date();
    start.setSeconds(0, 0);
    $("personalEventStart").value = dateInput(start);
    $("personalEventEnd").value = dateInput(new Date(+start + 3600000));
  }
  const controllers = new Set();
  function clear() {
    generation++;
    for (const controller of controllers) controller.abort();
    events = [];
    conversations = [];
    conversation = "";
    pendingMessage = null;
    preferences = { timezone: "UTC" };
    messageOffset = 0;
    noticeOffset = 0;
    currentDate = new Date();
    $("personalView").value = "agenda";
    editing = null;
    for (const id of [
      "personalDaily",
      "personalEvents",
      "personalMessages",
      "personalNotices",
      "personalAccount",
      "personalConversations",
    ])
      $(id).replaceChildren();
    $("personalDeleteChat").disabled = true;
    $("personalConversations").disabled = true;
    $("personalChatForm").reset();
    $("personalSettingsForm").reset();
    $("personalSearch").value = "";
    dialog.querySelector("[data-personal-tab=notifications]").textContent =
      "Notifications";
    newEvent();
    status("Connect in the main portal to load your private workspace.");
  }
  async function request(path, options = {}) {
    const controller = new AbortController();
    controllers.add(controller);
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      return await window.api(path.startsWith("/api/") ? path : base + path, {
        ...options,
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timeout);
      controllers.delete(controller);
    }
  }
  async function action(work) {
    if (busy) return;
    if (!context().connected) {
      clear();
      return;
    }
    if (context().driving) {
      clear();
      status("Park safely before using the personal workspace.");
      return;
    }
    const stamp = context().generation,
      run = ++generation;
    busy = true;
    dialog.setAttribute("aria-busy", "true");
    status("Working…");
    for (const button of dialog.querySelectorAll(
      "button:not(#personalClose),input,select,textarea",
    ))
      button.disabled = true;
    try {
      await work(() => same(stamp) && run === generation);
    } catch (error) {
      if (
        (error.status === 401 || error.status === 403) &&
        same(stamp) &&
        run === generation
      ) {
        clear();
        status(
          "Access expired or is no longer permitted. Reconnect in the main portal.",
        );
        return;
      }
      if (same(stamp) && run === generation)
        status(
          `Unable to complete: ${error.message}. Retry when connected. Your unsent input remains on this page.`,
        );
    } finally {
      busy = false;
      dialog.removeAttribute("aria-busy");
      for (const button of dialog.querySelectorAll(
        "button,input,select,textarea",
      ))
        button.disabled = false;
      $("personalDeleteChat").disabled = !conversation;
      $("personalConversations").disabled = !conversations.length;
    }
  }
  const button = (label, handler) => {
    const el = document.createElement("button");
    el.disabled = busy;
    el.type = "button";
    el.textContent = label;
    el.addEventListener("click", handler);
    return el;
  };
  function range() {
    const start = new Date(currentDate);
    start.setHours(0, 0, 0, 0);
    const end = new Date(start);
    switch ($("personalView").value) {
      case "month":
        start.setDate(1);
        end.setMonth(start.getMonth() + 1, 1);
        break;
      case "week":
        start.setDate(start.getDate() - ((start.getDay() + 6) % 7));
        end.setTime(+start);
        end.setDate(end.getDate() + 7);
        break;
      case "day":
        end.setDate(end.getDate() + 1);
        break;
      default:
        start.setDate(1);
        end.setMonth(start.getMonth() + 1, 1);
    }
    return { start, end };
  }
  function renderEvents() {
    const { start, end } = range();
    $("personalRange").textContent =
      `${start.toLocaleDateString()} – ${new Date(+end - 1).toLocaleDateString()} · ${zone}`;
    const target = $("personalEvents");
    target.replaceChildren();
    target.className =
      $("personalView").value === "agenda"
        ? "personal-agenda"
        : "personal-calendar";
    const filtered = events.filter((event) =>
      (event.title + " " + event.description)
        .toLocaleLowerCase()
        .includes($("personalSearch").value.toLocaleLowerCase()),
    );
    const add = (parent, event) => {
      const row = document.createElement("article");
      row.className = "personal-event";
      const heading = document.createElement("strong");
      heading.textContent =
        event.title + (event.completed ? " · complete" : "");
      const detail = document.createElement("p");
      detail.textContent = `${new Date(event.starts_at).toLocaleString()} · ${event.category.toLowerCase()} · ${event.description}`;
      const scope = document.createElement("select");
      scope.setAttribute("aria-label", `Change scope for ${event.title}`);
      for (const [value, label] of [
        ["occurrence", "Only this occurrence"],
        ["series", "Entire series"],
      ]) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = label;
        scope.append(option);
      }
      scope.hidden = !event.series_id;
      row.append(
        heading,
        detail,
        scope,
        button("Edit", () => {
          editing = event;
          $("personalEventHeading").textContent = "Edit event";
          $("personalEventTitle").value = event.title;
          $("personalEventDescription").value = event.description;
          $("personalEventStart").value = dateInput(event.starts_at);
          $("personalEventEnd").value = dateInput(event.ends_at);
          $("personalCategory").value = event.category;
          $("personalRemind").value = event.remind_minutes;
          $("personalRepeat").value = event.recurrence?.frequency || "";
          $("personalRepeatInterval").value = event.recurrence?.interval || 1;
          $("personalRepeatCount").value = event.recurrence?.count || 6;
          $("personalRepeatBound").value = event.recurrence?.until
            ? "until"
            : "count";
          $("personalRepeatUntil").value = event.recurrence?.until
            ? dateInput(event.recurrence.until)
            : "";
          $("personalScopeLabel").hidden = !event.series_id;
          $("personalEditScope").value = scope.value;
          $("personalCancelEdit").hidden = false;
          $("personalEventTitle").focus();
        }),
        button(event.completed ? "Reopen" : "Complete", () =>
          action(async (current) => {
            await request(`/events/${event.event_id}`, {
              method: "PUT",
              body: JSON.stringify({
                ...payload(event),
                completed: !event.completed,
                revision: event.revision,
                scope: "occurrence",
                series_revision: event.series_revision,
              }),
            });
            if (current()) {
              await loadEvents(current);
              status("Event updated.");
            }
          }),
        ),
        button("Delete", () => {
          if (
            confirm(
              `Delete ${scope.value === "series" ? "the entire series containing" : "only this occurrence of"} “${event.title}” and its personal reminders?`,
            )
          )
            void action(async (current) => {
              await request(
                `/events/${event.event_id}?revision=${event.revision}&scope=${scope.value}${event.series_revision ? `&series_revision=${event.series_revision}` : ""}`,
                { method: "DELETE" },
              );
              if (current()) {
                newEvent();
                await loadEvents(current);
                status("Event deleted.");
              }
            });
        }),
      );
      parent.append(row);
    };
    if ($("personalView").value === "agenda")
      for (const event of filtered) add(target, event);
    else
      for (
        let cursor = new Date(start);
        cursor < end;
        cursor.setDate(cursor.getDate() + 1)
      ) {
        const day = document.createElement("section");
        day.className = "personal-day";
        const heading = document.createElement("h5");
        heading.textContent = cursor.toLocaleDateString(undefined, {
          weekday: "short",
          day: "numeric",
          month: "short",
        });
        day.append(heading);
        const next = new Date(cursor);
        next.setDate(next.getDate() + 1);
        for (const event of filtered)
          if (
            new Date(event.starts_at) < next &&
            new Date(event.ends_at) > cursor
          )
            add(day, event);
        target.append(day);
      }
    if (!filtered.length) {
      const empty = document.createElement("p");
      empty.textContent =
        "No events match this period and search. Create an event below.";
      target.prepend(empty);
    }
  }
  const payload = (event) =>
    Object.fromEntries(
      [
        "title",
        "description",
        "starts_at",
        "ends_at",
        "timezone",
        "category",
        "remind_minutes",
        "completed",
        "recurrence",
      ].map((key) => [key, event[key]]),
    );
  async function loadEvents(current, append = false) {
    const { start, end } = range();
    const data = await request(
      `/events?after=${encodeURIComponent(start.toISOString())}&before=${encodeURIComponent(end.toISOString())}&q=${encodeURIComponent($("personalSearch").value)}&limit=200&offset=${append ? events.length : 0}`,
    );
    if (!current()) return;
    events = append ? events.concat(data.items) : data.items;
    $("personalMoreEvents").hidden = events.length >= data.total;
    renderEvents();
  }
  async function loadMessages(current, older = false) {
    const requestedConversation = conversation;
    if (!requestedConversation) {
      $("personalMessages").replaceChildren();
      return;
    }
    const meta = await request(
      `/conversations/${requestedConversation}/messages?limit=1`,
    );
    if (!current()) return;
    messageOffset = older
      ? Math.max(0, messageOffset - 100)
      : Math.max(0, meta.total - 100);
    const data = await request(
      `/conversations/${requestedConversation}/messages?offset=${messageOffset}&limit=100`,
    );
    if (!current()) return;
    $("personalMessages").replaceChildren();
    for (const item of data.items) {
      const row = document.createElement("article");
      row.className = "personal-message";
      const who = document.createElement("strong");
      who.textContent =
        item.speaker === "user" ? "You" : "Rule-based assistant";
      const text = document.createElement("p");
      text.textContent = item.content;
      const time = document.createElement("small");
      time.textContent = new Date(item.created_at).toLocaleString();
      row.append(who, text, time);
      $("personalMessages").append(row);
    }
    $("personalOlderMessages").hidden = messageOffset === 0;
  }
  async function loadConversations(current) {
    const data = await request("/conversations");
    if (!current()) return;
    conversations = data;
    if (!data.some((item) => item.conversation_id === conversation))
      conversation = data[0]?.conversation_id || "";
    $("personalConversations").replaceChildren();
    if (!data.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "Create a conversation to get started";
      $("personalConversations").append(option);
    }
    for (const item of data) {
      const option = document.createElement("option");
      option.value = item.conversation_id;
      option.textContent = item.title;
      $("personalConversations").append(option);
    }
    $("personalConversations").value = conversation;
    await loadMessages(current);
  }
  let noticeOffset = 0;
  async function loadNotices(current, more = false) {
    if (!more) noticeOffset = 0;
    const data = await request(
      `/notifications?offset=${noticeOffset}&limit=50`,
    );
    if (!current()) return;
    if (!more) $("personalNotices").replaceChildren();
    for (const item of data.items) {
      const row = document.createElement("article");
      row.className = "personal-event";
      const label = document.createElement("p");
      label.textContent = `${item.read ? "Read" : "Unread"} · ${item.message} · ${new Date(item.created_at).toLocaleString()}`;
      row.append(
        label,
        button(item.read ? "Mark unread" : "Mark read", () =>
          action(async (valid) => {
            await request(`/notifications/${item.notice_id}`, {
              method: "PUT",
              body: JSON.stringify({ read: !item.read }),
            });
            if (valid()) {
              await loadNotices(valid);
              status("Reminder updated.");
            }
          }),
        ),
        button("Archive", () =>
          action(async (valid) => {
            await request(`/notifications/${item.notice_id}`, {
              method: "PUT",
              body: JSON.stringify({ read: true, archived: true }),
            });
            if (valid()) {
              await loadNotices(valid);
              status("Reminder archived.");
            }
          }),
        ),
      );
      $("personalNotices").append(row);
    }
    noticeOffset += data.items.length;
    $("personalMoreNotices").hidden = noticeOffset >= data.total;
    if (!data.total)
      $("personalNotices").textContent =
        "No personal reminders. Create an event with a reminder to get started.";
    for (const tab of dialog.querySelectorAll(
      "[data-personal-tab=notifications]",
    ))
      tab.textContent = `Notifications (${data.unread})`;
  }
  function tab(name) {
    for (const section of dialog.querySelectorAll("[data-personal-page]"))
      section.hidden = section.dataset.personalPage !== name;
    for (const item of dialog.querySelectorAll("[data-personal-tab]"))
      item.setAttribute(
        "aria-pressed",
        String(item.dataset.personalTab === name),
      );
  }
  for (const item of dialog.querySelectorAll("[data-personal-tab]"))
    item.addEventListener("click", () => {
      tab(item.dataset.personalTab);
      if (item.dataset.personalTab === "notifications")
        void action(async (current) => {
          await loadNotices(current);
          if (current()) status("Reminders refreshed.");
        });
    });
  async function open() {
    document.querySelector("#workspaceDialog")?.close();
    if (!dialog.open) dialog.showModal();
    if (!context().connected) {
      clear();
      return;
    }
    if (context().driving) {
      clear();
      status("Park safely before using the personal workspace.");
      return;
    }
    await action(async (current) => {
      const [summary, prefs, account] = await Promise.all([
        request("/summary"),
        request("/preferences"),
        request("/api/v1/account-access"),
      ]);
      if (!current()) return;
      preferences = prefs;
      $("personalDaily").textContent =
        summary.message +
        (summary.reminder_delivery === "IN_APP_BACKGROUND"
          ? " The server worker generates in-app reminders."
          : " Reminders are generated on refresh; the server worker is not running.");
      $("personalName").value = prefs.display_name;
      $("personalZone").value = prefs.timezone;
      $("personalReminders").checked = prefs.reminders;
      $("personalAccount").replaceChildren();
      for (const text of [account.operator_help, account.driver_help]) {
        const p = document.createElement("p");
        p.textContent = text;
        $("personalAccount").append(p);
      }
      for (const key of ["login", "registration", "recovery", "security"])
        if (account[key + "_url"]) {
          const a = document.createElement("a");
          a.href = account[key + "_url"];
          a.textContent = `Open identity-provider ${key}`;
          a.target = "_blank";
          a.rel = "noopener noreferrer";
          $("personalAccount").append(a);
        }
      if (!account.recovery_url) {
        const p = document.createElement("p");
        p.textContent =
          "Identity-provider recovery link is not configured. No reset email can be sent from this application.";
        $("personalAccount").append(p);
      }
      await loadEvents(current);
      await loadConversations(current);
      await loadNotices(current);
      if (current()) status("Workspace loaded.");
    });
  }
  launcher.addEventListener("click", open);
  $("personalRefresh").addEventListener("click", open);
  $("personalClose").addEventListener("click", () => dialog.close());
  $("personalCancelEdit").addEventListener("click", newEvent);
  $("personalToday").addEventListener("click", () => {
    currentDate = new Date();
    void action(async (current) => {
      await loadEvents(current);
      if (current()) status("Calendar refreshed.");
    });
  });
  for (const [id, direction] of [
    ["personalPrevious", -1],
    ["personalNext", 1],
  ])
    $(id).addEventListener("click", () => {
      const view = $("personalView").value;
      if (view === "month" || view === "agenda")
        currentDate = new Date(
          currentDate.getFullYear(),
          currentDate.getMonth() + direction,
          1,
        );
      else
        currentDate.setDate(
          currentDate.getDate() + direction * (view === "week" ? 7 : 1),
        );
      void action(async (current) => {
        await loadEvents(current);
        if (current()) status("Calendar refreshed.");
      });
    });
  $("personalView").addEventListener(
    "change",
    () =>
      void action(async (current) => {
        await loadEvents(current);
        if (current()) status("Calendar view updated.");
      }),
  );
  $("personalSearch").addEventListener("input", renderEvents);
  const searchEvents = () =>
    void action(async (current) => {
      await loadEvents(current);
      if (current()) status("Event search updated.");
    });
  $("personalSearchApply").addEventListener("click", searchEvents);
  $("personalSearch").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      searchEvents();
    }
  });
  $("personalMoreEvents").addEventListener(
    "click",
    () =>
      void action(async (current) => {
        await loadEvents(current, true);
        if (current()) status("More events loaded.");
      }),
  );
  $("personalEventForm").addEventListener("submit", (event) => {
    event.preventDefault();
    const start = new Date($("personalEventStart").value),
      end = new Date($("personalEventEnd").value);
    if (!Number.isFinite(+start) || !Number.isFinite(+end) || end <= start) {
      status("Choose an end time after the start time.");
      return;
    }
    if (
      $("personalRepeat").value &&
      $("personalRepeatBound").value === "until" &&
      !Number.isFinite(+new Date($("personalRepeatUntil").value))
    ) {
      status(
        "Choose the local date and time through which the series repeats.",
      );
      return;
    }
    const body = {
      title: $("personalEventTitle").value,
      description: $("personalEventDescription").value,
      starts_at: start.toISOString(),
      ends_at: end.toISOString(),
      timezone: zone,
      category: $("personalCategory").value,
      remind_minutes: Number($("personalRemind").value),
      completed: editing?.completed || false,
      recurrence: $("personalRepeat").value
        ? {
            frequency: $("personalRepeat").value,
            interval: Number($("personalRepeatInterval").value),
            ...($("personalRepeatBound").value === "until"
              ? {
                  until: new Date($("personalRepeatUntil").value).toISOString(),
                }
              : { count: Number($("personalRepeatCount").value) }),
          }
        : null,
    };
    if (!editing) {
      const fingerprint = JSON.stringify(body);
      if (!pendingEvent || pendingEvent.fingerprint !== fingerprint)
        pendingEvent = { fingerprint, request_id: crypto.randomUUID() };
      body.request_id = pendingEvent.request_id;
    }
    void action(async (current) => {
      await request(editing ? `/events/${editing.event_id}` : "/events", {
        method: editing ? "PUT" : "POST",
        body: JSON.stringify(
          editing
            ? {
                ...body,
                revision: editing.revision,
                scope: $("personalEditScope").value,
                series_revision: editing.series_revision,
              }
            : body,
        ),
      });
      if (current()) {
        newEvent();
        await loadEvents(current);
        status("Event saved.");
      }
    });
  });
  $("personalNewChat").addEventListener(
    "click",
    () =>
      void action(async (current) => {
        const result = await request("/conversations", {
          method: "POST",
          body: JSON.stringify({
            title: `Daily review ${new Date().toLocaleDateString()}`,
          }),
        });
        if (current()) {
          conversation = result.conversation_id;
          pendingMessage = null;
          await loadConversations(current);
          status("Conversation created.");
        }
      }),
  );
  $("personalConversations").addEventListener("change", () => {
    conversation = $("personalConversations").value;
    pendingMessage = null;
    void action(async (current) => {
      await loadMessages(current);
      if (current()) status("Conversation loaded.");
    });
  });
  $("personalChatForm").addEventListener("submit", (event) => {
    event.preventDefault();
    if (!conversation) {
      status("Create a conversation before sending a message.");
      return;
    }
    const message = $("personalQuestion").value.trim();
    if (!message) return;
    if (
      !pendingMessage ||
      pendingMessage.message !== message ||
      pendingMessage.conversation !== conversation
    )
      pendingMessage = {
        message,
        request_id: crypto.randomUUID(),
        conversation,
      };
    void action(async (current) => {
      const body = pendingMessage;
      await request(`/conversations/${conversation}/messages`, {
        method: "POST",
        body: JSON.stringify({
          message: body.message,
          request_id: body.request_id,
        }),
      });
      if (current()) {
        pendingMessage = null;
        $("personalQuestion").value = "";
        await loadMessages(current);
        status("Reply saved.");
      }
    });
  });
  $("personalOlderMessages").addEventListener(
    "click",
    () =>
      void action(async (current) => {
        await loadMessages(current, true);
        if (current()) status("Earlier messages loaded.");
      }),
  );
  $("personalDeleteChat").addEventListener("click", () => {
    if (
      conversation &&
      confirm("Delete this conversation and all its messages?")
    )
      void action(async (current) => {
        await request(`/conversations/${conversation}`, { method: "DELETE" });
        if (current()) {
          conversation = "";
          pendingMessage = null;
          await loadConversations(current);
          status("Conversation deleted.");
        }
      });
  });
  $("personalReadAll").addEventListener(
    "click",
    () =>
      void action(async (current) => {
        await request("/notifications/read-all", { method: "POST" });
        if (current()) {
          await loadNotices(current);
          status("All personal reminders marked read.");
        }
      }),
  );
  $("personalMoreNotices").addEventListener(
    "click",
    () =>
      void action(async (current) => {
        await loadNotices(current, true);
        if (current()) status("More reminders loaded.");
      }),
  );
  $("personalSettingsForm").addEventListener("submit", (event) => {
    event.preventDefault();
    void action(async (current) => {
      const data = await request("/preferences", {
        method: "PUT",
        body: JSON.stringify({
          display_name: $("personalName").value,
          timezone: $("personalZone").value,
          reminders: $("personalReminders").checked,
        }),
      });
      if (current()) {
        preferences = data;
        status("Preferences saved.");
      }
    });
  });
  $("personalExport").addEventListener(
    "click",
    () =>
      void action(async (current) => {
        const data = await request("/export");
        if (!current()) return;
        const url = URL.createObjectURL(
          new Blob([JSON.stringify(data, null, 2)], {
            type: "application/json",
          }),
        );
        const a = document.createElement("a");
        a.href = url;
        a.download = "trickee-personal-workspace.json";
        a.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        status("Personal workspace exported.");
      }),
  );
  let lastContext = "";
  function checkIdentity() {
    const value = context(),
      stamp = JSON.stringify([
        value.connected,
        value.generation,
        value.driving,
      ]);
    if (stamp !== lastContext) {
      clear();
      lastContext = stamp;
    }
    launcher.hidden = !!value.driving;
    if (value.driving && dialog.open) dialog.close();
  }
  new MutationObserver(checkIdentity).observe(
    document.querySelector("#connection"),
    { childList: true, subtree: true, attributes: true },
  );
  for (const id of ["connectBtn", "disconnectBtn", "disconnect"])
    document
      .querySelector("#" + id)
      ?.addEventListener("click", clear, { capture: true });
  document
    .querySelector("#connectForm")
    ?.addEventListener("submit", clear, { capture: true });
  checkIdentity();
  newEvent();
})();
