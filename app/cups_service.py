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
# lpstat prints "printer X is idle", "printer X now printing X-1" and
# "printer X disabled" — keep the state word, not the "is"/"now" filler
_LPSTAT_PRINTER = re.compile(r"^printer (\S+) (?:is |now )?(\w+)")
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


_LPSTAT_JOB = re.compile(r"^(\S+)-\d+\s")


def parse_job_counts(jobs_output: str) -> dict[str, int]:
    """Pending job count per queue from `lpstat -o` (lines `<queue>-<id> …`)."""
    counts: dict[str, int] = {}
    for line in jobs_output.splitlines():
        if m := _LPSTAT_JOB.match(line):
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    return counts


_MODEL_NUMBER = re.compile(r"\d+")


def _number_prefix_score(target: str, candidate: str) -> float:
    """Similarity of the leading model numbers, by common prefix length.

    Printer families share the number prefix (HL-1200 series drives the
    HL-1210W); plain string similarity misses that and can rank an HL-2170W
    driver above the HL-1200 one for an HL-1210W printer."""
    t = _MODEL_NUMBER.search(target)
    c = _MODEL_NUMBER.search(candidate)
    if not t or not c:
        return 0.0
    common = 0
    for a, b in zip(t.group(), c.group()):
        if a != b:
            break
        common += 1
    return common / max(len(t.group()), len(c.group()))


def rank_drivers(drivers: list[dict], make_model: str) -> list[dict]:
    """Order drivers by similarity to the printer model.

    CUPS returns family matches in an arbitrary order and the UI picks the
    first one; rank by model-number family first, full-name similarity as
    tie-breaker."""
    target = _normalize_model(make_model)

    def score(driver: dict) -> float:
        name = _normalize_model(driver["name"])
        seq = difflib.SequenceMatcher(None, target, name).ratio()
        return 0.5 * _number_prefix_score(target, name) + 0.5 * seq

    return sorted(drivers, key=score, reverse=True)


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
    try:
        jobs = parse_job_counts(_run(["lpstat", "-o"]).stdout)
    except CupsError:
        jobs = {}
    for printer in printers:
        printer["make_model"] = _ppd_nickname(printer["name"])
        printer["jobs"] = jobs.get(printer["name"], 0)
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


def cancel_jobs(name: str) -> None:
    """Cancel every pending job on the queue (rescue for stuck queues)."""
    if not _VALID_QUEUE_NAME.match(name):
        raise CupsError("invalid queue name")
    _run(["cancel", "-a", name])


def print_test_page(name: str) -> None:
    if not _VALID_QUEUE_NAME.match(name):
        raise CupsError("invalid queue name")
    _run(["lp", "-d", name, TESTPRINT])
