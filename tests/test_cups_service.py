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


def fake_run(recorded, stdout="", returncode=0):
    def _fake(cmd, **kwargs):
        recorded.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")
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
