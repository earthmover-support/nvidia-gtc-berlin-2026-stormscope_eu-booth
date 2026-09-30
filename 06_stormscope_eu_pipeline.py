"""StormScope EU nowcasting pipeline for the EarthMover demo.

MTG/FCI observations (EUMETSAT) -> Earth2Studio inference (StormScopeMeteosatEU)
-> streaming Icechunk/ArrayLake publication -> tile-ready true-colour product.

This mirrors the official booth demo notebook (demo.ipynb):
`resolve_start_time()`'s EUMETSAT-Data-Store freshness search, the full
streaming schema (reduced here to the two composites, `true_color` and
`airmass`), and writing one Icechunk commit per
10-minute rollout step as it is produced, rather than computing the whole
forecast first and publishing once. Unlike the official demo, this writes to
the `stormscope_v2` group of `gtc-berlin/stormscope-eu` (a personal/test
repo), not the shared `gtc-berlin/stormscope_eu_gtc_berlin_2026` target --
see ARRAYLAKE_GROUP_PATH below.

Publishing goes through Earth2Studio's `earth2studio.io.IceChunkBackend`
directly, rather than the official demo's `ArraylakeBackend(IceChunkBackend)`
subclass: `arraylake.Client.get_icechunk_storage(name)` returns a plain
`icechunk.Storage` object, which is exactly what `IceChunkBackend`'s
`storage` parameter already accepts -- it calls
`icechunk.Repository.open_or_create(storage, ...)` internally either way, so
the hosted-vs-local choice is just which `storage` object gets passed in,
and no subclass is needed. `add_coord()` below is the one piece of the
official demo's subclass that base `IceChunkBackend` genuinely lacks --
writing a non-dimension 2-D coordinate (`lat`/`lon`) without it being taken
for a new index dimension -- ported as a plain function against
`backend.root` (a zarr Group) instead, since that is all it ever touched.

`add_array()` forwards unrecognized kwargs straight to
`zarr.Group.create_array()`, which has no `attrs` parameter (the official
demo's subclassed `add_array` absorbs `attrs=` itself; base `IceChunkBackend`
does not) -- attrs must be set via `.attrs.update()` on the array after
creation, as everywhere below.

Env vars required:
    EUMETSAT_CONSUMER_KEY, EUMETSAT_CONSUMER_SECRET
        (or credentials cached in ~/.eumdac/credentials)
    ARRAYLAKE_API_TOKEN
        only needed with --publish-target arraylake

Install:
    uv pip install "earth2studio[stormscope,data] @ git+https://github.com/NVIDIA/earth2studio.git" \
        cartopy python-dotenv icechunk arraylake xarray zarr eumdac

Usage:
    python 06_stormscope_eu_pipeline.py --n-steps 3
    python 06_stormscope_eu_pipeline.py --start-time 2026-06-30T12:00 --n-steps 12
    python 06_stormscope_eu_pipeline.py --publish-target arraylake --arraylake-repo gtc-berlin/stormscope-eu
    python 06_stormscope_eu_pipeline.py --help
"""

import argparse
import os
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

import cartopy.crs as ccrs
import eumdac
import matplotlib.pyplot as plt
import numpy as np
import pyproj
import torch
from dotenv import load_dotenv
from tqdm import tqdm
from zarr.codecs import ZstdCodec

import earth2studio
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

load_dotenv()

# ============================================================================
# CONFIG (defaults; override with CLI flags, see parse_args)
# ============================================================================
RGB_CHANNELS = ["fci06vis", "fci05vis", "fci04vis"]  # VIS 0.6 / 0.5 / 0.4 um
RGB_VALUES = np.array(["red", "green", "blue"])
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
# (y=1120, x=2432), ~8 MB per composite chunk instead of the whole frame (~33 MB). `rgb`
# is absent, and the backend leaves a dimension it has no chunk for unsplit, so a chunk
# holds all three bands. `lat`/`lon` use the same y/x chunks (see add_coord).
CHUNKS = {"ensemble": 1, "time": 1, "lead_time": 1, "y": 560, "x": 1216}
COMPRESSOR = ZstdCodec(level=3)

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
FCI_GEOS["crs_wkt"] = pyproj.CRS.from_cf(FCI_GEOS).to_wkt()  # for GDAL/rioxarray

# `grid_mapping` is what the Flux tiles service projects with. The 2-D lat/lon alone are
# not enough: the domain's top corners are off the Earth's disk, so their lat/lon are NaN,
# and Flux drops every grid row containing one -- everything north of ~50N. `coordinates`
# points at the non-dimension coordinates written by `add_coord`; without it
# `xr.open_zarr` would read them back as data variables instead of coordinates of every
# field.
GEOREF = {"coordinates": "lat lon valid_time", "grid_mapping": GRID_MAPPING}
PUBLISH_TARGETS = ["local", "arraylake", "none"]
# stormscope-eu's root already holds this script's older ring-buffer schema
# (05_stormscope_eu_pipeline.py's previous version) -- IceChunkBackend's own
# root-member scan breaks the moment it finds a subgroup instead of an array
# while listing, so root and any subgroup are mutually exclusive for it, not
# just a style choice. This notebook-mirroring, single-cube-per-run schema
# is fundamentally different (no ring buffer) and
# needs its own group.
ARRAYLAKE_GROUP_PATH = "stormscope_v2"

# EUMETSAT Data Store freshness search (see resolve_start_time), matching
# the official demo's defaults.
SCAN_STEP = timedelta(seconds=MeteosatFCI.SCAN_FREQUENCY)  # FCI repeat cycle: 10 min
FCI_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)  # anchor of the scan-slot grid
SEARCH_LOOKBACK = timedelta(hours=6)  # how far back to hunt for a complete input window
REALTIME_LAG = timedelta(hours=2)  # blind fallback when the Data Store cannot be searched
# Youngest slot this account is allowed to *download*. The Data Store publishes a product
# ~3 minutes after its scan ends and lists it immediately, but downloading one less than an
# hour past its sensing end requires the EUMETSAT NRT licence on the account -- without it
# the search still returns the slot and `fetch_data` then fails with a 403
# "NRTLicense required to access this collection". 80 min is that boundary plus a margin;
# holding the NRT licence lets this go to 0, moving the run to ~20 minutes behind real time.
MIN_LAG = timedelta(minutes=80)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="StormScope EU nowcast: fetch MTG/FCI -> Earth2Studio rollout -> RGB PNG "
                     "-> streaming Icechunk/ArrayLake publish",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--start-time", default=None,
                   help="initial-condition time, ISO 8601 UTC (e.g. 2026-06-30T12:00). "
                        "Default: the freshest slot the EUMETSAT Data Store can serve "
                        "this account (see MIN_LAG), falling back to --realtime-lag-hours "
                        "behind now if the Data Store can't be searched.")
    p.add_argument("--n-steps", type=int, default=12, help="forecast steps of 10 min (12 = 2 h)")
    p.add_argument("--output-dir", default="outputs", help="where the PNG and local Icechunk store go")
    p.add_argument("--publish-target", choices=PUBLISH_TARGETS, default=None,
                   help="local = Icechunk repo on disk, arraylake = hosted repo (needs ARRAYLAKE_API_TOKEN), "
                        "none = skip publish [env: STORMSCOPE_PUBLISH_TARGET, default local]")
    p.add_argument("--arraylake-repo", default=os.environ.get("ARRAYLAKE_REPO"),
                   help="ArrayLake org/repo when --publish-target arraylake [env: ARRAYLAKE_REPO]")
    p.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    args = p.parse_args()

    if args.n_steps < 1:
        p.error("--n-steps must be >= 1")
    if args.start_time is not None:
        try:
            args.start_time = np.datetime64(args.start_time)
        except ValueError:
            p.error(f"--start-time {args.start_time!r} is not an ISO 8601 timestamp")
        if np.isnat(args.start_time):
            p.error("--start-time must be a real timestamp")
        if not MeteosatFCI.available(args.start_time):
            p.error(f"--start-time {args.start_time} has no MTG FCI data (must be on a 10-min boundary "
                    "within the operational period)")
    if args.publish_target is None:
        # argparse does not check `choices` against an env-derived default
        args.publish_target = os.environ.get("STORMSCOPE_PUBLISH_TARGET", "local")
        if args.publish_target not in PUBLISH_TARGETS:
            p.error(f"STORMSCOPE_PUBLISH_TARGET={args.publish_target!r} must be one of {PUBLISH_TARGETS}")
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


def build_data_sources() -> tuple[dict, tuple]:
    """Step 2: FCI data sources at 1km/2km matching the model's pixel bbox."""
    bbox_2km = StormScopeMeteosatEU.Model_FCI_BBox
    fci_bbox = {"2km": bbox_2km}
    fci_bbox["1km"] = (
        (2 * bbox_2km[0][0], 2 * bbox_2km[0][1]),
        (2 * bbox_2km[1][0], 2 * bbox_2km[1][1]),
    )
    fci = {
        res: MeteosatFCI(resolution=res, pixel_bbox=bbox)
        for (res, bbox) in fci_bbox.items()
    }
    return fci, bbox_2km


def floor_to_slot(when: datetime) -> datetime:
    """Round `when` down onto the FCI scan grid.

    `MeteosatFCI` rejects any time that is not an exact multiple of `SCAN_FREQUENCY`
    seconds since 2000-01-01, so every candidate analysis time has to land on it.
    """
    when = (
        when.astimezone(timezone.utc)
        if when.tzinfo
        else when.replace(tzinfo=timezone.utc)
    )
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
    now = datetime.now(timezone.utc)
    token = eumdac.AccessToken(credentials=eumetsat_credentials())
    collection = eumdac.DataStore(token).get_collection(
        MeteosatFCI.COLLECTION_ID["FDHSI"]
    )
    products = collection.search(dtstart=now - lookback, dtend=now - min_lag)

    slots = {floor_to_slot(p.sensing_start) for p in products}
    for slot in sorted(slots, reverse=True):
        if slots.issuperset(slot - i * SCAN_STEP for i in range(n_frames)):
            return slot
    raise FileNotFoundError(
        f"no {n_frames} consecutive FDHSI slots published between {lookback} and "
        f"{min_lag} ago ({len(slots)} slots returned)"
    )


def resolve_start_time(pinned: np.datetime64 | None, n_frames: int) -> datetime:
    """The analysis time to run: pinned, else latest available, else `REALTIME_LAG` back."""
    if pinned is not None:
        return floor_to_slot(pinned.astype("datetime64[s]").astype(datetime).replace(tzinfo=timezone.utc))
    try:
        return latest_fci_slot(n_frames)
    except Exception as err:  # no credentials, no network, feed gap beyond the lookback
        fallback = floor_to_slot(datetime.now(timezone.utc) - REALTIME_LAG)
        print(f"Data Store search failed ({err!r}); falling back to {REALTIME_LAG} back")
        return fallback


def fetch_initial_condition(
    model: StormScopeMeteosatEU,
    fci: dict,
    start_time: np.datetime64,
    device: torch.device,
):
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

    x, coords = StormScopeMeteosatEU.combine_1km_2km_inputs(
        *x_res["1km"], *x_res["2km"]
    )
    del x_res
    x, coords = map_coords(x, coords, in_coords)
    return x, coords


def add_ensemble_dim(x: torch.Tensor, coords: dict, ensemble_size: int):
    """Step 4: expand to (ensemble, time, lead_time, variable, y, x)."""
    x = x.expand(ensemble_size, -1, -1, -1, -1, -1)
    coords["ensemble"] = np.arange(ensemble_size)
    coords.move_to_end("ensemble", last=False)
    return x, coords


def rgb_channel_indices(model: StormScopeMeteosatEU) -> list[int]:
    return [list(model.variables).index(ch) for ch in RGB_CHANNELS]


def frame_valid_time(coords_pred: dict) -> np.datetime64:
    # the model keeps `time` fixed at the IC time and advances only lead_time
    return coords_pred["time"][0] + coords_pred["lead_time"][0]


def render_rgb(rgb_pred, coords_pred, bbox_2km, out_path: str):
    """True-colour RGB composite (VIS 0.6/0.5/0.4 um) in geostationary proj, as a PNG."""
    rgb = (rgb_pred / 24.0).clamp(min=0, max=1) * 255
    rgb = rgb.permute(1, 2, 0).numpy()
    rgb = np.interp(rgb, [0, 30, 60, 120, 190, 255], [0, 110, 160, 210, 240, 255]) / 255.0

    proj = ccrs.Geostationary(
        central_longitude=0.0,
        satellite_height=PERSPECTIVE_POINT_HEIGHT,
        globe=ccrs.Globe(semimajor_axis=SEMI_MAJOR_AXIS, semiminor_axis=SEMI_MINOR_AXIS),
    )
    extent = MeteosatFCI.projection_extent(resolution="2km", pixel_bbox=bbox_2km)

    aspect = (extent[1] - extent[0]) / (extent[3] - extent[2])
    fig_height = 8
    fig = plt.figure(figsize=(fig_height * aspect, fig_height))
    ax = fig.add_subplot(1, 1, 1, projection=proj)
    ax.set_xlim(extent[0], extent[1])
    ax.set_ylim(extent[2], extent[3])
    ax.coastlines(color="white", linewidth=0.8)
    ax.imshow(rgb, transform=proj, extent=extent, origin="lower")

    valid_time = str(frame_valid_time(coords_pred).astype("datetime64[m]")).replace("T", " ")
    lead_min = int(coords_pred["lead_time"][0].astype("timedelta64[m]") / np.timedelta64(1, "m"))
    ax.set_title(f"StormScopeMeteosatEU - RGB\nValid {valid_time} UTC  (+{lead_min} min)")

    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return rgb, extent


def add_coord(
    backend: IceChunkBackend,
    name: str,
    values,
    dims: tuple[str, ...],
    attrs: dict | None = None,
) -> None:
    """Write a non-dimension coordinate, e.g. 2-D `lat`/`lon` over `(y, x)`.

    `IceChunkBackend.add_array` cannot: an n-dimensional entry in its
    CoordSystem is taken to *be* the grid and gets replaced by enumerated
    index dimensions, which would detach the array from the model's own
    `y`/`x` scan angles. Adapted from the official booth demo's
    `ArraylakeBackend.add_coord` -- that logic only ever touches `self.root`
    (a zarr Group), so it works here as a plain function against
    `backend.root` without needing a backend subclass.
    """
    values = np.asarray(values.detach().cpu() if torch.is_tensor(values) else values)
    # y/x split like the data arrays; any other dim stays whole
    chunks = tuple(
        CHUNKS[dim] if dim in ("y", "x") else size for dim, size in zip(dims, values.shape)
    )
    if name not in backend.root:
        array = backend.root.create_array(
            name,
            shape=values.shape,
            chunks=chunks,
            dtype=values.dtype,
            dimension_names=list(dims),
            compressors=backend.zarr_codecs,
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
    (..., y, x, rgb) floats in 0..1 -- display-ready, so plotting and tiling
    need no per-channel scaling. Same recipe as `render_rgb`, vectorised
    over every leading dim instead of a single (channel, y, x) frame.
    """
    idx = [list(variables).index(ch) for ch in RGB_CHANNELS]
    rgb = np.clip(arr[..., idx, :, :] / 24.0, 0, 1) * 255
    rgb = np.interp(
        rgb, [0, 30, 60, 120, 190, 255], [0, 110, 160, 210, 240, 255]
    ) / 255.0
    return np.moveaxis(rgb, -3, -1).astype("float32")


def airmass(arr: np.ndarray, variables: list[str]) -> np.ndarray:
    """EUMETSAT airmass RGB composite from the WV 6.3/7.3 and IR 9.7/10.5 um channels.

    Same (..., variable, y, x) -> (..., y, x, rgb) 0..1 contract as `true_color`. The
    radiances are converted to brightness temperatures (K) first. Off-disk pixels stay
    NaN.

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
    return np.clip(np.stack([r, g, b], axis=-1), 0, 1).astype("float32")


def forecast_only(x_pred: torch.Tensor, coords_pred: OrderedDict):
    """Trim a rollout step to `lead_time >= 0` -- the forecast, analysis frame included.

    The generator's step 0 yields the initial condition: the six *observed* FCI frames
    at -50..0 min. Only the last of them, lead 0, is part of the forecast; the five
    earlier ones are model input, so they are dropped and never reach Icechunk.
    Steps 1..N are already all-positive and pass through untouched.
    """
    lead = coords_pred["lead_time"]
    start = int(np.searchsorted(lead, np.timedelta64(0, "s")))
    if start == 0:
        return x_pred, coords_pred
    coords_out = coords_pred.copy()
    coords_out["lead_time"] = lead[start:]
    return x_pred[:, :, start:], coords_out


def step_writes(x_pred: torch.Tensor, coords_pred: OrderedDict, channels: list[str]):
    """One rollout step -> the `(tensors, coords, names)` to hand `write`.

    Only the two composites are stored, not the raw channels. Both are `(ensemble,
    time, lead_time, y, x, rgb)`, so one `write` call covers them.

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


def lead_minutes(lead_time: np.ndarray) -> np.ndarray:
    return (lead_time / np.timedelta64(1, "m")).astype(int)


def build_backend(publish_target: str, output_dir: str, arraylake_repo: str):
    """Open the Icechunk store to publish into. Returns (backend, location, group_path)."""
    local_icechunk_path = os.path.join(output_dir, "stormscope_eu.icechunk")
    if publish_target == "arraylake":
        import arraylake

        # get_icechunk_storage returns a plain icechunk.Storage -- exactly what
        # IceChunkBackend's `storage` param accepts, so no Arraylake-specific
        # subclass is needed (see module docstring).
        client = arraylake.Client(token=os.environ["ARRAYLAKE_API_TOKEN"])
        storage = client.get_icechunk_storage(arraylake_repo)
        group_path = ARRAYLAKE_GROUP_PATH
        backend = IceChunkBackend(
            storage=storage, chunks=CHUNKS, zarr_codecs=[COMPRESSOR],
            backend_kwargs={"overwrite": True, "path": group_path},
        )
        location = f"{arraylake_repo}::{group_path}"
    else:
        backend = IceChunkBackend(
            local_icechunk_path, chunks=CHUNKS, zarr_codecs=[COMPRESSOR],
            backend_kwargs={"overwrite": True},
        )
        location = local_icechunk_path
        group_path = None
    return backend, location, group_path


def commit(backend: IceChunkBackend, message: str, group_path: str | None) -> str:
    """`backend.commit`, keeping the group this backend was opened on.

    `IceChunkBackend.commit()` re-opens the new session's root as
    `zarr.group(store)` with no path, so a backend opened on a subgroup
    would silently drop back to the repo root after its first commit.
    """
    import zarr

    snapshot_id = backend.commit(message)
    if group_path:
        backend.root = zarr.group(backend.store, overwrite=False, path=group_path)
    return snapshot_id


def main():
    args = parse_args()
    os.makedirs(args.output_dir, exist_ok=True)

    if args.device == "auto":
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    else:
        device = torch.device(args.device)

    model = load_model(device)
    fci, bbox_2km = build_data_sources()
    in_coords = model.input_coords()

    analysis_time = resolve_start_time(args.start_time, n_frames=len(in_coords["lead_time"]))
    lag_min = (datetime.now(timezone.utc) - analysis_time).total_seconds() / 60
    print(f"analysis time {analysis_time:%Y-%m-%d %H:%M} UTC ({lag_min:.0f} min behind now)")
    start_time = np.datetime64(analysis_time.replace(tzinfo=None))  # numpy carries no timezone

    with torch.no_grad():
        x, coords = fetch_initial_condition(model, fci, start_time, device)
        x, coords = add_ensemble_dim(x, coords, ensemble_size=1)

    channels = [str(v) for v in coords["variable"]]

    if args.publish_target == "none":
        backend = group_path = None
    else:
        backend, location, group_path = build_backend(
            args.publish_target, args.output_dir, args.arraylake_repo
        )
        # The cube, allocated once. Only the forecast is stored, so `lead_time` runs
        # from 0 -- the observed analysis frame -- to n_steps * 10 min, and each step
        # below fills its own slice of it.
        total_coords = east_positive(
            OrderedDict(
                ensemble=coords["ensemble"],
                time=coords["time"],
                lead_time=np.arange(args.n_steps + 1) * model.output_times[0],
                y=coords["y"],
                x=coords["x"],
            )
        )
        rgb_coords = OrderedDict(**total_coords, rgb=RGB_VALUES)

        backend.add_array(total_coords, [])  # coordinate arrays only
        backend.add_array(rgb_coords, ["true_color", "airmass"], fill_value=float("nan"))
        backend.root["true_color"].attrs.update(
            {
                **GEOREF,
                "long_name": "true-colour RGB composite (VIS 0.6/0.5/0.4 um), 0-1",
                "source_channels": " ".join(RGB_CHANNELS),
            }
        )
        backend.root["airmass"].attrs.update(
            {
                **GEOREF,
                "long_name": "EUMETSAT airmass RGB composite (WV 6.3-7.3, IR 9.7-10.5, "
                             "WV 6.3 um inverted brightness temperatures), 0-1",
                "source_channels": " ".join(AIRMASS_CHANNELS),
            }
        )
        add_coord(
            backend, "lat", model.lat, ("y", "x"),
            {"standard_name": "latitude", "units": "degrees_north"},
        )
        add_coord(
            backend, "lon", model.lon, ("y", "x"),
            {"standard_name": "longitude", "units": "degrees_east"},
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
            backend, "valid_time",
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
            }
        )
        init = np.datetime_as_string(coords["time"][0], unit="m")
        print("schema ->", commit(backend, f"stormscope-eu {init} schema", group_path))

    rgb_last = coords_last = None
    for step, (x_pred, coords_pred) in enumerate(
        tqdm(model.create_iterator(x, coords), total=args.n_steps + 1)
    ):
        ch_idx = rgb_channel_indices(model)
        rgb_last, coords_last = x_pred[0, 0, 0, ch_idx].detach().cpu(), coords_pred

        if backend is not None:
            # Forecast only: step 0's five past observed frames are model input, not output.
            x_pred_out, coords_pred_out = forecast_only(x_pred, coords_pred)
            backend.write(*step_writes(x_pred_out, coords_pred_out, channels))

            lead = lead_minutes(coords_pred_out["lead_time"])
            if step == 0:
                message = f"stormscope-eu {init} analysis frame (lead {lead[-1]} min)"
            else:
                message = f"stormscope-eu {init} +{lead[-1]} min"
            # commit flushes the background writes above, then opens a fresh session --
            # an Icechunk session is spent once committed.
            tqdm.write(f"{message} -> {commit(backend, message, group_path)}")

        if step == args.n_steps:
            break

    png_path = os.path.join(args.output_dir, "stormscope_eu_latest.png")
    render_rgb(rgb_last, coords_last, bbox_2km, png_path)
    print(f"Saved {png_path}")

    if backend is not None:
        print(f"Published to {args.publish_target} ({location})")


if __name__ == "__main__":
    main()
