import json
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

from pdf_utils import make_simple_error_pdf

NMC_START_URL = "https://www.nmc.org.uk/registration/search-the-register/"


def _tag() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _mk_lines(message: str, meta: Optional[Dict[str, Any]] = None) -> list[str]:
    lines = [message.strip()]
    if meta:
        try:
            lines.append("")
            lines.append("Details:")
            lines.append(json.dumps(meta, indent=2, ensure_ascii=False))
        except Exception:
            pass
    return lines


def _error_pdf(out_dir: Path, title: str, message: str, meta: Optional[Dict[str, Any]] = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / f"NMC-Error-{_tag()}.pdf"
    make_simple_error_pdf(pdf_path, title, _mk_lines(message, meta))
    return pdf_path


async def _page_text(page) -> str:
    try:
        return (await page.inner_text("body")).strip()
    except Exception:
        try:
            return (await page.content())[:12000]
        except Exception:
            return ""


async def _click_first(page, selectors: list[str], timeout_ms: int = 2500) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if await loc.count() > 0:
                await loc.first.click(timeout=timeout_ms)
                return True
        except Exception:
            continue
    return False


async def _accept_cookies(page) -> None:
    # Try multiple common cookie banners used on the site
    selectors = [
        "#onetrust-accept-btn-handler",
        "button:has-text('Accept all cookies')",
        "button:has-text('Accept cookies')",
        "button:has-text('Accept all')",
        "button:has-text('I agree')",
        "button:has-text('I agree to all cookies')",
        "button#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
        "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
        "button:has-text('Allow all')",
    ]
    try:
        await _click_first(page, selectors, timeout_ms=2500)
    except Exception:
        pass


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
        "enable cookies",
    ]
    if any(k in txt for k in keywords):
        return "Blocked by bot protection / CAPTCHA (or cookies not enabled)"

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


async def _detect_invalid_or_no_results(page) -> Optional[str]:
    txt = await _page_text(page)
    if re.search(r"provide a valid pin number", txt, re.I):
        return "Invalid PIN"
    if re.search(r"no results", txt, re.I):
        return "No results"
    return None


async def _save_snapshot_pdf(page, out_path: Path) -> bool:
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            await page.emulate_media(media="screen")
        except Exception:
            pass
        await page.pdf(path=str(out_path), format="A4", print_background=True)
        return out_path.exists()
    except Exception:
        return False


async def _fill_pin(page, pin: str) -> bool:
    # Try label-based fill first
    try:
        await page.get_by_label(re.compile(r"pin\s*number", re.I)).fill(pin, timeout=8000)
        return True
    except Exception:
        pass

    # Then try common input attributes
    candidates = [
        "input[name*='pin' i]",
        "input[id*='pin' i]",
        "input[aria-label*='pin' i]",
        "input[placeholder*='pin' i]",
        "input[type='text']",
        "input[type='search']",
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel)
            if await loc.count() == 0:
                continue
            el = loc.first
            await el.click(timeout=3000)
            await el.fill("")
            await el.type(pin, delay=35)
            # verify
            val = (await el.input_value()) if hasattr(el, "input_value") else ""
            if (val or "").strip().upper() == pin:
                return True
        except Exception:
            continue
    return False


async def run_nmc_check_and_download_pdf(
    *,
    nmc_pin: str,
    out_dir: str,
    timeout_ms: int = 60000,
) -> Dict[str, Any]:
    """Runs NMC register check and downloads the official PDF when possible.

    Always returns a dict containing `pdf_path`.
    """
    out_dir_p = Path(out_dir)
    out_dir_p.mkdir(parents=True, exist_ok=True)

    tag = _tag()
    official_pdf = out_dir_p / f"NMC-Check-{tag}.pdf"
    snap_pdf = out_dir_p / f"NMC-Snapshot-{tag}.pdf"

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
            await page.wait_for_timeout(800)

            await _accept_cookies(page)
            await page.wait_for_timeout(500)

            blocked = await _detect_blocked(page)
            if blocked:
                # Site itself says cookies may need to be enabled.
                pdf_path = _error_pdf(out_dir_p, "NMC Check Blocked", blocked, {"pin": pin, "stage": "landing"})
                return {"ok": False, "error": blocked, "pdf_path": str(pdf_path)}

            # Fill PIN
            filled = await _fill_pin(page, pin)
            if not filled:
                pdf_path = _error_pdf(out_dir_p, "NMC Check Failed", "Could not find or fill the PIN input field.", {"pin": pin})
                return {"ok": False, "error": "PIN field not found", "pdf_path": str(pdf_path)}

            # Click search
            try:
                await page.get_by_role("button", name=re.compile(r"search", re.I)).click(timeout=15000)
            except Exception:
                try:
                    await page.locator("button:has-text('Search')").first.click(timeout=15000)
                except Exception:
                    pdf_path = _error_pdf(out_dir_p, "NMC Check Failed", "Could not click the Search button.", {"pin": pin})
                    return {"ok": False, "error": "Search click failed", "pdf_path": str(pdf_path)}

            await page.wait_for_timeout(1200)

            inv = await _detect_invalid_or_no_results(page)
            if inv:
                # snapshot helps show on-page message
                if await _save_snapshot_pdf(page, snap_pdf):
                    return {"ok": False, "error": inv, "pdf_path": str(snap_pdf)}
                pdf_path = _error_pdf(out_dir_p, "NMC Check Result", inv, {"pin": pin})
                return {"ok": False, "error": inv, "pdf_path": str(pdf_path)}

            blocked = await _detect_blocked(page)
            if blocked:
                if await _save_snapshot_pdf(page, snap_pdf):
                    return {"ok": False, "error": blocked, "pdf_path": str(snap_pdf)}
                pdf_path = _error_pdf(out_dir_p, "NMC Check Blocked", blocked, {"pin": pin, "stage": "after_search"})
                return {"ok": False, "error": blocked, "pdf_path": str(pdf_path)}

            # View details
            try:
                await page.get_by_role("link", name=re.compile(r"view details", re.I)).first.click(timeout=25000)
            except Exception:
                try:
                    await page.locator("a:has-text('View details')").first.click(timeout=25000)
                except Exception:
                    # If no view-details, snapshot current
                    if await _save_snapshot_pdf(page, snap_pdf):
                        return {"ok": False, "error": "View details not found", "pdf_path": str(snap_pdf)}
                    pdf_path = _error_pdf(out_dir_p, "NMC Check Failed", "Could not open 'View details'.", {"pin": pin})
                    return {"ok": False, "error": "View details not found", "pdf_path": str(pdf_path)}

            await page.wait_for_timeout(1000)

            blocked = await _detect_blocked(page)
            if blocked:
                if await _save_snapshot_pdf(page, snap_pdf):
                    return {"ok": False, "error": blocked, "pdf_path": str(snap_pdf)}
                pdf_path = _error_pdf(out_dir_p, "NMC Check Blocked", blocked, {"pin": pin, "stage": "details"})
                return {"ok": False, "error": blocked, "pdf_path": str(pdf_path)}

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
            if await _save_snapshot_pdf(page, snap_pdf):
                return {"ok": False, "error": "PDF download failed", "pdf_path": str(snap_pdf)}
            pdf_path = _error_pdf(out_dir_p, "NMC Check Failed", "PDF download failed and snapshot capture failed.", {"pin": pin})
            return {"ok": False, "error": "PDF download failed", "pdf_path": str(pdf_path)}

        except PWTimeoutError:
            if await _save_snapshot_pdf(page, snap_pdf):
                return {"ok": False, "error": "Timeout", "pdf_path": str(snap_pdf)}
            pdf_path = _error_pdf(out_dir_p, "NMC Check Timeout", "Timed out while running the NMC check.", {"pin": pin})
            return {"ok": False, "error": "Timeout", "pdf_path": str(pdf_path)}
        except Exception as e:
            if await _save_snapshot_pdf(page, snap_pdf):
                return {"ok": False, "error": f"Automation failed: {e}", "pdf_path": str(snap_pdf)}
            pdf_path = _error_pdf(out_dir_p, "NMC Check Failed", f"Unexpected error: {e}", {"pin": pin})
            return {"ok": False, "error": f"Automation failed: {e}", "pdf_path": str(pdf_path)}
        finally:
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass
