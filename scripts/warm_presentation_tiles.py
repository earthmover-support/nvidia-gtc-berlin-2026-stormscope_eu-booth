"""Render each new run's tiles once through the hosted presentation's Vercel proxy, before viewers ask.

Viewers who open a new run together each miss the cache, so Flux renders every tile several
times. This loop asks for the globe's tiles once, as soon as `/api/state` lists the run.

    uv run --no-project python scripts/warm_presentation_tiles.py https://<presentation>.vercel.app

Pass the exact hostname viewers use: the CDN cache is keyed by it. The URLs must match
`tileTemplate` in presentation/web/app.js byte for byte (same parameter order, snapshot id
as ref): the query is part of the cache key. Failed tiles are retried.
"""

import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

# The globe at its home view (fixed 1920 x 1080 layout, any window size): z4 x 5-11, y 3-6,
# measured in Chrome 2026-10-15. The sway and visitors' zooming still miss a few tiles.
GLOBE = [(4, y, x) for y in range(3, 7) for x in range(5, 12)]
IN_FLIGHT = 16
POLL_S = 5
RETRIES, RETRY_S = 3, 15  # rounds for the tiles that failed, and the pause before each


def get_json(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)


def get(url):
    with urllib.request.urlopen(url, timeout=120) as r:
        r.read()
        return r.status, r.headers.get("x-vercel-cache")


def urls(host, cfg, run):
    q = cfg["qr_tile"]  # the +0 composite previews and the "Raw tiles" QR
    tiles = GLOBE + [(q["z"], q["y"], q["x"])]
    out = []
    for lead in cfg["leads"]:  # playback order, default variable first
        for variable, v in cfg["variables"].items():
            p = urlencode(
                {
                    "variables": variable,
                    "style": v["style"],
                    "colorscalerange": v["colorscalerange"],
                    "time": run["time"],
                    "lead_time": f"{lead} minutes",
                    "ensemble": "0",
                    "f": "png",
                    "width": "512",
                    "height": "512",
                }
            )
            base = f"{host}{cfg['tile_base']}/{run['ref']}/tiles/WebMercatorQuad"
            out += [f"{base}/{z}/{y}/{x}?{p}" for z, y, x in tiles]
    return out


def warm(todo, init):
    t0 = time.time()
    for attempt in range(RETRIES + 1):
        if attempt:
            time.sleep(RETRY_S)
        with ThreadPoolExecutor(IN_FLIGHT) as pool:
            results = list(pool.map(_safe, todo))
        counts = {r: results.count(r) for r in set(results)}
        print(f"{init}: {len(todo)} tiles in {time.time() - t0:.0f} s {counts}", flush=True)
        todo = [u for u, r in zip(todo, results, strict=True) if r[0] != 200]
        if not todo:
            break
    return len(todo)


def _safe(url):
    try:
        return get(url)
    except Exception as e:  # 504s from a busy service: retried
        return type(e).__name__, getattr(e, "code", None)


def main():
    host = sys.argv[1].rstrip("/")
    cfg = get_json(f"{host}/api/config")
    done, passes = None, {}
    while True:
        try:
            run = get_json(f"{host}/api/state")["runs"][0]
            if run["init"] != done:
                passes[run["init"]] = passes.get(run["init"], 0) + 1
                # still failing after its retries: next poll tries again, up to 3 passes
                if warm(urls(host, cfg, run), run["init"]) == 0 or passes[run["init"]] >= 3:
                    done = run["init"]
        except Exception as e:
            print(f"poll failed: {e}", flush=True)
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
