"""AirPrint bridge API: printer detection, driver selection, queue management."""

import ipaddress
import tempfile
import threading
import urllib.parse
from concurrent.futures import Future
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import cups_service, detect

app = FastAPI(title="AirPrint Bridge")

UPLOADED_PPD_DIR = Path(tempfile.gettempdir()) / "airprint-ppds"
MAX_PPD_SIZE = 2 * 1024 * 1024  # PPDs are tens of KB; 2 MB is generous
PPD_MAGIC = b"*PPD-Adobe"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@app.middleware("http")
async def reject_cross_site_requests(request: Request, call_next):
    """CSRF guard. Form posts and body-less POSTs skip the CORS preflight, so
    without this any web page open on the LAN could create queues or print.
    Browsers always send Origin on cross-origin writes; clients without one
    (curl, scripts) are not browsers and carry no ambient authority."""
    origin = request.headers.get("origin")
    if request.method not in SAFE_METHODS and origin:
        if urllib.parse.urlsplit(origin).netloc != request.headers.get("host"):
            return JSONResponse(
                {"detail": "cross-origin request rejected"}, status_code=403
            )
    return await call_next(request)


def _check_uri(uri: str) -> str:
    uri = uri.strip()
    if not uri.startswith(detect.DISCOVERY_SCHEMES):
        raise ValueError(
            "uri must use one of: " + ", ".join(detect.DISCOVERY_SCHEMES)
        )
    return uri


class DetectRequest(BaseModel):
    ip: str

    @field_validator("ip")
    @classmethod
    def _valid_ip(cls, value: str) -> str:
        value = value.strip()
        try:
            ipaddress.ip_address(value)
        except ValueError:
            raise ValueError("ip must be a valid IPv4 or IPv6 address") from None
        return value


class PrinterCreate(BaseModel):
    name: str = Field(min_length=1)
    uri: str
    ppd: str = Field(min_length=1)

    @field_validator("uri")
    @classmethod
    def _valid_uri(cls, value: str) -> str:
        return _check_uri(value)

    @field_validator("ppd")
    @classmethod
    def _model_only(cls, value: str) -> str:
        # File paths are reserved for the upload route; the JSON route only
        # accepts lpinfo model names, never local filesystem paths.
        if value.startswith("/"):
            raise ValueError("ppd must be a driver model name, not a file path")
        return value


def _cups_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except cups_service.CupsTimeout as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except cups_service.CupsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _match_drivers(make_model: str | None, device_id: str | None) -> list:
    """Device-ID matching first (most reliable), then make-and-model, then a
    fuzzy search that catches family drivers (e.g. HL-1210W → HL-1200 series)."""
    drivers = []
    if device_id:
        drivers = _cups_call(cups_service.list_drivers, device_id=device_id)
    if not drivers and make_model:
        drivers = _cups_call(cups_service.list_drivers, make_model=make_model)
    if not drivers and make_model:
        drivers = _cups_call(cups_service.fuzzy_match_drivers, make_model)
    if drivers and make_model:
        drivers = cups_service.rank_drivers(drivers, make_model)
    return drivers


# A network scan holds a worker thread for up to 45 s; when several clients
# ask at once (multiple tabs), share a single underlying scan between them.
_scan_lock = threading.Lock()
_scan_future: Future | None = None


def _shared_scan() -> list:
    global _scan_future
    with _scan_lock:
        joined = _scan_future
        if joined is None:
            future = _scan_future = Future()
    if joined is not None:
        return joined.result()
    try:
        result = detect.scan()
        future.set_result(result)
        return result
    except BaseException as exc:
        future.set_exception(exc)
        raise
    finally:
        with _scan_lock:
            _scan_future = None


@app.get("/api/scan")
def scan_network():
    try:
        return _shared_scan()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"network scan failed: {exc}") from exc


@app.post("/api/detect")
def detect_printer(req: DetectRequest):
    try:
        result = detect.probe(req.ip)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"printer probe failed: {exc}") from exc
    result["drivers"] = (
        _match_drivers(result["make_model"], result.get("device_id"))
        if result["found"]
        else []
    )
    return result


@app.get("/api/drivers")
def search_drivers(q: str | None = None, device_id: str | None = None):
    if not (q and q.strip()) and not device_id:
        raise HTTPException(status_code=422, detail="q or device_id is required")
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
    try:
        uri = _check_uri(uri)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _cups_call(cups_service.queue_name, name)  # reject unusable names before I/O
    UPLOADED_PPD_DIR.mkdir(parents=True, exist_ok=True)
    # A unique file per request: concurrent uploads must not share a path
    with tempfile.NamedTemporaryFile(
        dir=UPLOADED_PPD_DIR, suffix=".ppd", delete=False
    ) as tmp:
        ppd_path = Path(tmp.name)
    try:
        with ppd_path.open("wb") as out:
            size = 0
            while chunk := ppd_file.file.read(64 * 1024):
                if size == 0 and not chunk.startswith(PPD_MAGIC):
                    raise HTTPException(
                        status_code=400,
                        detail="not a PPD file (must start with *PPD-Adobe)",
                    )
                size += len(chunk)
                if size > MAX_PPD_SIZE:
                    raise HTTPException(
                        status_code=413, detail="PPD file too large (2 MB max)"
                    )
                out.write(chunk)
            if size == 0:
                raise HTTPException(status_code=400, detail="empty PPD file")
        queue = _cups_call(cups_service.add_printer, name, uri, str(ppd_path))
    finally:
        # lpadmin copies the PPD into /etc/cups/ppd; the temp file is disposable
        ppd_path.unlink(missing_ok=True)
    return {"queue": queue}


@app.delete("/api/printers/{name}", status_code=204)
def delete_printer(name: str):
    _cups_call(cups_service.delete_printer, name)


@app.post("/api/printers/{name}/resume", status_code=204)
def resume_printer(name: str):
    _cups_call(cups_service.resume_printer, name)


@app.get("/api/printers/{name}/airprint")
def airprint_status(name: str):
    return {"advertised": _cups_call(cups_service.is_advertised, name)}


@app.delete("/api/printers/{name}/jobs", status_code=204)
def clear_jobs(name: str):
    _cups_call(cups_service.cancel_jobs, name)


@app.post("/api/printers/{name}/test")
def print_test_page(name: str):
    _cups_call(cups_service.print_test_page, name)
    return {"status": "ok"}


app.mount(
    "/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="ui"
)
