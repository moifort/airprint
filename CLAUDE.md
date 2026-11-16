# AirPrint Bridge

Expose network printers over AirPrint: CUPS + Avahi + OpenPrinting drivers in
a Docker container, driven by a minimal web UI (FastAPI + vanilla JS).

## Layout

- `app/main.py` — FastAPI routes (`/api/scan`, `/api/detect`, `/api/drivers`, `/api/printers`)
- `app/detect.py` — network discovery (SNMP backend, ipptool, `lpinfo -v`)
- `app/cups_service.py` — CUPS via CLI tools (lpadmin, lpinfo, lpstat)
- `app/static/` — UI (vanilla JS, no framework, no build step)
- `tests/` — pytest suite (backend only)

## Conventions

- All code comments, error messages and UI text in English.
- Design notes and specs live in this file, never in a `docs/` folder.
- Conventional commits `type(scope): description`, lowercase, imperative.

## UI design: auto-scan + one-click add

Frontend only — every progress step shown maps to a real API call, never a
cosmetic timer.

1. **Auto-scan on page load.** The network scan starts automatically alongside
   the printer-list refresh. A "Detected on your network" section below
   "Shared printers" shows a spinner ("Scanning your network… (up to a minute)"),
   then one card per detected printer with **Customize…** (wizard pre-filled
   via `selectScanned`, to pick another driver or connection up front) and
   **Add**. Printers already shared (same host as an existing queue URI) are
   filtered out, except a card still showing an add (progress, error or
   warning). A **Rescan** button re-runs the scan; it is disabled while an add
   is in flight. During a rescan the previous cards stay listed under a
   "Rescanning…" line with their buttons disabled (no add may start while
   indexes are about to change); new results clear every card state. A failed
   scan shows the error and **Retry scan** above the previous results.
2. **One-click add.** Clicking Add turns the card into a progress checklist:
   *Finding the best driver* (`GET /api/drivers`, device-id first; the
   chosen driver then shows on the card as "Driver: …") →
   *Installing driver and configuring the print queue* (`POST /api/printers`) →
   *Publishing over AirPrint* (polls `GET /api/printers/{queue}/airprint`,
   which looks for the queue's `rp=printers/<queue>` TXT record in
   `avahi-browse`, up to 10 × 2 s; if never seen, a warning instead of the
   success line, no Retry since the queue exists). Name derived from
   make-and-model, deduplicated server-side (`_2`, `_3`…) because `lpadmin -p`
   on an existing name would overwrite that queue; first returned driver is
   used (`rank_drivers` order, "(recommended)" breaking ties between entries
   for the same model). On failure or no driver match: inline error with
   **Retry** and **Configure manually** (wizard pre-filled via
   `selectScanned`).
   The list is state-driven: `state.cards` holds each card's state (progress
   step / error / done + advertised / gone) keyed by index in `state.scanned`,
   and `renderDetected()` rebuilds every card from it, so any re-render (poll,
   another add) is harmless. Success shows 2.5 s, then the card leaves the
   list. The "not announced" warning stays until **Dismiss** or a rescan; an
   error stays until Retry, Configure manually or a rescan. Progress is read
   out through one persistent `role="status"` region (`#detected-status`),
   not through the re-created cards; the lists carry no `aria-live`.
3. **Wizard as fallback.** Manual IP entry, manual driver search, PPD upload.
   No scan button inside the wizard; the main button reads "Add manually".
   Detect pre-fills an empty name with the make-and-model; Connection options
   read "Raw socket, port 9100 — socket://…", "IPP — ipp://…" etc. (value is
   the URI); a selected PPD file shows a hint that it overrides the driver.
   After creation the wizard runs the same AirPrint check (`waitForAirPrint`,
   button "Publishing over AirPrint…"), then a success notice (fades after
   6 s) or the one-click warning as a notice that stays until dismissed. A
   failed check never reports the creation as failed.
4. **Lighter cards.** Shared-printer cards show name and make-and-model, no
   URI, and a badge row under them: the queue state only (Ready / Printing /
   Stopped, no AirPrint claim) and "N jobs queued". `.printer-actions` holds
   buttons only. A stopped queue (lpstat state `disabled`) shows a warning
   badge and a **Resume** button (`cupsenable`). The CUPS message ("The
   printer is not responding.") shows when the queue is stopped or has
   pending jobs: with `retry-job`, an unreachable printer leaves the queue
   idle with jobs, not stopped. Per-card buttons carry an `aria-label` naming
   the printer. Delete confirms that sharing stops and pending jobs are
   cancelled.
5. **Touch.** Buttons are 44 px high under `@media (pointer: coarse)`; below
   600 px the actions row sits under the info, left-aligned.

## Auto power: switch the printer's smart plug on demand

Printers left switched off are powered on when a job arrives, and off again
once idle, through a Zigbee2MQTT plug (HomeKit sees the change via Homebridge).

- **Config (env vars).** `POWER_PLUGS="<queue>=<z2m friendly name>,…"`
  (queue names case-insensitive, spaces and underscores equivalent since the
  card shows `_` as spaces and CUPS names cannot contain spaces; plug names
  verbatim; empty = feature off, image behaves as before), `MQTT_URL` (default
  `mqtt://localhost:1883`; optional `user:password@`, percent-decoded, passed
  as `-u`/`-P`, redacted from config errors), `POWER_OFF_DELAY` in minutes
  (default 10), `POWER_MAX_ON` in minutes (default 60, 0 = no cap). Publishes
  `zigbee2mqtt/<plug>/set` → `{"state":"ON"|"OFF"}` via `mosquitto_pub`.
- **Loop.** `app/power.py`: a thread started from the FastAPI lifespan polls
  `lpstat -o` every 2 s and feeds a pure state machine
  (`PowerController.tick(job_counts, now)`). Queues sharing a plug add up.
- **On.** Any pending job → `ON`, republished every 60 s while jobs remain
  (idempotent, covers a lost message). CUPS keeps the job meanwhile:
  `retry-job` policy and `JobRetryInterval 10` in `cupsd.conf`.
- **Off.** A plug is armed only once a job was seen on it; `POWER_OFF_DELAY`
  after its queues emptied → `OFF`, disarmed. A plug switched on by hand
  without printing, or a container restart, never turns anything off. The
  delay starts when the job leaves CUPS, while the printer may still be
  printing from its buffer — hence the margin.
- **Cap.** Jobs pending for `POWER_MAX_ON` without leaving the queue (printer
  unreachable, job held, wrong driver) → `OFF` anyway, logged as a warning;
  the plug then stays off while the job count stays at or below its lowest
  value since. A count above it (a new job) switches it on again with a fresh
  timer; an empty queue resets everything (no second `OFF`). A failed cap
  `OFF` is retried 10 s later without republishing `ON` meanwhile.
- **Errors.** A failed publish is logged and retried 10 s later; a failed
  `lpstat` skips the tick (never read as "no jobs"), logged once when it
  starts failing and once (info) when it recovers. Printing never blocks
  on MQTT. `GET /api/printers` adds `power_plug` and `power_gave_up`; the
  card shows "Auto power: <plug>", plus a warning line when the cap cut it.

## Backend notes

- Queue names in routes must start with a letter, digit or underscore: a
  leading `-` would be parsed as options (`cancel -a -x` purges every queue).
- `_existing_queues()` (name dedup, POWER_PLUGS check) lists local queues
  with `lpstat -p` (`-e` includes DNS-SD discoveries); only cupsd's "No
  destinations added" reads as empty, any other failure aborts the add.
- Queues are created with `printer-error-policy=retry-job`; the entrypoint
  applies it to existing queues too. CUPS' default `stop-printer` leaves the
  queue stopped whenever the printer is briefly off.
- `cupsd.conf` belongs to the image and is overwritten on every start;
  queues in the `/etc/cups` volume are kept. It uses `Port 631` (every
  address, IPv6 included: Avahi announces both and Apple devices may pick
  IPv6), not `Listen 0.0.0.0:631`.
- Supervision: tini is PID 1 (reaps orphaned filters, forwards SIGTERM).
  The entrypoint runs cupsd (`-f`) and uvicorn in the background and
  watches them, plus avahi and dbus through their pid files, every 10 s;
  it also counts cupsd as dead after 3 failed `lpstat -r` in a row. Any loss
  → exit 1, so `restart: unless-stopped` restarts the container (Docker
  never restarts an "unhealthy" one). SIGTERM → `avahi-daemon --kill`
  (goodbye packets), stop uvicorn and cupsd, exit 0.
- PPD uploads are refused with 413 on `Content-Length` before FastAPI parses
  the multipart body (it spools all of it first); the route's streaming 2 MB
  check stays for clients without the header.
- CI: `tests.yml` runs on pushes to other branches and PRs; `main` and `v*`
  tags are tested once, by `docker-publish.yml` calling it as the image gate.
- Writes (non-GET) carrying an `Origin` that differs from `Host` are rejected
  (CSRF). This does not stop DNS rebinding; the UI still has no auth.
- UI static files (index.html included, 304s too) are served with
  `Cache-Control: no-cache`: browsers revalidate via ETag on every load, so a
  stale cached index.html never runs against a newer app.js after an upgrade.
