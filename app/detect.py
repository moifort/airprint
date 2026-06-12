"""Probe a network printer to identify its model and connection URI.

Two mechanisms, in order:
1. the CUPS SNMP backend (`/usr/lib/cups/backend/snmp <ip>`), which queries
   the printer and returns its device URI and make-and-model;
2. as a fallback, an IPP Get-Printer-Attributes request through `ipptool`
   for printers that speak IPP but not SNMP.
"""

import re
import shlex
import subprocess

SNMP_BACKEND = "/usr/lib/cups/backend/snmp"
IPPTOOL_TEST = "get-printer-attributes.test"
PROBE_TIMEOUT = 15

_IPPTOOL_MAKE_MODEL = re.compile(r"printer-make-and-model \([^)]*\) = (.+)")


def parse_snmp_output(output: str) -> dict | None:
    """Extract URI and make-and-model from CUPS SNMP backend output.

    Line format: `network <uri> "<make-and-model>" "<info>" "<device-id>" "<location>"`
    """
    for line in output.splitlines():
        try:
            parts = shlex.split(line)
        except ValueError:
            continue
        if len(parts) >= 3 and parts[0] == "network":
            return {"uri": parts[1], "make_model": parts[2]}
    return None


def parse_ipptool_output(output: str) -> str | None:
    match = _IPPTOOL_MAKE_MODEL.search(output)
    return match.group(1).strip() if match else None


def candidate_uris(ip: str, detected_uri: str | None = None) -> list[str]:
    """Detected URI first, then the standard network protocols."""
    uris = [
        f"socket://{ip}:9100",
        f"ipp://{ip}/ipp/print",
        f"lpd://{ip}/queue",
    ]
    if detected_uri and detected_uri not in uris:
        uris.insert(0, detected_uri)
    return uris


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=PROBE_TIMEOUT
    )


def probe(ip: str) -> dict:
    """Return {found, make_model, uris} for the printer at this address."""
    make_model = None
    detected_uri = None

    try:
        result = _run([SNMP_BACKEND, ip])
        snmp = parse_snmp_output(result.stdout)
        if snmp:
            make_model = snmp["make_model"]
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
        "uris": candidate_uris(ip, detected_uri),
    }
