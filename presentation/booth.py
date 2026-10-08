"""Logic shared by the booth server (server.py) and the hosted Vercel functions (api/).

Both read config.json, group Icechunk commits into forecast runs and run the Zax-SQL
query; only how they hold state differs (server.py polls in threads, api/ reads per
request).
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import os
import re
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "config.json").read_text())

# "stormscope-eu 2026-09-15T23:50 lead 0-120 min": one commit per init
MSG_RE = re.compile(r"stormscope-eu\s+(?P<init>\d{4}-\d{2}-\d{2}T\d{2}:\d{2})\b")


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def all_leads() -> list[int]:
    """Every stored lead in minutes: 0 (the observed satellite frame), then lead_steps forecast steps."""
    return [CONFIG["lead_step_min"] * i for i in range(CONFIG["lead_steps"] + 1)]


def place_queries() -> dict[str, str]:
    """config.json query filled in per sql.places entry; {init} stays for fill_init()."""
    sql = CONFIG["sql"]
    template = "\n".join(sql["query"]).replace("{box}", str(sql["box_deg"]))
    return {
        name: template.replace("{lat}", str(lat)).replace("{lon}", str(lon))
        for name, (lat, lon) in sql["places"].items()
    }


def default_sql() -> str:
    """The query for the first place in sql.places."""
    return next(iter(place_queries().values()))


def fill_init(template: str, init: str) -> str:
    """`template` with {init} set to `init` ("YYYY-MM-DDTHH:MM:SS"), as a tz-naive timestamp."""
    return template.replace("{init}", init.replace("T", " "))


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


# --------------------------------------------------------------------------- runs


def ref_by_init(commits: list[dict]) -> dict[str, str]:
    """Newest snapshot id whose commit message names each init 'YYYY-MM-DDTHH:MM'."""
    out: dict[str, str] = {}
    for c in commits:  # newest first
        m = MSG_RE.search(c.get("message") or "")
        if m:
            out.setdefault(m["init"], c["id"])
    return out


def build_runs(times: list[str], commits: list[dict]) -> list[dict]:
    """One run per init in the repo's `time` coordinate, newest first.

    The pipeline commits each init once, with every lead (`/api/config` lists them),
    so a run in `time` is always complete.

    `ref` is the snapshot that committed the run; runs older than the history get the
    oldest snapshot in it. Snapshot tile URLs are immutable and cached for a month.
    """
    refs = ref_by_init(commits)
    oldest = commits[-1]["id"] if commits else None
    return [
        {
            "init": t[:16],
            "time": t,
            "ref": refs.get(t[:16], oldest),
        }
        for t in sorted(set(times), reverse=True)
    ]


def open_repo():
    from arraylake import Client

    # falls back to `arraylake auth login`
    return Client(token=os.environ.get("ARRAYLAKE_API_TOKEN")).get_repo(CONFIG["repo"])


def read_commits(repo) -> list[dict]:
    """The newest `history_depth` commits on the branch, newest first."""
    return [
        {
            "id": snap.id,
            "message": snap.message,
            "written_at": snap.written_at.astimezone(dt.UTC).isoformat(),
        }
        for snap in itertools.islice(
            repo.ancestry(branch=CONFIG["branch"]), CONFIG["history_depth"]
        )
    ]


def read_times(repo) -> list[str]:
    """Init times currently held in the repo (the `time` coordinate, NaT slots dropped)."""
    import numpy as np
    import zarr

    session = repo.readonly_session(branch=CONFIG["branch"])
    group = zarr.open_group(session.store, mode="r")
    values = np.asarray(group["time"][:])
    return [np.datetime_as_string(v, unit="s") for v in values if not np.isnat(v)]


# --------------------------------------------------------------------------- SQL


def sql_host() -> str:
    return os.environ.get("ARRAYLAKE_SQL_HOST") or CONFIG["sql"].get("host") or ""


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


def run_sql_once(query: str) -> dict:
    """Run `query` on the Zax-SQL service; returns the fields of an /api/sql answer."""
    import psycopg

    notices: list[str] = []
    t0 = time.perf_counter()
    with psycopg.connect(
        host=sql_host(),
        port=5432,
        sslmode="require",
        user="arraylake",
        dbname=CONFIG["org"],
        password=os.environ["ARRAYLAKE_API_TOKEN"],
        autocommit=True,
        connect_timeout=15,
    ) as conn:
        conn.add_notice_handler(lambda d: notices.append(d.message_primary or ""))
        cur = conn.execute(query)
        cols = [d.name for d in cur.description or []]
        rows = [[_jsonable(v) for v in row] for row in cur.fetchmany(CONFIG["sql"]["max_rows"])]
    return dict(
        status="ok",
        query=query,
        columns=cols,
        rows=rows,
        seconds=round(time.perf_counter() - t0, 2),
        notices=notices[-3:],
        ran_at=utcnow().isoformat(),
    )


# --------------------------------------------------------------------------- HTTP


def send_json(handler, payload: dict, code: int = 200, cache: str = "no-store") -> None:
    """Answer `handler` (a BaseHTTPRequestHandler) with `payload` as JSON."""
    body = json.dumps(payload, default=str).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", cache)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def public_config(sql_editable: bool) -> dict:
    """The settings the page needs, no secrets. `sql_editable` is False on the hosted page."""
    keys = [
        "org",
        "repo",
        "tile_base",
        "data_bounds",
        "max_tile_level",
        "variables",
        "lead_steps",
        "run_interval_min",
        "run_phases_s",
        "max_runs",
        "panel_seconds",
        "playback_speed",
        "hold_ms",
        "station",
        "camera",
        "viewer_url",
        "qr_tile",
        "branch",
    ]
    return {k: CONFIG[k] for k in keys} | {
        "leads": all_leads(),
        "sql_places": place_queries(),
        "sql_editable": sql_editable,
    }
