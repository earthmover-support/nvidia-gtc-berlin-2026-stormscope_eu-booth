# nvidia-gtc-berlin-2026-stormscope_eu-booth

StormScope-EU nowcasting on MTG-I1 FCI, streamed into Arraylake — the booth demo for
NVIDIA GTC Berlin 2026.

## Links

- https://nvidia.github.io/earth2studio/main/modules/generated/models/px/StormScopeMeteosatEU/
- https://hackmd.io/@ukEyv-C_TnK1RVN9gH211w/BkSIAjzOGl
- https://app.earthmover.io/gtc-berlin/stormscope-eu_gtc_berlin_2026_EU_region

[`scripts/stormscope_eu_pipeline.py`](scripts/stormscope_eu_pipeline.py) runs
[`StormScopeMeteosatEU`](https://nvidia.github.io/earth2studio/main/modules/generated/models/px/StormScopeMeteosatEU/)
end to end: fetch six consecutive FCI frames from the EUMETSAT Data Store, roll the model
forward autoregressively in 10-minute steps, and write each step to Icechunk on Arraylake as
it is produced. By default it initialises from the freshest slot this account may download
(~80–90 min behind real time — downloading anything fresher needs the EUMETSAT NRT licence;
see `MIN_LAG` in the script), so it can be run on a schedule as is. See
[Pipeline script](#pipeline-script).

**Ingest target:**
[gtc-berlin/stormscope-eu_gtc_berlin_2026_EU_region](https://app.earthmover.io/gtc-berlin/stormscope-eu_gtc_berlin_2026_EU_region)

## Deployments

The forecasts reach screens in three ways:

| | Runs as | Live SQL | Audience | Link |
|---|---|---|---|---|
| **Booth presentation** ([`presentation/`](presentation/)) | `uv run presentation/server.py` on the booth machine, full screen on the TV | editable | GTC Berlin visitors | |
| **Hosted presentation** ([`presentation/`](presentation/)) | Vercel project, root directory `presentation` | fixed: the Berlin / Paris / London queries only, cached per init | internal sharing, unlisted public URL | [nvidia-gtc-berlin-2026-stormscope-e.vercel.app](https://nvidia-gtc-berlin-2026-stormscope-e.vercel.app/) ([Vercel project](https://vercel.com/earthmover/nvidia-gtc-berlin-2026-stormscope-eu-booth)) |
| **Mobile site** ([`site/`](site/)) | Vercel project, root directory `site`, static | none | phones, via the booth QR code | [nvidia-gtc-berlin-2026-stormscopeeu.vercel.app](https://nvidia-gtc-berlin-2026-stormscopeeu.vercel.app/) ([Vercel project](https://vercel.com/earthmover/nvidia-gtc-berlin-2026-stormscope_eu-booth-site)) |

Both presentation deployments serve the same `presentation/web/` page and the same logic in
`presentation/booth.py`. The difference is the `sql_editable` flag in `/api/config`: `server.py`
answers `true`, the Vercel functions in `presentation/api/` answer `false`. Each Vercel project
skips commits that change nothing under its own root directory, so a presentation change
does not redeploy the mobile site, and the other way round.

Earthmover owns both Vercel projects and the read-only Arraylake token the hosted
presentation uses. The hosted presentation needs the tiles and SQL services up for as long
as it is shared.

## Viz

[Browse the dataset on Arraylake](https://app.earthmover.io/gtc-berlin/stormscope-eu_gtc_berlin_2026_EU_region)
— needs a login with `gtc-berlin` access.

Tiles are served by the org's Flux service `tiles-4f49272b`, which is **public on purpose
and normally left disabled** between events (billing is per node-hour, and a public endpoint
left idle gets woken by crawlers). Public, because the booth screen and phones fetch tiles
straight from the browser, which can't attach an `Authorization` header; rendered tiles are
reachable by anyone holding the URL, the underlying chunks stay private. Bring it up with:

```
al compute enable gtc-berlin tiles --sku-code s --is-public
al compute update gtc-berlin tiles-4f49272b --min-replicas 8 --max-replicas 8
```

It reaches `available` in about a minute. While the booth is showing it, run it on S
(2× the tile throughput of XS) with 8 replicas pinned, as agreed with Earthmover, so
nothing waits on a cold start or a scale-up. After booth hours set `--min-replicas 0`, and
tear it down after the event with `al compute disable gtc-berlin tiles-4f49272b`. The deployment id
and hostname are deterministic, so they survive the cycle.

Render parameters that matter for `true_color` and `airmass`:

| Parameter | Value | Why |
| --- | --- | --- |
| `style` | `raster/rgb` | Real 3-band composite; pass `variables=true_color` with no `rgb` selector. The only styles the service has are `raster` and `polygons`. |
| `colorscalerange` | `0,255` | Both composites are stored as uint8 0–255 (see [Pipeline script](#pipeline-script)). |
| `lead_time` | `PT10M` | 13 frames: 0, 10, …, 120 minutes. 0 is the observed satellite frame, the rest forecast. ISO 8601 and `"10 minutes"` both work. |
| `ensemble` | `0` | Single member; required because every variable is 5-D. |

Dark regions on the eastern edge are the night side of the terminator — the VIS channels
see nothing there.

## Running it

The model needs a CUDA GPU. Install the GPU extra there (`uv sync --extra gpu`, with
NATTEN's prebuilt wheels from <https://whl.natten.org/>) and run the
[pipeline script](#pipeline-script).

Credentials come from a local `.env` (see [`.env.example`](.env.example)):

- `EUMETSAT_CONSUMER_KEY` / `EUMETSAT_CONSUMER_SECRET` — model input, from
  <https://api.eumetsat.int/api-key/>
- `ARRAYLAKE_API_TOKEN` — ingest target (read by the script)

Each init is one commit, made after its last step, so stopping a run partway publishes
nothing. Rerunning the same init overwrites its slice.

### Docker

On a cloud GPU instance, [`compose.yaml`](compose.yaml) runs the pipeline in a container
built from [`Dockerfile`](Dockerfile): the NGC PhysicsNeMo 26.08 image plus earth2studio.
The container starts the script with no arguments, so it runs
continuously, publishing each new init to Arraylake, and restarts unless stopped. The host
needs the NVIDIA Container Toolkit, and pulling from `nvcr.io` may need an NGC login.

Credentials are passed as Compose secrets, one file each in `secrets/` (git-ignored, and
kept out of the image by [`.dockerignore`](.dockerignore));
[`docker/entrypoint.sh`](docker/entrypoint.sh) exports them as the environment variables
above:

```bash
mkdir -p secrets
printf %s "<key>"    > secrets/eumetsat_consumer_key
printf %s "<secret>" > secrets/eumetsat_consumer_secret
printf %s "<token>"  > secrets/arraylake_api_token
docker compose up -d --build
docker compose logs -f
```

`STORMSCOPE_PUBLISH_TARGET` and `ARRAYLAKE_REPO` can be overridden from the shell or a
`.env` next to `compose.yaml`. If the EUMETSAT account holds the NRT licence (real-time data),
set `STORMSCOPE_REALTIME=1` to run with `--min-lag 0 --wait-seconds 15`; the default `0`
keeps the script's 62-minute lag and 60-second wait:

```bash
STORMSCOPE_REALTIME=1 docker compose up -d --build
```

Arguments after the service name replace the default command, e.g. a single pinned run:

```bash
docker compose run --rm pipeline python scripts/stormscope_eu_pipeline.py --start-time 2026-06-30T12:00
```

The model checkpoint and downloaded FCI products persist in the `cache` volume. Nothing
prunes old FCI products, so it grows by one slot every 10 minutes.

If you need to debug inside the container, you can run an interactive shell inside it instead:

```bash
docker compose run --rm --build pipeline bash
```

## Pipeline script

[`scripts/stormscope_eu_pipeline.py`](scripts/stormscope_eu_pipeline.py) comes from [negin513/GTC-Berlin-EarthMover](https://github.com/negin513/GTC-Berlin-EarthMover)
(`gtc-demo/05_stormscope_eu_pipeline.py`):

```
MTG observations → Earth2Studio inference → Icechunk/ArrayLake publish → tile dashboard
```

It searches for the freshest downloadable slot (`MIN_LAG`) and makes one commit per init;
steps upload in the background while the next one is computed.
It publishes through plain `earth2studio.io.IceChunkBackend` with
`arraylake.Client.get_icechunk_storage`, so it needs no backend subclass, and writes to the
root group of `gtc-berlin/stormscope-eu_gtc_berlin_2026_EU_region` (the default for `--arraylake-repo`). Each run
extends the schema by its init time and appends along `time` in that same commit, keeping earlier inits; every
run must therefore use the same `--n-steps` (lead-time layout).

```bash
uv run python scripts/stormscope_eu_pipeline.py --n-steps 3                    # local Icechunk in outputs/
uv run python scripts/stormscope_eu_pipeline.py --start-time 2026-06-30T12:00 --n-steps 12
uv run python scripts/stormscope_eu_pipeline.py --publish-target arraylake --arraylake-repo gtc-berlin/stormscope-eu_gtc_berlin_2026_EU_region
```

| Flag | Default | Notes |
|---|---|---|
| `--start-time` | freshest downloadable slot | ISO 8601 UTC on a 10-min boundary. If the Data Store can't be searched, falls back to 2 h behind now. |
| `--min-lag` | `62` | minutes a slot must be past its slot time to be considered; `0` with the EUMETSAT NRT licence (see `MIN_LAG` in the script) |
| `--wait-seconds` | `60` | sleep before looking again when there is no new init or a pass failed |
| `--n-steps` | `12` | 10-min steps |
| `--output-dir` | `outputs` | local Icechunk store |
| `--publish-target` | `$STORMSCOPE_PUBLISH_TARGET` or `local` | `local` \| `arraylake` |
| `--arraylake-repo` | `$ARRAYLAKE_REPO` or `gtc-berlin/stormscope-eu_gtc_berlin_2026_EU_region` | required with `arraylake`, which also needs `ARRAYLAKE_API_TOKEN` |
| `--device` | `auto` | `auto` \| `cuda` \| `cpu` |

It stores only two RGB composites, not the 16 raw channels. Both are
`(ensemble, time, lead_time, y, x, rgb)` uint8 in 0–255: each composite is computed in
0–1, then stored as `round(x * 255)` clipped to 1–255. Missing pixels (off the Earth's disk,
or a step not written yet) are 0, the Zarr `fill_value`; there is no CF `_FillValue`, which
makes the tiles service upcast to float32. `valid_min`/`valid_max` are 0/255. Tile requests use
`colorscalerange=0,255`. The pipeline refuses to append to a repo whose composites are still
float.

- `true_color`: the VIS 0.6/0.5/0.4 µm recipe.
- `airmass`: the
  [EUMETSAT airmass recipe](https://eumetrain.org/sites/default/files/2020-05/RGB_recipes.pdf)
  (p. 4), on brightness temperatures converted from radiance with
  `earth2studio.data.utils.radiance_to_bt`:

  ```
  R = ((bt63 - bt73)  - (-23.8)) / (1.4 - (-23.8))
  G = ((bt97 - bt105) - (-39.7)) / (4.1 - (-39.7))
  B = (bt63 - 244.5) / (209.4 - 244.5)   # WV6.3 inverted
  ```

  Each is clipped to 0–1, gamma 1. The ranges are within ~1.5 K of EUMETSAT's (−25…0 K,
  −40…+5 K, 243→208 K).

Georeference follows #3, so Flux tiles render north of ~50°N. Both composites name a CF
geostationary grid mapping, `fci_geos_projection` (`sweep_angle_axis="y"`), in their
`grid_mapping` attribute. `x` is stored positive east: earth2studio's FCI `x` is positive
west, so it is negated in the schema and in every write. `x`/`y` get CF
`projection_*_coordinate` attributes, and the 2-D `lat`/`lon` stay as coordinates. `time`, `lead_time` and `ensemble` carry the CF standard names `forecast_reference_time`, `forecast_period` and `realization`. A 2-D `valid_time(time, lead_time)` = `time + lead_time` carries standard name `time`.
The same CRS is also written with the [GeoZarr](https://geozarr.org/conventions.html) `proj`
and `spatial` conventions (v0.1) on the dataset (root group attributes): `proj:wkt2`,
`proj:projjson`, and an affine `spatial:transform` in metres (scan angle × satellite height),
south-up because `y` runs south to north. Chunks
are one `(ensemble, time, lead_time)` by 560 × 1216 pixels (2 × 2 per frame), all three
`rgb` bands in one chunk, zstd-compressed; `lat`/`lon` share the y/x chunks, stored as
float32 with Blosc zstd + byte shuffle.

Each run appends its init time to the repo's root group. The model checkpoint (`nvidia/stormscope-meteosat`
on Hugging Face) is downloaded on first run.
