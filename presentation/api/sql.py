"""GET /api/sql on the hosted page: ?place=<name>&init=<...> or ?lat=<..>&lon=<..>&init=<...>.

Runs only the config.json query for one of `sql.places` or for a coordinate inside
`data_bounds`, never free text from the browser. The URL names everything the answer
depends on, so the CDN keeps each answer for a day.
"""

import datetime as dt
import re
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from booth import (  # noqa: E402
    CONFIG,
    fill_init,
    place_queries,
    query_for_coords,
    run_sql_once,
    send_json,
    utcnow,
)

INIT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00")
MAX_AGE = dt.timedelta(days=7)


def check_init(init: str) -> str:
    """`init` if it can be a run time: on the run grid, not in the future, at most a week old."""
    if not INIT_RE.fullmatch(init):
        raise ValueError("init must look like YYYY-MM-DDTHH:MM:00")
    t = dt.datetime.fromisoformat(init).replace(tzinfo=dt.UTC)
    if t.minute % CONFIG["run_interval_min"] or not utcnow() - MAX_AGE <= t <= utcnow():
        raise ValueError("init is not a recent run")
    return init


def check_coords(lat: str, lon: str) -> tuple[float, float]:
    """Numeric lat/lon inside data_bounds [west, south, east, north]. Raises ValueError."""
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        raise ValueError("lat and lon must be numbers") from None
    if not (-90 <= la <= 90 and -180 <= lo <= 180):
        raise ValueError("lat and lon out of range")
    w, s, e, n = CONFIG["data_bounds"]
    if not (w <= lo <= e and s <= la <= n):
        raise ValueError(
            f"location ({la:.2f}, {lo:.2f}) is outside the StormScope-EU domain "
            f"(lon {w}…{e}, lat {s}…{n})"
        )
    return la, lo


class handler(BaseHTTPRequestHandler):  # noqa: N801 (name the Vercel runtime looks for)
    def do_GET(self):  # noqa: N802
        args = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
        if not args:
            return send_json(self, {"status": "idle"})
        queries = place_queries()
        try:
            if "place" in args:
                if args.get("place") not in queries:
                    raise ValueError(f"place must be one of {', '.join(queries)}")
                query = fill_init(queries[args["place"]], check_init(args.get("init", "")))
            elif "lat" in args or "lon" in args:
                la, lo = check_coords(args.get("lat", ""), args.get("lon", ""))
                query = fill_init(query_for_coords(la, lo), check_init(args.get("init", "")))
            else:
                raise ValueError(f"place must be one of {', '.join(queries)}")
        except ValueError as exc:
            return send_json(self, {"status": "error", "error": str(exc)}, 400)
        try:
            result = run_sql_once(query)
        except Exception as exc:
            return send_json(
                self, {"status": "error", "query": query, "error": str(exc)[:300]}, 502
            )
        send_json(self, result, cache="public, s-maxage=86400")
