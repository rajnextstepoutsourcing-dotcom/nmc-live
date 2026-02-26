import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

NMC_START_URL = "https://www.nmc.org.uk/registration/search-the-register/"


def _tag() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


async def _page_text(page) -> str:
    try:
        return (await page.inner_text("body")).strip()
    except Exception:
        try:
            return (await page.content())[:12000]
        except Exception:
            return ""


async def _click_if_exists(page, selectors, timeout_ms: int = 1500) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if await loc.count() > 0:
                await loc.first.click(timeout=timeout_ms)
                return True
        except Exception:
            continue
    return False


async def _detect_blocked(page) -> Optional[str]:
    txt = (await _page_text(page)).lower()
    keywords = [
        "verify you are not a robot",
        "captcha",
        "cloudflare",
        "attention required",
        "unusual traffic",
        "blocked",
        "challenge",
        "turnstile",
    ]
    if any(k in txt for k in keywords):
        return "Blocked by bot protection / CAPTCHA"

    selectors = [
        "iframe[src*='captcha']",
        "iframe[src*='challenge']",
        "input[name='cf-turnstile-response']",
        "div[id*='turnstile']",
        "div[class*='captcha']",
    ]
    for sel in selectors:
        try:
            if await page.locator(sel).count() > 0:
                return "Blocked by bot protection / CAPTCHA"
        except Exception:
            pass
    return None


async def _detect_invalid_pin_or_no_results(page) -> Optional[str]:
    txt = await _page_text(page)
    if re.search(r"provide a valid pin number", txt, re.I):
        return "Invalid PIN"
    if re.search(r"no results", txt, re.I):
        return "No results"
    return None


async def _save_snapshot_pdf(page, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        await page.emulate_media(media="screen")
    except Exception:
        pass
    # If this fails (rare), caller will still return a simple error PDF from app.py
    await page.pdf(path=str(out_path), format="A4", print_background=True)


async def run_nmc_check_and_download_pdf(
    *,
    nmc_pin: str,
    out_dir: str,
    timeout_ms: int = 60000,
) -> Dict[str, Any]:
    """Runs NMC register check and downloads the official PDF when possible.

    Always returns a PDF path in `pdf_path`:
      - official PDF on success
      - snapshot PDF on failure
    """
    out_dir_p = Path(out_dir)
    out_dir_p.mkdir(parents=True, exist_ok=True)

    tag = _tag()
    official_pdf = out_dir_p / f"NMC-Check-{tag}.pdf"
    error_pdf = out_dir_p / f"NMC-Error-{tag}.pdf"

    pin = (nmc_pin or "").strip().upper()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        try:
            await page.goto(NMC_START_URL, wait_until="domcontentloaded", timeout=timeout_ms)

            # Cookies banner (only if present)
            await _click_if_exists(
                page,
                [
                    "#onetrust-accept-btn-handler",
                    "button:has-text('Accept all cookies')",
                    "button:has-text('Accept cookies')",
                    "button:has-text('Accept all')",
                ],
            )

            blocked = await _detect_blocked(page)
            if blocked:
                await _save_snapshot_pdf(page, error_pdf)
                return {"ok": False, "error": blocked, "pdf_path": str(error_pdf)}

            # Fill PIN
            try:
                await page.get_by_label(re.compile(r"pin number", re.I)).fill(pin, timeout=8000)
            except Exception:
                await page.locator("input[type='text']").first.fill(pin, timeout=8000)

            # Search
            try:
                await page.get_by_role("button", name=re.compile(r"search", re.I)).click(timeout=15000)
            except Exception:
                await page.locator("button:has-text('Search')").first.click(timeout=15000)

            await page.wait_for_timeout(800)

            inv = await _detect_invalid_pin_or_no_results(page)
            if inv:
                await _save_snapshot_pdf(page, error_pdf)
                return {"ok": False, "error": inv, "pdf_path": str(error_pdf)}

            blocked = await _detect_blocked(page)
            if blocked:
                await _save_snapshot_pdf(page, error_pdf)
                return {"ok": False, "error": blocked, "pdf_path": str(error_pdf)}

            # View details
            try:
                await page.get_by_role("link", name=re.compile(r"view details", re.I)).first.click(timeout=25000)
            except Exception:
                await page.locator("a:has-text('View details')").first.click(timeout=25000)

            await page.wait_for_timeout(700)

            blocked = await _detect_blocked(page)
            if blocked:
                await _save_snapshot_pdf(page, error_pdf)
                return {"ok": False, "error": blocked, "pdf_path": str(error_pdf)}

            # Download official PDF
            try:
                async with page.expect_download(timeout=30000) as dl_info:
                    await page.get_by_role("link", name=re.compile(r"download.*pdf", re.I)).click(timeout=20000)
                download = await dl_info.value
                await download.save_as(str(official_pdf))
                if official_pdf.exists():
                    return {"ok": True, "pdf_path": str(official_pdf)}
            except Exception:
                pass

            # Fallback snapshot
            await _save_snapshot_pdf(page, error_pdf)
            return {"ok": False, "error": "PDF download failed", "pdf_path": str(error_pdf)}

        except PWTimeoutError:
            await _save_snapshot_pdf(page, error_pdf)
            return {"ok": False, "error": "Timeout", "pdf_path": str(error_pdf)}
        except Exception as e:
            await _save_snapshot_pdf(page, error_pdf)
            return {"ok": False, "error": f"Automation failed: {e}", "pdf_path": str(error_pdf)}
        finally:
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass
