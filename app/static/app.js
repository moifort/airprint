const $ = (id) => document.getElementById(id);

const state = { uris: [], drivers: [], scanned: [], shared: [] };

async function api(path, options = {}) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = `Error ${res.status}`;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

// --- Printer list ------------------------------------------------------------

const STATE_LABELS = { idle: "Ready", printing: "Printing", stopped: "Stopped" };

async function refreshPrinters() {
  const list = $("printers-list");
  try {
    state.shared = await api("/api/printers");
    if (state.shared.length === 0) {
      list.innerHTML = `<p class="empty">No shared printer yet.</p>`;
      return;
    }
    list.innerHTML = state.shared.map((p) => `
      <div class="card printer">
        <div class="printer-info">
          <strong>${esc(p.name.replaceAll("_", " "))}</strong>
          <span class="model">${esc(p.make_model || "")}</span>
        </div>
        <div class="printer-actions">
          <span class="badge ${p.state === "stopped" ? "warn" : "ok"}">
            ${p.state === "stopped" ? "⚠︎" : "✓"} ${STATE_LABELS[p.state] || esc(p.state)} · AirPrint
          </span>
          ${p.jobs > 0 ? `
          <span class="badge warn">${p.jobs} job${p.jobs > 1 ? "s" : ""} queued</span>
          <button data-clear="${esc(p.name)}">Clear queue</button>` : ""}
          <button data-test="${esc(p.name)}">Test page</button>
          <button data-delete="${esc(p.name)}" class="danger">Delete</button>
        </div>
      </div>`).join("");
  } catch (err) {
    list.innerHTML = `<p class="error">${esc(err.message)}</p>`;
  }
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
  const test = e.target.dataset.test;
  const del = e.target.dataset.delete;
  const clear = e.target.dataset.clear;
  try {
    if (test) {
      e.target.disabled = true;
      await api(`/api/printers/${encodeURIComponent(test)}/test`, { method: "POST" });
      e.target.textContent = "Sent ✓";
      setTimeout(() => { e.target.textContent = "Test page"; e.target.disabled = false; }, 3000);
    } else if (clear && confirm(`Cancel all pending jobs on "${clear.replaceAll("_", " ")}"?`)) {
      e.target.disabled = true;
      await api(`/api/printers/${encodeURIComponent(clear)}/jobs`, { method: "DELETE" });
      refreshPrinters();
    } else if (del && confirm(`Delete "${del.replaceAll("_", " ")}"?`)) {
      await api(`/api/printers/${encodeURIComponent(del)}`, { method: "DELETE" });
      await refreshPrinters();
      renderDetected();
    }
  } catch (err) {
    alert(err.message);
    e.target.disabled = false;
  }
});

// --- Network scan --------------------------------------------------------------

$("rescan-btn").addEventListener("click", scanNetwork);

async function scanNetwork() {
  const btn = $("rescan-btn");
  btn.disabled = true;
  $("detected-list").innerHTML = `
    <p class="scanning"><span class="spinner"></span> Scanning your network for printers… (up to 30 s)</p>`;
  try {
    state.scanned = await api("/api/scan");
    renderDetected();
  } catch (err) {
    $("detected-list").innerHTML = `<p class="error">${esc(err.message)}</p>`;
  } finally {
    btn.disabled = false;
  }
}

function hostOf(uri) {
  try { return new URL(uri).hostname || null; } catch { return null; }
}

function renderDetected() {
  const sharedHosts = new Set(state.shared.map((p) => hostOf(p.uri)).filter(Boolean));
  const detected = state.scanned
    .map((p, i) => ({ p, i }))
    .filter(({ p }) => !p.uris.some((u) => sharedHosts.has(hostOf(u))));
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

async function addDetected(index) {
  const printer = state.scanned[index];
  const card = document.querySelector(`[data-card="${index}"]`);
  if (!printer || !card) return;
  try {
    renderAddProgress(card, printer, 0);
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
    await api("/api/printers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: printer.make_model, uri: printer.uris[0], ppd: drivers[0].ppd }),
    });
    renderAddProgress(card, printer, 2);
    await refreshPrinters();
    renderAddProgress(card, printer, ADD_STEPS.length);
    setTimeout(renderDetected, 2500);
  } catch (err) {
    renderAddError(card, index, err.message);
  }
}

function renderAddProgress(card, printer, current) {
  const done = current >= ADD_STEPS.length;
  card.innerHTML = `
    <div class="printer-info">
      <strong>${esc(printer.make_model)}</strong>
      <ul class="steps">
        ${ADD_STEPS.map((label, i) => {
          if (i < current) return `<li class="done">✓ ${label}</li>`;
          if (i === current) return `<li class="active"><span class="spinner"></span> ${label}…</li>`;
          return `<li>○ ${label}</li>`;
        }).join("")}
      </ul>
      ${done ? `<span class="success">✓ Now available on your Apple devices.</span>` : ""}
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
  $("wizard").classList.remove("hidden");
  $("printer-name").value = printer.make_model || "";
  selectScanned(printer);
  $("wizard").scrollIntoView({ behavior: "smooth" });
}

// --- Wizard ------------------------------------------------------------------

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
  try {
    const drivers = await api(`/api/drivers?${params}`);
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
  $("wizard").classList.remove("hidden");
  $("ip").focus();
});

$("cancel-wizard").addEventListener("click", () => $("wizard").classList.add("hidden"));

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
  if (!ppd && !ppdFile) return showError("Pick a driver or provide a PPD file.");
  showError("");

  const btn = $("create-btn");
  btn.disabled = true;
  btn.textContent = "Configuring…";
  try {
    if (ppdFile) {
      const form = new FormData();
      form.append("name", name);
      form.append("uri", uri);
      form.append("ppd_file", ppdFile);
      await api("/api/printers/upload", { method: "POST", body: form });
    } else {
      await api("/api/printers", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, uri, ppd }),
      });
    }
    $("wizard").classList.add("hidden");
    await refreshPrinters();
    renderDetected();
  } catch (err) {
    showError(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Make available over AirPrint";
  }
});

// --- Misc --------------------------------------------------------------------

function esc(text) {
  const div = document.createElement("div");
  div.textContent = text ?? "";
  return div.innerHTML;
}

$("cups-link").href = `http://${location.hostname}:631`;

refreshPrinters();
scanNetwork();
setInterval(refreshPrinters, 15000);
