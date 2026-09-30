const $ = (id) => document.getElementById(id);

const state = {
  uris: [],
  drivers: [],
  scanned: [],
  shared: [],
  adding: new Set(), // indexes of scanned printers with an add in flight
  scanning: false,
};

async function api(path, options = {}) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = `Error ${res.status}`;
    try {
      const body = await res.json();
      if (Array.isArray(body.detail)) {
        // FastAPI validation errors come as a list of {loc, msg, …}
        detail = body.detail.map((d) => d.msg || String(d)).join("; ");
      } else if (body.detail) {
        detail = body.detail;
      }
    } catch {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

// --- Printer list ------------------------------------------------------------

// lpstat states (LC_ALL=C): "idle", "printing", "disabled" (stopped queue)
const STATE_LABELS = { idle: "Ready", printing: "Printing", disabled: "Stopped" };

let lastSharedJson = null;
let refreshInFlight = false;

async function refreshPrinters() {
  if (refreshInFlight) return;
  refreshInFlight = true;
  try {
    state.shared = await api("/api/printers");
    $("printers-error").classList.add("hidden");
    const json = JSON.stringify(state.shared);
    if (json !== lastSharedJson) {
      // Only touch the DOM when the data changed: a blind rewrite every 15 s
      // would steal keyboard focus and wipe transient button states.
      lastSharedJson = json;
      renderPrinters();
      renderDetected();
    }
  } catch (err) {
    // Keep the last known list on a transient failure; just show a banner.
    const banner = $("printers-error");
    banner.textContent = `Could not refresh printers: ${err.message}`;
    banner.classList.remove("hidden");
    if (lastSharedJson === null) $("printers-list").innerHTML = "";
  } finally {
    refreshInFlight = false;
  }
}

function renderPrinters() {
  const list = $("printers-list");
  if (state.shared.length === 0) {
    list.innerHTML = `<p class="empty">No shared printers yet.</p>`;
    return;
  }
  list.innerHTML = state.shared.map((p) => {
    const stopped = p.state === "disabled";
    return `
    <div class="card printer">
      <div class="printer-info">
        <strong>${esc(p.name.replaceAll("_", " "))}</strong>
        <span class="model">${esc(p.make_model || "")}</span>
        ${stopped && p.message ? `<span class="stop-reason">${esc(p.message)}</span>` : ""}
      </div>
      <div class="printer-actions">
        <span class="badge ${stopped ? "warn" : "ok"}">
          <span aria-hidden="true">${stopped ? "⚠︎" : "✓"}</span>
          ${STATE_LABELS[p.state] || esc(p.state)} · AirPrint
        </span>
        ${stopped ? `<button data-resume="${esc(p.name)}" class="primary">Resume</button>` : ""}
        ${p.jobs > 0 ? `
        <span class="badge warn">${p.jobs} job${p.jobs > 1 ? "s" : ""} queued</span>
        <button data-clear="${esc(p.name)}">Clear queue</button>` : ""}
        <button data-test="${esc(p.name)}">Test page</button>
        <button data-delete="${esc(p.name)}" class="danger">Delete</button>
      </div>
    </div>`;
  }).join("");
}

// Transient confirmation above the printer list (manual wizard success)
let noticeTimer = null;

function showNotice(message) {
  const el = $("printers-notice");
  el.textContent = message;
  el.classList.remove("hidden");
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => el.classList.add("hidden"), 6000);
}

function showCardError(el, message) {
  const card = el.closest(".card");
  if (!card) {
    const banner = $("printers-error");
    banner.textContent = message;
    banner.classList.remove("hidden");
    return;
  }
  let slot = card.querySelector(".card-error");
  if (!slot) {
    slot = document.createElement("p");
    slot.className = "error card-error";
    card.appendChild(slot);
  }
  slot.textContent = message;
}

function clearCardError(el) {
  el.closest(".card")?.querySelector(".card-error")?.remove();
}

document.addEventListener("click", async (e) => {
  const add = e.target.closest("[data-add]");
  if (add) {
    addDetected(Number(add.dataset.add));
    return;
  }
  const manual = e.target.closest("[data-manual]");
  if (manual) {
    configureManually(Number(manual.dataset.manual));
    return;
  }
  if (e.target.closest("[data-retry-scan]")) {
    scanNetwork();
    return;
  }
  const testBtn = e.target.closest("[data-test]");
  const clearBtn = e.target.closest("[data-clear]");
  const delBtn = e.target.closest("[data-delete]");
  const resumeBtn = e.target.closest("[data-resume]");
  const btn = testBtn || clearBtn || delBtn || resumeBtn;
  if (!btn) return;
  clearCardError(btn);
  try {
    if (resumeBtn) {
      resumeBtn.disabled = true;
      await api(`/api/printers/${encodeURIComponent(resumeBtn.dataset.resume)}/resume`, { method: "POST" });
      await refreshPrinters();
    } else if (testBtn) {
      testBtn.disabled = true;
      await api(`/api/printers/${encodeURIComponent(testBtn.dataset.test)}/test`, { method: "POST" });
      testBtn.textContent = "Sent ✓";
      setTimeout(() => {
        if (!testBtn.isConnected) return;
        testBtn.textContent = "Test page";
        testBtn.disabled = false;
      }, 3000);
    } else if (clearBtn && confirm(`Cancel all pending jobs on "${clearBtn.dataset.clear.replaceAll("_", " ")}"?`)) {
      clearBtn.disabled = true;
      await api(`/api/printers/${encodeURIComponent(clearBtn.dataset.clear)}/jobs`, { method: "DELETE" });
      await refreshPrinters();
    } else if (delBtn && confirm(`Delete "${delBtn.dataset.delete.replaceAll("_", " ")}"?`)) {
      delBtn.disabled = true;
      await api(`/api/printers/${encodeURIComponent(delBtn.dataset.delete)}`, { method: "DELETE" });
      await refreshPrinters();
    }
  } catch (err) {
    showCardError(btn, err.message);
    btn.disabled = false;
  }
});

// --- Network scan --------------------------------------------------------------

$("rescan-btn").addEventListener("click", scanNetwork);

// A rescan replaces state.scanned and the whole section: never run one while
// an add is in flight, it would wipe the progress card and shift indexes.
function syncRescanButton() {
  $("rescan-btn").disabled = state.scanning || state.adding.size > 0;
}

async function scanNetwork() {
  if (state.scanning || state.adding.size > 0) return;
  state.scanning = true;
  syncRescanButton();
  $("detected-list").innerHTML = `
    <p class="scanning" role="status"><span class="spinner" aria-hidden="true"></span> Scanning your network for printers… (up to a minute)</p>`;
  try {
    state.scanned = await api("/api/scan");
    state.scanning = false;
    renderDetected();
  } catch (err) {
    $("detected-list").innerHTML = `
      <p class="error">${esc(err.message)}</p>
      <button data-retry-scan>Retry scan</button>`;
  } finally {
    state.scanning = false;
    syncRescanButton();
  }
}

function hostOf(uri) {
  try { return new URL(uri).hostname || null; } catch { return null; }
}

function renderDetected() {
  if (state.scanning) return; // the scan owns this section right now
  if (state.adding.size > 0) return; // don't clobber in-progress add cards
  const sharedHosts = new Set(state.shared.map((p) => hostOf(p.uri)).filter(Boolean));
  const detected = state.scanned
    .map((p, i) => ({ p, i }))
    .filter(({ p }) =>
      !(p.ip && sharedHosts.has(p.ip)) &&
      !p.uris.some((u) => sharedHosts.has(hostOf(u))));
  if (detected.length === 0) {
    $("detected-list").innerHTML = `
      <p class="empty">No new printer detected on the network. Use “Add manually” if yours is missing.</p>`;
    return;
  }
  $("detected-list").innerHTML = detected.map(({ p, i }) => `
    <div class="card printer" data-card="${i}">
      <div class="printer-info">
        <strong>${esc(p.make_model)}</strong>
        <span class="model">${esc(p.ip || "")}</span>
      </div>
      <div class="printer-actions">
        <button class="primary" data-add="${i}">Add</button>
      </div>
    </div>`).join("");
}

// --- One-click add -------------------------------------------------------------

const ADD_STEPS = [
  "Finding the best driver",
  "Installing driver and configuring the print queue",
  "Publishing over AirPrint",
];

// CUPS registers the queue with Avahi asynchronously: poll the real DNS-SD
// announcement for a while before giving up.
const PUBLISH_ATTEMPTS = 10;
const PUBLISH_INTERVAL_MS = 2000;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitForAirPrint(queue) {
  for (let attempt = 1; attempt <= PUBLISH_ATTEMPTS; attempt++) {
    const { advertised } = await api(`/api/printers/${encodeURIComponent(queue)}/airprint`);
    if (advertised) return true;
    if (attempt < PUBLISH_ATTEMPTS) await sleep(PUBLISH_INTERVAL_MS);
  }
  return false;
}

async function addDetected(index) {
  if (state.adding.has(index)) return; // ignore double-clicks
  const printer = state.scanned[index];
  const card = document.querySelector(`[data-card="${index}"]`);
  if (!printer || !card) return;
  state.adding.add(index);
  syncRescanButton();
  let created = false;
  try {
    renderAddProgress(card, printer, 0);
    if (!printer.make_model && !printer.device_id) {
      renderAddError(card, index,
        "This printer did not report a model — configure it manually with a driver search or a PPD file.");
      return;
    }
    const params = new URLSearchParams();
    if (printer.make_model) params.set("q", printer.make_model);
    if (printer.device_id) params.set("device_id", printer.device_id);
    const drivers = await api(`/api/drivers?${params}`);
    if (drivers.length === 0) {
      renderAddError(card, index,
        "No bundled driver matches this printer — configure it manually with a driver search or a PPD file.");
      return;
    }
    renderAddProgress(card, printer, 1);
    const { queue } = await api("/api/printers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: printer.make_model, uri: printer.uris[0], ppd: drivers[0].ppd }),
    });
    created = true;
    renderAddProgress(card, printer, 2);
    refreshPrinters();
    const advertised = await waitForAirPrint(queue);
    renderAddProgress(card, printer, ADD_STEPS.length, advertised);
  } catch (err) {
    if (created) {
      // The queue exists; a failed check must not offer a Retry that would
      // create a duplicate queue.
      renderAddProgress(card, printer, ADD_STEPS.length, false);
    } else {
      renderAddError(card, index, err.message);
    }
  } finally {
    state.adding.delete(index);
    syncRescanButton();
  }
  if (created) setTimeout(renderDetected, 2500);
}

// `advertised` only matters once every step ran: false marks the last step
// (the AirPrint announcement) as unconfirmed instead of done.
function renderAddProgress(card, printer, current, advertised = true) {
  const done = current >= ADD_STEPS.length;
  const last = ADD_STEPS.length - 1;
  card.innerHTML = `
    <div class="printer-info" role="status">
      <strong>${esc(printer.make_model)}</strong>
      <ul class="steps">
        ${ADD_STEPS.map((label, i) => {
          if (done && !advertised && i === last) return `<li class="unconfirmed"><span aria-hidden="true">⚠︎</span> ${label}</li>`;
          if (i < current) return `<li class="done"><span aria-hidden="true">✓</span> ${label}</li>`;
          if (i === current) return `<li class="active"><span class="spinner" aria-hidden="true"></span> ${label}…</li>`;
          return `<li><span aria-hidden="true">○</span> ${label}</li>`;
        }).join("")}
      </ul>
      ${done && advertised ? `<span class="success">✓ Now available on your Apple devices.</span>` : ""}
      ${done && !advertised ? `<span class="warn-text">The queue was created but is not announced over AirPrint yet. It may appear in a moment; if not, restart the container.</span>` : ""}
    </div>`;
}

function renderAddError(card, index, message) {
  const printer = state.scanned[index];
  card.innerHTML = `
    <div class="printer-info">
      <strong>${esc(printer.make_model)}</strong>
      <span class="error">${esc(message)}</span>
    </div>
    <div class="printer-actions">
      <button data-add="${index}">Retry</button>
      <button data-manual="${index}">Configure manually</button>
    </div>`;
}

function configureManually(index) {
  const printer = state.scanned[index];
  resetWizard();
  toggleWizard(true);
  $("printer-name").value = printer.make_model || "";
  selectScanned(printer);
  $("wizard").scrollIntoView({ behavior: "smooth" });
  $("printer-name").focus({ preventScroll: true });
}

// --- Wizard ------------------------------------------------------------------

function toggleWizard(show) {
  $("wizard").classList.toggle("hidden", !show);
  $("show-wizard").setAttribute("aria-expanded", String(show));
}

function showError(message) {
  const el = $("wizard-error");
  el.textContent = message;
  el.classList.toggle("hidden", !message);
}

function resetWizard() {
  $("step-2").classList.add("hidden");
  $("step-3").classList.add("hidden");
  $("ip").value = "";
  $("printer-name").value = "";
  $("ppd-file").value = "";
  $("detect-result").innerHTML = "";
  showError("");
}

async function selectScanned(printer) {
  showError("");
  $("detect-result").innerHTML =
    `<p class="success">✓ Selected printer: <strong>${esc(printer.make_model)}</strong></p>`;
  fillSelect($("uri-select"), printer.uris.map((u) => ({ value: u, label: u })));
  state.uris = printer.uris;

  const params = new URLSearchParams();
  if (printer.make_model) params.set("q", printer.make_model);
  if (printer.device_id) params.set("device_id", printer.device_id);
  const hasCriteria = Boolean(printer.make_model || printer.device_id);
  try {
    const drivers = hasCriteria ? await api(`/api/drivers?${params}`) : [];
    setDrivers(drivers);
    if (drivers.length === 0) {
      $("manual-search").open = true;
      showError("No bundled driver matches — try the manual search or a PPD file.");
    }
  } catch (err) {
    setDrivers([]);
    showError(err.message);
  }
  $("step-2").classList.remove("hidden");
  $("step-3").classList.remove("hidden");
}

$("show-wizard").addEventListener("click", () => {
  resetWizard();
  toggleWizard(true);
  $("ip").focus();
});

$("cancel-wizard").addEventListener("click", () => toggleWizard(false));

$("detect-btn").addEventListener("click", detectPrinter);
$("ip").addEventListener("keydown", (e) => { if (e.key === "Enter") detectPrinter(); });

async function detectPrinter() {
  const ip = $("ip").value.trim();
  if (!ip) return showError("Enter the printer's IP address.");
  showError("");
  const btn = $("detect-btn");
  btn.disabled = true;
  btn.textContent = "Detecting…";
  try {
    const result = await api("/api/detect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ip }),
    });
    state.uris = result.uris;
    fillSelect($("uri-select"), result.uris.map((u) => ({ value: u, label: u })));

    if (result.found) {
      $("detect-result").innerHTML =
        `<p class="success">✓ Printer detected: <strong>${esc(result.make_model)}</strong></p>`;
      setDrivers(result.drivers);
      if (result.drivers.length === 0) {
        $("manual-search").open = true;
        showError("No bundled driver matches — try the manual search or a PPD file.");
      }
    } else {
      $("detect-result").innerHTML =
        `<p class="warn-text">Could not identify the model automatically. Search for the driver manually below.</p>`;
      setDrivers([]);
      $("manual-search").open = true;
    }
    $("step-2").classList.remove("hidden");
    $("step-3").classList.remove("hidden");
  } catch (err) {
    showError(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Detect";
  }
}

function setDrivers(drivers) {
  state.drivers = drivers;
  fillSelect(
    $("driver-select"),
    drivers.map((d) => ({ value: d.ppd, label: d.name })),
    "— pick a driver —",
  );
}

function fillSelect(select, options, placeholder) {
  select.innerHTML = "";
  if (placeholder && options.length === 0) {
    select.append(new Option(placeholder, ""));
  }
  options.forEach((o, i) => select.append(new Option(o.label, o.value, i === 0, i === 0)));
}

$("search-btn").addEventListener("click", searchDrivers);
$("driver-query").addEventListener("keydown", (e) => { if (e.key === "Enter") searchDrivers(); });

async function searchDrivers() {
  const q = $("driver-query").value.trim();
  if (!q) return;
  showError("");
  try {
    const drivers = await api(`/api/drivers?q=${encodeURIComponent(q)}`);
    if (drivers.length === 0) {
      showError("No driver found for this search — try a PPD file.");
      return;
    }
    setDrivers(drivers);
  } catch (err) {
    showError(err.message);
  }
}

$("create-btn").addEventListener("click", async () => {
  const name = $("printer-name").value.trim();
  const uri = $("uri-select").value;
  const ppd = $("driver-select").value;
  const ppdFile = $("ppd-file").files[0];

  if (!name) return showError("Give the printer a name.");
  if (!uri) return showError("No connection URI — detect the printer first.");
  if (!ppd && !ppdFile) return showError("Pick a driver or provide a PPD file.");
  showError("");

  const btn = $("create-btn");
  btn.disabled = true;
  btn.textContent = "Configuring…";
  try {
    let result;
    if (ppdFile) {
      const form = new FormData();
      form.append("name", name);
      form.append("uri", uri);
      form.append("ppd_file", ppdFile);
      result = await api("/api/printers/upload", { method: "POST", body: form });
    } else {
      result = await api("/api/printers", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, uri, ppd }),
      });
    }
    toggleWizard(false);
    showNotice(`✓ “${result.queue.replaceAll("_", " ")}” is now shared over AirPrint.`);
    await refreshPrinters();
  } catch (err) {
    showError(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Make available over AirPrint";
  }
});

// --- Misc --------------------------------------------------------------------

const ESC_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ESC_MAP[c]);
}

$("cups-link").href = `http://${location.hostname}:631`;

$("printers-list").innerHTML = `
  <p class="scanning" role="status"><span class="spinner" aria-hidden="true"></span> Loading printers…</p>`;
refreshPrinters();
scanNetwork();
setInterval(() => { if (!document.hidden) refreshPrinters(); }, 15000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshPrinters();
});
