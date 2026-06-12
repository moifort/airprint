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


@app.post("/api/detect")
def detect_printer(req: DetectRequest):
    result = detect.probe(req.ip.strip())
    result["drivers"] = (
        _cups_call(cups_service.list_drivers, result["make_model"])
        if result["found"]
        else []
    )
    return result


@app.get("/api/drivers")
def search_drivers(q: str):
    if not q.strip():
        return []
    return _cups_call(cups_service.list_drivers, q.strip())


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
