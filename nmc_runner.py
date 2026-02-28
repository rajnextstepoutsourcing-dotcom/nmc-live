"""
nmc_runner.py

Compatibility runner:
- Accepts out_dir (used by app.py) plus aliases to avoid TypeError.
- Always returns {"ok": bool, "pdf_path": str, "error": str|None}

Uses pdf_utils.make_simple_error_pdf for failures.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Optional

from pdf_utils import make_simple_error_pdf
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

NMC_SEARCH_URL = "https://www.nmc.org.uk/registration/search-the-register/"


def _sanitize_pin(pin: str) -> str:
    pin = (pin or "").strip()
    pin = re.sub(r"[\s\-_/]", "", pin)
    return pin


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _error_pdf(out_dir: Path, title: str, message: str, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    _ensure_dir(out_dir)
    pdf_path = out_dir / "NMC-Error.pdf"
    extra = ""
    if meta:
        lines = [f"{k}: {v}" for k, v in meta.items()]
        extra = "\n\n" + "\n".join(lines)
    make_simple_error_pdf(str(pdf_path), title=title, message=message + extra)
    return {"ok": False, "pdf_path": str(pdf_path), "error": message}


async def _click_first(page, selectors, timeout=5000) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click(timeout=timeout)
                return True
        except Exception:
            pass
    return False


async def _accept_cookies(page) -> bool:
    selectors = [
        "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
        "button:has-text('I agree to all cookies')",
        "button:has-text('Accept all cookies')",
        "button#onetrust-accept-btn-handler",
        "button:has-text('Accept all')",
    ]
    return await _click_first(page, selectors, timeout=8000)


async def _detect_bot_or_captcha(page) -> Optional[str]:
    try:
        html = (await page.content()).lower()
    except Exception:
        return None
    cues = [
        "please verify you are not a robot",
        "verify you are not a robot",
        "captcha",
        "robot check",
        "enable cookies",
        "access denied",
        "unusual traffic",
    ]
    for cue in cues:
        if cue in html:
            if "enable cookies" in cue:
                return "NMC site indicates cookies must be enabled/accepted to use search."
            return "Blocked by bot protection (e.g., 'Please verify you are not a robot')."
    return None


async def run_nmc_check_and_download_pdf(
    nmc_pin: str,
    out_dir: Optional[str] = None,
    output_dir: Optional[str] = None,
    output_pdf_path: Optional[str] = None,
    **_kwargs,
) -> Dict[str, Any]:
    """
    NOTE: out_dir is supported for compatibility with your existing app.py.
    """
    pin = _sanitize_pin(nmc_pin)

    # Resolve output path
    if output_pdf_path:
        pdf_path = Path(output_pdf_path)
        out_path_dir = pdf_path.parent
    else:
        out_path_dir = Path(out_dir or output_dir or "output").resolve()
        pdf_path = out_path_dir / "NMC-Register.pdf"

    if not pin:
        return _error_pdf(out_path_dir, "NMC Check Failed", "No PIN provided.", {"pin": nmc_pin})

    _ensure_dir(out_path_dir)

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 720},
                locale="en-GB",
            )
            page = await context.new_page()
            await page.goto(NMC_SEARCH_URL, wait_until="domcontentloaded", timeout=45000)

            # cookies
            await _accept_cookies(page)
            await page.wait_for_timeout(800)
            await _accept_cookies(page)

            bot = await _detect_bot_or_captcha(page)
            if bot:
                await context.close()
                await browser.close()
                return _error_pdf(out_path_dir, "NMC Check Blocked", bot, {"pin": pin, "stage": "landing"})

            # fill pin
            pin_selectors = [
                "input[aria-label*='Pin' i]",
                "input[placeholder*='Pin' i]",
                "input[id*='pin' i]",
                "input[name*='pin' i]",
                "xpath=//label[contains(translate(., 'PIN', 'pin'),'pin')]/following::input[1]",
            ]
            filled = False
            for sel in pin_selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() == 0:
                        continue
                    await loc.scroll_into_view_if_needed()
                    await loc.click(timeout=5000)
                    await loc.fill(pin, timeout=5000)
                    val = await loc.input_value()
                    if (val or "").replace(" ", "") == pin:
                        filled = True
                        break
                except Exception:
                    continue
            if not filled:
                bot = await _detect_bot_or_captcha(page)
                if bot:
                    await context.close()
                    await browser.close()
                    return _error_pdf(out_path_dir, "NMC Check Blocked", bot, {"pin": pin, "stage": "pin_fill"})
                await context.close()
                await browser.close()
                return _error_pdf(out_path_dir, "NMC Check Failed", "Could not locate/fill the PIN field.", {"pin": pin})

            # search
            search_selectors = [
                "button:has-text('Search')",
                "input[type='submit'][value*='Search' i]",
                "xpath=//button[contains(.,'Search')]",
            ]
            if not await _click_first(page, search_selectors, timeout=12000):
                await context.close()
                await browser.close()
                return _error_pdf(out_path_dir, "NMC Check Failed", "Could not click Search.", {"pin": pin})

            await page.wait_for_timeout(1200)

            bot = await _detect_bot_or_captcha(page)
            if bot:
                await context.close()
                await browser.close()
                return _error_pdf(out_path_dir, "NMC Check Blocked", bot, {"pin": pin, "stage": "after_search"})

            # view details
            view_sel = "a:has-text('View details'), button:has-text('View details')"
            try:
                await page.wait_for_selector(view_sel, timeout=20000)
            except PlaywrightTimeoutError:
                await context.close()
                await browser.close()
                return _error_pdf(out_path_dir, "NMC Check Failed", "Timed out waiting for results / View details.", {"pin": pin})

            await page.locator(view_sel).first.click(timeout=15000)
            await page.wait_for_timeout(1200)

            bot = await _detect_bot_or_captcha(page)
            if bot:
                await context.close()
                await browser.close()
                return _error_pdf(out_path_dir, "NMC Check Blocked", bot, {"pin": pin, "stage": "details"})

            # download pdf
            download_selectors = [
                "a:has-text('Download PDF')",
                "button:has-text('Download PDF')",
                "a[href*='.pdf' i]",
            ]
            try:
                async with page.expect_download(timeout=30000) as dl_info:
                    ok = await _click_first(page, download_selectors, timeout=15000)
                    if not ok:
                        raise PlaywrightTimeoutError("Download control not clickable")
                download = await dl_info.value
                await download.save_as(str(pdf_path))
            except Exception as e:
                await context.close()
                await browser.close()
                return _error_pdf(out_path_dir, "NMC Check Failed", f"Could not download PDF: {e}", {"pin": pin})

            await context.close()
            await browser.close()

        return {"ok": True, "pdf_path": str(pdf_path), "error": None}

    except Exception as e:
        return _error_pdf(out_path_dir, "NMC Check Failed", f"Unexpected error: {e}", {"pin": pin})
