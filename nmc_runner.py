"""
nmc_runner.py

Automates NMC register lookup by PIN.

Requirements (as per project notes):
- DO NOT modify app.py unless absolutely necessary (we don't).
- run_nmc_check_and_download_pdf must accept: nmc_pin and out_dir
- On failure: must return a FULL-PAGE visual snapshot PDF of the site, showing what happened.
- Save final downloaded PDF using the practitioner's Name (e.g., "Balkar Singh nmc check.pdf")

How we produce "full page snapshot PDF":
- We take full_page screenshots (PNG) at key steps.
- We assemble them into a single PDF (images + URL + key notes) using reportlab.
This is more reliable than Playwright's page.pdf() print-to-PDF (which hides overlays / input values).
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List

from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

from pdf_utils import make_simple_error_pdf

# reportlab is available in this environment (used only to assemble screenshot-PDF evidence)
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader


NMC_SEARCH_URL = "https://www.nmc.org.uk/registration/search-the-register/"


def _now_tag() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe_filename(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^a-zA-Z0-9 .,_-]+", "", s)
    s = s.strip(" ._-")
    if not s:
        return "file"
    # keep reasonable length
    return s[:120].strip()


def _error_pdf(out_dir: Path, title: str, lines: List[str]) -> Path:
    """
    pdf_utils.make_simple_error_pdf(out_path, title, lines)
    """
    _ensure_dir(out_dir)
    pdf_path = out_dir / f"NMC-Error-{_now_tag()}.pdf"
    make_simple_error_pdf(pdf_path, title, lines)
    return pdf_path


def _write_screenshot_pdf(out_pdf: Path, title: str, url: str, notes: List[str], images: List[Tuple[str, str]]) -> None:
    """
    Create a PDF where each page is:
      - title + URL + notes (top)
      - one screenshot (scaled to fit)
      - caption
    images: [(caption, image_path), ...]
    """
    _ensure_dir(out_pdf.parent)
    c = canvas.Canvas(str(out_pdf), pagesize=A4)
    page_w, page_h = A4

    def draw_header():
        y = page_h - 36
        c.setFont("Helvetica-Bold", 14)
        c.drawString(36, y, title)
        y -= 18
        c.setFont("Helvetica", 9)
        # URL
        c.drawString(36, y, f"URL: {url}")
        y -= 14
        # Notes (up to a few lines)
        for line in (notes or [])[:10]:
            c.drawString(36, y, str(line)[:140])
            y -= 12
        return y

    for caption, img_path in images:
        top_y = draw_header()
        # Image area
        img = ImageReader(img_path)
        iw, ih = img.getSize()
        # fit into remaining area with margins
        max_w = page_w - 72
        max_h = top_y - 72  # leave bottom margin
        scale = min(max_w / iw, max_h / ih) if iw and ih else 1.0
        draw_w = iw * scale
        draw_h = ih * scale
        x = (page_w - draw_w) / 2
        y = 54  # bottom margin
        c.drawImage(img, x, y, width=draw_w, height=draw_h, preserveAspectRatio=True, anchor='c')
        # Caption at bottom
        c.setFont("Helvetica-Oblique", 9)
        c.drawString(36, 36, caption[:180])
        c.showPage()

    # If no images, still produce a simple page
    if not images:
        draw_header()
        c.setFont("Helvetica", 10)
        c.drawString(36, 72, "No screenshots were captured.")
        c.showPage()

    c.save()


async def _click_any(page, selectors: List[str], timeout_ms: int = 2500) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count():
                # Sometimes an overlay is present; force click can help but avoid if not needed.
                await loc.click(timeout=timeout_ms)
                return True
        except Exception:
            continue
    return False


async def _accept_cookies(page) -> None:
    """
    NMC cookie banners vary. We attempt known accept buttons.
    Continue even if not present.
    """
    for _ in range(4):
        clicked = await _click_any(
            page,
            [
                "button:has-text('I agree to all cookies')",
                "button:has-text('Allow all cookies')",
                "button:has-text('Accept all cookies')",
                "button:has-text('Accept all')",
                "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
                "#CybotCookiebotDialogBodyButtonAccept",
                "button#onetrust-accept-btn-handler",
                "button[aria-label*='cookie' i]:has-text('Accept')",
            ],
            timeout_ms=3000,
        )
        await page.wait_for_timeout(500)
        if not clicked:
            await page.wait_for_timeout(600)


async def _detect_captcha_widget(page) -> bool:
    """
    Detect real CAPTCHA widgets (not just text).
    """
    patterns = [
        "iframe[src*='captcha' i]",
        "iframe[title*='captcha' i]",
        "iframe[src*='hcaptcha' i]",
        "iframe[src*='recaptcha' i]",
        "div.g-recaptcha",
        "[data-sitekey]",
        "iframe[src*='challenges.cloudflare.com' i]",
        "div[class*='turnstile' i]",
        "div[id*='turnstile' i]",
        "div[class*='hcaptcha' i]",
    ]
    try:
        for sel in patterns:
            if await page.locator(sel).count():
                return True
    except Exception:
        pass
    return False


async def _find_pin_input(page):
    """
    Find the PIN input robustly (prefer label-based / aria-based).
    """
    candidates = [
        # Best: input with label/aria/placeholder mentioning pin
        "input[aria-label*='pin number' i]",
        "input[placeholder*='pin number' i]",
        "input[aria-label*='pin' i]",
        "input[placeholder*='pin' i]",
        "input[name*='pin' i]",
        "input[id*='pin' i]",
    ]
    for sel in candidates:
        loc = page.locator(sel).first
        try:
            if await loc.count() and await loc.is_visible() and await loc.is_enabled():
                return loc
        except Exception:
            continue

    # Fallback: find input near text "Pin number"
    try:
        # Label -> input
        label = page.locator("label:has-text('Pin number')").first
        if await label.count():
            # for=...
            for_attr = await label.get_attribute("for")
            if for_attr:
                loc = page.locator(f"#{for_attr}").first
                if await loc.count() and await loc.is_visible() and await loc.is_enabled():
                    return loc
            # try adjacent input
            loc = label.locator("xpath=following::input[1]")
            if await loc.count() and await loc.first.is_visible() and await loc.first.is_enabled():
                return loc.first
    except Exception:
        pass

    return None


async def _fill_pin(page, pin: str) -> Tuple[bool, str]:
    pin = (pin or "").strip()
    loc = await _find_pin_input(page)
    if not loc:
        return False, ""
    try:
        await loc.scroll_into_view_if_needed()
    except Exception:
        pass

    for _ in range(3):
        try:
            await loc.click(timeout=3000)
            await loc.fill("", timeout=3000)
            await loc.type(pin, delay=60)
            await page.wait_for_timeout(200)
            val = await loc.input_value()
            if val and pin.replace(" ", "") in val.replace(" ", ""):
                return True, val
        except Exception:
            await page.wait_for_timeout(250)

    # Last resort JS set
    try:
        await page.evaluate(
            """([el, v]) => { el.value = v; el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); }""",
            [loc, pin],
        )
        await page.wait_for_timeout(200)
        val = await loc.input_value()
        return (bool(val), val or "")
    except Exception:
        return False, ""


async def _click_search(page) -> bool:
    selectors = [
        "button:has-text('Search')",
        "input[type='submit'][value*='Search' i]",
        "button[type='submit']:has-text('Search')",
        # last fallback: any submit in the same form as the pin input
        "button[type='submit']",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible() and await loc.is_enabled():
                await loc.click(timeout=8000)
                return True
        except Exception:
            continue
    return False


async def _get_name_from_details(page) -> str:
    """
    Extract practitioner name from the details popup/panel.
    We try multiple DOM patterns (definition lists / tables / headings).
    """
    patterns = [
        # Common <dt>Name</dt><dd>Value</dd>
        ("dt:has-text('Name') + dd", True),
        ("dt:has-text('Name')", False),
        # Table-like
        ("tr:has(td:has-text('Name')) td:nth-child(2)", True),
        # Headings inside modal
        ("[role='dialog'] h2", True),
        ("[role='dialog'] h3", True),
        ("h2:has-text('Practitioner')", True),
    ]
    for sel, direct in patterns:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                txt = (await loc.inner_text()).strip()
                if not txt and not direct:
                    # try next sibling as value
                    nxt = loc.locator("xpath=following::*[1]").first
                    if await nxt.count():
                        txt = (await nxt.inner_text()).strip()
                txt = re.sub(r"\s+", " ", txt)
                # Filter out headings that are not names
                if txt and len(txt) <= 80 and "Practitioner" not in txt:
                    # Often includes extra lines; keep first line-ish
                    txt = txt.split("\n")[0].strip()
                    return txt
        except Exception:
            continue
    return ""


async def _capture_step(page, out_dir: Path, step: str, images: List[Tuple[str, str]]) -> None:
    """
    Capture a full-page screenshot and append to images list.
    """
    try:
        png = out_dir / f"{_safe_filename(step)}-{_now_tag()}.png"
        await page.screenshot(path=str(png), full_page=True)
        if png.exists() and png.stat().st_size > 0:
            images.append((step, str(png)))
    except Exception:
        pass


async def run_nmc_check_and_download_pdf(nmc_pin: str, out_dir: str) -> Dict[str, Any]:
    """
    Returns:
      { ok: bool, pdf_path: str, error: str, stage: str }
    """
    pin = (nmc_pin or "").strip()
    out_path_dir = _ensure_dir(Path(out_dir or "output"))
    evidence_images: List[Tuple[str, str]] = []
    stage = "start"

    def fail_with_snapshot(title: str, error: str, extra_lines: List[str]) -> Dict[str, Any]:
        # Create screenshot PDF evidence if possible
        url = current_url[0] if current_url[0] else NMC_SEARCH_URL
        snap_pdf = out_path_dir / f"NMC-Snapshot-{_now_tag()}.pdf"
        notes = [f"PIN: {pin}", f"Stage: {stage}", f"Error: {error}"] + (extra_lines or [])
        try:
            _write_screenshot_pdf(snap_pdf, title, url, notes, evidence_images)
            if snap_pdf.exists() and snap_pdf.stat().st_size > 0:
                return {"ok": False, "pdf_path": str(snap_pdf), "error": error, "stage": stage}
        except Exception:
            pass
        # Fallback to simple error PDF
        err_pdf = _error_pdf(out_path_dir, title, notes + [f"URL: {url}"])
        return {"ok": False, "pdf_path": str(err_pdf), "error": error, "stage": stage}

    current_url = [""]

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
            context = await browser.new_context(
                accept_downloads=True,
                viewport={"width": 1365, "height": 768},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
                locale="en-GB",
            )
            page = await context.new_page()

            stage = "landing"
            await page.goto(NMC_SEARCH_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(1200)
            current_url[0] = page.url
            await _capture_step(page, out_path_dir, "01_landing", evidence_images)

            # cookies (best effort)
            await _accept_cookies(page)
            await page.wait_for_timeout(800)
            current_url[0] = page.url
            await _capture_step(page, out_path_dir, "02_after_cookies", evidence_images)

            # Captcha check (widget)
            if await _detect_captcha_widget(page):
                stage = "captcha"
                await _capture_step(page, out_path_dir, "captcha_detected", evidence_images)
                await context.close()
                await browser.close()
                return fail_with_snapshot("NMC Check Blocked", "CAPTCHA widget detected", [])

            # Fill PIN
            stage = "fill"
            ok_fill, observed = await _fill_pin(page, pin)
            current_url[0] = page.url
            await _capture_step(page, out_path_dir, "03_after_pin_filled", evidence_images)

            if not ok_fill:
                await context.close()
                await browser.close()
                return fail_with_snapshot(
                    "NMC Check Failed",
                    "Could not locate/fill PIN input field",
                    [f"Observed PIN value: {observed!r}"],
                )

            # Click Search
            stage = "search"
            clicked = await _click_search(page)
            if not clicked:
                try:
                    await page.keyboard.press("Enter")
                    clicked = True
                except Exception:
                    clicked = False
            await page.wait_for_timeout(1500)
            current_url[0] = page.url
            await _capture_step(page, out_path_dir, "04_after_search", evidence_images)

            # Captcha after search
            if await _detect_captcha_widget(page):
                stage = "captcha"
                await _capture_step(page, out_path_dir, "captcha_after_search", evidence_images)
                await context.close()
                await browser.close()
                return fail_with_snapshot("NMC Check Blocked", "CAPTCHA after search", [f"Observed PIN: {observed!r}"])

            # Results: View details
            stage = "results"
            view_loc = page.locator("a:has-text('View details'), button:has-text('View details')").first
            try:
                await view_loc.wait_for(timeout=20000)
                await view_loc.click(timeout=12000)
            except Exception:
                await _capture_step(page, out_path_dir, "05_no_view_details", evidence_images)
                await context.close()
                await browser.close()
                return fail_with_snapshot(
                    "NMC Check Failed",
                    "Could not find/click 'View details' after search",
                    [f"Observed PIN: {observed!r}"],
                )

            await page.wait_for_timeout(1200)
            current_url[0] = page.url
            await _capture_step(page, out_path_dir, "06_after_view_details", evidence_images)

            # Get name (best effort)
            name = await _get_name_from_details(page)
            safe_name = _safe_filename(name) if name else ""
            if not safe_name:
                safe_name = f"NMC-{pin}"

            # Download a pdf (exact label on site)
            stage = "download"
            dl_selectors = [
                "a:has-text('Download a pdf')",
                "button:has-text('Download a pdf')",
                "a:has-text('Download a PDF')",
                "button:has-text('Download a PDF')",
                "a[href*='pdf' i]:has-text('Download')",
            ]

            downloaded_path: Optional[Path] = None

            for sel in dl_selectors:
                loc = page.locator(sel).first
                try:
                    if not (await loc.count()) or not (await loc.is_visible()):
                        continue

                    # Try as a real download
                    try:
                        async with page.expect_download(timeout=25000) as dl_info:
                            await loc.click(timeout=12000)
                        download = await dl_info.value
                        target_path = out_path_dir / f"{safe_name} nmc check.pdf"
                        await download.save_as(str(target_path))
                        downloaded_path = target_path
                        break
                    except PWTimeoutError:
                        # No download event; could open PDF in tab or same page.
                        await loc.click(timeout=12000)
                        await page.wait_for_timeout(1500)
                        current_url[0] = page.url
                        await _capture_step(page, out_path_dir, "07_after_download_click", evidence_images)

                        # If current URL looks like PDF, fetch it and save
                        if ".pdf" in (page.url or "").lower() or "pdf" in (page.url or "").lower():
                            try:
                                resp = await context.request.get(page.url, timeout=25000)
                                if resp.ok:
                                    data = await resp.body()
                                    target_path = out_path_dir / f"{safe_name} nmc check.pdf"
                                    target_path.write_bytes(data)
                                    downloaded_path = target_path
                                    break
                            except Exception:
                                pass

                        # Or it opened a new page with PDF (best effort)
                        pages = context.pages
                        if len(pages) > 1:
                            pdf_page = pages[-1]
                            try:
                                if ".pdf" in (pdf_page.url or "").lower() or "pdf" in (pdf_page.url or "").lower():
                                    resp = await context.request.get(pdf_page.url, timeout=25000)
                                    if resp.ok:
                                        data = await resp.body()
                                        target_path = out_path_dir / f"{safe_name} nmc check.pdf"
                                        target_path.write_bytes(data)
                                        downloaded_path = target_path
                                        break
                            except Exception:
                                pass

                except Exception:
                    continue

            await context.close()
            await browser.close()

            if downloaded_path and downloaded_path.exists() and downloaded_path.stat().st_size > 0:
                return {"ok": True, "pdf_path": str(downloaded_path), "error": "", "stage": "download"}

            # If download failed, return evidence snapshot PDF (with URL + steps)
            return fail_with_snapshot(
                "NMC Check Failed",
                "Could not download official PDF",
                [f"Name detected: {name!r}", f"Observed PIN: {observed!r}"],
            )

    except Exception as e:
        return fail_with_snapshot("NMC Check Failed", f"Unexpected error: {e!r}", [])
