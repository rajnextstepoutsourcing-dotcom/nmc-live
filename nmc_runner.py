"""nmc_runner.py

Playwright automation for NMC register search -> view details -> download PDF.

Key rules
- Always return a PDF path (official PDF on success OR a generated error PDF)
- Accept cookies (Cookiebot / OneTrust)
- Detect bot protection / CAPTCHA and fail gracefully

NOTE: This file is designed to work with the project's existing pdf_utils.py,
which provides: make_simple_error_pdf(out_path, title, lines)
"""

from __future__ import annotations

import re
import time
import tempfile
from pathlib import Path
from typing import Optional, Tuple

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

from pdf_utils import make_simple_error_pdf

NMC_SEARCH_URL = "https://www.nmc.org.uk/registration/search-the-register/"


def _safe_slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s).strip("_")[:80] or "nmc"


def _tmp_pdf_path(prefix: str) -> Path:
    tmpdir = Path(tempfile.gettempdir())
    return tmpdir / f"{_safe_slug(prefix)}-{int(time.time())}.pdf"


def _click_if_visible(page, selector: str, timeout_ms: int = 2000) -> bool:
    try:
        loc = page.locator(selector).first
        if loc.is_visible(timeout=timeout_ms):
            loc.click()
            return True
    except Exception:
        return False
    return False


def _accept_cookies(page) -> bool:
    """Best-effort cookie accept for Cookiebot/OneTrust and common banners."""
    candidates = [
        "button:has-text('I agree to all cookies')",
        "button:has-text('I agree')",
        "button#onetrust-accept-btn-handler",
        "button:has-text('Accept all')",
        "button:has-text('Accept All')",
        "button:has-text('Accept')",
        "button[aria-label*='Accept' i]",
        "text=I agree to all cookies",
    ]

    # Sometimes banner is inside an iframe
    try:
        for frame in page.frames:
            for sel in candidates:
                try:
                    loc = frame.locator(sel).first
                    if loc.is_visible(timeout=800):
                        loc.click()
                        return True
                except Exception:
                    pass
    except Exception:
        pass

    for sel in candidates:
        if _click_if_visible(page, sel, timeout_ms=1500):
            return True
    return False


def _is_bot_block(page) -> bool:
    try:
        txt = (page.content() or "").lower()
    except Exception:
        txt = ""

    needles = [
        "please verify you are not a robot",
        "verify you are human",
        "are you a robot",
        "bot detection",
        "access denied",
        "captcha",
    ]
    return any(n in txt for n in needles)


def _find_pin_input(page):
    """Find the PIN input on the Search the register page."""
    # Try near the label first
    try:
        label = page.locator("text=Pin number").first
        if label.is_visible(timeout=1500):
            near = label.locator("xpath=ancestor::*[1]//input").first
            if near.count() > 0:
                return near
    except Exception:
        pass

    selectors = [
        "input[name*='pin' i]",
        "input[id*='pin' i]",
        "input[aria-label*='pin' i]",
        "input[placeholder*='pin' i]",
        "input[type='search']",
        "input[type='text']",
    ]

    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=1500):
                return loc
        except Exception:
            continue

    return None


def _fill_pin(page, pin: str) -> Tuple[bool, str]:
    pin = (pin or "").strip().upper()
    inp = _find_pin_input(page)
    if inp is None:
        return False, "PIN input not found"

    try:
        inp.click()
        inp.fill("")
        inp.type(pin, delay=40)

        val = (inp.input_value() or "").strip().upper()
        if val == pin:
            return True, "PIN filled"

        # fallback
        inp.fill(pin)
        val2 = (inp.input_value() or "").strip().upper()
        if val2 == pin:
            return True, "PIN filled"

        return False, f"PIN fill did not stick (value='{val2}')"
    except Exception as e:
        return False, f"PIN fill error: {e}"


def _click_search(page) -> bool:
    selectors = [
        "button:has-text('Search')",
        "input[type='submit'][value*='Search' i]",
        "button[type='submit']",
    ]
    for sel in selectors:
        if _click_if_visible(page, sel, timeout_ms=2500):
            return True
    return False


def _wait_for_results(page, timeout_ms: int = 20000) -> bool:
    targets = [
        "a:has-text('View details')",
        "text=Search results",
        "text=View details",
    ]

    end = time.time() + timeout_ms / 1000
    while time.time() < end:
        if _is_bot_block(page):
            return False
        for t in targets:
            try:
                if page.locator(t).first.is_visible(timeout=500):
                    return True
            except Exception:
                pass
        time.sleep(0.4)

    return False


def _open_first_result(page) -> bool:
    for sel in ["a:has-text('View details')", "a:has-text('View Details')"]:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=3000):
                loc.click()
                return True
        except Exception:
            pass
    return False


def _download_pdf_from_details(page, output_pdf_path: Path, timeout_ms: int = 30000) -> Tuple[bool, str]:
    selectors = [
        "a:has-text('Download PDF')",
        "button:has-text('Download PDF')",
        "a[download]",
        "a[href$='.pdf']",
    ]

    try:
        with page.expect_download(timeout=timeout_ms) as dl_info:
            clicked = False
            for sel in selectors:
                try:
                    loc = page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible(timeout=2000):
                        loc.click()
                        clicked = True
                        break
                except Exception:
                    continue

            if not clicked:
                return False, "Download PDF control not found"

        download = dl_info.value
        download.save_as(str(output_pdf_path))
        return True, "PDF downloaded"

    except PWTimeoutError:
        return False, "Timed out waiting for PDF download"
    except Exception as e:
        return False, f"PDF download error: {e}"


def run_nmc_check_and_download_pdf(nmc_pin: str, output_pdf_path: Optional[str] = None) -> str:
    """Returns a path to a PDF.

    On success: official NMC PDF saved to output_pdf_path
    On failure: generated error PDF saved to output_pdf_path
    """

    out_path = Path(output_pdf_path) if output_pdf_path else _tmp_pdf_path(f"nmc-{nmc_pin}")

    def _err(title: str, lines: list[str]) -> str:
        make_simple_error_pdf(out_path, title=title, lines=lines)
        return str(out_path)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()

            page.goto(NMC_SEARCH_URL, wait_until="domcontentloaded", timeout=30000)

            _accept_cookies(page)

            if _is_bot_block(page):
                return _err(
                    "NMC check blocked by bot protection",
                    [
                        "The NMC website displayed bot verification (e.g., Please verify you are not a robot).",
                        "Try again later or complete the check manually.",
                        f"NMC PIN: {nmc_pin}",
                        f"URL: {NMC_SEARCH_URL}",
                    ],
                )

            ok, msg = _fill_pin(page, nmc_pin)
            if not ok:
                return _err("NMC check failed", [msg, f"NMC PIN: {nmc_pin}", f"URL: {NMC_SEARCH_URL}"])

            if not _click_search(page):
                return _err("NMC check failed", ["Could not click Search button.", f"NMC PIN: {nmc_pin}", f"URL: {NMC_SEARCH_URL}"])

            if not _wait_for_results(page, timeout_ms=20000):
                if _is_bot_block(page):
                    return _err(
                        "NMC check blocked by bot protection",
                        [
                            "Bot verification appeared after clicking Search.",
                            "Try again later, or complete the check manually on the NMC website.",
                            f"NMC PIN: {nmc_pin}",
                            f"URL: {NMC_SEARCH_URL}",
                        ],
                    )
                return _err("NMC check failed", ["No results appeared after Search (timeout).", f"NMC PIN: {nmc_pin}", f"URL: {NMC_SEARCH_URL}"])

            _open_first_result(page)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass

            ok_dl, msg_dl = _download_pdf_from_details(page, out_path, timeout_ms=30000)
            if not ok_dl:
                return _err("NMC check failed", [msg_dl, f"NMC PIN: {nmc_pin}", f"URL: {page.url}"])

            context.close()
            browser.close()
            return str(out_path)

    except Exception as e:
        return _err("NMC automation error", [f"Unexpected error: {e}", f"NMC PIN: {nmc_pin}", f"URL: {NMC_SEARCH_URL}"])
