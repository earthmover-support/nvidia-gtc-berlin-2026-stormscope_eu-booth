#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright", "pillow"]
# ///
"""8-frame GIF of the booth page for LinkedIn: lead +0..+70 min next to panels 1..8.

Opens the booth page at 1920x1080, waits until the latest run has loaded, pauses the
forecast loop and the panel rotation, then for each frame scrubs to the next lead and
presses the right arrow for the next panel. If most of the true_color map is dark
(European evening/night), it switches to airmass first. Needs ffmpeg and Google Chrome.

  uv run presentation/record_gif.py                                  # hosted page
  uv run presentation/record_gif.py --url http://127.0.0.1:8000      # local server.py
  uv run presentation/record_gif.py --var airmass --out ~/Desktop/stormscope.gif
"""

import argparse
import subprocess
import tempfile
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

FRAMES = 8
DARK_LUMA = 40  # 0-255; true_color pixels darker than this count as night


def dark_fraction(png: Path, box) -> float:
    # ponytail: centre of the globe only, so the dark basemap around Europe doesn't count
    x, y, w, h = box["x"], box["y"], box["width"], box["height"]
    img = (
        Image.open(png)
        .convert("L")
        .crop((int(x + w / 4), int(y + h / 4), int(x + 3 * w / 4), int(y + 3 * h / 4)))
    )
    hist = img.histogram()
    return sum(hist[:DARK_LUMA]) / sum(hist)


def wait_loaded(page):
    page.wait_for_function(
        # a bare "Forecast" eyebrow means no run on screen yet; "· loading n/13" means leads missing
        """() => { const t = document.querySelector('#validEyebrow').textContent;
                   return t !== 'Forecast' && !/loading/.test(t); }""",
        timeout=180_000,
    )


def scrub(page, k):
    page.evaluate(
        "k => { const r = document.querySelector('#scrub'); r.value = String(k); r.dispatchEvent(new Event('input')); }",
        k,
    )


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default="https://nvidia-gtc-berlin-2026-stormscope-e.vercel.app/")
    ap.add_argument("--out", default="stormscope-booth.gif")
    ap.add_argument("--var", choices=["auto", "true_color", "airmass"], default="auto")
    ap.add_argument("--seconds", type=float, default=3.0, help="per frame")
    ap.add_argument("--width", type=int, default=1280, help="GIF width in px")
    ap.add_argument(
        "--settle", type=float, default=2.5, help="seconds for tiles/panel to settle per frame"
    )
    a = ap.parse_args()

    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
        tmp = Path(tmp)
        # real Chrome, headed: MapLibre needs WebGL, which headless renders slowly or not at all
        browser = p.chromium.launch(
            channel="chrome", headless=False, args=["--window-size=1920,1160"]
        )
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.goto(a.url)
        print("waiting for the latest run to load...")
        wait_loaded(page)
        page.keyboard.press("p")  # pause the forecast loop
        page.keyboard.press(" ")  # pause the panel rotation
        while page.text_content("#panelCount").split("/")[0].strip() != "1":
            page.keyboard.press("ArrowRight")

        var = a.var
        if var == "auto":
            scrub(page, FRAMES - 1)  # the darkest frame if the sun is setting
            page.wait_for_timeout(a.settle * 1000)
            page.screenshot(path=tmp / "probe.png")
            dark = dark_fraction(tmp / "probe.png", page.locator("#globe").bounding_box())
            var = "airmass" if dark > 0.5 else "true_color"
            print(f"true_color {dark:.0%} dark -> {var}")
        if var == "airmass":
            page.keyboard.press("a")
            page.wait_for_timeout(500)
            wait_loaded(page)

        for k in range(FRAMES):
            scrub(page, k)
            if k:
                page.keyboard.press("ArrowRight")
            page.wait_for_timeout(a.settle * 1000)
            page.screenshot(path=tmp / f"f{k}.png")
            print(
                f"frame {k + 1}/{FRAMES}: {page.text_content('#validEyebrow')} | panel {page.text_content('#panelCount')}"
            )
        browser.close()

        # one shared palette keeps colours steady between frames
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-framerate",
                f"1/{a.seconds}",
                "-i",
                str(tmp / "f%d.png"),
                "-vf",
                f"scale={a.width}:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=256[p];[b][p]paletteuse=dither=sierra2_4a",
                "-loop",
                "0",
                a.out,
            ],
            check=True,
        )
    print(f"{a.out}: {Path(a.out).stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
