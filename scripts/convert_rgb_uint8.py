"""Rewrite the RGB composites on an Arraylake branch as uint8 (0-255) instead of float 0-1.

Usage: uv run python scripts/convert_rgb_uint8.py [--branch test-uint8]

`true_color` and `airmass` are scaled by 255, rounded and clipped to 1..255; NaN (outside
the disk / not yet computed) becomes 0, stored as CF `_FillValue` (the tiles service
ignores the bare Zarr v3 `fill_value`), so missing pixels stay transparent and xarray
decodes 0 back to NaN. `valid_min`/`valid_max` (the tiles service's byte stretch) become 0/255.
A Zarr array cannot change dtype in place, so each variable is deleted and recreated
with the same chunks, dimensions and attributes, in a single commit.
"""

import argparse

import xarray as xr
import zarr
from arraylake import Client

REPO = "gtc-berlin/stormscope-eu_gtc_berlin_2026"
VARIABLES = ["true_color", "airmass"]


def to_uint8(da: xr.DataArray) -> xr.DataArray:
    out = (da * 255).round().clip(1, 255).fillna(0).astype("uint8")
    out.attrs = {
        **da.attrs,
        "long_name": da.attrs["long_name"].replace(", 0-1", ", 0-255"),
        "valid_min": 0,
        "valid_max": 255,
        # Written as an attr: xarray only encodes it from coords present in the dataset.
        "coordinates": da.encoding["coordinates"],
    }
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--branch", default="test-uint8")
    args = parser.parse_args()

    session = Client().get_repo(REPO).writable_session(args.branch)
    ds = xr.open_zarr(session.store, zarr_format=3)
    converted = {}
    for name in VARIABLES:
        da = ds[name]
        if da.encoding["dtype"] == "uint8":  # decoded dtype is float32 (_FillValue)
            print(f"{name} is already uint8, skipping")
            continue
        # Load before the source array is deleted below (~200 MB per variable as uint8).
        converted[name] = (to_uint8(da).reset_coords(drop=True).load(), da.encoding)

    if not converted:
        return
    group = zarr.open_group(session.store, zarr_format=3)
    for name in converted:
        del group[name]
    for name, (da, enc) in converted.items():
        # Write only the data variable: coordinates (lat/lon/valid_time and the dim
        # coords) are already in the store and stay untouched.
        out = da.to_dataset(name=name).drop_vars(list(da.coords))
        out.to_zarr(
            session.store,
            mode="a",
            zarr_format=3,
            consolidated=False,
            encoding={
                name: {
                    "chunks": enc["chunks"],
                    "compressors": enc["compressors"],
                    "_FillValue": 0,
                }
            },
        )
        print(f"wrote {name} as uint8")
    snapshot = session.commit(f"Store {', '.join(converted)} as uint8 0-255 (fill 0)")
    print(f"committed {snapshot} to {args.branch}")


if __name__ == "__main__":
    main()
