const $ = (id) => document.getElementById(id);

const state = {
  uris: [],
  drivers: [],
  scanned: [],
  shared: [],
  // One-click add state per detected printer, keyed by index in
  // state.scanned (stable: rescans are blocked while an add is in flight,
  // and a rescan clears every entry). No entry means an idle "Add" card.
  //   { status: "progress", step }       — checklist, `step` is current
  //   { status: "error", message }       — Retry / Configure manually
  //   { status: "done", advertised }     — success line or warning
  //   { status: "gone" }                 — hidden (success shown, dismissed)
  cards: new Map(),
  scanning: false,
};

const addInFlight = () =>
  [...state.cards.values()].some((card) => card.status === "progress");

async function api(path, options = {}) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = `Error ${res.status}`;
    try {
      const body = await res.json();
      if (Array.isArray(body.detail)) {
        // FastAPI validation errors come as a list of {loc, msg, …}
        // pydantic prefixes validator messages with "Value error, "
        detail = body.detail
          .map((d) => (d.msg || String(d)).replace(/^Value error, /, ""))
          .join("; ");
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
let refreshPromise = null;
let refreshAgain = false;

// Callers that just changed a queue await this. When a poll is already in
// flight its answer may predate the change: run one more right after it
// instead of returning early with a stale list.
function refreshPrinters() {
  if (refreshPromise) {
    refreshAgain = true;
    return refreshPromise;
  }
  refreshPromise = (async () => {
    do {
      refreshAgain = false;
      await loadPrinters();
    } while (refreshAgain);
  })().finally(() => { refreshPromise = null; });
  return refreshPromise;
}

async function loadPrinters() {
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
    const label = esc(displayName(p.name));
    // With the retry-job policy a printer that is off leaves the queue idle
    // with pending jobs: its reason ("The printer is not responding.") is
    // just as useful as a stopped queue's.
    const showMessage = p.message && (stopped || p.jobs > 0);
    return `
    <div class="card printer">
      <div class="printer-info">
        <strong>${label}</strong>
        <span class="model">${esc(p.make_model || "")}</span>
        ${showMessage ? `<span class="queue-message">${esc(p.message)}</span>` : ""}
        ${p.power_plug ? `<span class="power">Auto power: ${esc(p.power_plug)}</span>` : ""}
        <span class="badges">
          <span class="badge ${stopped ? "warn" : "ok"}">
            <span aria-hidden="true">${stopped ? "⚠︎" : "✓"}</span>
            ${STATE_LABELS[p.state] || esc(p.state)}
          </span>
          ${p.jobs > 0 ? `<span class="badge warn">${p.jobs} job${p.jobs > 1 ? "s" : ""} queued</span>` : ""}
        </span>
      </div>
      <div class="printer-actions">
        ${stopped ? `<button data-resume="${esc(p.name)}" class="primary" aria-label="Resume ${label}">Resume</button>` : ""}
        ${p.jobs > 0 ? `<button data-clear="${esc(p.name)}" aria-label="Clear queue of ${label}">Clear queue</button>` : ""}
        <button data-test="${esc(p.name)}" aria-label="Print a test page on ${label}">Test page</button>
        <button data-delete="${esc(p.name)}" class="danger" aria-label="Delete ${label}">Delete</button>
      </div>
    </div>`;
  }).join("");
}

// Queue names cannot contain spaces; show them with spaces for underscores
const displayName = (queue) => queue.replaceAll("_", " ");

// Notice above the printer list (manual wizard result). A success fades
// after 6 s; a warning stays until dismissed or replaced by the next notice.
let noticeTimer = null;

function showNotice(message, { warn = false } = {}) {
  const el = $("printers-notice");
  el.className = `notice ${warn ? "warn-text" : "success"}`;
  el.innerHTML = `<span>${esc(message)}</span>${warn
    ? `<button class="link" data-dismiss-notice aria-label="Dismiss notice">Dismiss</button>` : ""}`;
  clearTimeout(noticeTimer);
  if (!warn) noticeTimer = setTimeout(() => el.classList.add("hidden"), 6000);
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
  const dismiss = e.target.closest("[data-dismiss]");
  if (dismiss) {
    state.cards.set(Number(dismiss.dataset.dismiss), { status: "gone" });
    renderDetected();
    return;
  }
  if (e.target.closest("[data-dismiss-notice]")) {
    $("printers-notice").classList.add("hidden");
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
      testBtn.textContent = "Sent ✓"; // aria-label keeps the printer name
      setTimeout(() => {
        if (!testBtn.isConnected) return;
        testBtn.textContent = "Test page";
        testBtn.disabled = false;
      }, 3000);
    } else if (clearBtn && confirm(`Cancel all pending jobs on "${displayName(clearBtn.dataset.clear)}"?`)) {
      clearBtn.disabled = true;
      await api(`/api/printers/${encodeURIComponent(clearBtn.dataset.clear)}/jobs`, { method: "DELETE" });
      await refreshPrinters();
    } else if (delBtn && confirm(
      `Delete "${displayName(delBtn.dataset.delete)}"?\n\n` +
      "It will no longer be shared over AirPrint, and its pending jobs will be cancelled.")) {
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
  $("rescan-btn").disabled = state.scanning || addInFlight();
}

async function scanNetwork() {
  if (state.scanning || addInFlight()) return;
  state.scanning = true;
  state.cards.clear();
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

// Renders every detected card from state.scanned + state.cards: safe to call
// at any time (poll, add step, another add finishing), it never loses a
// progress, error or warning card.
function renderDetected() {
  if (state.scanning) return; // the scan owns this section right now
  const sharedHosts = new Set(state.shared.map((p) => hostOf(p.uri)).filter(Boolean));
  const isShared = (p) =>
    (p.ip && sharedHosts.has(p.ip)) || p.uris.some((u) => sharedHosts.has(hostOf(u)));
  const detected = [];
  state.scanned.forEach((p, i) => {
    const card = state.cards.get(i);
    if (card?.status === "gone") {
      // Once the printer list shows the new queue, the shared filter hides
      // it on its own; forget the entry so it reappears if deleted later.
      if (isShared(p)) state.cards.delete(i);
      return;
    }
    // A card with an add under way or a result to show stays visible even
    // though its printer is now shared
    if (card || !isShared(p)) detected.push({ p, i, card });
  });
  const list = $("detected-list");
  if (detected.length === 0) {
    list.innerHTML = `
      <p class="empty">No new printer detected on the network. Use “Add manually” if yours is missing.</p>`;
    return;
  }
  // Re-rendering replaces the buttons: keep keyboard focus where it was
  const focused = list.contains(document.activeElement) ? focusKey(document.activeElement) : null;
  list.innerHTML = detected.map(renderDetectedCard).join("");
  if (focused) list.querySelector(focused)?.focus();
}

function focusKey(el) {
  const [key, value] = Object.entries(el.dataset)[0] || [];
  return key ? `[data-${key}="${CSS.escape(value)}"]` : null;
}

function renderDetectedCard({ p, i, card }) {
  const name = esc(p.make_model);
  if (!card) {
    return `
    <div class="card printer">
      <div class="printer-info">
        <strong>${name}</strong>
        <span class="model">${esc(p.ip || "")}</span>
      </div>
      <div class="printer-actions">
        <button class="primary" data-add="${i}" aria-label="Add ${name}">Add</button>
      </div>
    </div>`;
  }
  if (card.status === "error") {
    return `
    <div class="card printer">
      <div class="printer-info">
        <strong>${name}</strong>
        <span class="error">${esc(card.message)}</span>
      </div>
      <div class="printer-actions">
        <button data-add="${i}" aria-label="Retry adding ${name}">Retry</button>
        <button data-manual="${i}" aria-label="Configure ${name} manually">Configure manually</button>
      </div>
    </div>`;
  }
  const done = card.status === "done";
  const step = done ? ADD_STEPS.length : card.step;
  // `advertised` only matters once every step ran: false marks the last
  // step (the AirPrint announcement) as unconfirmed instead of done.
  const unconfirmed = done && !card.advertised;
  const last = ADD_STEPS.length - 1;
  return `
    <div class="card printer">
      <div class="printer-info">
        <strong>${name}</strong>
        <ul class="steps">
          ${ADD_STEPS.map((label, s) => {
            if (unconfirmed && s === last) return `<li class="unconfirmed"><span aria-hidden="true">⚠︎</span> ${label}</li>`;
            if (s < step) return `<li class="done"><span aria-hidden="true">✓</span> ${label}</li>`;
            if (s === step) return `<li class="active"><span class="spinner" aria-hidden="true"></span> ${label}…</li>`;
            return `<li><span aria-hidden="true">○</span> ${label}</li>`;
          }).join("")}
        </ul>
        ${done && card.advertised ? `<span class="success">✓ Now available on your Apple devices.</span>` : ""}
        ${unconfirmed ? `<span class="warn-text">${UNCONFIRMED_MESSAGE}</span>` : ""}
      </div>
      ${unconfirmed ? `
      <div class="printer-actions">
        <button data-dismiss="${i}" aria-label="Dismiss the warning for ${name}">Dismiss</button>
      </div>` : ""}
    </div>`;
}

// Screen readers: one persistent live region for add progress, since the
// cards themselves are re-created on every render
function announce(message) {
  $("detected-status").textContent = message;
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

const UNCONFIRMED_MESSAGE =
  "The queue was created but is not announced over AirPrint yet. It may appear in a moment; if not, restart the container.";

function setCard(index, card, message) {
  state.cards.set(index, card);
  renderDetected();
  if (message) announce(message);
}

async function addDetected(index) {
  const printer = state.scanned[index];
  if (!printer || state.cards.get(index)?.status === "progress") return; // ignore double-clicks
  const name = printer.make_model;
  setCard(index, { status: "progress", step: 0 }, `Adding ${name}: ${ADD_STEPS[0]}…`);
  syncRescanButton();
  let created = false;
  try {
    const params = new URLSearchParams({ q: printer.make_model });
    if (printer.device_id) params.set("device_id", printer.device_id);
    const drivers = await api(`/api/drivers?${params}`);
    if (drivers.length === 0) {
      failAdd(index, name,
        "No bundled driver matches this printer — configure it manually with a driver search or a PPD file.");
      return;
    }
    setCard(index, { status: "progress", step: 1 }, `Adding ${name}: ${ADD_STEPS[1]}…`);
    const { queue } = await api("/api/printers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: printer.make_model, uri: printer.uris[0], ppd: drivers[0].ppd }),
    });
    created = true;
    setCard(index, { status: "progress", step: 2 }, `Adding ${name}: ${ADD_STEPS[2]}…`);
    refreshPrinters();
    const advertised = await waitForAirPrint(queue);
    finishAdd(index, name, advertised);
  } catch (err) {
    if (created) {
      // The queue exists; a failed check must not offer a Retry that would
      // create a duplicate queue.
      finishAdd(index, name, false);
    } else {
      failAdd(index, name, err.message);
    }
  } finally {
    syncRescanButton();
  }
}

function failAdd(index, name, message) {
  setCard(index, { status: "error", message }, `Could not add ${name}: ${message}`);
}

// Success shows briefly, then the card leaves the list (the printer is now
// shared). A warning stays until dismissed or a rescan.
function finishAdd(index, name, advertised) {
  setCard(index, { status: "done", advertised },
    advertised ? `${name} is now available on your Apple devices.` : `${name}: ${UNCONFIRMED_MESSAGE}`);
  if (!advertised) return;
  const card = state.cards.get(index);
  setTimeout(() => {
    // A rescan in the meantime replaced the entry: leave the new state alone
    if (state.cards.get(index) !== card) return;
    state.cards.set(index, { status: "gone" });
    renderDetected();
  }, 2500);
}

function configureManually(index) {
  const printer = state.scanned[index];
  state.cards.delete(index); // the error card goes back to a plain "Add"
  renderDetected();
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
  $("ppd-hint").classList.add("hidden");
  $("detect-result").innerHTML = "";
  showError("");
}

async function selectScanned(printer) {
  showError("");
  $("detect-result").innerHTML =
    `<p class="success">✓ Selected printer: <strong>${esc(printer.make_model)}</strong></p>`;
  fillSelect($("uri-select"), uriOptions(printer.uris));
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
    fillSelect($("uri-select"), uriOptions(result.uris));

    if (result.found) {
      $("detect-result").innerHTML =
        `<p class="success">✓ Printer detected: <strong>${esc(result.make_model)}</strong></p>`;
      if (!$("printer-name").value.trim()) $("printer-name").value = result.make_model;
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

// Friendly labels for the Connection select; the value stays the raw URI
const SCHEME_LABELS = { ipp: "IPP", ipps: "IPP over TLS", lpd: "LPD", dnssd: "Bonjour" };

function uriLabel(uri) {
  const scheme = uri.split(":", 1)[0].toLowerCase();
  if (scheme === "socket") {
    let port = "";
    try { port = new URL(uri).port; } catch {}
    return `Raw socket, port ${port || "9100"} — ${uri}`;
  }
  return SCHEME_LABELS[scheme] ? `${SCHEME_LABELS[scheme]} — ${uri}` : uri;
}

const uriOptions = (uris) => uris.map((u) => ({ value: u, label: uriLabel(u) }));

// A PPD file silently wins over the selected driver: say so
$("ppd-file").addEventListener("change", () => {
  $("ppd-hint").classList.toggle("hidden", $("ppd-file").files.length === 0);
});

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
  let result;
  try {
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
  } catch (err) {
    showError(err.message);
    resetCreateButton();
    return;
  }
  refreshPrinters();
  btn.textContent = "Publishing over AirPrint…";
  let advertised = false;
  try {
    advertised = await waitForAirPrint(result.queue);
  } catch {
    // The queue exists: a failed check is "unconfirmed", not a failed creation
  }
  const queueName = displayName(result.queue);
  toggleWizard(false);
  resetCreateButton();
  if (advertised) {
    showNotice(`✓ “${queueName}” is now shared over AirPrint.`);
  } else {
    showNotice(`“${queueName}”: ${UNCONFIRMED_MESSAGE}`, { warn: true });
  }
  await refreshPrinters();
});

function resetCreateButton() {
  const btn = $("create-btn");
  btn.disabled = false;
  btn.textContent = "Make available over AirPrint";
}

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
