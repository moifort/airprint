"""AirPrint bridge API: printer detection, driver selection, queue management."""

import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import cups_service, detect

app = FastAPI(title="AirPrint Bridge")

UPLOADED_PPD_DIR = Path(tempfile.gettempdir()) / "airprint-ppds"


class DetectRequest(BaseModel):
    ip: str


class PrinterCreate(BaseModel):
    name: str
    uri: str
    ppd: str


def _cups_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except cups_service.CupsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _match_drivers(make_model: str | None, device_id: str | None) -> list:
    """Device-ID matching first (most reliable), make-and-model as fallback."""
    drivers = []
    if device_id:
        drivers = _cups_call(cups_service.list_drivers, device_id=device_id)
    if not drivers and make_model:
        drivers = _cups_call(cups_service.list_drivers, make_model=make_model)
    return drivers


@app.get("/api/scan")
def scan_network():
    try:
        return detect.scan()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"network scan failed: {exc}") from exc


@app.post("/api/detect")
def detect_printer(req: DetectRequest):
    result = detect.probe(req.ip.strip())
    result["drivers"] = (
        _match_drivers(result["make_model"], result.get("device_id"))
        if result["found"]
        else []
    )
    return result


@app.get("/api/drivers")
def search_drivers(q: str | None = None, device_id: str | None = None):
    if not (q and q.strip()) and not device_id:
        return []
    return _match_drivers(q.strip() if q else None, device_id)


@app.get("/api/printers")
def list_printers():
    return _cups_call(cups_service.list_printers)


@app.post("/api/printers", status_code=201)
def create_printer(printer: PrinterCreate):
    queue = _cups_call(
        cups_service.add_printer, printer.name, printer.uri, printer.ppd
    )
    return {"queue": queue}


@app.post("/api/printers/upload", status_code=201)
def create_printer_with_ppd(
    name: str = Form(...), uri: str = Form(...), ppd_file: UploadFile = File(...)
):
    UPLOADED_PPD_DIR.mkdir(parents=True, exist_ok=True)
    ppd_path = UPLOADED_PPD_DIR / f"{cups_service.queue_name(name)}.ppd"
    with ppd_path.open("wb") as out:
        shutil.copyfileobj(ppd_file.file, out)
    queue = _cups_call(cups_service.add_printer, name, uri, str(ppd_path))
    return {"queue": queue}


@app.delete("/api/printers/{name}", status_code=204)
def delete_printer(name: str):
    _cups_call(cups_service.delete_printer, name)


@app.post("/api/printers/{name}/test")
def print_test_page(name: str):
    _cups_call(cups_service.print_test_page, name)
    return {"status": "ok"}


app.mount(
    "/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="ui"
)
