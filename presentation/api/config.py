"""GET /api/config on the hosted page: the booth settings, with the SQL editor locked."""

import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from booth import public_config, send_json  # noqa: E402


class handler(BaseHTTPRequestHandler):  # noqa: N801 (name the Vercel runtime looks for)
    def do_GET(self):  # noqa: N802
        send_json(self, public_config(sql_editable=False), cache="public, s-maxage=300")
