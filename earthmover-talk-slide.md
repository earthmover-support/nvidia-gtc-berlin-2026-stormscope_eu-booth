# StormScope-EU nowcasting @ GTC Berlin 2026

**Goal:** demonstrate a real-time nowcasting pipeline, from satellite scan to animated forecast on screen.

## Links

- Dashboard: https://nvidia-gtc-berlin-2026-stormscope-e.vercel.app
- Mobile: https://nvidia-gtc-berlin-2026-stormscopeeu.vercel.app
- GitHub repo: https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth
- Arraylake repo: https://app.earthmover.io/gtc-berlin/stormscope-eu_gtc_berlin_2026_EU_region
- Tiles service: https://app.earthmover.io/gtc-berlin/services/tiles-eu-8af53d67?period=last15Minutes

## Delivery

- Tiles to users — must work on phones, therefore an external tileserver (not deck.gl-raster).
- 13-step animated forecast = bombarding the tile server with 260 requests per run.

## Tech stack

- earth2studio (StormScope-EU inference)
- Arraylake tiles (Flux tiles service)
- HTML & JS frontend hosted on Vercel

## Enablements (Earthmover)

- RGB rendering on the tileserver
- Tile server compute region selector
- Repo + frontend hosting on Vercel + blog post

## Improvements

- `polygons/rgb` style
- uint8 instead of float32 — 2x
- One commit per init instead of per forecast step (earth2studio non-blocking ingest)
- Reprojection fix
- Co-location of input data / inference compute / storage / tiles compute / user request — 2x
- Pending: HTTP/2 — unlimited requests from the client instead of 6 in-flight tiles with HTTP/1.1
- Hard: measuring improvements with cold instances and caching

## Feature recommendation

- "Create pyramids" button: one-click low-resolution pyramid levels
