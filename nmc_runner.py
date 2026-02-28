"""
NMC runner (Render / FastAPI)

Goals:
- Automate NMC "Search the register" by PIN:
  https://www.nmc.org.uk/registration/search-the-register/
- Accept cookies if banner appears
- Fill PIN, click Search, click View details, click Download a pdf
- Save final PDF as: "<Full Name> nmc check.pdf"
- On ANY failure: return a FULL-PAGE VISUAL SNAPSHOT PDF (screenshots stitched into a PDF)
  that shows the real page state (including whether PIN was filled).

Important:
- Function signature MUST be: run_nmc_check_and_download_pdf(nmc_pin, out_dir)
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

# We only rely on the existing simple error PDF if present, but we do NOT use it by default.
# Snapshot PDF is created directly from screenshots (real visual proof).
try:
    from pdf_utils import make_simple_error_pdf  # type: ignore
except Exception:  # pragma: no cover
    make_simple_error_pdf = None  # type: ignore


NMC_URL = "https://www.nmc.org.uk/registration/search-the-register/"


def _now_tag() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _sanitize_filename(s: str) -> str:
    s = s.strip()
    s = re.sub(r"\s+", " ", s)
    # Keep letters, numbers, space, dash, underscore
    s = re.sub(r"[^A-Za-z0-9 _-]", "", s)
    s = s.strip()
    return s or "nmc"


@dataclass
class StepShot:
    label: str
    path: Path


def _write_snapshot_pdf(out_path: Path, shots: List[StepShot], title: str, meta_lines: List[str]) -> None:
    """
    Create a PDF from PNG screenshots (full visual proof).
    Uses reportlab (available in your environment).
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader

    out_path.parent.mkdir(parents=True, exist_ok=True)

    c = canvas.Canvas(str(out_path), pagesize=A4)
    w, h = A4

    # Cover page with meta
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, h - 50, title)

    c.setFont("Helvetica", 10)
    y = h - 80
    for line in meta_lines:
        if y < 60:
            c.showPage()
            y = h - 60
            c.setFont("Helvetica", 10)
        c.drawString(40, y, line[:120])
        y -= 14

    c.showPage()

    # One screenshot per page, scaled to fit with margins
    margin = 30
    max_w = w - 2 * margin
    max_h = h - 2 * margin

    for ss in shots:
        try:
            img = ImageReader(str(ss.path))
        except Exception:
            continue
        iw, ih = img.getSize()

        # Scale to fit
        scale = min(max_w / iw, max_h / ih)
        dw, dh = iw * scale, ih * scale
        x = (w - dw) / 2
        y = (h - dh) / 2

        # Label
        c.setFont("Helvetica-Bold", 11)
        c.drawString(margin, h - margin + 5, ss.label[:90])

        c.drawImage(img, x, y, width=dw, height=dh, preserveAspectRatio=True, anchor='c')
        c.showPage()

    c.save()


def _snap(page, shots: List[StepShot], out_dir: Path, label: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    img_path = out_dir / f"{label}.png"
    page.screenshot(path=str(img_path), full_page=True)
    shots.append(StepShot(label=label, path=img_path))


def _accept_cookies_if_present(page) -> bool:
    """
    Cookie banner: Cookiebot / Usercentrics.
    We try the most specific and safe targets first.
    """
    candidates = [
        # What you see: "I agree to all cookies"
        "button:has-text('I agree to all cookies')",
        "button:has-text('I agree')",
        # Some Cookiebot variants
        "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
        "button#CybotCookiebotDialogBodyButtonAccept",
        "button:has-text('Allow all')",
        "button:has-text('Accept all')",
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=8000, force=True)
                # allow UI to settle
                page.wait_for_timeout(600)
                return True
        except Exception:
            continue
    return False


def _find_pin_input(page):
    """
    Pin input is the 'Pin number' field on the search form.
    Prefer label-based targeting to avoid filling the wrong field.
    """
    # 1) Label-based
    try:
        loc = page.get_by_label("Pin number")
        if loc.count() > 0:
            return loc.first
    except Exception:
        pass

    # 2) Table header "Pin number" then input in same form row
    try:
        # The form appears as a table-like layout; find input near text 'Pin number'
        block = page.locator("xpath=//*[normalize-space()='Pin number']/ancestor::*[self::table or self::div][1]")
        if block.count() > 0:
            inp = block.locator("input").first
            if inp.count() > 0:
                return inp
    except Exception:
        pass

    # 3) Conservative fallback: name/id contains pin
    for sel in [
        "input[name*='pin' i]",
        "input[id*='pin' i]",
        "input[placeholder*='pin' i]",
        "input[aria-label*='pin' i]",
    ]:
        loc = page.locator(sel).first
        if loc.count() > 0:
            return loc
    return None


def _click_search(page) -> None:
    # Prefer exact Search button in the form area
    # The button is magenta with text Search
    candidates = [
        "button:has-text('Search')",
        "input[type='submit'][value*='Search' i]",
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=10000, force=True)
                return
        except Exception:
            continue
    # As a last resort, press Enter in the pin input
    raise RuntimeError("Search button not found/clickable")


def _wait_for_results(page) -> None:
    # Results section contains "Your search returned"
    page.wait_for_selector("text=Your search returned", timeout=20000)


def _click_view_details(page) -> None:
    # Link text is "View details" in results table
    loc = page.locator("a:has-text('View details')").first
    loc.wait_for(state="visible", timeout=20000)
    loc.click(timeout=15000, force=True)


def _wait_for_details_popup(page) -> None:
    # Popup title "Practitioner Details"
    page.wait_for_selector("text=Practitioner Details", timeout=20000)


def _extract_name_from_popup(page) -> str:
    """
    In the popup, left column has "Name" and the value below (e.g., Balkar Singh).
    We'll extract the first plausible name value.
    """
    # Most reliable: table row with header 'Name'
    try:
        # Find the cell that contains 'Name' then the following cell text
        name_value = page.locator("xpath=//*[normalize-space()='Name']/following::*[1]").first
        if name_value.count() > 0:
            txt = name_value.inner_text(timeout=5000).strip()
            if txt and len(txt.split()) >= 2:
                return txt
    except Exception:
        pass

    # Fallback: within popup, look for a label 'Name' and grab next text block
    try:
        popup = page.locator("xpath=//*[contains(.,'Practitioner Details')]/ancestor::*[contains(@class,'modal') or contains(@role,'dialog')][1]")
        if popup.count() > 0:
            txt = popup.first.inner_text(timeout=5000)
            # crude extraction: find line after "Name"
            lines = [l.strip() for l in txt.splitlines() if l.strip()]
            for i, l in enumerate(lines):
                if l.lower() == "name" and i + 1 < len(lines):
                    cand = lines[i + 1]
                    if len(cand.split()) >= 2:
                        return cand
    except Exception:
        pass

    return "NMC Practitioner"


def _click_download_pdf_in_popup(page) -> None:
    # The popup link says "Download a pdf"
    loc = page.locator("a:has-text('Download a pdf')").first
    loc.wait_for(state="visible", timeout=20000)
    loc.click(timeout=15000, force=True)


def _save_download(download, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    download.save_as(str(out_path))


def _download_pdf_after_click(page, context, out_dir: Path, desired_name: str) -> Path:
    """
    Handles both patterns:
    - real download event
    - PDF opens in same/new tab (url contains 'pdf=1' or ends with .pdf)
    """
    safe = _sanitize_filename(desired_name)
    out_path = out_dir / f"{safe} nmc check.pdf"

    # 1) Try real download event
    try:
        with page.expect_download(timeout=20000) as dl_info:
            _click_download_pdf_in_popup(page)
        dl = dl_info.value
        _save_download(dl, out_path)
        return out_path
    except Exception:
        pass

    # 2) If it opens a new page/tab, capture that
    try:
        with context.expect_page(timeout=8000) as pinfo:
            _click_download_pdf_in_popup(page)
        pdf_page = pinfo.value
        pdf_page.wait_for_load_state("domcontentloaded", timeout=15000)
        url = pdf_page.url
        # If it's a direct pdf, we can fetch it via request
        if "pdf" in url.lower() or url.lower().endswith(".pdf"):
            resp = pdf_page.request.get(url, timeout=20000)
            if resp.ok:
                out_path.write_bytes(resp.body())
                return out_path
    except Exception:
        pass

    # 3) Same tab URL changes
    try:
        _click_download_pdf_in_popup(page)
        page.wait_for_timeout(1200)
        url = page.url
        if "pdf" in url.lower() or url.lower().endswith(".pdf") or "pdf=1" in url.lower():
            resp = page.request.get(url, timeout=20000)
            if resp.ok:
                out_path.write_bytes(resp.body())
                return out_path
    except Exception:
        pass

    raise RuntimeError("Could not download PDF (no download event and no PDF URL detected)")


def run_nmc_check_and_download_pdf(nmc_pin: str, out_dir: str) -> str:
    """
    Returns the path of the downloaded PDF on success,
    or the path of a snapshot PDF on failure.

    Signature required: (nmc_pin, out_dir)
    """
    out_dir_path = Path(out_dir)
    shots: List[StepShot] = []
    stage = "start"
    last_url = NMC_URL

    # Where we store snapshots
    snap_dir = out_dir_path / f"nmc_debug_{_now_tag()}"
    snapshot_pdf_path = out_dir_path / f"NMC-Snapshot-{_now_tag()}.pdf"

    def fail(err: str) -> str:
        meta = [
            f"URL: {last_url}",
            f"PIN: {nmc_pin}",
            f"Stage: {stage}",
            f"Error: {err}",
        ]
        # Always create visual snapshot PDF
        try:
            _write_snapshot_pdf(snapshot_pdf_path, shots, "NMC Snapshot", meta)
        except Exception:
            # last resort: text-only error PDF if available
            if make_simple_error_pdf:
                make_simple_error_pdf(str(snapshot_pdf_path), "NMC Snapshot", meta)
        return str(snapshot_pdf_path)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = browser.new_context(
            accept_downloads=True,
            viewport={"width": 1280, "height": 720},
            locale="en-GB",
        )
        page = context.new_page()

        try:
            stage = "goto"
            page.goto(NMC_URL, wait_until="domcontentloaded", timeout=45000)
            last_url = page.url
            _snap(page, shots, snap_dir, "01_landing")

            stage = "cookies"
            _accept_cookies_if_present(page)
            last_url = page.url
            _snap(page, shots, snap_dir, "02_after_cookies")

            stage = "find_pin"
            pin_input = _find_pin_input(page)
            if pin_input is None:
                return fail("Pin input not found")

            stage = "fill_pin"
            pin_input.click(timeout=8000, force=True)
            pin_input.fill("")
            pin_input.type(nmc_pin, delay=60)  # human-like
            # readback proof
            try:
                readback = pin_input.input_value(timeout=3000)
            except Exception:
                readback = ""
            _snap(page, shots, snap_dir, "03_after_pin_filled")
            if readback.strip() != nmc_pin:
                # don't stop; sometimes input_value may be blocked; but keep evidence
                pass

            stage = "click_search"
            try:
                # click search if available, otherwise Enter
                try:
                    _click_search(page)
                except Exception:
                    pin_input.press("Enter")
            except Exception as e:
                return fail(f"Failed to trigger Search: {e}")

            last_url = page.url
            _snap(page, shots, snap_dir, "04_after_search")

            stage = "wait_results"
            try:
                _wait_for_results(page)
            except PWTimeoutError:
                # Sometimes results are below fold; still capture and fail with evidence
                last_url = page.url
                _snap(page, shots, snap_dir, "05_no_results_timeout")
                return fail("Timed out waiting for results section")

            last_url = page.url
            _snap(page, shots, snap_dir, "05_results_visible")

            stage = "view_details"
            try:
                _click_view_details(page)
            except Exception as e:
                _snap(page, shots, snap_dir, "06_view_details_failed")
                return fail(f"Could not click View details: {e}")

            stage = "details_popup"
            try:
                _wait_for_details_popup(page)
            except PWTimeoutError:
                _snap(page, shots, snap_dir, "07_details_popup_timeout")
                return fail("Details popup did not appear")

            last_url = page.url
            _snap(page, shots, snap_dir, "07_details_popup")

            stage = "extract_name"
            full_name = _extract_name_from_popup(page)
            safe_name = _sanitize_filename(full_name)

            stage = "download_pdf"
            try:
                pdf_path = _download_pdf_after_click(page, context, out_dir_path, safe_name)
            except Exception as e:
                _snap(page, shots, snap_dir, "08_download_failed")
                return fail(f"Download failed: {e}")

            # Success evidence snapshot (optional but useful)
            last_url = page.url
            _snap(page, shots, snap_dir, "09_success_after_download")

            browser.close()
            return str(pdf_path)

        except Exception as e:
            last_url = page.url if page else last_url
            try:
                _snap(page, shots, snap_dir, "99_exception")
            except Exception:
                pass
            browser.close()
            return fail(str(e))
