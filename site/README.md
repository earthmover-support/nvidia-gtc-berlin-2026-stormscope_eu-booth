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
`dub1` next to Flux in eu-west-1):

- forwards only the page's two kinds of request (tiles and the `_meta` tree of this repo);
- keeps successful responses in two caches: Vercel's CDN (per edge region and hostname) and
  the Runtime Cache, which every region and hostname share because the function always runs
  in `dub1`. Tiles a day, the `_meta` init list 30 s. Errors such as a `504` are not cached;
- merges concurrent misses that land on the same function instance into one Flux request;
- tags tiles `tiles` and `init-<YYYYMMDDHHMM>`, so a rerun init can be cleared with
  `vercel cache invalidate --tag init-202610151500`. A redeploy empties the CDN cache too.

Locally (`http:`) the page goes straight to Flux.

Simultaneous requests for a new run still spread over several function instances: 10 clients
opening one together made 1,007 Flux requests for 234 tiles (CDN alone: 1,427). So
`scripts/warm_tiles.py` requests each new run's default-view tiles once through the proxy as
soon as Flux lists it, retrying failed tiles. Keep it running while the page gets traffic,
pointed at the exact hostname visitors use:

```bash
uv run --no-project python scripts/warm_tiles.py https://<this site>.vercel.app
```

Measured 2026-10-15 on a test deployment, 10 clients starting 30 s after a new init became
visible: with the warmer 95 of 2,340 requests missed, without it 1,597; a full run took each
client 27 s instead of 66 s.

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

Redeployed 2026-10-16 (temporary-demo note #96).
