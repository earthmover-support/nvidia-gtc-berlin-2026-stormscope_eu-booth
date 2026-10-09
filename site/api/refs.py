"""GET /api/refs: the snapshot id that committed each init, {"YYYY-MM-DDTHH:MM": id}.

The page pins tile URLs to these instead of the branch: the tiles service then skips
resolving `main` per tile, and answers with a month-long max-age instead of max-age=0.
The CDN keeps it to about one Arraylake read every few seconds however many viewers poll.
"""

import itertools
import json
import os
import re
from http.server import BaseHTTPRequestHandler

REPO = "gtc-berlin/stormscope-eu_gtc_berlin_2026_EU_region"
# "stormscope-eu 2026-09-15T23:50 lead 0-120 min": one commit per init
MSG_RE = re.compile(r"stormscope-eu\s+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})\b")


def refs_by_init(snapshots) -> dict[str, str]:
    """Newest snapshot id whose message names each init; `snapshots` newest first."""
    out: dict[str, str] = {}
    for s in snapshots:
        m = MSG_RE.search(s.message or "")
        if m:
            out.setdefault(m[1], s.id)
    return out


class handler(BaseHTTPRequestHandler):  # noqa: N801 (name the Vercel runtime looks for)
    def do_GET(self):  # noqa: N802
        try:
            from arraylake import Client

            repo = Client(token=os.environ.get("ARRAYLAKE_API_TOKEN")).get_repo(REPO)
            body = refs_by_init(itertools.islice(repo.ancestry(branch="main"), 120))
            code, cache = 200, "public, s-maxage=5, stale-while-revalidate=60"
        except Exception as exc:
            body, code, cache = {"error": str(exc)[:300]}, 503, "no-store"
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    from types import SimpleNamespace as S

    snaps = [
        S(id="C", message="stormscope-eu 2026-10-09T08:50 lead 0-120 min"),
        S(id="B", message="rerun"),
        S(id="A", message="stormscope-eu 2026-10-09T08:50 lead 0-120 min"),
        S(id="Z", message="stormscope-eu 2026-10-09T08:40 lead 0-120 min"),
        S(id="R", message=None),
    ]
    assert refs_by_init(snaps) == {"2026-10-09T08:50": "C", "2026-10-09T08:40": "Z"}
    print("ok")
