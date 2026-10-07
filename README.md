# AirPrint Bridge

Print from your iPhone, iPad or Mac to any network printer — even one that doesn't support AirPrint.

Run one container, open the web page: printers on your network are detected automatically, and one click makes them visible to all your Apple devices.

![AirPrint Bridge web UI](docs/screenshot.png)

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

Open `http://<server-ip>:8080`, wait a few seconds for the scan, then click **Add** next to your printer. It now shows up in the print dialog of every Apple device on the network.

If your printer isn't detected, **Add manually** lets you enter its IP address, search the bundled driver database, or upload the manufacturer's PPD file.

### CasaOS

App Store → **Install a customized app** (`+` icon) → paste the compose above.

## How it works

The container bundles **CUPS** (the printing system), **Avahi** (Bonjour/mDNS) and the **OpenPrinting driver database** (Gutenprint, HPLIP, brlaser, SpliX, foomatic…):

1. the network is scanned over **SNMP broadcast** and **Bonjour** — printers answer with their model and connection URI;
2. the right driver is matched automatically — by IEEE 1284 device ID first, then by make-and-model;
3. the printer becomes a CUPS queue announced over mDNS — which is all AirPrint is.

`network_mode: host` is required because Apple devices discover printers through mDNS multicast, which does not cross Docker's bridge network.

## Configuration

| | Purpose |
|---|---|
| `UI_PORT` (default `8080`) | Web interface port |
| Port `631` | Classic CUPS administration at `http://<server-ip>:631` |
| Port `5353/udp` | mDNS (Avahi) — Bonjour announcements |
| Volume `/etc/cups` | Printer configuration (persists queues across restarts) |
| `POWER_PLUGS` (default empty) | Auto power: `<queue>=<Zigbee2MQTT device>,…` — see below |
| `MQTT_URL` (default `mqtt://localhost:1883`) | Auto power: MQTT broker used by Zigbee2MQTT |
| `POWER_OFF_DELAY` (default `10`) | Auto power: minutes after the last job before switching off |

### Auto power (smart plug)

Keep the printer switched off and let the bridge power it on when a job comes in. The printer's smart plug must be a [Zigbee2MQTT](https://www.zigbee2mqtt.io) device (if it shows up in HomeKit through Homebridge's Zigbee2MQTT plugin, it is one).

```yaml
    environment:
      UI_PORT: "8080"
      POWER_PLUGS: "Brother_HL-1210W_series=atelier_imprimante"
      MQTT_URL: "mqtt://192.168.1.199:1883"
      POWER_OFF_DELAY: "10"
```

The queue name is the one shown on the printer card; the plug name is the device's friendly name in Zigbee2MQTT. When a job arrives, the bridge publishes `{"state":"ON"}` to `zigbee2mqtt/<plug>/set`; CUPS holds the job until the printer answers, then prints it. `POWER_OFF_DELAY` minutes after the queue empties, the plug is switched off — only if a job went through, so a plug switched on by hand is left alone. The printer card shows **Auto power: \<plug\>** when configured.

### Security note

The web UI has **no authentication**: anyone who can reach port `8080` can add, delete or reconfigure printers. It is designed for a trusted home LAN — do not expose the port to the internet or an untrusted network (put a reverse proxy with authentication in front if you need remote access).

## Troubleshooting

- **The printer doesn't show up on the Mac**: check the `host` network mode, then run `dns-sd -B _ipp._tcp` on a Mac — the printer must be listed. Also make sure the server and the Mac are on the same network/VLAN.
- **The printer appears then disappears, or shows up as `name @ host-34`**: two mDNS responders on the same host are fighting over the records. Run only **one** mDNS responder per host: disable the host's avahi (`systemctl disable --now avahi-daemon`) and the embedded avahi of other containers (e.g. Homebridge: set `ENABLE_AVAHI=0`).
- **Model not detected**: some printers expose neither SNMP nor IPP. Use the manual driver search or provide the manufacturer's PPD file.
- **Printing fails despite detection**: try another connection in the selector (`socket://` works on most printers, port 9100).

## Development

```bash
pip install -r requirements-dev.txt
pytest

docker build -t airprint .
docker run --rm --network host airprint
```

On every push to `main` (and `v*` tag), the multi-architecture image (amd64 + arm64) is published to GHCR by GitHub Actions.
