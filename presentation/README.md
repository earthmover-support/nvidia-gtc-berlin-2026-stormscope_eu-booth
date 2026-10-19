# Booth screen

Web app for the GTC Berlin booth: a MapLibre globe fixed on Europe showing the
latest StormScope-EU run from Arraylake, rotating explainer panels, a live ingest strip,
QR codes and a live Zax-SQL query. It runs locally on the booth machine and is also
[hosted on Vercel](#hosted-on-vercel) for internal sharing, with the SQL editor locked.

```
presentation/
  server.py        booth: stdlib HTTP server + Arraylake/SQL pollers (uv inline deps)
  booth.py         logic shared by server.py and api/: runs, commits, SQL, public config
  api/             hosted: Vercel Python functions for /api/config, /api/state, /api/sql;
                   flux.js, the tile proxy
  requirements.txt hosted: the functions' dependencies
  vercel.json      hosted: build, output directory, function timeout
  config.json      everything tunable: tile service, variables, timings, station, SQL
  package.json     MapLibre GL + qrcode-generator, vendored so the booth runs without CDNs
                   (the dark CARTO basemap and the Earthmover Compute tiles still need the network)
  web/             index.html, app.css, app.js
```

## Run

```bash
cd presentation && npm install && cd ..        # once: vendors MapLibre into node_modules
uv run presentation/server.py                  # live → http://127.0.0.1:8000
uv run presentation/server.py --demo           # rehearsal: fake commits + SQL, no Arraylake
```

`server.py` reads `ARRAYLAKE_API_TOKEN` (and optionally `ARRAYLAKE_SQL_HOST`) from the repo's
`.env`. The token stays on the server; the browser only gets `/api/*` JSON and public tile URLs.

Booth window (full screen):

```bash
chromium --app=http://127.0.0.1:8000 --window-position=0,0 --window-size=1920,1080 \
         --noerrdialogs --disable-session-crashed-bubble
```

## Keys

| Key | Action |
| --- | --- |
| ← / → | previous / next panel (auto-rotation resumes after 60 s) |
| Space | pause / resume panel rotation |
| P | pause / resume the forecast loop |
| O | loop / play once (stops on +120) |
| S | cycle playback speed (slow 1 s / medium 0.5 s / fast 0.25 s per frame) |
| T / A | `true_color` / `airmass` |
| 1–9, L | pick an initialisation (1 = L = latest) |
| C | collapse / expand the ingest strip |

Every toggle (loop, pause, speed, variable, init, rotation, strip) reverts to the defaults
after `attract_reset_s` (config.json, 180 s) without any input, so a visitor's click never
sticks for the day. `config.json` is read when `server.py` starts: restart it after editing.

## Before the event

1. **Tiles**: `al compute enable gtc-berlin tiles --sku-code s --is-public`, then
   `al compute update gtc-berlin tiles-eu-8af53d67 --min-replicas 8 --max-replicas 8`
   (see [Viz](../README.md#viz)). The service is public, so browsers and phones fetch
   tiles without a token. Set `--min-replicas 0` again after booth hours.
   Each pod caches on its own and starts empty, so warm them up before doors open: load
   the booth page once and let one run play
   ([TILE_SERVER_PERFORMANCE.md](../docs/TILE_SERVER_PERFORMANCE.md#server-side-caches-and-http2-2026-10-15)).
2. **Inits**: the page reads `gtc-berlin/stormscope-eu_gtc_berlin_2026_EU_region` (root group). The
   pipeline appends every run's init to `time` and never drops one; the page offers the
   newest 144, the past 24 h (`max_runs`). The server re-reads the `time` coordinate whenever `main` moves,
   and each tile picks its frame with `time=` + `lead_time=` on the newest snapshot holding
   that run, so the browser can cache it.
   The pipeline commits each init once, with all its leads, so every init in `time` is
   complete; the server finds each run's snapshot from commit messages like
   `stormscope-eu 2026-09-15T11:00 lead 0-120 min`.
   Quick check that the tile server is up:
   ```bash
   B=https://tiles-edf5beae0d2afdc4.eu-west-1.compute.arraylake.app/v1/services/tiles-eu/gtc-berlin/stormscope-eu_gtc_berlin_2026_EU_region
   curl -o t.png -w '%{http_code}\n' "$B/main/tiles/WebMercatorQuad/3/2/4?variables=true_color&style=raster/rgb&colorscalerange=0,255&time=<INIT>&lead_time=10%20minutes&ensemble=0&f=png"
   ```
3. **SQL**: `al compute enable gtc-berlin sql --sku-code xs`; its hostname goes in
   `config.json` (`sql.host`) or `.env` (`ARRAYLAKE_SQL_HOST=...`, wins). The service
   scales to zero: the first query after a sleep takes ~50 s, later ones ~20 s (each
   lead reads a full 1120 × 2432 frame; 8–16 s measured warm on 2026-10-07). Fire one query before visitors arrive.
   Panel 08 is a live editor: click a city chip (`sql.places`) or type any single
   `SELECT`/`WITH` statement and press ⌘/Ctrl+Enter; Esc leaves the box.
   A city chip fills the editor and runs at once; Reset only fills it. The server runs the
   query (with the token; the browser never sees it), and once by itself for each new init: it
   resets the editor to the first city (Berlin) and keeps that result in memory until the
   next init. `{init}` is replaced by the newest complete init; `{lat}`/`{lon}`/`{box}` in `sql.query` are filled per city. `lat`/`lon` are
   2-D pixel coordinates, so filter on a `BETWEEN` box, never on equality.
4. **Variables**: `true_color` and `airmass` are both uint8 0-255 RGB composites
   (`raster/rgb`, `colorscalerange` `0,255`). Add more under `variables` in `config.json`.
5. **Run length**: the strip and scrubber show the observed +0 frame plus 12 steps,
   +10 to +120 min (`lead_steps`, `lead_step_min`). Keep the commit-message format in the ingest: `stormscope-eu <init> ...`,
   one commit per init.
6. `viewer_url` in `config.json` drives the first QR code on the last panel (slide 8).
7. **Time zone**: every time on screen is shown in `timezone` (`config.json`, `Europe/Berlin`),
   whatever the booth machine or a hosted viewer's browser is set to. Only the SQL
   `WHERE time = TIMESTAMP '{init}'` stays in UTC.
8. **Offline backup**: with tiles and SQL up, save a fallback in case the venue has no
   internet:
   ```bash
   uv run presentation/server.py --fetch-offline   # ~25 min with 8 replicas; rerun resumes
   uv run presentation/server.py --offline         # no network needed
   ```
   The fetch saves the 3 newest runs (both variables, ~170 tiles per frame up to z5), the
   Carto basemap for the same zooms, the Geist fonts and each run's city SQL results into
   `presentation/offline/` (gitignored, ~1.2 GB). `--offline` replays the saved runs on the
   demo cadence, one landing every 10 min; the header reads REPLAY and each commit names
   the run it replays. Zoom stops at 6 (one level past the saved tiles), and only the city
   SQL queries answer. The QR codes still point online: phones use mobile data.

## Hosted on Vercel

The same page, for internal sharing at an unlisted URL. Earthmover owns the Vercel project
and the token. Differences from the booth:

- **SQL is locked.** `/api/config` answers `sql_editable: false`: the editor is read-only,
  Reset is hidden, and the city chips pick the query. Run sends
  `GET /api/sql?place=<city>&init=<newest init>`. The function only runs the `config.json`
  query for a `sql.places` entry, and only for an init on the 10-minute grid from the last
  7 days. It rejects anything else, including query text and POSTs. The CDN keeps each
  answer for a day, so each city costs at most one SQL run per forecast run, however
  many people press Run.
- **No pollers.** `/api/state` reads commits and the `time` coordinate on each request
  (about 1.5 s warm, 8 s cold) and the CDN caches it for 5 s with
  stale-while-revalidate, so viewers polling every 4 s rarely wait.
- **Tiles go through a caching proxy.** `/api/config` answers `tile_base: /flux/<repo>`, and
  `vercel.json` rewrites `/flux/*` to `api/flux.js` (Node.js, pinned to `dub1` next to Flux in
  eu-west-1; the Python functions keep their default region). It forwards only this repo's
  tiles (branch or snapshot id) and keeps successful ones for a day in Vercel's CDN and in
  the Runtime Cache, which every region shares, so viewers share one render per tile instead
  of each making Flux render it again. Errors such as a `504` are not cached. Tiles are tagged
  `tiles` and `init-<YYYYMMDDHHMM>` (`vercel cache invalidate --tag ...`). The booth's
  `server.py` still fetches tiles straight from Flux. The "Raw tiles" QR code points at the
  proxy URL.
- **Warm each new run before viewers open it.** The CDN doesn't merge simultaneous misses,
  so viewers opening a new run together still make Flux render each tile several times.
  `scripts/warm_presentation_tiles.py` polls `/api/state` and requests the new run's globe
  tiles (z4 home view plus the composite previews, both variables, every lead: 754 tiles,
  ~20 s) once through the proxy, with the URLs `tileTemplate` builds. Keep it running while
  the page gets traffic, pointed at the exact hostname viewers use:

  ```bash
  uv run --no-project python scripts/warm_presentation_tiles.py https://<this page>.vercel.app
  ```

  Measured 2026-10-15 on a test deployment: after it ran, a browser opening the page got
  227/227 tiles as `x-vercel-cache: HIT`. Zooming or panning past the home view still misses.
- **Windows smaller than 1920 × 1080** show the booth layout zoomed down to fit. At
  1920 × 1080 the booth is unaffected.
- The vendored libraries are copied into `web/vendor/` at build time (`npm run vendor`),
  so the page loads them from its own origin, as at the booth.

Vercel project settings: root directory `presentation`, framework preset "Other";
install, build and output directory come from `vercel.json`. Environment variables:

| Variable | Value |
| --- | --- |
| `ARRAYLAKE_API_TOKEN` | read-only token with access to `gtc-berlin/stormscope-eu_gtc_berlin_2026_EU_region`; also the SQL password |
| `ARRAYLAKE_SQL_HOST` | optional, overrides `sql.host` in `config.json` |

The tiles and SQL services must stay up while the link is shared. Functions may run 60 s
(`maxDuration`, allowed on Hobby and Pro), enough for a cold SQL service. `vercel.json`
skips deploys of commits that change nothing under `presentation/`. Only `main` deploys to
production; pushes to other branches make preview deployments.

Check a build locally without deploying anything. With a made-up local project link,
`vercel build` works offline. It writes a `pyproject.toml` and `uv.lock` generated from
`requirements.txt` next to them, so build in a throwaway copy:

```bash
B=$(mktemp -d) && git -C presentation ls-files -co --exclude-standard | grep -v '^node_modules' \
  | tar -C presentation -cf - -T - | tar -C "$B" -xf -
mkdir "$B/.vercel" && echo '{"projectId":"local","orgId":"local","settings":{"framework":null}}' > "$B/.vercel/project.json"
(cd "$B" && vercel build --yes)    # -> .vercel/output/static (the page) and .vercel/output/functions/api/*.func
```

Each function's `.vc-config.json` lists the files it ships with (`filePathMap`).


## Eight-panel scientific narrative

The eight panels introduce the scientific definition of nowcasting, then explain
FCI observations, generative autoregressive prediction,
recurring Earth2Studio cycles, overlapped uploads and atomic Icechunk publication,
Compute tiles and SQL, and the complete workflow. Observation cards show real +0
composite tiles over the configured Berlin tile and switch the globe layer when
clicked; unavailable previews retain an explicit fallback.

All booth timestamps default to Europe/Berlin; only the SQL init stays UTC. The persistent map status shows the displayed
run's observation/initialization time, matching snapshot publication time, observation
age, and forecast horizon **from initialization**. The API reports publications, not
GPU progress: the bottom strip's countdown and phase activity are labeled estimates.
A publication more than two configured run intervals old is marked overdue; this does
not diagnose the pipeline. Demo and offline replay clearly identify simulated timing.
SQL reports mean brightness -- high means cloudy, low means sunny or dark.

The model feedback, recurring-cycle, upload, delivery, and architecture diagrams are
schematics, not live measurements. Viewer and raw-tile QR codes share one grid
version and quiet-zone style so both scan the same way.

The opening panel introduces observation-based nowcasting and the end-to-end demo.
The approved seven-panel wording is implemented with minor typo corrections and an
initial-condition sentence completed from its unfinished draft. Headings retain readable
Title Case and 24 px bold numbered titles; scientific acronyms keep established casing.
The panel column retains its original 560 px width at the 1080p booth size.
Visual spacing is compacted to fit the approved copy and diagrams.


Panel navigation includes Pause/Resume and eight directly selectable chapter markers.
Manual navigation holds a chapter for 60 s; focusing or using the SQL editor extends
that hold to 120 s. The visible progress marker follows the same timing as rotation.
Hidden panels are inert so their buttons, links, and editor stay out of keyboard navigation.
The header labels the current wall clock; forecast valid time stays on the globe.
Body copy uses 16 px text at the 1080p booth size, with 15–18 px diagram labels.


The persistent forecast-cycle status includes separate observation, initialization,
valid, and publication times (shown in Europe/Berlin); displayed forecast lead; and snapshot ID. The observation
is the newest input frame of the displayed published run, which defines its initialization.
Incoming uncommitted satellite scans are not reported by the dashboard API. The valid
and lead fields follow playback and scrubbing, while the snapshot follows run selection.
Publication history explicitly labels each committed snapshot Published.


## Shared-reference visual refresh

Font Awesome Free 6.7.2 symbols are served as `web/img/fontawesome.svg`; no icon CDN
or icon font is needed. `npm run icons` regenerates the selected SVG symbols and
copies the package license. `npm run vendor` also performs this step for hosted builds.
Rounded diagrams consistently distinguish observations (slate), NVIDIA compute
(NVIDIA green), Arraylake I/O and atomic publication (Earthmover lavender), with text labels.

The model panel loads actual snapshot-pinned +0 thumbnails from published runs whose
initialization matches each required observed timestamp, and forecast thumbnails from
the displayed run's published lead array. Missing observations are never substituted
with predictions; availability counts remain visible. Thumbnails load when their panel
is active. The shifting context is an illustration, paused with panel rotation and
suppressed when reduced motion is requested, rather than GPU progress telemetry.

The Earth2Studio panel shows three separate published runs with observation-window,
initialization, horizon, and publication times. It does not infer exact scheduling
cadence from satellite scan frequency. Publication history adds absolute publication
time (Europe/Berlin) alongside status, snapshot, and age, and highlights genuinely new commit IDs.

Compute shows a snapshot-pinned map tile beside live SQL results. The editable query
remains available through Inspect / Edit SQL; hosted read-only queries use Inspect SQL.
The result header identifies the queried composite. The final panel uses a thin
architecture ribbon and prioritizes the viewer QR over developer tiles.


Redeployed 2026-10-19 (booth page startup race fixed, store init time 10 min after the FCI slot #111, #112).
