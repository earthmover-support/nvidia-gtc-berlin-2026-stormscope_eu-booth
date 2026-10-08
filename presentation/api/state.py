"""GET /api/state on the hosted page: recent commits and runs, read fresh per request.

The booth server polls in a thread instead; here the CDN keeps it to about one
Arraylake read every few seconds however many viewers poll.
"""

import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from booth import build_runs, open_repo, read_commits, read_times, send_json, utcnow  # noqa: E402


class handler(BaseHTTPRequestHandler):  # noqa: N801 (name the Vercel runtime looks for)
    def do_GET(self):  # noqa: N802
        try:
            repo = open_repo()
            commits = read_commits(repo)
            runs = build_runs(read_times(repo), commits)
        except Exception as exc:
            state = dict(status="error", error=str(exc)[:300], commits=[], runs=[])
            return send_json(self, state | {"updated_at": utcnow().isoformat()}, 503)
        state = dict(status="ok", commits=commits[:20], runs=runs)
        send_json(
            self,
            state | {"updated_at": utcnow().isoformat()},
            cache="public, s-maxage=5, stale-while-revalidate=60",
        )
