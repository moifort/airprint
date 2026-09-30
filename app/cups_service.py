"""Drive CUPS through its command-line tools (lpadmin, lpinfo, lpstat).

Every queue is created shared (`printer-is-shared=true`): that sharing,
combined with Avahi, is what makes queues visible over AirPrint.
"""

import difflib
import functools
import re
import subprocess
import threading
import unicodedata
from pathlib import Path

PPD_DIR = Path("/etc/cups/ppd")
TESTPRINT = "/usr/share/cups/data/testprint"
COMMAND_TIMEOUT = 60
AVAHI_TIMEOUT = 15
# CUPS caps queue names at 127 bytes; leave room for a "_N" dedup suffix
MAX_QUEUE_NAME = 100

_VALID_QUEUE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
# lpstat prints "printer X is idle", "printer X now printing X-1" and
# "printer X disabled" — keep the state word, not the "is"/"now" filler
_LPSTAT_PRINTER = re.compile(r"^printer (\S+) (?:is |now )?(\w+)")
_LPSTAT_DEVICE = re.compile(r"^device for (\S+): (.+)$")
_PPD_NICKNAME = re.compile(r'^\*NickName:\s*"(.+)"')


class CupsError(Exception):
    pass


class CupsTimeout(CupsError):
    pass


def _run(cmd: list[str], timeout: int = COMMAND_TIMEOUT) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env={"LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
        )
    except subprocess.TimeoutExpired as exc:
        raise CupsTimeout(f"{cmd[0]} timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise CupsError(f"{cmd[0]} not found") from exc
    if result.returncode != 0:
        raise CupsError(result.stderr.strip() or f"{cmd[0]} failed")
    return result


def queue_name(friendly_name: str) -> str:
    """Convert a free-form name into a valid CUPS queue name.

    Accented letters are transliterated ("Épson" → "Epson") rather than
    dropped, so the queue name stays recognizable."""
    name = unicodedata.normalize("NFKD", friendly_name)
    name = name.encode("ascii", "ignore").decode().strip().replace(" ", "_")
    name = re.sub(r"[^A-Za-z0-9_-]", "", name)[:MAX_QUEUE_NAME]
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


@functools.cache
def _all_drivers() -> tuple[dict, ...]:
    """Every installed driver. `lpinfo -m` walks thousands of PPDs and takes
    seconds; the driver set is baked into the image, so list it once."""
    return tuple(parse_lpinfo_output(_run(["lpinfo", "-m"]).stdout))


def fuzzy_match_drivers(make_model: str, limit: int = 10) -> list[dict]:
    """Heuristic fallback when CUPS exact matching finds nothing.

    Scores every installed driver against the printer model and keeps the
    closest ones. Catches family drivers that exact matching misses, e.g.
    a Brother HL-1210W is driven by the "HL-1200 series" brlaser entry."""
    drivers = _all_drivers()
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


def parse_lpstat(output: str) -> list[dict]:
    """Parse combined `lpstat -p -v` output.

    A stopped queue is followed by an indented line with the reason, e.g.
    `\tUnable to connect to printer; will retry in 30 seconds...`."""
    devices = dict(
        m.groups() for line in output.splitlines()
        if (m := _LPSTAT_DEVICE.match(line))
    )
    printers = []
    for line in output.splitlines():
        if m := _LPSTAT_PRINTER.match(line):
            name, state = m.groups()
            printers.append(
                {"name": name, "state": state, "uri": devices.get(name), "message": None}
            )
        elif line[:1].isspace() and printers and printers[-1]["message"] is None:
            printers[-1]["message"] = line.strip() or None
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
    # One lpstat call instead of three: the UI polls this every 15 s per tab
    try:
        output = _run(["lpstat", "-p", "-v", "-o"]).stdout
    except CupsTimeout:
        raise
    except CupsError:
        # lpstat fails when no printer is configured
        return []
    printers = parse_lpstat(output)
    jobs = parse_job_counts(output)
    for printer in printers:
        printer["make_model"] = _ppd_nickname(printer["name"])
        printer["jobs"] = jobs.get(printer["name"], 0)
    return printers


def _existing_queues() -> set[str]:
    try:
        output = _run(["lpstat", "-e"]).stdout
    except CupsTimeout:
        raise
    except CupsError:
        # lpstat -e fails when no printer is configured
        return set()
    return {line.strip().lower() for line in output.splitlines() if line.strip()}


# Name deduplication and queue creation must be atomic across requests
_add_lock = threading.Lock()


def add_printer(name: str, uri: str, ppd: str) -> str:
    """Create a shared queue. `ppd` is either an lpinfo model name (-m) or a
    path to an uploaded PPD file (-P).

    `lpadmin -p` on an existing name silently reconfigures that queue, so a
    second printer of the same model would overwrite the first: pick a free
    name instead ("HP_LaserJet_2"). CUPS queue names are case-insensitive.

    Queues retry failed jobs instead of stopping: with CUPS' default
    stop-printer policy, a printer that is off for a moment leaves the queue
    disabled and every later AirPrint job stuck."""
    base = queue_name(name)
    ppd_flag = "-P" if ppd.startswith("/") else "-m"
    with _add_lock:
        taken = _existing_queues()
        queue, description, n = base, name, 1
        while queue.lower() in taken:
            n += 1
            queue, description = f"{base}_{n}", f"{name} ({n})"
        _run([
            "lpadmin", "-p", queue, "-E", "-v", uri, ppd_flag, ppd,
            "-o", "printer-is-shared=true",
            "-o", "printer-error-policy=retry-job",
            "-D", description,
        ])
    try:
        # Redundant with the entrypoint; the queue exists either way, so a
        # failure here must not report the whole creation as failed.
        _run(["cupsctl", "--share-printers"])
    except CupsError:
        pass
    return queue


def delete_printer(name: str) -> None:
    if not _VALID_QUEUE_NAME.match(name):
        raise CupsError("invalid queue name")
    _run(["lpadmin", "-x", name])


def resume_printer(name: str) -> None:
    """Re-enable a queue that CUPS stopped after a device error."""
    if not _VALID_QUEUE_NAME.match(name):
        raise CupsError("invalid queue name")
    _run(["cupsenable", name])


def parse_avahi_queues(output: str) -> set[str]:
    """Queue names advertised in `avahi-browse -rpt _ipp._tcp` output.

    CUPS publishes each shared queue with a TXT record `rp=printers/<queue>`;
    in parsable mode TXT entries are space-separated quoted strings."""
    queues = set()
    for line in output.splitlines():
        parts = line.split(";", 9)
        if len(parts) == 10 and parts[0] == "=":
            queues.update(re.findall(r'"rp=printers/([^"]+)"', parts[9]))
    return queues


def is_advertised(name: str) -> bool:
    """Whether Avahi currently announces this queue over AirPrint (DNS-SD)."""
    if not _VALID_QUEUE_NAME.match(name):
        raise CupsError("invalid queue name")
    output = _run(["avahi-browse", "-rpt", "_ipp._tcp"], timeout=AVAHI_TIMEOUT).stdout
    return name in parse_avahi_queues(output)


def cancel_jobs(name: str) -> None:
    """Cancel every pending job on the queue (rescue for stuck queues)."""
    if not _VALID_QUEUE_NAME.match(name):
        raise CupsError("invalid queue name")
    _run(["cancel", "-a", name])


def print_test_page(name: str) -> None:
    if not _VALID_QUEUE_NAME.match(name):
        raise CupsError("invalid queue name")
    _run(["lp", "-d", name, TESTPRINT])
