"""How long after an MTG FCI slot is due does it become available on the EUMETSAT Data Store?

Each FDHSI slot is measured against two "due" times: the nominal slot time (what
`stormscope_eu_pipeline.py` uses as the init time) and the product's sensing end (when
the 10-minute repeat cycle finishes scanning). It is checked against two kinds of
availability:

    listed      the product appears in Data Store search. Its `ingested` metadata
                records this, so `history` reads it for past slots without waiting.
    downloadable  a download with this account succeeds. Without the EUMETSAT NRT
                licence the Data Store answers 403 until the product is old enough, so
                only `poll` can measure this, by trying a small download of each pending
                slot on every pass.

`throughput` downloads whole product zips the way the pipeline does -- earth2studio
fetches the full FDHSI zip for each of the model's 6 input frames, concurrently -- and
reports MB and MB/s, to size a run's download on this machine or a cloud instance.

Top-of-hour slots (:00) are reported separately: EUMETSAT's data policy makes hourly FCI
data free at any latency, so they download as soon as they are listed. The other five
slots of each hour need the NRT licence until an hour past their slot time.

Env vars: EUMETSAT_CONSUMER_KEY, EUMETSAT_CONSUMER_SECRET (or ~/.eumdac/credentials)

Usage:
    uv run python scripts/measure_fci_latency.py history --hours 24
    uv run python scripts/measure_fci_latency.py poll --minutes 120 --interval 60 --csv latency.csv
    uv run python scripts/measure_fci_latency.py throughput --frames 6
"""

import argparse
import csv
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import eumdac
import numpy as np
from dotenv import load_dotenv
from earth2studio.data.meteosat_fci import MeteosatFCI
from earth2studio.data.utils_eumetsat import eumetsat_credentials

load_dotenv()

SCAN_STEP = timedelta(seconds=MeteosatFCI.SCAN_FREQUENCY)  # 10 min
FIELDS = [
    "slot",
    "sensing_end",
    "listed",
    "first_seen",
    "downloadable",
    "size_mb",
    "listed_after_slot_min",
    "listed_after_end_min",
    "download_after_slot_min",
    "download_after_end_min",
]


def utc(t: datetime) -> datetime:
    """eumdac returns naive UTC datetimes."""
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t


def slot_of(product) -> datetime:
    """Nominal slot time: sensing start (slot + ~6 s) floored onto the 10-min grid."""
    start = utc(product.sensing_start)
    return start - (start - datetime(2000, 1, 1, tzinfo=UTC)) % SCAN_STEP


def collection():
    token = eumdac.AccessToken(credentials=eumetsat_credentials())
    return eumdac.DataStore(token).get_collection(MeteosatFCI.COLLECTION_ID["FDHSI"])


def search(coll, since: timedelta) -> list:
    now = datetime.now(UTC)
    return [p for p in coll.search(dtstart=now - since, dtend=now)]


def can_download(product) -> bool:
    """Read the first KB of the slot's smallest file (the TRAIL chunk). False on a 403."""
    entry = next((e for e in product.entries if "TRAIL" in e), product.entries[0])
    try:
        with product.open(entry=entry) as f:
            f.read(1024)
        return True
    except eumdac.product.ProductError as err:
        cause = err.__cause__ or err.__context__
        if "403" in str(cause):
            return False
        raise


def minutes(later: datetime | None, earlier: datetime) -> float | None:
    return None if later is None else round((later - earlier).total_seconds() / 60, 2)


def size_mb(product) -> float:
    """Size of the whole product zip; eumdac reports it in kB."""
    return round(product.size / 1000, 1)


def row(slot, end, listed, size, first_seen=None, downloadable=None) -> dict:
    return {
        "slot": slot,
        "sensing_end": end,
        "listed": listed,
        "first_seen": first_seen,
        "downloadable": downloadable,
        "size_mb": size,
        "listed_after_slot_min": minutes(listed, slot),
        "listed_after_end_min": minutes(listed, end),
        "download_after_slot_min": minutes(downloadable, slot),
        "download_after_end_min": minutes(downloadable, end),
    }


def fmt(t: datetime | None) -> str:
    return "-" if t is None else f"{t:%H:%M:%S}"


def print_table(rows: list[dict]) -> None:
    print(
        f"{'slot (UTC)':16} {'sens. end':>9} {'listed':>8} {'download':>8} "
        f"{'list-slot':>9} {'list-end':>8} {'dl-slot':>8} {'dl-end':>8} {'MB':>6}"
    )
    for r in rows:
        nums = [
            r[k]
            for k in (
                "listed_after_slot_min",
                "listed_after_end_min",
                "download_after_slot_min",
                "download_after_end_min",
            )
        ]
        print(
            f"{r['slot']:%Y-%m-%d %H:%M} {fmt(r['sensing_end']):>9} {fmt(r['listed']):>8} "
            f"{fmt(r['downloadable']):>8} "
            + " ".join(
                f"{'-' if n is None else f'{n:.1f}':>{w}}"
                for n, w in zip(nums, (9, 8, 8, 8), strict=True)
            )
            + f" {r['size_mb']:6.0f}"
        )


def print_summary(rows: list[dict]) -> None:
    groups = {
        "all slots": rows,
        ":00 slots": [r for r in rows if r["slot"].minute == 0],
        "other slots": [r for r in rows if r["slot"].minute != 0],
    }
    print("\nmin / median / p95 / max (n); gaps in minutes")
    for key, label in (
        ("listed_after_slot_min", "listed after slot"),
        ("listed_after_end_min", "listed after sensing end"),
        ("download_after_slot_min", "downloadable after slot"),
        ("download_after_end_min", "downloadable after sensing end"),
        ("size_mb", "product zip size (MB)"),
    ):
        for group, rs in groups.items():
            vals = np.array([r[key] for r in rs if r[key] is not None])
            if len(vals) == 0:
                continue
            q = np.percentile(vals, [0, 50, 95, 100])
            print(
                f"  {label:32} {group:12} "
                + " / ".join(f"{v:5.1f}" for v in q)
                + f"  ({len(vals)})"
            )


def write_csv(rows: list[dict], path: str) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} rows to {path}")


def history(args) -> list[dict]:
    """Listing gaps from the `ingested` metadata of every slot in the last --hours."""
    products = search(collection(), timedelta(hours=args.hours))
    rows = [row(slot_of(p), utc(p.sensing_end), utc(p.ingested), size_mb(p)) for p in products]
    return sorted(rows, key=lambda r: r["slot"])


def poll(args) -> list[dict]:
    """Watch new slots get listed, then try a download each pass until one succeeds.

    Only slots first listed during the poll are tracked, so their download time is
    bounded by --interval. A slot that never becomes downloadable before the poll ends
    keeps an empty `downloadable`; poll for over an hour to see the non-:00 boundary.
    """
    coll = collection()
    known = {slot_of(p) for p in search(coll, timedelta(hours=2))}
    print(f"ignoring {len(known)} slots already listed; tracking new ones")
    tracked: dict[datetime, dict] = {}
    pending: dict[datetime, object] = {}
    deadline = datetime.now(UTC) + timedelta(minutes=args.minutes)
    while datetime.now(UTC) < deadline:
        for p in search(coll, timedelta(minutes=40)):
            slot = slot_of(p)
            if slot in known or slot in tracked:
                continue
            tracked[slot] = row(
                slot, utc(p.sensing_end), utc(p.ingested), size_mb(p), datetime.now(UTC)
            )
            pending[slot] = p
            print(
                f"{datetime.now(UTC):%H:%M:%S} listed {slot:%H:%M} (ingested {fmt(utc(p.ingested))})"
            )
        for slot, p in list(pending.items()):
            checked = datetime.now(UTC)
            if can_download(p):
                r = tracked[slot]
                tracked[slot] = row(
                    slot, r["sensing_end"], r["listed"], r["size_mb"], r["first_seen"], checked
                )
                del pending[slot]
                print(f"{checked:%H:%M:%S} downloadable {slot:%H:%M}")
        time.sleep(args.interval)
    return sorted(tracked.values(), key=lambda r: r["slot"])


def download(product) -> tuple[float, float]:
    """Stream the whole product zip and discard it. Returns (MB, seconds)."""
    start, n = time.perf_counter(), 0
    with product.open() as f:
        while chunk := f.read(1 << 20):
            n += len(chunk)
    return n / 1e6, time.perf_counter() - start


def throughput(args) -> None:
    """Download the --frames newest slots this account can fetch, concurrently."""
    coll = collection()
    # an hour past the slot time: downloadable without the NRT licence
    old = [
        p
        for p in search(coll, timedelta(hours=3))
        if datetime.now(UTC) - slot_of(p) > timedelta(minutes=62)
    ]
    products = sorted(old, key=lambda p: p.sensing_start)[-args.frames :]
    print(f"downloading {len(products)} FDHSI zips, {len(products)} at a time")
    start = time.perf_counter()
    with ThreadPoolExecutor(len(products)) as pool:
        results = list(pool.map(download, products))
    wall = time.perf_counter() - start
    for p, (mb, secs) in zip(products, results, strict=True):
        print(f"  {slot_of(p):%H:%M}  {mb:6.0f} MB  {secs:6.1f} s  {mb / secs:6.1f} MB/s")
    total = sum(mb for mb, _ in results)
    print(
        f"total {total:.0f} MB in {wall:.1f} s wall = {total / wall:.1f} MB/s ({8 * total / wall:.0f} Mbit/s)"
    )


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="mode", required=True)
    h = sub.add_parser("history", help="listing gaps from product metadata, no waiting")
    h.add_argument("--hours", type=float, default=24, help="how far back to look")
    w = sub.add_parser("poll", help="live: listing and download gaps")
    w.add_argument("--minutes", type=float, default=120, help="how long to poll")
    w.add_argument("--interval", type=float, default=60, help="seconds between passes")
    for sp in (h, w):
        sp.add_argument("--csv", help="also write the per-slot rows to this CSV")
    t = sub.add_parser("throughput", help="time downloading whole product zips")
    t.add_argument("--frames", type=int, default=6, help="zips to download concurrently")
    args = p.parse_args()

    if args.mode == "throughput":
        throughput(args)
        return

    rows = history(args) if args.mode == "history" else poll(args)
    print_table(rows)
    print_summary(rows)
    if args.csv:
        write_csv(rows, args.csv)


if __name__ == "__main__":
    main()
