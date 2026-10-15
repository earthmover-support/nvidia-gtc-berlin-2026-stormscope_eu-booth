# site

A static MapLibre globe of the latest StormScope-EU nowcasts. Tiles come from the public
Earthmover Flux tiles service (`gtc-berlin`), branch `main`; on Vercel through a caching proxy. The page has an init
selector, a true colour / airmass toggle, and playback over lead times. Playback holds the
first frame until all leads have loaded. Every minute it checks for a new init, loads it in
the background, and switches to it once it is complete. No build step and no server:
`index.html`, `sw.js`, three logo SVGs and one edge function.

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

Flux keeps no cache of rendered tiles, so every visitor's browser would make it render the
same ~260 tiles of a run again. On Vercel (any `https:` origin) the page asks for `/flux/...`
instead, which `vercel.json` rewrites to `api/flux.js`. That edge function forwards the
request to Flux and marks successful responses cacheable on Vercel's CDN (tiles a day, the
`_meta` init list 30 s), so each tile is rendered once for everyone. Errors such as a `504`
from a busy service are not cached. Measured 2026-10-15: a cold run took 28 s in the browser;
the same 234 tiles from a second client were all `x-vercel-cache: HIT`, 49 ms p50. Locally
(`http:`) the page goes straight to Flux.

## Run locally

Serve the folder over HTTP from the repo root:

```bash
uv run --no-project python -m http.server 8765 --directory site
```

Then open http://localhost:8765. The tiles service must be up and public (see
[Viz](../README.md#viz) in the top-level README). The first frames take a while because the XS service
renders slowly.

## Hosting

Earthmover hosts this page on Vercel, deployed from `main` with root directory `site`,
framework preset "Other" and no build command. `vercel.json` sets an ignored build step,
so Vercel skips deploying commits that change nothing under `site/`. The tiles service sends
`Access-Control-Allow-Origin: *`, so the page works from any domain. Only `main` deploys to
production; pushes to other branches make preview deployments.

Redeployed 2026-10-15.
