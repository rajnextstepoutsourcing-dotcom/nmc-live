"""NMC runner

Goals
- Compatible with existing app.py call:
    await run_nmc_check_and_download_pdf(nmc_pin=pin, out_dir=str(job_dir))
- Always return a PDF path (official PDF on success; otherwise a screenshot+diagnostic PDF)
- Best-effort cookie acceptance, PIN entry, search, view details, download PDF

Note: NMC may trigger bot protection / CAPTCHA on server IPs. In that case we return a
screenshot PDF of the blocking page so the user can see exactly why it stopped.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

from pdf_utils import make_simple_error_pdf


NMC_SEARCH_URL = "https://www.nmc.org.uk/registration/search-the-register/"


def _now_ts() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _safe_filename(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-")
    return s or "file"


def _write_screenshot_pdf(out_pdf: Path, screenshot_path: Path, title: str, lines: list[str]) -> None:
    """Create a 2-page PDF:
    - Page 1: screenshot scaled to fit A4
    - Page 2: text diagnostics
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader

    out_pdf.parent.mkdir(parents=True, exist_ok=True)

    c = canvas.Canvas(str(out_pdf), pagesize=A4)
    width, height = A4

    # Page 1: screenshot
    try:
        img = ImageReader(str(screenshot_path))
        iw, ih = img.getSize()
        # Fit with margins
        max_w = width - 48
        max_h = height - 96
        scale = min(max_w / iw, max_h / ih)
        dw = iw * scale
        dh = ih * scale
        x = (width - dw) / 2
        y = (height - dh) / 2
        c.setFont("Helvetica-Bold", 12)
        c.drawString(24, height - 32, title)
        c.drawImage(img, x, y, width=dw, height=dh, preserveAspectRatio=True, mask='auto')
        c.showPage()
    except Exception:
        # If image embedding fails, still produce a text PDF.
        c.setFont("Helvetica-Bold", 16)
        c.drawString(72, height - 72, title)
        c.setFont("Helvetica", 11)
        c.drawString(72, height - 100, f"Screenshot could not be embedded: {screenshot_path.name}")
        c.showPage()

    # Page 2: diagnostics text
    c.setFont("Helvetica-Bold", 16)
    c.drawString(72, height - 72, title)
    y = height - 104
    c.setFont("Helvetica", 11)
    for ln in lines:
        for wrapped in _wrap(ln, 95):
            c.drawString(72, y, wrapped)
            y -= 16
            if y < 72:
                c.showPage()
                y = height - 72
                c.setFont("Helvetica", 11)
    c.showPage()
    c.save()


def _wrap(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    words = text.split()
    out: list[str] = []
    cur = ""
    for w in words:
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= max_chars:
            cur += " " + w
        else:
            out.append(cur)
            cur = w
    if cur:
        out.append(cur)
    return out


def _make_error_pdf(out_dir: Path, title: str, message: str, *, pin: str, stage: str, screenshot: Optional[Path] = None) -> Dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    base = _safe_filename(f"NMC-{stage}-{_now_ts()}")

    if screenshot and screenshot.exists():
        pdf_path = out_dir / f"{base}.pdf"
        _write_screenshot_pdf(
            pdf_path,
            screenshot,
            title,
            [
                f"PIN: {pin}",
                f"Stage: {stage}",
                message,
                "",
                "If you see bot protection / CAPTCHA, the service cannot complete the check automatically.",
                "You can open the NMC register page manually, accept cookies, solve CAPTCHA (if shown), search the PIN, and download the PDF.",
            ],
        )
        return {"ok": False, "pdf_path": str(pdf_path), "error": message, "stage": stage}

    # Fallback: plain text PDF
    pdf_path = out_dir / f"{base}.pdf"
    make_simple_error_pdf(
        pdf_path,
        title,
        [f"PIN: {pin}", f"Stage: {stage}", message],
    )
    return {"ok": False, "pdf_path": str(pdf_path), "error": message, "stage": stage}


async def run_nmc_check_and_download_pdf(
    nmc_pin: str,
    out_dir: Optional[str] = None,
    output_dir: Optional[str] = None,
    output_pdf_path: Optional[str] = None,
    **_: Any,
) -> Dict[str, Any]:
    """Run the NMC register search and download the official PDF.

    Compatibility:
    - app.py passes out_dir=...
    """

    pin = (nmc_pin or "").strip().upper()

    # Resolve output directory
    if output_pdf_path:
        out_path_dir = Path(output_pdf_path).expanduser().resolve().parent
        official_pdf_path = Path(output_pdf_path).expanduser().resolve()
    else:
        chosen_dir = out_dir or output_dir or os.getenv("OUTPUT_DIR") or "./data"
        out_path_dir = Path(chosen_dir).expanduser().resolve()
        official_pdf_path = out_path_dir / f"NMC-Register-{_safe_filename(pin)}-{_now_ts()}.pdf"

    out_path_dir.mkdir(parents=True, exist_ok=True)

    screenshot_path = out_path_dir / f"nmc-debug-{_safe_filename(pin)}-{_now_ts()}.png"

    # Lazy import so web service can boot even if playwright deps are missing
    try:
        from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    except Exception as e:
        return _make_error_pdf(
            out_path_dir,
            "NMC Check Failed",
            f"Playwright import failed: {e}",
            pin=pin,
            stage="startup",
        )

    async def _maybe_click_cookie_buttons(page) -> None:
        # Cookiebot common IDs
        candidates = [
            "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
            "#CybotCookiebotDialogBodyButtonAccept",
            "button:has-text('I agree')",
            "button:has-text('Allow all')",
            "button:has-text('Accept all')",
            "button:has-text('Accept All')",
            "button:has-text('Accept cookies')",
            "button:has-text('Accept Cookies')",
            "button[aria-label*='Accept' i]",
            "button#onetrust-accept-btn-handler",
        ]
        for sel in candidates:
            try:
                loc = page.locator(sel).first
                if await loc.count():
                    if await loc.is_visible():
                        await loc.click(timeout=1500)
                        await page.wait_for_timeout(500)
            except Exception:
                pass

    async def _detect_bot(page) -> Optional[str]:
        txt = (await page.content()).lower()
        # quick checks
        bot_phrases = [
            "please verify you are not a robot",
            "verify you are human",
            "bot",
            "captcha",
            "enable cookies",
        ]
        for p in bot_phrases:
            if p in txt:
                return p
        return None

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context = await browser.new_context(accept_downloads=True)
            page = await context.new_page()

            # Human-ish pacing
            page.set_default_timeout(30000)

            await page.goto(NMC_SEARCH_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(1200)
            await _maybe_click_cookie_buttons(page)
            await page.wait_for_timeout(800)

            bot = await _detect_bot(page)
            if bot:
                await page.screenshot(path=str(screenshot_path), full_page=True)
                await browser.close()
                return _make_error_pdf(out_path_dir, "NMC Check Blocked", f"Blocked by bot protection: {bot}", pin=pin, stage="landing", screenshot=screenshot_path)

            # Find PIN input
            pin_selectors = [
                "input[name*='pin' i]",
                "input[id*='pin' i]",
                "input[placeholder*='pin' i]",
                "input[aria-label*='pin' i]",
                "input[name*='registration' i]",
                "input[id*='registration' i]",
            ]
            pin_input = None
            for sel in pin_selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() and await loc.is_visible():
                        pin_input = loc
                        break
                except Exception:
                    continue

            if pin_input is None:
                await page.screenshot(path=str(screenshot_path), full_page=True)
                await browser.close()
                return _make_error_pdf(out_path_dir, "NMC Check Failed", "Could not find PIN input field on the page.", pin=pin, stage="find_pin", screenshot=screenshot_path)

            # Fill PIN and verify
            await pin_input.click()
            await pin_input.fill("")
            await page.wait_for_timeout(200)
            await pin_input.type(pin, delay=60)
            await page.wait_for_timeout(300)

            try:
                filled_val = (await pin_input.input_value()).strip().upper()
            except Exception:
                filled_val = ""

            if filled_val != pin:
                await page.screenshot(path=str(screenshot_path), full_page=True)
                await browser.close()
                return _make_error_pdf(
                    out_path_dir,
                    "NMC Check Failed",
                    f"PIN did not stick in the input. Expected '{pin}', found '{filled_val}'.",
                    pin=pin,
                    stage="fill_pin",
                    screenshot=screenshot_path,
                )

            await _maybe_click_cookie_buttons(page)

            # Click Search
            search_selectors = [
                "button:has-text('Search')",
                "input[type='submit'][value*='Search' i]",
                "button[type='submit']",
                "button:has-text('Find')",
            ]
            clicked = False
            for sel in search_selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() and await loc.is_visible():
                        await loc.click()
                        clicked = True
                        break
                except Exception:
                    continue

            if not clicked:
                await page.screenshot(path=str(screenshot_path), full_page=True)
                await browser.close()
                return _make_error_pdf(out_path_dir, "NMC Check Failed", "Could not find/click the Search button.", pin=pin, stage="click_search", screenshot=screenshot_path)

            # Wait for results / view details or bot
            await page.wait_for_timeout(1500)
            await _maybe_click_cookie_buttons(page)

            # If bot appears after search
            bot2 = await _detect_bot(page)
            if bot2:
                await page.screenshot(path=str(screenshot_path), full_page=True)
                await browser.close()
                return _make_error_pdf(out_path_dir, "NMC Check Blocked", f"Blocked after search: {bot2}", pin=pin, stage="after_search", screenshot=screenshot_path)

            # Try to open "View details"
            view_selectors = [
                "a:has-text('View details')",
                "a:has-text('View Details')",
                "button:has-text('View details')",
            ]
            opened = False
            for sel in view_selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() and await loc.is_visible():
                        await loc.click()
                        opened = True
                        break
                except Exception:
                    continue

            if not opened:
                # Could be "no results" or different layout
                await page.screenshot(path=str(screenshot_path), full_page=True)
                await browser.close()
                return _make_error_pdf(out_path_dir, "NMC Check Not Completed", "Could not reach the details page (no results or page layout changed).", pin=pin, stage="view_details", screenshot=screenshot_path)

            await page.wait_for_timeout(1200)
            await _maybe_click_cookie_buttons(page)

            # Download PDF
            download_selectors = [
                "a:has-text('Download PDF')",
                "a:has-text('Download pdf')",
                "button:has-text('Download PDF')",
                "a[href*='pdf' i]:has-text('Download')",
            ]

            download_clicked = False
            for sel in download_selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() and await loc.is_visible():
                        async with page.expect_download(timeout=30000) as dl_info:
                            await loc.click()
                        download = await dl_info.value
                        await download.save_as(str(official_pdf_path))
                        download_clicked = True
                        break
                except PWTimeout:
                    continue
                except Exception:
                    continue

            if not download_clicked:
                await page.screenshot(path=str(screenshot_path), full_page=True)
                await browser.close()
                return _make_error_pdf(out_path_dir, "NMC PDF Not Downloaded", "Could not find or download the official PDF on the details page.", pin=pin, stage="download_pdf", screenshot=screenshot_path)

            await browser.close()
            return {"ok": True, "pdf_path": str(official_pdf_path), "error": None, "stage": "success", "pin": pin}

    except Exception as e:
        # Ensure we return a PDF even on unexpected exceptions.
        try:
            # If screenshot exists, include it
            if screenshot_path.exists():
                return _make_error_pdf(out_path_dir, "NMC Check Failed", f"Unexpected error: {e}", pin=pin, stage="exception", screenshot=screenshot_path)
        except Exception:
            pass

        pdf_path = out_path_dir / f"NMC-Exception-{_safe_filename(pin)}-{_now_ts()}.pdf"
        make_simple_error_pdf(pdf_path, "NMC Check Failed", [f"PIN: {pin}", f"Unexpected error: {e}"])
        return {"ok": False, "pdf_path": str(pdf_path), "error": str(e), "stage": "exception"}
