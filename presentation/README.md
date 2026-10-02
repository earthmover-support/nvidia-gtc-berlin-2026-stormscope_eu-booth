# Booth screen

Local web app for the GTC Berlin booth: a MapLibre globe fixed on Europe showing the
latest StormScope-EU run from Arraylake, rotating explainer panels, a live ingest strip,
QR codes and a live Zax-SQL query.

```
presentation/
  server.py      stdlib HTTP server + Arraylake/SQL pollers (uv inline deps)
  config.json    everything tunable: tile service, variables, timings, station, SQL
  package.json   MapLibre GL + qrcode-generator, vendored so the booth runs without CDNs
                 (the dark CARTO basemap and the Flux tiles still need the network)
  web/           index.html, app.css, app.js
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
| S | cycle playback speed (slow / normal / fast) |
| T / A | `true_color` / `airmass` |
| 1–6, L | pick an initialisation (1 = L = latest) |

## Before the event

1. **Tiles**: `al compute enable gtc-berlin tiles --sku-code xs`. The service is set
   public (`is_public: true`), so browsers and phones fetch tiles without a token.
   Disable it again after booth hours.
2. **Inits**: the page reads `gtc-berlin/stormscope-eu_gtc_berlin_2026` (root group). The
   pipeline appends every run's init to `time` and never drops one; the page offers the
   newest 6 (`max_runs`). The server re-reads the `time` coordinate whenever `main` moves,
   and each tile picks its frame with `time=` + `lead_time=` on the newest snapshot holding
   that run (`tile_ref_mode: "snapshot"`), so the browser can cache it.
   The newest run only offers the leads already committed (parsed from commit messages
   like `stormscope-eu 2026-09-15T11:00 +30 min`), so no unwritten tile gets cached.
   Quick check that the tile server is up:
   ```bash
   B=https://tiles-9db3ca32bddbb94b.compute.arraylake.app/v1/services/tiles/gtc-berlin/stormscope-eu_gtc_berlin_2026
   curl -o t.png -w '%{http_code}\n' "$B/main/tiles/WebMercatorQuad/3/2/4?variables=true_color&style=raster/rgb&colorscalerange=0,1&time=<INIT>&lead_time=10%20minutes&ensemble=0&f=png"
   ```
3. **SQL**: `al compute enable gtc-berlin sql --sku-code xs`; its hostname goes in
   `config.json` (`sql.host`) or `.env` (`ARRAYLAKE_SQL_HOST=...`, wins). The service
   scales to zero: the first query after a sleep takes ~50 s, later ones ~20 s (each
   lead reads a full 1120 × 2432 frame). Fire one query before visitors arrive.
   Panel 06 is a live editor: click a city chip (`sql.places`) or type any single
   `SELECT`/`WITH` statement and press Run or ⌘/Ctrl+Enter; Esc leaves the box.
   Nothing runs on its own: chips and Reset only fill the editor, and the server runs
   a query (with the token; the browser never sees it) only when Run is pressed. `{init}` is replaced by the newest complete
   init; `{lat}`/`{lon}`/`{box}` in `sql.query` are filled per city. `lat`/`lon` are
   2-D pixel coordinates, so filter on a `BETWEEN` box, never on equality.
4. **Variables**: `true_color` and `airmass` are both 0-1 RGB composites
   (`raster/rgb`, `colorscalerange` `0,1`). Add more under `variables` in `config.json`.
5. **Run length**: the strip and scrubber assume 12 steps, +10 to +120 min
   (`lead_steps`). Keep the commit-message format in the ingest: `... schema`,
   `... analysis frame (lead 0 min)`, `... +N min`.
6. `viewer_url` in `config.json` drives the second QR code on the "access" panel (slide 6).
