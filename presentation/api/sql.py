"""GET /api/sql?place=<name>&init=<YYYY-MM-DDTHH:MM:SS> on the hosted page.

Runs only the config.json query for one of `sql.places`, never text from the browser.
The URL names everything the answer depends on, so the CDN keeps each answer for a day:
one SQL run per city and forecast run, however many people press Run.
"""

import datetime as dt
import re
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from booth import CONFIG, fill_init, place_queries, run_sql_once, send_json, utcnow  # noqa: E402

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


class handler(BaseHTTPRequestHandler):  # noqa: N801 (name the Vercel runtime looks for)
    def do_GET(self):  # noqa: N802
        args = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
        if not args:
            return send_json(self, {"status": "idle"})
        queries = place_queries()
        try:
            if args.get("place") not in queries:
                raise ValueError(f"place must be one of {', '.join(queries)}")
            query = fill_init(queries[args["place"]], check_init(args.get("init", "")))
        except ValueError as exc:
            return send_json(self, {"status": "error", "error": str(exc)}, 400)
        try:
            result = run_sql_once(query)
        except Exception as exc:
            return send_json(
                self, {"status": "error", "query": query, "error": str(exc)[:300]}, 502
            )
        send_json(self, result, cache="public, s-maxage=86400")
