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
   "Shared printers" shows a spinner ("Scanning your network… (up to 30 s)"),
   then one card per detected printer with an **Add** button. Printers already
   shared (same host as an existing queue URI) are filtered out. A **Rescan**
   button re-runs the scan.
2. **One-click add.** Clicking Add turns the card into a progress checklist:
   *Finding the best driver* (`GET /api/drivers`, device-id first) →
   *Installing driver and configuring the print queue* (`POST /api/printers`) →
   *Publishing over AirPrint* (list refresh confirms the queue). Name derived
   from make-and-model; first returned driver is used. On failure or no
   driver match: inline error with **Retry** and **Configure manually**
   (wizard pre-filled via `selectScanned`).
3. **Wizard as fallback.** Manual IP entry, manual driver search, PPD upload.
   No scan button inside the wizard; the main button reads "Add manually".
4. **Lighter cards.** Shared-printer cards show name and make-and-model, no
   URI.
