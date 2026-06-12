"""Drive CUPS through its command-line tools (lpadmin, lpinfo, lpstat).

Every queue is created shared (`printer-is-shared=true`): that sharing,
combined with Avahi, is what makes queues visible over AirPrint.
"""

import difflib
import re
import subprocess
from pathlib import Path

PPD_DIR = Path("/etc/cups/ppd")
TESTPRINT = "/usr/share/cups/data/testprint"
COMMAND_TIMEOUT = 60

_VALID_QUEUE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
# lpstat prints "printer X is idle" but "printer X disabled" (no "is")
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
        raise CupsError(result.stderr.strip() or f"{cmd[0]} failed")
    return result


def queue_name(friendly_name: str) -> str:
    """Convert a free-form name into a valid CUPS queue name."""
    name = friendly_name.strip().replace(" ", "_")
    name = re.sub(r"[^A-Za-z0-9_-]", "", name)
    if not name:
        raise CupsError("invalid printer name")
    return name


def parse_lpinfo_output(output: str) -> list[dict]:
    """One lpinfo -m line: `<ppd> <make and model>`."""
    drivers = []
    for line in output.splitlines():
        ppd, _, name = line.partition(" ")
        if ppd:
            drivers.append({"ppd": ppd, "name": name.strip() or ppd})
    return drivers


def list_drivers(make_model: str | None = None, device_id: str | None = None) -> list[dict]:
    """Installed drivers matching a printer, using CUPS' native matching.

    Matching by IEEE 1284 device ID is far more reliable than by
    make-and-model; use it whenever the printer reported one."""
    if device_id:
        criteria = ["--device-id", device_id]
    elif make_model:
        criteria = ["--make-and-model", make_model]
    else:
        raise CupsError("missing driver search criteria")
    try:
        result = _run(["lpinfo", *criteria, "-m"])
    except CupsError as exc:
        # lpinfo exits 1 with client-error-not-found when nothing matches:
        # that is an empty result, not a failure
        if "client-error-not-found" in str(exc):
            return []
        raise
    return parse_lpinfo_output(result.stdout)


def _normalize_model(name: str) -> str:
    """Reduce a driver/printer name to its make-and-model core for comparison."""
    name = name.partition(",")[0]
    name = re.sub(r"foomatic/\S+|\(recommended\)|-?\s*cups\+gutenprint.*", "", name, flags=re.I)
    name = re.sub(r"[^a-z0-9]+", " ", name.lower())
    return name.replace(" series", "").strip()


def fuzzy_match_drivers(make_model: str, limit: int = 10) -> list[dict]:
    """Heuristic fallback when CUPS exact matching finds nothing.

    Scores every installed driver against the printer model and keeps the
    closest ones. Catches family drivers that exact matching misses, e.g.
    a Brother HL-1210W is driven by the "HL-1200 series" brlaser entry."""
    drivers = parse_lpinfo_output(_run(["lpinfo", "-m"]).stdout)
    target = _normalize_model(make_model)
    scored = []
    for driver in drivers:
        score = difflib.SequenceMatcher(
            None, target, _normalize_model(driver["name"])
        ).ratio()
        if score >= 0.75:
            scored.append((score, driver))
    scored.sort(key=lambda item: -item[0])
    return [driver for _, driver in scored[:limit]]


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
        # lpstat -p fails when no printer is configured
        return []
    devices_out = _run(["lpstat", "-v"]).stdout
    printers = parse_lpstat(printers_out, devices_out)
    for printer in printers:
        printer["make_model"] = _ppd_nickname(printer["name"])
    return printers


def add_printer(name: str, uri: str, ppd: str, description: str | None = None) -> str:
    """Create a shared queue. `ppd` is either an lpinfo model name (-m) or a
    path to an uploaded PPD file (-P)."""
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
        raise CupsError("invalid queue name")
    _run(["lpadmin", "-x", name])


def print_test_page(name: str) -> None:
    if not _VALID_QUEUE_NAME.match(name):
        raise CupsError("invalid queue name")
    _run(["lp", "-d", name, TESTPRINT])
