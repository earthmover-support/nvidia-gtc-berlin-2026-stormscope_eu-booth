#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "arraylake[obstore]>=1.3",
#     "python-dotenv",
# ]
# ///
"""Backup booth page: no tile server.

The browser reads the Icechunk repo itself (icechunk-js + zarrita) and renders it on the
GPU (deck.gl-raster). This server only does two dumb things:

  /            the built page from ./dist (npm run build)
  /icechunk/*  raw Icechunk objects (repo, snapshots, manifests, chunks), byte ranges
               included, read from the repo's bucket with Arraylake-vended credentials

The proxy exists only because the Earthmover-managed R2 bucket is private and sends no
CORS headers; it never decodes, renders or reprojects anything. With a public,
CORS-enabled bucket the page could point at the bucket URL and this file would go away.

Run from the repo root:

    cd backup && npm install && npm run build && cd ..
    uv run backup/server.py                  # -> http://127.0.0.1:8001
"""

from __future__ import annotations

import argparse
import logging
import os
import re
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import obstore
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
DIST = HERE / "dist"
load_dotenv(HERE.parent / ".env")

REPO = "gtc-berlin/stormscope-eu_gtc_berlin_2026"
RANGE_RE = re.compile(r"bytes=(\d+)-(\d*)$")

log = logging.getLogger("backup")


def open_repo_store(repo: str) -> tuple[obstore.store.ObjectStore, str]:
    """obstore handle on the repo's bucket plus the repo's key prefix inside it."""
    from arraylake import Client

    client = Client(token=os.environ.get("ARRAYLAKE_API_TOKEN"))  # falls back to `arraylake auth login`
    org, name = repo.split("/")
    info = client.get_repo_object(repo)
    store = client.get_obstore_for_bucket(org=org, nickname=info.bucket.nickname)
    prefix = "/".join(p for p in (info.bucket.prefix, info.prefix) if p)
    log.info("serving %s from bucket %s, prefix %s", repo, info.bucket.name, prefix)
    return store, prefix


class Handler(SimpleHTTPRequestHandler):
    store: obstore.store.ObjectStore
    prefix: str

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(DIST), **kw)

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")  # lets `npm run dev` (vite) use this proxy
        self.send_header("Access-Control-Expose-Headers", "Content-Range, Content-Length")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD")
        self.send_header("Access-Control-Allow-Headers", "Range")
        self.end_headers()

    def do_HEAD(self):
        if self.path.startswith("/icechunk/"):
            return self._icechunk(head=True)
        return super().do_HEAD()

    def do_GET(self):
        if self.path.startswith("/icechunk/"):
            return self._icechunk(head=False)
        return super().do_GET()

    def _icechunk(self, head: bool):
        rel = self.path.split("?", 1)[0].removeprefix("/icechunk/")
        if ".." in rel.split("/"):
            return self.send_error(400)
        key = f"{self.prefix}/{rel}"
        rng = RANGE_RE.match(self.headers.get("Range", ""))
        try:
            if head:
                meta = obstore.head(self.store, key)
                body, size, status = b"", meta["size"], 200
            elif rng:
                start = int(rng[1])
                if rng[2]:
                    body = bytes(obstore.get_range(self.store, key, start=start, end=int(rng[2]) + 1))
                else:
                    body = bytes(obstore.get(self.store, key, options={"range": {"offset": start}}).bytes())
                status, size = 206, None
            else:
                body = bytes(obstore.get(self.store, key).bytes())
                status, size = 200, len(body)
        except FileNotFoundError:
            return self.send_error(404)
        except Exception as e:  # noqa: BLE001  credentials, network: surface as 502, keep serving
            log.warning("GET %s failed: %s", key, e)
            return self.send_error(502, str(e)[:200])

        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size if head else len(body)))
        if status == 206:
            self.send_header("Content-Range", f"bytes {int(rng[1])}-{int(rng[1]) + len(body) - 1}/*")
        # `repo` holds the branch refs and changes on every commit; everything else is
        # content-addressed (snapshots, manifests, chunks) and never changes.
        immutable = rel != "repo" and not rel.startswith("refs/")
        self.send_header("Cache-Control", "public, max-age=31536000, immutable" if immutable else "no-cache")
        self.end_headers()
        if not head:
            self.wfile.write(body)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # arraylake logs every API call at INFO

    if not DIST.exists():
        log.warning("%s missing: run `npm install && npm run build` in backup/ (or use `npm run dev`)", DIST)
    Handler.store, Handler.prefix = open_repo_store(args.repo)
    log.info("http://%s:%d", args.host, args.port)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
