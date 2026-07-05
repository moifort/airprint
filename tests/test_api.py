from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import cups_service, detect, main


@pytest.fixture
def client():
    return TestClient(main.app)


def test_detect_returns_drivers_when_found(client, monkeypatch):
    monkeypatch.setattr(detect, "probe", lambda ip: {
        "found": True,
        "make_model": "HP LaserJet 1320",
        "device_id": None,
        "uris": ["socket://192.168.1.50:9100"],
    })
    monkeypatch.setattr(cups_service, "list_drivers", lambda **kw: [
        {"ppd": "drv:///hpcups.drv/hp-laserjet_1320.ppd", "name": "HP LaserJet 1320"},
    ])
    res = client.post("/api/detect", json={"ip": "192.168.1.50"})
    assert res.status_code == 200
    body = res.json()
    assert body["found"] is True
    assert body["drivers"][0]["ppd"] == "drv:///hpcups.drv/hp-laserjet_1320.ppd"


def test_detect_not_found_returns_empty_drivers(client, monkeypatch):
    monkeypatch.setattr(detect, "probe", lambda ip: {
        "found": False, "make_model": None, "device_id": None,
        "uris": ["socket://10.0.0.9:9100"],
    })
    body = client.post("/api/detect", json={"ip": "10.0.0.9"}).json()
    assert body["found"] is False
    assert body["drivers"] == []


def test_scan_endpoint(client, monkeypatch):
    monkeypatch.setattr(detect, "scan", lambda: [
        {"uri": "socket://192.168.1.50:9100", "ip": "192.168.1.50",
         "make_model": "HP LaserJet 1320", "device_id": None,
         "uris": ["socket://192.168.1.50:9100"]},
    ])
    res = client.get("/api/scan")
    assert res.status_code == 200
    assert res.json()[0]["ip"] == "192.168.1.50"


def test_drivers_fall_back_to_make_model_when_device_id_matches_nothing(client, monkeypatch):
    calls = []

    def fake_list_drivers(make_model=None, device_id=None):
        calls.append({"make_model": make_model, "device_id": device_id})
        return [] if device_id else [{"ppd": "everywhere", "name": "IPP Everywhere"}]

    monkeypatch.setattr(cups_service, "list_drivers", fake_list_drivers)
    res = client.get("/api/drivers", params={"q": "HP X", "device_id": "MFG:HP;"})
    assert res.status_code == 200
    assert res.json() == [{"ppd": "everywhere", "name": "IPP Everywhere"}]
    assert calls == [
        {"make_model": None, "device_id": "MFG:HP;"},
        {"make_model": "HP X", "device_id": None},
    ]


def test_list_printers(client, monkeypatch):
    monkeypatch.setattr(cups_service, "list_printers", lambda: [
        {"name": "Bureau", "state": "idle",
         "uri": "socket://192.168.1.50:9100", "make_model": "HP LaserJet 1320"},
    ])
    res = client.get("/api/printers")
    assert res.status_code == 200
    assert res.json()[0]["name"] == "Bureau"


def test_create_printer(client, monkeypatch):
    received = {}

    def fake_add(name, uri, ppd):
        received.update(name=name, uri=uri, ppd=ppd)
        return "Bureau"

    monkeypatch.setattr(cups_service, "add_printer", fake_add)
    res = client.post("/api/printers", json={
        "name": "Bureau", "uri": "socket://192.168.1.50:9100", "ppd": "everywhere",
    })
    assert res.status_code == 201
    assert res.json() == {"queue": "Bureau"}
    assert received == {
        "name": "Bureau", "uri": "socket://192.168.1.50:9100", "ppd": "everywhere",
    }


def test_create_printer_cups_error_becomes_400(client, monkeypatch):
    def boom(*args, **kwargs):
        raise cups_service.CupsError("lpadmin: printer unreachable")

    monkeypatch.setattr(cups_service, "add_printer", boom)
    res = client.post("/api/printers", json={
        "name": "X", "uri": "socket://1.2.3.4:9100", "ppd": "everywhere",
    })
    assert res.status_code == 400
    assert "unreachable" in res.json()["detail"]


def test_delete_printer(client, monkeypatch):
    deleted = []
    monkeypatch.setattr(cups_service, "delete_printer", deleted.append)
    res = client.delete("/api/printers/Bureau")
    assert res.status_code == 204
    assert deleted == ["Bureau"]


def test_ui_served_at_root(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "AirPrint Bridge" in res.text


def test_clear_jobs(client, monkeypatch):
    cleared = []
    monkeypatch.setattr(cups_service, "cancel_jobs", cleared.append)
    res = client.delete("/api/printers/Atelier/jobs")
    assert res.status_code == 204
    assert cleared == ["Atelier"]


def test_print_test_page(client, monkeypatch):
    printed = []
    monkeypatch.setattr(cups_service, "print_test_page", printed.append)
    res = client.post("/api/printers/Bureau/test")
    assert res.status_code == 200
    assert printed == ["Bureau"]


def test_detect_rejects_invalid_ip(client):
    # a hostname (or URL fragment) must not reach ipptool: SSRF vector
    res = client.post("/api/detect", json={"ip": "printer.evil.example/x#"})
    assert res.status_code == 422


def test_detect_probe_failure_becomes_502(client, monkeypatch):
    def boom(ip):
        raise RuntimeError("snmp backend exploded")

    monkeypatch.setattr(detect, "probe", boom)
    res = client.post("/api/detect", json={"ip": "192.168.1.50"})
    assert res.status_code == 502


def test_drivers_without_criteria_is_422(client):
    assert client.get("/api/drivers").status_code == 422


def test_create_printer_rejects_ppd_path(client):
    # file paths are reserved for the upload route (arbitrary-path read via
    # lpadmin -P otherwise)
    res = client.post("/api/printers", json={
        "name": "X", "uri": "socket://1.2.3.4:9100", "ppd": "/etc/shadow",
    })
    assert res.status_code == 422


def test_create_printer_rejects_unknown_uri_scheme(client):
    res = client.post("/api/printers", json={
        "name": "X", "uri": "file:///etc/passwd", "ppd": "everywhere",
    })
    assert res.status_code == 422


def test_create_printer_timeout_becomes_504(client, monkeypatch):
    def slow(*args, **kwargs):
        raise cups_service.CupsTimeout("lpadmin timed out after 60s")

    monkeypatch.setattr(cups_service, "add_printer", slow)
    res = client.post("/api/printers", json={
        "name": "X", "uri": "socket://1.2.3.4:9100", "ppd": "everywhere",
    })
    assert res.status_code == 504


def test_upload_ppd_creates_queue_and_cleans_up(client, monkeypatch, tmp_path):
    received = {}

    def fake_add(name, uri, ppd):
        received.update(name=name, uri=uri, ppd=ppd)
        assert Path(ppd).read_bytes().startswith(b"*PPD-Adobe")
        return "Salon"

    monkeypatch.setattr(cups_service, "add_printer", fake_add)
    monkeypatch.setattr(main, "UPLOADED_PPD_DIR", tmp_path)
    res = client.post(
        "/api/printers/upload",
        data={"name": "Salon", "uri": "ipp://192.168.1.51/ipp/print"},
        files={"ppd_file": ("x.ppd", b'*PPD-Adobe: "4.3"\n*NickName: "X"\n')},
    )
    assert res.status_code == 201
    assert res.json() == {"queue": "Salon"}
    assert received["ppd"].endswith("Salon.ppd")
    # lpadmin copies the PPD into CUPS; the temp file must not accumulate
    assert list(tmp_path.iterdir()) == []


def test_upload_rejects_non_ppd_content(client, monkeypatch, tmp_path):
    monkeypatch.setattr(main, "UPLOADED_PPD_DIR", tmp_path)
    res = client.post(
        "/api/printers/upload",
        data={"name": "X", "uri": "socket://1.2.3.4:9100"},
        files={"ppd_file": ("x.ppd", b"MZ\x90\x00binary")},
    )
    assert res.status_code == 400
    assert list(tmp_path.iterdir()) == []


def test_upload_rejects_oversized_ppd(client, monkeypatch, tmp_path):
    monkeypatch.setattr(main, "UPLOADED_PPD_DIR", tmp_path)
    big = b'*PPD-Adobe: "4.3"\n' + b"x" * main.MAX_PPD_SIZE
    res = client.post(
        "/api/printers/upload",
        data={"name": "X", "uri": "socket://1.2.3.4:9100"},
        files={"ppd_file": ("x.ppd", big)},
    )
    assert res.status_code == 413
    assert list(tmp_path.iterdir()) == []
