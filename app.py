import shutil
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from nmc_extract import extract_nmc_pin
from nmc_runner import run_nmc_check_and_download_pdf
from pdf_utils import make_simple_error_pdf

BASE_DIR = Path(__file__).resolve().parent
DATA_ROOT = BASE_DIR / "data"
DATA_ROOT.mkdir(parents=True, exist_ok=True)

app = FastAPI()

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/health")
def health():
    return {"ok": True}


def _new_job_dir() -> Path:
    job_id = f"nmc_{uuid.uuid4().hex}"
    job_dir = DATA_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    return job_dir


def _save_upload(upload: UploadFile) -> Path:
    suffix = Path(upload.filename or "").suffix.lower()
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    Path(tmp_path).unlink(missing_ok=True)
    tmp = Path(tmp_path)
    with tmp.open("wb") as f:
        shutil.copyfileobj(upload.file, f)
    return tmp


@app.post("/run")
async def run_nmc(file: UploadFile = File(...)):
    """PDF-only endpoint.

    - Extract NMC PIN from uploaded file
    - Run NMC automation and download official PDF
    - Always returns a PDF (official or error PDF)
    """
    job_dir = _new_job_dir()

    tmp = _save_upload(file)
    try:
        extracted = extract_nmc_pin(tmp)
    finally:
        tmp.unlink(missing_ok=True)

    pin = (extracted.get("nmc_pin") or "").strip().upper()
    if not pin:
        out = job_dir / "NMC-Error-Extraction.pdf"
        make_simple_error_pdf(
            out_path=out,
            title="NMC check failed",
            lines=[
                "Unable to extract NMC PIN from the uploaded document.",
                "Please upload a clearer NMC document (PDF/image) that contains the PIN.",
            ],
        )
        return FileResponse(str(out), media_type="application/pdf", filename=out.name)

    result = await run_nmc_check_and_download_pdf(nmc_pin=pin, out_dir=str(job_dir))
    pdf_path = Path(result.get("pdf_path") or "")

    if not pdf_path.exists():
        out = job_dir / "NMC-Error-Internal.pdf"
        make_simple_error_pdf(
            out_path=out,
            title="NMC check failed",
            lines=[
                "The check could not generate a PDF.",
                "Please try again.",
            ],
        )
        return FileResponse(str(out), media_type="application/pdf", filename=out.name)

    return FileResponse(str(pdf_path), media_type="application/pdf", filename=pdf_path.name)
