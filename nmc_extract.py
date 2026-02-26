import os
import re
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

import pdfplumber
import fitz  # PyMuPDF

try:
    from google import genai
    from google.genai import types
except Exception:
    genai = None
    types = None

# ------------------------------------------------------------
# NMC PIN extraction (production-safe)
#
# Preferred strict format (official structure):
#   YY M #### C
#   - YY: 2 digits (year)
#   - M : A–L (month code)
#   - ####: 4 digits
#   - C : country code in {E,S,W,N,O}
#
# Examples seen in real docs: 23B0365O, 09B0112E, 16J0151E
# ------------------------------------------------------------

STRICT_NMC_RE = re.compile(r"\b\d{2}[A-L]\d{4}[ESWNO]\b", re.I)
LOOSE_8_RE = re.compile(r"\b\d{2}[A-Z]\d{4}[A-Z]\b", re.I)

ANCHOR_RE = re.compile(
    r"("
    r"NMC\s*PIN"
    r"|NMC\s*PIN\s*NUMBER"
    r"|PIN\s*NUMBER"
    r"|PIN\s*NO\.?"
    r"|PIN\s*#"
    r"|PIN\s*:"
    r"|REGISTRATION\s*NUMBER"
    r"|NMC\s*REGISTRATION\s*NUMBER"
    r"|PERSONAL\s*IDENTIFICATION\s*NUMBER"
    r")",
    re.I
)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL_FAST = os.getenv("GEMINI_MODEL_FAST", "gemini-2.0-flash")
GEMINI_MODEL_STRONG = os.getenv("GEMINI_MODEL_STRONG", "gemini-2.5-pro")

NMC_PDF_TEXT_PAGES = int(os.getenv("NMC_PDF_TEXT_PAGES", "8"))
NMC_PDF_IMAGE_PAGES = int(os.getenv("NMC_PDF_IMAGE_PAGES", "4"))

_client = None
if GEMINI_API_KEY and genai is not None:
    try:
        _client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception:
        _client = None


# --- positional OCR fixes -----------------------------------------------------

_DIGIT_FIX = {
    "O": "0",
    "I": "1",
    "L": "1",
    "S": "5",
    "B": "8",
}

_LETTER_FIX = {
    "0": "O",
    "1": "I",
    "5": "S",
    "8": "B",
}


def _normalize_token(token: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (token or "").upper())


def _fix_by_position(token8: str) -> str:
    """Apply simple OCR corrections based on known PIN positions."""
    s = list(token8)
    # digit positions: 0,1,3,4,5,6
    for i in (0, 1, 3, 4, 5, 6):
        s[i] = _DIGIT_FIX.get(s[i], s[i])
    # letter positions: 2 (month), 7 (country)
    for i in (2, 7):
        s[i] = _LETTER_FIX.get(s[i], s[i])
    return "".join(s)


def _validate_strict(pin: str) -> bool:
    return bool(STRICT_NMC_RE.fullmatch(pin))


def _clean_and_validate(raw: str) -> Optional[str]:
    """
    Try to turn 'raw' into a valid strict NMC PIN.
    - Normalizes
    - Tries 8-char windows
    - Applies positional OCR fixes
    - Validates strict format (month A–L, country ESWNO)
    """
    s = _normalize_token(raw)
    if not s:
        return None

    # Quick win: direct strict match somewhere inside
    m = STRICT_NMC_RE.search(s)
    if m:
        return m.group(0).upper()

    # Try 8-char windows (handles extra chars around)
    if len(s) >= 8:
        for start in range(0, min(len(s) - 7, 16)):  # don't scan too far; PIN is near start usually
            chunk = s[start:start + 8]
            if len(chunk) != 8:
                continue
            fixed = _fix_by_position(chunk)
            if _validate_strict(fixed):
                return fixed

    # Last attempt: if we have an 8-char loose match, fix+validate it
    m2 = LOOSE_8_RE.search(s)
    if m2:
        fixed = _fix_by_position(m2.group(0).upper())
        if _validate_strict(fixed):
            return fixed

    return None


# --- PDF/text helpers ---------------------------------------------------------

def _read_pdf_text(path: Path, max_pages: int = 3) -> str:
    try:
        with pdfplumber.open(str(path)) as pdf:
            parts = []
            for page in pdf.pages[:max_pages]:
                t = page.extract_text() or ""
                if t:
                    parts.append(t)
            return "\n".join(parts)
    except Exception:
        return ""


def _pdf_to_images(path: Path, max_pages: int = 2) -> List[Tuple[bytes, str]]:
    out: List[Tuple[bytes, str]] = []
    try:
        doc = fitz.open(str(path))
        for i in range(min(max_pages, doc.page_count)):
            page = doc.load_page(i)
            pix = page.get_pixmap(dpi=200)
            out.append((pix.tobytes("png"), "image/png"))
        doc.close()
    except Exception:
        pass
    return out


def _file_to_image(path: Path) -> Optional[Tuple[bytes, str]]:
    ext = path.suffix.lower()
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(ext)
    if not mime:
        return None
    try:
        return (path.read_bytes(), mime)
    except Exception:
        return None


# --- extraction strategies ----------------------------------------------------

def _extract_from_text(text: str) -> Tuple[Optional[str], float]:
    """
    Try anchor-first extraction from text, then global scan.
    Returns (pin, confidence).
    """
    if not text:
        return None, 0.0

    T = text.upper()

    # 1) Anchor-first: look near labels and parse next ~80 chars
    for m in ANCHOR_RE.finditer(T):
        window = T[m.end(): m.end() + 120]
        # Try strict directly in the window
        m_strict = STRICT_NMC_RE.search(window)
        if m_strict:
            return m_strict.group(0).upper(), 0.99

        # Try loose candidate then clean/validate
        m_loose = LOOSE_8_RE.search(window)
        if m_loose:
            pin = _clean_and_validate(m_loose.group(0))
            if pin:
                return pin, 0.98

        # As last resort, pick first token-like chunk and clean
        tokenish = re.findall(r"[A-Z0-9]{7,12}", window)
        for tok in tokenish[:3]:
            pin = _clean_and_validate(tok)
            if pin:
                return pin, 0.96

    # 2) Global strict search
    m2 = STRICT_NMC_RE.search(T)
    if m2:
        return m2.group(0).upper(), 0.95

    # 3) Global loose search + validate
    candidates = LOOSE_8_RE.findall(T)
    if candidates:
        # Prefer candidates with "NMC" nearby
        for cand in candidates:
            idx = T.find(cand.upper())
            if idx != -1:
                vicinity = T[max(0, idx - 80): idx + 80]
                if "NMC" in vicinity:
                    pin = _clean_and_validate(cand)
                    if pin:
                        return pin, 0.92
        # Otherwise first valid
        for cand in candidates:
            pin = _clean_and_validate(cand)
            if pin:
                return pin, 0.88

    return None, 0.0


def _gemini_extract(images: List[Tuple[bytes, str]]) -> Tuple[Optional[str], float]:
    if _client is None or types is None or not images:
        return None, 0.0

    prompt = (
        "Extract the NMC PIN from the document image (it may be on an application form). Look for labels like 'NMC PIN', 'PIN number', or 'Registration number'. "
        "Return ONLY the PIN value, nothing else.\n"
        "Valid format:\n"
        "- 2 digits (year)\n"
        "- 1 letter A to L (month code)\n"
        "- 4 digits\n"
        "- 1 letter (country code: E, S, W, N, or O)\n"
        "Example: 12A3456S"
    )

    def _call(model: str) -> str:
        parts = [types.Part.from_text(text=prompt)]
        for b, mime in images:
            parts.append(types.Part.from_bytes(data=b, mime_type=mime))
        resp = _client.models.generate_content(
            model=model,
            contents=[types.Content(role="user", parts=parts)],
        )
        return (getattr(resp, "text", None) or "").strip()

    for model in (GEMINI_MODEL_FAST, GEMINI_MODEL_STRONG):
        try:
            txt = _call(model)
            pin = _clean_and_validate(txt)
            if pin:
                return pin, 0.90 if model == GEMINI_MODEL_FAST else 0.93
        except Exception:
            continue
    return None, 0.0


def extract_nmc_pin(file_path: Path) -> Dict[str, Any]:
    """
    Best-effort extraction.

    Returns:
      { ok: bool, nmc_pin: str|None, confidence: { nmc_pin: float } }
    """
    path = Path(file_path)

    # PDF
    if path.suffix.lower() == ".pdf":
        # 1) Text extraction (page-by-page, stops early)
        try:
            with pdfplumber.open(str(path)) as pdf:
                combined_parts = []
                for page in pdf.pages[:max(1, NMC_PDF_TEXT_PAGES)]:
                    t = page.extract_text() or ""
                    if t:
                        # Try on this page first (helps when PIN is later in the PDF)
                        pin_page, conf_page = _extract_from_text(t)
                        if pin_page:
                            return {"ok": True, "nmc_pin": pin_page, "confidence": {"nmc_pin": conf_page}}
                        combined_parts.append(t)
                text = "\n".join(combined_parts)
        except Exception:
            text = ""

        pin, conf = _extract_from_text(text)
        if pin:
            return {"ok": True, "nmc_pin": pin, "confidence": {"nmc_pin": conf}}

        # 2) Vision extraction (render first N pages as images)
        imgs = _pdf_to_images(path, max_pages=max(1, NMC_PDF_IMAGE_PAGES))
        pin2, conf2 = _gemini_extract(imgs)
        if pin2:
            return {"ok": True, "nmc_pin": pin2, "confidence": {"nmc_pin": conf2}}

        return {"ok": False, "nmc_pin": None, "confidence": {"nmc_pin": 0.0}}

    # Images
    img = _file_to_image(path)
    if img:
        pin3, conf3 = _gemini_extract([img])
        if pin3:
            return {"ok": True, "nmc_pin": pin3, "confidence": {"nmc_pin": conf3}}
        return {"ok": False, "nmc_pin": None, "confidence": {"nmc_pin": 0.0}}

    # Other types: text scan
    try:
        text = path.read_text(errors="ignore")
    except Exception:
        text = ""
    pin4, conf4 = _extract_from_text(text)
    return {"ok": bool(pin4), "nmc_pin": pin4, "confidence": {"nmc_pin": conf4}}
