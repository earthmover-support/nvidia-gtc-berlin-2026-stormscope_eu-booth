# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx[http2]"]
# ///
"""How many booth maps per second can the Flux tiles service render? (issue #11)

One **map** is one booth frame: the 20 z4 tiles (512 px) the booth globe requests for one
lead, recorded from the real page at 1920x1080 including the camera sway. One run is 12
maps = 240 tiles. They are requested in playback order (+10 first, as the booth page does)
with N requests in flight; N=6 is what one browser does over HTTP/1.1.

Snapshot-pinned URLs, so the service cannot answer from a branch lookup; each response is
a real render (check `inbound_MB` and `png_kB_median` -- an empty tile is ~0 kB).

    uv run benchmarks/tiles_throughput.py airmass 2026-09-30T09:00:00 NM149HDW8RNTJEJF1050 6

Prints one JSON line: wall time, tiles/s, maps/s, time to the first complete map, tile
latency p50/p95/max, bytes the service read, status codes and the multiscale level used.
"""

import argparse
import asyncio
import json
import statistics
import time
from collections import Counter
from pathlib import Path

import httpx

# the booth's own endpoint, styles and leads, so the benchmark measures what the page requests
CONFIG = json.loads((Path(__file__).parent.parent / "presentation" / "config.json").read_text())
# [z, y, x] in URL path order: the booth frame at camera zoom ~3.2-4
TILES = [
    [4, 3, 5], [4, 3, 6], [4, 3, 7], [4, 3, 8],
    [4, 4, 5], [4, 4, 6], [4, 4, 7], [4, 4, 8], [4, 4, 9], [4, 4, 10], [4, 4, 11],
    [4, 5, 6], [4, 5, 7], [4, 5, 8], [4, 5, 9], [4, 5, 10], [4, 5, 11],
    [4, 6, 7], [4, 6, 8], [4, 6, 9],
]
LEADS = [CONFIG["lead_step_min"] * (i + 1) for i in range(CONFIG["lead_steps"])]


def tile_url(a, lead, z, y, x):
    v = CONFIG["variables"][a.variable]
    q = httpx.QueryParams(variables=a.variable, style=v["style"], colorscalerange=v["colorscalerange"],
                          time=a.time, lead_time=f"{lead} minutes", ensemble="0", f="png",
                          width="512", height="512")
    return f"{CONFIG['tile_base']}/{a.snapshot}/tiles/WebMercatorQuad/{z}/{y}/{x}?{q}"


async def run(a) -> dict:
    jobs = [(lead, tile_url(a, lead, z, y, x)) for lead in LEADS for z, y, x in TILES]
    queue: asyncio.Queue = asyncio.Queue()
    for j in jobs:
        queue.put_nowait(j)
    lat, png = [], []
    status, levels, versions = Counter(), Counter(), Counter()
    inbound = 0
    left = Counter(lead for lead, _ in jobs)
    map_done: dict[int, float] = {}
    limits = httpx.Limits(max_connections=a.concurrency, max_keepalive_connections=a.concurrency)
    async with httpx.AsyncClient(http2=True, limits=limits, timeout=120) as client:
        t0 = time.perf_counter()

        async def worker():
            nonlocal inbound
            while not queue.empty():
                lead, u = queue.get_nowait()
                s = time.perf_counter()
                try:
                    r = await client.get(u)
                    status[r.status_code] += 1
                    versions[r.http_version] += 1
                    levels[r.headers.get("x-multiscale-level", "-")] += 1
                    inbound += int(r.headers.get("x-flux-inbound-bytes", 0))
                    png.append(len(r.content))
                except httpx.HTTPError as e:
                    status[type(e).__name__] += 1
                lat.append(time.perf_counter() - s)
                left[lead] -= 1
                if not left[lead]:
                    map_done[lead] = time.perf_counter() - t0

        await asyncio.gather(*(worker() for _ in range(a.concurrency)))
        wall = time.perf_counter() - t0
    lat.sort()
    return {
        "label": a.label, "variable": a.variable, "time": a.time, "concurrency": a.concurrency,
        "tiles": len(jobs), "wall_s": round(wall, 1), "tiles_per_s": round(len(jobs) / wall, 2),
        "maps_per_s": round(len(LEADS) / wall, 3), "first_map_s": round(min(map_done.values()), 1),
        "p50_s": round(statistics.median(lat), 2), "p95_s": round(lat[int(0.95 * len(lat)) - 1], 2),
        "max_s": round(lat[-1], 2), "inbound_MB": round(inbound / 1e6),
        "png_kB_median": round(statistics.median(png) / 1e3) if png else 0,
        "status": dict(status), "levels": dict(levels), "http": dict(versions),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("variable", help="true_color or airmass")
    ap.add_argument("time", help="init time, e.g. 2026-09-30T09:00:00")
    ap.add_argument("snapshot", help="Icechunk snapshot id holding that run")
    ap.add_argument("concurrency", type=int, help="requests in flight (6 = one browser)")
    ap.add_argument("--label", default="")
    print(json.dumps(asyncio.run(run(ap.parse_args()))))


if __name__ == "__main__":
    main()
