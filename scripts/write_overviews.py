#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "arraylake>=0.14",
#     "icechunk>=1.0",
#     "zarr>=3",
#     "numpy",
#     "python-dotenv",
# ]
# ///
"""Write low-resolution overview levels (default: 2x) next to the native StormScope-EU cube.

Issue #9: with no coarser level to read, the Flux tiles service renders every zoomed-out
tile from the full 1120 x 2432 grid. This backfills block-averaged copies of the stored
composites into `overviews/{factor}x/` and describes the pyramid in a `multiscales`
attribute on the root group (https://github.com/zarr-conventions/multiscales), so the
service can pick a coarser level for low zooms.

The layout copies `earthmover-public/synthetic-multiscales`, which Flux already serves:

    /                   native cube (untouched) + `multiscales` layout
    overviews/2x/       y_2x, x_2x: 560 x 1216

Only 2x by default: Flux picks the level from the zoom alone, whatever the tile size, and
serves a 4x level at z3-4 -- visibly blocky at the booth globe's zoom. With 2x as the
coarsest level, z3-5 read 2x and z6+ the native grid.

Each overview group is a self-contained GeoZarr dataset: its own block-averaged `y`/`x`
scan angles, `lat`/`lon`, the CF grid mapping and the shared `ensemble`/`time`/
`lead_time`/`rgb`/`valid_time` coordinates. The spatial dimensions get a `_{factor}x`
suffix because xarray's DataTree requires a child group to align with its parent on
every dimension name they share; `y` of length 560 under a parent `y` of length 1120
would make the whole tree unopenable.

Every level is averaged from the native grid (NaN-aware, so off-disk pixels do not
blank out a block that still has valid ones). It reads the branch's native data, so run
it after the pipeline has committed the inits it should cover; rerunning overwrites.

Env vars required:
    ARRAYLAKE_API_TOKEN

Usage:
    uv run --script scripts/write_overviews.py --branch test-overviews
    uv run --script scripts/write_overviews.py --branch main --factors 2 4 --dry-run  # add a 4x level
"""

import argparse
import os
import warnings

import arraylake
import numpy as np
import zarr
from dotenv import load_dotenv

ARRAYLAKE_REPO = "gtc-berlin/stormscope-eu_gtc_berlin_2026"
VARIABLES = ["true_color", "airmass"]
FACTORS = [2]
# Copied unchanged into every overview group: coordinates without a spatial dimension and
# the CF grid mapping the data arrays point at.
SHARED = ["ensemble", "time", "lead_time", "rgb", "valid_time", "fci_geos_projection"]
MULTISCALES_CONVENTION = {
    "schema_url": "https://raw.githubusercontent.com/zarr-conventions/multiscales/refs/tags/v1/schema.json",
    "spec_url": "https://github.com/zarr-conventions/multiscales/blob/v1/README.md",
    "uuid": "d35379db-88df-4056-af3a-620245f8e347",
    "name": "multiscales",
    "description": "Multiscale layout of zarr datasets",
}
RESAMPLING = "mean"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--arraylake-repo",
        default=os.environ.get("ARRAYLAKE_REPO", ARRAYLAKE_REPO),
        help="ArrayLake org/repo [env: ARRAYLAKE_REPO]",
    )
    p.add_argument("--branch", required=True, help="branch to read native data from and commit to")
    p.add_argument("--factors", type=int, nargs="+", default=FACTORS)
    p.add_argument("--variables", nargs="+", default=VARIABLES)
    p.add_argument("--dry-run", action="store_true", help="write everything, but do not commit")
    return p.parse_args()


def block_mean(a: np.ndarray, factor: int, axes: tuple[int, int]) -> np.ndarray:
    """NaN-aware mean over `factor` x `factor` blocks of the two `axes` of `a`."""
    shape = list(a.shape)
    for ax in sorted(axes, reverse=True):
        if shape[ax] % factor:
            raise ValueError(f"axis {ax} of length {shape[ax]} is not divisible by {factor}")
        shape[ax : ax + 1] = [shape[ax] // factor, factor]
    block_axes = tuple(ax + i + 1 for i, ax in enumerate(sorted(axes)))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN block -> NaN, as intended
        return np.nanmean(a.reshape(shape), axis=block_axes)


def coarse_dims(dims, factor: int) -> list[str]:
    return [f"{d}_{factor}x" if d in ("y", "x") else d for d in dims]


def copy_array(
    src: zarr.Array, group: zarr.Group, name: str, data, dims, chunks, shape=None
) -> zarr.Array:
    """Create `group[name]` holding `data`, with `src`'s dtype, codecs, fill value and attrs.

    With `data=None` the array is left at its fill value, sized by `shape`.
    """
    dst = group.create_array(
        name,
        shape=np.shape(data) if shape is None else shape,
        chunks=chunks,
        dtype=src.dtype,
        fill_value=src.fill_value,
        compressors=src.compressors,
        filters=src.filters,
        dimension_names=dims,
        attributes=dict(src.attrs),
        overwrite=True,
    )
    if data is not None:
        dst[...] = data
    return dst


def scaled_spatial(attrs: dict, factor: int) -> dict:
    """The root's GeoZarr `spatial:` attributes for a grid `factor` times coarser.

    Pixels grow by `factor` around the same outer corner, so the affine transform keeps
    its origin and the bbox is unchanged.
    """
    a, b, c, d, e, f = attrs["spatial:transform"]
    ny, nx = attrs["spatial:shape"]
    return {
        "spatial:dimensions": coarse_dims(attrs["spatial:dimensions"], factor),
        "spatial:shape": [ny // factor, nx // factor],
        "spatial:transform": [a * factor, b, c, d, e * factor, f],
        "spatial:bbox": attrs["spatial:bbox"],
        "spatial:registration": attrs["spatial:registration"],
    }


def init_level(root: zarr.Group, factor: int, variables: list[str]) -> zarr.Group:
    """Create `overviews/{factor}x` with its coordinates and empty (NaN) data arrays."""
    group = root.require_group(f"overviews/{factor}x")
    group.attrs.update(
        {
            "zarr_conventions": root.attrs["zarr_conventions"],
            **{k: v for k, v in root.attrs.items() if k.startswith("proj:")},
            **scaled_spatial(dict(root.attrs), factor),
        }
    )
    for name in SHARED:
        src = root[name]
        copy_array(src, group, name, src[...], src.metadata.dimension_names, src.chunks)
    for name in ("y", "x"):
        src = root[name]
        values = src[:].reshape(-1, factor).mean(axis=1)  # pixel centres of each block
        copy_array(src, group, f"{name}_{factor}x", values, [f"{name}_{factor}x"], values.shape)
    for name in ("lat", "lon"):
        src = root[name]
        values = block_mean(src[:], factor, (0, 1))
        copy_array(
            src,
            group,
            name,
            values,
            coarse_dims(src.metadata.dimension_names, factor),
            values.shape,
        )
    for name in variables:
        src = root[name]
        dims = src.metadata.dimension_names
        shape = tuple(
            n // factor if d in ("y", "x") else n for d, n in zip(dims, src.shape, strict=True)
        )
        # one chunk per frame: 560 x 1216 x 3 float32 is 8 MB at 2x, the native chunk size
        chunks = tuple(n if d in ("y", "x", "rgb") else 1 for d, n in zip(dims, shape, strict=True))
        copy_array(src, group, name, None, coarse_dims(dims, factor), chunks, shape)
    return group


def multiscales_attrs(root: zarr.Group, factors: list[int]) -> dict:
    """Root attributes describing the native grid plus each overview level."""
    layout = [
        {
            "asset": "/",
            "transform": {"scale": [1.0, 1.0], "translation": [0.0, 0.0]},
            "spatial:transform": root.attrs["spatial:transform"],
            "spatial:shape": root.attrs["spatial:shape"],
        }
    ]
    for f in factors:
        spatial = scaled_spatial(dict(root.attrs), f)
        layout.append(
            {
                "asset": f"/overviews/{f}x",
                "derived_from": "/",
                # scale/translation in pixels of the source level, over (y, x)
                "transform": {"scale": [float(f)] * 2, "translation": [(f - 1) / 2] * 2},
                "spatial:transform": spatial["spatial:transform"],
                "spatial:shape": spatial["spatial:shape"],
                "resampling_method": RESAMPLING,
            }
        )
    conventions = [c for c in root.attrs["zarr_conventions"] if c["name"] != "multiscales"]
    return {
        "zarr_conventions": [*conventions, MULTISCALES_CONVENTION],
        "multiscales": {"layout": layout, "resampling_method": RESAMPLING},
    }


def main():
    load_dotenv()
    args = parse_args()
    client = arraylake.Client(token=os.environ["ARRAYLAKE_API_TOKEN"])
    repo = client.get_repo(args.arraylake_repo)
    session = repo.writable_session(args.branch)
    root = zarr.open_group(session.store, mode="a")

    levels = {f: init_level(root, f, args.variables) for f in args.factors}
    for name in args.variables:
        src = root[name]
        dims = src.metadata.dimension_names
        frame_dims = tuple(d for d in dims if d not in ("y", "x", "rgb"))
        yx = (dims.index("y") - len(frame_dims), dims.index("x") - len(frame_dims))
        for idx in np.ndindex(*src.shape[: len(frame_dims)]):
            frame = src[idx]
            if np.isnan(frame).all():
                continue  # slot the pipeline never filled; the overview stays at fill value
            for f, group in levels.items():
                group[name][idx] = block_mean(frame, f, yx)
            print(f"{name} {dict(zip(frame_dims, idx, strict=True))}")

    root.attrs.update(multiscales_attrs(root, args.factors))
    if args.dry_run:
        print(f"dry run: {session.status()}")
        return
    factors = ", ".join(f"{f}x" for f in args.factors)
    snapshot = session.commit(f"Overviews {factors} ({RESAMPLING}) of {', '.join(args.variables)}")
    print(f"committed {snapshot} to {args.arraylake_repo}@{args.branch}")


if __name__ == "__main__":
    main()
