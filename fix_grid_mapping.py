"""Add a CF geostationary grid mapping to gtc-berlin/stormscope-eu so Flux tiles render north of ~50N.

Metadata-only: no data chunks are rewritten. Per group it
  1. works out from the stored 2-D lat/lon how `x`/`y` are stored: stormscope_v2 has scan angles
     in radians, positive WEST (earth2studio's FCI convention); the root group has metres
     (scan angle * H), already positive east. A west-positive `x` is negated, so re-running is
     a no-op,
  2. adds CF projection-coordinate attrs (with the detected units) to `x`/`y`,
  3. creates the scalar grid mapping variable `fci_geos_projection`,
  4. sets `grid_mapping` on every data variable on the (y, x) grid.
2-D lat/lon are kept; Flux prefers the grid mapping when both exist.
See TILES_BUG.md and docs/WEB_TILES_SERVICE_REQUIREMENTS.md.

    uv run python fix_grid_mapping.py [BRANCH]    # default: main
"""

import sys

import arraylake as al
import numpy as np
import pyproj
import zarr

REPO = "gtc-berlin/stormscope-eu"
GROUPS = ["", "stormscope_v2"]  # "" = root group
BRANCH = sys.argv[1] if len(sys.argv) > 1 else "main"
GRID_MAPPING = "fci_geos_projection"
H = 35786400.0  # MTG-I1 perspective_point_height, m

GM_ATTRS = {
    "long_name": "MTG-I1 FCI fixed grid projection",
    "grid_mapping_name": "geostationary",
    "perspective_point_height": H,
    "semi_major_axis": 6378137.0,
    "semi_minor_axis": 6356752.3142,
    "inverse_flattening": 298.257223563,
    "latitude_of_projection_origin": 0.0,
    "longitude_of_projection_origin": 0.0,
    "sweep_angle_axis": "y",  # MTG/SEVIRI; GOES is "x"
}
CRS = pyproj.CRS.from_cf(GM_ATTRS)
GM_ATTRS["crs_wkt"] = CRS.to_wkt()
TO_LONLAT = pyproj.Transformer.from_crs(CRS, "EPSG:4326", always_xy=True)
UNITS = {"rad": H, "m": 1.0}  # factor from stored x/y to projected metres


def max_error(x_m, y_m, lat, lon):
    """Max |d(lat, lon)| in degrees between the projection of (x_m, y_m) metres and the stored
    2-D coordinates.

    inf if the off-disk (NaN) masks differ in more than a few limb pixels, e.g. when x has
    the wrong sign.
    """
    lon_p, lat_p = TO_LONLAT.transform(*np.meshgrid(x_m, y_m))
    lat_p[~np.isfinite(lat_p)] = np.nan
    if (np.isnan(lat) != np.isnan(lat_p)).mean() > 1e-4:
        return np.inf
    ok = np.isfinite(lat) & np.isfinite(lat_p)
    return max(np.abs(lat_p - lat)[ok].max(), np.abs(lon_p - lon)[ok].max())


repo = al.Client().get_repo(REPO)
session = repo.writable_session(BRANCH)

for path in GROUPS:
    g = zarr.open_group(session.store, path=path, mode="r+")
    x, y, lat, lon = (g[k][:] for k in ("x", "y", "lat", "lon"))

    errors = {
        (units, sign): max_error(sign * x * f, y * f, lat, lon)
        for units, f in UNITS.items()
        for sign in (1, -1)
    }
    (units, sign), err = min(errors.items(), key=lambda kv: kv[1])
    if err > 0.05:
        raise ValueError(f"{path!r}: projection does not match lat/lon: {errors}")
    if sign == -1:
        g["x"][:] = -x  # earth2studio FCI convention is positive west
    action = f"x/y in {units}, {'negated x' if sign == -1 else 'x already positive east'}, max err {err:.1e} deg"

    what = "scan angle" if units == "rad" else "scan angle * perspective_point_height"
    for dim, axis, direction in (("x", "X", "east"), ("y", "Y", "north")):
        g[dim].attrs.update(
            {
                "units": units,
                "axis": axis,
                "standard_name": f"projection_{dim}_coordinate",
                "long_name": f"FCI fixed grid projection {dim}-coordinate ({what}, positive {direction})",
            }
        )

    if GRID_MAPPING not in g.array_keys():
        gm = g.create_array(GRID_MAPPING, shape=(), dtype="int32", fill_value=0, dimension_names=())
        gm[...] = 0
    g[GRID_MAPPING].attrs.update(GM_ATTRS)

    data_vars = [
        k
        for k in g.array_keys()
        if k not in ("lat", "lon")
        and {"y", "x"} <= set(g[k].metadata.dimension_names or ())
    ]
    for k in data_vars:
        g[k].attrs["grid_mapping"] = GRID_MAPPING
    print(f"{path or '<root>'}: {action}; grid_mapping on {sorted(data_vars)}")

if session.has_uncommitted_changes:
    snapshot = session.commit(
        "Add geostationary grid mapping (fci_geos_projection), x positive east, "
        f"to groups {GROUPS} so Flux tiles render north of 50N"
    )
    print(BRANCH, "->", snapshot)
else:
    print(BRANCH, "already fixed, nothing to commit")
