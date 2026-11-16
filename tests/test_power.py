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
    assert power.parse_mqtt_url("mqtt://192.168.1.199:1883") == power.Broker("192.168.1.199", 1883)
    assert power.parse_mqtt_url("mqtt://broker") == power.Broker("broker", 1883)


def test_parse_mqtt_url_with_credentials():
    assert power.parse_mqtt_url("mqtt://z2m:p%40ss%3Aw@broker:1884") == power.Broker(
        "broker", 1884, "z2m", "p@ss:w"
    )
    assert power.parse_mqtt_url("mqtt://z2m@broker") == power.Broker("broker", 1883, "z2m", None)


def test_invalid_mqtt_url_error_hides_the_password():
    with pytest.raises(power.PowerConfigError) as exc:
        power.parse_mqtt_url("mqtt://z2m:secret@broker:notaport")
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize("value", ["http://broker", "broker:1883", "mqtt://", "mqtt://b:99999"])
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
    power.publish(power.Broker("broker", 1884), "workshop_lower", "ON")
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
        power.publish(power.Broker("broker"), "plug", "OFF")


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
    monkeypatch.setattr(power, "publish", lambda broker, plug, state: sent.append((broker.host, broker.port, plug, state)))

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
    assert sent == [(power.Broker("localhost", 1883), "plug", "ON")]


def test_parse_plugs_accepts_queue_names_with_spaces():
    # The printer card shows "Brother HL-1210W series" for the queue
    # Brother_HL-1210W_series; CUPS queue names cannot contain spaces
    assert power.parse_plugs("Brother HL-1210W series=Atelier Plug") == {
        "brother_hl-1210w_series": "Atelier Plug",
    }


def test_plug_for_treats_spaces_and_underscores_alike():
    c = make({"Brother HL-1210W series": "plug"})
    assert c.plug_for("Brother_HL-1210W_series") == "plug"
    c = make({"Brother_HL-1210W_series": "plug"})
    assert c.plug_for("brother hl-1210w series") == "plug"
    assert c.tick({"Brother_HL-1210W_series": 1}, now=0) == [("plug", "ON")]


def test_manager_logs_lpstat_failure_once_then_recovery(monkeypatch, caplog):
    manager = power.PowerManager.from_env({"POWER_PLUGS": "brother=plug"})
    monkeypatch.setattr(power, "publish", lambda *a: None)

    def failing():
        raise cups_service.CupsError("cupsd down")

    monkeypatch.setattr(power, "job_counts", failing)
    with caplog.at_level("INFO", logger=power.log.name):
        for now in range(0, 20, 2):
            manager.step(now=now)
        failures = [r for r in caplog.records if "lpstat failed" in r.message]
        assert len(failures) == 1 and failures[0].levelname == "WARNING"

        monkeypatch.setattr(power, "job_counts", lambda: {})
        manager.step(now=20)
        manager.step(now=22)
        recovered = [r for r in caplog.records if "lpstat recovered" in r.message]
        assert len(recovered) == 1 and recovered[0].levelname == "INFO"

        monkeypatch.setattr(power, "job_counts", failing)
        manager.step(now=24)
        assert len([r for r in caplog.records if "lpstat failed" in r.message]) == 2


def test_warn_unknown_queues_accepts_card_names(monkeypatch, caplog):
    manager = power.PowerManager.from_env({"POWER_PLUGS": "Brother HL-1210W series=plug,Ghost=x"})
    monkeypatch.setattr(cups_service, "_existing_queues", lambda: {"brother_hl-1210w_series"})
    with caplog.at_level("WARNING", logger=power.log.name):
        manager._warn_unknown_queues()
    unknown = [r.message for r in caplog.records if "unknown queue" in r.message]
    assert unknown == ["auto power: POWER_PLUGS names unknown queue 'ghost'"]


def test_publish_passes_mqtt_credentials(monkeypatch):
    calls = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""),
    )
    power.publish(power.Broker("broker", 1883, "z2m", "secret"), "plug", "ON")
    assert calls[0][-4:] == ["-u", "z2m", "-P", "secret"]


# --- POWER_MAX_ON ---

def test_parse_max_on_minutes():
    assert power.parse_max_on("") == 3600
    assert power.parse_max_on("0") == 0
    with pytest.raises(power.PowerConfigError):
        power.parse_max_on("-5")


def test_stuck_jobs_switch_plug_off_after_max_on():
    c = power.PowerController({"brother": "plug"}, off_delay=600, max_on=3600)
    assert c.tick({"brother": 1}, now=0) == [("plug", "ON")]
    assert c.tick({"brother": 1}, now=3599) == [("plug", "ON")]  # periodic republish
    assert c.tick({"brother": 1}, now=3600) == [("plug", "OFF")]
    assert c.gave_up("plug")
    # The same stuck job neither switches it on again nor republishes
    assert c.tick({"brother": 1}, now=3700) == []
    assert c.tick({"brother": 1}, now=99999) == []


def test_new_job_after_max_on_switches_plug_on_again():
    c = power.PowerController({"brother": "plug"}, off_delay=600, max_on=3600)
    c.tick({"brother": 2}, now=0)
    c.tick({"brother": 2}, now=3600)
    # One stuck job cancelled, then a new one: count back to 2, above the low
    assert c.tick({"brother": 1}, now=3700) == []
    assert c.tick({"brother": 2}, now=3800) == [("plug", "ON")]
    assert not c.gave_up("plug")


def test_empty_queue_after_max_on_sends_no_second_off():
    c = power.PowerController({"brother": "plug"}, off_delay=60, max_on=600)
    c.tick({"brother": 1}, now=0)
    assert c.tick({"brother": 1}, now=600) == [("plug", "OFF")]
    assert c.tick({}, now=700) == []
    assert c.tick({}, now=2000) == []
    assert not c.gave_up("plug")
    assert c.tick({"brother": 1}, now=2100) == [("plug", "ON")]


def test_max_on_zero_disables_the_cap():
    c = power.PowerController({"brother": "plug"}, off_delay=600, max_on=0)
    c.tick({"brother": 1}, now=0)
    assert c.tick({"brother": 1}, now=10**6) == [("plug", "ON")]


def test_failed_max_on_off_is_retried_soon():
    c = power.PowerController({"brother": "plug"}, off_delay=600, max_on=3600)
    c.tick({"brother": 1}, now=0)
    assert c.tick({"brother": 1}, now=3600) == [("plug", "OFF")]
    c.publish_failed("plug", "OFF", now=3600)
    assert c.tick({"brother": 1}, now=3602) == []
    assert c.tick({"brother": 1}, now=3610) == [("plug", "OFF")]


def test_list_printers_reports_power_gave_up(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main

    manager = power.PowerManager(
        power.PowerController({"brother": "plug"}, off_delay=600, max_on=60), power.Broker("b")
    )
    manager.controller.tick({"brother": 1}, now=0)
    manager.controller.tick({"brother": 1}, now=60)
    monkeypatch.setattr(main, "power_manager", manager)
    monkeypatch.setattr(cups_service, "list_printers", lambda: [
        {"name": "Brother", "state": "idle", "uri": "socket://1.2.3.4:9100"},
        {"name": "Other", "state": "idle", "uri": "socket://1.2.3.5:9100"},
    ])
    body = TestClient(main.app).get("/api/printers").json()
    assert [(p["power_plug"], p["power_gave_up"]) for p in body] == [("plug", True), (None, False)]
