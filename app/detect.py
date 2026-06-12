"""Discover and probe network printers.

Probing a known IP uses two mechanisms, in order:
1. the CUPS SNMP backend (`/usr/lib/cups/backend/snmp <ip>`), which queries
   the printer and returns its device URI, make-and-model and device ID;
2. as a fallback, an IPP Get-Printer-Attributes request through `ipptool`
   for printers that speak IPP but not SNMP.

Scanning the whole network relies on `lpinfo -v`, which runs every CUPS
discovery backend (SNMP broadcast, DNS-SD/Bonjour…).
"""

import re
import shlex
import subprocess
import urllib.parse

SNMP_BACKEND = "/usr/lib/cups/backend/snmp"
IPPTOOL_TEST = "get-printer-attributes.test"
PROBE_TIMEOUT = 15
# lpinfo is told to stop discovery after 10 s; the process timeout adds slack
SCAN_DISCOVERY_SECONDS = "10"
SCAN_TIMEOUT = 45
DISCOVERY_SCHEMES = ("socket://", "ipp://", "ipps://", "lpd://", "dnssd://")

_IPPTOOL_MAKE_MODEL = re.compile(r"printer-make-and-model \([^)]*\) = (.+)")


def parse_snmp_output(output: str) -> dict | None:
    """Extract URI, make-and-model and device ID from CUPS SNMP backend output.

    Line format: `network <uri> "<make-and-model>" "<info>" "<device-id>" "<location>"`
    """
    for line in output.splitlines():
        try:
            parts = shlex.split(line)
        except ValueError:
            continue
        if len(parts) >= 3 and parts[0] == "network":
            return {
                "uri": parts[1],
                "make_model": parts[2],
                "device_id": parts[4] if len(parts) >= 5 and parts[4] else None,
            }
    return None


def parse_ipptool_output(output: str) -> str | None:
    match = _IPPTOOL_MAKE_MODEL.search(output)
    return match.group(1).strip() if match else None


def parse_lpinfo_devices(output: str) -> list[dict]:
    """Parse `lpinfo -l -v` output into device dicts.

    Block format:
        Device: uri = socket://192.168.1.50:9100
                class = network
                info = HP LaserJet 1320
                make-and-model = HP LaserJet 1320
                device-id = MFG:HP;MDL:LaserJet 1320;
    """
    devices = []
    current = None
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("Device:"):
            current = {}
            devices.append(current)
            stripped = stripped[len("Device:"):].strip()
        if current is None or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        current[key.strip()] = value.strip()
    return devices


def candidate_uris(ip: str, detected_uri: str | None = None) -> list[str]:
    """Detected URI first, then the standard network protocols.

    Exception: dnssd URIs go last — they require live mDNS resolution on
    every job, which breaks as soon as the printer's Bonjour name changes;
    direct IP transport is far more reliable."""
    uris = [
        f"socket://{ip}:9100",
        f"ipp://{ip}/ipp/print",
        f"lpd://{ip}/queue",
    ]
    if detected_uri and detected_uri not in uris:
        if detected_uri.startswith("dnssd://"):
            uris.append(detected_uri)
        else:
            uris.insert(0, detected_uri)
    return uris


def _host_ip(uri: str) -> str | None:
    try:
        host = urllib.parse.urlsplit(uri).hostname
    except ValueError:
        return None
    return host if host and re.fullmatch(r"[0-9.]+", host) else None


def _run(cmd: list[str], timeout: int = PROBE_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def probe(ip: str) -> dict:
    """Return {found, make_model, device_id, uris} for the printer at this address."""
    make_model = None
    device_id = None
    detected_uri = None

    try:
        result = _run([SNMP_BACKEND, ip])
        snmp = parse_snmp_output(result.stdout)
        if snmp:
            make_model = snmp["make_model"]
            device_id = snmp["device_id"]
            detected_uri = snmp["uri"]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    if not make_model:
        try:
            result = _run(["ipptool", "-tv", f"ipp://{ip}/ipp/print", IPPTOOL_TEST])
            make_model = parse_ipptool_output(result.stdout)
            if make_model:
                detected_uri = f"ipp://{ip}/ipp/print"
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    return {
        "found": make_model is not None,
        "make_model": make_model,
        "device_id": device_id,
        "uris": candidate_uris(ip, detected_uri),
    }


# avahi-browse escapes special chars as \DDD (decimal), e.g. \032 for space
_AVAHI_ESCAPE = re.compile(r"\\(\d{3})")
_DNSSD_INSTANCE = re.compile(r"^dnssd://([^/]+?)\._")


def parse_avahi_browse(output: str) -> dict[str, str]:
    """Map service instance name -> IPv4 address from `avahi-browse -rpt`.

    Resolved line format:
        =;<iface>;IPv4;<escaped instance>;<type>;<domain>;<host>;<address>;<port>;<txt>
    """
    addresses = {}
    for line in output.splitlines():
        parts = line.split(";")
        if len(parts) >= 9 and parts[0] == "=" and parts[2] == "IPv4":
            name = _AVAHI_ESCAPE.sub(lambda m: chr(int(m.group(1))), parts[3])
            addresses[name] = parts[7]
    return addresses


def _resolve_dnssd_ips() -> dict[str, str]:
    """Resolve every announced service to its IPv4 address through Avahi."""
    try:
        result = _run(["avahi-browse", "--all", "-rpt"], timeout=PROBE_TIMEOUT)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {}
    return parse_avahi_browse(result.stdout)


def _scan_entry(device: dict) -> dict | None:
    uri = device.get("uri", "")
    if device.get("class") != "network" or not uri.startswith(DISCOVERY_SCHEMES):
        return None
    make_model = device.get("make-and-model") or device.get("info") or ""
    if not make_model or make_model.lower() == "unknown":
        make_model = device.get("info") or "Unknown printer"
    ip = _host_ip(uri)
    return {
        "uri": uri,
        "ip": ip,
        "make_model": make_model,
        "device_id": device.get("device-id") or None,
        "uris": candidate_uris(ip, uri) if ip else [uri],
    }


def scan() -> list[dict]:
    """Discover network printers through every CUPS backend (SNMP, DNS-SD…).

    The same printer can be reported by several backends: entries with an IP
    are deduplicated by IP, then IP-less entries (DNS-SD) are dropped when a
    printer with the same make-and-model was already found.
    """
    result = _run(
        ["lpinfo", "-l", "--timeout", SCAN_DISCOVERY_SECONDS, "-v"],
        timeout=SCAN_TIMEOUT,
    )
    entries = [
        entry for device in parse_lpinfo_devices(result.stdout)
        if (entry := _scan_entry(device))
    ]

    # DNS-SD discoveries carry a Bonjour name instead of an IP; resolve it so
    # the queue can be driven over a direct, reliable transport (socket://ip).
    if any(entry["ip"] is None for entry in entries):
        resolved = _resolve_dnssd_ips()
        for entry in entries:
            if entry["ip"] is not None:
                continue
            m = _DNSSD_INSTANCE.match(entry["uri"])
            instance = urllib.parse.unquote(m.group(1)) if m else None
            if instance and (ip := resolved.get(instance)):
                entry["ip"] = ip
                entry["uris"] = candidate_uris(ip, entry["uri"])

    printers = []
    seen_ips = set()
    seen_models = set()
    for entry in sorted(entries, key=lambda e: e["ip"] is None):
        if entry["ip"]:
            if entry["ip"] in seen_ips:
                continue
            seen_ips.add(entry["ip"])
        elif entry["make_model"] in seen_models:
            continue
        seen_models.add(entry["make_model"])
        printers.append(entry)
    return printers
