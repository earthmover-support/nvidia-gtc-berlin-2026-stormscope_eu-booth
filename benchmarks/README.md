# Does moving compute to Frankfurt speed up the EUMETSAT download?

`demo.ipynb` runs in `us-east-1`, next to the Arraylake bucket it writes to — but its
input comes from the EUMETSAT Data Store in Darmstadt, so every initialisation crosses
the Atlantic. This measures what that costs, by running the identical download in
`us-east-1` and `eu-central-1`.

**Answer: Frankfurt saves ~5 s per 10-minute slot (26.5 s → 21.6 s median, ~18 %), and
it is a latency win, not a bandwidth one. Not worth moving the run for on its own.**

## What was measured

One 10-minute slot — not the six-frame input window. In a rolling near-real-time loop
the five earlier frames are already in `MeteosatFCI`'s cache from the previous cycle, so
the only new work per cycle is the newest FDHSI product. Both the 2 km and the 1 km
`MeteosatFCI` instances in the notebook read from that same product and share one cache
directory, so one slot is exactly one ~0.9 GiB zip, 40 BODY segments.

[`fci_download_latency.py`](fci_download_latency.py) copies
`MeteosatFCI._fetch_product` / `._download_product` phase for phase (token → collection
search → stream the zip → extract the BODY `.nc` files) and puts a stopwatch on each. It
is a copy rather than a call so the benchmark depends on `eumdac` alone — pulling in
earth2studio would mean torch and a ~10 GB per-region image build, none of which touches
the network path under test.

Setup: `m7i.large` (2 vCPU, up to 12.5 Gbps) in both regions, cold cache every slot, the
same explicit slots in both regions (product size varies over the day, so "latest slot"
would not compare like with like). Two rounds of three slots each, with the region order
reversed in round 2 so that any server-side warm-cache effect does not land on the same
region twice. n = 6 per region, all on 2026-09-15 between 10:45 and 11:00 UTC.

| run | region | slots (UTC) | Coiled |
| --- | --- | --- | --- |
| round 1 | us-east-1 | 08:40, 08:50, 09:00 | <https://cloud.coiled.io/clusters/2051910> |
| round 1 | eu-central-1 | 08:40, 08:50, 09:00 | <https://cloud.coiled.io/clusters/2051916> |
| round 2 | eu-central-1 | 08:10, 08:20, 08:30 | <https://cloud.coiled.io/clusters/2051924> |
| round 2 | us-east-1 | 08:10, 08:20, 08:30 | <https://cloud.coiled.io/clusters/2051934> |

## Result

Medians over n = 6 per region, seconds unless stated. The `eu-central-1` column drops two
runs that hit a ~60 s server-side stall (see below); the raw column keeps them.

| phase | us-east-1 | eu-central-1 | eu, raw |
| --- | --- | --- | --- |
| API round trip (min of 5) | 0.81–1.05 | 0.50–0.53 | — |
| auth (token) | 0.33 | 0.05 | 0.05 |
| collection search | 3.04 | 1.10 | 1.15 |
| time to first byte | 2.66 | 1.35 | 1.66 |
| zip download | 19.33 | 16.84 | 19.01 |
| throughput (MiB/s) | 48.5 | 55.7 | 49.6 |
| unzip BODY segments (CPU) | 3.76 | 3.67 | 3.74 |
| **total per slot** | **26.54** | **21.60** | **24.01** |

Where the ~5 s comes from:

- **Control plane, ~3.5 s of it.** Auth + search + TTFB is 6.0 s from Virginia against
  2.5 s from Frankfurt — a handful of request/response round trips against an API whose
  RTT roughly halves (~1.0 s → ~0.5 s; both numbers are high because the probe includes
  TLS setup and an error response).
- **Transfer, ~2.5 s of it.** 55.7 vs 48.5 MiB/s on a ~0.9 GiB zip. Both regions are far
  below what an `m7i.large` can pull, so the ceiling is EUMETSAT's, not the network's —
  which is why closing 4,000 miles buys 15 % rather than a multiple.
- **Nothing in the unzip**, as expected: same CPU, same work, 3.7 s in both.

## Caveats

- **Two of the six `eu-central-1` runs stalled ~60 s before the first byte** (08:50 in
  round 1, 08:20 in round 2 — different slots, both the second download of their run),
  turning a 20 s fetch into an 80 s one. Nothing in `us-east-1` did this. Six samples per
  region cannot tell a regional property from an unlucky coincidence, and a ~60.0 s stall
  followed by a normal transfer looks like a server-side queue rather than a network
  path. If it is real and recurring, it dwarfs the 5 s the move saves, and it argues for
  a retry/timeout on the first byte rather than against Frankfurt.
- n = 6 per region, one instance type, one hour of one day. Treat the 5 s as an order of
  magnitude, not a number.

## What this means for the booth demo

Keep the run in `us-east-1`. Five seconds per cycle is noise next to what actually sets
the wall-clock lag of the demo:

- the **~80-minute NRT-licence hold-off** in `demo.ipynb` (`MIN_LAG`), which is three
  orders of magnitude larger and is an account-permissions question, not a compute-region
  one;
- the model rollout itself;
- and, on the other side of the run, the Icechunk writes — which *are* region-sensitive,
  and currently are in the right region. Moving compute to Frankfurt would trade a 5 s
  download win for a cross-region write on every 10-minute commit.

The one case that flips this: if the account gets the NRT licence and the target becomes
"newest slot, as fast as possible", the download stops being amortised over a long lag
and every second is visible at the booth. Even then the honest fix is the same first-byte
retry the stall above calls for.

## Reproducing

```bash
coiled env create --name fci-download-bench --workspace earthmover-devs \
  --architecture x86_64 --pip coiled/requirements-fci-download-bench.txt

coiled run --workspace earthmover-devs --software fci-download-bench \
  --name fci-bench --vm-type m7i.large --region eu-central-1 \
  -f benchmarks/fci_download_latency.py \
  --env EUMETSAT_CONSUMER_KEY=$EUMETSAT_CONSUMER_KEY \
  --env EUMETSAT_CONSUMER_SECRET=$EUMETSAT_CONSUMER_SECRET \
  --env BENCH_LABEL=eu-central-1 \
  "python benchmarks/fci_download_latency.py --slots 2026-09-15T08:10 2026-09-15T08:20"
```

Two things that cost time getting here, both worth knowing:

- The command has to be **one quoted string**; unquoted, Click eats `--slots` as a
  `coiled run` option.
- The environment needs **`coiled` installed** even though the script never imports it.
  `coiled run` hosts the command in a one-node Dask cluster, and an env with `eumdac`
  alone dies at startup with `Scheduler Stopped -> Software environment exited with error
  code 1` and no traceback in `coiled logs`.

---

# How many booth maps per second can the tiles service render? (#11)

One **map** = one booth frame: the 20 z4 tiles (512 px) the booth globe requests for one
lead. One run = 12 maps = 240 tiles. Target: a full run in ~10 s (~1.2 maps/s, ~28 tiles/s).

[`tiles_throughput.py`](tiles_throughput.py) requests those 240 tiles in playback order with
N requests in flight (N = 6 is one browser) against snapshot-pinned URLs, and reports
tiles/s, maps/s, latency, bytes read and status codes. Replica counts came from polling
`al compute status` every ~15 s.

**Answer (2026-10-01): about 1 tile/s per CPU. XS gives one browser 0.05 maps/s, S 0.10.
Results, limits and options: [docs/TILE_SERVER_PERFORMANCE.md](../docs/TILE_SERVER_PERFORMANCE.md).**
