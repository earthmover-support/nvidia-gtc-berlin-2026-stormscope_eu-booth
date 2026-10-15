# site

A static MapLibre globe of the latest StormScope-EU nowcasts. Tiles come from the public
Earthmover Flux tiles service (`gtc-berlin`), branch `main`; on Vercel through a caching proxy. The page has an init
selector, a true colour / airmass toggle, and playback over lead times. Playback holds the
first frame until all leads have loaded. Every minute it checks for a new init, loads it in
the background, and switches to it once it is complete. No build step and no server:
`index.html`, `sw.js`, three logo SVGs and one Vercel function.

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
instead, which `vercel.json` rewrites to `api/flux.js`. That function (Node.js, pinned to
`dub1` next to Flux in eu-west-1) forwards the request and marks successful responses
cacheable on Vercel's CDN (tiles a day, the `_meta` init list 30 s), so a tile is rendered
once and then served to everyone. Errors such as a `504` from a busy service are not cached.
Locally (`http:`) the page goes straight to Flux.

Vercel does not merge simultaneous requests for an uncached tile: 10 clients opening a new
run together still made Flux render most tiles several times (1,427 misses for 234 tiles).
So `scripts/warm_tiles.py` requests each new run's default-view tiles once through the proxy
as soon as it is listed; keep it running while the page gets traffic:

```bash
uv run --no-project python scripts/warm_tiles.py https://<this site>.vercel.app
```

Measured 2026-10-15 on a test deployment: a new run takes the warmer ~35 s (598 tiles, both
variables, z2-z4). A browser opening it afterwards got 117/117 tiles as `x-vercel-cache: HIT`
and the animation started 6.8 s after page load, against 28 s for a cold run.

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
