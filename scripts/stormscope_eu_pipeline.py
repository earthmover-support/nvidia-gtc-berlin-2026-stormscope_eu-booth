"""StormScope EU nowcasting pipeline for the EarthMover demo.

MTG/FCI observations (EUMETSAT) -> Earth2Studio inference (StormScopeMeteosatEU)
-> streaming Icechunk/ArrayLake publication -> tile-ready true-colour product.

It finds the freshest downloadable initial condition with
`resolve_start_time()`'s EUMETSAT-Data-Store search, stores two composites,
`true_color` and `airmass`, and writes one Icechunk commit per init. Each
10-minute rollout step is handed to the backend's background writers as it is
produced, so uploads overlap inference, and the commit at the end of the
rollout publishes the whole init at once. It writes to the root group of
`gtc-berlin/stormscope-eu_gtc_berlin_2026_EU_region` (see ARRAYLAKE_REPO below). Runs
accumulate: each one appends its init to `time` in that same commit, so
readers never see a partly written init.

Without --start-time the script runs continuously: the model is loaded once
(`StormScopeEUPipeline`), and `main()` polls the Data Store, starting a new run
whenever a fresher initial condition becomes downloadable -- one every 10 minutes
once the feed is caught up. With --start-time it runs that one init and exits.

Publishing goes through Earth2Studio's `earth2studio.io.IceChunkBackend`
directly, with no Arraylake-specific subclass:
`arraylake.Client.get_icechunk_storage(name)` returns a plain
`icechunk.Storage` object, which is exactly what `IceChunkBackend`'s
`storage` parameter already accepts -- it calls
`icechunk.Repository.open_or_create(storage, ...)` internally either way, so
the hosted-vs-local choice is just which `storage` object gets passed in.
The one subclass here, `ReopenableIceChunkBackend`, only works around base
`IceChunkBackend` failing to reopen a repo that holds a scalar array (the CF
grid mapping), which appending to an existing cube requires. `add_coord()`
below covers the one thing base `IceChunkBackend` lacks -- writing a
non-dimension 2-D coordinate (`lat`/`lon`) without it being taken for a new
index dimension -- as a plain function against `backend.root` (a zarr Group).

`add_array()` forwards unrecognized kwargs straight to
`zarr.Group.create_array()`, which has no `attrs` parameter -- attrs must be
set via `.attrs.update()` on the array after creation, as everywhere below.

Env vars required:
    EUMETSAT_CONSUMER_KEY, EUMETSAT_CONSUMER_SECRET
        (or credentials cached in ~/.eumdac/credentials)
    ARRAYLAKE_API_TOKEN
        only needed with --publish-target arraylake

Install:
    uv sync  # add --extra gpu on a linux+CUDA host

Usage:
    python scripts/stormscope_eu_pipeline.py --n-steps 3  # runs until interrupted
    python scripts/stormscope_eu_pipeline.py --start-time 2026-06-30T12:00 --n-steps 12  # one run
    python scripts/stormscope_eu_pipeline.py --publish-target arraylake --arraylake-repo gtc-berlin/stormscope-eu_gtc_berlin_2026_EU_region
    python scripts/stormscope_eu_pipeline.py --help
"""

import argparse
import os
import time
from collections import OrderedDict
from datetime import UTC, datetime, timedelta

import earth2studio
import eumdac
import icechunk
import numpy as np
import pyproj
import torch
from dotenv import load_dotenv
from earth2studio.data import fetch_data
from earth2studio.data.meteosat_fci import (
    PERSPECTIVE_POINT_HEIGHT,
    SEMI_MAJOR_AXIS,
    SEMI_MINOR_AXIS,
    MeteosatFCI,
)
from earth2studio.data.utils import radiance_to_bt
from earth2studio.data.utils_eumetsat import eumetsat_credentials
from earth2studio.io import IceChunkBackend
from earth2studio.models.px.stormscope_meteosat import StormScopeMeteosatEU
from earth2studio.utils.coords import map_coords
from earth2studio.utils.type import CoordSystem
from tqdm import tqdm
from zarr.codecs import BloscCodec, ZstdCodec

load_dotenv()

# ============================================================================
# CONFIG (defaults; override with CLI flags, see parse_args)
# ============================================================================
RGB_CHANNELS = ["fci06vis", "fci05vis", "fci04vis"]  # VIS 0.6 / 0.5 / 0.4 um
RGB_VALUES = np.array(["red", "green", "blue"])
# Band metadata for both composites. The tiles service takes the `rgb` dim as R, G, B by
# position and reads only `valid_min`/`valid_max` (the byte stretch), so the order here
# is a record for readers, not something the service checks yet (see
# docs/WEB_TILES_SERVICE_REQUIREMENTS.md).
RGB_BAND_ATTRS = {"long_name": "colour band", "color_interpretation": " ".join(RGB_VALUES)}
# Both composites are stored as bytes: 1..255 for data, 0 for missing (off the Earth's disk,
# or a step not written yet), as the Zarr v3 `fill_value`. No CF `_FillValue` attribute: the
# tiles service decodes it by upcasting the bytes to float32, which costs a 4x larger array
# per tile (see extend_schema, which strips it from older repos).
RGB_DTYPE = "uint8"
RGB_FILL = 0
RGB_ENCODING = {"valid_min": 0, "valid_max": 255}
# FCI central wavelengths; `radiance_to_bt` takes their reciprocal, the wavenumber in cm-1.
WAVELENGTH_CM = {
    "fci04vis": 0.444e-4,
    "fci05vis": 0.510e-4,
    "fci06vis": 0.640e-4,
    "fci08vis": 0.865e-4,
    "fci09vis": 0.914e-4,
    "fci13nir": 1.380e-4,
    "fci16nir": 1.610e-4,
    "fci22nir": 2.250e-4,
    "fci38ir": 3.800e-4,
    "fci63wv": 6.300e-4,
    "fci73wv": 7.350e-4,
    "fci87ir": 8.700e-4,
    "fci97ir": 9.660e-4,
    "fci105ir": 10.500e-4,
    "fci123ir": 12.300e-4,
    "fci133ir": 13.300e-4,
}
AIRMASS_CHANNELS = ["fci63wv", "fci73wv", "fci97ir", "fci105ir"]

# 560 x 1216 spatial chunks: 2 x 2 = 4 per frame of the default Model_FCI_BBox
# (y=1120, x=2432), ~2 MB (uint8) per composite chunk instead of the whole frame (~8 MB). `rgb`
# is absent, and the backend leaves a dimension it has no chunk for unsplit, so a chunk
# holds all three bands. `lat`/`lon` use the same y/x chunks (see add_coord).
CHUNKS = {"ensemble": 1, "time": 1, "lead_time": 1, "y": 560, "x": 1216}
# `time` and `valid_time` hold a few bytes per init. With the data arrays' `time` chunk
# of 1, every init added a chunk to each, and every reader opening a snapshot (the tile
# service on each commit) read all of them. 4096 inits, ~4 weeks at one per 10 min, fit
# one chunk; `extend_schema`'s resize then rewrites that one chunk per commit.
COORD_TIME_CHUNK = 4096
COMPRESSOR = ZstdCodec(level=3)
# lat/lon only: Blosc byte-shuffle groups each value's bytes (exponents together,
# mantissas together) before zstd, which compresses smooth float fields much better.
LATLON_COMPRESSOR = BloscCodec(cname="zstd", clevel=3, shuffle="shuffle")

# MTG-I1 FCI fixed grid as a CF geostationary projection. `x`/`y` are scan angles in
# radians; `pyproj.CRS.from_cf` scales them by `perspective_point_height`. Checked against
# `model.lat`/`model.lon`: max error 4e-6 deg, identical off-disk mask.
GRID_MAPPING = "fci_geos_projection"
FCI_GEOS = {
    "long_name": "MTG-I1 FCI fixed grid projection",
    "grid_mapping_name": "geostationary",
    "perspective_point_height": PERSPECTIVE_POINT_HEIGHT,
    "semi_major_axis": SEMI_MAJOR_AXIS,
    "semi_minor_axis": SEMI_MINOR_AXIS,
    "inverse_flattening": 298.257223563,
    "latitude_of_projection_origin": 0.0,
    "longitude_of_projection_origin": 0.0,
    # MTG and SEVIRI sweep about "y", GOES about "x". The wrong axis shifts positions
    # by up to 0.35 deg, with no error raised.
    "sweep_angle_axis": "y",
}
FCI_CRS = pyproj.CRS.from_cf(FCI_GEOS)
FCI_GEOS["crs_wkt"] = FCI_CRS.to_wkt()  # for GDAL/rioxarray

# GeoZarr `proj` and `spatial` conventions (https://geozarr.org/conventions.html), the
# same CRS for readers that do not follow CF grid mappings. See geozarr_attrs.
ZARR_CONVENTIONS = [
    {
        "schema_url": "https://raw.githubusercontent.com/zarr-conventions/proj/refs/tags/v0.1/schema.json",
        "spec_url": "https://github.com/zarr-conventions/proj/blob/v0.1/README.md",
        "uuid": "f17cb550-5864-4468-aeb7-f3180cfb622f",
        "name": "proj",
        "description": "Coordinate reference system information for geospatial data",
    },
    {
        "schema_url": "https://raw.githubusercontent.com/zarr-conventions/spatial/refs/tags/v0.1/schema.json",
        "spec_url": "https://github.com/zarr-conventions/spatial/blob/v0.1/README.md",
        "uuid": "689b58e2-cf7b-45e0-9fff-9cfc0883d6b4",
        "name": "spatial",
        "description": "Spatial coordinate information",
    },
]

# `grid_mapping` is what the Flux tiles service projects with. The 2-D lat/lon alone are
# not enough: the domain's top corners are off the Earth's disk, so their lat/lon are NaN,
# and Flux drops every grid row containing one -- everything north of ~50N. `coordinates`
# points at the non-dimension coordinates written by `add_coord`; without it
# `xr.open_zarr` would read them back as data variables instead of coordinates of every
# field.
GEOREF = {"coordinates": "lat lon valid_time", "grid_mapping": GRID_MAPPING}
PUBLISH_TARGETS = ["local", "arraylake"]
# Default --publish-target arraylake destination; the cube goes in its root group.
ARRAYLAKE_REPO = "gtc-berlin/stormscope-eu_gtc_berlin_2026_EU_region"

# EUMETSAT Data Store freshness search (see resolve_start_time).
SCAN_STEP = timedelta(seconds=MeteosatFCI.SCAN_FREQUENCY)  # FCI repeat cycle: 10 min
FCI_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)  # anchor of the scan-slot grid
SEARCH_LOOKBACK = timedelta(hours=6)  # how far back to hunt for a complete input window
REALTIME_LAG = timedelta(hours=2)  # blind fallback when the Data Store cannot be searched
# Youngest slot this account is allowed to *download*. The Data Store lists a product ~5
# minutes after its scan ends (~14 min after the slot time), but downloading one less than
# an hour past its *slot time* requires the EUMETSAT NRT licence on the account -- without
# it the search still returns the slot and `fetch_data` then fails with a 403
# "NRTLicense required to access this collection". Only the hourly :00 slots are free at
# once, and the 6-frame input window always needs five sub-hourly ones. Measured with
# scripts/measure_fci_latency.py on 2026-10-06, slots unlocked 60.1-60.4 min after their
# slot time; 62 min is that boundary plus a margin. Holding the NRT licence lets this go to
# 0, moving the run to ~15 minutes behind real time. This is the default: override it
# with --min-lag (minutes).
MIN_LAG = timedelta(minutes=62)


def parse_args() -> argparse.Namespace:
    """Parse and validate the CLI flags.

    `--start-time` comes back as a `np.datetime64` (or None), already checked against
    `MeteosatFCI.available`. `--publish-target` falls back to `STORMSCOPE_PUBLISH_TARGET`,
    then "local"; the Arraylake repo and token are checked here so a misconfigured run
    fails before the model is loaded.
    """
    p = argparse.ArgumentParser(
        description="StormScope EU nowcast: fetch MTG/FCI -> Earth2Studio rollout "
        "-> streaming Icechunk/ArrayLake publish",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--start-time",
        default=None,
        help="initial-condition time, ISO 8601 UTC (e.g. 2026-06-30T12:00). "
        "Default: the freshest slot the EUMETSAT Data Store can serve "
        "this account (see --min-lag), falling back to REALTIME_LAG "
        "behind now if the Data Store can't be searched.",
    )
    p.add_argument(
        "--min-lag",
        type=float,
        default=MIN_LAG.total_seconds() / 60,
        help="minutes a slot must be past its slot time before it is considered for download; "
        "0 with the EUMETSAT NRT licence",
    )
    p.add_argument(
        "--wait-seconds",
        type=float,
        default=60,
        help="seconds to sleep before looking again when no new init is available or a pass failed",
    )
    p.add_argument("--n-steps", type=int, default=12, help="forecast steps of 10 min (12 = 2 h)")
    p.add_argument("--output-dir", default="outputs", help="where the local Icechunk store goes")
    p.add_argument(
        "--publish-target",
        choices=PUBLISH_TARGETS,
        default=None,
        help="local = Icechunk repo on disk, arraylake = hosted repo (needs ARRAYLAKE_API_TOKEN) "
        "[env: STORMSCOPE_PUBLISH_TARGET, default local]",
    )
    p.add_argument(
        "--arraylake-repo",
        default=os.environ.get("ARRAYLAKE_REPO", ARRAYLAKE_REPO),
        help="ArrayLake org/repo when --publish-target arraylake [env: ARRAYLAKE_REPO]",
    )
    p.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    args = p.parse_args()

    if args.n_steps < 1:
        p.error("--n-steps must be >= 1")
    if args.min_lag < 0:
        p.error("--min-lag must be >= 0")
    args.min_lag = timedelta(minutes=args.min_lag)
    if args.wait_seconds < 0:
        p.error("--wait-seconds must be >= 0")
    if args.start_time is not None:
        try:
            args.start_time = np.datetime64(args.start_time)
        except ValueError:
            p.error(f"--start-time {args.start_time!r} is not an ISO 8601 timestamp")
        if np.isnat(args.start_time):
            p.error("--start-time must be a real timestamp")
        if not MeteosatFCI.available(args.start_time):
            p.error(
                f"--start-time {args.start_time} has no MTG FCI data (must be on a 10-min boundary "
                "within the operational period)"
            )
    if args.publish_target is None:
        # argparse does not check `choices` against an env-derived default
        args.publish_target = os.environ.get("STORMSCOPE_PUBLISH_TARGET", "local")
        if args.publish_target not in PUBLISH_TARGETS:
            p.error(
                f"STORMSCOPE_PUBLISH_TARGET={args.publish_target!r} must be one of {PUBLISH_TARGETS}"
            )
    if args.publish_target == "arraylake":
        if not args.arraylake_repo:
            p.error("--publish-target arraylake needs --arraylake-repo or ARRAYLAKE_REPO")
        if not os.environ.get("ARRAYLAKE_API_TOKEN"):
            p.error("--publish-target arraylake needs ARRAYLAKE_API_TOKEN in the environment")
    return args


def load_model(device: torch.device) -> StormScopeMeteosatEU:
    """Step 1: load the pretrained StormScopeMeteosatEU checkpoint."""
    package = StormScopeMeteosatEU.load_default_package()
    model = StormScopeMeteosatEU.load_model(package=package)
    model = model.to(device)
    model.eval()
    model.compile_model()
    return model


def build_data_sources() -> dict:
    """Step 2: FCI data sources at 1km/2km matching the model's pixel bbox."""
    bbox_2km = StormScopeMeteosatEU.Model_FCI_BBox
    fci_bbox = {"2km": bbox_2km}
    fci_bbox["1km"] = (
        (2 * bbox_2km[0][0], 2 * bbox_2km[0][1]),
        (2 * bbox_2km[1][0], 2 * bbox_2km[1][1]),
    )
    fci = {res: MeteosatFCI(resolution=res, pixel_bbox=bbox) for (res, bbox) in fci_bbox.items()}
    return fci


def floor_to_slot(when: datetime) -> datetime:
    """Round `when` down onto the FCI scan grid.

    `MeteosatFCI` rejects any time that is not an exact multiple of `SCAN_FREQUENCY`
    seconds since 2000-01-01, so every candidate analysis time has to land on it.
    """
    when = when.astimezone(UTC) if when.tzinfo else when.replace(tzinfo=UTC)
    slots = int((when - FCI_EPOCH).total_seconds()) // MeteosatFCI.SCAN_FREQUENCY
    return FCI_EPOCH + slots * SCAN_STEP


def latest_fci_slot(
    n_frames: int,
    lookback: timedelta = SEARCH_LOOKBACK,
    min_lag: timedelta = MIN_LAG,
) -> datetime:
    """Newest downloadable slot whose `n_frames`-frame input window is complete.

    Slots younger than `min_lag` are excluded from the search, so the answer is the
    freshest initial condition this account can actually fetch rather than the freshest
    one the Data Store lists -- and a gap in the feed just moves it one slot further back
    instead of failing halfway through `fetch_data`.

    Searching FDHSI alone is enough: every channel the model reads comes from that
    collection, the IR/WV bands at 2 km and the VIS/NIR bands at 1 km. HRFI only repeats
    four of them at finer resolution, which this pipeline does not use.
    """
    now = datetime.now(UTC)
    token = eumdac.AccessToken(credentials=eumetsat_credentials())
    collection = eumdac.DataStore(token).get_collection(MeteosatFCI.COLLECTION_ID["FDHSI"])
    products = collection.search(dtstart=now - lookback, dtend=now - min_lag)

    slots = {floor_to_slot(p.sensing_start) for p in products}
    for slot in sorted(slots, reverse=True):
        if slots.issuperset(slot - i * SCAN_STEP for i in range(n_frames)):
            return slot
    raise FileNotFoundError(
        f"no {n_frames} consecutive FDHSI slots published between {lookback} and "
        f"{min_lag} ago ({len(slots)} slots returned)"
    )


def resolve_start_time(
    pinned: np.datetime64 | None, n_frames: int, min_lag: timedelta = MIN_LAG
) -> datetime:
    """The analysis time to run: pinned, else latest available, else `REALTIME_LAG` back."""
    if pinned is not None:
        return floor_to_slot(pinned.astype("datetime64[s]").astype(datetime).replace(tzinfo=UTC))
    try:
        return latest_fci_slot(n_frames, min_lag=min_lag)
    except Exception as err:  # no credentials, no network, feed gap beyond the lookback
        fallback = floor_to_slot(datetime.now(UTC) - REALTIME_LAG)
        print(f"Data Store search failed ({err!r}); falling back to {REALTIME_LAG} back")
        return fallback


def fetch_initial_condition(
    model: StormScopeMeteosatEU,
    fci: dict,
    start_time: np.datetime64,
    device: torch.device,
) -> tuple[torch.Tensor, CoordSystem]:
    """Step 3: fetch the 6-frame sliding window of FCI observations."""
    in_coords = model.input_coords()
    variables = in_coords["variable"]

    x_res = {}
    for res in ["2km", "1km"]:
        available_vars = fci[res].available_variables()
        if res == "1km":
            available_vars -= fci["2km"].available_variables()
        variables_res = [var for var in variables if var in available_vars]
        x_res[res] = fetch_data(
            fci[res],
            time=np.array([start_time]),
            variable=variables_res,
            lead_time=in_coords["lead_time"],
            device=device,
        )

    x, coords = StormScopeMeteosatEU.combine_1km_2km_inputs(*x_res["1km"], *x_res["2km"])
    del x_res
    x, coords = map_coords(x, coords, in_coords)
    return x, coords


def add_ensemble_dim(x: torch.Tensor, coords: dict, ensemble_size: int):
    """Step 4: expand to (ensemble, time, lead_time, variable, y, x)."""
    x = x.expand(ensemble_size, -1, -1, -1, -1, -1)
    coords["ensemble"] = np.arange(ensemble_size)
    coords.move_to_end("ensemble", last=False)
    return x, coords


def add_coord(
    backend: IceChunkBackend,
    name: str,
    values,
    dims: tuple[str, ...],
    attrs: dict | None = None,
    dtype=None,
    compressors=None,
) -> None:
    """Write a non-dimension coordinate, e.g. 2-D `lat`/`lon` over `(y, x)`.

    `IceChunkBackend.add_array` cannot: an n-dimensional entry in its
    CoordSystem is taken to *be* the grid and gets replaced by enumerated
    index dimensions, which would detach the array from the model's own
    `y`/`x` scan angles. This only touches `backend.root` (a zarr Group),
    so it needs no backend subclass.
    """
    values = np.asarray(values.detach().cpu() if torch.is_tensor(values) else values, dtype=dtype)
    # y/x split like the data arrays, `time` sized for many inits; any other dim stays whole
    chunks = tuple(
        CHUNKS[dim] if dim in ("y", "x") else COORD_TIME_CHUNK if dim == "time" else size
        for dim, size in zip(dims, values.shape, strict=True)
    )
    if name not in backend.root:
        array = backend.root.create_array(
            name,
            shape=values.shape,
            chunks=chunks,
            dtype=values.dtype,
            dimension_names=list(dims),
            compressors=compressors or backend.zarr_codecs,
        )
        array[:] = values
    if attrs:
        backend.root[name].attrs.update(attrs)


def add_grid_mapping(backend: IceChunkBackend, name: str, attrs: dict) -> None:
    """Write a CF grid mapping variable: a scalar whose attributes describe the CRS.

    Data arrays name it in their `grid_mapping` attribute. Its value is never read,
    so it is a single int32.
    """
    if name not in backend.root:
        array = backend.root.create_array(
            name, shape=(), dtype="int32", fill_value=0, dimension_names=()
        )
        array[...] = 0
    backend.root[name].attrs.update(attrs)


def geozarr_attrs(y, x) -> dict:
    """GeoZarr `proj` + `spatial` attributes for a group over this `(y, x)` grid.

    `y`/`x` are the stored (east-positive) scan angles of the pixel centres. The CRS
    is in metres, scan angle times `perspective_point_height`, so the affine transform
    is too. Its origin is the corner of pixel (0, 0); `y` runs south to north here, so
    the transform is south-up (`e > 0`).
    """
    y_m = np.asarray(y, dtype="float64") * PERSPECTIVE_POINT_HEIGHT
    x_m = np.asarray(x, dtype="float64") * PERSPECTIVE_POINT_HEIGHT
    dy, dx = y_m[1] - y_m[0], x_m[1] - x_m[0]
    if not (np.allclose(np.diff(y_m), dy) and np.allclose(np.diff(x_m), dx)):
        raise ValueError("y/x are not evenly spaced; no affine transform describes them")
    x0, y0 = x_m[0] - dx / 2, y_m[0] - dy / 2
    x1, y1 = x0 + dx * len(x_m), y0 + dy * len(y_m)
    return {
        "zarr_conventions": ZARR_CONVENTIONS,
        "proj:wkt2": FCI_CRS.to_wkt(),
        "proj:projjson": FCI_CRS.to_json_dict(),
        "spatial:dimensions": ["y", "x"],
        "spatial:shape": [len(y_m), len(x_m)],
        "spatial:transform": [float(dx), 0.0, float(x0), 0.0, float(dy), float(y0)],
        "spatial:bbox": [
            float(min(x0, x1)),
            float(min(y0, y1)),
            float(max(x0, x1)),
            float(max(y0, y1)),
        ],
        "spatial:registration": "pixel",
    }


def east_positive(coords) -> OrderedDict:
    """`coords` with `x` negated to CF's east-positive scan angle.

    earth2studio's FCI `x` is positive west. Stored as is, every CF reader (pyproj, GDAL,
    Flux) would mirror the image east-west. Applied to the schema *and* to every write,
    because `write` locates a slice by looking up coordinate values in the stored ones.
    """
    out = OrderedDict(coords)
    out["x"] = -np.asarray(out["x"])
    return out


def true_color(arr: np.ndarray, variables: list[str]) -> np.ndarray:
    """EUMETSAT-style "true-colour" RGB composite from the three VIS channels.

    Takes the (..., variable, y, x) array of a rollout step and returns
    (..., y, x, rgb) uint8 as stored -- display-ready, so plotting and tiling
    need no per-channel scaling. Data is 1..255, so that a black pixel never reads
    as missing; off-disk (NaN) pixels are `RGB_FILL`.
    """
    idx = [list(variables).index(ch) for ch in RGB_CHANNELS]
    rgb = np.clip(arr[..., idx, :, :] / 24.0, 0, 1) * 255
    rgb = np.interp(rgb, [0, 30, 60, 120, 190, 255], [0, 110, 160, 210, 240, 255])
    rgb = np.where(np.isnan(rgb), RGB_FILL, np.clip(np.round(rgb), 1, 255))
    return np.moveaxis(rgb, -3, -1).astype(RGB_DTYPE)


def airmass(arr: np.ndarray, variables: list[str]) -> np.ndarray:
    """EUMETSAT airmass RGB composite from the WV 6.3/7.3 and IR 9.7/10.5 um channels.

    Same (..., variable, y, x) -> (..., y, x, rgb) uint8 contract as `true_color`. The
    radiances are converted to brightness temperatures (K) first, and each band's
    0..1 stretch is scaled to 1..255.

    EUMETSAT recipe (EUMeTrain, "Compilation of RGB Recipes", p. 4): red WV6.2 - WV7.3,
    green IR9.7 - IR10.8, blue WV6.2 inverted, gamma 1 -- on FCI, WV6.3 and IR10.5. The
    ranges here are within ~1.5 K of the recipe's (-25..0, -40..+5, 243..208 K).
    """
    bt = {
        var: radiance_to_bt(arr[..., list(variables).index(var), :, :], 1.0 / WAVELENGTH_CM[var])
        for var in AIRMASS_CHANNELS
    }
    r = ((bt["fci63wv"] - bt["fci73wv"]) - (-23.8)) / (1.4 - (-23.8))
    g = ((bt["fci97ir"] - bt["fci105ir"]) - (-39.7)) / (4.1 - (-39.7))
    b = (bt["fci63wv"] - 244.5) / (209.4 - 244.5)  # WV6.3 inverted: dry upper levels -> blue
    rgb = np.stack([r, g, b], axis=-1) * 255
    return np.where(np.isnan(rgb), RGB_FILL, np.clip(np.round(rgb), 1, 255)).astype(RGB_DTYPE)


def forecast_only(x_pred: torch.Tensor, coords_pred: OrderedDict):
    """Trim a rollout step to `lead_time >= 0` -- the forecast, analysis frame included.

    The generator's step 0 yields the initial condition: the six *observed* FCI frames
    at -50..0 min. Only the last of them, lead 0, is part of the forecast; the five
    earlier ones are model input, so they are dropped and never reach Icechunk.
    Steps 1..N are already all-positive and pass through untouched.
    """
    lead = coords_pred["lead_time"]
    start = int(np.searchsorted(lead, np.timedelta64(0, "s")))
    coords_out = coords_pred.copy()
    coords_out["lead_time"] = lead[start:]
    return x_pred[:, :, start:], coords_out


def step_composites(x_pred: torch.Tensor, coords_pred: OrderedDict, channels: list[str]):
    """One rollout step -> the `(tensors, coords, names)` to hand `write`.

    Only the two composites are stored, not the raw channels. Both are `(ensemble,
    time, lead_time, y, x, rgb)` uint8, so one `write` call covers them.

    `copy=True` because the generator keeps writing into its own buffers between
    yields -- and with non-blocking writes, the store write for this step may still be
    reading the array after the loop has moved on.
    """
    arr = x_pred.detach().to("cpu", torch.float32, copy=True).numpy()

    rgb_coords_step = east_positive(
        (dim, values) for dim, values in coords_pred.items() if dim != "variable"
    )
    rgb_coords_step["rgb"] = RGB_VALUES

    return (
        [torch.from_numpy(true_color(arr, channels)), torch.from_numpy(airmass(arr, channels))],
        rgb_coords_step,
        ["true_color", "airmass"],
    )


class ReopenableIceChunkBackend(IceChunkBackend):
    """`IceChunkBackend` that can reopen a repo holding a scalar array.

    Runs append to the existing cube, so the repo is reopened on every run. Base
    `_read_store_state` iterates each array's `dimension_names`, and zarr stores a
    scalar's `()` as None -- the CF grid mapping (see add_grid_mapping) is a scalar,
    so opening fails with a TypeError. Scalars have no dims or chunks to record, so
    this version of the scan just skips them.
    """

    def _read_store_state(self, chunks: dict[str, int]) -> None:
        self.coords = OrderedDict()
        self.chunks = chunks.copy()
        arrays = [(name, array) for name, array in self.root.arrays() if array.ndim > 0]
        for _, array in arrays:
            for dim in array.metadata.dimension_names:
                if dim not in self.coords:
                    self.coords[dim] = self.root[dim][:]
        for name, array in arrays:
            if name not in self.coords:
                self.chunks.update(zip(array.metadata.dimension_names, array.chunks, strict=True))


def build_backend(publish_target: str, output_dir: str, arraylake_repo: str):
    """Open the Icechunk store to publish into, keeping earlier runs. Returns (backend, location)."""
    if publish_target == "arraylake":
        import arraylake

        client = arraylake.Client(token=os.environ["ARRAYLAKE_API_TOKEN"])
        storage, location = client.get_icechunk_storage(arraylake_repo), arraylake_repo
    else:
        storage = location = os.path.join(output_dir, "stormscope_eu.icechunk")
    backend = ReopenableIceChunkBackend(storage, chunks=CHUNKS, zarr_codecs=[COMPRESSOR])
    return backend, location


def create_schema(
    backend: IceChunkBackend,
    model: StormScopeMeteosatEU,
    coords: CoordSystem,
    lead_time: np.ndarray,
) -> None:
    """Allocate the cube in an empty repo, sized for this run's init alone.

    Only the forecast is stored, so `lead_time` runs from 0 -- the observed analysis
    frame -- to n_steps * 10 min, and each rollout step fills its own slice. Later
    runs grow `time` by their own init (see `extend_schema`), so `time` only ever
    lists inits that were actually generated.
    """
    total_coords = east_positive(
        OrderedDict(
            ensemble=coords["ensemble"],
            time=coords["time"],
            lead_time=lead_time,
            y=coords["y"],
            x=coords["x"],
        )
    )
    rgb_coords = OrderedDict(**total_coords, rgb=RGB_VALUES)

    # `time` first, so `add_array` skips it rather than chunking it at this run's length.
    add_coord(backend, "time", total_coords["time"], ("time",))
    backend.coords["time"] = total_coords["time"]
    backend.add_array(rgb_coords, [])  # other coordinate arrays only
    # Created directly: `add_array` takes its dtype from initial data only, and passing
    # data would write every chunk of the cube.
    for name in ("true_color", "airmass"):
        backend.root.create_array(
            name,
            shape=[len(values) for values in rgb_coords.values()],
            chunks=[backend.chunks.get(dim, len(values)) for dim, values in rgb_coords.items()],
            dtype=RGB_DTYPE,
            fill_value=RGB_FILL,
            dimension_names=list(rgb_coords),
            compressors=backend.zarr_codecs,
        )
    backend.root["true_color"].attrs.update(
        {
            **GEOREF,
            **RGB_ENCODING,
            "long_name": "true-colour RGB composite (VIS 0.6/0.5/0.4 um), 0-255",
            "source_channels": " ".join(RGB_CHANNELS),
        }
    )
    backend.root["airmass"].attrs.update(
        {
            **GEOREF,
            **RGB_ENCODING,
            "long_name": "EUMETSAT airmass RGB composite (WV 6.3-7.3, IR 9.7-10.5, "
            "WV 6.3 um inverted brightness temperatures), 0-255",
            "source_channels": " ".join(AIRMASS_CHANNELS),
        }
    )
    backend.root["rgb"].attrs.update(RGB_BAND_ATTRS)
    add_coord(
        backend,
        "lat",
        model.lat,
        ("y", "x"),
        {"standard_name": "latitude", "units": "degrees_north"},
        dtype="float32",
        compressors=[LATLON_COMPRESSOR],
    )
    add_coord(
        backend,
        "lon",
        model.lon,
        ("y", "x"),
        {"standard_name": "longitude", "units": "degrees_east"},
        dtype="float32",
        compressors=[LATLON_COMPRESSOR],
    )
    add_grid_mapping(backend, GRID_MAPPING, FCI_GEOS)
    for dim, axis, direction in (("x", "X", "east"), ("y", "Y", "north")):
        backend.root[dim].attrs.update(
            {
                "standard_name": f"projection_{dim}_coordinate",
                "units": "rad",
                "axis": axis,
                "long_name": f"FCI fixed grid projection {dim}-coordinate "
                f"(scan angle, positive {direction})",
            }
        )
    # valid_time is 2-D: one run per `time`, each with its own `lead_time` offsets.
    add_coord(
        backend,
        "valid_time",
        total_coords["time"][:, None] + total_coords["lead_time"][None, :],
        ("time", "lead_time"),
        {"standard_name": "time", "long_name": "valid time (time + lead_time)"},
    )
    backend.root["time"].attrs.update(
        {
            "standard_name": "forecast_reference_time",
            "long_name": "analysis (nowcast initialisation) time",
        }
    )
    backend.root["lead_time"].attrs.update(
        {
            "standard_name": "forecast_period",
            "long_name": "offset from analysis time; 0 = observed analysis frame, > 0 = "
            "forecast. Earlier observed frames are model input and are not stored.",
        }
    )
    backend.root["ensemble"].attrs.update(
        {"standard_name": "realization", "long_name": "ensemble member"}
    )
    backend.root.attrs.update(
        {
            "title": "StormScope-EU nowcast on MTG-I1 FCI",
            "model": "StormScopeMeteosatEU",
            "earth2studio_version": earth2studio.__version__,
            "fci_pixel_bbox": str(StormScopeMeteosatEU.Model_FCI_BBox),
            # GeoZarr proj/spatial conventions describe the group, not each array.
            **geozarr_attrs(total_coords["y"], total_coords["x"]),
        }
    )


# Every array with a `time` dimension; `extend_schema` grows them together.
TIME_ARRAYS = ("time", "valid_time", "true_color", "airmass")


def extend_schema(backend: IceChunkBackend, time: np.ndarray, lead_time: np.ndarray) -> None:
    """Append this run's init to `time` in an existing cube, uncommitted.

    The rollout steps then fill the new `time` slice of every data array in the same
    session, and one commit publishes both. A repo whose composites are not uint8
    (written before they were stored as bytes) is refused rather than appended to. An
    init already in `time` is a rerun: it keeps its slice, which the steps overwrite in
    place. An init older than the newest one is still appended at the end, so `time` is
    in commit order, not necessarily sorted.
    """
    for name in ("true_color", "airmass"):
        if backend.root[name].dtype != RGB_DTYPE:
            raise ValueError(
                f"repo stores {name} as {backend.root[name].dtype}, this pipeline writes "
                f"{RGB_DTYPE}; convert the repo's composites to {RGB_DTYPE} 0-255 (fill 0) "
                "or publish to an empty repo or branch"
            )
        backend.root[name].attrs.pop("_FillValue", None)  # ponytail: drop once main is rewritten
    stored_lead = backend.root["lead_time"][:]
    if not np.array_equal(stored_lead, lead_time):
        raise ValueError(
            f"repo has {len(stored_lead)} lead times (--n-steps {len(stored_lead) - 1}), this "
            f"run has {len(lead_time)} (--n-steps {len(lead_time) - 1}); rerun with the repo's --n-steps"
        )
    init = np.datetime_as_string(time[0], unit="m")
    stored_time = backend.coords["time"]
    if np.isin(time, stored_time).all():
        print(f"init {init} already in repo; overwriting its slice")
        return

    n = len(stored_time)
    for name in TIME_ARRAYS:
        array = backend.root[name]
        shape = list(array.shape)
        shape[array.metadata.dimension_names.index("time")] += len(time)
        array.resize(tuple(shape))
    backend.root["time"][n:] = time
    backend.root["valid_time"][n:] = time[:, None] + lead_time[None, :]
    # `write` locates slices through this in-memory copy, which is read once at open.
    backend.coords["time"] = np.concatenate([stored_time, time])


class StormScopeEUPipeline:
    """StormScope-EU loaded once, run on any number of inits.

    Loading and compiling the model is the slow part, so it happens in `__init__`;
    each call then fetches one initial condition, rolls it out and publishes it.
    Nothing carries over between calls except the model and data sources: the
    rollout state lives in the generator `create_iterator` returns, and the
    Icechunk backend is reopened per call so it sees the `time` axis as stored.
    """

    def __init__(
        self,
        device: torch.device | str = "auto",
    ):
        """Load the model onto `device` and build the FCI data sources.

        `device` is a torch device, or "auto" for CUDA when available, else CPU.
        """
        if device == "auto":
            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        else:
            self.device = torch.device(device)

        self.model = load_model(self.device)
        self.fci = build_data_sources()

    def __call__(
        self,
        start_time: np.datetime64,
        n_steps: int,
        publish_target: str,
        output_dir: str,
        arraylake_repo: str,
    ):
        """Run one nowcast from `start_time` and publish it in one commit.

        Fetches the FCI input window ending at `start_time`, adds this init to the
        cube (creating the cube in an empty repo), then writes each of the
        `n_steps + 1` forecast frames -- the analysis frame and `n_steps` 10-minute
        steps -- as soon as it is produced, and commits once after the last. An init
        already in the repo is rerun and its slice overwritten. `publish_target`,
        `output_dir` and `arraylake_repo` choose the store, as in `build_backend`.
        """
        os.makedirs(output_dir, exist_ok=True)

        with torch.no_grad():
            x, coords = fetch_initial_condition(self.model, self.fci, start_time, self.device)
            x, coords = add_ensemble_dim(x, coords, ensemble_size=1)

        channels = [str(v) for v in coords["variable"]]

        backend, location = build_backend(publish_target, output_dir, arraylake_repo)
        lead_time = np.arange(n_steps + 1) * self.model.output_times[0]
        init = np.datetime_as_string(coords["time"][0], unit="m")
        if "true_color" not in backend.root:
            create_schema(backend, self.model, coords, lead_time)
        else:
            extend_schema(backend, coords["time"], lead_time)

        for step, (x_pred, coords_pred) in enumerate(
            tqdm(self.model.create_iterator(x, coords), total=n_steps + 1)
        ):
            # Forecast only: step 0's five past observed frames are model input, not output.
            x_pred_out, coords_pred_out = forecast_only(x_pred, coords_pred)
            # Non-blocking: the backend's thread pool uploads while the GPU computes
            # the next step.
            backend.write(*step_composites(x_pred_out, coords_pred_out, channels))

            if step == n_steps:
                break

        # commit flushes the background writes above, then publishes the schema change
        # and every step together. Commit metadata makes `repo.ancestry()` filterable
        # without reading data. `rebase_with` lets the init land on top of a commit
        # made meanwhile by another writer; it still fails on a real conflict, such as
        # that commit also resizing `time`.
        message = f"stormscope-eu {init} lead 0-{int(lead_time[-1] / np.timedelta64(1, 'm'))} min"
        snapshot = backend.commit(
            message,
            metadata={"time": init, "status": "ingested"},
            rebase_with=icechunk.ConflictDetector(),
        )
        print(f"{message} -> {snapshot}")

        print(f"Published to {publish_target} ({location})")


def main():
    """Load the model once, then run every new init as it becomes available.

    Each pass resolves the freshest analysis time (see `resolve_start_time`) and runs
    it unless it was the last one run, in which case it sleeps `--wait-seconds` and
    looks again. An exception in a pass is printed and the loop carries on. With
    --start-time the pinned init is run once and the loop ends.
    """
    args = parse_args()
    pipeline = StormScopeEUPipeline(device=args.device)

    last_start = None
    wait_seconds = args.wait_seconds

    while True:
        try:
            analysis_time = resolve_start_time(
                args.start_time,
                n_frames=len(pipeline.model.input_coords()["lead_time"]),
                min_lag=args.min_lag,
            )
            lag_min = (datetime.now(UTC) - analysis_time).total_seconds() / 60
            print(
                f"analysis time {analysis_time:%Y-%m-%d %H:%M} UTC ({lag_min:.0f} min behind now)"
            )
            start_time = np.datetime64(
                analysis_time.replace(tzinfo=None)
            )  # numpy carries no timezone
            if start_time == last_start:
                time.sleep(wait_seconds)
                continue

            pipeline(
                start_time=start_time,
                n_steps=args.n_steps,
                publish_target=args.publish_target,
                output_dir=args.output_dir,
                arraylake_repo=args.arraylake_repo,
            )
            last_start = start_time
        except Exception as e:
            print(
                f"{datetime.now()}: Exception occurred: {e}, retrying after {wait_seconds} seconds"
            )
            time.sleep(wait_seconds)

        if args.start_time is not None:
            break  # run only one if start_time is fixed


if __name__ == "__main__":
    main()
