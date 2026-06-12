"""Sonde une imprimante réseau pour identifier son modèle et son URI.

Deux mécanismes, dans l'ordre :
1. le backend SNMP de CUPS (`/usr/lib/cups/backend/snmp <ip>`), qui interroge
   l'imprimante et renvoie son URI de connexion et son make-and-model ;
2. en repli, une requête IPP Get-Printer-Attributes via `ipptool` pour les
   imprimantes qui parlent IPP mais pas SNMP.
"""

import re
import shlex
import subprocess

SNMP_BACKEND = "/usr/lib/cups/backend/snmp"
IPPTOOL_TEST = "get-printer-attributes.test"
PROBE_TIMEOUT = 15

_IPPTOOL_MAKE_MODEL = re.compile(r"printer-make-and-model \([^)]*\) = (.+)")


def parse_snmp_output(output: str) -> dict | None:
    """Extrait URI et make-and-model d'une sortie du backend SNMP de CUPS.

    Format d'une ligne : `network <uri> "<make-and-model>" "<info>" "<device-id>" "<location>"`
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
    """URI détectée en premier, puis les protocoles réseau standards."""
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
    """Renvoie {found, make_model, uris} pour l'imprimante à cette adresse."""
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
