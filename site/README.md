# site

A static MapLibre globe of the latest StormScope-EU nowcasts. Tiles come straight from the
public Earthmover Flux tiles service (`gtc-berlin`). The page has an init selector, a
true colour / airmass toggle, and playback over lead times. No build step and no server:
`index.html` plus three logo SVGs.

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
framework preset "Other" and no build command. The tiles service sends
`Access-Control-Allow-Origin: *`, so the page works from any domain.
