import subprocess

import pytest

from app import cups_service, power


def make(plugs=None, delay=600):
    return power.PowerController(plugs or {"brother": "workshop_lower"}, off_delay=delay)


# --- config parsing ---

def test_parse_plugs():
    assert power.parse_plugs(" Brother_HL=workshop_lower , HP=desk plug ") == {
        "brother_hl": "workshop_lower",
        "hp": "desk plug",
    }


def test_parse_plugs_empty_disables():
    assert power.parse_plugs("") == {}
    assert power.parse_plugs("  ") == {}


@pytest.mark.parametrize("value", ["Brother", "=plug", "Brother=", "Brother=a/#", "Brother=a+b"])
def test_parse_plugs_rejects_invalid(value):
    with pytest.raises(power.PowerConfigError):
        power.parse_plugs(value)


def test_parse_mqtt_url():
    assert power.parse_mqtt_url("mqtt://192.168.1.199:1883") == ("192.168.1.199", 1883)
    assert power.parse_mqtt_url("mqtt://broker") == ("broker", 1883)


@pytest.mark.parametrize("value", ["http://broker", "broker:1883", "mqtt://"])
def test_parse_mqtt_url_rejects_invalid(value):
    with pytest.raises(power.PowerConfigError):
        power.parse_mqtt_url(value)


def test_parse_off_delay_minutes():
    assert power.parse_off_delay("") == 600
    assert power.parse_off_delay("2.5") == 150
    with pytest.raises(power.PowerConfigError):
        power.parse_off_delay("-1")
    with pytest.raises(power.PowerConfigError):
        power.parse_off_delay("soon")


# --- state machine ---

def test_job_switches_plug_on():
    c = make()
    assert c.tick({"Brother": 1}, now=0) == [("workshop_lower", "ON")]


def test_on_is_not_repeated_every_tick_but_republished():
    c = make()
    c.tick({"brother": 1}, now=0)
    assert c.tick({"brother": 1}, now=2) == []
    assert c.tick({"brother": 1}, now=60) == [("workshop_lower", "ON")]


def test_off_after_delay_once_queue_empty():
    c = make(delay=600)
    c.tick({"brother": 1}, now=0)
    c.tick({"brother": 1}, now=30)
    assert c.tick({}, now=32) == []
    assert c.tick({}, now=629) == []
    assert c.tick({}, now=630) == [("workshop_lower", "OFF")]
    assert c.tick({}, now=2000) == []


def test_new_job_resets_off_timer():
    c = make(delay=600)
    c.tick({"brother": 1}, now=0)
    c.tick({}, now=2)
    assert c.tick({"brother": 1}, now=500) == [("workshop_lower", "ON")]
    assert c.tick({}, now=900) == []
    assert c.tick({}, now=1100) == [("workshop_lower", "OFF")]


def test_never_turns_off_a_plug_without_seen_jobs():
    c = make(delay=0)
    assert c.tick({}, now=0) == []
    assert c.tick({}, now=10_000) == []


def test_other_queues_are_ignored():
    c = make()
    assert c.tick({"other": 3}, now=0) == []


def test_queues_sharing_a_plug_add_up():
    c = make({"a": "plug", "b": "plug"}, delay=100)
    assert c.tick({"a": 1}, now=0) == [("plug", "ON")]
    assert c.tick({"b": 1}, now=50) == []
    assert c.tick({}, now=120) == []
    assert c.tick({}, now=150) == [("plug", "OFF")]


def test_failed_on_is_retried_soon():
    c = make()
    c.tick({"brother": 1}, now=0)
    c.publish_failed("workshop_lower", "ON", now=0)
    assert c.tick({"brother": 1}, now=2) == []
    assert c.tick({"brother": 1}, now=10) == [("workshop_lower", "ON")]


def test_failed_off_is_retried_soon():
    c = make(delay=100)
    c.tick({"brother": 1}, now=0)
    assert c.tick({}, now=100) == [("workshop_lower", "OFF")]
    c.publish_failed("workshop_lower", "OFF", now=100)
    assert c.tick({}, now=102) == []
    assert c.tick({}, now=110) == [("workshop_lower", "OFF")]


def test_plug_for_queue_is_case_insensitive():
    c = make()
    assert c.plug_for("BROTHER") == "workshop_lower"
    assert c.plug_for("other") is None


# --- MQTT publish ---

def test_publish_runs_mosquitto_pub(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    power.publish("broker", 1884, "workshop_lower", "ON")
    assert calls == [[
        "mosquitto_pub", "-h", "broker", "-p", "1884", "-q", "1",
        "-t", "zigbee2mqtt/workshop_lower/set", "-m", '{"state": "ON"}',
    ]]


def test_publish_failure_raises(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 5, "", "Connection refused"),
    )
    with pytest.raises(power.PowerError, match="Connection refused"):
        power.publish("broker", 1883, "plug", "OFF")


# --- manager wiring ---

def test_manager_from_env_disabled_without_plugs():
    assert power.PowerManager.from_env({}) is None


def test_manager_from_env_invalid_config_disables():
    assert power.PowerManager.from_env({"POWER_PLUGS": "nonsense"}) is None


def test_manager_step_publishes_and_skips_failed_lpstat(monkeypatch):
    manager = power.PowerManager.from_env(
        {"POWER_PLUGS": "brother=workshop_lower", "MQTT_URL": "mqtt://b:1883"}
    )
    sent = []
    monkeypatch.setattr(power, "publish", lambda host, port, plug, state: sent.append((host, port, plug, state)))

    monkeypatch.setattr(power, "job_counts", lambda: {"brother": 1})
    manager.step(now=0)
    assert sent == [("b", 1883, "workshop_lower", "ON")]

    def failing():
        raise cups_service.CupsError("cupsd down")

    monkeypatch.setattr(power, "job_counts", failing)
    manager.step(now=10_000)  # must not be read as "no jobs"
    assert len(sent) == 1


def test_manager_step_reports_publish_failure(monkeypatch):
    manager = power.PowerManager.from_env({"POWER_PLUGS": "brother=plug"})
    monkeypatch.setattr(power, "job_counts", lambda: {"brother": 1})

    def failing(*args):
        raise power.PowerError("down")

    monkeypatch.setattr(power, "publish", failing)
    manager.step(now=0)
    sent = []
    monkeypatch.setattr(power, "publish", lambda *a: sent.append(a))
    manager.step(now=10)
    assert sent == [("localhost", 1883, "plug", "ON")]
