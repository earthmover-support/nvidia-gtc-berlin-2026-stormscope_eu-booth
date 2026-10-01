# Backup booth page: no tile server

StormScope-EU read straight from the Icechunk repo in the browser and rendered on the
GPU with [deck.gl-raster](https://github.com/developmentseed/deck.gl-raster). Use it if
the Flux tile service is down, or to show the "no server" story.

```
backup/
  server.py      serves dist/ + a dumb byte proxy to the repo's bucket (uv inline deps)
  src/store.ts   icechunk-js + zarrita: open `main`, read time/lead_time, the RGB arrays
  src/geos.ts    teaches proj4js the repo's geostationary CRS (see "Workarounds")
  src/main.ts    MapLibre + ZarrLayer, playback, polling for new commits
```

## Run

```bash
cd backup && npm install && npm run build && cd ..
uv run backup/server.py                 # -> http://127.0.0.1:8001
```

Reads `ARRAYLAKE_API_TOKEN` from the repo's `.env` (falls back to `arraylake auth login`).
While editing the page: keep `server.py` running and use `npm run dev` (port 3000, proxies
`/icechunk` to it).

Keys: Space/P pause, T true colour, A airmass, L latest run.

## Tile server vs. reading the data in the browser

**What a tile server does.** Web maps draw the world as a pyramid of 256 × 256 px
images in Web Mercator, addressed `z/x/y`. The map asks for exactly the tiles on screen.
Our data isn't stored that way: it's a 1120 × 2432 float32 grid in the satellite's own
geostationary projection, in Zarr chunks of 560 × 1216 × 3. So something has to, for each
tile request: find the chunks it overlaps, read and decompress them, reproject them from
geostationary to Mercator, colour-scale the floats to 8-bit RGB, and encode a PNG.
The Flux tile service (`tiles-*.compute.arraylake.app`) does that on a server next to the
data. It holds the Arraylake credentials, so the browser gets small public PNGs
(roughly 100-300 KB per screen) and knows nothing about Zarr, projections or auth.

**What deck.gl-raster does instead.** It moves every one of those steps into the
browser: icechunk-js + zarrita fetch and decompress the chunks over plain HTTP range
requests, proj4js gives the projection, and deck.gl-raster builds a mesh per chunk that
warps it into Mercator *on the GPU* (an adaptive triangle mesh, refined until the
error is under 1/8 px). The chunk itself is the tile, and nothing is pre-rendered.
That only needs the data to be:

1. reachable over HTTP from the browser (public, or signed URLs, and **CORS** headers),
2. described well enough to place it: CRS + affine transform (the GeoZarr
   `proj:*` / `spatial:*` attributes this repo already writes on its root group).

**Trade-offs, measured here:**

| | Flux tiles | this page |
|---|---|---|
| server | tile service running (and paid for) during booth hours | none needed with a public bucket; here a byte proxy (below) |
| bytes per frame | ~0.1-0.3 MB PNG | ~27 MB (4 chunks of 5-7 MB zstd float32) |
| first loop | fast | network-bound: about 2.6 s per frame at about 5 MB/s to R2 |
| later loops | one request per tile | from memory, 0.4 s per frame |
| resolution | resampled per zoom | full 2 km native pixels at every zoom |
| styling | server parameters (`colorscalerange`, `style`) | anything, client-side |
| north of ~50°N | cut off (rows with NaN 2-D lat/lon are dropped) | fine (no 2-D lat/lon used) |

## Why there is still a `server.py`

The repo lives in an Earthmover-managed Cloudflare R2 bucket that is private and returns
`403` to browser CORS preflights. A browser can't read it directly, with or without
credentials. `server.py` fetches raw Icechunk objects (`repo`, snapshots, manifests,
chunks) with Arraylake-vended read credentials and passes the bytes through, Range
requests included. It never decodes or renders anything. Content-addressed objects are
marked immutable, so the browser's HTTP cache serves reloads. With a public,
CORS-enabled bucket the page would point `REPO_URL` (`src/store.ts`) at the bucket and
the proxy would go away.

## Workarounds in the code (each one is small, commented, and upstream-able)

- **datetime dtypes** (`store.ts`): zarrita has no `numpy.datetime64`/`timedelta64`.
  A thin store wrapper presents `time`/`lead_time` as `int64` (their on-disk form) and
  converts units itself.
- **trailing `rgb` dim** (`main.ts`, `withoutRgb`): ZarrLayer tiles on the last two
  dims, but ours are `[..., y, x, rgb]`. The layer gets a view without `rgb`, and the
  real read appends it.
- **geostationary CRS** (`geos.ts`): proj4js has `geos` but doesn't recognise pyproj's
  method name "Geostationary Satellite (Sweep Y)" or wkt-parser's `satellite_height`,
  so it's registered as an alias. The crop's northern corners look past the Earth's limb
  (NaN), which breaks deck.gl's tile bounds and mesh refinement. Off-disk scan angles are
  folded onto the far side of the globe instead: the transform stays continuous and
  exactly invertible, and those pixels are NaN in the data, so they draw transparent.
  On-disk it matches proj4's `geos` exactly (Berlin, 52.5°N 13.4°E, lands on grid cell
  y=798, x=1348, as read from the repo's own `lat`/`lon`).
