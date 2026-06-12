# AirPrint Bridge

Make any network printer visible over **AirPrint** to your Mac, iPhone and iPad — even if it doesn't support it natively.

The container bundles **CUPS** (printer driving), **Avahi** (Bonjour/mDNS advertisement) and the **OpenPrinting driver database** (Gutenprint, HPLIP, brlaser, SpliX, foomatic…), all driven by a minimalist web interface:

1. enter the printer's IP address;
2. the model is detected automatically (SNMP/IPP) and the recommended driver is pre-selected — manual search or PPD file as fallbacks;
3. one click, and the printer shows up on all your Apple devices.

## Quick start

```yaml
# docker-compose.yml
services:
  airprint:
    image: ghcr.io/moifort/airprint:latest
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

App Store → **Install a customized app** (`+` icon) → paste the compose above (or fill in the same values: image `ghcr.io/moifort/airprint:latest`, network `host`, volume `/DATA/AppData/airprint/cups` → `/etc/cups`).

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

## Why host networking?

AirPrint relies on **mDNS** (multicast DNS, port 5353): Apple devices discover printers by listening to Bonjour announcements on the local network. Docker's *bridge* network does not pass that multicast traffic — without `network_mode: host`, the printer will never be visible.

## Troubleshooting

- **The printer doesn't show up on the Mac**: check the `host` network mode, then run `dns-sd -B _ipp._tcp` on a Mac — the printer must be listed. Also make sure the server and the Mac are on the same network/VLAN.
- **Avahi conflict**: if the host already runs `avahi-daemon` (port 5353 busy), the container won't be able to advertise printers. Disable the host's avahi (`systemctl disable --now avahi-daemon`) or use it to publish the services.
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
