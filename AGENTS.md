# AGENTS.md

## Goals

Booth demo for NVIDIA GTC Berlin 2026: run StormScope-EU nowcasting on MTG-I1 FCI
satellite data, stream each forecast step into Icechunk on Arraylake
(`gtc-berlin/stormscope-eu_gtc_berlin_2026_EU_region`), and show the latest run on a booth screen
through the Earthmover Flux tiles service.

## File tree

```
.
├── scripts/        # GPU pipeline: FCI fetch -> StormScope-EU inference -> Icechunk/Arraylake ingest
├── presentation/   # booth screen: web app (MapLibre globe, explainer panels, live SQL), its local server, and Vercel functions for the hosted copy
├── site/           # static MapLibre globe hosted on Vercel: init/variable selector, lead-time playback
├── docs/           # notes on the Flux tiles service and end-to-end latency
├── pyproject.toml  # uv project; `gpu` extra adds NATTEN for linux+CUDA
└── README.md       # overview, viz/tiles setup, how to run the pipeline
```

## docs/

- `END2END_NEARREALTIME_PERFORMANCE.md`: latency of each stage from satellite scan to the forecast playing in the browser, initial vs current, and the PRs that cut it.
- `VIDEO_FALLBACK.md`: recipe for the 20-min offline video of the booth page (`presentation/record_video.sh`), looped if the booth has no internet.
- `TILE_SERVER_PERFORMANCE.md`: how fast the Flux tiles service renders what the booth globe needs, and how the page works around it.
- `WEB_TILES_SERVICE_REQUIREMENTS.md`: metadata a dataset must carry for the Flux tiles service to render it correctly.
