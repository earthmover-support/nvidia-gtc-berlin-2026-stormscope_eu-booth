# site

A static MapLibre globe of the latest StormScope-EU nowcasts. Tiles come straight from the
public Earthmover Flux tiles service (`gtc-berlin`). The page has a branch selector
(`BRANCHES` in `index.html`; the service cannot list them), an init selector, a
true colour / airmass toggle, and playback over lead times. No build step and no server:
`index.html`, `sw.js` and three logo SVGs.

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
`docs/TILE_SERVER_PERFORMANCE.md`). The first frames take a while because the XS service
renders slowly.

## Hosting

Earthmover hosts this page on Vercel, deployed from `main` with root directory `site`,
framework preset "Other" and no build command. `vercel.json` sets an ignored build step,
so Vercel skips deploying commits that change nothing under `site/`. The tiles service sends
`Access-Control-Allow-Origin: *`, so the page works from any domain.
