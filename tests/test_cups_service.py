import subprocess

import pytest

from app import cups_service

LPINFO_OUTPUT = """\
drv:///hpcups.drv/hp-laserjet_1320.ppd HP LaserJet 1320, hpcups 3.22.10
everywhere IPP Everywhere
"""

LPSTAT_P = """\
printer Bureau is idle.  enabled since Thu 12 Jun 2026
printer Salon disabled since Thu 12 Jun 2026 -
"""

LPSTAT_V = """\
device for Bureau: socket://192.168.1.50:9100
device for Salon: ipp://192.168.1.51/ipp/print
"""


def fake_run(recorded, stdout="", returncode=0, stderr=""):
    def _fake(cmd, **kwargs):
        recorded.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)
    return _fake


def test_parse_lpinfo_output():
    drivers = cups_service.parse_lpinfo_output(LPINFO_OUTPUT)
    assert drivers == [
        {"ppd": "drv:///hpcups.drv/hp-laserjet_1320.ppd",
         "name": "HP LaserJet 1320, hpcups 3.22.10"},
        {"ppd": "everywhere", "name": "IPP Everywhere"},
    ]


def test_parse_lpstat():
    printers = cups_service.parse_lpstat(LPSTAT_P, LPSTAT_V)
    assert printers == [
        {"name": "Bureau", "state": "idle", "uri": "socket://192.168.1.50:9100"},
        {"name": "Salon", "state": "disabled", "uri": "ipp://192.168.1.51/ipp/print"},
    ]


def test_queue_name_sanitizes():
    assert cups_service.queue_name("Imprimante Bureau") == "Imprimante_Bureau"
    assert cups_service.queue_name("HP (étage) #2") == "HP_tage_2"
    with pytest.raises(cups_service.CupsError):
        cups_service.queue_name("///")


def test_list_drivers_prefers_device_id(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", fake_run(calls, stdout=LPINFO_OUTPUT))
    cups_service.list_drivers(make_model="HP LaserJet 1320", device_id="MFG:HP;MDL:LaserJet 1320;")
    assert calls[0] == ["lpinfo", "--device-id", "MFG:HP;MDL:LaserJet 1320;", "-m"]


def test_list_drivers_requires_criteria():
    with pytest.raises(cups_service.CupsError):
        cups_service.list_drivers()


def test_list_drivers_no_match_returns_empty_list(monkeypatch):
    # Real-world case: lpinfo exits 1 with client-error-not-found when no
    # driver matches (e.g. Brother HL-1210W) — that is not an error
    monkeypatch.setattr(subprocess, "run", fake_run(
        [], returncode=1, stderr="lpinfo: client-error-not-found"
    ))
    assert cups_service.list_drivers(make_model="Brother HL-1210W series") == []


FULL_LPINFO_OUTPUT = """\
drv:///brlaser.drv/br1200.ppd Brother HL-1200 series, using brlaser v6
drv:///brlaser.drv/br2030.ppd Brother HL-2030 series, using brlaser v6
foomatic:Brother-HL-1250-hl1250.ppd Brother HL-1250 Foomatic/hl1250 (recommended)
drv:///hpcups.drv/hp-laserjet_1320.ppd HP LaserJet 1320, hpcups 3.22.10
everywhere IPP Everywhere
"""


def test_fuzzy_match_finds_family_driver(monkeypatch):
    monkeypatch.setattr(subprocess, "run", fake_run([], stdout=FULL_LPINFO_OUTPUT))
    drivers = cups_service.fuzzy_match_drivers("Brother HL-1210W series")
    ppds = [d["ppd"] for d in drivers]
    assert "drv:///brlaser.drv/br1200.ppd" in ppds
    assert "drv:///hpcups.drv/hp-laserjet_1320.ppd" not in ppds
    assert "everywhere" not in ppds


def test_add_printer_with_model(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", fake_run(calls))
    queue = cups_service.add_printer(
        "Imprimante Bureau", "socket://192.168.1.50:9100",
        "drv:///hpcups.drv/hp-laserjet_1320.ppd",
    )
    assert queue == "Imprimante_Bureau"
    lpadmin = calls[0]
    assert lpadmin[:3] == ["lpadmin", "-p", "Imprimante_Bureau"]
    assert "-m" in lpadmin and "drv:///hpcups.drv/hp-laserjet_1320.ppd" in lpadmin
    assert "printer-is-shared=true" in lpadmin
    assert calls[1] == ["cupsctl", "--share-printers"]


def test_add_printer_with_ppd_file_uses_capital_p(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", fake_run(calls))
    cups_service.add_printer("Salon", "ipp://192.168.1.51/ipp/print", "/tmp/x.ppd")
    assert "-P" in calls[0] and "/tmp/x.ppd" in calls[0]
    assert "-m" not in calls[0]


def test_add_printer_raises_on_failure(monkeypatch):
    monkeypatch.setattr(subprocess, "run", fake_run([], returncode=1))
    with pytest.raises(cups_service.CupsError):
        cups_service.add_printer("X", "socket://1.2.3.4:9100", "everywhere")


def test_delete_printer_rejects_bad_name():
    with pytest.raises(cups_service.CupsError):
        cups_service.delete_printer("foo; rm -rf /")
