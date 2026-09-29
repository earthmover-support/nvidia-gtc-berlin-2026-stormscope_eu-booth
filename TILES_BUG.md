# Flux tiles stop at ~50°N on the StormScope-EU grid

Found 2026-09-29 while pointing `truecolor_viewer.html` at
`gtc-berlin/stormscope-eu`, group `stormscope_v2`.

## Symptom

Flux tiles (`tiles-4f49272b`, style `raster/rgb`, `variables=true_color`) are
transparent north of a curved line at roughly Nuremberg's latitude: 49.7°N at
13.4°E and 49.85°N at 16.4°E. Berlin, Hamburg, Warsaw and everything further
north are blank on the map.

The data is **not** missing. Read straight from the repo, Berlin (52.51°N,
13.41°E, grid cell y=798, x=1348) has valid values at every lead time:

| field | lead 0 | +1 h | +2 h |
|---|---|---|---|
| `true_color` | (0.565, 0.515, 0.474) | | |
| `fci105ir` | 92.4 | 82.1 | 79.7 |

The full grid spans 29.8–75.6°N and 65.0°W–73.9°E.

## Root cause

The renderer drops **every grid row that contains any NaN in the 2-D
`lat`/`lon` coordinates**, not just the NaN pixels.

- The crop (`Model_FCI_BBox = ((4320, 5440), (1856, 4288))`, 1120×2432) is a
  rectangle cut from a round satellite image. Its top-left and top-right
  corners lie beyond the edge of the Earth as the satellite sees it, so
  `lat`/`lon` are NaN there (6.5% of the grid).
- From **row 722** upward (398 of 1120 rows), every row has NaN corners. The
  middle of those rows is ordinary European land and sea with valid data.
- Row 721, the last row with finite coordinates everywhere, sits at 49.67°N at
  13.4°E and 49.80°N at 16.4°E. That matches where the tiles cut off (49.72°N,
  49.85°N). The row is curved in lat/lon, so the cutoff looks like an arc.

The NaNs come from earth2studio's package `metadata.nc`: `demo.ipynb` (cell 20)
writes `model.lat` / `model.lon` through `add_coord` unchanged. The data arrays
are NaN at the same pixels, which is correct and not the problem.

## Reproduce

```python
# uv run --no-project --python 3.12 --with arraylake --with icechunk --with xarray --with zarr --with numpy python
import arraylake as al, numpy as np, xarray as xr

repo = al.Client().get_repo("gtc-berlin/stormscope-eu")
ds = xr.open_zarr(repo.readonly_session("main").store, group="stormscope_v2", consolidated=False)
lat, lon = ds.lat.values, ds.lon.values

nanrow = ~np.isfinite(lat).all(axis=1)
first = np.argmax(nanrow)                      # -> 722
j = np.nanargmin(np.abs(lon[first - 1] - 13.4))
print(first, lat[first - 1, j])                # -> 722, ~49.67 (the tile cutoff at Berlin's longitude)
```

Then compare against a tile over Berlin. It comes back fully transparent:
`.../tiles/WebMercatorQuad/7/41/68?style=raster/rgb&variables=true_color&colorscalerange=0,1&ensemble=0&lead_time=0 minutes`.

## Fix options

1. **Fill the off-disk coordinates when writing (recommended).** Replace NaN
   `lat`/`lon` with finite values, e.g. the nearest valid value along the row,
   before the `add_coord` calls. The data stays NaN at those pixels, so they
   still render transparent, and the renderer keeps every row. Existing groups
   only need the two coordinate arrays rewritten, not the data.
2. **Add a geostationary `grid_mapping`** (CF `geostationary` on the `y`/`x`
   scan angles) so Flux can use the projection instead of 2-D lat/lon. This is
   the neater fix, but it's untested whether Flux supports it.
3. **Report it to Earthmover.** Dropping a whole row because some of its cells
   have NaN coordinates is a renderer bug, whatever we do on our side.

## Related gotchas found at the same time

- The notebook still writes to `gtc-berlin/stormscope_eu_gtc_berlin_2026` in
  the root group. `stormscope-eu/stormscope_v2` was written elsewhere, so a fix
  to the notebook alone won't change it.
- `stormscope_v2`'s `true_color` is already stretched to 0–1: use
  `colorscalerange=0,1`. The older `0,0.15` saturates it to white.
- The repo root of `stormscope-eu` doesn't open as one dataset (groups not
  aligned), so always pass `group=stormscope_v2`.
- `/_meta/datasets/stormscope-eu/main/stormscope_v2/info` crashes the XS
  (2 GiB) service, which then restarts. Tile rendering itself is fine.
