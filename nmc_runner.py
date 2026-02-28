"""
nmc_runner.py

Goal:
- Run NMC register search by PIN
- Best-effort: accept cookies, fill PIN, click Search, click View details, download official PDF
- If anything fails: return a PDF snapshot of the page (via Playwright page.pdf) + an error PDF
- Compatible with app.py calling: run_nmc_check_and_download_pdf(nmc_pin=..., out_dir=...)

Notes:
- We intentionally DO NOT stop early just because we see bot-protection text.
  We only stop early if we detect an actual CAPTCHA widget (turnstile/hcaptcha/recaptcha) or if the form is disabled.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

from pdf_utils import make_simple_error_pdf, make_debug_snapshot_pdf


NMC_SEARCH_URL = "https://www.nmc.org.uk/registration/search-the-register/"


def _now_tag() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe_filename(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", s).strip("-")
    return s or "file"


def _error_pdf(out_dir: Path, title: str, lines: list[str]) -> Path:
    """
    pdf_utils.make_simple_error_pdf(out_path, title, lines)
    """
    _ensure_dir(out_dir)
    pdf_path = out_dir / f"NMC-Error-{_now_tag()}.pdf"
    make_simple_error_pdf(pdf_path, title, lines)
    return pdf_path


async def _capture_scroll_screenshots(page, out_dir: Path, prefix: str) -> list[Path]:
    """Capture a *readable* full-page snapshot by taking multiple viewport screenshots.

    Why:
      - page.pdf() is print-to-PDF (often hides overlays, typed values, widgets)
      - a single full_page screenshot becomes unreadable when scaled onto A4

    This function scrolls and captures the viewport repeatedly with slight overlap.
    """
    _ensure_dir(out_dir)
    shots: list[Path] = []

    try:
        viewport = page.viewport_size or {"width": 1280, "height": 720}
        vw = int(viewport.get("width", 1280))
        vh = int(viewport.get("height", 720))

        # Best-effort total height
        total_h = await page.evaluate(
            """() => Math.max(
              document.documentElement.scrollHeight,
              document.body ? document.body.scrollHeight : 0,
              document.documentElement.offsetHeight,
              document.body ? document.body.offsetHeight : 0
            )"""
        )
        total_h = int(total_h) if total_h else vh

        step = max(200, vh - 80)  # overlap for continuity
        y = 0
        idx = 1
        max_pages = 18  # safety cap to avoid huge PDFs
        while y < total_h and idx <= max_pages:
            await page.evaluate("(yy) => window.scrollTo(0, yy)", y)
            await page.wait_for_timeout(350)

            img_path = out_dir / f"{_safe_filename(prefix)}-{_now_tag()}-{idx:02d}.png"
            await page.screenshot(path=str(img_path), full_page=False)
            if img_path.exists() and img_path.stat().st_size > 0:
                shots.append(img_path)

            y += step
            idx += 1

        # Return to top (helps if further actions happen)
        try:
            await page.evaluate("() => window.scrollTo(0, 0)")
        except Exception:
            pass
    except Exception:
        # Last resort: one full-page screenshot
        try:
            img_path = out_dir / f"{_safe_filename(prefix)}-{_now_tag()}-FULL.png"
            await page.screenshot(path=str(img_path), full_page=True)
            if img_path.exists() and img_path.stat().st_size > 0:
                shots.append(img_path)
        except Exception:
            pass

    return shots


async def _full_page_snapshot_pdf(page, out_dir: Path, prefix: str, title: str, lines: list[str]) -> Optional[Path]:
    """Create a *true* full-page snapshot PDF from screenshots (not print-to-PDF)."""
    _ensure_dir(out_dir)
    pdf_path = out_dir / f"{_safe_filename(prefix)}-{_now_tag()}.pdf"

    shots = await _capture_scroll_screenshots(page, out_dir, prefix)
    if not shots:
        return None
    try:
        make_debug_snapshot_pdf(pdf_path, title, lines, shots)
        if pdf_path.exists() and pdf_path.stat().st_size > 0:
            return pdf_path
    except Exception:
        return None
    return None


async def _click_any(page, selectors: list[str], timeout_ms: int = 2500) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count():
                await loc.click(timeout=timeout_ms)
                return True
        except Exception:
            continue
    return False


async def _accept_cookies(page) -> None:
    """
    NMC uses cookie banners that vary (Cookiebot / OneTrust / custom).
    We try multiple known accept buttons.
    """
    # Try a few times, banners can appear late.
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
                "button[aria-label*='Accept'][aria-label*='cookie' i]",
                "button:has-text('Accept')",
            ],
            timeout_ms=2500,
        )
        if not clicked:
            # Sometimes inside shadow/iframe; just wait a bit and try again
            await page.wait_for_timeout(600)
        else:
            await page.wait_for_timeout(500)


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
        "[data-sitekey]",  # recaptcha/turnstile/hcaptcha
        "iframe[src*='challenges.cloudflare.com' i]",  # Cloudflare Turnstile
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
    Locate the NMC PIN input field robustly.
    """
    # Prefer accessible label-based selectors first.
    try:
        loc = page.get_by_label(re.compile(r"\bPIN\b", re.I)).first
        if await loc.count() and await loc.is_visible() and await loc.is_enabled():
            return loc
    except Exception:
        pass

    candidates = [
        "input[name*='pin' i]",
        "input[id*='pin' i]",
        "input[aria-label*='pin' i]",
        "input[placeholder*='pin' i]",
        # Avoid generic input[type=text] fallbacks; they can target the wrong field.
    ]
    for sel in candidates:
        loc = page.locator(sel).first
        try:
            if await loc.count():
                # Only accept visible/enabled inputs
                if await loc.is_visible() and await loc.is_enabled():
                    return loc
        except Exception:
            continue
    return None


async def _fill_pin(page, pin: str) -> Tuple[bool, str]:
    """
    Fill pin and verify value.
    Returns (ok, observed_value).
    """
    pin = (pin or "").strip()
    loc = await _find_pin_input(page)
    if not loc:
        return False, ""
    try:
        await loc.scroll_into_view_if_needed()
    except Exception:
        pass

    # Try fill strategies
    for _ in range(3):
        try:
            await loc.click(timeout=2000)
            await loc.fill("", timeout=2000)
            await loc.type(pin, delay=50)  # human-like
            await page.wait_for_timeout(200)
            val = (await loc.input_value()) if hasattr(loc, "input_value") else ""
            if val and pin.replace(" ", "") in val.replace(" ", ""):
                return True, val
        except Exception:
            await page.wait_for_timeout(250)

    # Last resort: evaluate set value
    try:
        await page.evaluate(
            """([el, v]) => { el.value = v; el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); }""",
            [loc, pin],
        )
        await page.wait_for_timeout(200)
        val = await loc.input_value()
        if val and pin.replace(" ", "") in val.replace(" ", ""):
            return True, val
        return False, val
    except Exception:
        return False, ""


async def _click_search(page) -> bool:
    """
    Click the search/submit button.
    """
    # Prefer a button with Search text.
    # Prefer ARIA role lookups to avoid clicking the wrong button.
    try:
        btn = page.get_by_role("button", name=re.compile(r"^Search$", re.I)).first
        if await btn.count() and await btn.is_visible() and await btn.is_enabled():
            await btn.click(timeout=6000)
            return True
    except Exception:
        pass

    selectors = [
        "button:has-text('Search')",
        "input[type='submit'][value*='Search' i]",
        "button[type='submit']",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                if await loc.is_enabled():
                    await loc.click(timeout=4000)
                    return True
        except Exception:
            continue
    return False


async def run_nmc_check_and_download_pdf(nmc_pin: str, out_dir: str) -> Dict[str, Any]:
    """
    Returns:
      { ok: bool, pdf_path: str, error: str, stage: str }

    Compatibility:
      app.py may call with out_dir=...
    """
    pin = (nmc_pin or "").strip()
    out_path_dir = Path(out_dir or "output")
    _ensure_dir(out_path_dir)

    forced_pdf_path = None  # keep runner signature strict (nmc_pin, out_dir)

    stage = "start"
    try:
        async with async_playwright() as p:
            # Use chromium
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-web-security",
                ],
            )
            context = await browser.new_context(
                accept_downloads=True,
                viewport={"width": 1280, "height": 720},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
                locale="en-GB",
            )
            page = await context.new_page()

            stage = "landing"
            await page.goto(NMC_SEARCH_URL, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(1200)

            # Cookies first
            await _accept_cookies(page)
            await page.wait_for_timeout(800)

            # If there's an actual captcha widget, stop and return snapshot PDF.
            if await _detect_captcha_widget(page):
                snap = await _full_page_snapshot_pdf(
                    page,
                    out_path_dir,
                    "NMC-captcha",
                    "NMC Check Blocked",
                    [f"PIN: {pin}", "Stage: captcha", "CAPTCHA widget detected on the page."],
                )
                if snap:
                    return {"ok": False, "pdf_path": str(snap), "error": "CAPTCHA widget detected", "stage": "captcha"}
                err = _error_pdf(
                    out_path_dir,
                    "NMC Check Blocked",
                    [f"PIN: {pin}", "Stage: captcha", "CAPTCHA widget detected on the page."],
                )
                return {"ok": False, "pdf_path": str(err), "error": "CAPTCHA widget detected", "stage": "captcha"}

            # Fill PIN (DO NOT stop just because bot-protection text exists)
            stage = "fill"
            ok_fill, observed = await _fill_pin(page, pin)
            if not ok_fill:
                snap = await _full_page_snapshot_pdf(
                    page,
                    out_path_dir,
                    "NMC-no-pin-field",
                    "NMC Check Failed",
                    [f"PIN: {pin}", "Stage: fill", "Could not locate/fill the PIN input field."],
                )
                if snap:
                    return {
                        "ok": False,
                        "pdf_path": str(snap),
                        "error": "Could not locate/fill PIN input field",
                        "stage": "fill",
                    }
                err = _error_pdf(
                    out_path_dir,
                    "NMC Check Failed",
                    [f"PIN: {pin}", "Stage: fill", "Could not locate/fill the PIN input field."],
                )
                return {"ok": False, "pdf_path": str(err), "error": "PIN fill failed", "stage": "fill"}

            # Click Search
            stage = "search"
            clicked = await _click_search(page)
            if not clicked:
                # sometimes form needs enter key
                try:
                    await page.keyboard.press("Enter")
                    clicked = True
                except Exception:
                    clicked = False

            # Wait for results to change
            await page.wait_for_timeout(1500)

            # If captcha widget appears after search, snapshot and stop
            if await _detect_captcha_widget(page):
                snap = await _full_page_snapshot_pdf(
                    page,
                    out_path_dir,
                    "NMC-captcha-after-search",
                    "NMC Check Blocked",
                    [f"PIN: {pin}", "Stage: captcha", "CAPTCHA appeared after clicking Search."],
                )
                if snap:
                    return {"ok": False, "pdf_path": str(snap), "error": "CAPTCHA after search", "stage": "captcha"}
                err = _error_pdf(
                    out_path_dir,
                    "NMC Check Blocked",
                    [f"PIN: {pin}", "Stage: captcha", "CAPTCHA appeared after clicking Search."],
                )
                return {"ok": False, "pdf_path": str(err), "error": "CAPTCHA after search", "stage": "captcha"}

            # Look for "View details" (or similar)
            stage = "results"
            view_loc = page.locator("a:has-text('View details'), button:has-text('View details')").first
            try:
                await view_loc.wait_for(timeout=12000)
                await view_loc.click(timeout=6000)
            except Exception:
                # Maybe no results / still on same page
                snap = await _full_page_snapshot_pdf(
                    page,
                    out_path_dir,
                    "NMC-results-not-found",
                    "NMC Check Failed",
                    [
                        f"PIN: {pin}",
                        "Stage: results",
                        f"PIN filled as observed: {observed}",
                        "Could not find 'View details' after clicking Search.",
                    ],
                )
                if snap:
                    return {
                        "ok": False,
                        "pdf_path": str(snap),
                        "error": "Could not find 'View details' after search",
                        "stage": "results",
                    }
                err = _error_pdf(
                    out_path_dir,
                    "NMC Check Failed",
                    [
                        f"PIN: {pin}",
                        "Stage: results",
                        f"PIN filled as: {observed}",
                        "Could not find 'View details' after clicking Search.",
                    ],
                )
                return {"ok": False, "pdf_path": str(err), "error": "No view details", "stage": "results"}

            await page.wait_for_timeout(1200)


            # Download official PDF from the "Practitioner Details" modal
            stage = "download"

            # Wait for the modal/popup to be visible (it appears after clicking "View details")
            modal = page.locator("div[role='dialog'], .modal, [aria-modal='true']").first
            try:
                # Prefer heading text if present
                await page.locator("text=Practitioner Details").first.wait_for(timeout=12000)
            except Exception:
                # Still proceed; modal locator may work
                pass

            # Extract practitioner name (used for output filename)
            practitioner_name = ""
            try:
                # Scope to dialog if we have one
                scope = modal if await modal.count() else page
                txt = await scope.inner_text()
                m_name = re.search(r"\bName\b\s*\n\s*([^\n]+)", txt, flags=re.IGNORECASE)
                if m_name:
                    practitioner_name = m_name.group(1).strip()
                else:
                    # fallback: look for first strong-ish text line under the "Name" column
                    # This is intentionally loose; evidence snapshots will show correctness.
                    pass
            except Exception:
                practitioner_name = ""

            # Prepare target filename
            base_name = practitioner_name if practitioner_name else f"NMC-{pin}"
            target_filename = f"{base_name} nmc check.pdf"
            target_filename = _safe_filename(target_filename)
            target_path = (out_path_dir / target_filename)

            # Find the "Download a pdf" control inside the modal first
            download_locators = [
                (modal, "a:has-text('Download a pdf')"),
                (modal, "button:has-text('Download a pdf')"),
                (page, "a:has-text('Download a pdf')"),
                (page, "button:has-text('Download a pdf')"),
                (page, "a[href*='pdf=1' i]"),
            ]

            downloaded_path: Optional[Path] = None

            async def _save_pdf_bytes(pdf_bytes: bytes, pth: Path) -> None:
                pth.parent.mkdir(parents=True, exist_ok=True)
                with open(pth, "wb") as f:
                    f.write(pdf_bytes)

            # Attempt 1: real browser download
            for scope, sel in download_locators:
                try:
                    loc = scope.locator(sel).first
                    if not (await loc.count()):
                        continue
                    if not (await loc.is_visible()):
                        continue

                    # Try download event
                    try:
                        async with page.expect_download(timeout=15000) as dl_info:
                            await loc.click(timeout=8000)
                        download = await dl_info.value
                        await download.save_as(str(target_path))
                        downloaded_path = target_path
                        break
                    except PWTimeoutError:
                        pass

                    # Attempt 2: opens a new page/tab
                    try:
                        async with context.expect_page(timeout=7000) as pg_info:
                            await loc.click(timeout=8000)
                        pdf_page = await pg_info.value
                        await pdf_page.wait_for_load_state("domcontentloaded", timeout=15000)
                        pdf_url = pdf_page.url
                        resp = await context.request.get(pdf_url, timeout=20000)
                        if resp.ok:
                            b = await resp.body()
                            if b and len(b) > 1000:
                                await _save_pdf_bytes(b, target_path)
                                downloaded_path = target_path
                        await pdf_page.close()
                        if downloaded_path:
                            break
                    except PWTimeoutError:
                        pass
                    except Exception:
                        pass

                    # Attempt 3: same-page navigation to ?pdf=1
                    try:
                        before_url = page.url
                        await loc.click(timeout=8000)
                        await page.wait_for_timeout(1200)
                        after_url = page.url
                        if after_url != before_url and ("pdf" in after_url.lower() or "pdf=1" in after_url.lower()):
                            resp = await context.request.get(after_url, timeout=20000)
                            if resp.ok:
                                b = await resp.body()
                                if b and len(b) > 1000:
                                    await _save_pdf_bytes(b, target_path)
                                    downloaded_path = target_path
                            # Go back so we can still snapshot in case of error
                            try:
                                await page.go_back(timeout=10000)
                            except Exception:
                                pass
                            if downloaded_path:
                                break
                    except Exception:
                        pass

                except Exception:
                    continue

            if downloaded_path and downloaded_path.exists() and downloaded_path.stat().st_size > 0:

                await context.close()
                await browser.close()
                return {"ok": True, "pdf_path": str(downloaded_path), "error": "", "stage": "download"}

            # If no download event, try print-to-pdf of details page as fallback (still useful)
            snap = await _full_page_snapshot_pdf(
                page,
                out_path_dir,
                "NMC-details",
                "NMC Details Page",
                [
                    f"PIN: {pin}",
                    "Stage: download",
                    f"PIN filled as observed: {observed}",
                    "Could not trigger official 'Download PDF'. This is a full-page screenshot snapshot.",
                ],
            )
            await context.close()
            await browser.close()

            if snap:
                return {
                    "ok": False,
                    "pdf_path": str(snap),
                    "error": "Could not trigger official Download PDF; returned page snapshot PDF instead.",
                    "stage": "download",
                }

            err = _error_pdf(
                out_path_dir,
                "NMC Check Failed",
                [f"PIN: {pin}", "Stage: download", "Could not download official PDF and could not create page snapshot PDF."],
            )
            return {"ok": False, "pdf_path": str(err), "error": "Download failed", "stage": "download"}

    except Exception as e:
        # Always return an error PDF (never raise to app)
        err = _error_pdf(out_path_dir, "NMC Check Failed", [f"PIN: {pin}", "Stage: exception", f"Unexpected error: {e!r}"])
        return {"ok": False, "pdf_path": str(err), "error": str(e), "stage": "exception"}
