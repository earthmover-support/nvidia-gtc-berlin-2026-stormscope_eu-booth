"""GET /api/config on the hosted page: the booth settings, with the SQL editor locked."""

import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from booth import CONFIG, public_config, send_json  # noqa: E402


class handler(BaseHTTPRequestHandler):  # noqa: N801 (name the Vercel runtime looks for)
    def do_GET(self):  # noqa: N802
        # Tiles through api/flux.js, so viewers share one render per tile (Vercel caches).
        tiles = {"tile_base": f"/flux/{CONFIG['repo'].split('/')[1]}"}
        send_json(self, public_config(sql_editable=False) | tiles, cache="public, s-maxage=300")
