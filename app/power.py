"""Switch printers' smart plugs on when a job arrives, off once idle.

Plugs are Zigbee2MQTT devices driven over MQTT (`mosquitto_pub`); Homebridge
picks the state change up, so HomeKit stays in sync. While a printer boots,
CUPS holds the job (retry-job policy) and sends it once the printer answers.

Off is only ever sent for a plug that had a job: a plug switched on by hand
without printing, or a container restart, never turns anything off.
"""

import json
import logging
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
BASE_TOPIC = "zigbee2mqtt"
DEFAULT_MQTT_URL = "mqtt://localhost:1883"

# Child of uvicorn's logger: inherits its handler and INFO level
log = logging.getLogger("uvicorn.error.power")


class PowerError(Exception):
    pass


class PowerConfigError(ValueError):
    pass


def parse_plugs(value: str) -> dict[str, str]:
    """`POWER_PLUGS="<queue>=<plug>,…"` → {queue (lowercase): plug}.

    CUPS queue names are case-insensitive; plug names are Zigbee2MQTT
    friendly names, kept verbatim (they may contain spaces or slashes)."""
    plugs = {}
    for entry in filter(None, (e.strip() for e in value.split(","))):
        queue, sep, plug = (part.strip() for part in entry.partition("="))
        if not sep or not queue or not plug:
            raise PowerConfigError(f"invalid POWER_PLUGS entry {entry!r} (expected queue=plug)")
        if any(c in plug for c in "+#"):
            raise PowerConfigError(f"invalid plug name {plug!r} (MQTT wildcards)")
        plugs[queue.lower()] = plug
    return plugs


def parse_mqtt_url(url: str) -> tuple[str, int]:
    parts = urllib.parse.urlsplit(url.strip())
    if parts.scheme != "mqtt" or not parts.hostname:
        raise PowerConfigError(f"invalid MQTT_URL {url!r} (expected mqtt://host:port)")
    return parts.hostname, parts.port or 1883


def parse_off_delay(value: str) -> float:
    """`POWER_OFF_DELAY` in minutes → seconds."""
    if not value.strip():
        return DEFAULT_OFF_DELAY_MINUTES * 60
    try:
        minutes = float(value)
    except ValueError:
        raise PowerConfigError(f"invalid POWER_OFF_DELAY {value!r} (minutes)") from None
    if minutes < 0:
        raise PowerConfigError("POWER_OFF_DELAY must not be negative")
    return minutes * 60


@dataclass
class _PlugState:
    last_on: float | None = None  # last ON sent while jobs are pending
    last_job: float | None = None  # armed for OFF once set


class PowerController:
    """Pure state machine: job counts in, plug commands out. No I/O."""

    def __init__(self, plugs: dict[str, str], off_delay: float):
        self._plugs = {queue.lower(): plug for queue, plug in plugs.items()}
        self._off_delay = off_delay
        self._state = {plug: _PlugState() for plug in self._plugs.values()}

    def plug_for(self, queue: str) -> str | None:
        return self._plugs.get(queue.lower())

    def tick(self, job_counts: dict[str, int], now: float) -> list[tuple[str, str]]:
        busy = {plug: 0 for plug in self._state}
        for queue, count in job_counts.items():
            if plug := self.plug_for(queue):
                busy[plug] += count
        actions = []
        for plug, state in self._state.items():
            if busy[plug]:
                state.last_job = now
                if state.last_on is None or now - state.last_on >= REPUBLISH_INTERVAL:
                    state.last_on = now
                    actions.append((plug, "ON"))
            else:
                state.last_on = None
                if state.last_job is not None and now - state.last_job >= self._off_delay:
                    state.last_job = None
                    actions.append((plug, "OFF"))
        return actions

    def publish_failed(self, plug: str, command: str, now: float) -> None:
        """Schedule a retry RETRY_DELAY seconds from now."""
        state = self._state[plug]
        if command == "ON":
            state.last_on = now - REPUBLISH_INTERVAL + RETRY_DELAY
        else:
            state.last_job = now - self._off_delay + RETRY_DELAY


def publish(host: str, port: int, plug: str, command: str) -> None:
    cmd = [
        "mosquitto_pub", "-h", host, "-p", str(port), "-q", "1",
        "-t", f"{BASE_TOPIC}/{plug}/set", "-m", json.dumps({"state": command}),
    ]
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
    def __init__(self, controller: PowerController, host: str, port: int):
        self.controller = controller
        self._host, self._port = host, port

    @classmethod
    def from_env(cls, env) -> "PowerManager | None":
        """None when POWER_PLUGS is unset or the config is invalid (logged):
        a typo must not take printing down with it."""
        try:
            plugs = parse_plugs(env.get("POWER_PLUGS", ""))
            if not plugs:
                return None
            host, port = parse_mqtt_url(env.get("MQTT_URL", "") or DEFAULT_MQTT_URL)
            off_delay = parse_off_delay(env.get("POWER_OFF_DELAY", ""))
        except PowerConfigError as exc:
            log.error("auto power disabled: %s", exc)
            return None
        return cls(PowerController(plugs, off_delay), host, port)

    def step(self, now: float) -> None:
        try:
            counts = job_counts()
        except cups_service.CupsError as exc:
            # Unknown is not "no jobs": skip rather than risk an OFF
            log.warning("auto power: lpstat failed: %s", exc)
            return
        for plug, command in self.controller.tick(counts, now):
            try:
                publish(self._host, self._port, plug, command)
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
