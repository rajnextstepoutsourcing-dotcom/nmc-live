import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

NMC_START_URL = "https://www.nmc.org.uk/registration/search-the-register/"


def _tag() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


async def _page_text(page) -> str:
    try:
        return (await page.inner_text("body")) or ""
    except Exception:
        return ""


async def _click_if_exists(page, selectors: List[str], timeout_ms: int = 1500) -> bool:
    """Try a list of selectors; click the first that is visible and enabled."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                continue
            if await loc.is_visible():
                try:
                    await loc.click(timeout=timeout_ms)
                    return True
                except Exception:
                    # Sometimes banner overlays intercept; try force click
                    try:
                        await loc.click(timeout=timeout_ms, force=True)
                        return True
                    except Exception:
                        continue
        except Exception:
            continue
    return False


async def _detect_blocked(page) -> Optional[str]:
    """Detect bot/captcha blocks or cookie-required blocks."""
    txt = (await _page_text(page)).lower()

    # NMC page itself warns about this message
    if "please verify you are not a robot" in txt or "verify you are not a robot" in txt:
        return "Blocked by bot protection ("Please verify you are not a robot")."

    # Common captcha terms
    if "captcha" in txt or "recaptcha" in txt or "are you a robot" in txt:
        return "Blocked by CAPTCHA / bot protection."

    # If cookies are required the search may not work
    if "enable cookies" in txt or "set cookie preferences" in txt:
        # Not always a hard block, but usually means banner must be accepted.
        return "Cookies not accepted / cookies required. Please accept cookies and try again."

    return None


async def _detect_invalid_pin_or_no_results(page) -> Optional[str]:
    txt = (await _page_text(page)).lower()
    if "no results" in txt:
        return "No results found for this PIN."
    if "enter at least one search field" in txt:
        return "Search fields were not filled (PIN not entered)."
    if "pin number" in txt and "search results" not in txt and "view details" not in txt:
        # Still on the search page; may indicate search did not run.
        return None
    return None


async def _save_snapshot_pdf(page, out_pdf: Path) -> None:
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    await page.pdf(path=str(out_pdf), format="A4", print_background=True)


async def _accept_cookies(page) -> None:
    """Try all known cookie banners on NMC site (Cookiebot/OneTrust/etc)."""
    # Cookiebot buttons often show: "I agree to all cookies"
    await _click_if_exists(
        page,
        [
            "button:has-text('I agree to all cookies')",
            "button:has-text('Agree to all')",
            "button:has-text('Accept all cookies')",
            "button:has-text('Accept cookies')",
            "button:has-text('Accept all')",
            "#onetrust-accept-btn-handler",
            "button#onetrust-accept-btn-handler",
            "button[aria-label*='Accept']",
        ],
        timeout_ms=2500,
    )
    # Some banners require scrolling into view
    try:
        await page.wait_for_timeout(300)
    except Exception:
        pass


async def _fill_pin(page, pin: str) -> bool:
    """Fill the PIN field robustly and verify the value actually landed."""
    pin = (pin or "").strip().upper()
    if not pin:
        return False

    # Strong selectors first: label, id/name contains pin, placeholder, aria-label
    candidates = [
        # label based
        ("label", None),
        # attribute based
        ("css", "input[id*='pin' i]"),
        ("css", "input[name*='pin' i]"),
        ("css", "input[aria-label*='pin' i]"),
        ("css", "input[placeholder*='pin' i]"),
        # If the form fields are the three text inputs, PIN is usually first
        ("css", "form input[type='text']"),
        ("css", "input[type='text']"),
    ]

    # 1) Try get_by_label for "Pin number"
    try:
        await page.get_by_label(re.compile(r"pin\s*number", re.I)).fill(pin, timeout=8000)
        # Verify
        try:
            val = await page.get_by_label(re.compile(r"pin\s*number", re.I)).input_value(timeout=2000)
            if val.strip().upper() == pin:
                return True
        except Exception:
            return True
    except Exception:
        pass

    # 2) Try CSS candidates and verify the field contains the PIN after filling
    for kind, sel in candidates:
        if kind == "label":
            continue
        try:
            loc = page.locator(sel)
            cnt = await loc.count()
            if cnt == 0:
                continue

            # Prefer the first visible enabled input
            chosen = None
            for i in range(min(cnt, 6)):
                cand = loc.nth(i)
                try:
                    if await cand.is_visible() and await cand.is_enabled():
                        chosen = cand
                        break
                except Exception:
                    continue
            if chosen is None:
                chosen = loc.first

            await chosen.fill(pin, timeout=8000)

            # Verify
            try:
                val = (await chosen.input_value(timeout=2000)).strip().upper()
                if val == pin:
                    return True
            except Exception:
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

    Always returns a PDF path in `pdf_path`:
      - official PDF on success
      - snapshot PDF on failure

    NOTE: NMC warns that if users see "Please verify you are not a robot",
    they must accept cookies for the service to work. fileciteturn5file1L6-L8
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

            # Always try to accept cookies first (Cookiebot/OneTrust)
            await _accept_cookies(page)

            # If still blocked, return snapshot
            blocked = await _detect_blocked(page)
            if blocked and "Cookies not accepted" not in blocked:
                await _save_snapshot_pdf(page, error_pdf)
                return {"ok": False, "error": blocked, "pdf_path": str(error_pdf)}

            # Fill PIN robustly
            filled = await _fill_pin(page, pin)
            if not filled:
                await _save_snapshot_pdf(page, error_pdf)
                return {"ok": False, "error": "Could not locate or fill the PIN field.", "pdf_path": str(error_pdf)}

            # Click Search (button)
            search_clicked = False
            try:
                await page.get_by_role("button", name=re.compile(r"search", re.I)).click(timeout=15000)
                search_clicked = True
            except Exception:
                try:
                    await page.locator("button:has-text('Search')").first.click(timeout=15000)
                    search_clicked = True
                except Exception:
                    search_clicked = False

            if not search_clicked:
                await _save_snapshot_pdf(page, error_pdf)
                return {"ok": False, "error": "Could not click the Search button.", "pdf_path": str(error_pdf)}

            # Wait for results: either 'View details' link, or some results container
            try:
                await page.wait_for_selector("a:has-text('View details')", timeout=25000)
            except Exception:
                # maybe blocked or no results; detect again
                await page.wait_for_timeout(800)

            inv = await _detect_invalid_pin_or_no_results(page)
            if inv:
                await _save_snapshot_pdf(page, error_pdf)
                return {"ok": False, "error": inv, "pdf_path": str(error_pdf)}

            blocked = await _detect_blocked(page)
            if blocked:
                # try accepting cookies one more time, then re-check
                await _accept_cookies(page)
                blocked2 = await _detect_blocked(page)
                if blocked2:
                    await _save_snapshot_pdf(page, error_pdf)
                    return {"ok": False, "error": blocked2, "pdf_path": str(error_pdf)}

            # View details
            try:
                await page.get_by_role("link", name=re.compile(r"view details", re.I)).first.click(timeout=25000)
            except Exception:
                await page.locator("a:has-text('View details')").first.click(timeout=25000)

            await page.wait_for_timeout(600)

            blocked = await _detect_blocked(page)
            if blocked:
                await _save_snapshot_pdf(page, error_pdf)
                return {"ok": False, "error": blocked, "pdf_path": str(error_pdf)}

            # Download official PDF
            try:
                async with page.expect_download(timeout=30000) as dl_info:
                    # Some pages use a button instead of link
                    try:
                        await page.get_by_role("link", name=re.compile(r"download.*pdf", re.I)).click(timeout=20000)
                    except Exception:
                        await page.locator("a:has-text('Download PDF'), button:has-text('Download PDF')").first.click(timeout=20000)
                download = await dl_info.value
                await download.save_as(str(official_pdf))
                if official_pdf.exists():
                    return {"ok": True, "pdf_path": str(official_pdf)}
            except Exception:
                pass

            await _save_snapshot_pdf(page, error_pdf)
            return {"ok": False, "error": "PDF download failed", "pdf_path": str(error_pdf)}

        except PWTimeoutError:
            await _save_snapshot_pdf(page, error_pdf)
            return {"ok": False, "error": "Timeout", "pdf_path": str(error_pdf)}
        except Exception as e:
            await _save_snapshot_pdf(page, error_pdf)
            return {"ok": False, "error": f"Unhandled error: {e}", "pdf_path": str(error_pdf)}
        finally:
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass
