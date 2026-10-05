# site

A static MapLibre globe of the latest StormScope-EU nowcasts. Tiles come straight from the
public Earthmover Flux tiles service (`gtc-berlin`). The page has an Arraylake branch
selector, an init selector, a true colour / airmass toggle, and playback over lead times.
No build step and no server: `index.html` plus three logo SVGs.

The branch list is `REFS` in `index.html`; `?ref=<branch>` opens any branch, listed or not.
The colour scale is `0,<valid_max>` of the branch's RGB variables, so float 0-1 branches
(`main`) and uint8 0-255 branches (`test-uint8`) both render correctly.

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
