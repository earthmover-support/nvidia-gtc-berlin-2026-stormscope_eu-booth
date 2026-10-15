"""Render each new run's tiles once through the site's Vercel proxy, before visitors ask.

Visitors who open a new run together still make Flux render each tile several times: the
proxy's in-flight map only merges requests that land on the same function instance (10
clients, no warmer: 1,007 Flux requests for 234 tiles, 2026-10-15). This loop asks for every
tile of the default view once, as soon as the init is listed, so visitors get cache hits.

    uv run --no-project python scripts/warm_tiles.py https://<site>.vercel.app

Pass the exact hostname visitors use: the CDN cache is keyed by it. The URLs must match
`tileUrl` in site/index.html byte for byte: the query is part of the cache key. Failed
tiles are retried; a run counts as done only when every tile is in.
"""

import math
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

REPO = "stormscope-eu_gtc_berlin_2026_EU_region"
# Polled directly, not through the proxy: the proxy caches the run list for 30 s, which would
# give visitors the new init as early as the warmer.
FLUX = (
    "https://tiles-edf5beae0d2afdc4.eu-west-1.compute.arraylake.app/v1/services/tiles-eu/gtc-berlin"
)
VARIABLES = ["true_color", "airmass"]  # the page's default first
# What phones and desktops see at the page's initial bounds ([-5, 44, 26, 58]): z2-z4
# over a box wide enough for a 1920 px screen. Panning and zooming in still miss.
BOX, ZOOMS = (-25, 35, 45, 65), (2, 3, 4)
IN_FLIGHT = 16  # done in ~20 s, before most visitors' next 60 s poll
POLL_S = 5
RETRIES, RETRY_S = 3, 15  # rounds for the tiles that failed, and the pause before each


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


def run_list():
    """(newest init, leads) from the dataset tree, parsed like loadCoords in index.html."""
    html = (
        urllib.request.urlopen(f"{FLUX}/_meta/datasets/{REPO}/main/tree", timeout=60)
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
    t0 = time.time()
    for attempt in range(RETRIES + 1):
        if attempt:
            time.sleep(RETRY_S)
        with ThreadPoolExecutor(IN_FLIGHT) as pool:
            results = list(pool.map(lambda u: _safe(get, u), urls))
        counts = {r: results.count(r) for r in set(results)}
        print(f"{init}: {len(urls)} tiles in {time.time() - t0:.0f} s {counts}", flush=True)
        urls = [u for u, r in zip(urls, results, strict=True) if r[0] != 200]
        if not urls:
            break
    return len(urls)


def _safe(f, *a):
    try:
        return f(*a)
    except Exception as e:  # 504s from a busy service: retried
        return type(e).__name__, getattr(e, "code", None)


def main():
    host = sys.argv[1].rstrip("/")
    done, passes = None, {}
    while True:
        try:
            init, leads = run_list()
            if init != done:
                passes[init] = passes.get(init, 0) + 1
                # still failing after its retries: next poll tries again, up to 3 passes
                if warm(host, init, leads) == 0 or passes[init] >= 3:
                    done = init
        except Exception as e:
            print(f"poll failed: {e}", flush=True)
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
