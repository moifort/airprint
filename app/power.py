"""Switch printers' smart plugs on when a job arrives, off once idle.

Plugs are Zigbee2MQTT devices driven over MQTT (`mosquitto_pub`); Homebridge
picks the state change up, so HomeKit stays in sync. While a printer boots,
CUPS holds the job (retry-job policy) and sends it once the printer answers.

Off is only ever sent for a plug that had a job: a plug switched on by hand
without printing, or a container restart, never turns anything off. Jobs
that stay pending longer than POWER_MAX_ON (printer unreachable, job held)
switch the plug off anyway, until a new job arrives.
"""

import json
import logging
import re
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass

from . import cups_service

POLL_INTERVAL = 2
REPUBLISH_INTERVAL = 60
RETRY_DELAY = 10
PUBLISH_TIMEOUT = 10
DEFAULT_OFF_DELAY_MINUTES = 10
DEFAULT_MAX_ON_MINUTES = 60
BASE_TOPIC = "zigbee2mqtt"
DEFAULT_MQTT_URL = "mqtt://localhost:1883"

# Child of uvicorn's logger: inherits its handler and INFO level
log = logging.getLogger("uvicorn.error.power")


class PowerError(Exception):
    pass


class PowerConfigError(ValueError):
    pass


def queue_key(queue: str) -> str:
    """Normalised queue name for plug lookups.

    CUPS queue names are case-insensitive and cannot contain spaces, while
    the UI shows them with spaces for underscores: accept either form, so a
    name copied from a printer card still matches its queue."""
    return queue.strip().lower().replace(" ", "_")


def parse_plugs(value: str) -> dict[str, str]:
    """`POWER_PLUGS="<queue>=<plug>,…"` → {queue key: plug} (see queue_key).

    Plug names are Zigbee2MQTT friendly names, kept verbatim (they may
    contain spaces or slashes)."""
    plugs = {}
    for entry in filter(None, (e.strip() for e in value.split(","))):
        queue, sep, plug = (part.strip() for part in entry.partition("="))
        if not sep or not queue or not plug:
            raise PowerConfigError(f"invalid POWER_PLUGS entry {entry!r} (expected queue=plug)")
        if any(c in plug for c in "+#"):
            raise PowerConfigError(f"invalid plug name {plug!r} (MQTT wildcards)")
        plugs[queue_key(queue)] = plug
    return plugs


@dataclass(frozen=True)
class Broker:
    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = None


def parse_mqtt_url(url: str) -> Broker:
    """`mqtt://[user[:password]@]host[:port]`; credentials are optional and
    percent-decoded (a password may contain reserved characters)."""
    parts = urllib.parse.urlsplit(url.strip())
    try:
        port = parts.port
    except ValueError:
        port = -1
    if parts.scheme != "mqtt" or not parts.hostname or port == -1:
        raise PowerConfigError(
            f"invalid MQTT_URL {redact_url(url)!r} (expected mqtt://[user:password@]host:port)"
        )
    unquote = lambda v: urllib.parse.unquote(v) if v is not None else None  # noqa: E731
    return Broker(parts.hostname, port or 1883, unquote(parts.username), unquote(parts.password))


def redact_url(url: str) -> str:
    """MQTT_URL for logs: never print the password."""
    return re.sub(r"(//[^:/@]*):[^@/]*@", r"\1:***@", url)


def _parse_minutes(name: str, value: str, default: float) -> float:
    """A duration env var in minutes → seconds."""
    if not value.strip():
        return default * 60
    try:
        minutes = float(value)
    except ValueError:
        raise PowerConfigError(f"invalid {name} {value!r} (minutes)") from None
    if minutes < 0:
        raise PowerConfigError(f"{name} must not be negative")
    return minutes * 60


def parse_off_delay(value: str) -> float:
    """`POWER_OFF_DELAY` in minutes → seconds."""
    return _parse_minutes("POWER_OFF_DELAY", value, DEFAULT_OFF_DELAY_MINUTES)


def parse_max_on(value: str) -> float:
    """`POWER_MAX_ON` in minutes → seconds; 0 disables the cap."""
    return _parse_minutes("POWER_MAX_ON", value, DEFAULT_MAX_ON_MINUTES)


@dataclass
class _PlugState:
    last_on: float | None = None  # last ON sent while jobs are pending
    last_job: float | None = None  # armed for OFF once set
    busy_since: float | None = None  # start of the current run of pending jobs
    # Set when POWER_MAX_ON cut the plug: the lowest job count seen since.
    # Only a count above it (a new job) switches the plug on again.
    gave_up_at: int | None = None


class PowerController:
    """Pure state machine: job counts in, plug commands out. No I/O."""

    def __init__(self, plugs: dict[str, str], off_delay: float, max_on: float = 0):
        self._plugs = {queue_key(queue): plug for queue, plug in plugs.items()}
        self._off_delay = off_delay
        self._max_on = max_on
        self._state = {plug: _PlugState() for plug in self._plugs.values()}

    @property
    def max_on(self) -> float:
        return self._max_on

    def plug_for(self, queue: str) -> str | None:
        return self._plugs.get(queue_key(queue))

    def gave_up(self, plug: str) -> bool:
        """Whether the plug was switched off by POWER_MAX_ON with jobs still
        pending (they never printed: printer unreachable, job held…)."""
        state = self._state.get(plug)
        return state is not None and state.gave_up_at is not None

    def tick(self, job_counts: dict[str, int], now: float) -> list[tuple[str, str]]:
        busy = {plug: 0 for plug in self._state}
        for queue, count in job_counts.items():
            if plug := self.plug_for(queue):
                busy[plug] += count
        actions = []
        for plug, state in self._state.items():
            if busy[plug]:
                if state.gave_up_at is not None:
                    if busy[plug] <= state.gave_up_at:
                        # Same stuck jobs (or fewer): stay off
                        state.gave_up_at = busy[plug]
                        continue
                    # A new job came in: give the printer another chance
                    state.gave_up_at = None
                if state.busy_since is None:
                    state.busy_since = now
                if self._max_on and now - state.busy_since >= self._max_on:
                    # Jobs that never leave the queue would otherwise keep the
                    # printer powered forever
                    state.gave_up_at = busy[plug]
                    state.last_on = state.last_job = state.busy_since = None
                    actions.append((plug, "OFF"))
                    continue
                state.last_job = now
                if state.last_on is None or now - state.last_on >= REPUBLISH_INTERVAL:
                    state.last_on = now
                    actions.append((plug, "ON"))
            else:
                state.last_on = state.busy_since = state.gave_up_at = None
                if state.last_job is not None and now - state.last_job >= self._off_delay:
                    state.last_job = None
                    actions.append((plug, "OFF"))
        return actions

    def publish_failed(self, plug: str, command: str, now: float) -> None:
        """Schedule a retry RETRY_DELAY seconds from now."""
        state = self._state[plug]
        if command == "ON":
            state.last_on = now - REPUBLISH_INTERVAL + RETRY_DELAY
        elif state.gave_up_at is not None:
            # The POWER_MAX_ON cut-off did not go through: redo it
            state.gave_up_at = None
            state.busy_since = now - self._max_on + RETRY_DELAY
            state.last_on = now  # no ON republish before the retried OFF
        else:
            state.last_job = now - self._off_delay + RETRY_DELAY


def publish(broker: Broker, plug: str, command: str) -> None:
    cmd = [
        "mosquitto_pub", "-h", broker.host, "-p", str(broker.port), "-q", "1",
        "-t", f"{BASE_TOPIC}/{plug}/set", "-m", json.dumps({"state": command}),
    ]
    # Credentials go on the command line: only visible to processes inside
    # the container, which already run as root
    if broker.username is not None:
        cmd += ["-u", broker.username]
    if broker.password is not None:
        cmd += ["-P", broker.password]
    try:
        result = subprocess.run(
            cmd, capture_output=True, encoding="utf-8", errors="replace",
            timeout=PUBLISH_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise PowerError(f"mosquitto_pub timed out after {PUBLISH_TIMEOUT}s") from exc
    except FileNotFoundError as exc:
        raise PowerError("mosquitto_pub not found") from exc
    if result.returncode != 0:
        raise PowerError(result.stderr.strip() or "mosquitto_pub failed")


def job_counts() -> dict[str, int]:
    """Pending jobs per queue. Raises CupsError when lpstat fails."""
    return cups_service.parse_job_counts(cups_service._run(["lpstat", "-o"]).stdout)


class PowerManager:
    def __init__(self, controller: PowerController, broker: Broker):
        self.controller = controller
        self._broker = broker
        self._lpstat_failing = False

    @classmethod
    def from_env(cls, env) -> "PowerManager | None":
        """None when POWER_PLUGS is unset or the config is invalid (logged):
        a typo must not take printing down with it."""
        try:
            plugs = parse_plugs(env.get("POWER_PLUGS", ""))
            if not plugs:
                return None
            broker = parse_mqtt_url(env.get("MQTT_URL", "") or DEFAULT_MQTT_URL)
            off_delay = parse_off_delay(env.get("POWER_OFF_DELAY", ""))
            max_on = parse_max_on(env.get("POWER_MAX_ON", ""))
        except PowerConfigError as exc:
            log.error("auto power disabled: %s", exc)
            return None
        return cls(PowerController(plugs, off_delay, max_on), broker)

    def step(self, now: float) -> None:
        try:
            counts = job_counts()
        except cups_service.CupsError as exc:
            # Unknown is not "no jobs": skip rather than risk an OFF. Log once
            # per outage, not every POLL_INTERVAL.
            if not self._lpstat_failing:
                self._lpstat_failing = True
                log.warning("auto power: lpstat failed, pausing until it recovers: %s", exc)
            return
        if self._lpstat_failing:
            self._lpstat_failing = False
            log.info("auto power: lpstat recovered")
        for plug, command in self.controller.tick(counts, now):
            try:
                publish(self._broker, plug, command)
                if self.controller.gave_up(plug):
                    log.warning(
                        "auto power: %s → OFF, jobs pending for %g min without "
                        "printing; a new job switches it on again",
                        plug, self.controller.max_on / 60,
                    )
                else:
                    log.info("auto power: %s → %s", plug, command)
            except PowerError as exc:
                log.warning("auto power: %s → %s failed: %s", plug, command, exc)
                self.controller.publish_failed(plug, command, now)

    def _warn_unknown_queues(self) -> None:
        try:
            queues = cups_service._existing_queues()
        except cups_service.CupsError:
            return
        for queue in self.controller._plugs:
            if queue not in queues:
                log.warning("auto power: POWER_PLUGS names unknown queue %r", queue)

    def _loop(self) -> None:
        self._warn_unknown_queues()
        while True:
            try:
                self.step(time.monotonic())
            except Exception:
                log.exception("auto power: unexpected error")
            time.sleep(POLL_INTERVAL)

    def start(self) -> None:
        threading.Thread(target=self._loop, name="auto-power", daemon=True).start()
