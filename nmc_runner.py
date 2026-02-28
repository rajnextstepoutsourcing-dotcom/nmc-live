"""
nmc_runner.py (ASYNC)

Automates: https://www.nmc.org.uk/registration/search-the-register/

Flow:
1) Open page
2) Accept cookies (if present)
3) Fill "Pin number"
4) Click Search
5) Click "View details"
6) In modal "Practitioner Details": read Name + click "Download a pdf"
7) Save as "<Name> nmc check.pdf" into out_dir

On ANY failure:
- Generates a FULL-PAGE VISUAL SNAPSHOT PDF (screenshots, not print-PDF),
  including current URL + stage + key notes.
- Returns {"ok": False, "pdf_path": "<snapshot_pdf_path>", ...}

Required signature:
- run_nmc_check_and_download_pdf(nmc_pin, out_dir)
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from pdf_utils import make_simple_error_pdf

# ReportLab is already used in pdf_utils and available in this project.
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader


NMC_URL = "https://www.nmc.org.uk/registration/search-the-register/"


def _sanitize_filename(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    # Remove characters that are problematic on Windows/Linux
    s = re.sub(r'[\\/:*?"<>|]', "", s)
    return s[:120].strip() or "NMC"


def _make_snapshot_pdf(
    out_path: Path,
    *,
    title: str,
    url: str,
    stage: str,
    notes: List[str],
    image_paths: List[Path],
) -> None:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    c = canvas.Canvas(str(out_path), pagesize=A4)
    w, h = A4

    # Cover page (text)
    y = h - 72
    c.setFont("Helvetica-Bold", 16)
    c.drawString(72, y, title)
    y -= 28
    c.setFont("Helvetica", 10)
    c.drawString(72, y, f"Stage: {stage}")
    y -= 14
    c.drawString(72, y, f"URL: {url}")
    y -= 18

    c.setFont("Helvetica", 10)
    for line in notes[:30]:
        for wrapped in _wrap(line, 95):
            if y < 72:
                c.showPage()
                y = h - 72
                c.setFont("Helvetica", 10)
            c.drawString(72, y, wrapped)
            y -= 12

    c.showPage()

    # Image pages
    for p in image_paths:
        try:
            img = ImageReader(str(p))
        except Exception:
            continue

        # Fit image onto A4 keeping aspect ratio
        iw, ih = img.getSize()
        margin = 36
        max_w = w - 2 * margin
        max_h = h - 2 * margin
        scale = min(max_w / iw, max_h / ih)
        draw_w = iw * scale
        draw_h = ih * scale
        x = (w - draw_w) / 2
        y = (h - draw_h) / 2
        c.drawImage(img, x, y, width=draw_w, height=draw_h, preserveAspectRatio=True, mask='auto')
        c.showPage()

    c.save()


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


async def _maybe_accept_cookies(page, shots: List[Path], out_dir: Path) -> None:
    # Cookiebot banner commonly has button text "I agree to all cookies"
    candidates = [
        page.get_by_role("button", name=re.compile(r"I\s*agree\s*to\s*all\s*cookies", re.I)),
        page.get_by_role("button", name=re.compile(r"Allow\s+all", re.I)),
        page.get_by_text(re.compile(r"I\s*agree\s*to\s*all\s*cookies", re.I)),
    ]
    for loc in candidates:
        try:
            if await loc.first.is_visible(timeout=1500):
                await loc.first.click(timeout=3000)
                await page.wait_for_timeout(800)
                # Save evidence after cookie click
                p = out_dir / f"02_after_cookies_{int(time.time())}.png"
                await page.screenshot(path=str(p), full_page=True)
                shots.append(p)
                return
        except Exception:
            continue


async def _find_pin_input(page):
    # Best: use label "Pin number"
    try:
        loc = page.get_by_label(re.compile(r"Pin\s*number", re.I))
        await loc.first.wait_for(state="visible", timeout=5000)
        return loc.first
    except Exception:
        pass

    # Fallback: input near text "Pin number"
    candidates = [
        "input[name*='pin' i]",
        "input[id*='pin' i]",
        "input[aria-label*='pin' i]",
        "input[placeholder*='pin' i]",
    ]
    for sel in candidates:
        loc = page.locator(sel)
        try:
            if await loc.first.is_visible(timeout=1500):
                return loc.first
        except Exception:
            continue

    raise RuntimeError("Could not locate PIN input field")


async def _extract_name_from_modal(page) -> str:
    # Wait for modal heading
    await page.get_by_text(re.compile(r"Practitioner\s+Details", re.I)).first.wait_for(timeout=15000)

    # Grab some nearby text and regex Name
    # The modal content varies, so we search the whole page but prefer modal containers.
    modal_candidates = [
        page.locator("div[role='dialog']"),
        page.locator(".modal"),
        page.locator(".c-modal"),
        page.locator("section[aria-modal='true']"),
    ]

    text = ""
    for loc in modal_candidates:
        try:
            if await loc.first.is_visible(timeout=1500):
                text = await loc.first.inner_text()
                if text.strip():
                    break
        except Exception:
            continue

    if not text.strip():
        # last resort
        text = await page.inner_text("body")

    # Pattern: "Name\nBalkar Singh" or "Name Balkar Singh"
    m = re.search(r"\bName\b\s*[:\n]\s*([A-Za-z][A-Za-z .,'-]{1,80})", text)
    if not m:
        # Sometimes just the name is first cell under Name header; try table-like pattern
        m = re.search(r"\bName\b\s+([A-Za-z][A-Za-z .,'-]{1,80})\s+\bGeograph", text, re.I)
    if not m:
        return "NMC"

    return m.group(1).strip()


async def run_nmc_check_and_download_pdf(nmc_pin: str, out_dir: str):
    out_dir_path = Path(out_dir)
    out_dir_path.mkdir(parents=True, exist_ok=True)

    stage = "start"
    shots: List[Path] = []
    notes: List[str] = []
    current_url = ""

    # Defensive normalize
    pin = (nmc_pin or "").strip().upper()
    if not pin:
        out = out_dir_path / "NMC-Error-Missing-PIN.pdf"
        make_simple_error_pdf(out, "NMC check failed", ["Missing NMC PIN."])
        return {"ok": False, "pdf_path": str(out), "stage": "missing_pin"}

    try:
        async with async_playwright() as p:
            stage = "launch"
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
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()

            stage = "goto"
            await page.goto(NMC_URL, wait_until="domcontentloaded", timeout=60000)
            current_url = page.url
            p1 = out_dir_path / f"01_loaded_{int(time.time())}.png"
            await page.screenshot(path=str(p1), full_page=True)
            shots.append(p1)

            stage = "cookies"
            await _maybe_accept_cookies(page, shots, out_dir_path)
            current_url = page.url

            stage = "fill_pin"
            pin_input = await _find_pin_input(page)
            await pin_input.click(timeout=5000)
            await pin_input.fill(pin, timeout=10000)
            # Read back value for proof
            try:
                val = await pin_input.input_value(timeout=2000)
                notes.append(f"PIN readback after fill: '{val}'")
            except Exception:
                notes.append("PIN readback after fill: (failed to read)")

            p2 = out_dir_path / f"03_after_pin_{int(time.time())}.png"
            await page.screenshot(path=str(p2), full_page=True)
            shots.append(p2)

            stage = "click_search"
            search_btn = page.get_by_role("button", name=re.compile(r"^Search$", re.I))
            await search_btn.first.click(timeout=15000)

            p3 = out_dir_path / f"04_after_search_click_{int(time.time())}.png"
            await page.wait_for_timeout(1200)
            await page.screenshot(path=str(p3), full_page=True)
            shots.append(p3)

            stage = "wait_results"
            # Wait until "View details" appears or results header appears
            try:
                await page.get_by_text(re.compile(r"Your\s+search\s+returned", re.I)).first.wait_for(timeout=20000)
            except Exception:
                # fallback: view details link
                await page.get_by_role("link", name=re.compile(r"View\s+details", re.I)).first.wait_for(timeout=20000)

            p4 = out_dir_path / f"05_results_{int(time.time())}.png"
            await page.screenshot(path=str(p4), full_page=True)
            shots.append(p4)

            stage = "view_details"
            await page.get_by_role("link", name=re.compile(r"View\s+details", re.I)).first.click(timeout=15000)
            await page.wait_for_timeout(800)

            p5 = out_dir_path / f"06_details_popup_{int(time.time())}.png"
            await page.screenshot(path=str(p5), full_page=True)
            shots.append(p5)

            stage = "extract_name"
            name = await _extract_name_from_modal(page)
            safe_name = _sanitize_filename(name)
            out_pdf = out_dir_path / f"{safe_name} nmc check.pdf"

            stage = "download_pdf"
            download_link = page.get_by_role("link", name=re.compile(r"Download\s+a\s+pdf", re.I))
            # Try download event first
            try:
                async with page.expect_download(timeout=15000) as dl_info:
                    await download_link.first.click(timeout=15000)
                dl = await dl_info.value
                await dl.save_as(str(out_pdf))
            except PlaywrightTimeoutError:
                # Some cases open the PDF in same tab (e.g., ?pdf=1). Detect and fetch.
                try:
                    await download_link.first.click(timeout=15000)
                except Exception:
                    pass
                await page.wait_for_timeout(1200)
                current_url = page.url
                # If the click navigated to a PDF url, fetch it
                if "pdf=1" in current_url or current_url.lower().endswith(".pdf"):
                    resp = await context.request.get(current_url, timeout=30000)
                    if resp.ok:
                        data = await resp.body()
                        out_pdf.write_bytes(data)
                    else:
                        raise RuntimeError(f"PDF fetch failed: HTTP {resp.status}")
                else:
                    raise RuntimeError("Download did not trigger and PDF URL not detected")

            stage = "done"
            current_url = page.url
            await browser.close()

            if out_pdf.exists() and out_pdf.stat().st_size > 2000:
                return {"ok": True, "pdf_path": str(out_pdf), "name": name, "stage": stage}

            raise RuntimeError("Downloaded PDF missing or too small")

    except Exception as e:
        # Always return a visual snapshot PDF with URL + stage
        try:
            current_url = current_url or NMC_URL
            notes2 = notes + [f"Error: {type(e).__name__}: {e}"]
            snap_pdf = out_dir_path / f"NMC-Snapshot-{int(time.time())}.pdf"
            _make_snapshot_pdf(
                snap_pdf,
                title="NMC automation snapshot",
                url=current_url,
                stage=stage,
                notes=notes2,
                image_paths=shots,
            )
            return {"ok": False, "pdf_path": str(snap_pdf), "stage": stage, "error": str(e), "url": current_url}
        except Exception:
            # Last resort: simple error PDF
            out = out_dir_path / f"NMC-Error-{int(time.time())}.pdf"
            make_simple_error_pdf(out, "NMC check failed", [f"Stage: {stage}", str(e)])
            return {"ok": False, "pdf_path": str(out), "stage": stage, "error": str(e), "url": current_url or NMC_URL}
