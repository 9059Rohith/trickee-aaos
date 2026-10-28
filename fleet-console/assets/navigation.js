/* Server-owned navigation, saved calendar views and onboarding. No private local storage. */
(() => {
  "use strict";
  const dialog = document.getElementById("workspaceDialog");
  if (!dialog) return;
  const audience = location.pathname.startsWith("/driver")
    ? "driver"
    : "operator";
  const endpoint = `/api/v1/${audience}/workspace/navigation`;
  const context = () =>
    window.trickeeWorkspaceContext?.() || { connected: false };
  const panel = document.createElement("section");
  panel.className = "navigation-personal";
  panel.innerHTML = `<h3>Make it yours</h3><p id="navigationStatus" role="status" aria-live="polite">Connect to save your workspace.</p>
    <div id="navigationPrivate" hidden><h4>Favorite sections</h4><div id="navigationFavorites"></div>
    <label>Section<select id="navigationSection"></select></label><button id="navigationAdd" type="button">Add favorite</button>
    <h4>Saved calendar views</h4><div id="navigationViews"></div><form id="navigationViewForm"><label>View name<input id="navigationViewName" required maxlength="80"></label>
    <label>Calendar layout<select id="navigationViewLayout"><option value="agenda">Agenda</option><option value="month">Month</option><option value="week">Week</option><option value="day">Day</option></select></label>
    <label>Event search<input id="navigationViewQuery" maxlength="120"></label><button type="submit">Save calendar view</button></form>
    <h4>Getting started</h4><p id="navigationOnboarding" aria-live="polite"></p><div class="navigation-actions"><button id="navigationNext" type="button">Next step</button><button id="navigationRestart" type="button">Restart guide</button><button id="navigationExport" type="button">Export saved workspace</button></div></div>`;
  dialog.append(panel);
  const $ = (id) => panel.querySelector("#" + id);
  let state = null,
    stamp = "",
    sequence = 0,
    busy = false;
  const controllers = new Set();
  const status = (text) => {
    $("navigationStatus").textContent = text;
  };
  const identity = () => {
    const c = context();
    return JSON.stringify([c.connected, c.generation, c.driving]);
  };
  function clear() {
    sequence++;
    for (const controller of controllers) controller.abort();
    state = null;
    $("navigationPrivate").hidden = true;
    for (const id of [
      "navigationFavorites",
      "navigationViews",
      "navigationSection",
      "navigationOnboarding",
    ])
      $(id).replaceChildren();
    $("navigationViewForm").reset();
    status(
      context().driving
        ? "Park safely before changing your workspace."
        : "Connect to save your workspace.",
    );
  }
  function checkIdentity() {
    const next = identity();
    if (stamp !== next) {
      stamp = next;
      clear();
    }
  }
  async function request(options = {}) {
    const controller = new AbortController();
    controllers.add(controller);
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      return await window.api(endpoint, {
        ...options,
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timeout);
      controllers.delete(controller);
    }
  }
  async function action(work) {
    checkIdentity();
    if (busy || !context().connected || context().driving) return;
    const owner = stamp,
      run = ++sequence;
    const current = () => identity() === owner && sequence === run;
    busy = true;
    panel.setAttribute("aria-busy", "true");
    panel.querySelectorAll("button,input,select").forEach((el) => {
      el.disabled = true;
    });
    try {
      await work(current);
    } catch (error) {
      if (current()) {
        if ([401, 403].includes(error.status)) clear();
        if (error.status === 409) {
          state = null;
          $("navigationPrivate").hidden = true;
        }
        status(
          error.status === 409
            ? "Saved workspace changed elsewhere. Close and reopen this dialog before retrying."
            : `Workspace could not be saved: ${error.message || "Request failed"}`,
        );
      }
    } finally {
      busy = false;
      panel.removeAttribute("aria-busy");
      panel.querySelectorAll("button,input,select").forEach((el) => {
        el.disabled = false;
      });
      if (state) $("navigationNext").disabled = state.onboarding_completed;
    }
  }
  const guide = [
    "1 of 4 · Access: use your own scoped credential. Account access explains real recovery; never paste secrets into messages.",
    "2 of 4 · Trust the source: check signal freshness and provider provenance. Missing vehicle data must not be treated as a real zero.",
    audience === "driver"
      ? "3 of 4 · Trips: review confirmed plans and proposals while parked. Phone GPS requires explicit consent; use service cases for fleet support."
      : "3 of 4 · Fleet work: review alerts, confirm daily plans and manage proposals through the authorized controls. Check reconciliation evidence before closing assignments.",
    "4 of 4 · Personal work: Planner & assistant holds private events, reminders and conversations. Use saved views and favorites to return quickly. Files stay scoped to your identity.",
    "Guide complete. You can restart this guide at any time. Your server-saved choices follow this authenticated workspace.",
  ];
  function button(label, handler) {
    const el = document.createElement("button");
    el.type = "button";
    el.textContent = label;
    el.disabled = busy;
    el.addEventListener("click", handler);
    return el;
  }
  function available() {
    return [...document.querySelectorAll("#workspaceLinks a")].filter(
      (link) =>
        document.getElementById(link.hash.slice(1))?.getClientRects().length,
    );
  }
  function render() {
    if (!state) return;
    $("navigationPrivate").hidden = false;
    const choices = available();
    $("navigationSection").replaceChildren(
      ...choices.map((link) => {
        const option = document.createElement("option");
        option.value = link.hash.slice(1);
        option.textContent = link.textContent;
        return option;
      }),
    );
    $("navigationFavorites").replaceChildren();
    for (const favorite of state.favorites) {
      const live = choices.find(
        (link) => link.hash.slice(1) === favorite.section_id,
      );
      const row = document.createElement("div");
      row.className = "navigation-row";
      if (live) {
        const link = document.createElement("a");
        link.href = "#" + favorite.section_id;
        link.textContent = live.textContent;
        link.addEventListener("click", () => {
          dialog.close();
          const target = document.getElementById(favorite.section_id);
          target?.setAttribute("tabindex", "-1");
          target?.focus({ preventScroll: true });
        });
        row.append(link);
      } else {
        const text = document.createElement("span");
        text.textContent = favorite.label + " (currently unavailable)";
        row.append(text);
      }
      row.append(
        button(`Remove ${favorite.label}`, () =>
          save({
            ...state,
            favorites: state.favorites.filter(
              (item) => item.section_id !== favorite.section_id,
            ),
          }),
        ),
      );
      $("navigationFavorites").append(row);
    }
    if (!state.favorites.length)
      $("navigationFavorites").textContent = "No favorite sections yet.";
    $("navigationViews").replaceChildren();
    for (const view of state.saved_views) {
      const row = document.createElement("div");
      row.className = "navigation-row";
      row.append(
        button(`Open ${view.name}`, () => {
          if (context().driving) return;
          const layout = document.getElementById("personalView"),
            search = document.getElementById("personalSearch");
          if (!layout || !search) return;
          layout.value = view.view;
          search.value = view.query;
          document.querySelector("[data-personal-tab=calendar]")?.click();
          dialog.close();
          document.getElementById("personalLauncher")?.click();
        }),
        button(`Delete ${view.name}`, () =>
          save({
            ...state,
            saved_views: state.saved_views.filter(
              (item) => item.view_id !== view.view_id,
            ),
          }),
        ),
      );
      $("navigationViews").append(row);
    }
    if (!state.saved_views.length)
      $("navigationViews").textContent = "No saved calendar views yet.";
    $("navigationOnboarding").textContent = guide[state.onboarding_step];
    $("navigationNext").textContent =
      state.onboarding_step === 3 ? "Finish guide" : "Next step";
    $("navigationNext").disabled = busy || state.onboarding_completed;
  }
  function save(value) {
    void action(async (current) => {
      const saved = await request({
        method: "PUT",
        body: JSON.stringify(value),
      });
      if (current()) {
        state = saved;
        render();
        status("Workspace saved.");
      }
    });
  }
  $("navigationAdd").addEventListener("click", () => {
    if (!state) return;
    const selected = $("navigationSection").selectedOptions[0];
    if (!selected) return;
    if (state.favorites.some((item) => item.section_id === selected.value)) {
      status("This section is already a favorite.");
      return;
    }
    if (state.favorites.length >= 20) {
      status("You can save up to 20 favorite sections.");
      return;
    }
    save({
      ...state,
      favorites: [
        ...state.favorites,
        { section_id: selected.value, label: selected.textContent },
      ],
    });
  });
  $("navigationViewForm").addEventListener("submit", (event) => {
    event.preventDefault();
    if (!state) return;
    if (state.saved_views.length >= 10) {
      status("You can save up to 10 calendar views.");
      return;
    }
    const name = $("navigationViewName").value.trim();
    if (!name) {
      status("Enter a view name.");
      return;
    }
    save({
      ...state,
      saved_views: [
        ...state.saved_views,
        {
          view_id: crypto.randomUUID(),
          name,
          view: $("navigationViewLayout").value,
          query: $("navigationViewQuery").value.trim(),
        },
      ],
    });
  });
  $("navigationNext").addEventListener("click", () => {
    if (!state || state.onboarding_completed) return;
    const step = Math.min(4, state.onboarding_step + 1);
    save({ ...state, onboarding_step: step, onboarding_completed: step === 4 });
  });
  $("navigationRestart").addEventListener("click", () => {
    if (state)
      save({ ...state, onboarding_step: 0, onboarding_completed: false });
  });
  $("navigationExport").addEventListener("click", () => {
    if (!state || context().driving) return;
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(state, null, 2)], { type: "application/json" }),
    );
    const link = document.createElement("a");
    link.href = url;
    link.download = "trickee-saved-workspace.json";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  new MutationObserver(() => {
    if (dialog.open)
      void action(async (current) => {
        const value = await request();
        if (current()) {
          state = value;
          render();
          status("Your saved workspace is loaded.");
        }
      });
  }).observe(dialog, { attributes: true, attributeFilter: ["open"] });
  new MutationObserver(checkIdentity).observe(
    document.getElementById("connection"),
    { attributes: true, childList: true, subtree: true },
  );
  for (const id of ["connectBtn", "disconnectBtn", "disconnect"])
    document
      .getElementById(id)
      ?.addEventListener("click", clear, { capture: true });
  document
    .getElementById("connectForm")
    ?.addEventListener("submit", clear, { capture: true });
  setInterval(checkIdentity, 1000);
  checkIdentity();
})();
