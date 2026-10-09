# site

A static MapLibre globe of the latest StormScope-EU nowcasts. Tiles come straight from the
public Earthmover Flux tiles service (`gtc-berlin`), each init pinned to the snapshot that
committed it (see [Snapshot ids](#snapshot-ids)). The page has an init
selector, a true colour / airmass toggle, and playback over lead times. Playback holds the
first frame until all leads have loaded. Every minute it checks for a new init, loads it in
the background, and switches to it once it is complete. No build step:
`index.html`, `sw.js`, three logo SVGs and one Vercel function, `api/refs.py`.

## Snapshot ids

`api/refs.py` answers `GET /api/refs` with the snapshot id that committed each init, read
from the branch's commit messages. Tile URLs use that id instead of `main`, which saves the
tiles service a branch lookup on every tile, and the service then sends them with a month-long
`max-age` instead of `max-age=0`. The init list itself still comes from `main`, so new inits
show up. If the function is down or slower than 5 s, or the page is served without it,
tiles fall back to `main`. It needs `ARRAYLAKE_API_TOKEN` (read access to the repo) in the
Vercel project's environment variables.

## Tile cache

The tiles service sends branch tiles with `max-age=0`, so the browser would ask the service
again for every tile it has already seen. `sw.js` is a service worker that answers tile
requests from the browser's Cache API instead, and only goes to the service on a miss.
This is safe because the pipeline publishes each init in a single commit: once an init is
listed, its tiles never change. Only successful responses are stored, and only the newest
12 inits are kept.

Change `CACHE` in `sw.js` (`tiles-v1` → `tiles-v2`) and redeploy if tiles change under the
same URL: an init that was rerun, a reset branch, or a tiles service upgrade that renders
differently. To clear it in one browser: DevTools → Application → Storage → Clear site data.

## Run locally

Serve the folder over HTTP from the repo root:

```bash
uv run --no-project python -m http.server 8765 --directory site
```

Then open http://localhost:8765. The tiles service must be up and public (see
[Viz](../README.md#viz) in the top-level README). The first frames take a while because the XS service
renders slowly.

Plain `http.server` has no `/api/refs`, so tiles go through `main`. `vercel dev` in `site/`
runs the function too.

## Hosting

Earthmover hosts this page on Vercel, deployed from `main` with root directory `site`,
framework preset "Other" and no build command. `vercel.json` sets an ignored build step,
so Vercel skips deploying commits that change nothing under `site/`. The tiles service sends
`Access-Control-Allow-Origin: *`, so the page works from any domain. Only `main` deploys to
production; pushes to other branches make preview deployments.
