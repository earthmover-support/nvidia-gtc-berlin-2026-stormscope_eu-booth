# Flux tiles service performance for the booth

How fast the [Flux tiles service](https://docs.earthmover.io/compute/tiles) (`gtc-berlin`,
service [`tiles-4f49272b`](https://app.earthmover.io/gtc-berlin/services/tiles-4f49272b))
renders what the booth globe needs, what slows it down, and what the booth page does about
it. Measured on 2026-10-01 against `gtc-berlin/stormscope-eu_gtc_berlin_2026`, runs
`2026-09-30T09:00` and `09:10`.
Related: [WEB_TILES_SERVICE_REQUIREMENTS.md](WEB_TILES_SERVICE_REQUIREMENTS.md) (metadata), issues
#5 (cache/replicas), #9 (pyramids), #10 (RGB band order), #11 (throughput).

## TL;DR

1. **About 1 tile/s per CPU.** Each z4 booth tile reads ~10 MB of full-resolution data
   (`X-Multiscale-Level: root`) and costs about 1 CPU-second. A full booth run takes ~4 min
   on XS and ~2 min on S for one browser. The target (a run in ~10 s) is ~20× away.
2. **Use snapshot-pinned tile URLs.** Branch URLs come back with `max-age=0`, snapshot URLs
   with `max-age=2628000` (a month). The booth page uses snapshots.
3. **Load leads in playback order.** The service is the bottleneck, so requesting all 12
   leads at once leaves every frame half drawn for minutes. In order, the first complete
   frame arrives in ~20 s.
4. **Pyramids (#9) are what would actually close the gap.** More replicas don't shorten a
   single tile, and one browser can't keep more than ~6 requests in flight.
5. **Update 2026-10-15 (eu-west-1): pin the replicas during booth hours.** On `tiles-eu`
   (SKU S) with `min = max = 8`, a full run arrives in ~10 s at 20 in flight, and repeats
   within ~10 %. On autoscaling 0–8 the same load took 10–38 s, with single tiles up to 17 s,
   because new pods start with an empty cache. The service now speaks HTTP/2, so the ~6
   request limit above no longer applies. See
   [Server-side caches and HTTP/2](#server-side-caches-and-http2-2026-10-15).

## Units

- **Tile:** 512 px PNG, `style=raster/rgb`, `true_color` or `airmass`.
- **Map:** one booth frame, meaning all tiles visible at the booth camera for one lead. At
  1920×1080 with the camera sway, that's 20 z4 tiles (`z/y/x` = `4/3/5`–`4/6/11`).
- **Run:** 12 maps (+10 … +120 min) = 240 tiles.

## Throughput

A load script requested the 240 tiles of one run in playback order with N in flight, on
snapshot-pinned URLs. N = 6 is what one browser does. Replica counts are from polling `al compute status` every ~15 s.

| config | in flight | tiles/s | maps/s | full run | p50 / p95 tile | 504s | replicas |
|---|---|---|---|---|---|---|---|
| XS (1 CPU, 2 Gi) | 6 | 1.0 (0.76¹) | 0.05 (0.04¹) | 232–244 s (315 s¹) | 6.6–6.8 / ~10 s | 0 | 1 |
| XS | 24 | 1.9–2.4 | 0.10–0.12 | 99–125 s | ~9 / 19–25 s | 0–32 | 2–3 |
| XS | 96 | 3.0–3.4 | 0.15–0.17 | 70–81 s | ~24 / ~50 s | 57–157 | 4 |
| S (2 CPU, 4 Gi) | 6 | 2.1 | 0.10 | 114 s | 1.8–2.1 / ~8 s | 0 | 1 |

¹ A run that overlapped with other tile traffic (the service had scaled to 2 replicas).

Not measured: `min_replicas: 1`, more than 4 replicas, SKU M.

### What limits it

- **CPU, not I/O caching.** Every tile reads ~10 MB, and repeating the same 240 tiles reads
  the same bytes again, so nothing is cached between requests. Doubling the CPUs (XS → S)
  doubled throughput. The service's cache settings (`store_cache_ttl`, `store_cache_size`,
  `icechunk_cache_chunk_bytes`, `icechunk_cache_chunk_ref_count`) are unset and untested.
- **Full-resolution reads at low zoom.** There's no coarser level to read from, so a z4
  tile reprojects the native 1120 × 2432 grid. Single tiles: z4 cold 9.1 s / 35.5 MB read,
  z4 repeat 1.5 s, z6 0.8 s / 5.3 MB.
- **One browser, ~6 requests.** The service only spoke HTTP/1.1, so a browser kept at
  most ~6 tiles in flight. ~28 tiles/s from one screen would need ~0.2 s per tile. (Since
  2026-10-15 the service also offers HTTP/2: see
  [Server-side caches and HTTP/2](#server-side-caches-and-http2-2026-10-15).)
- **Scale-out needs more than one browser.** At 6 in flight XS stayed at 1 replica. At 24
  and 96 it reached 3–4 replicas within about a minute, and dropped back to 1 about 2 min
  after the load stopped.
- **Queueing turns into missing tiles.** Requests waiting more than ~30 s fail with 504.
- **Cold starts.** With `min_replicas: 0`, the first request after idling waits for a
  replica to start.

## Caching: branch vs snapshot URLs

| tile URL | `Cache-Control` | `ETag` | repeat request |
|---|---|---|---|
| `.../main/tiles/...` (branch) | `public, max-age=0` | snapshot id | revalidated every time: `304` if unchanged |
| `.../<snapshot id>/tiles/...` | `public, max-age=2628000` | snapshot id | served from the browser cache, no request |

The service itself has `cache_max_age: 0` and no CLI option to change it. With branch URLs,
every tile the page asks for again (camera sway, reload) shows up as a `3xx` (304) in the
[service dashboard](https://app.earthmover.io/gtc-berlin/services/tiles-4f49272b). That was
most of the traffic in the dashboard's 10:30 bucket (731 of 969 requests).

The booth page now pins each run to the newest snapshot holding it (`ref` in `/api/state`).
Runs older than the commit history get the oldest snapshot in it. Measured in headless
Chrome on a reload: 44–74 tiles came from the browser cache with snapshot URLs, against 0
with branch URLs. The first load is unchanged.

So **load the booth page once before visitors arrive**: completed runs then play from the
browser cache. Each new run is new tiles and pays the render cost again.

## Booth page loading strategy

The page keeps one MapLibre raster source per lead at opacity 0 and switches opacity to
animate. Measured with headless Chrome (1920×1080, cold browser cache, 180 s):

| strategy | first complete frame | leads complete after 180 s |
|---|---|---|
| all 12 sources added at once | 59–180 s | 1 of 12 |
| one source at a time, in playback order | 17–25 s | 7–12 of 12 |

The page keeps 3 leads loading at once, in playback order, and adds the next lead's source
once one of them has loaded, or after 20 s if a tile hangs. The loop holds the first frame
until every lead has loaded (or timed out), so it never shows a half-drawn frame. A new init
loads behind the run on screen, which keeps looping until the new run is complete.

**Leads in flight (2026-10-15, `presentation/`, eu-west-1, SKU S pinned to 8 warm replicas).**
Local `server.py` in Chrome, 1512 × 749 px, about 20 z4 tiles per lead. Time from page load
until the loop starts, each on an init no pod or browser had loaded before:

| leads in flight | MapLibre parallel requests | until the loop starts |
|---|---|---|
| 1 (before) | 16 (default) | 13.7, 15.3, 17.5, 12.6 s (mean 14.8 s) |
| 3 (now) | 64 | 9.0, 11.3, 10.9, 7.5 s (mean 9.7 s) |

One at a time, every lead waits for its slowest tile (~1.2 s per lead, so 13 leads ≈ 15 s).
With 3 in flight those waits overlap. All 13 leads at once was no faster than 3 (9.5–13.2 s
in three runs) and the first frames arrived later. MapLibre allows only 16 image requests
in parallel by default, which would cap 3 leads × 20 tiles, so the page raises it to 64. The
service speaks HTTP/2, so Chrome's 6 connections per host don't limit this (Chrome kept up
to 64 in flight).

The phone viewer (`site/`) got the same change. Switching it to an unseen init (local copy,
same Chrome window, same 8 replicas) until all 13 frames loaded: one at a time 7.3, 6.8,
6.4, 11.5 s (mean 8.0 s); 3 in flight 4.1, 3.7, 3.7, 5.3 s (mean 4.2 s).

A run committed seconds earlier cost more: 13.8 s about a minute after its commit, and 25.9 s
when the page loaded it within seconds of the commit. That load got 34 `504`s for the first 3
leads, all within 4 s, on the brand-new snapshot. Not yet reported to Earthmover.

## Other observations

- The service logs "Treating dimension as RGB bands in positional R, G, B order; no
  attributes confirm this" for every `raster/rgb` tile. The colours are right today
  (`rgb` = `red`, `green`, `blue`), but the band order is a guess (#10).
- Camera sway changes which tiles are visible, and MapLibre cancels requests for tiles that
  leave the view before they arrive: about 1–20 per 3-minute load. That's minor next to the
  render cost.

## Region: us-east-1 vs eu-west-1 (2026-10-09)

The booth now runs on eu-west-1 only: the pipeline writes to the EU repo and the site and
presentation use `tiles-eu` (#62). The measurements below used a temporary region selector
on the site (#61) to switch between two copies of the data:

| region | tiles service | repo | storage |
|---|---|---|---|
| us-east-1 | [tiles-4f49272b](https://app.earthmover.io/gtc-berlin/services/tiles-4f49272b?period=last15Minutes) | [stormscope-eu_gtc_berlin_2026](https://app.earthmover.io/gtc-berlin/stormscope-eu_gtc_berlin_2026) | Earthmover-managed Cloudflare R2 |
| eu-west-1 | [tiles-eu-8af53d67](https://app.earthmover.io/gtc-berlin/services/tiles-eu-8af53d67?period=last15Minutes) | [stormscope-eu_gtc_berlin_2026_EU_region](https://app.earthmover.io/gtc-berlin/stormscope-eu_gtc_berlin_2026_EU_region) | S3 eu-west-1 (`nv-gtc-berlin-scratch`) |

The EU repo holds copies of the inits 08:30, 08:40 and 08:50 UTC, byte-identical to the us-east-1 ones.
Both services were on SKU S, autoscaling 0–8, restarted (SKU xs → s) right before.
Measured from Berlin with local `site/` in Chrome, 1512 × 783 px, at the page's default view.
Each run switched to an init/variable pair that pod had not served yet, with the service worker cache cleared and `cache: "no-store"` on every tile.
A warm-up run (the latest init, true colour, `no-store`) went first on each page load.
"Ready" means the page's `complete(run)` became true: all 13 frames loaded, so the animation starts.
Round trip from the test laptop to `/_meta/health`: us-east-1 ~106 ms, eu-west-1 ~33 ms.

First series:

| # | region | init (UTC), variable | ready | first frame | per frame after first | tiles | tile p50 / p90 / max |
|---|---|---|---|---|---|---|---|
| 1 | eu-west-1 | 08:30 airmass | 38.9 s | 6.4 s | 2.7 s | 221 | 1.25 / 5.07 / 9.43 s |
| 2 | us-east-1 | 08:30 airmass | 46.2 s | 7.0 s | 3.3 s | 221 | 1.93 / 3.28 / 6.70 s |
| 3 | us-east-1 | 08:40 airmass | 38.8 s | 2.9 s | 3.0 s | 221 | 1.65 / 2.64 / 3.34 s |
| 4 | eu-west-1 | 08:40 airmass | 19.0 s | 1.3 s | 1.5 s | 208 | 0.83 / 1.34 / 1.77 s |
| 5 | eu-west-1 | 08:30 true colour | 19.4 s | 1.6 s | 1.5 s | 208 | 0.90 / 1.39 / 2.28 s |
| 6 | us-east-1 | 08:30 true colour | 43.8 s | 5.3 s | 3.2 s | 220 | 1.82 / 3.17 / 5.20 s |

Second series, same day, in reverse order: both services restarted (us-east-1 SKU bounce;
eu-west-1 had scaled to zero), local server restarted, and the warm-up also sent with `no-store`:

| # | region | init (UTC), variable | ready | first frame | per frame after first | tiles | tile p50 / p90 / max |
|---|---|---|---|---|---|---|---|
| 1 | us-east-1 | 08:30 true colour | 45.7 s | 2.6 s | 3.6 s | 260 | 1.75 / 3.12 / 4.27 s |
| 2 | eu-west-1 | 08:30 true colour | 40.9 s | 2.4 s | 3.2 s | 221 | 1.26 / 2.17 / 15.08 s |
| 3 | eu-west-1 | 08:40 airmass | 24.6 s | 4.2 s | 1.7 s | 221 | 1.00 / 1.75 / 4.12 s |
| 4 | us-east-1 | 08:40 airmass | 47.0 s | 7.2 s | 3.3 s | 221 | 1.97 / 3.38 / 7.00 s |
| 5 | us-east-1 | 08:30 airmass | 43.5 s | 3.0 s | 3.4 s | 221 | 1.80 / 3.07 / 4.24 s |
| 6 | eu-west-1 | 08:30 airmass | 23.8 s | 1.7 s | 1.8 s | 221 | 1.03 / 1.67 / 2.93 s |

- us-east-1 is steady: 39–47 s to ready in all six runs, 3.0–3.6 s per frame.
- eu-west-1 is ~2× faster per frame (1.5–1.8 s) and per tile (p50 0.8–1.3 s vs 1.65–1.97 s).
  Without a stall it is ready in 19–25 s.
- But 2 of the 6 EU runs had one 9–15 s tile that held up a frame (first series run 1,
  second series run 2), which pushed them to 39–41 s, as slow as us-east-1. No US run had
  a tile over 7 s. The cause is a guess: a new pod taking requests while still cold when
  the autoscaler scales out. Pinning `min_replicas` would test it.
- The ~70 ms extra round trip explains only ~3 s of that (221 tiles over Chrome's 6 HTTP/1.1
  connections ≈ 37 round trips). The rest is the service reading its data: the EU pod sits
  next to its S3 bucket, while the us-east-1 pod reads from R2. This setup can't separate
  storage from compute region.
- First-series run 1 also had its warm-up sent without `no-store`, so the pod may not have
  been fully warm. Second-series run 2 had a proper warm-up and stalled anyway, so the
  stall looks like the main cause.
- One run per row. Tile counts vary from 208 to 260 per run at the same viewport, in both
  regions; not investigated.

## Server-side caches and HTTP/2 (2026-10-15)

### From Earthmover (Slack, #C0BR6535GBD)

Samantha Hughes on the per-pod caches, from Earthmover's traces of requests for this dataset
on the eu-west-1 pods
([thread](https://earthmoverworkspace.slack.com/archives/C0BR6535GBD/p1791562430384989?thread_ts=1791474690.734569&cid=C0BR6535GBD)):

> Cold steps, and how long each result stays cached:
> - Open repo: ~335 ms. Kept 1 hour, max 2 repos per pod.
> - Open a dataset version: ~440 ms. Kept until pushed out by newer ones (32 per pod) or the
>   hourly repo reset. Every new commit needs a fresh open.
> - Check a branch for new commits: ~60 ms. Rechecked every 60 s, so new data shows up within
>   a minute.
> - Read a chunk: ~20 ms from storage vs ~2.5 ms from memory. Memory cache is sized to the pod
>   and cleared with the hourly repo reset.
>
> Browser caching:
> - Branch URLs (e.g. `main`): the browser asks again on every view. The service looks up which
>   snapshot the branch currently points to; if it's the same one the browser already has, it
>   replies "unchanged" (304) without re-rendering. That's ~50 ms, but still a network round
>   trip each time.
> - Snapshot ID URLs: a snapshot can never change, so the browser keeps the tile for a month
>   and doesn't ask at all. Repeat views are instant, and first views skip the branch lookup on
>   the server too. Use snapshot IDs when you can.
>
> Each pod has its own cache. New pods, scale-ups, and scale-from-zero start fully cold.

And on bypassing the caches
([thread](https://earthmoverworkspace.slack.com/archives/C0BR6535GBD/p1791570407454749?thread_ts=1791483071.468579&cid=C0BR6535GBD)):

> ```
> curl -sI -H "X-Flux-Cache-Bypass: all" "$URL" | grep -i cache-status
> ```
> Deployed this for y'all, you can bypass most of the cache and get a header with hit/miss for
> them too.

Each tile response now carries `cache-status` (one entry per cache: `flux-repo`,
`flux-credentials`, `flux-ref`, `flux-dataset`, `flux-tiles_grid`, each `hit`, `fwd=miss` or
`fwd=bypass`), `x-flux-inbound-bytes` / `x-flux-inbound-chunks` (data read from storage for
this tile), `x-keda-http-cold-start` and `x-multiscale-level`.

### Measured

A Python script (`httpx`) loaded one full run from `tiles-eu`: 13 leads × the 20 z4 booth
tiles = 260 tiles, `airmass`, in playback order, with N requests in flight. It ran from Berlin
on 2026-10-15, with every other viewer of the service closed. Each run used an init nobody had
requested before (hourly inits of 2026-10-10/11), so no pod held those chunks. The cases ran
interleaved, three rounds. `x-keda-http-cold-start` was `false` for every tile, so the pods
were already up. Service: SKU S (2 CPU, 4 Gi), autoscaling 0–8, cache settings unset; a few
minutes after the tests it had 2 ready replicas (not polled during them).

| case | full run | first frame | tile p50 / p90 | read from storage per tile |
|---|---|---|---|---|
| `main`, 6 in flight, HTTP/2 | 21.9, 25.5, 25.9 s | 2.1–4.3 s | 0.41–0.54 / 0.84–0.98 s | 2.4–2.6 MB |
| `main`, 6 in flight, HTTP/1.1 | 17.4, 21.1, 24.5 s | 1.8–2.2 s | 0.37–0.53 / 0.66–0.97 s | 2.4 MB |
| snapshot id, 6 in flight, HTTP/2 | 22.7, 25.2, 33.8 s | 2.0–5.7 s | 0.42–0.60 / 0.77–1.31 s | 2.4–2.6 MB |
| `main`, 20 in flight, HTTP/2 | 10.0, 21.7, 37.9 s | 1.3–8.8 s | 0.68–2.46 / 1.09–4.72 s | 2.4–2.5 MB |
| `main`, 24 in flight, HTTP/2 | 9.5, 16.7, 20.5 s | 1.2–3.0 s | 0.77–1.44 / 1.30–3.21 s | 2.4 MB |
| `main`, all 260 at once, HTTP/2 | 10.1, 18.9, 30.8 s | 3.9–18.6 s | 5.1–15.2 / 9.5–27.6 s | 2.4–2.5 MB |
| `main`, 6 in flight, `X-Flux-Cache-Bypass: all` | 108.6, 130.5, 143.1 s | 8.8–26.3 s | 2.4–3.1 / 3.3–4.5 s | 11.3 MB |

An earlier series the same morning, with other viewers still open, gave the same picture
(6 in flight 18–26 s, 24 in flight 9–19 s, bypass 103–185 s).

Then the same matrix with the service pinned to `min = max = 8` replicas, after a warm-up of
2 × 260 tiles. All 8 replicas stayed ready throughout (polled every 30 s):

| case (8 replicas) | full run | first frame | tile p50 / p90 / max | read from storage per tile |
|---|---|---|---|---|
| `main`, 6 in flight, HTTP/2 | 18.6, 19.4, 19.7 s | 1.8–2.3 s | 0.41–0.42 / 0.63–0.72 / ≤1.5 s | 2.3–2.4 MB |
| `main`, 6 in flight, HTTP/1.1 | 17.6, 17.9, 18.2 s | 1.6–2.0 s | 0.37–0.38 / 0.66–0.70 / ≤1.4 s | 2.3–2.4 MB |
| snapshot id, 6 in flight, HTTP/2 | 18.4, 18.6, 18.8 s | 1.7–1.9 s | 0.40–0.42 / 0.64–0.67 / ≤1.0 s | 2.3–2.6 MB |
| `main`, 20 in flight, HTTP/2 | 9.5, 10.2, 10.3 s | 1.2–1.4 s | 0.68–0.75 / 0.94–1.08 / ≤1.7 s | 2.3–2.4 MB |
| `main`, 24 in flight, HTTP/2 | 9.9, 10.5, 10.6 s | 1.6–2.0 s | 0.84–0.91 / 1.20–1.31 / ≤2.0 s | 2.3 MB |
| `main`, all 260 at once, HTTP/2 | 8.2, 10.4, 10.9 s | 3.4–9.9 s | 3.6–6.0 / 7.4–10.4 / ≤10.9 s | 2.3–2.4 MB |
| `main`, 6 in flight, `X-Flux-Cache-Bypass: all` | 96.4, 98.5, 98.7 s | 8.2–10.7 s | 2.1–2.2 / 2.8–3.0 / ≤4.2 s | 11.2–11.3 MB |

- **Pinning 8 replicas removes the spread.** On autoscaling 0–8 the same case varied up to
  4× (20 in flight: 10–38 s) and single tiles took up to 17 s. On 8 pinned replicas every
  case repeated within ~10 %, and no tile took more than 2 s (outside the all-at-once case).
  The likely cause of the spread is new pods starting with an empty cache when the autoscaler
  scales out (see Earthmover's note above). So for the booth: pin `min_replicas`.
- **With 8 replicas a full run takes ~10 s at 20 in flight**, the doc's old target, against
  ~18 s at 6. Per tile, 6 in flight is client-bound: ~0.4 s × 260 / 6 ≈ 18 s.
- **The pod's chunk cache is what makes tiles fast.** With the cache bypassed, every tile read
  ~11 MB from storage and a run took 4–6× longer. With the cache on, neighbouring tiles and
  later leads reuse chunks, and a tile reads only ~2.4 MB. That overturns "nothing is cached
  between requests" under [What limits it](#what-limits-it), which was measured on us-east-1
  before these caches existed or were visible. It also means every new pod (scale-out, scale
  from zero, the hourly reset) pays the uncached cost at first.
- **The service speaks HTTP/2 now, and the booth page uses it.** In Chrome the
  [mobile page](https://nvidia-gtc-berlin-2026-stormscopeeu.vercel.app/) kept up to 16 tiles
  in flight to the service; HTTP/1.1 would cap that at 6. (Chrome hides the protocol name for
  these cross-origin tiles, so this is inferred from the count.) It had 12 of 13 frames after
  30 s.
- **On autoscaling 0–8, more in flight helps, but not reliably.** The best runs with 20–24 in flight took ~10 s,
  the doc's old target, against ~22 s with 6. But the spread is wide (9.5–38 s), so a few slow
  tiles still decide when the loop starts. All 260 at once delays the first frame, because the
  service renders requests in any order: load in playback order, a lead at a time, as the page
  does. At the same 6 in flight, HTTP/2 was no faster than HTTP/1.1.
- **The branch lookup is cheap.** On `main`, about 1 tile in 100 shows `flux-ref; fwd=miss`
  (the 60 s recheck), and the rest hit. Snapshot URLs remove that lookup but were not faster
  here either (compare PR #67). Their benefit is the month-long browser cache.

## Options, by expected impact

1. **Pyramids (#9):** a z3/z4 tile from a 2× or 4× coarser level reads 4–16× less data.
   This is the only option that shortens a single tile. Untested.
2. **Bigger SKU:** helps in proportion to CPUs (S ≈ 2× XS). Costs credits per hour.
3. **Warm-up before doors open:** load the booth page once per completed run, so later
   reloads come from the browser cache.
4. **Service cache settings:** might stop repeat requests from re-reading all their data.
   Untested.
5. **Pin replicas (`min = max = 8`) during booth hours:** each pod keeps its own cache, and
   pods the autoscaler adds start empty. Pinned and warmed up, one browser gets a full run in
   ~10 s, every time (measured 2026-10-15 on `tiles-eu`). Set min back to 0 after hours, since
   every replica bills while it runs. Warm the pods up after pinning: load the booth page or
   one run before visitors arrive.

## Reproduce

```bash
# check the service shape and replicas
uvx --from arraylake al compute status gtc-berlin tiles-eu-8af53d67
# booth hours: pin 8 replicas (all ready ~20 s later)
uvx --from arraylake al compute update gtc-berlin tiles-eu-8af53d67 --min-replicas 8 --max-replicas 8
# after hours: back to autoscaling
uvx --from arraylake al compute update gtc-berlin tiles-eu-8af53d67 --min-replicas 0 --max-replicas 8
# restart (fresh, cold pods) before a benchmark
uvx --from arraylake al compute update gtc-berlin tiles-eu-8af53d67 --sku xs
uvx --from arraylake al compute update gtc-berlin tiles-eu-8af53d67 --sku s
# cache hit/miss for one tile; add -H "X-Flux-Cache-Bypass: all" to skip the caches
curl -s -D - -o /dev/null "$TILE_URL" | grep -i "cache-status\|x-flux-inbound"
```

Each SKU bills while it runs. The service is deliberately public and deliberately disabled
between events: keep both after any change.
