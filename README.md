# AirPrint Bridge

Make any network printer visible over **AirPrint** to your Mac, iPhone and iPad — even if it doesn't support it natively.

The container bundles **CUPS** (printer driving), **Avahi** (Bonjour/mDNS advertisement) and the **OpenPrinting driver database** (Gutenprint, HPLIP, brlaser, SpliX, foomatic…), all driven by a minimalist web interface:

1. scan the network — printers are discovered automatically (SNMP broadcast + Bonjour), or enter an IP manually;
2. the recommended driver is pre-selected (matched by IEEE 1284 device ID, with make-and-model as fallback) — manual search or PPD file as last resorts;
3. one click, and the printer shows up on all your Apple devices.

## Quick start

```yaml
# docker-compose.yml
services:
  airprint:
    image: ghcr.io/moifort/airprint:main
    container_name: airprint
    # Required: AirPrint relies on mDNS (multicast), which does not
    # cross Docker's bridge network.
    network_mode: host
    restart: unless-stopped
    environment:
      UI_PORT: "8080"
    volumes:
      - /DATA/AppData/airprint/cups:/etc/cups
```

```bash
docker compose up -d
```

Then open `http://<server-ip>:8080` and add your printer.

### CasaOS

App Store → **Install a customized app** (`+` icon) → paste the compose above (or fill in the same values: image `ghcr.io/moifort/airprint:main`, network `host`, volume `/DATA/AppData/airprint/cups` → `/etc/cups`).

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `UI_PORT` | `8080` | Web interface port |

| Port | Purpose |
|------|---------|
| `${UI_PORT}` (8080) | Web interface |
| `631` | CUPS/IPP — classic CUPS administration at `http://<server-ip>:631` |
| `5353/udp` | mDNS (Avahi) — Bonjour announcements |

| Volume | Purpose |
|--------|---------|
| `/etc/cups` | Printer configuration (persists queues across restarts) |

## How network auto-detection works

Clicking **Scan the network** runs every CUPS discovery backend (`lpinfo -v`) from inside the container:

- **SNMP broadcast** — a probe is sent to the broadcast address of every local subnet; virtually all network printers answer with their model, connection URI and IEEE 1284 device ID. This is how printers that predate Bonjour are found.
- **DNS-SD / Bonjour** — Avahi listens for printers announcing themselves over mDNS (`_pdl-datastream`, `_ipp`, `_printer`).

Results from both sources are merged and deduplicated (by IP, then by model). Selecting a printer triggers the driver matching, in order of reliability:

1. **IEEE 1284 device ID** (`MFG:Brother;MDL:HL-1210W series;…`) — the identifier printers embed for exact driver lookup;
2. **make-and-model** — CUPS' native name matching against the bundled OpenPrinting database;
3. **fuzzy family matching** — when nothing matches exactly, the closest driver names are proposed; this catches *family* drivers (a Brother HL-1210W is driven by the brlaser «HL-1200 series» entry);
4. **manual fallbacks** — free-text driver search, or uploading the manufacturer's PPD file.

Detection by IP (the manual field under the scan button) uses the same SNMP probe against a single address, with an IPP Get-Printer-Attributes query as fallback.

## Why host networking?

AirPrint relies on **mDNS** (multicast DNS, port 5353): Apple devices discover printers by listening to Bonjour announcements on the local network. Docker's *bridge* network does not pass that multicast traffic — without `network_mode: host`, the printer will never be visible.

## Troubleshooting

- **The printer doesn't show up on the Mac**: check the `host` network mode, then run `dns-sd -B _ipp._tcp` on a Mac — the printer must be listed. Also make sure the server and the Mac are on the same network/VLAN.
- **The printer appears then disappears, or shows up as `name @ host-34`**: another mDNS responder on the same host is fighting over the records. Because every responder on the host shares the same IP, two mDNS daemons inevitably conflict on the reverse-PTR record (`x.x.x.x.in-addr.arpa`) and rename each other in a loop. Run only **one** mDNS responder per host: disable the host's avahi (`systemctl disable --now avahi-daemon`) and the embedded avahi of other containers (e.g. Homebridge: set `ENABLE_AVAHI=0` — its HomeKit advertiser does not need it).
- **Model not detected**: some printers expose neither SNMP nor IPP. Use the manual driver search (bundled OpenPrinting database) or provide the manufacturer's PPD file.
- **Printing fails despite detection**: try another connection in the selector (`socket://` works on most printers, port 9100).

## Development

```bash
pip install -r requirements-dev.txt
pytest

docker build -t airprint .
docker run --rm --network host airprint
```

On every push to `main` (and `v*` tag), the multi-architecture image (amd64 + arm64) is published to GHCR by GitHub Actions.
