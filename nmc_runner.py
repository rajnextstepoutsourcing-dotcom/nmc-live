"""
nmc_runner.py (ASYNC) — cookie-gate fix

Fix:
- NMC disables the PIN input until Cookiebot consent is accepted.
  The PIN input shows class 'cookies-only-disabled' when blocked (seen in your snapshot PDF).
- This runner clicks Cookiebot "Allow all / I agree to all cookies" using robust selectors
  (including Cookiebot's known button id), then waits until #PinNumber is enabled.

Signature:
- async def run_nmc_check_and_download_pdf(nmc_pin: str, out_dir: str)

Success:
- Downloads and saves as "<Full Name> nmc check.pdf"

Failure:
- Returns a VISUAL snapshot PDF (screenshots stitched) containing URL + stage + error.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import List

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from pdf_utils import make_simple_error_pdf

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

NMC_URL = "https://www.nmc.org.uk/registration/search-the-register/"


def _sanitize_filename(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r'[\\/:*?"<>|]', "", s)
    return (s[:120].strip() or "NMC")


def _wrap(text: str, width: int) -> List[str]:
    words = (text or "").split()
    lines: List[str] = []
    cur: List[str] = []
    for w in words:
        if sum(len(x) for x in cur) + len(cur) + len(w) > width:
            lines.append(" ".join(cur))
            cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(" ".join(cur))
    return lines or [""]


def _make_snapshot_pdf(out_path: Path, *, url: str, stage: str, notes: List[str], image_paths: List[Path]) -> None:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    c = canvas.Canvas(str(out_path), pagesize=A4)
    w, h = A4

    # Cover page
    y = h - 72
    c.setFont("Helvetica-Bold", 16)
    c.drawString(72, y, "NMC automation snapshot")
    y -= 28
    c.setFont("Helvetica", 10)
    c.drawString(72, y, f"Stage: {stage}")
    y -= 14
    c.drawString(72, y, f"URL: {url}")
    y -= 18

    c.setFont("Helvetica", 10)
    for line in notes[:40]:
        for wrapped in _wrap(line, 95):
            if y < 72:
                c.showPage()
                y = h - 72
                c.setFont("Helvetica", 10)
            c.drawString(72, y, wrapped)
            y -= 12

    c.showPage()

    # Images
    for p in image_paths:
        try:
            img = ImageReader(str(p))
        except Exception:
            continue
        iw, ih = img.getSize()
        margin = 36
        max_w = w - 2 * margin
        max_h = h - 2 * margin
        scale = min(max_w / iw, max_h / ih)
        draw_w = iw * scale
        draw_h = ih * scale
        x = (w - draw_w) / 2
        y = (h - draw_h) / 2
        c.drawImage(img, x, y, width=draw_w, height=draw_h, preserveAspectRatio=True, mask="auto")
        c.showPage()

    c.save()


async def _save_shot(page, out_dir: Path, prefix: str, shots: List[Path]) -> None:
    p = out_dir / f"{prefix}_{int(time.time())}.png"
    await page.screenshot(path=str(p), full_page=True)
    shots.append(p)


async def _accept_cookies_and_wait_enable_pin(page, out_dir: Path, shots: List[Path]) -> None:
    """Accept Cookiebot consent (if present) and wait until PIN input is enabled.

    Why we do so much here:
    - On the NMC site, the PIN input is gated by Cookiebot and stays disabled with class
      'cookies-only-disabled' until consent is registered.
    - Pure "click the banner" is flaky in automation (late-load, iframe, overlay, focus traps).
    - So we do a 4-step fallback sequence:
        1) Click common Cookiebot accept buttons (page + iframes)
        2) Call Cookiebot JS APIs if present
        3) Set common consent cookies then reload
        4) (Last resort) remove the blocking class + hide overlay so the flow can proceed
    """
    await _save_shot(page, out_dir, "01_before_cookies", shots)

    cookie_selectors = [
        # Cookiebot common IDs (varies by site/config)
        "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
        "#CybotCookiebotDialogBodyButtonAccept",
        "#CybotCookiebotDialogBodyButtonAcceptAll",
        "#CybotCookiebotDialogBodyLevelButtonAccept",

        # Text fallbacks
        "button:has-text('I agree to all cookies')",
        "button:has-text('Agree to all cookies')",
        "button:has-text('Allow all')",
        "button:has-text('Accept all')",
        "button:has-text('Accept')",
    ]

    async def try_click_in_context(ctx) -> bool:
        for sel in cookie_selectors:
            loc = ctx.locator(sel).first
            try:
                if await loc.is_visible(timeout=1200):
                    await loc.click(timeout=8000, force=True)
                    return True
            except Exception:
                continue
        return False

    pin_loc = page.locator("#PinNumber").first
    await pin_loc.wait_for(state="visible", timeout=20000)

    async def pin_enabled() -> bool:
        cls = (await pin_loc.get_attribute("class")) or ""
        dis = await pin_loc.get_attribute("disabled")
        return (dis is None) and ("cookies-only-disabled" not in cls)

    async def wait_pin_enabled(ms_total: int) -> bool:
        end = time.time() + (ms_total / 1000.0)
        while time.time() < end:
            try:
                if await pin_enabled():
                    return True
            except Exception:
                pass
            await page.wait_for_timeout(350)
        return False

    # 1) Try clicking banner buttons on page and in iframes
    clicked = await try_click_in_context(page)
    if not clicked:
        for fr in page.frames:
            try:
                if await try_click_in_context(fr):
                    clicked = True
                    break
            except Exception:
                continue

    await page.wait_for_timeout(900)
    await _save_shot(page, out_dir, "02_after_cookie_click", shots)
    if await wait_pin_enabled(8000):
        return

    # 2) Try Cookiebot JS APIs (when available)
    try:
        await page.evaluate(
            """() => {
                try {
                    if (window.Cookiebot && typeof window.Cookiebot.submitCustomConsent === 'function') {
                        window.Cookiebot.submitCustomConsent(true, true, true);
                        return 'submitCustomConsent';
                    }
                    if (window.Cookiebot && typeof window.Cookiebot.submitConsent === 'function') {
                        window.Cookiebot.submitConsent(true, true, true);
                        return 'submitConsent';
                    }
                    if (window.Cookiebot && window.Cookiebot.consent) {
                        window.Cookiebot.consent.preferences = true;
                        window.Cookiebot.consent.statistics = true;
                        window.Cookiebot.consent.marketing = true;
                        return 'consentObject';
                    }
                } catch (e) {}
                return null;
            }"""
        )
    except Exception:
        pass

    await page.wait_for_timeout(900)
    await _save_shot(page, out_dir, "02c_after_cookiebot_js", shots)
    if await wait_pin_enabled(6000):
        return

    # 3) Set common consent cookies then reload.
    try:
        ctx = page.context
        domains = [".nmc.org.uk", "www.nmc.org.uk"]
        cookies = []
        for d in domains:
            cookies.extend(
                [
                    {"name": "CookieConsent", "value": "true", "domain": d, "path": "/"},
                    {"name": "CookiebotDialogClosed", "value": "true", "domain": d, "path": "/"},
                    {
                        "name": "CookiebotConsent",
                        "value": "preferences%3Dtrue%26statistics%3Dtrue%26marketing%3Dtrue",
                        "domain": d,
                        "path": "/",
                    },
                ]
            )
        await ctx.add_cookies(cookies)
    except Exception:
        pass

    try:
        await page.reload(wait_until="domcontentloaded", timeout=60000)
    except Exception:
        await page.goto(page.url, wait_until="domcontentloaded", timeout=60000)

    await page.wait_for_timeout(900)
    await _save_shot(page, out_dir, "02d_after_cookie_cookies_reload", shots)
    pin_loc = page.locator("#PinNumber").first
    await pin_loc.wait_for(state="visible", timeout=20000)
    if await wait_pin_enabled(9000):
        return

    # 4) LAST RESORT: remove the client-side gate + overlay.
    try:
        await page.evaluate(
            """() => {
                const pin = document.querySelector('#PinNumber');
                if (pin) {
                    pin.classList.remove('cookies-only-disabled');
                    pin.removeAttribute('disabled');
                }
                const ids = ['CybotCookiebotDialog','CybotCookiebotDialogBody','CybotCookiebotDialogBodyContent'];
                for (const id of ids) {
                    const el = document.getElementById(id);
                    if (el) el.remove();
                }
                const overlays = document.querySelectorAll('[id^="CybotCookiebot"], .CybotCookiebotDialog');
                overlays.forEach(e => { try { e.remove(); } catch(_) {} });
            }"""
        )
    except Exception:
        pass

    await page.wait_for_timeout(500)
    await _save_shot(page, out_dir, "02e_after_force_enable", shots)
    if await wait_pin_enabled(3000):
        return

    last_class = ""
    last_disabled = None
    try:
        last_class = (await pin_loc.get_attribute("class")) or ""
        last_disabled = await pin_loc.get_attribute("disabled")
    except Exception:
        pass
    raise RuntimeError(
        f"PIN input still disabled after cookie consent. disabled={last_disabled}, class='{last_class}'"
    )


async def _extract_name_from_modal(page) -> str:
    await page.get_by_text(re.compile(r"Practitioner\s+Details", re.I)).first.wait_for(timeout=20000)

    dialog = page.locator("div[role='dialog']").first
    try:
        text = await dialog.inner_text() if await dialog.is_visible(timeout=1500) else await page.inner_text("body")
    except Exception:
        text = await page.inner_text("body")

    m = re.search(r"\bName\b\s*[:\n]\s*([A-Za-z][A-Za-z .,'-]{1,80})", text)
    if not m:
        m = re.search(r"\bName\b\s+([A-Za-z][A-Za-z .,'-]{1,80})\s+\bGeograph", text, re.I)
    return (m.group(1).strip() if m else "NMC")


async def run_nmc_check_and_download_pdf(nmc_pin: str, out_dir: str):
    out_dir_path = Path(out_dir)
    out_dir_path.mkdir(parents=True, exist_ok=True)

    pin = (nmc_pin or "").strip().upper()
    if not pin:
        out = out_dir_path / "NMC-Error-Missing-PIN.pdf"
        make_simple_error_pdf(out, "NMC check failed", ["Missing NMC PIN."])
        return {"ok": False, "pdf_path": str(out), "stage": "missing_pin"}

    stage = "start"
    shots: List[Path] = []
    notes: List[str] = []
    current_url = NMC_URL

    try:
        async with async_playwright() as p:
            stage = "launch"
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
            )
            context = await browser.new_context(
                accept_downloads=True,
                viewport={"width": 1365, "height": 768},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()

            stage = "goto"
            await page.goto(NMC_URL, wait_until="domcontentloaded", timeout=60000)
            current_url = page.url

            stage = "cookies"
            await _accept_cookies_and_wait_enable_pin(page, out_dir_path, shots)

            stage = "fill_pin"
            pin_input = page.locator("#PinNumber").first
            await pin_input.scroll_into_view_if_needed(timeout=8000)

            # Type like a user (more reliable than fill on some cookie-gated/JS-heavy pages)
            await pin_input.click(timeout=20000, force=True)
            try:
                await pin_input.press("Control+A")
            except Exception:
                pass
            await pin_input.type(pin, delay=60)

            # Verify it actually went in; retry once if not
            try:
                val = await pin_input.input_value(timeout=2000)
            except Exception:
                val = ""

            if (val or "").strip().upper() != pin:
                await pin_input.click(timeout=10000, force=True)
                try:
                    await pin_input.press("Control+A")
                except Exception:
                    pass
                await pin_input.type(pin, delay=80)

            try:
                notes.append(f"PIN readback after type: '{await pin_input.input_value(timeout=2000)}'")
            except Exception:
                notes.append("PIN readback after type: (failed to read)")

            await _save_shot(page, out_dir_path, "03_after_pin_fill", shots)

            stage = "click_search"
            search_btn = page.get_by_role("button", name=re.compile(r"^Search$", re.I)).first
            await search_btn.scroll_into_view_if_needed(timeout=8000)
            await search_btn.wait_for(state="visible", timeout=25000)
            await search_btn.click(timeout=25000, force=True)

            await page.wait_for_timeout(1200)
            await _save_shot(page, out_dir_path, "04_after_search_click", shots)

            stage = "wait_results"
            try:
                await page.get_by_text(re.compile(r"Your\s+search\s+returned", re.I)).first.wait_for(timeout=30000)
            except Exception:
                await page.get_by_role("link", name=re.compile(r"View\s+details", re.I)).first.wait_for(timeout=30000)

            await _save_shot(page, out_dir_path, "05_results_visible", shots)

            stage = "view_details"
            view_details = page.get_by_role("link", name=re.compile(r"View\s+details", re.I)).first
            await view_details.scroll_into_view_if_needed(timeout=8000)
            await view_details.click(timeout=25000)

            await page.wait_for_timeout(900)
            await _save_shot(page, out_dir_path, "06_details_modal", shots)

            stage = "extract_name"
            name = await _extract_name_from_modal(page)
            out_pdf = out_dir_path / f"{_sanitize_filename(name)} nmc check.pdf"

            stage = "download_pdf"
            download_link = page.get_by_role("link", name=re.compile(r"Download\s+a\s+pdf", re.I)).first
            await download_link.scroll_into_view_if_needed(timeout=8000)

            try:
                async with page.expect_download(timeout=25000) as dl_info:
                    await download_link.click(timeout=25000)
                dl = await dl_info.value
                await dl.save_as(str(out_pdf))
            except PlaywrightTimeoutError:
                await download_link.click(timeout=25000)
                await page.wait_for_timeout(1500)
                current_url = page.url
                if "pdf=1" in current_url or current_url.lower().endswith(".pdf"):
                    resp = await context.request.get(current_url, timeout=30000)
                    if resp.ok:
                        out_pdf.write_bytes(await resp.body())
                    else:
                        raise RuntimeError(f"PDF fetch failed: HTTP {resp.status}")
                else:
                    raise RuntimeError("Download did not trigger and PDF URL not detected")

            await browser.close()

            if out_pdf.exists() and out_pdf.stat().st_size > 2000:
                return {"ok": True, "pdf_path": str(out_pdf), "name": name, "stage": "done"}

            raise RuntimeError("Downloaded PDF missing or too small")

    except Exception as e:
        try:
            snap = out_dir_path / f"NMC-Snapshot-{int(time.time())}.pdf"
            _make_snapshot_pdf(
                snap,
                url=current_url,
                stage=stage,
                notes=notes + [f"Error: {type(e).__name__}: {e}"],
                image_paths=shots,
            )
            return {"ok": False, "pdf_path": str(snap), "stage": stage, "error": str(e), "url": current_url}
        except Exception:
            out = out_dir_path / f"NMC-Error-{int(time.time())}.pdf"
            make_simple_error_pdf(out, "NMC check failed", [f"Stage: {stage}", str(e)])
            return {"ok": False, "pdf_path": str(out), "stage": stage, "error": str(e), "url": current_url}
