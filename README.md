# nvidia-gtc-berlin-2026-stormscope_eu-booth

StormScope-EU nowcasting on MTG-I1 FCI, streamed into Arraylake — the booth demo for
NVIDIA GTC Berlin 2026.

## Links

- https://nvidia.github.io/earth2studio/main/modules/generated/models/px/StormScopeMeteosatEU/
- https://hackmd.io/@ukEyv-C_TnK1RVN9gH211w/BkSIAjzOGl
- https://app.earthmover.io/gtc-berlin/stormscope_eu_gtc_berlin_2026

[`demo.ipynb`](demo.ipynb) runs
[`StormScopeMeteosatEU`](https://nvidia.github.io/earth2studio/main/modules/generated/models/px/StormScopeMeteosatEU/)
end to end: fetch six consecutive FCI frames from the EUMETSAT Data Store, roll the model
forward autoregressively in 10-minute steps, and write each step to Icechunk on Arraylake as
it is produced. By default it initialises from the freshest slot this account may download
(~80–90 min behind real time — downloading anything fresher needs the EUMETSAT NRT licence;
see `MIN_LAG` in the notebook), so the notebook can be run on a schedule as is.

**Ingest target:**
[gtc-berlin/stormscope_eu_gtc_berlin_2026](https://app.earthmover.io/gtc-berlin/stormscope_eu_gtc_berlin_2026)

## Viz

[Browse the dataset on Arraylake](https://app.earthmover.io/gtc-berlin/stormscope_eu_gtc_berlin_2026)
— needs a login with `gtc-berlin` access.

Tiles are served by the org's Flux service `tiles-4f49272b`, which is **private and
normally left disabled** between events (billing is per node-hour, and a public endpoint
left idle gets woken by crawlers). Bring it up with:

```
al compute enable gtc-berlin tiles --sku-code xs
```

It reaches `available` in about a minute and cold-starts the first tile in ~2.7 s
thereafter, so `min_replicas: 0` is fine — no need to pin a replica for the booth.
Tear it down again with `al compute disable gtc-berlin tiles-4f49272b`. The deployment id
and hostname are deterministic, so they survive the cycle.

Because the service is private, a browser cannot fetch tiles directly — it can't attach an
`Authorization` header. Mint a ticketed URL (valid 1 hour) through the Arraylake MCP
server's `get_tile_url`, or drive the service from Python where you can set the header.

Render parameters that matter for `true_color`:

| Parameter | Value | Why |
| --- | --- | --- |
| `style` | `raster/rgb` | Real 3-band composite; pass `variables=true_color` with no `rgb` selector. The only styles the service has are `raster` and `polygons`. |
| `colorscalerange` | `0,0.15` | The band is 0–1 linear reflectance with no gamma. The default `0,1` renders almost black. |
| `lead_time` | `PT10M` | Four frames: 0, 10, 20, 30 minutes. 0 is the observed analysis, the rest forecast. ISO 8601 and `"10 minutes"` both work. |
| `ensemble` | `0` | Single member; required because every variable is 5-D. |

Dark regions on the eastern edge are the night side of the terminator — the VIS channels
see nothing there.

## Running it

for now coiled: to be changed to brev: The model needs a CUDA GPU; the notebook is run on a Coiled GPU VM in the `earthmover-devs`
workspace. The environment definition lives in [`coiled/`](coiled/) and the notebook's *Run
this on Coiled* section has the commands to build it and launch the notebook on a VM.

Credentials come from a local `.env` (see [`.env.example`](.env.example)):

- `EUMETSAT_CONSUMER_KEY` / `EUMETSAT_CONSUMER_SECRET` — model input, from
  <https://api.eumetsat.int/api-key/>
- `ARRAYLAKE_TOKEN` — ingest target, or `al auth login`
