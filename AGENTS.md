# AGENTS.md

## Goals

Booth demo for NVIDIA GTC Berlin 2026: run StormScope-EU nowcasting on MTG-I1 FCI
satellite data, stream each forecast step into Icechunk on Arraylake
(`gtc-berlin/stormscope-eu_gtc_berlin_2026`), and show the latest run on a booth screen
through the Earthmover Flux tiles service.

## File tree

```
.
├── scripts/        # GPU pipeline: FCI fetch -> StormScope-EU inference -> Icechunk/Arraylake ingest
├── presentation/   # booth screen: local web app (MapLibre globe, explainer panels, live SQL) and its server
├── docs/           # notes on the Flux tiles service
├── pyproject.toml  # uv project; `gpu` extra adds NATTEN for linux+CUDA
└── README.md       # overview, viz/tiles setup, how to run the pipeline
```

## docs/

- `TILE_SERVER_PERFORMANCE.md`: how fast the Flux tiles service renders what the booth globe needs, and how the page works around it.
- `WEB_TILES_SERVICE_REQUIREMENTS.md`: metadata a dataset must carry for the Flux tiles service to render it correctly.
