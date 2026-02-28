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

from pdf_utils import make_simple_error_pdf


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


async def _page_snapshot_pdf(page, out_dir: Path, prefix: str) -> Optional[Path]:
    """
    Best effort: generate a PDF of the currently rendered page using Chromium's print-to-PDF.
    This is perfect for 'why did it stop' debugging.

    Returns path if created, else None.
    """
    _ensure_dir(out_dir)
    pdf_path = out_dir / f"{_safe_filename(prefix)}-{_now_tag()}.pdf"
    try:
        await page.pdf(
            path=str(pdf_path),
            format="A4",
            print_background=True,
            margin={"top": "10mm", "bottom": "10mm", "left": "10mm", "right": "10mm"},
        )
        if pdf_path.exists() and pdf_path.stat().st_size > 0:
            return pdf_path
    except Exception:
        # Fallback: try screenshot to help log, but still return None if PDF can't be made.
        try:
            await page.screenshot(path=str(out_dir / f"{_safe_filename(prefix)}-{_now_tag()}.png"), full_page=True)
        except Exception:
            pass
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
    candidates = [
        "input[name*='pin' i]",
        "input[id*='pin' i]",
        "input[aria-label*='pin' i]",
        "input[placeholder*='pin' i]",
        "input[type='text']",
        "input[type='search']",
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
    selectors = [
        "button:has-text('Search')",
        "input[type='submit'][value*='Search' i]",
        "button[type='submit']",
        "form button",
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


async def run_nmc_check_and_download_pdf(
    nmc_pin: str,
    out_dir: Optional[str] = None,
    output_dir: Optional[str] = None,
    output_pdf_path: Optional[str] = None,
    **kwargs,
) -> Dict[str, Any]:
    """
    Returns:
      { ok: bool, pdf_path: str, error: str, stage: str }

    Compatibility:
      app.py may call with out_dir=...
    """
    pin = (nmc_pin or "").strip()
    out_path_dir = Path(out_dir or output_dir or "output")
    _ensure_dir(out_path_dir)

    # If app passes a full pdf path, honor it (best effort)
    forced_pdf_path = Path(output_pdf_path) if output_pdf_path else None

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
                snap = await _page_snapshot_pdf(page, out_path_dir, "NMC-captcha")
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
                snap = await _page_snapshot_pdf(page, out_path_dir, "NMC-no-pin-field")
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
                snap = await _page_snapshot_pdf(page, out_path_dir, "NMC-captcha-after-search")
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
                snap = await _page_snapshot_pdf(page, out_path_dir, "NMC-results-not-found")
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

            # Download PDF
            stage = "download"
            dl_selectors = [
                "a:has-text('Download PDF')",
                "button:has-text('Download PDF')",
                "a[href*='.pdf' i]:has-text('Download')",
                "a:has-text('Download')",
            ]

            # Sometimes it opens in same tab; prefer expect_download
            downloaded_path: Optional[Path] = None
            for sel in dl_selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() and await loc.is_visible():
                        async with page.expect_download(timeout=20000) as dl_info:
                            await loc.click(timeout=8000)
                        download = await dl_info.value
                        suggested = download.suggested_filename or "nmc-register.pdf"
                        target_dir = _ensure_dir(out_path_dir)
                        target_path = forced_pdf_path or (target_dir / _safe_filename(suggested))
                        await download.save_as(str(target_path))
                        downloaded_path = target_path
                        break
                except PWTimeoutError:
                    continue
                except Exception:
                    continue

            if downloaded_path and downloaded_path.exists() and downloaded_path.stat().st_size > 0:
                await context.close()
                await browser.close()
                return {"ok": True, "pdf_path": str(downloaded_path), "error": "", "stage": "download"}

            # If no download event, try print-to-pdf of details page as fallback (still useful)
            snap = await _page_snapshot_pdf(page, out_path_dir, "NMC-details")
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
