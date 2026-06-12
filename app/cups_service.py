"""Pilotage de CUPS via ses outils en ligne de commande (lpadmin, lpinfo, lpstat).

Toutes les files sont créées partagées (`printer-is-shared=true`) : c'est ce
partage, combiné à Avahi, qui les rend visibles en AirPrint.
"""

import re
import subprocess
from pathlib import Path

PPD_DIR = Path("/etc/cups/ppd")
TESTPRINT = "/usr/share/cups/data/testprint"
COMMAND_TIMEOUT = 60

_VALID_QUEUE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
# lpstat écrit « printer X is idle » mais « printer X disabled » (sans « is »)
_LPSTAT_PRINTER = re.compile(r"^printer (\S+) (?:is )?(\w+)")
_LPSTAT_DEVICE = re.compile(r"^device for (\S+): (.+)$")
_PPD_NICKNAME = re.compile(r'^\*NickName:\s*"(.+)"')


class CupsError(Exception):
    pass


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT,
        env={"LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
    )
    if result.returncode != 0:
        raise CupsError(result.stderr.strip() or f"échec de {cmd[0]}")
    return result


def queue_name(friendly_name: str) -> str:
    """Convertit un nom libre en nom de file CUPS valide."""
    name = friendly_name.strip().replace(" ", "_")
    name = re.sub(r"[^A-Za-z0-9_-]", "", name)
    if not name:
        raise CupsError("nom d'imprimante invalide")
    return name


def parse_lpinfo_output(output: str) -> list[dict]:
    """Une ligne lpinfo -m : `<ppd> <make and model>`."""
    drivers = []
    for line in output.splitlines():
        ppd, _, name = line.partition(" ")
        if ppd:
            drivers.append({"ppd": ppd, "name": name.strip() or ppd})
    return drivers


def list_drivers(make_model: str) -> list[dict]:
    """Drivers installés correspondant à un modèle, via le matching natif de CUPS."""
    result = _run(["lpinfo", "--make-and-model", make_model, "-m"])
    return parse_lpinfo_output(result.stdout)


def parse_lpstat(printers_output: str, devices_output: str) -> list[dict]:
    devices = dict(
        m.groups() for line in devices_output.splitlines()
        if (m := _LPSTAT_DEVICE.match(line))
    )
    printers = []
    for line in printers_output.splitlines():
        if m := _LPSTAT_PRINTER.match(line):
            name, state = m.groups()
            printers.append({"name": name, "state": state, "uri": devices.get(name)})
    return printers


def _ppd_nickname(name: str) -> str | None:
    ppd = PPD_DIR / f"{name}.ppd"
    try:
        for line in ppd.read_text(errors="replace").splitlines():
            if m := _PPD_NICKNAME.match(line):
                return m.group(1)
    except OSError:
        pass
    return None


def list_printers() -> list[dict]:
    try:
        printers_out = _run(["lpstat", "-p"]).stdout
    except CupsError:
        # lpstat -p échoue quand aucune imprimante n'est configurée
        return []
    devices_out = _run(["lpstat", "-v"]).stdout
    printers = parse_lpstat(printers_out, devices_out)
    for printer in printers:
        printer["make_model"] = _ppd_nickname(printer["name"])
    return printers


def add_printer(name: str, uri: str, ppd: str, description: str | None = None) -> str:
    """Crée une file partagée. `ppd` est un nom de modèle lpinfo (-m) ou un
    chemin de fichier PPD uploadé (-P)."""
    queue = queue_name(name)
    ppd_flag = "-P" if ppd.startswith("/") else "-m"
    cmd = [
        "lpadmin", "-p", queue, "-E", "-v", uri, ppd_flag, ppd,
        "-o", "printer-is-shared=true",
        "-D", description or name,
    ]
    _run(cmd)
    _run(["cupsctl", "--share-printers"])
    return queue


def delete_printer(name: str) -> None:
    if not _VALID_QUEUE_NAME.match(name):
        raise CupsError("nom de file invalide")
    _run(["lpadmin", "-x", name])


def print_test_page(name: str) -> None:
    if not _VALID_QUEUE_NAME.match(name):
        raise CupsError("nom de file invalide")
    _run(["lp", "-d", name, TESTPRINT])
