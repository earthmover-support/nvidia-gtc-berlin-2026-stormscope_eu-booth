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

- [x] `--fetch-offline`: newest 3 runs -> runs.json
- [x] Forecast tiles z0-5 inside data_bounds (8 threads, existing files skipped = resumable)
- [x] Carto style with local URLs: vector tiles (world z0-3, data_bounds z4-5), sprites, glyphs
- [x] Default SQL per run x place
- [x] Geist fonts: Google CSS + woff2 saved; --offline rewrites the link in index.html
- [x] `--offline`: serve /offline/; /api/config swaps tile_base + basemap, caps max_zoom at 6
- [x] Replay poller on the demo cadence (reuses demo_commits); header reads REPLAY
- [x] Offline SQL: saved result for city queries, error for anything else
- [x] app.js: basemap from config, REPLAY / saved labels
- [x] Verify in Chrome (see review)
- [x] README section

## Review (2026-10-09)

- Real `--fetch-offline` NOT run yet: tiles service is disabled between events. Basemap + fonts
  were fetched for real (299/299 files, 31 MB); runs, frame tiles and SQL were stand-ins.
- `--offline` checked in Chrome: basemap with labels, frames, REPLAY badge, init rotation, SQL
  panel. All 66 page requests went to localhost and returned 200.
- `test_offline.py`: tile ranges, URL -> file mapping, replay rotation (3 passed).

QR codes stay online URLs: visitors' phones use mobile data, not the booth network.

## Open points

- Local tile path drops the query string: server maps `.../{ref}/tiles/WebMercatorQuad/{z}/{y}/{x}?variables=..&lead_time=..`
  to `tiles/<ref>/<variable>/<lead>/<z>/<y>/<x>.png`, so app.js needs no change for frames.
