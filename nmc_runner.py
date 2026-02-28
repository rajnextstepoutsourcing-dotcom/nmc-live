import os
import re
import time
from typing import Optional, Tuple

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from pdf_utils import make_error_pdf


NMC_SEARCH_URL = "https://www.nmc.org.uk/registration/search-the-register/"


def _maybe_accept_cookies(page) -> None:
    # Cookiebot common selectors
    candidates = [
        "#CookiebotDialogBodyButtonAccept",
        "button#CookiebotDialogBodyButtonAccept",
        "button:has-text('I agree to all cookies')",
        "button:has-text('Accept all cookies')",
        "button:has-text('Allow all cookies')",
        "button:has-text('I agree')",
        "button:has-text('Accept all')",
        "button:has-text('Agree')",
        # OneTrust common selectors
        "#onetrust-accept-btn-handler",
        "button#onetrust-accept-btn-handler",
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=3000)
                page.wait_for_timeout(600)
                return
        except Exception:
            pass

    # Cookiebot sometimes inside an iframe
    try:
        frames = page.frames
        for fr in frames:
            try:
                btn = fr.locator("#CookiebotDialogBodyButtonAccept").first
                if btn.count() > 0 and btn.is_visible():
                    btn.click(timeout=3000)
                    page.wait_for_timeout(600)
                    return
            except Exception:
                continue
    except Exception:
        pass


def _find_pin_input(page):
    # Prefer an input near the "Pin number" label
    xpaths = [
        "//label[contains(normalize-space(.), 'Pin number')]/following::input[1]",
        "//label[contains(translate(normalize-space(.), 'PIN', 'pin'), 'pin')]/following::input[1]",
        "//input[contains(translate(@name,'PIN','pin'),'pin')]",
        "//input[contains(translate(@id,'PIN','pin'),'pin')]",
        "//input[@type='search']",
        "//input[@type='text']",
    ]
    for xp in xpaths:
        try:
            loc = page.locator(f"xpath={xp}").first
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:
            continue
    return None


def _click_search(page) -> None:
    # Prefer a real Search button
    candidates = [
        "button:has-text('Search')",
        "input[type='submit'][value*='Search']",
        "a:has-text('Search')",
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_enabled():
                loc.click(timeout=5000)
                return
        except Exception:
            continue
    raise RuntimeError("Could not click Search button.")


def _is_bot_block(page) -> bool:
    txt = (page.inner_text("body") or "").lower()
    # Detect real block pages (not the informational sentence on the normal page)
    needles = [
        "verify you are not a robot",
        "are you a robot",
        "unusual traffic",
        "captcha",
        "please complete the security check",
        "access denied",
    ]
    return any(n in txt for n in needles)


def run_nmc_check_and_download_pdf(nmc_pin: str, output_pdf_path: str) -> Tuple[bool, str]:
    nmc_pin = (nmc_pin or "").strip()
    if not nmc_pin:
        make_error_pdf(output_pdf_path, "NMC check failed", "No NMC PIN provided.")
        return False, "No NMC PIN provided."

    # Basic sanity: allow the common format but don't hard fail
    if not re.fullmatch(r"[0-9]{2}[A-L][0-9]{4}[A-Z]", nmc_pin, flags=re.IGNORECASE):
        # continue anyway; user might have valid variants
        pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        try:
            page.goto(NMC_SEARCH_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(800)

            _maybe_accept_cookies(page)

            # If cookies not accepted, the page can show "To use this feature, please enable cookies."
            # Try accepting again if we still see it.
            try:
                if page.locator("text=To use this feature, please enable cookies.").count() > 0:
                    _maybe_accept_cookies(page)
                    page.wait_for_timeout(800)
            except Exception:
                pass

            if _is_bot_block(page):
                make_error_pdf(
                    output_pdf_path,
                    "NMC check blocked",
                    "The NMC website triggered a bot check / CAPTCHA. Please try again later or run the check manually.",
                )
                return False, "Blocked by bot check / CAPTCHA."

            pin_input = _find_pin_input(page)
            if pin_input is None:
                make_error_pdf(
                    output_pdf_path,
                    "NMC check failed",
                    "Could not find the PIN input on the NMC search page (possible cookie banner / layout change).",
                )
                return False, "PIN input not found."

            pin_input.fill(nmc_pin, timeout=8000)
            page.wait_for_timeout(300)
            _click_search(page)

            # Wait for results - "View details" is typically present for a match
            try:
                page.wait_for_selector("a:has-text('View details'), button:has-text('View details')", timeout=20000)
            except PWTimeout:
                if _is_bot_block(page):
                    make_error_pdf(
                        output_pdf_path,
                        "NMC check blocked",
                        "The NMC website triggered a bot check / CAPTCHA after searching. Please try again later.",
                    )
                    return False, "Blocked by bot check / CAPTCHA after searching."

                # No results or layout change - return error PDF with a helpful message
                make_error_pdf(
                    output_pdf_path,
                    "NMC check failed",
                    f"No result found or page did not load results for PIN: {nmc_pin}.",
                )
                return False, "No result / results page not reached."

            # Open details
            try:
                page.locator("a:has-text('View details'), button:has-text('View details')").first.click(timeout=15000)
            except Exception:
                make_error_pdf(
                    output_pdf_path,
                    "NMC check failed",
                    "Could not open 'View details' page (site layout may have changed).",
                )
                return False, "Could not open details."

            # Look for Download/Print PDF controls
            # We rely on the browser download if available; otherwise print-to-pdf fallback isn't always enabled in hosted chromium.
            try:
                with page.expect_download(timeout=30000) as dl_info:
                    # Try multiple labels
                    for sel in [
                        "a:has-text('Download PDF')",
                        "button:has-text('Download PDF')",
                        "a:has-text('Download')",
                        "button:has-text('Download')",
                        "a:has-text('Print')",
                        "button:has-text('Print')",
                    ]:
                        loc = page.locator(sel).first
                        if loc.count() > 0 and loc.is_visible():
                            loc.click()
                            break
                download = dl_info.value
                download.save_as(output_pdf_path)
                return True, "Success"
            except Exception:
                make_error_pdf(
                    output_pdf_path,
                    "NMC check failed",
                    "Reached the details page but could not download the official PDF (button not found or download blocked).",
                )
                return False, "Download PDF not found/blocked."

        except Exception as e:
            make_error_pdf(output_pdf_path, "NMC check failed", str(e))
            return False, str(e)
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass
