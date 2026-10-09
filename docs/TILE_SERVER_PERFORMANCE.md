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
- **One browser, ~6 requests.** The service only speaks HTTP/1.1, so a browser keeps at
  most ~6 tiles in flight. ~28 tiles/s from one screen would need ~0.2 s per tile.
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

The page adds the next lead's source once the previous one has loaded, or after 20 s if a
tile hangs. The loop holds the first frame until every lead has loaded (or timed out), so it
never shows a half-drawn frame. A new init loads behind the run on screen, which keeps
looping until the new run is complete.

## Other observations

- The service logs "Treating dimension as RGB bands in positional R, G, B order; no
  attributes confirm this" for every `raster/rgb` tile. The colours are right today
  (`rgb` = `red`, `green`, `blue`), but the band order is a guess (#10).
- Camera sway changes which tiles are visible, and MapLibre cancels requests for tiles that
  leave the view before they arrive: about 1–20 per 3-minute load. That's minor next to the
  render cost.

## Region: us-east-1 vs eu-west-1 (2026-10-09)

The site's region selector (`?region=`) switches between two copies of the data:

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

| # | region | init (UTC), variable | ready | first frame | per frame after first | tiles | tile p50 / p90 / max |
|---|---|---|---|---|---|---|---|
| 1 | eu-west-1 | 08:30 airmass | 38.9 s | 6.4 s | 2.7 s | 221 | 1.25 / 5.07 / 9.43 s |
| 2 | us-east-1 | 08:30 airmass | 46.2 s | 7.0 s | 3.3 s | 221 | 1.93 / 3.28 / 6.70 s |
| 3 | us-east-1 | 08:40 airmass | 38.8 s | 2.9 s | 3.0 s | 221 | 1.65 / 2.64 / 3.34 s |
| 4 | eu-west-1 | 08:40 airmass | 19.0 s | 1.3 s | 1.5 s | 208 | 0.83 / 1.34 / 1.77 s |
| 5 | eu-west-1 | 08:30 true colour | 19.4 s | 1.6 s | 1.5 s | 208 | 0.90 / 1.39 / 2.28 s |
| 6 | us-east-1 | 08:30 true colour | 43.8 s | 5.3 s | 3.2 s | 220 | 1.82 / 3.17 / 5.20 s |

- Warm, eu-west-1 gets the animation going in ~19 s; us-east-1 needs 39–46 s, so EU is about 2× faster.
  Every tile is faster, not only the first one: p50 0.85 s vs 1.8 s.
- The ~70 ms extra round trip explains only ~3 s of that (221 tiles over Chrome's 6 HTTP/1.1
  connections ≈ 37 round trips). The rest is the service reading its data: the EU pod sits
  next to its S3 bucket, while the us-east-1 pod reads from R2. This setup can't separate
  storage from compute region.
- Run 1 is slower than runs 4–5. It was the first run after the restart, and its warm-up
  went out without `no-store`, so the browser's HTTP cache may have answered part of it and
  left the pod not fully warm. One 9.4 s tile stalled frame 8.
- One run per row, so expect noise. The EU page drew 208 tiles against 220–221 for us-east-1
  at the same viewport; not investigated.

## Options, by expected impact

1. **Pyramids (#9):** a z3/z4 tile from a 2× or 4× coarser level reads 4–16× less data.
   This is the only option that shortens a single tile. Untested.
2. **Bigger SKU:** helps in proportion to CPUs (S ≈ 2× XS). Costs credits per hour.
3. **Warm-up before doors open:** load the booth page once per completed run, so later
   reloads come from the browser cache.
4. **Service cache settings:** might stop repeat requests from re-reading all their data.
   Untested.
5. **More replicas / `min_replicas: 1`:** removes cold starts and helps with several screens
   or phones, not with one browser.

## Reproduce

```bash
# check the service shape and replicas
uvx --from arraylake al compute status gtc-berlin tiles-4f49272b
# resize for a test, then restore the booth settings (S, 8 replicas; min 0 outside booth hours)
uvx --from arraylake al compute update gtc-berlin tiles-4f49272b --sku xs
uvx --from arraylake al compute update gtc-berlin tiles-4f49272b --sku s --min-replicas 8 --max-replicas 8
```

Each SKU bills while it runs. The service is deliberately public and deliberately disabled
between events: keep both after any change.
