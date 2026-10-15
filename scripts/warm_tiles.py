"""Render each new run's tiles once through the site's Vercel proxy, before visitors ask.

Vercel's CDN does not merge simultaneous requests for an uncached tile, so N visitors
opening a new run together make Flux render each tile N times (10 clients: 1,427 renders
for 234 tiles, 2026-10-15). This loop asks for every tile of the default view once, as soon
as an init shows up in the run list, so visitors get cache hits.

    uv run --no-project python scripts/warm_tiles.py https://<site>.vercel.app

The URLs must match `tileUrl` in site/index.html byte for byte: the query is part of the
cache key.
"""

import math
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

REPO = "stormscope-eu_gtc_berlin_2026_EU_region"
VARIABLES = ["true_color", "airmass"]  # the page's default first
# What phones and desktops see at the page's initial bounds ([-5, 44, 26, 58]): z2-z4
# over a box wide enough for a 1920 px screen. Panning and zooming in still miss.
BOX, ZOOMS = (-25, 35, 45, 65), (2, 3, 4)
IN_FLIGHT = 8  # one browser's worth; more only queues on Flux
POLL_S = 20


def tile_x(lon, z):
    return int((lon + 180) / 360 * 2**z)


def tile_y(lat, z):
    return int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * 2**z)


def tiles():
    west, south, east, north = BOX
    for z in ZOOMS:
        for ty in range(tile_y(north, z), tile_y(south, z) + 1):
            for tx in range(tile_x(west, z), tile_x(east, z) + 1):
                yield z, ty, tx


def get(url):
    with urllib.request.urlopen(url, timeout=120) as r:
        r.read()
        return r.status, r.headers.get("x-vercel-cache")


def run_list(host):
    """(newest init, leads) from the dataset tree, parsed like loadCoords in index.html."""
    html = (
        urllib.request.urlopen(f"{host}/flux/_meta/datasets/{REPO}/main/tree", timeout=60)
        .read()
        .decode()
    )
    pres = [
        p.replace("&#x27;", "'") for p in re.findall(r"<pre>(array\(\[[^\[][\s\S]*?\))</pre>", html)
    ]

    def values(s):
        inner = s[s.index("[") + 1 : s.index("]")].split(",")
        return [v.strip().strip("'") for v in inner if v.strip() != "..."]

    times = [t[:19] for t in values(next(p for p in pres if "datetime64" in p))]
    leads = [int(v) for v in values(next(p for p in pres if "timedelta64[m]" in p))]
    return max(times), leads


def warm(host, init, leads):
    urls = []
    for variable in VARIABLES:
        for lead in leads:  # playback order
            q = urlencode(
                {
                    "variables": variable,
                    "style": "raster/rgb",
                    "colorscalerange": "0,255",
                    "time": init,
                    "lead_time": f"{lead} minutes",
                    "ensemble": "0",
                    "width": "512",
                    "height": "512",
                    "f": "png",
                }
            )
            urls += [
                f"{host}/flux/{REPO}/main/tiles/WebMercatorQuad/{z}/{y}/{x}?{q}"
                for z, y, x in tiles()
            ]
    t0, counts = time.time(), {}
    with ThreadPoolExecutor(IN_FLIGHT) as pool:
        for res in pool.map(lambda u: _safe(get, u), urls):
            counts[res] = counts.get(res, 0) + 1
    print(f"{init}: {len(urls)} tiles in {time.time() - t0:.0f} s {counts}", flush=True)


def _safe(f, *a):
    try:
        return f(*a)
    except Exception as e:  # a 504 now is retried by the next visitor; carry on
        return type(e).__name__


def main():
    host = sys.argv[1].rstrip("/")
    done = None
    while True:
        try:
            init, leads = run_list(host)
            if init != done:
                warm(host, init, leads)
                done = init
        except Exception as e:
            print(f"poll failed: {e}", flush=True)
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
