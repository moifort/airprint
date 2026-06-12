const $ = (id) => document.getElementById(id);

const state = { uris: [], drivers: [] };

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
    const printers = await api("/api/printers");
    if (printers.length === 0) {
      list.innerHTML = `<p class="empty">No shared printer yet.</p>`;
      return;
    }
    list.innerHTML = printers.map((p) => `
      <div class="card printer">
        <div class="printer-info">
          <strong>${esc(p.name.replaceAll("_", " "))}</strong>
          <span class="model">${esc(p.make_model || "")}</span>
          <span class="uri">${esc(p.uri || "")}</span>
        </div>
        <div class="printer-actions">
          <span class="badge ${p.state === "stopped" ? "warn" : "ok"}">
            ${p.state === "stopped" ? "⚠︎" : "✓"} ${STATE_LABELS[p.state] || esc(p.state)} · AirPrint
          </span>
          <button data-test="${esc(p.name)}">Test page</button>
          <button data-delete="${esc(p.name)}" class="danger">Delete</button>
        </div>
      </div>`).join("");
  } catch (err) {
    list.innerHTML = `<p class="error">${esc(err.message)}</p>`;
  }
}

document.addEventListener("click", async (e) => {
  const test = e.target.dataset.test;
  const del = e.target.dataset.delete;
  try {
    if (test) {
      e.target.disabled = true;
      await api(`/api/printers/${encodeURIComponent(test)}/test`, { method: "POST" });
      e.target.textContent = "Sent ✓";
      setTimeout(() => { e.target.textContent = "Test page"; e.target.disabled = false; }, 3000);
    } else if (del && confirm(`Delete "${del.replaceAll("_", " ")}"?`)) {
      await api(`/api/printers/${encodeURIComponent(del)}`, { method: "DELETE" });
      refreshPrinters();
    }
  } catch (err) {
    alert(err.message);
    e.target.disabled = false;
  }
});

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
    refreshPrinters();
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
setInterval(refreshPrinters, 15000);
