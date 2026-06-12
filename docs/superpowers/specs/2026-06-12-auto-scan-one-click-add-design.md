# Auto-scan on page load + one-click printer add

**Date:** 2026-06-12
**Status:** Approved

## Goal

Remove friction from the first-run experience: when the user opens the web UI,
the network scan starts automatically and detected printers can be added with a
single click, with a step-by-step progress display explaining what is happening
(driver lookup, queue configuration, AirPrint publication).

## Scope

Frontend only (`app/static/index.html`, `app/static/app.js`,
`app/static/style.css`). No backend changes: every progress step shown to the
user maps to a real existing API call, not a cosmetic timer.

## Design

### 1. Auto-scan on page load

- On page load, `refreshPrinters()` and the network scan start in parallel.
- A new section **“Detected on your network”** sits below “Shared printers”.
  While scanning it shows a spinner with “Scanning your network… (up to 30 s)”.
- Detected printers that are **already shared** are filtered out, by comparing
  the host (IP) of the detected URIs with the host of each existing CUPS queue
  URI.
- A **Rescan** button re-runs the scan. When nothing is found, the section
  shows a short message pointing to manual add.

### 2. One-click add with step-by-step progress

Clicking **Add** on a detected printer turns its card into a progress
checklist. Each step corresponds to a real call:

| Step | Label | Backing call |
|------|-------|--------------|
| 1 | Finding the best driver | `GET /api/drivers` (device-id first, then make-and-model) |
| 2 | Installing driver and configuring the print queue | `POST /api/printers` (lpadmin installs the PPD, creates the shared queue) |
| 3 | Publishing over AirPrint | printer list refresh; step completes when the new queue appears |

- The printer name is derived from the detected make-and-model.
- The driver is the first result returned (the API already orders device-id
  matches first).
- If no driver matches, or a step fails, the card shows the error with two
  actions: **Retry** and **Configure manually** (opens the wizard pre-filled
  via the existing `selectScanned` flow).

### 3. Slimmed-down wizard

The wizard remains as the fallback path: manual IP entry, manual driver
search, PPD upload. Its embedded scan button is removed (the scan now lives on
the main page). The “+ Add a printer” button becomes **“Add manually”**.

### 4. Lighter printer cards

The URI line is removed from shared-printer cards; name and make-and-model
remain.

## Error handling

- Scan failure: error message in the detected section with a Retry button.
- Driver lookup empty or queue creation failure: inline error on the card with
  Retry / Configure manually.
- All UI text stays in English.

## Testing

No backend change, so the existing pytest suite is unaffected. Frontend is
verified by running the app and exercising: auto-scan render, one-click add
happy path, no-driver fallback, manual wizard path, URI removal.
