/* Private file library. Credentials are supplied only by the shared API helper. */
(() => {
  "use strict";
  const context = () =>
    window.trickeeWorkspaceContext?.() || { connected: false };
  const base = `/api/v1/${location.pathname.startsWith("/driver") ? "driver" : "operator"}/workspace/files`;
  const launcher = document.createElement("button");
  launcher.id = "filesLauncher";
  launcher.type = "button";
  launcher.textContent = "Private files";
  launcher.setAttribute("aria-haspopup", "dialog");
  launcher.setAttribute("aria-controls", "filesDialog");
  const host = document.querySelector(".topbar");
  if (!host || typeof HTMLDialogElement === "undefined") return;
  host.append(launcher);
  const dialog = document.createElement("dialog");
  dialog.id = "filesDialog";
  dialog.className = "workspace-dialog files-dialog";
  dialog.setAttribute("aria-labelledby", "filesTitle");
  dialog.innerHTML = `<div class="workspace-heading"><h2 id="filesTitle">Private files</h2><button type="button" id="filesClose">Close</button></div><p>PNG, JPEG, PDF or plain UTF-8 TXT. Up to 2 MiB each; 20 MiB and 40 files per account. PDFs download only. No external malware scanning is provided.</p><p id="filesStatus" role="status" aria-live="polite"></p><form id="filesUploadForm"><label>Select a file<input id="filesUpload" type="file" accept=".png,.jpg,.jpeg,.pdf,.txt" required></label><button type="submit">Upload file</button></form><form id="filesSearchForm"><label>Find files<input id="filesSearch" type="search" maxlength="120"></label><button type="submit">Search</button></form><ul id="filesList"></ul><div class="files-paging"><button id="filesPrevious" type="button">Previous</button><span id="filesPage"></span><button id="filesNext" type="button">Next</button></div><section id="filesPreview" aria-label="File preview"></section>`;
  document.body.append(dialog);
  const el = (id) => dialog.querySelector("#" + id);
  let epoch = 0,
    busy = false,
    offset = 0,
    total = 0,
    identity = "",
    url = null,
    controller = null,
    pending = null;
  const stamp = () =>
    JSON.stringify([
      context().connected,
      context().generation,
      context().driving,
    ]);
  function revoke() {
    if (url) URL.revokeObjectURL(url);
    url = null;
    el("filesPreview").replaceChildren();
  }
  function clear() {
    epoch++;
    controller?.abort();
    controller = null;
    busy = false;
    pending = null;
    offset = 0;
    total = 0;
    revoke();
    el("filesList").replaceChildren();
    el("filesUpload").value = "";
    el("filesSearch").value = "";
    el("filesStatus").textContent = "";
    el("filesPage").textContent = "";
    if (dialog.open) dialog.close();
    controls();
  }
  function controls() {
    dialog.querySelectorAll("input,button").forEach((node) => {
      node.disabled = busy && node.id !== "filesClose";
    });
    el("filesPrevious").disabled = busy || offset === 0;
    el("filesNext").disabled = busy || offset + 20 >= total;
  }
  async function request(path, options = {}) {
    controller = new AbortController();
    const active = controller;
    const timeout = setTimeout(() => active.abort(), 15000);
    try {
      return await window.api(base + path, {
        ...options,
        cache: "no-store",
        signal: active.signal,
      });
    } finally {
      clearTimeout(timeout);
      if (controller === active) controller = null;
    }
  }
  async function action(work) {
    if (busy) return;
    if (!context().connected || context().driving) {
      el("filesStatus").textContent =
        "Connect while parked to use private files.";
      return;
    }
    busy = true;
    controls();
    const current = epoch;
    const owner = stamp();
    const valid = () =>
      current === epoch &&
      owner === stamp() &&
      context().connected &&
      !context().driving;
    try {
      await work(valid);
    } catch (error) {
      if (error.status === 401 || error.status === 403) {
        if (valid()) clear();
        return;
      }
      if (valid())
        el("filesStatus").textContent =
          error.name === "AbortError"
            ? "Request timed out. Retry when connected."
            : error.message;
    } finally {
      if (current === epoch) {
        busy = false;
        controls();
      }
    }
  }
  async function listing(valid) {
    const data = await request(
      `?q=${encodeURIComponent(el("filesSearch").value.trim())}&offset=${offset}&limit=20`,
    );
    if (!valid()) return;
    total = data.total;
    revoke();
    el("filesList").replaceChildren();
    for (const item of data.items) {
      const li = document.createElement("li"),
        label = document.createElement("span");
      label.textContent = `${item.filename} (${Math.ceil(item.size / 1024)} KiB)`;
      li.append(label);
      for (const type of ["Preview", "Download", "Delete"]) {
        if (type === "Preview" && item.media_type === "application/pdf")
          continue;
        const button = document.createElement("button");
        button.type = "button";
        button.disabled = busy;
        button.textContent = type;
        button.setAttribute("aria-label", `${type} ${item.filename}`);
        button.addEventListener("click", () =>
          action(async (ok) => {
            if (type === "Delete") {
              await request("/" + item.file_id, {
                method: "DELETE",
                responseType: "response",
              });
              if (ok()) await listing(ok);
              return;
            }
            const blob = await request(
              "/" +
                item.file_id +
                (type === "Preview" ? "/preview" : "/download"),
              { responseType: "blob" },
            );
            if (!ok()) return;
            revoke();
            url = URL.createObjectURL(blob);
            if (type === "Download") {
              const a = document.createElement("a");
              a.href = url;
              a.download = item.filename;
              a.click();
            } else if (item.media_type.startsWith("image/")) {
              const image = document.createElement("img");
              image.alt = item.filename;
              image.src = url;
              el("filesPreview").append(image);
            } else {
              const text = document.createElement("pre");
              text.textContent = await blob.text();
              if (ok()) el("filesPreview").append(text);
            }
          }),
        );
        li.append(button);
      }
      el("filesList").append(li);
    }
    el("filesStatus").textContent = total ? "Files loaded." : "No files found.";
    el("filesPage").textContent = total
      ? `${offset + 1}–${Math.min(offset + 20, total)} of ${total}`
      : "0 files";
  }
  launcher.addEventListener("click", () => {
    dialog.showModal();
    el("filesSearch").focus();
    void action(listing);
  });
  el("filesClose").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => {
    epoch++;
    controller?.abort();
    busy = false;
    revoke();
    el("filesList").replaceChildren();
    el("filesUpload").value = "";
    pending = null;
    launcher.focus();
  });
  el("filesSearchForm").addEventListener("submit", (event) => {
    event.preventDefault();
    offset = 0;
    void action(listing);
  });
  el("filesUpload").addEventListener("change", () => {
    pending = null;
  });
  el("filesUploadForm").addEventListener("submit", (event) => {
    event.preventDefault();
    void action(async (valid) => {
      const file = el("filesUpload").files[0];
      if (!file) return;
      if (file.size > 2097152) {
        throw new Error("File exceeds 2 MiB.");
      }
      pending ||= crypto.randomUUID();
      await request(
        `?filename=${encodeURIComponent(file.name)}&request_id=${pending}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/octet-stream" },
          body: file,
        },
      );
      if (!valid()) return;
      pending = null;
      el("filesUpload").value = "";
      offset = 0;
      await listing(valid);
    });
  });
  el("filesPrevious").addEventListener("click", () => {
    offset = Math.max(0, offset - 20);
    void action(listing);
  });
  el("filesNext").addEventListener("click", () => {
    offset += 20;
    void action(listing);
  });
  function check() {
    const next = stamp();
    if (next !== identity) {
      clear();
      identity = next;
    }
    launcher.hidden = !!context().driving;
    launcher.disabled = !context().connected;
  }
  setInterval(check, 250);
  for (const id of ["connectBtn", "disconnectBtn", "disconnect"])
    document
      .getElementById(id)
      ?.addEventListener("click", clear, { capture: true });
  document
    .getElementById("connectForm")
    ?.addEventListener("submit", clear, { capture: true });
  check();
  controls();
})();
