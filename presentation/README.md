# Booth screen

Local web app for the GTC Berlin booth: a MapLibre globe fixed on Europe showing the
latest StormScope-EU run from Arraylake, rotating explainer panels, a live ingest strip,
QR codes and a live Zax-SQL query. The Slack app runs in its own window next to it.

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

`server.py` reads `ARRAYLAKE_TOKEN` (and optionally `ARRAYLAKE_SQL_HOST`) from the repo's
`.env`. The token stays on the server; the browser only gets `/api/*` JSON and public tile URLs.

Booth window (Slack takes the right ~30 %):

```bash
chromium --app=http://127.0.0.1:8000 --window-position=0,0 --window-size=1344,1080 \
         --noerrdialogs --disable-session-crashed-bubble
```

## Keys

| Key | Action |
| --- | --- |
| ← / → | previous / next panel (auto-rotation resumes after 60 s) |
| Space | pause / resume panel rotation |
| P | pause / resume the forecast loop |
| T / C | `true_color` / `cloud_mass` |
| 1–6, L | pick an initialisation (1 = L = latest) |

## Before the event

1. **Tiles**: `al compute enable gtc-berlin tiles --sku-code xs`. The service is set
   public (`is_public: true`), so browsers and phones fetch tiles without a token.
   Disable it again after booth hours.
2. **Inits**: the page reads `gtc-berlin/stormscope-eu`, whose `time` dimension is a
   rolling window of the last 6 inits. The server re-reads the `time` coordinate whenever
   `main` moves, and each tile picks its frame with `time=` + `lead_time=` on `main`.
   The newest run only offers the leads already committed (parsed from commit messages
   like `stormscope-eu 2026-09-15T11:00 +30 min`), so no unwritten tile gets cached.
   Quick check that the tile server is up:
   ```bash
   B=https://tiles-9db3ca32bddbb94b.compute.arraylake.app/v1/services/tiles/gtc-berlin/stormscope-eu
   curl -o t.png -w '%{http_code}\n' "$B/main/tiles/WebMercatorQuad/3/2/4?variables=true_color&style=raster/rgb&colorscalerange=0,0.15&time=<INIT>&lead_time=10%20minutes&ensemble=0&f=png"
   ```
3. **SQL**: `al compute enable gtc-berlin sql --sku-code xs`; its hostname goes in
   `config.json` (`sql.host`) or `.env` (`ARRAYLAKE_SQL_HOST=...`, wins). The service
   scales to zero: the first query after a sleep takes ~50 s, later ones ~20 s (each
   lead reads a full 1120 × 2432 chunk). Fire one query before visitors arrive.
   Panel 06 is a live editor: click a city chip (`sql.places`) or type any single
   `SELECT`/`WITH` statement and press Run or ⌘/Ctrl+Enter; Esc leaves the box.
   The server runs it with the token (the browser never sees it) and keeps refreshing
   it on every new commit until Reset. `{init}` is replaced by the newest complete
   init; `{lat}`/`{lon}`/`{box}` in `sql.query` are filled per city. `lat`/`lon` are
   2-D pixel coordinates, so filter on a `BETWEEN` box, never on equality.
4. **cloud_mass**: once the variable exists in the repo, set its `style` /
   `colorscalerange` and `"available": true` in `config.json`.
5. **Run length**: the strip and scrubber assume 12 steps, +10 to +120 min
   (`lead_steps`). Keep the commit-message format in the ingest: `... schema`,
   `... analysis frame (lead 0 min)`, `... +N min`.
6. Fill `viewer_url` in `config.json` for the second QR code, and swap the two logo
   placeholders in `web/index.html` for image files in `web/img/`.
