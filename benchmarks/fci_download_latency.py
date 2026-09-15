"""Time a single 10-minute FCI initial-condition download from the EUMETSAT Data Store.

Why: `demo.ipynb` runs on a Coiled VM in `us-east-1` (same region as the Arraylake
bucket), but the model input comes from EUMETSAT in Darmstadt. This measures how much
of the initialisation latency is the transatlantic hop, by running the identical
download in `us-east-1` and `eu-central-1`.

**What is timed is one 10-minute slot**, not the six-frame input window: in a rolling
near-real-time loop the five earlier frames are already in `MeteosatFCI`'s cache from
the previous run, so the only new work per cycle is the newest slot.

The download path is a faithful copy of `earth2studio.data.MeteosatFCI._fetch_product`
/ `._download_product` (token -> collection search -> stream the product zip ->
extract the BODY `.nc` segments), with a stopwatch on each phase. It is a copy rather
than a call so the benchmark needs `eumdac` alone: pulling in earth2studio would mean
torch and a ~10 GB per-region image build, none of which touches the network path
under test.

Both the 2 km and the 1 km `MeteosatFCI` instances in the notebook read from the same
FDHSI product and share one cache directory, so one slot is exactly one zip.

Usage
-----
    python fci_download_latency.py --slots 2026-09-15T09:00 2026-09-15T09:10 ...

Pass the same explicit slots to every region -- product size varies over the day, so
letting each region pick its own "latest" slot would not compare like with like.
Slots must sit on the 10-minute FCI grid and be older than the NRT window (~80 min)
unless the account holds the EUMETSAT NRT licence.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import ssl
import time
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import eumdac

FDHSI = "EO:EUM:DAT:0662"  # MeteosatFCI.COLLECTION_ID["FDHSI"]
SCAN_FREQUENCY = 600  # MeteosatFCI.SCAN_FREQUENCY: FCI repeat cycle, seconds
CHUNK = 1 << 20  # stream granularity; shutil.copyfileobj uses 64 KiB, this is closer
# to what a fast link wants and only affects the read loop, not the transfer itself.
API_HOST = "https://api.eumetsat.int"


def credentials() -> tuple[str, str]:
    """EUMETSAT consumer key/secret, as `earth2studio.data.utils_eumetsat` finds them."""
    key = os.environ.get("EUMETSAT_CONSUMER_KEY")
    secret = os.environ.get("EUMETSAT_CONSUMER_SECRET")
    if key and secret:
        return key, secret
    cred_file = Path.home() / ".eumdac" / "credentials"
    if cred_file.is_file():
        key, _, secret = cred_file.read_text().strip().partition(",")
        return key.strip(), secret.strip()
    raise RuntimeError("set EUMETSAT_CONSUMER_KEY / EUMETSAT_CONSUMER_SECRET")


def rtt_probe(n: int = 5) -> dict[str, float]:
    """Round-trip time to the Data Store API, TLS handshake included.

    A bare-latency number next to the throughput numbers: it separates "the data is
    far away" from "the account is being throttled".
    """
    ctx = ssl.create_default_context()
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        req = urllib.request.Request(f"{API_HOST}/data/browse/collections", method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                resp.read(1)
        except Exception:  # noqa: BLE001 -- an HTTP error still measures the round trip
            pass
        samples.append(time.perf_counter() - t0)
    return {"rtt_min_s": min(samples), "rtt_median_s": sorted(samples)[len(samples) // 2]}


def download_slot(slot: datetime, cache: Path) -> dict[str, float | str | int]:
    """Download and extract the FDHSI product for one scan slot, timing each phase.

    Returns the phase timings plus the transferred size, so the region comparison can
    say whether a difference is latency (search, time to first byte) or bandwidth.
    """
    product_cache = cache / f"mtg_{slot:%Y%m%dT%H%M%S}"
    shutil.rmtree(product_cache, ignore_errors=True)  # always a cold fetch
    product_cache.mkdir(parents=True)
    zip_path = product_cache / "product.zip"

    t0 = time.perf_counter()
    token = eumdac.AccessToken(credentials=credentials())
    str(token)  # the token is fetched lazily on first use
    t_auth = time.perf_counter()

    collection = eumdac.DataStore(token).get_collection(FDHSI)
    half = timedelta(seconds=SCAN_FREQUENCY // 2)
    products = list(collection.search(dtstart=slot - half, dtend=slot + half))
    t_search = time.perf_counter()
    if not products:
        raise FileNotFoundError(f"no FDHSI product for {slot:%Y-%m-%d %H:%M} UTC")

    # Stream the zip in a manual loop rather than `shutil.copyfileobj` so the first
    # chunk can be timed: time-to-first-byte is the part a region change moves most.
    n_bytes = 0
    t_first_byte = None
    with products[0].open() as stream, open(zip_path, "wb") as fh:
        while chunk := stream.read(CHUNK):
            if t_first_byte is None:
                t_first_byte = time.perf_counter()
            fh.write(chunk)
            n_bytes += len(chunk)
    t_download = time.perf_counter()

    with zipfile.ZipFile(zip_path) as zf:
        body = [
            n
            for n in zf.namelist()
            if "BODY" in os.path.basename(n) and n.endswith(".nc")
        ]
        for name in body:
            zf.extract(name, product_cache)
    t_extract = time.perf_counter()

    zip_path.unlink()
    return {
        "slot": f"{slot:%Y-%m-%dT%H:%M}Z",
        "product": str(products[0]),
        "auth_s": t_auth - t0,
        "search_s": t_search - t_auth,
        "ttfb_s": (t_first_byte or t_search) - t_search,
        "download_s": t_download - t_search,
        "extract_s": t_extract - t_download,
        "total_s": t_extract - t0,
        "bytes": n_bytes,
        "mb_per_s": n_bytes / (1 << 20) / max(t_download - t_search, 1e-9),
        "n_body_segments": len(body),
    }


def parse_slot(text: str) -> datetime:
    slot = datetime.fromisoformat(text.replace("Z", "+00:00"))
    slot = slot.replace(tzinfo=timezone.utc) if slot.tzinfo is None else slot
    if (slot.minute * 60 + slot.second) % SCAN_FREQUENCY:
        raise argparse.ArgumentTypeError(f"{text} is not on the 10-minute FCI grid")
    return slot.astimezone(timezone.utc)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--slots",
        type=parse_slot,
        nargs="+",
        required=True,
        help="FCI scan slots to download, sequentially, e.g. 2026-09-15T09:00",
    )
    parser.add_argument(
        "--label",
        default=os.environ.get("BENCH_LABEL", platform.node()),
        help="tag for this run, e.g. the region; goes into the JSON output",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=Path(os.environ.get("BENCH_CACHE", "/tmp/fci-bench")),
    )
    args = parser.parse_args()

    print(f"label {args.label}  host {platform.node()}", flush=True)
    rtt = rtt_probe()
    print(f"api.eumetsat.int RTT min {rtt['rtt_min_s']*1e3:.0f} ms", flush=True)

    runs = []
    for slot in args.slots:
        run = download_slot(slot, args.cache)
        runs.append(run)
        print(
            f"{run['slot']}  {run['bytes']/(1<<20):7.1f} MiB  "
            f"auth {run['auth_s']:5.2f}s  search {run['search_s']:5.2f}s  "
            f"ttfb {run['ttfb_s']:5.2f}s  download {run['download_s']:6.2f}s "
            f"({run['mb_per_s']:5.1f} MiB/s)  extract {run['extract_s']:5.2f}s  "
            f"total {run['total_s']:6.2f}s",
            flush=True,
        )

    totals = sorted(r["total_s"] for r in runs)
    print(
        f"per-slot total: median {totals[len(totals)//2]:.2f}s  "
        f"min {totals[0]:.2f}s  max {totals[-1]:.2f}s",
        flush=True,
    )
    print("JSON " + json.dumps({"label": args.label, **rtt, "runs": runs}), flush=True)


if __name__ == "__main__":
    main()
