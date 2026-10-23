#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "arraylake>=0.14",
#     "icechunk>=1.0",
#     "zarr>=3",
#     "numpy",
#     "psycopg[binary]>=3.2",
#     "certifi",
#     "python-dotenv",
# ]
# ///
"""GTC Berlin booth server.

Serves the booth page from ./web and three small JSON endpoints:

  /api/config   public part of config.json (no secrets)
  /api/state    recent Icechunk commits on the repo, grouped into forecast runs
  /api/sql      latest result of the Zax-SQL query
                POST {"query": "..."} runs that query once (Run on the page), {"reset": true} runs the default

Run from the repo root:

    uv run presentation/server.py            # live, needs ARRAYLAKE_API_TOKEN in .env
    uv run presentation/server.py --demo     # fake commits, no network needed
    uv run presentation/server.py --fetch-offline   # online: save a few runs to presentation/offline/
    uv run presentation/server.py --offline  # replay the saved runs, no network needed
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import itertools
import json
import logging
import math
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode

from booth import (
    CONFIG,
    HERE,
    all_leads,
    build_runs,
    check_sql,
    default_sql,
    fill_init,
    open_repo,
    place_queries,
    public_config,
    read_commits,
    read_times,
    run_sql_once,
    send_json,
    sql_host,
    utcnow,
)
from dotenv import load_dotenv

WEB = HERE / "web"
NODE = HERE / "node_modules"
load_dotenv(HERE.parent / ".env")

log = logging.getLogger("booth")
MODE = "live"  # "live", "demo" or "offline"; set in main()

# --offline: what --fetch-offline saves, and how far the page may zoom into it
OFFLINE = HERE / "offline"
OFFLINE_RUNS = 3
OFFLINE_ZOOM = 5  # deepest tile level saved; MapLibre overzooms past it
CARTO_STYLE = "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json"
UA = "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"  # woff2 from Google

# URL prefix -> directory for vendored front-end libraries (npm install in ./presentation)
VENDOR = {
    "/vendor/maplibre/": NODE / "maplibre-gl" / "dist",
    "/vendor/qrcode/": NODE / "qrcode-generator" / "dist",
}


class Shared:
    """Thread-safe holder for the latest state snapshots."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state: dict = {"status": "starting", "commits": [], "runs": []}
        self.sql: dict = {"status": "idle"}  # until the first run lands or someone presses Run
        self.sql_template: str = default_sql()
        self.sql_wake = threading.Event()  # set to run the query now

    def set_state(self, **kw) -> None:
        with self.lock:
            self.state = {**kw, "updated_at": utcnow().isoformat()}

    def set_sql(self, **kw) -> None:
        with self.lock:
            self.sql = {**kw, "updated_at": utcnow().isoformat()}

    def set_sql_template(self, template: str) -> None:
        with self.lock:
            self.sql_template = template
        self.sql_wake.set()

    def get(self, name: str) -> dict:
        with self.lock:
            return dict(getattr(self, name))


SHARED = Shared()


# --------------------------------------------------------------------------- live poller


def poll_arraylake(stop: threading.Event) -> None:
    repo = None
    head, times = None, []
    while not stop.is_set():
        try:
            if repo is None:
                repo = open_repo()
                head = None
            commits = read_commits(repo)
            if commits and commits[0]["id"] != head:
                times = read_times(repo)  # re-read only when main moved
                head = commits[0]["id"]
            SHARED.set_state(status="ok", commits=commits[:20], runs=build_runs(times, commits))
        except Exception as exc:  # keep the booth running whatever happens
            log.warning("arraylake poll failed: %s", exc)
            prev = SHARED.get("state")
            SHARED.set_state(
                status="error",
                error=str(exc)[:300],
                commits=prev.get("commits", []),
                runs=prev.get("runs", []),
            )
            repo = None
        stop.wait(CONFIG["poll_seconds"])


# --------------------------------------------------------------------------- demo poller


def run_seconds() -> float:
    """Estimated length of one run: download IC, every forecast step, commit."""
    p = CONFIG["run_phases_s"]
    return p["ic"] + CONFIG["lead_steps"] * p["step"] + p["commit"]


def demo_commits(now: dt.datetime) -> tuple[list[dict], list[str]]:
    """Fake history: a run every 10 min, one commit each, written `run_seconds()` after its start.

    So the booth strip's estimate (next start = last commit + interval - run length)
    lines up exactly with the demo cadence.

    Returns (commits newest first, committed init times in the rolling window).
    """
    interval = CONFIG["run_interval_min"]
    base = now.replace(second=0, microsecond=0)
    base -= dt.timedelta(minutes=base.minute % interval)
    commits, times = [], []
    last = all_leads()[-1]
    for k in range(CONFIG["max_runs"]):
        start = base - dt.timedelta(minutes=interval * k)
        init = start - dt.timedelta(minutes=90)  # ~80-90 min licence lag
        t = start + dt.timedelta(seconds=run_seconds())
        if t > now:
            continue
        times.append(f"{init:%Y-%m-%dT%H:%M:%S}")
        msg = f"stormscope-eu {init:%Y-%m-%dT%H:%M} lead 0-{last} min"
        sid = f"DEMO{init:%H%M}".ljust(20, "X")
        commits.append({"id": sid, "message": msg, "written_at": t.isoformat()})
    commits.sort(key=lambda c: c["written_at"], reverse=True)
    return commits, times


def poll_demo(stop: threading.Event) -> None:
    while not stop.is_set():
        commits, times = demo_commits(utcnow())
        SHARED.set_state(status="demo", commits=commits[:20], runs=build_runs(times, commits))
        stop.wait(2)


# --------------------------------------------------------------------------- offline
#
# presentation/offline/ after --fetch-offline:
#   runs.json                                  the saved runs [{init, time, ref}], newest first
#   tiles/<ref>/<variable>/<lead>/<z>/<y>/<x>.png
#   sql/<ref>/<place>.json                     default query per run and place
#   basemap/style.json, sprite*, tiles/<z>/<x>/<y>.pbf, fonts/<fontstack>/<range>.pbf


def tile_xy(lon: float, lat: float, z: int) -> tuple[int, int]:
    """Web-mercator tile (x, y) holding a point at zoom z."""
    k = 2**z
    lat = math.radians(max(-85.0511, min(85.0511, lat)))
    x = int((lon + 180) / 360 * k)
    y = int((1 - math.asinh(math.tan(lat)) / math.pi) / 2 * k)
    return min(k - 1, max(0, x)), min(k - 1, max(0, y))


def tiles_in(bounds, zooms):
    """(z, x, y) of every web-mercator tile touching bounds [west, south, east, north]."""
    w, s, e, n = bounds
    for z in zooms:
        (x0, y0), (x1, y1) = tile_xy(w, n, z), tile_xy(e, s, z)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                yield z, x, y


def tile_url(run: dict, variable: str, lead: int, z: int, x: int, y: int) -> str:
    """The URL app.js tileTemplate() asks for, with 512 px tiles."""
    v = CONFIG["variables"][variable]
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
    ref = quote(run["ref"], safe="")
    return f"{CONFIG['tile_base']}/{ref}/tiles/WebMercatorQuad/{z}/{y}/{x}?{p}"


def get(url: str) -> bytes | None:
    """Body of `url`, gunzipped; None on 404 (a tile outside the data)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read()
            return gzip.decompress(body) if r.headers.get("Content-Encoding") == "gzip" else body
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def save(job: tuple[str, Path]) -> bool:
    """Download one (url, path); existing files are kept, so a rerun resumes."""
    url, path = job
    if path.exists():
        return True
    try:
        body = get(url)
    except Exception as exc:
        log.warning("fetch failed: %s: %s", url[:120], exc)
        return False
    if body is None:
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(body)
    tmp.replace(path)  # an interrupted fetch never leaves half a file
    return True


def basemap_jobs() -> list[tuple[str, Path]]:
    """Save the Carto style with local URLs; return its tiles, sprites and glyphs to fetch.

    "{origin}" in the saved style is replaced per request (MapLibre wants absolute URLs).
    """
    out = OFFLINE / "basemap"
    style = json.loads(get(CARTO_STYLE))
    # the one vector source: Carto renamed it carto -> protomaps and inlined its tiles (2026-10)
    ((name, src),) = style["sources"].items()
    tilejson = src if "tiles" in src else json.loads(get(src["url"]))
    style["sources"][name] = {
        "type": "vector",
        "tiles": ["{origin}/offline/basemap/tiles/{z}/{x}/{y}.pbf"],
        "maxzoom": OFFLINE_ZOOM,
        "attribution": tilejson.get("attribution", ""),
    }
    jobs = []
    # whole globe when zoomed out, the forecast domain closer in
    world = tiles_in([-180, -85, 180, 85], range(4))
    for z, x, y in itertools.chain(
        world, tiles_in(CONFIG["data_bounds"], range(4, OFFLINE_ZOOM + 1))
    ):
        url = (
            tilejson["tiles"][0]
            .replace("{z}", str(z))
            .replace("{x}", str(x))
            .replace("{y}", str(y))
        )
        jobs.append((url, out / "tiles" / str(z) / str(x) / f"{y}.pbf"))
    for suffix in ("", "@2x"):
        for ext in (".json", ".png"):
            jobs.append((style["sprite"] + suffix + ext, out / f"sprite{suffix}{ext}"))
    style["sprite"] = "{origin}/offline/basemap/sprite"
    stacks = {
        ",".join(layer["layout"]["text-font"])
        for layer in style["layers"]
        if isinstance(layer.get("layout", {}).get("text-font"), list)
    }
    for stack in stacks:
        for start in (0, 256, 512, 768, 8192):  # Latin, Latin extended, punctuation
            rng = f"{start}-{start + 255}"
            url = style["glyphs"].replace("{fontstack}", quote(stack)).replace("{range}", rng)
            jobs.append((url, out / "fonts" / stack / f"{rng}.pbf"))
    style["glyphs"] = "{origin}/offline/basemap/fonts/{fontstack}/{range}.pbf"
    out.mkdir(parents=True, exist_ok=True)
    (out / "style.json").write_text(json.dumps(style))
    return jobs


def fetch_offline() -> None:
    """Save the newest runs, the basemap and SQL results for --offline."""
    repo = open_repo()
    commits = read_commits(repo)
    runs = build_runs(read_times(repo), commits)[:OFFLINE_RUNS]
    if len({r["ref"] for r in runs}) < len(runs):
        raise SystemExit("the newest runs share a snapshot; history_depth too small?")
    log.info("saving runs %s", ", ".join(r["init"] for r in runs))
    OFFLINE.mkdir(exist_ok=True)

    for run in runs:
        for place, query in place_queries().items():
            path = OFFLINE / "sql" / run["ref"] / f"{place}.json"
            if path.exists():
                continue
            try:
                result = run_sql_once(fill_init(query, run["time"]))
            except Exception as exc:  # the tiles matter more: carry on, a rerun retries
                log.warning("sql for %s %s failed: %s", run["init"], place, exc)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(result, default=str))

    jobs = basemap_jobs()
    frames = itertools.product(runs, CONFIG["variables"], all_leads())
    for run, variable, lead in frames:
        for z, x, y in tiles_in(CONFIG["data_bounds"], range(OFFLINE_ZOOM + 1)):
            path = (
                OFFLINE / "tiles" / run["ref"] / variable / str(lead) / str(z) / str(y) / f"{x}.png"
            )
            jobs.append((tile_url(run, variable, lead, z, x, y), path))
    log.info("%d files to fetch (existing ones are skipped)", len(jobs))
    failed = 0
    with ThreadPoolExecutor(8) as pool:
        for i, ok in enumerate(pool.map(save, jobs), 1):
            failed += not ok
            if i % 500 == 0 or i == len(jobs):
                log.info("%d / %d done, %d failed", i, len(jobs), failed)
    (OFFLINE / "runs.json").write_text(json.dumps(runs, indent=1))
    if failed:
        log.warning("%d files failed: run --fetch-offline again to retry them", failed)


def offline_state(now: dt.datetime, saved: list[dict]) -> tuple[list[dict], list[dict]]:
    """The demo cadence (a run lands every interval) filled with the saved runs in turn.

    Each slot replays saved run (slot number % len(saved)), so the newest run on screen
    changes every interval and the visible runs are always distinct.
    Returns (commits, runs), newest first.
    """
    commits, times = demo_commits(now)
    n, step = len(saved), CONFIG["run_interval_min"] * 60
    runs = []
    for c, t in zip(commits[:n], times[:n], strict=True):
        slot = int(dt.datetime.fromisoformat(t).replace(tzinfo=dt.UTC).timestamp() // step)
        src = saved[slot % n]
        c["id"] = src["ref"]
        c["message"] += f" (replay of {src['init']} UTC)"
        runs.append({"init": t[:16], "time": t, "ref": src["ref"]})
    return commits[:n], runs


def poll_offline(stop: threading.Event) -> None:
    saved = json.loads((OFFLINE / "runs.json").read_text())
    while not stop.is_set():
        commits, runs = offline_state(utcnow(), saved)
        SHARED.set_state(status="offline", commits=commits, runs=runs)
        stop.wait(2)


def offline_sql(query: str) -> dict:
    """The saved result when `query` is a place query for the newest run."""
    run = SHARED.get("state")["runs"][0]
    for place, template in place_queries().items():
        path = OFFLINE / "sql" / run["ref"] / f"{place}.json"
        if fill_init(template, run["time"]) == query and path.exists():
            saved = json.loads(path.read_text())
            return {**saved, "status": "offline", "query": query, "ran_at": utcnow().isoformat()}
    return dict(status="error", query=query, error="offline: only the saved place queries run")


def offline_path(path: str) -> Path:
    """File under OFFLINE for an /offline/... URL; forecast tiles drop to their saved key."""
    clean, _, query = path.partition("#")[0].partition("?")
    parts = unquote(clean).split("/")[2:]
    # tiles/<ref>/tiles/WebMercatorQuad/<z>/<y>/<x>?variables=..&lead_time=10 minutes
    if len(parts) == 7 and parts[0] == "tiles" and parts[2] == "tiles":
        q = parse_qs(query)
        lead = q.get("lead_time", ["0"])[0].split()[0]
        parts = [
            "tiles",
            parts[1],
            q.get("variables", [""])[0],
            lead,
            *parts[4:6],
            parts[6] + ".png",
        ]
    target = OFFLINE.joinpath(*parts).resolve()
    return target if target.is_relative_to(OFFLINE.resolve()) else OFFLINE / "__forbidden__"


# --------------------------------------------------------------------------- SQL


def sql_text() -> str:
    """The active query with {init} set to the newest run (tz-naive timestamp)."""
    runs = SHARED.get("state").get("runs") or []
    with SHARED.lock:
        template = SHARED.sql_template
    return fill_init(template, runs[0]["time"] if runs else "1970-01-01T00:00:00")


def newest_init() -> str | None:
    """Init time of the newest run; runs are committed whole, so it is always complete."""
    runs = SHARED.get("state").get("runs") or []
    return runs[0]["time"] if runs else None


def poll_sql(stop: threading.Event) -> None:
    """Run the default (Berlin) query once per new init, and otherwise only on Run (panel 07).

    Each new init resets the editor to the default query, so the booth screen returns to
    Berlin. Results are cached per (init, query text): pressing Run again before the next
    run lands shows the stored result, marked `cached`, without querying the service.
    """
    wake = SHARED.sql_wake
    cache: dict[str, dict] = {}  # query text -> result, for init `cache_init`
    cache_init = None
    while not stop.is_set():
        if MODE == "live" and not sql_host():
            SHARED.set_sql(
                status="disabled",
                reason="Set ARRAYLAKE_SQL_HOST in .env (al compute enable gtc-berlin sql)",
            )
            stop.wait(30)
            continue
        init = newest_init()
        if init and init != cache_init:
            cache, cache_init = {}, init
            SHARED.set_sql_template(default_sql())  # sets wake
        if not wake.wait(1):
            continue
        wake.clear()
        query = sql_text()
        if query in cache:
            SHARED.set_sql(**cache[query], cached=True)
            continue
        if MODE == "offline":
            result = offline_sql(query)
        elif MODE == "demo":
            phase = time.time() / 600 + len(query) % 7
            rows = [[m, 60 + 25 * math.sin(m / 20 + phase)] for m in all_leads()]
            result = dict(
                status="demo",
                query=query,
                columns=["lead_time", "brightness"],
                rows=rows,
                seconds=0.4,
                ran_at=utcnow().isoformat(),
            )
        else:
            prev = SHARED.get("sql")
            SHARED.set_sql(**{**prev, "status": "running", "query": query, "cached": False})
            try:
                result = run_sql_once(query)
            except Exception as exc:
                log.warning("sql failed: %s", exc)
                SHARED.set_sql(status="error", query=query, error=str(exc)[:300])
                continue
        cache[query] = result
        SHARED.set_sql(**result)


# --------------------------------------------------------------------------- HTTP


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB), **kwargs)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/config":
            if MODE == "offline":
                return send_json(self, self.offline_config())
            return send_json(self, public_config(sql_editable=True))
        if MODE == "offline" and path == "/offline/basemap/style.json":
            style = (OFFLINE / "basemap" / "style.json").read_text()
            return self.send_text(style.replace("{origin}", self.origin()), "application/json")
        if path == "/api/state":
            return send_json(self, SHARED.get("state"))
        if path == "/api/sql":
            return send_json(self, SHARED.get("sql"))
        return super().do_GET()

    def do_POST(self):  # noqa: N802
        if self.path.split("?", 1)[0] != "/api/sql":
            return send_json(self, {"error": "not found"}, 404)
        try:
            body = json.loads(
                self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 16384)) or b"{}"
            )
            SHARED.set_sql_template(
                default_sql() if body.get("reset") else check_sql(str(body.get("query", "")))
            )
        except (ValueError, TypeError) as exc:
            return send_json(self, {"error": str(exc)}, 400)
        return send_json(self, {"ok": True})

    def origin(self) -> str:
        return f"http://{self.headers.get('Host', 'localhost')}"

    def offline_config(self) -> dict:
        """public_config() pointed at the saved tiles and basemap, zoom capped near them."""
        cfg = public_config(sql_editable=True)
        cam = cfg["camera"]
        return cfg | {
            "tile_base": f"{self.origin()}/offline/tiles",
            "basemap": f"{self.origin()}/offline/basemap/style.json",
            "max_tile_level": OFFLINE_ZOOM,
            "camera": cam | {"max_zoom": min(cam.get("max_zoom", 8.5), OFFLINE_ZOOM + 1)},
        }

    def send_text(self, text: str, mime: str) -> None:
        body = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", f"{mime}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def translate_path(self, path):
        clean = path.split("?", 1)[0].split("#", 1)[0]
        if MODE == "offline" and clean.startswith("/offline/"):
            return str(offline_path(path))
        for prefix, root in VENDOR.items():
            if clean.startswith(prefix):
                rel = Path(clean[len(prefix) :])
                target = (root / rel).resolve()
                if target.is_relative_to(root.resolve()):
                    return str(target)
                return str(WEB / "__forbidden__")
        return super().translate_path(path)

    def end_headers(self):
        # static files revalidate on every load, so a reload picks up edits to app.css / app.js
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)  # args[0] can be an HTTPStatus, not a str

    def log_error(self, fmt, *args):
        log.debug(fmt, *args)  # a missing favicon etc. is not worth a line at the booth


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--demo", action="store_true", help="fake commits and SQL, no Arraylake access"
    )
    mode.add_argument(
        "--offline", action="store_true", help="replay runs saved by --fetch-offline, no network"
    )
    mode.add_argument(
        "--fetch-offline", action="store_true", help=f"save the newest runs to {OFFLINE}, then exit"
    )
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    global MODE
    MODE = "demo" if args.demo else "offline" if args.offline else "live"

    if args.fetch_offline:
        return fetch_offline()
    if args.offline and not (OFFLINE / "runs.json").exists():
        raise SystemExit(f"{OFFLINE / 'runs.json'} missing: run --fetch-offline while online")
    if not (NODE / "maplibre-gl").exists():
        log.warning("presentation/node_modules missing: run `npm install` in presentation/ first")
    if MODE == "live" and not os.environ.get("ARRAYLAKE_API_TOKEN"):
        log.warning(
            "ARRAYLAKE_API_TOKEN not set: commit polling will fail (use --demo to rehearse)"
        )
    if MODE == "live":
        try:
            import psycopg  # noqa: F401
        except ImportError:
            log.warning(
                "psycopg missing: SQL panel will fail. Start with `uv run presentation/server.py` "
                "(inline deps), not `uv run python presentation/server.py` (project venv)"
            )

    stop = threading.Event()
    poller = {"demo": poll_demo, "offline": poll_offline, "live": poll_arraylake}[MODE]
    threading.Thread(target=poller, args=(stop,), daemon=True).start()
    threading.Thread(target=poll_sql, args=(stop,), daemon=True).start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    log.info("booth page on http://%s:%d (%s mode)", args.host, args.port, MODE)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
