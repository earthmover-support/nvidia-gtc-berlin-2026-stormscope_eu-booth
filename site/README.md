# site

A static MapLibre globe of the latest StormScope-EU nowcasts. Tiles come straight from the
public Earthmover Flux tiles service (`gtc-berlin`), pinned to `main`'s newest snapshot (see
[Tile caching](#tile-caching)). The page has an init
selector, a true colour / airmass toggle, and playback over lead times. Playback holds the
first frame until all leads have loaded. Every minute it checks for a new init, loads it in
the background, and switches to it once it is complete. No build step and no server of its
own: `index.html`, three logo SVGs, and a `vercel.json` rewrite.

## Run locally

Serve the folder over HTTP from the repo root:

```bash
uv run --no-project python -m http.server 8765 --directory site
```

Then open http://localhost:8765. A plain static server has no `/api/state`, so tiles use
branch `main` (see [Tile caching](#tile-caching)). The tiles service must be up and public (see
[Viz](../README.md#viz) in the top-level README). The first frames take a while because the XS service
renders slowly.

## Hosting

Earthmover hosts this page on Vercel, deployed from `main` with root directory `site`,
framework preset "Other" and no build command. `vercel.json` sets an ignored build step,
so Vercel skips deploying commits that change nothing under `site/`. The tiles service sends
`Access-Control-Allow-Origin: *`, so the page works from any domain.

## Tile caching

Tile URLs for a branch come back with `max-age=0`, so a browser revalidates every tile it has
already seen (a `304` per tile on the service). Snapshot URLs come back with
`max-age=2628000` and are served from the browser cache with no request. The pipeline appends
one commit per init and never rewrites one, so `main`'s newest snapshot holds every init, and
the page pins all tile URLs to it.

The browser can't learn that snapshot id from the tiles service: the `ETag` carries it but
isn't exposed through CORS. So `vercel.json` proxies `/api/state` to the hosted booth
presentation, which lists each run with its snapshot, and the page takes the newest. When
that call fails, or an init is newer than the snapshot, the page falls back to `main`.
Measured in headless Chrome: a reload completes in 4 s with no tile requests, against 6 s
and 117 revalidations with `main`.
