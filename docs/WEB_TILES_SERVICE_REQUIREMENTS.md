# Metadata the Earthmover Flux tiles service needs

What a dataset has to carry for the [Flux tiles service](https://docs.earthmover.io/compute/tiles)
to render it correctly. It combines the Earthmover docs with what we measured on
`gtc-berlin/stormscope-eu` (service `tiles-4f49272b`) on 2026-09-29.

## TL;DR for StormScope-EU (MTG FCI, geostationary)

1. Add a **grid mapping variable** with the CF `geostationary` projection, and point every
   data variable at it with `grid_mapping`. This is required. Without it the service
   georeferences from the 2-D `lat`/`lon`, and on this grid that loses everything north of
   ~50°N.
2. Give `x`/`y` `standard_name` `projection_x_coordinate`/`projection_y_coordinate` and
   `units: rad` (scan angle), with **`x` positive east**.
3. Keeping the 2-D `lat`/`lon` alongside the grid mapping is fine. The grid mapping wins.

## What the Earthmover docs require

From https://docs.earthmover.io/compute/tiles:

- **CF conventions**, especially for coordinates. The service finds them by attributes, not names:
  - geographic: `standard_name` `latitude`/`longitude`, or `units` `degrees_north`/`degrees_east`;
  - projected: `standard_name` `projection_x_coordinate`/`projection_y_coordinate`.
- **CRS lookup order**:
  1. the variable named by a data variable's `grid_mapping` attribute (recommended);
  2. otherwise a variable called `spatial_ref` or `crs`;
  3. otherwise the service assumes EPSG:4326.

  Earthmover "*strongly* recommend[s]" an explicit grid mapping variable plus `standard_name`
  and `units` on the coordinate variables.
- **`valid_min` / `valid_max`** on a data variable make `colorscalerange` optional. Without
  them, every tile request must pass `colorscalerange`.
- **`units`** is shown in the legend.
- **Extra dimensions** (`time`, `level`, `band`, ...) default to their first value unless the
  request selects one.
- GeoZarr **multiscales** (pyramids) are supported.
- Raster styles take one variable (`raster/<matplotlib colormap>`). Formats are `png` (default)
  and `jpeg`. Tile width and height must be multiples of 256.

## Measured: does it still work with 2-D lat/lon present?

Branch `tiles_drop_rows` holds three copies of `true_color` (2 lead times), which differ only
in their georeference. The two tiles tested are Berlin (`WebMercatorQuad/7/41/68`) and Europe
(`3/2/4`). Settings: `colorscalerange=0,1`, `ensemble=0`, `lead_time=0 minutes`.

| group | georeference | style | Berlin tile opaque | Europe tile opaque |
|---|---|---|---|---|
| `stormscope_v2` | grid mapping only | `raster/rgb` | 100% | 99.8% |
| `stormscope_v2_gm_latlon` | grid mapping **and** 2-D lat/lon | `raster/rgb` | 100% | 99.8% |
| `stormscope_v2_latlon_only` | 2-D lat/lon only | `raster/rgb` | 0% | 30% |
| `stormscope_v2_latlon_only` | 2-D lat/lon only | `polygons/rgb` | 0% | 30% |

- **The grid mapping takes precedence over 2-D lat/lon.** With both present, tiles and the
  reported bounds are identical to the grid-mapping-only group. Bounds are `[-45.9, 29.8, 67.3, 74.0]`,
  from the projection; the lat/lon-only group reports `[-63.8, 29.8, 72.3, 75.8]` from lat/lon.
- **2-D lat/lon with NaNs is broken in both styles.** `polygons` does not rescue it here.
  Everything north of ~50°N is dropped: every grid row containing any NaN coordinate is
  discarded.
- The remaining 0.2% transparent in the Europe tile is off the Earth's disk. That's correct.

## Geostationary grid mapping for MTG-I1 FCI

Verified against earth2studio's stored lat/lon: max error 4e-6°, identical off-disk NaN mask.

```python
H = 35786400.0  # m
fci_geos_projection = xr.DataArray(np.int32(0), attrs={
    "grid_mapping_name": "geostationary",
    "perspective_point_height": H,
    "semi_major_axis": 6378137.0,
    "semi_minor_axis": 6356752.3142,
    "inverse_flattening": 298.257223563,
    "latitude_of_projection_origin": 0.0,
    "longitude_of_projection_origin": 0.0,
    "sweep_angle_axis": "y",            # GOES is "x"; MTG/SEVIRI is "y"
    "crs_wkt": pyproj.CRS.from_cf(...).to_wkt(),  # optional, helps rioxarray/GDAL
})
x = -earth2studio_x                     # earth2studio FCI x is positive WEST; CF wants east
x.attrs = {"units": "rad", "axis": "X", "standard_name": "projection_x_coordinate"}
y.attrs = {"units": "rad", "axis": "Y", "standard_name": "projection_y_coordinate"}
data_var.attrs["grid_mapping"] = "fci_geos_projection"
```

Two pitfalls, each silently wrong rather than an error:

- `sweep_angle_axis = "x"` shifts positions by up to 0.35°.
- Leaving `x` un-negated mirrors the image east-west.

Reference layout: `earthmover-public/goes-16`, group `ABI-L2-MCMIPF/post-2023-04-19`, variable
`goes_imager_projection`. Coordinates are in radians and there are no 2-D lat/lon.

## Other request-side requirements (measured)

- `colorscalerange` must be a single `"min,max"` string. Two repeated parameters give a 422.
- 5-D variables need explicit `ensemble=` and `lead_time=`. Pass `lead_time` as an
  xarray-style string (`0 minutes`) or ISO 8601 (`PT10M`), not a number.
- `raster/rgb` renders a 3-band composite from a dimension of length 3 (`rgb` =
  `red`/`green`/`blue`). Pass `variables=true_color` with no band selector.
  `true_color` and `airmass` are stored stretched to 0-1, so use `colorscalerange=0,1`.
- `raster/rgb` maps a size-3 dim named `band` or `rgb` to R, G, B **by position**. Every
  rgb tile logs "Treating dimension as RGB bands in positional R, G, B order; no attributes
  confirm this." The warning is unconditional: xpublish-tiles (`pipeline.py`, `apply_query`,
  checked at 7372993, 2026-09-16) reads no attribute for band order, so no metadata of ours
  silences it (#10). The only attributes the RGB path reads are `valid_min`/`valid_max` on the
  data variable. They set the byte stretch, and without them float data falls back to 0..1.
  We write the order anyway, as `color_interpretation = "red green blue"` on the `rgb`
  coordinate, so a reader (or a later service version) does not have to guess.
- The service offers two styles, `raster` and `polygons`. A bogus style returns a 422 listing
  the accepted values.

## Where this is applied

- **New runs:** `06_stormscope_eu_pipeline.py` writes all of the above at ingest. It creates
  `fci_geos_projection` (`add_grid_mapping`), `grid_mapping` goes on every data array through
  `GEOREF`, and `east_positive` negates `x` in the schema and in every `write`. The negation has
  to go in both places, because `write` finds its slice by looking up coordinate values.
- **RGB band metadata (#10):** `06_stormscope_eu_pipeline.py` writes it at schema creation.
  The existing repo got the same attributes in a metadata-only commit, `0ER90FTHKN0KF6TNCS60`
  on `main` (2026-10-01), applied by the one-off script in the comments of #12.
- **Existing data:** the one-off script in
  [#3 (comment)](https://github.com/earthmover-support/nvidia-gtc-berlin-2026-stormscope_eu-booth/pull/3#issuecomment-5893671054)
  added the same metadata to the root group and `stormscope_v2` of the earlier repo
  `gtc-berlin/stormscope-eu` in place, without rewriting any chunks. Applied to `main` on
  2026-09-29.
- Checked 2026-09-29 without a GPU, with the writer of the since-removed `demo.ipynb`
  (the same georeference code as the pipeline). It was fed two lead times of
  `true_color` from `main` and wrote them to Arraylake branch `writer_gm_check`. The stored
  `x` equals `main`'s, the projection matches lat/lon to 4e-6° with an identical off-disk mask,
  and the tiles are byte-identical to `main`'s (`3/2/4`, `7/41/68`).

## Spatial chunking (measured)

The pipeline writes 560 × 1216 spatial chunks with zstd (`CHUNKS` in
`06_stormscope_eu_pipeline.py`): `true_color` chunks are `(1, 1, 1, 560, 1216, 3)`, 4 per
frame. Before #4, a frame was one ~33 MB chunk. The measurement below used 280 × 304 chunks
(32 per frame) and found that **for tiles, chunking changed nothing measurable.** Same
tiles, branch `writer_gm_check` (chunked, zstd) vs.
`main` (one chunk per frame, uncompressed), 512 px, XS service, 2026-09-29:

| tile | `main` | chunked |
|---|---|---|
| `3/2/4` (Europe) | 8.3 s | 7.9-9.0 s |
| `3/2/3` | 1.2 s | 1.3-1.6 s |
| `4/5/9` | 3.9 s | 3.9 s |
| `4/5/8` | 1.3 s | 1.2 s |
| `5/10/17`, `5/11/16`, `5/9/16` | 0.5-0.8 s | 0.6-0.9 s |
| `7/41/68` (Berlin) | 0.3-0.4 s | 0.4-0.5 s |

The chunked branch had never been read before, so reading chunks is not what costs the
time. The render cost follows the tile's footprint on the source grid, which points to
reprojection on the service's single CPU. For faster tiles, try a bigger SKU or caching
(#5), not a different chunking. The chunking still stays: it costs nothing here, and the
SQL panel (#4) and any partial reads may benefit (not measured).

## Writing checklist

- [ ] Grid mapping variable present, CF attributes as above.
- [ ] `grid_mapping` attribute on **every** data variable.
- [ ] Check it in the **raw zarr attributes** after writing, e.g.
      `zarr.open_group(store, path=group)["true_color"].attrs`. `xr.open_zarr` moves
      `grid_mapping` into `.encoding`, so it's missing from `.attrs` even when stored. And when
      we wrote a dataset carrying both `coordinates: "lat lon"` and `grid_mapping` via
      `to_icechunk`, the `grid_mapping` attribute was silently not written. We had to set it
      afterwards through zarr.
- [ ] `x`/`y` with `standard_name` and `units`. `x` positive east.
- [ ] Optional: `valid_min`/`valid_max` on each data variable so tile requests can omit
      `colorscalerange`. For `raster/rgb` composites they are the byte stretch, so write them:
      `0`/`1` on `true_color` and `airmass`.
- [ ] RGB band dim named `rgb` (or `band`), size 3, values in R, G, B order, with
      `color_interpretation = "red green blue"` on the coordinate. The service still assumes
      position (see above), so never reorder the bands.
- [ ] `units` on data variables (used in legends).
- [ ] If 2-D `lat`/`lon` are kept: `standard_name` and `units` on them. They're harmless next to
      a grid mapping, but they must not be the only georeference while they contain NaNs.
