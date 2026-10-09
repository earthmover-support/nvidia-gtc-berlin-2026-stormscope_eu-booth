# Offline booth backup

Goal: the booth page keeps running with no internet. Fetch while online, serve from disk.

    uv run presentation/server.py --fetch-offline   # online, ~3 h: writes presentation/offline/
    uv run presentation/server.py --offline         # no network needed

## Decisions (2026-10-09)

- 3 most recent runs, true_color + airmass, frames z0-5 (about 150 tiles per frame)
- Offline camera.max_zoom capped at 5 + 1 (512 px tiles at z5 still look fine at z6)
- Rotation: time-shift like `--demo`; a cached run "lands" every 10 min, cycling 3 runs
- Carto dark-matter cached for the same zooms: style, vector tiles, glyphs, sprites
- SQL: default query per run x place saved; custom queries answer "offline"
- Fetch step lives in server.py (`--fetch-offline`), reuses booth.py helpers

## Layout of presentation/offline/ (gitignored)

    state.json                      commits + runs at fetch time
    sql/<init>/<place>.json         saved default-query results
    tiles/<ref>/<variable>/<lead>/<z>/<y>/<x>.png
    basemap/style.json              URLs rewritten to /offline/basemap/...
    basemap/tiles/<z>/<x>/<y>.pbf, basemap/sprite*, basemap/fonts/<stack>/<range>.pbf
    fonts/                          Geist woff2 + css (or vendor npm `geist`)

## Tasks

- [ ] `--fetch-offline`: read commits/runs, take newest 3 runs, save state.json
- [ ] Fetch forecast tiles for z0-5 inside data_bounds (thread pool, skip existing files = resumable)
- [ ] Fetch Carto style + tilejson, rewrite URLs, fetch vector tiles z0-5 for the cover box, sprites, glyph ranges the style uses
- [ ] Run default SQL per run x place, save results
- [ ] Vendor Geist fonts (npm `geist` under /vendor/, drop Google Fonts link)
- [ ] `--offline`: serve /offline/ from disk; /api/config rewrites tile_base + basemap URL, caps max_zoom
- [ ] Offline state poller: time-shift the 3 runs into a 10-min cycle (reuse demo_commits shape)
- [ ] Offline SQL: answer from sql/<init>/<place>.json, else status "offline"
- [ ] app.js: take basemap URL from config (one line); local tile URL must match tileTemplate's path shape
- [ ] Verify: fetch, then turn Wi-Fi off, run --offline, check the page in Chrome (no failed requests)
- [ ] README section

QR codes stay online URLs: visitors' phones use mobile data, not the booth network.

## Open points

- Local tile path drops the query string: server maps `.../{ref}/tiles/WebMercatorQuad/{z}/{y}/{x}?variables=..&lead_time=..`
  to `tiles/<ref>/<variable>/<lead>/<z>/<y>/<x>.png`, so app.js needs no change for frames.
