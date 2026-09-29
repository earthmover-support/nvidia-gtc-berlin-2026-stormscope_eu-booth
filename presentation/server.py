#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "arraylake>=0.14",
#     "icechunk>=1.0",
#     "zarr>=3",
#     "numpy",
#     "psycopg[binary]>=3.2",
#     "python-dotenv",
# ]
# ///
"""GTC Berlin booth server.

Serves the booth page from ./web and three small JSON endpoints:

  /api/config   public part of config.json (no secrets)
  /api/state    recent Icechunk commits on the repo, grouped into forecast runs
  /api/sql      latest result of the live Zax-SQL query
                POST {"query": "..."} replaces the query (typed on the page), {"reset": true} restores it

Run from the repo root:

    uv run presentation/server.py            # live, needs ARRAYLAKE_TOKEN in .env
    uv run presentation/server.py --demo     # fake commits, no network needed
"""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
import json
import logging
import os
import re
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
WEB = HERE / "web"
NODE = HERE / "node_modules"
load_dotenv(HERE.parent / ".env")
CONFIG = json.loads((HERE / "config.json").read_text())

log = logging.getLogger("booth")

# "stormscope-eu 2026-09-15T23:50 +30 min" / "... analysis frame (lead 0 min)" / "... schema"
MSG_RE = re.compile(
    r"stormscope-eu\s+(?P<init>\d{4}-\d{2}-\d{2}T\d{2}:\d{2})\s+"
    r"(?:\+(?P<lead>\d+)\s*min|analysis frame|schema)"
)

# URL prefix -> directory for vendored front-end libraries (npm install in ./presentation)
VENDOR = {
    "/vendor/maplibre/": NODE / "maplibre-gl" / "dist",
    "/vendor/qrcode/": NODE / "qrcode-generator" / "dist",
}


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Shared:
    """Thread-safe holder for the latest state snapshots."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state: dict = {"status": "starting", "commits": [], "runs": []}
        self.sql: dict = {"status": "starting"}
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


def default_sql() -> str:
    """config.json query for the first place in sql.places; {init} stays for sql_text()."""
    sql = CONFIG["sql"]
    lat, lon = next(iter(sql["places"].values()))
    return (
        "\n".join(sql["query"])
        .replace("{lat}", str(lat))
        .replace("{lon}", str(lon))
        .replace("{box}", str(sql["box_deg"]))
    )


def check_sql(query: str) -> str:
    """A single SELECT/WITH statement from the page, trailing ';' dropped. Raises ValueError."""
    q = query.strip().rstrip(";").strip()
    if not q or len(q) > 4000:
        raise ValueError("empty or too long")
    if ";" in q:
        raise ValueError("one statement only")
    if not re.match(r"(?is)^(select|with)\b", q):
        raise ValueError("only SELECT / WITH queries")
    return q + ";"


SHARED = Shared()


# --------------------------------------------------------------------------- runs


def leads_by_init(commits: list[dict]) -> dict[str, dict]:
    """Forecast leads (> 0 min) seen in commit messages, per init 'YYYY-MM-DDTHH:MM'."""
    out: dict[str, dict] = {}
    for c in commits:  # newest first
        m = MSG_RE.search(c.get("message") or "")
        if not m:
            continue
        rec = out.setdefault(m["init"], {"leads": set(), "ref": c["id"]})
        if m["lead"] and int(m["lead"]) > 0:
            rec["leads"].add(int(m["lead"]))
    return out


def build_runs(times: list[str], commits: list[dict]) -> list[dict]:
    """One run per init in the repo's `time` coordinate (rolling window), newest first.

    A run whose commits are in the recent history and have not reached the last lead
    is in progress: only its committed leads are offered, so no browser ever caches
    a tile that has not been written yet. Anything else is complete.
    """
    full = [10 * (i + 1) for i in range(CONFIG["lead_steps"])]
    seen = leads_by_init(commits)
    runs = []
    for t in sorted(set(times), reverse=True):
        init = t[:16]
        rec = seen.get(init)
        if rec is None or (rec["leads"] and max(rec["leads"]) >= full[-1]):
            leads, complete = full, True
        else:
            leads, complete = sorted(rec["leads"]), False
        runs.append({"init": init, "time": t, "leads": leads, "complete": complete,
                     "ref": rec["ref"] if rec else None})
    return runs


def read_times(repo) -> list[str]:
    """Init times currently held in the repo (the `time` coordinate, NaT slots dropped).

    With `init` set in config.json ("YYYY-MM-DDTHH:MM"), only that run is offered.
    """
    import numpy as np
    import zarr

    session = repo.readonly_session(branch=CONFIG["branch"])
    group = zarr.open_group(session.store, mode="r", path=CONFIG.get("group", "") or None)
    values = np.asarray(group["time"][:])
    times = [np.datetime_as_string(v, unit="s") for v in values if not np.isnat(v)]
    pin = CONFIG.get("init")
    return [t for t in times if t.startswith(pin)] if pin else times


# --------------------------------------------------------------------------- live poller


def poll_arraylake(stop: threading.Event) -> None:
    from arraylake import Client

    client = Client()  # reads ARRAYLAKE_TOKEN
    repo = None
    head, times = None, []
    while not stop.is_set():
        try:
            if repo is None:
                repo = client.get_repo(CONFIG["repo"])
                head = None
            commits = []
            for snap in itertools.islice(
                repo.ancestry(branch=CONFIG["branch"]), CONFIG["history_depth"]
            ):
                commits.append(
                    {
                        "id": snap.id,
                        "message": snap.message,
                        "written_at": snap.written_at.astimezone(dt.timezone.utc).isoformat(),
                    }
                )
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


def demo_commits(now: dt.datetime) -> tuple[list[dict], list[str]]:
    """Fake history: a run every 10 min, 13 commits (0..120) written over ~3 min.

    Returns (commits newest first, init times in the rolling window).
    """
    interval = CONFIG["run_interval_min"]
    base = now.replace(second=0, microsecond=0)
    base -= dt.timedelta(minutes=base.minute % interval)
    commits, times = [], []
    for k in range(CONFIG["max_runs"]):
        start = base - dt.timedelta(minutes=interval * k)
        init = start - dt.timedelta(minutes=90)  # ~80-90 min licence lag
        times.append(f"{init:%Y-%m-%dT%H:%M:%S}")
        for i in range(CONFIG["lead_steps"] + 1):
            t = start + dt.timedelta(seconds=15 * i)
            if t > now:
                break
            msg = f"stormscope-eu {init:%Y-%m-%dT%H:%M} " + (
                "analysis frame (lead 0 min)" if i == 0 else f"+{i * 10} min"
            )
            sid = f"DEMO{init:%H%M}{i:02d}".ljust(20, "X")
            commits.append({"id": sid, "message": msg, "written_at": t.isoformat()})
    commits.sort(key=lambda c: c["written_at"], reverse=True)
    return commits, times


def poll_demo(stop: threading.Event) -> None:
    while not stop.is_set():
        commits, times = demo_commits(utcnow())
        SHARED.set_state(status="demo", commits=commits[:20], runs=build_runs(times, commits))
        stop.wait(2)


# --------------------------------------------------------------------------- SQL


def sql_host() -> str:
    return os.environ.get("ARRAYLAKE_SQL_HOST") or CONFIG["sql"].get("host") or ""


def sql_text() -> str:
    """The active query with {init} set to the newest complete run (tz-naive timestamp)."""
    runs = SHARED.get("state").get("runs") or []
    done = [r for r in runs if r["complete"]]
    init = (done[0]["time"] if done else (runs[0]["time"] if runs else "1970-01-01T00:00:00")).replace("T", " ")
    with SHARED.lock:
        template = SHARED.sql_template
    return template.replace("{init}", init)


def _jsonable(v):
    if isinstance(v, dt.timedelta):
        return v.total_seconds() / 60.0  # minutes
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    if isinstance(v, (int, float, str)) or v is None:
        return v
    try:
        return float(v)
    except Exception:
        return str(v)


def run_sql_once() -> None:
    import psycopg

    notices: list[str] = []
    query = sql_text()
    t0 = time.perf_counter()
    with psycopg.connect(
        host=sql_host(),
        port=5432,
        sslmode="require",
        user="arraylake",
        dbname=CONFIG["org"],
        password=os.environ["ARRAYLAKE_TOKEN"],
        autocommit=True,
        connect_timeout=15,
    ) as conn:
        conn.add_notice_handler(lambda d: notices.append(d.message_primary or ""))
        cur = conn.execute(query)
        cols = [d.name for d in cur.description or []]
        rows = [[_jsonable(v) for v in row] for row in cur.fetchmany(CONFIG["sql"]["max_rows"])]
    SHARED.set_sql(
        status="ok",
        query=query,
        columns=cols,
        rows=rows,
        seconds=round(time.perf_counter() - t0, 2),
        notices=notices[-3:],
        ran_at=utcnow().isoformat(),
    )


def poll_sql(stop: threading.Event, demo: bool) -> None:
    last_head = None
    last_run = 0.0
    wake = SHARED.sql_wake
    while not stop.is_set():
        asked = wake.is_set()
        wake.clear()
        if demo:
            import math

            phase = time.time() / 600 + len(sql_text()) % 7
            rows = [[m * 10, 60 + 25 * math.sin(m / 2 + phase)] for m in range(13)]
            rows = rows[1:]  # leads 10..120
            SHARED.set_sql(status="demo", query=sql_text(), columns=["lead_time", "brightness"], rows=rows, seconds=0.4, ran_at=utcnow().isoformat())
            wake.wait(CONFIG["sql"]["refresh_seconds"])
            continue
        if not sql_host():
            SHARED.set_sql(status="disabled", reason="Set ARRAYLAKE_SQL_HOST in .env (al compute enable gtc-berlin sql)")
            stop.wait(30)
            continue
        commits = SHARED.get("state").get("commits") or []
        head = commits[0]["id"] if commits else None
        due = time.time() - last_run > CONFIG["sql"]["refresh_seconds"]
        if head != last_head or due or asked:
            prev = SHARED.get("sql")
            SHARED.set_sql(**{**prev, "status": "running", "query": sql_text()})
            try:
                run_sql_once()
            except Exception as exc:
                log.warning("sql failed: %s", exc)
                SHARED.set_sql(status="error", query=sql_text(), error=str(exc)[:300])
            last_head, last_run = head, time.time()
        wake.wait(3)


# --------------------------------------------------------------------------- HTTP


def public_config() -> dict:
    keys = [
        "org", "repo", "group", "tile_base", "tile_ref_mode", "data_bounds", "max_tile_level", "variables",
        "lead_steps", "run_interval_min", "max_runs", "panel_seconds", "playback_speed", "hold_ms",
        "station", "camera", "viewer_url", "qr_tile",
    ]
    cfg = {k: CONFIG[k] for k in keys}
    cfg["branch"] = CONFIG["branch"]
    cfg["sql_query"] = default_sql()
    cfg["sql_template"] = "\n".join(CONFIG["sql"]["query"])
    cfg["sql_places"] = CONFIG["sql"]["places"]
    cfg["sql_box"] = CONFIG["sql"]["box_deg"]
    return cfg


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB), **kwargs)

    def _json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/config":
            return self._json(public_config())
        if path == "/api/state":
            return self._json(SHARED.get("state"))
        if path == "/api/sql":
            return self._json(SHARED.get("sql"))
        return super().do_GET()

    def do_POST(self):  # noqa: N802
        if self.path.split("?", 1)[0] != "/api/sql":
            return self._json({"error": "not found"}, 404)
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 16384)) or b"{}")
            SHARED.set_sql_template(default_sql() if body.get("reset") else check_sql(str(body.get("query", ""))))
        except (ValueError, TypeError) as exc:
            return self._json({"error": str(exc)}, 400)
        return self._json({"ok": True})

    def translate_path(self, path):
        clean = path.split("?", 1)[0].split("#", 1)[0]
        for prefix, root in VENDOR.items():
            if clean.startswith(prefix):
                rel = Path(clean[len(prefix):])
                target = (root / rel).resolve()
                if root.resolve() in target.parents or target == root.resolve():
                    return str(target)
                return str(WEB / "__forbidden__")
        return super().translate_path(path)

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)  # args[0] can be an HTTPStatus, not a str

    def log_error(self, fmt, *args):
        log.debug(fmt, *args)  # a missing favicon etc. is not worth a line at the booth


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--demo", action="store_true", help="fake commits and SQL, no Arraylake access")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not (NODE / "maplibre-gl").exists():
        log.warning("presentation/node_modules missing: run `npm install` in presentation/ first")
    if not args.demo and not os.environ.get("ARRAYLAKE_TOKEN"):
        log.warning("ARRAYLAKE_TOKEN not set: commit polling will fail (use --demo to rehearse)")

    stop = threading.Event()
    poller = poll_demo if args.demo else poll_arraylake
    threading.Thread(target=poller, args=(stop,), daemon=True).start()
    threading.Thread(target=poll_sql, args=(stop, args.demo), daemon=True).start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    log.info("booth page on http://%s:%d %s", args.host, args.port, "(demo mode)" if args.demo else "")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
