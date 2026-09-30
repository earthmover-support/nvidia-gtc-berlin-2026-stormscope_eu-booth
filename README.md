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

## Pipeline script

[`06_stormscope_eu_pipeline.py`](06_stormscope_eu_pipeline.py) is the notebook as a CLI,
from [negin513/GTC-Berlin-EarthMover](https://github.com/negin513/GTC-Berlin-EarthMover)
(`gtc-demo/05_stormscope_eu_pipeline.py`):

```
MTG observations → Earth2Studio inference → Icechunk/ArrayLake publish → tile dashboard
```

It uses the same freshness search (`MIN_LAG`) and the same one-commit-per-step streaming
as `demo.ipynb`. It differs in two ways. It publishes through plain
`earth2studio.io.IceChunkBackend` with `arraylake.Client.get_icechunk_storage`, so it needs
no backend subclass. And it writes to group `stormscope_v2` of `gtc-berlin/stormscope-eu`,
not to the booth target.

```bash
uv run python 06_stormscope_eu_pipeline.py --n-steps 3                    # local Icechunk in outputs/
uv run python 06_stormscope_eu_pipeline.py --start-time 2026-06-30T12:00 --n-steps 12
uv run python 06_stormscope_eu_pipeline.py --publish-target arraylake --arraylake-repo gtc-berlin/stormscope-eu
uv run python 06_stormscope_eu_pipeline.py --publish-target none          # PNG only
```

| Flag | Default | Notes |
|---|---|---|
| `--start-time` | freshest downloadable slot | ISO 8601 UTC on a 10-min boundary. If the Data Store can't be searched, falls back to 2 h behind now. |
| `--n-steps` | `12` | 10-min steps |
| `--output-dir` | `outputs` | PNG + local Icechunk store |
| `--publish-target` | `$STORMSCOPE_PUBLISH_TARGET` or `local` | `local` \| `arraylake` \| `none` |
| `--arraylake-repo` | `$ARRAYLAKE_REPO` | required with `arraylake`, which also needs `ARRAYLAKE_API_TOKEN` (not `ARRAYLAKE_TOKEN`) |
| `--device` | `auto` | `auto` \| `cuda` \| `cpu` |

Unlike the notebook, it stores only two RGB composites, not the 16 raw channels. Both are
`(ensemble, time, lead_time, y, x, rgb)` float32 in 0–1:

- `true_color`: the VIS 0.6/0.5/0.4 µm recipe, as in the notebook.
- `airmass`: WV 6.3−7.3, IR 9.7−10.5 and IR 10.5 µm brightness temperatures, converted
  from radiance with `earth2studio.data.utils.radiance_to_bt`. Blue uses
  `(bt105 - 209.4) / (209.4 - 244.5)`, which clips to 0 above 209.4 K. So only cloud
  tops colder than that get any blue.

Georeference follows #3, so Flux tiles render north of ~50°N. Both composites name a CF
geostationary grid mapping, `fci_geos_projection` (`sweep_angle_axis="y"`), in their
`grid_mapping` attribute. `x` is stored positive east: earth2studio's FCI `x` is positive
west, so it is negated in the schema and in every write. `x`/`y` get CF
`projection_*_coordinate` attributes, and the 2-D `lat`/`lon` stay as coordinates. `time`, `lead_time` and `ensemble` carry the CF standard names `forecast_reference_time`, `forecast_period` and `realization`. A 2-D `valid_time(time, lead_time)` = `time + lead_time` carries standard name `time`. Chunks
are one `(ensemble, time, lead_time)` by 280 × 304 pixels, zstd-compressed.

Each run overwrites the group. It saves a true-colour PNG of the last step to
`<output-dir>/stormscope_eu_latest.png`. The model checkpoint (`nvidia/stormscope-meteosat`
on Hugging Face) is downloaded on first run.
