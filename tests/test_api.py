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
        "uris": ["socket://192.168.1.50:9100"],
    })
    monkeypatch.setattr(cups_service, "list_drivers", lambda mm: [
        {"ppd": "drv:///hpcups.drv/hp-laserjet_1320.ppd", "name": "HP LaserJet 1320"},
    ])
    res = client.post("/api/detect", json={"ip": "192.168.1.50"})
    assert res.status_code == 200
    body = res.json()
    assert body["found"] is True
    assert body["drivers"][0]["ppd"] == "drv:///hpcups.drv/hp-laserjet_1320.ppd"


def test_detect_not_found_returns_empty_drivers(client, monkeypatch):
    monkeypatch.setattr(detect, "probe", lambda ip: {
        "found": False, "make_model": None, "uris": ["socket://10.0.0.9:9100"],
    })
    body = client.post("/api/detect", json={"ip": "10.0.0.9"}).json()
    assert body["found"] is False
    assert body["drivers"] == []


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

    def fake_add(name, uri, ppd, description=None):
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
        raise cups_service.CupsError("lpadmin: imprimante injoignable")

    monkeypatch.setattr(cups_service, "add_printer", boom)
    res = client.post("/api/printers", json={
        "name": "X", "uri": "socket://1.2.3.4:9100", "ppd": "everywhere",
    })
    assert res.status_code == 400
    assert "injoignable" in res.json()["detail"]


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
